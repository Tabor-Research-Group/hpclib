"""Tests for hpclib/servers/agent_console.py, against a real REST server
(fake SLURM, from test_rest_jobs) reached through a profile's port.

Run with:  python -m unittest tests.test_agent_console -v
"""
import contextlib
import http.server
import io
import json
import os
import socket
import subprocess
import sys
import textwrap
import time
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_rest_jobs import OWNER, JobServerTestCase  # noqa: E402
import agent_console  # noqa: E402
import agent_profiles  # noqa: E402

KEY = "console-key"
SPEC = {"description": "Run xtb on an xyz file", "parameters": {"xyz": {"type": "path", "kind": "file"}},
        "resources": {"time": "00:10:00", "mem": "1G", "cpus_per_task": 1}}
SCRIPT = "#!/bin/bash\nset -euo pipefail\nxtb \"$HPC_PARAM_XYZ\" --opt\n"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeShell:
    """Stands in for `bash -c '. hpclib.sh; agent_tunnel ...'`."""
    def __init__(self):
        self.calls = []
        self.setup_status = "missing not installed: no image at /x/vscode.sif"   # what tunnel_setup --check says
        self.stop_answer = None                                                  # what --stop-instance says
        self.smb_status = {"host": "files.example.edu", "user": "me", "auth": "auto", "kinit": "host",
                           "rclone_kerberos": True, "ticket": None, "credentials": False, "image": True}

    def __call__(self, args, log):
        self.calls.append(list(args))
        log.write(f"ran {' '.join(args)}\n".encode())
        if args[0] == "tunnel_setup":   # like tunnels/setup_tunnel.sh
            if "--save" in args:
                log.write(f"saved {args.count('--set')} setting(s) for the tunnel\n".encode())
            if "--check" in args:
                log.write(f"HPCLIB_TUNNEL_STATUS {self.setup_status}\n".encode())
            if "--instances" in args:
                log.write(b"HPCLIB_TUNNEL_INSTANCE 152400 chem-entr-c04 3104 RUNNING maboyer\n")
            if "--stop-instance" in args:
                job = args[args.index("--stop-instance") + 1]
                log.write((self.stop_answer or f"HPCLIB_TUNNEL_INSTANCE_STOPPED {job}").encode() + b"\n")
        if args[0] == "smbshell" and "status" in args:      # like tunnels/data-transfer/smbshell.sh status --json
            log.write(("HPCLIB_SMB_STATUS " + json.dumps(self.smb_status) + "\n").encode())
        return subprocess.Popen(["sleep", "30" if args[0] in ("agent_tunnel", "launch_tunnel") else "0"], start_new_session=True)


class ConsoleTestCase(JobServerTestCase):

    def setUp(self):
        super().setUp()
        self.agents = self.tmp / "agents"
        self.console = self.tmp / "console"
        env = mock.patch.dict(os.environ, {"HPCLIB_AGENTS_DIR": str(self.agents),
                                           "HPCLIB_CONSOLE_DIR": str(self.console)})
        env.start()
        self.addCleanup(env.stop)
        self.live = self.make_profile("me@live.example.edu", self.server.server_address[1],
                                      owner=OWNER, agent=self.llm_token)
        self.dead = self.make_profile("me@dead.example.edu", free_port(), owner=None, agent="x")
        self.shell = FakeShell()
        self.clusters = agent_console.Clusters(shell_runner=self.shell, timeout=10)
        self.console_server = agent_console.ConsoleServer(("127.0.0.1", 0), KEY, self.clusters,
                                                          allow_origin="http://127.0.0.1:5173",
                                                          static_dir=str(self.static()))
        self.console_server.RequestHandlerClass.log_message = lambda *a: None
        threading.Thread(target=self.console_server.serve_forever, daemon=True).start()
        self.addCleanup(self.console_server.server_close)
        self.addCleanup(self.console_server.shutdown)
        self.addCleanup(self.stop_fakes)
        self.base = f"http://127.0.0.1:{self.console_server.port}"

    def stop_fakes(self):
        for proc in self.clusters.procs.values():
            proc.kill()
            proc.wait()

    def static(self):
        d = self.tmp / "ui"
        d.mkdir()
        (d / "index.html").write_text("<!doctype html><title>console</title>")
        (d / "app.js").write_text("console.log(1)")
        return d

    def make_profile(self, host, port, owner, agent):
        name = agent_profiles.name_for(host)
        with mock.patch.object(agent_profiles, "random_port", return_value=port), \
                contextlib.redirect_stdout(io.StringIO()):
            agent_profiles.cmd_init(name, host)
        d = Path(agent_profiles.profile_dir(name))
        for fname, token in (("owner_token", owner), ("agent_token", agent)):
            if token:
                (d / fname).write_text(token + "\n")
                (d / fname).chmod(0o600)
        (d / "mcp.json").write_text(json.dumps({"mcpServers": {"hpclib-x": {"command": "python3"}}}))
        agent_profiles.cmd_set(name, f"port={port}", "work_dirs=/scratch/me/llm")
        return name

    def call(self, verb, route, body=None, key=KEY, headers=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"} if data else {}
        if key:
            hdrs["Authorization"] = f"Bearer {key}"
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + route, data=data, method=verb, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                status, content, out_headers = res.status, res.read(), res.headers
        except urllib.error.HTTPError as e:
            status, content, out_headers = e.code, e.read(), e.headers
        if raw:
            return status, content, out_headers
        return status, json.loads(content) if content else {}


class TestSecurity(ConsoleTestCase):

    def test_key_required(self):
        self.assertEqual(self.call("GET", "/api/health", key=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/health", key="wrong")[0], 401)
        self.assertEqual(self.call("GET", "/api/health")[0], 200)

    def test_host_header(self):
        status, _ = self.call("GET", "/api/health", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 421)
        port = self.console_server.port
        self.assertEqual(self.call("GET", "/api/health", headers={"Host": f"localhost:{port}"})[0], 200)

    def test_cors_only_for_the_allowed_origin(self):
        _, _, headers = self.call("GET", "/api/health", raw=True, headers={"Origin": "http://127.0.0.1:5173"})
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:5173")
        _, _, headers = self.call("GET", "/api/health", raw=True, headers={"Origin": "http://evil.example"})
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))
        status, _, headers = self.call("OPTIONS", "/api/clusters", key=None, raw=True,
                                       headers={"Origin": "http://127.0.0.1:5173"})
        self.assertEqual(status, 204)
        self.assertIn("Authorization", headers.get("Access-Control-Allow-Headers"))
        self.assertEqual(self.call("OPTIONS", "/api/clusters", key=None, raw=True,
                                   headers={"Origin": "http://evil.example"})[0], 403)

    def test_no_token_values(self):
        status, out = self.call("GET", "/api/clusters")
        self.assertEqual(status, 200)
        text = json.dumps(out)
        for secret in (OWNER, self.llm_token):
            self.assertNotIn(secret, text)
        self.assertNotIn("token_file", text)

    def test_static_needs_no_key_and_stays_inside(self):
        status, content, headers = self.call("GET", "/", key=None, raw=True)
        self.assertEqual((status, headers.get("Content-Type")), (200, "text/html"))
        self.assertEqual(self.call("GET", "/app.js", key=None, raw=True)[1], b"console.log(1)")
        self.assertIn(b"<title>console", self.call("GET", "/jobs/123", key=None, raw=True)[1])  # SPA fallback
        self.assertNotIn(b"secret", self.call("GET", "/../agents/x", key=None, raw=True)[1])

    def test_serves_the_bundled_front_end(self):
        ui = Path(__file__).resolve().parents[1] / "agent-console"
        self.console_server.static_dir = str(ui)
        status, content, _ = self.call("GET", "/", key=None, raw=True)
        self.assertIn(b'src="app.js"', content)
        status, content, headers = self.call("GET", "/app.js", key=None, raw=True)
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type"))  # module scripts need a JS type

    def test_bad_names_and_routes(self):
        self.assertEqual(self.call("GET", "/api/clusters/..%2Fetc")[0], 400)
        self.assertEqual(self.call("GET", "/api/clusters/nobody@nowhere")[0], 404)
        self.assertEqual(self.call("GET", f"/api/clusters/{self.live}/rest/../health")[0] in (400, 404), True)
        self.assertEqual(self.call("GET", f"/api/clusters/{self.live}/rest/a%20b")[0], 400)

    def test_args(self):
        with self.assertRaises(SystemExit):
            agent_console.parse_args(["--allow-origin", "*"])
        self.assertEqual(agent_console.parse_args(["--port", "1234"]).port, 1234)


class TestClusters(ConsoleTestCase):

    def test_list(self):
        status, out = self.call("GET", "/api/clusters")
        by_name = {c["name"]: c for c in out["clusters"]}
        self.assertEqual(set(by_name), {self.live, self.dead})
        live, dead = by_name[self.live], by_name[self.dead]
        self.assertEqual((live["tunnel"]["state"], live["tunnel"]["token"]), ("up", "owner"))
        self.assertTrue(live["has_owner_token"])
        self.assertEqual(dead["tunnel"]["state"], "down")
        self.assertFalse(dead["has_owner_token"])
        self.assertEqual(live["work_dirs"], ["/scratch/me/llm"])

    def test_one_and_by_host(self):
        status, out = self.call("GET", "/api/clusters/me@live.example.edu")
        self.assertEqual((status, out["name"]), (200, self.live))
        self.assertEqual(self.call("GET", f"/api/clusters/{self.live}/mcp")[1]["mcp"]["mcpServers"]["hpclib-x"],
                         {"command": "python3"})

    def test_tunnel_start_stop_log(self):
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/tunnel/start")[0], 409)   # no login yet
        self.clusters.logins.alive = lambda profile: True
        status, out = self.call("POST", f"/api/clusters/{self.dead}/tunnel/start",
                                {"auto_approve_templates": "new"})
        self.assertEqual(status, 202, out)
        self.assertEqual(self.shell.calls[-1], ["agent_tunnel", self.dead, "--auto-approve-templates=new"])
        state = self.call("GET", f"/api/clusters/{self.dead}")[1]["tunnel"]
        self.assertEqual((state["state"], state["started_here"]), ("starting", True))
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/tunnel/start")[0], 409)
        self.assertEqual(self.call("POST", f"/api/clusters/{self.live}/tunnel/start")[0], 409)  # port in use
        status, out = self.call("POST", f"/api/clusters/{self.dead}/tunnel/stop")
        self.assertEqual((status, out["agent_stop_exit"]), (200, 0))
        self.assertEqual(self.shell.calls[-1], ["agent_stop", self.dead])
        log = self.call("GET", f"/api/clusters/{self.dead}/tunnel/log")[1]["lines"]
        self.assertTrue(any("ran agent_tunnel" in line for line in log))
        self.assertTrue(any("agent_stop" in line for line in log))
        self.assertEqual(self.call("GET", f"/api/clusters/{self.dead}")[1]["tunnel"]["state"], "down")
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/tunnel/start",
                                   {"auto_approve_templates": "yes"})[0], 400)


class TestSettings(ConsoleTestCase):

    def test_tunnel_settings(self):
        status, out = self.call("GET", f"/api/clusters/{self.live}/settings")
        self.assertEqual((status, out["auto_approve_templates"], out["tunnel_args"]), (200, "all", []))
        status, out = self.call("PUT", f"/api/clusters/{self.live}/settings",
                                {"auto_approve_templates": "review", "tunnel_args": ["--time=12:00:00", "--mem=2gb"]})
        self.assertEqual(status, 200, out)
        profile = agent_profiles.load(self.live)
        self.assertEqual((profile["auto_approve_templates"], profile["tunnel_args"]),
                         ("review", ["--time=12:00:00", "--mem=2gb"]))
        self.assertEqual(profile["port"], self.server.server_address[1])          # the rest is kept
        for bad in ({"auto_approve_templates": "yes"}, {"tunnel_args": ["--wrap=rm -rf ~"]},
                    {"tunnel_args": "--time=1:00:00"}, {"port": 1}):
            self.assertIn(self.call("PUT", f"/api/clusters/{self.live}/settings", bad)[0], (400, 422), bad)
        self.assertEqual(agent_profiles.load(self.live)["tunnel_args"], ["--time=12:00:00", "--mem=2gb"])

    def test_agent_tunnel_reads_them(self):
        repo = Path(__file__).resolve().parents[1]
        agent_profiles.cmd_set(self.live, "auto_approve_templates=new", "tunnel_args=--time=12:00:00",
                               "connection_hours=6")
        script = (f'source {repo / "hpclib" / "hpclib.sh"}; '
                  f'launch_tunnel() {{ printf "%s %s\\n" "$HPCLIB_SSH_PERSIST" "$*"; }}; '
                  f'agent_tunnel {self.live}; agent_tunnel {self.live} --review-templates --time=2:00:00; '
                  f'echo "after: $HPCLIB_SSH_PERSIST"')
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, HPCLIB_AGENTS_DIR=str(self.agents)))
        first, second, after = res.stdout.splitlines()
        self.assertTrue(first.startswith("6h "), first)                            # the login hours, for pssh
        self.assertEqual(after, "after: 12h")                                      # only for that call
        self.assertIn("--time=12:00:00 --", first)
        self.assertTrue(first.endswith("--auto-approve-templates=new"), first)
        self.assertIn("--time=12:00:00 --time=2:00:00 --", second)                # the later one wins in sbatch
        self.assertNotIn("auto-approve", second)

    def test_server_config_through_the_proxy(self):
        path = self.data / "rest" / "config.json"
        path.write_text("{}")
        path.chmod(0o600)
        self.server.config_path = str(path)
        status, out = self.call("GET", f"/api/clusters/{self.live}/rest/admin/config")
        self.assertEqual((status, out["config"]), (200, {}))
        status, out = self.call("PUT", f"/api/clusters/{self.live}/rest/admin/config",
                                {"changes": {"environments": {"modules": ["WebProxy"]}}})
        self.assertEqual(status, 200, out)
        self.assertEqual(json.loads(path.read_text()), {"environments": {"modules": ["WebProxy"]}})
        self.assertEqual(self.call("PUT", f"/api/clusters/{self.live}/rest/admin/config?as=agent",
                                   {"changes": {"cluster_notes": "x"}})[0], 403)


FAKE_SSH_LOGIN = textwrap.dedent("""\
    #!/usr/bin/env python3
    # Stands in for ssh: -O check/exit on the master, or a login that asks through SSH_ASKPASS.
    import os, subprocess, sys, time
    args = sys.argv[1:]
    state = os.environ["FAKE_SSH_DIR"]
    host = args[-1]
    master = os.path.join(state, "master-" + host)
    if "-O" in args:
        op = args[args.index("-O") + 1]
        if op == "check":
            sys.exit(0 if os.path.exists(master) else 255)
        if op == "exit" and os.path.exists(master):
            os.remove(master)
            sys.exit(0)
        sys.exit(255)
    with open(os.path.join(state, "args"), "w") as f:
        f.write(" ".join(args))
    def ask(prompt):
        res = subprocess.run([os.environ["SSH_ASKPASS"], prompt], capture_output=True, text=True)
        return res.returncode, res.stdout.rstrip("\\n")
    mode = os.environ.get("FAKE_SSH_MODE", "duo")
    if mode == "hostkey":
        ask("Are you sure you want to continue connecting (yes/no/[fingerprint])? ")
        sys.exit(255)
    if mode == "odd":
        ask("Favourite colour: ")
        sys.exit(255)
    if mode == "duo":
        code, answer = ask("(me@" + host + ") Password: ")
        if code or answer != os.environ["FAKE_SSH_PASSWORD"]:
            ask("(me@" + host + ") Password: ")
            print("Permission denied (keyboard-interactive).", file=sys.stderr)
            sys.exit(255)
        code, answer = ask("Duo two-factor login for me\\n\\nEnter a passcode or select one of the following "
                           "options:\\n\\n 1. Duo Push to XXX-XXX-1234\\n\\nPasscode or option (1-1): ")
        if code or answer != "1":
            sys.exit(255)
        time.sleep(float(os.environ.get("FAKE_PUSH_SECONDS", "0.6")))   # the phone
    open(master, "w").close()
    sys.exit(0)
""")


class TestLogin(ConsoleTestCase):

    def setUp(self):
        super().setUp()
        self.ssh_dir = self.tmp / "fake-ssh"
        self.ssh_dir.mkdir()
        fake = self.tmp / "bin" / "fake-ssh"
        fake.write_text(FAKE_SSH_LOGIN)
        fake.chmod(0o755)
        env = mock.patch.dict(os.environ, {"FAKE_SSH_DIR": str(self.ssh_dir), "FAKE_SSH_PASSWORD": "hunter2",
                                           "HOME": str(self.tmp / "home")})
        env.start()
        self.addCleanup(env.stop)
        self.clusters.logins = agent_console.Logins(ssh=str(fake))
        self.host = agent_profiles.load(self.dead)["host"]

    def login(self, body, until=("connected", "failed", "expired")):
        status, out = self.call("POST", f"/api/clusters/{self.dead}/login", body)
        self.assertIn(status, (202, 409), out)
        seen = [out.get("state")]
        for _ in range(60):
            out = self.call("GET", f"/api/clusters/{self.dead}/login")[1]
            seen.append(out.get("state"))
            if out.get("state") in until:
                break
            time.sleep(0.1)
        return out, seen

    def test_password_then_push(self):
        out, seen = self.login({"password": "hunter2"})
        self.assertEqual((out["state"], out["connected"]), ("connected", True), out)
        self.assertIn("push_sent", seen)
        args = (self.ssh_dir / "args").read_text()
        self.assertIn("-M -N -f -o ControlPersist=12h", args)
        self.assertIn("ControlPath=~/.ssh/connections/%r@%h:%p", args)     # the socket pssh reuses
        self.assertTrue((self.tmp / "home" / ".ssh" / "connections").is_dir())
        listed = {c["name"]: c for c in self.call("GET", "/api/clusters")[1]["clusters"]}
        self.assertEqual(listed[self.dead]["login"]["state"], "connected")
        # the password is nowhere it shouldn't be
        for path in (self.console / "logs").iterdir():
            self.assertNotIn("hunter2", path.read_text())
        self.assertNotIn("hunter2", json.dumps(out))
        self.assertFalse(any(a.get("password") for a in self.clusters.logins.attempts.values()))
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/logout")[1]["logged_out"], True)
        self.assertEqual(self.call("GET", f"/api/clusters/{self.dead}/login")[1]["state"], "none")

    def test_wrong_or_missing_password(self):
        out, _ = self.login({"password": "wrong"})
        self.assertEqual((out["state"], out["message"]), ("failed", "the password was not accepted"))
        out, _ = self.login({})
        self.assertEqual(out["state"], "failed")
        self.assertIn("asks for a password", out["message"])
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/login", {"password": "a\nb"})[0], 400)

    def test_prompts_it_does_not_answer(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_MODE": "hostkey"}):
            out, _ = self.login({"password": "hunter2"})
        self.assertIn("connect once in a terminal", out["message"])
        with mock.patch.dict(os.environ, {"FAKE_SSH_MODE": "odd"}):
            out, _ = self.login({"password": "hunter2"})
        self.assertEqual((out["state"], out["prompt"]), ("failed", "Favourite colour:"))

    def test_keys_and_existing_login(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_MODE": "keys"}):
            out, _ = self.login({})
        self.assertEqual(out["state"], "connected")
        (self.ssh_dir / "args").unlink()
        status, out = self.call("POST", f"/api/clusters/{self.dead}/login", {"password": "hunter2"})
        self.assertEqual(out["state"], "connected")
        self.assertFalse((self.ssh_dir / "args").exists())                  # no second ssh

    def test_stored_login_includes_the_host(self):
        # setup_agents stores its whole login, the host included
        agent_profiles.cmd_set(self.dead, "login=-p", "login+=2222", f"login+={self.host}")
        out, _ = self.login({"password": "hunter2"})
        self.assertEqual(out["state"], "connected", out)
        args = (self.ssh_dir / "args").read_text().split()
        self.assertEqual(args.count(self.host), 1)
        self.assertEqual(args[:2], ["-p", "2222"])
        self.assertEqual(args[-1], self.host)

    def test_login_hours_setting(self):
        self.call("PUT", f"/api/clusters/{self.dead}/settings", {"connection_hours": 6})
        self.assertEqual(self.call("PUT", f"/api/clusters/{self.dead}/settings", {"connection_hours": 0})[0], 422)
        self.login({"password": "hunter2"})
        self.assertIn("ControlPersist=6h", (self.ssh_dir / "args").read_text())

    def test_askpass_needs_the_attempts_nonce(self):
        logins = self.clusters.logins
        self.assertEqual(logins.handle_prompt("made-up", "Password:"), {"error": "no login in progress"})
        self.assertEqual(logins.handle_prompt(None, "Password:"), {"error": "no login in progress"})


class TestManage(TestLogin):
    """Adding clusters, and install_hpclib / setup_agents over the console's login."""

    def wait_op(self, name):
        for _ in range(50):
            out = self.call("GET", f"/api/clusters/{name}/operation")[1]
            if out["state"] != "running":
                return out
            time.sleep(0.1)
        return out

    def test_add_cluster(self):
        status, out = self.call("POST", "/api/clusters", {"host": "me@grace.example.edu", "port": 2222,
                                                         "jump": "me@gateway.example.edu"})
        self.assertEqual(status, 201, out)
        profile = agent_profiles.load(out["name"])
        self.assertEqual(profile["login"], ["-p", "2222", "-J", "me@gateway.example.edu", "me@grace.example.edu"])
        self.assertFalse(out["set_up"])
        self.assertEqual(agent_console.ssh_options(profile), ["-p", "2222", "-J", "me@gateway.example.edu"])
        self.assertEqual(self.call("POST", "/api/clusters", {"host": "me@grace.example.edu"})[0], 409)
        for bad in ({"host": "grace"}, {"host": "me@grace; rm -rf ~"}, {"host": "me@x.edu", "port": 0},
                    {"host": "me@x.edu", "jump": "-oProxyCommand=evil"}, {"host": "me@x.edu", "user": "x"}):
            self.assertIn(self.call("POST", "/api/clusters", bad)[0], (400, 422), bad)

    def test_needs_a_login(self):
        status, out = self.call("POST", f"/api/clusters/{self.dead}/install", {})
        self.assertEqual(status, 409)
        self.assertIn("log in", out["error"])
        self.assertEqual(self.shell.calls, [])

    def test_install(self):
        agent_profiles.cmd_set(self.dead, "login=-p", "login+=2222", f"login+={self.host}")
        self.login({"password": "hunter2"})
        status, out = self.call("POST", f"/api/clusters/{self.dead}/install", {"force": True})
        self.assertEqual((status, out["kind"]), (202, "install"), out)
        self.assertEqual(self.shell.calls[-1], ["install_hpclib", "--force", "-p", "2222", self.host])
        done = self.wait_op(self.dead)
        self.assertEqual((done["state"], done["exit_code"]), ("succeeded", 0))
        self.assertTrue(any("ran install_hpclib" in line for line in done["log"]))
        listed = {c["name"]: c for c in self.call("GET", "/api/clusters")[1]["clusters"]}
        self.assertEqual(listed[self.dead]["operation"]["state"], "succeeded")
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/install", {"force": "yes"})[0], 400)

    def test_setup(self):
        status, out = self.call("POST", "/api/clusters", {"host": "me@new.example.edu"})
        name = out["name"]
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.assertEqual(self.call("POST", f"/api/clusters/{name}/setup", {})[0], 422)   # needs a work dir
            for bad in ({"work_dirs": ["relative/dir"]}, {"work_dirs": ["/ok"], "templates": "a b"},
                        {"work_dirs": ["/ok"], "rebuild": "yes"}, {"work_dirs": ["/ok"], "scopes": "*"}):
                self.assertIn(self.call("POST", f"/api/clusters/{name}/setup", bad)[0], (400, 422), bad)
            status, out = self.call("POST", f"/api/clusters/{name}/setup",
                                    {"work_dirs": ["/scratch/me/llm"], "binds": ["/software"],
                                     "templates": "hello,python_project", "rebuild": True})
            self.assertEqual(status, 202, out)
            self.assertEqual(self.shell.calls[-1], ["setup_agents", "--work-dir", "/scratch/me/llm", "--bind",
                                                    "/software", "--templates", "hello,python_project",
                                                    "--rebuild", name])
            self.wait_op(name)
            self.clusters.ops[name]["state"] = "running"                 # one at a time
            self.assertEqual(self.call("POST", f"/api/clusters/{name}/install", {})[0], 409)

    def test_versions(self):
        local = agent_console.local_hpclib_version()
        self.assertRegex(local, r"^\d+\.\d+\.\d+$")
        self.assertEqual(self.call("GET", "/api/health")[1]["hpclib_version"], local)
        tunnel = self.call("GET", f"/api/clusters/{self.live}")[1]["tunnel"]
        self.assertEqual(tunnel["hpclib_version"], local)                # the test server runs from this repo


class FakeApp(http.server.BaseHTTPRequestHandler):
    """Stands in for JupyterLab (JSON at /api) or for the tunnel's waiting page while the job is queued."""
    waiting = False

    def do_GET(self):
        if self.waiting:
            body, ctype = (b"<html><h2>Waiting for jupyter&hellip;</h2>\n<p>job 123 queued (Priority) - poll #4</p>"
                           b"</html>", "text/html")
        else:
            body, ctype = b'{"version": "4.2.5"}', "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class TestApps(ConsoleTestCase):

    def serve(self, port, waiting=False):
        handler = type("H", (FakeApp,), {"waiting": waiting})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def port(self, name=None):
        return agent_profiles.load(name or self.dead)["apps"]["jupyter"]["port"]

    def test_listing_and_ports(self):
        out = self.call("GET", "/api/apps")[1]
        self.assertEqual([a["id"] for a in out["apps"]], ["agents", "jupyter", "vscode", "transfer", "pai"])
        self.assertEqual({a["id"]: a.get("kind") for a in out["apps"]}["transfer"], "tool")
        status, out = self.call("GET", "/api/apps/jupyter")
        self.assertEqual(status, 200)
        by_name = {x["cluster"]: x for x in out["sessions"]}
        self.assertEqual(by_name[self.dead]["state"], "down")
        apps = agent_profiles.load(self.dead)["apps"]["jupyter"]
        profile = agent_profiles.load(self.dead)
        self.assertNotIn(apps["port"], (profile["port"], profile["process_port"], apps["process_port"]))
        self.assertEqual(self.call("GET", "/api/apps/nope")[0], 404)

    def test_settings(self):
        base = f"/api/apps/jupyter/{self.dead}/settings"
        status, out = self.call("PUT", base, {"tunnel_args": ["--time=4:00:00", "--mem=16gb"], "conda_env": "",
                                              "modules": ["GCCcore/13.2.0", "JupyterLab/4.2.0"],
                                              "project": "/scratch/me/llm/analysis"})
        self.assertEqual(status, 200, out)
        self.assertEqual((out["conda_env"], out["modules"][1]), ("", "JupyterLab/4.2.0"))
        for bad in ({"tunnel_args": ["--wrap=x"]}, {"conda_env": "a b"}, {"modules": ["a;b"]},
                    {"project": "relative"}, {"project": "/a b"}, {"project": "/a,b"}, {"port": 1}):
            self.assertIn(self.call("PUT", base, bad)[0], (400, 422), bad)

    def test_start_needs_login_then_launches(self):
        self.assertEqual(self.call("POST", f"/api/apps/jupyter/{self.dead}/start")[0], 409)
        self.call("PUT", f"/api/apps/jupyter/{self.dead}/settings",
                  {"tunnel_args": ["--time=4:00:00"], "conda_env": "jl", "modules": ["JupyterLab/4.2.0"],
                   "project": "/scratch/me/p"})
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            status, out = self.call("POST", f"/api/apps/jupyter/{self.dead}/start")
        self.assertEqual(status, 202, out)
        conf = agent_profiles.load(self.dead)["apps"]["jupyter"]
        host = agent_profiles.load(self.dead)["host"]
        self.assertEqual(self.shell.calls[-1], [
            "launch_tunnel", "-A", "none", "-P", str(conf["port"]), host, "jupyter",
            f"--process-port={conf['process_port']}", "--time=4:00:00",
            "--env=CONDA_ENVIRONMENT=jl,HPCLIB_JUPYTER_MODULES=JupyterLab/4.2.0,HPCLIB_JUPYTER_PROJECT=/scratch/me/p"])
        state = self.call("GET", f"/api/apps/jupyter/{self.dead}")[1]
        self.assertEqual((state["state"], state["started_here"]), ("starting", True))
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.assertEqual(self.call("POST", f"/api/apps/jupyter/{self.dead}/start")[0], 409)
        out = self.call("POST", f"/api/apps/jupyter/{self.dead}/stop")[1]
        self.assertEqual(self.shell.calls[-1], ["stop_tunnel", "-P", str(conf["port"]), host])
        self.assertEqual(self.call("GET", f"/api/apps/jupyter/{self.dead}")[1]["state"], "down")

    def test_queued_then_up_with_token(self):
        self.call("GET", f"/api/apps/jupyter/{self.dead}")              # picks the ports
        port = self.port()
        waiting = self.serve(port, waiting=True)
        state = self.call("GET", f"/api/apps/jupyter/{self.dead}")[1]
        self.assertEqual((state["state"], state["status"]), ("queued", "job 123 queued (Priority) - poll #4"))
        waiting.shutdown()
        waiting.server_close()
        self.serve(port)
        log = self.console / "logs" / f"{self.dead}.jupyter.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n=== 2026-10-01 launch_tunnel ... ===\n    http://c1:8888/lab?token=oldoldoldoldoldoldold\n"
                       "\n=== 2026-10-04 launch_tunnel ... ===\nLaunching Jupyter on 8888\n"
                       "    http://c2:8888/lab?token=0123456789abcdef0123456789abcdef\n")
        state = self.call("GET", f"/api/apps/jupyter/{self.dead}")[1]
        self.assertEqual((state["state"], state["version"]), ("up", "4.2.5"))
        self.assertEqual(state["url"], f"http://127.0.0.1:{port}/lab?token=0123456789abcdef0123456789abcdef")
        lines = self.call("GET", f"/api/apps/jupyter/{self.dead}/log")[1]["lines"]
        self.assertIn("Launching Jupyter on 8888", lines)


FAKE_TUNNEL = textwrap.dedent("""\
    #!/bin/bash
    # Like start_tunnel.sh reaching the compute node: the login node's ssh asks for the password on the terminal.
    echo "Submitted batch job 152373"
    echo "ssh -L 127.0.0.1:20523:127.0.0.1:24665 -t chem-entr-c01 source env.sh"
    for try in 1 2 3; do
      printf "maboyer@chem-entr-c01's password: "
      IFS= read -rs pw < /dev/tty
      echo
      if [ "$pw" = "$FAKE_NODE_PASSWORD" ]; then
        echo "connected to chem-entr-c01"
        sleep 30
        exit 0
      fi
      echo "Permission denied, please try again."
    done
    echo "maboyer@chem-entr-c01: Permission denied (publickey,password)."
    exit 255
""")


class TestTunnelInstall(ConsoleTestCase):
    """vscode and pai: tunnel settings saved on the cluster, Check and Install with install.sh, shared instances."""
    serve = TestApps.serve

    def wait_op(self, name):
        for _ in range(50):
            out = self.call("GET", f"/api/clusters/{name}/operation")[1]
            if out.get("state") != "running":
                return out
            time.sleep(0.1)
        return out

    def test_settings_check_install_start(self):
        base = f"/api/apps/vscode/{self.dead}"
        status, out = self.call("PUT", base + "/settings", {"settings": {"VSCODE_CONTAINER": "/x/vscode.sif",
                                                                        "VSCODE_ROOT_DIR": " "}})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["settings"], {"VSCODE_CONTAINER": "/x/vscode.sif"})      # blank: the tunnel's default
        self.assertEqual([f["name"] for f in out["fields"]], ["VSCODE_CONTAINER", "VSCODE_ROOT_DIR", "VSCODE_BIND_PATHS"])
        self.assertTrue(out["installable"])
        self.assertFalse(out["python_env"])
        for bad in ({"settings": {"PATH": "/evil"}}, {"settings": {"VSCODE_CONTAINER": "a\nb"}}, {"settings": []}):
            self.assertEqual(self.call("PUT", base + "/settings", bad)[0], 422, bad)
        # a setting with choices (PAI's source bind): one of them, or empty for the tunnel's default
        pai = f"/api/apps/pai/{self.dead}/settings"
        bind = {f["name"]: f for f in self.call("GET", pai)[1]["fields"]}["PAI_BIND_SOURCE"]
        self.assertEqual(bind["choices"], ["1", "0"])
        self.assertEqual(self.call("PUT", pai, {"settings": {"PAI_BIND_SOURCE": "yes"}})[0], 422)
        self.assertEqual(self.call("PUT", pai, {"settings": {"PAI_BIND_SOURCE": "0"}})[1]["settings"], {"PAI_BIND_SOURCE": "0"})
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.call("POST", f"/api/apps/pai/{self.dead}/check")
        self.assertIn("PAI_BIND_SOURCE=0", self.shell.calls[-1])
        self.assertEqual(self.call("GET", base)[1]["install"]["state"], "unchecked")
        self.assertEqual(self.call("POST", base + "/check")[0], 409)                     # needs the login
        host = agent_profiles.load(self.dead)["host"]
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            status, out = self.call("POST", base + "/check")
            self.assertEqual((status, out["install"]["state"]), (200, "missing"), out)
            self.assertEqual(self.shell.calls[-1], ["tunnel_setup", host, "vscode", "--set",
                                                    "VSCODE_CONTAINER=/x/vscode.sif", "--save", "--check"])
            status, out = self.call("POST", base + "/start")
            self.assertEqual(status, 409)
            self.assertIn("isn't installed", out["error"])
            # Install: an operation of the cluster's, which records what the check after it found
            self.shell.setup_status = "installed installed: /x/vscode.sif"
            status, out = self.call("POST", base + "/install", {"force": True})
            self.assertEqual((status, out["kind"], out["title"]), (202, "tunnel_install", "install VS Code"), out)
            self.assertEqual(self.shell.calls[-1][-3:], ["--install", "--force", "--check"])
            self.assertEqual(self.wait_op(self.dead)["state"], "succeeded")
            time.sleep(0.2)
            state = self.call("GET", base)[1]
            self.assertEqual(state["install"]["state"], "installed", state)
            self.assertEqual(state["operation"]["app"], "vscode")
            listed = {c["name"]: c for c in self.call("GET", "/api/clusters")[1]["clusters"]}
            self.assertEqual(listed[self.dead]["operation"]["title"], "install VS Code")
            # the settings went with it, so Start launches straight away
            status, out = self.call("POST", base + "/start")
            self.assertEqual(status, 202, out)
            self.assertEqual(self.shell.calls[-1][:7], ["launch_tunnel", "-A", "none", "-P",
                str(agent_profiles.load(self.dead)["apps"]["vscode"]["port"]), host, "vscode"])
            self.call("POST", base + "/stop")
            # a later change is sent before the next start
            self.call("PUT", base + "/settings", {"settings": {"VSCODE_CONTAINER": "/y/vscode.sif"}})
            self.call("POST", base + "/start")
            self.assertEqual(self.shell.calls[-2][:2], ["tunnel_setup", host])
            self.assertIn("VSCODE_CONTAINER=/y/vscode.sif", self.shell.calls[-2])
            self.assertEqual(self.shell.calls[-1][0], "launch_tunnel")
        self.assertEqual(self.call("POST", f"/api/apps/jupyter/{self.dead}/install", {"force": "x"})[0], 400)

    def test_old_cluster_without_setup_tunnel(self):
        # an older hpclib on the cluster: no setup_tunnel.sh, so no status line
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.clusters.shell_runner = lambda args, log: (log.write(b"tunnel_setup: command not found\n"),
                                                            subprocess.Popen(["false"]))[1]
            status, out = self.call("POST", f"/api/apps/vscode/{self.dead}/check")
        self.assertEqual(status, 502)
        self.assertIn("update hpclib", out["error"])

    def test_vscode_password_and_shared_pai(self):
        self.call("GET", f"/api/apps/vscode/{self.dead}")
        self.call("GET", f"/api/apps/pai/{self.dead}")
        apps = agent_profiles.load(self.dead)["apps"]
        logs = self.console / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / f"{self.dead}.vscode.log").write_text("\n=== launch ===\nbind-addr: 127.0.0.1:8080\n"
                                                      "auth: password\npassword: 1f2e3d4c5b6a\ncert: false\n")
        (logs / f"{self.dead}.pai.log").write_text("\n=== launch ===\nhpclib: attaching to the running pai instance: "
                                                   "job 152400 on chem-entr-c04, port 3104\n")
        self.serve(apps["vscode"]["port"])
        self.serve(apps["pai"]["port"])
        vs = self.call("GET", f"/api/apps/vscode/{self.dead}")[1]
        self.assertEqual((vs["state"], vs["password"], vs["token_known"]), ("up", "1f2e3d4c5b6a", True))
        self.assertTrue(vs["url"].endswith("/"))
        pai = self.call("GET", f"/api/apps/pai/{self.dead}")[1]
        self.assertEqual((pai["state"], pai["shared"]), ("up", True))
        self.assertEqual(pai["instance"], {"job": "152400", "node": "chem-entr-c04", "port": 3104, "how": "attached"})
        self.assertNotIn("password", pai)
        # its toolbar says which job serves the database, and a control ends that job
        self.assertEqual([t["id"] for t in pai["tools"]], ["instance"])
        self.assertIn("job 152400 on chem-entr-c04", pai["tools"][0]["text"])
        self.assertEqual(pai["controls"][0]["id"], "stop_instance")
        self.assertEqual(pai["controls"][0]["args"], {"job": "152400"})
        self.assertEqual((vs["tools"], vs["controls"]), ([], []))

    def test_stop_shared_instance(self):
        self.test_vscode_password_and_shared_pai()
        base = f"/api/apps/pai/{self.dead}"
        self.assertEqual(self.call("POST", base + "/control/stop_instance", {"job": "152400"})[0], 409)   # login
        host = agent_profiles.load(self.dead)["host"]
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            out = self.call("GET", base + "/instances")[1]
            self.assertEqual(out["instances"], [{"job": "152400", "node": "chem-entr-c04", "port": 3104,
                                                 "state": "RUNNING", "owner": "maboyer"}])
            for bad in ({"job": "1; rm"}, {}, {"job": "1", "x": 2}):
                self.assertEqual(self.call("POST", base + "/control/stop_instance", bad)[0], 400, bad)
            self.assertEqual(self.call("POST", base + "/control/nope", {})[0], 404)
            self.assertEqual(self.call("POST", f"/api/apps/vscode/{self.dead}/control/stop_instance", {"job": "1"})[0], 404)
            self.shell.stop_answer = "setup_tunnel: job 152400 belongs to someone, not you; not cancelling it"
            status, out = self.call("POST", base + "/control/stop_instance", {"job": "152400"})
            self.assertEqual(status, 409)
            self.assertIn("belongs to someone", out["error"])
            self.shell.stop_answer = None
            status, out = self.call("POST", base + "/control/stop_instance", {"job": "152400"})
            self.assertEqual((status, out["result"]), (200, "stopped"), out)
        self.assertIn(["tunnel_setup", host, "pai", "--stop-instance", "152400"], self.shell.calls)
        self.assertEqual(self.shell.calls[-1][0], "stop_tunnel")                 # and this tunnel stops
        pai = self.call("GET", base)[1]
        self.assertNotIn("stop_instance", [c["id"] for c in pai["controls"]])
        self.assertIn("none seen yet", pai["tools"][0]["text"])


FAKE_KINIT = textwrap.dedent("""\
    #!/bin/bash
    # smbshell --on HOST login: kinit asks for the password on the terminal
    printf "Password for me@AUTH.EXAMPLE.EDU: "
    IFS= read -rs pw < /dev/tty
    echo
    [ "$pw" = "kerberos-pass" ] || { echo "kinit: Password incorrect while getting initial credentials"; exit 1; }
    echo "signed in: me@AUTH.EXAMPLE.EDU"
""")


class TestDataTransfer(ConsoleTestCase):
    """The Data transfer page: signing in to the SMB server (kinit, or a password saved for jobs) for smbshell."""

    def setUp(self):
        super().setUp()
        script = self.tmp / "fake-kinit"
        script.write_text(FAKE_KINIT)
        script.chmod(0o755)
        self.clusters.tunnel_runner = lambda args, log_path: agent_console.PtySession([str(script)], log_path, dict(os.environ))
        self.route = f"/api/apps/transfer/{self.dead}"
        self.host = agent_profiles.load(self.dead)["host"]

    def test_status_and_controls(self):
        state = self.call("GET", self.route)[1]
        self.assertEqual((state["kind"], state["auth"], state["port"]), ("tool", None, None))
        self.assertEqual([c["id"] for c in state["controls"]], ["status"])
        self.assertEqual(self.call("POST", self.route + "/start")[0], 400)
        self.assertEqual(self.call("POST", self.route + "/control/status", {})[0], 409)          # needs the login
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            status, out = self.call("POST", self.route + "/control/status", {})
            self.assertEqual(status, 200, out)
            self.assertEqual(out["auth"]["kinit"], "host")
            self.assertEqual(self.shell.calls[-1], ["smbshell", "--on", self.host, "status", "--json"])
            state = self.call("GET", self.route)[1]
            self.assertIn("files.example.edu as me", state["tools"][0]["text"])
            self.assertEqual([c["id"] for c in state["controls"]], ["kinit", "status"])   # Kerberos: no password kept
            # a ticket: log out instead
            self.shell.smb_status["ticket"] = {"principal": "me@AUTH.EXAMPLE.EDU", "expires": "10/05/2026 18:00:00"}
            self.call("POST", self.route + "/control/status", {})
            state = self.call("GET", self.route)[1]
            tools = {t["id"]: t["text"] for t in state["tools"]}
            self.assertIn("Kerberos ticket for me@AUTH.EXAMPLE.EDU", tools["signin"])
            self.assertEqual([c["id"] for c in state["controls"]], ["kdestroy", "status"])
            self.call("POST", self.route + "/control/kdestroy", {})
            self.assertEqual(self.shell.calls[-2], ["smbshell", "--on", self.host, "logout"])
            # no kinit: a password saved for sync jobs
            self.shell.smb_status.update(kinit=None, ticket=None)
            self.call("POST", self.route + "/control/status", {})
            self.assertEqual([c["id"] for c in self.call("GET", self.route)[1]["controls"]], ["save_credentials", "status"])
            self.shell.smb_status["credentials"] = True
            self.call("POST", self.route + "/control/status", {})
            controls = self.call("GET", self.route)[1]["controls"]
            self.assertEqual(controls[0]["id"], "forget_credentials")
            self.assertIn("confirm", controls[0])
            self.assertEqual(self.call("POST", self.route + "/control/nope", {})[0], 404)
            # a server given by address: Kerberos can't sign in to it, so the password is offered
            self.shell.smb_status.update(host="10.55.179.23", kinit="host", credentials=False,
                                         ticket={"principal": "me@AUTH.EXAMPLE.EDU", "expires": "later"})
            self.call("POST", self.route + "/control/status", {})
            state = self.call("GET", self.route)[1]
            self.assertIn("needs the server's DNS name", {t["id"]: t["text"] for t in state["tools"]}["signin"])
            self.assertEqual([c["id"] for c in state["controls"]], ["save_credentials", "kdestroy", "status"])
        # its settings: the server, and how to sign in
        out = self.call("PUT", self.route + "/settings", {"settings": {"SMB_HOST": "files.example.edu", "SMB_AUTH": "kerberos"}})[1]
        self.assertEqual(out["settings"], {"SMB_HOST": "files.example.edu", "SMB_AUTH": "kerberos"})
        self.assertEqual(self.call("PUT", self.route + "/settings", {"settings": {"SMB_AUTH": "magic"}})[0], 422)
        # the address as it is usually written: the host, and the folder smbshell's paths are relative to
        for given in ("//10.55.179.23/CLAT_research/chem/our_lab", "\\\\10.55.179.23\\CLAT_research\\chem\\our_lab"):
            out = self.call("PUT", self.route + "/settings", {"settings": {"SMB_HOST": given}})[1]
            self.assertEqual(out["settings"], {"SMB_HOST": "10.55.179.23", "SMB_ROOT": "CLAT_research/chem/our_lab"}, given)
        # and they reach the cluster before the next status
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.call("POST", self.route + "/control/status", {})
        pushed = self.shell.calls[-2]
        self.assertEqual(pushed[:3], ["tunnel_setup", self.host, "data-transfer"])
        self.assertIn("SMB_ROOT=CLAT_research/chem/our_lab", pushed)

    def test_kinit_in_the_page(self):
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            status, out = self.call("POST", self.route + "/control/kinit", {})
            self.assertEqual(status, 200, out)
            for _ in range(50):
                prompts = self.call("GET", "/api/prompts")[1]["prompts"]
                if prompts:
                    break
                time.sleep(0.1)
            self.assertEqual((prompts[0]["app"], prompts[0]["title"]), ("transfer", "Data transfer"))
            self.assertIn("Kerberos", prompts[0]["prompt"]["note"])
            self.assertEqual(self.call("POST", self.route + "/control/kinit", {})[0], 409)       # one at a time
            self.shell.smb_status["ticket"] = {"principal": "me@AUTH.EXAMPLE.EDU", "expires": "later"}
            self.assertEqual(self.call("POST", "/api/" + prompts[0]["answer"], {"answer": "kerberos-pass"})[0], 200)
            for _ in range(50):
                auth = self.call("GET", self.route)[1].get("auth") or {}
                if auth.get("ticket"):
                    break
                time.sleep(0.1)
        self.assertEqual(auth["ticket"]["principal"], "me@AUTH.EXAMPLE.EDU")          # status asked afterwards
        log = (self.console / "logs" / f"{self.dead}.transfer.log").read_text()
        self.assertIn("signed in: me@AUTH.EXAMPLE.EDU", log)
        self.assertIn(f"smbshell --on {self.host} login", log)
        self.assertNotIn("kerberos-pass", log)


class TestSecondLogin(ConsoleTestCase):
    """Tunnels run in a pseudo-terminal, so the compute node's password prompt can be answered from the page."""

    def setUp(self):
        super().setUp()
        script = self.tmp / "fake-tunnel"
        script.write_text(FAKE_TUNNEL)
        script.chmod(0o755)
        env = dict(os.environ, FAKE_NODE_PASSWORD="node-pass")
        self.clusters.tunnel_runner = lambda args, log_path: agent_console.PtySession([str(script)], log_path, env)
        self.clusters.logins.alive = lambda profile: True
        self.addCleanup(self.kill_tunnels)

    def kill_tunnels(self):
        for proc in list(self.clusters.procs.values()) + list(self.clusters.app_procs.values()):
            if proc.poll() is None:
                os.killpg(proc.pid, 15)
                proc.wait(5)

    def prompt(self, route):
        for _ in range(50):
            out = self.call("GET", route)[1]
            prompt = (out.get("tunnel") or out).get("prompt")
            if prompt:
                return prompt
            time.sleep(0.1)
        self.fail(f"no prompt: {out}")

    def log_text(self, name):
        return (self.console / "logs" / f"{name}.log").read_text(errors="replace")

    def test_agent_tunnel_second_password(self):
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/tunnel/start")[0], 202)
        prompt = self.prompt(f"/api/clusters/{self.dead}")
        self.assertEqual((prompt["host"], prompt["retry"]), ("maboyer@chem-entr-c01", False))
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/tunnel/answer", {"answer": "wrong"})[0], 200)
        prompt = self.prompt(f"/api/clusters/{self.dead}")
        self.assertTrue(prompt["retry"])                                      # refused: asked again
        self.call("POST", f"/api/clusters/{self.dead}/tunnel/answer", {"answer": "node-pass"})
        for _ in range(50):
            if "connected to chem-entr-c01" in self.log_text(self.dead):
                break
            time.sleep(0.1)
        log = self.log_text(self.dead)
        self.assertIn("connected to chem-entr-c01", log)
        self.assertNotIn("node-pass", log)                                    # echo was off: not in the log
        self.assertNotIn("wrong", log)
        self.assertIsNone(self.call("GET", f"/api/clusters/{self.dead}")[1]["tunnel"]["prompt"])
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/tunnel/answer", {"answer": "x"})[0], 409)

    def test_gives_up_after_three(self):
        self.call("POST", f"/api/clusters/{self.dead}/tunnel/start")
        for _ in range(3):
            self.prompt(f"/api/clusters/{self.dead}")
            self.call("POST", f"/api/clusters/{self.dead}/tunnel/answer", {"answer": "nope"})
        for _ in range(50):
            if self.clusters.procs[self.dead].poll() is not None:
                break
            time.sleep(0.1)
        self.assertEqual(self.clusters.procs[self.dead].returncode, 255)
        self.assertIsNone(self.call("GET", f"/api/clusters/{self.dead}")[1]["tunnel"]["prompt"])
        self.assertIn("Permission denied (publickey,password)", self.log_text(self.dead))

    def test_jupyter_second_password(self):
        self.assertEqual(self.call("POST", f"/api/apps/jupyter/{self.dead}/start")[0], 202)
        prompt = self.prompt(f"/api/apps/jupyter/{self.dead}")
        self.assertEqual(prompt["kind"], "password")
        self.assertEqual(self.call("POST", f"/api/apps/jupyter/{self.dead}/answer", {"answer": "node-pass"})[0], 200)
        self.assertEqual(self.call("POST", f"/api/apps/jupyter/{self.dead}/answer", {"answer": "a\nb"})[0], 400)

    def use_tunnel(self, text):
        script = self.tmp / "fake-tunnel-2"
        script.write_text(text)
        script.chmod(0o755)
        env = dict(os.environ, FAKE_NODE_PASSWORD="node-pass")
        self.clusters.tunnel_runner = lambda args, log_path: agent_console.PtySession([str(script)], log_path, env)

    def test_new_compute_node_host_key(self):
        # the login node's first ssh to a compute node asks whether to trust its key, then for the password
        self.use_tunnel(FAKE_TUNNEL.replace('echo "ssh -L', textwrap.dedent("""\
            echo "The authenticity of host 'chem-entr-c04 (192.168.10.104)' can't be established."
            echo "ED25519 key fingerprint is SHA256:dhRCX7/vDiAVXre6VZpF8szOoDB93/b/5ePukPSfyEI."
            echo "This key is not known by any other names."
            printf "Are you sure you want to continue connecting (yes/no/[fingerprint])? "
            IFS= read -r ans < /dev/tty
            [ "$ans" = yes ] || { echo "Host key verification failed."; exit 255; }
            echo "Warning: Permanently added 'chem-entr-c04' (ED25519) to the list of known hosts."
            echo "ssh -L""")))
        self.call("POST", f"/api/clusters/{self.dead}/tunnel/start")
        prompt = self.prompt(f"/api/clusters/{self.dead}")
        self.assertEqual((prompt["kind"], prompt["host"], prompt["address"], prompt["key_type"], prompt["fingerprint"]),
                         ("hostkey", "chem-entr-c04", "192.168.10.104", "ED25519",
                          "SHA256:dhRCX7/vDiAVXre6VZpF8szOoDB93/b/5ePukPSfyEI"))
        listed = self.call("GET", "/api/prompts")[1]["prompts"]
        self.assertEqual(listed[0]["prompt"]["kind"], "hostkey")
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/tunnel/answer", {"answer": "sure"})[0], 400)
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/tunnel/answer", {"answer": "yes"})[0], 200)
        for _ in range(50):
            prompt = self.call("GET", f"/api/clusters/{self.dead}")[1]["tunnel"]["prompt"]
            if prompt and prompt["kind"] == "password":
                break
            time.sleep(0.1)
        self.assertEqual(prompt["kind"], "password")                         # then the password, as before
        self.call("POST", f"/api/clusters/{self.dead}/tunnel/answer", {"answer": "node-pass"})
        for _ in range(50):
            if "connected to chem-entr-c01" in self.log_text(self.dead):
                break
            time.sleep(0.1)
        self.assertIn("Permanently added 'chem-entr-c04'", self.log_text(self.dead))
        self.assertIn("connected to chem-entr-c01", self.log_text(self.dead))

    def test_host_key_refused(self):
        self.use_tunnel("#!/bin/bash\necho \"The authenticity of host 'c9' can't be established.\"\n"
                        "printf 'Are you sure you want to continue connecting (yes/no)? '\n"
                        "IFS= read -r ans < /dev/tty\n[ \"$ans\" = yes ] || { echo 'Host key verification failed.'; exit 255; }\n")
        self.call("POST", f"/api/clusters/{self.dead}/tunnel/start")
        prompt = self.prompt(f"/api/clusters/{self.dead}")
        self.assertEqual((prompt["kind"], prompt["host"], prompt["fingerprint"]), ("hostkey", "c9", None))
        self.call("POST", f"/api/clusters/{self.dead}/tunnel/answer", {"answer": "no"})
        for _ in range(50):
            if self.clusters.procs[self.dead].poll() is not None:
                break
            time.sleep(0.1)
        self.assertEqual(self.clusters.procs[self.dead].returncode, 255)
        self.assertIn("Host key verification failed", self.log_text(self.dead))

    def test_connecting_is_not_an_error(self):
        # while the login node's ssh to the compute node waits, the forwarded port is open but nothing answers:
        # a tunnel started here is still starting (the page keeps watching it), not in error
        port = agent_profiles.load(self.dead)["port"]
        self.call("POST", f"/api/clusters/{self.dead}/tunnel/start")
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen()
        self.addCleanup(srv.close)

        def close_all():
            with contextlib.suppress(OSError):
                while True:
                    srv.accept()[0].close()
        threading.Thread(target=close_all, daemon=True).start()
        tunnel = self.call("GET", f"/api/clusters/{self.dead}")[1]["tunnel"]
        self.assertEqual(tunnel["state"], "starting", tunnel)
        self.assertIn("connecting", tunnel["error"])

    def test_prompts_everywhere(self):
        # one local route lists every waiting prompt, so the page can ask from wherever it is
        self.assertEqual(self.call("GET", "/api/prompts")[1], {"ok": True, "prompts": [], "running": 0})
        self.call("POST", f"/api/clusters/{self.dead}/tunnel/start")
        self.call("POST", f"/api/apps/jupyter/{self.dead}/start")
        for _ in range(50):
            out = self.call("GET", "/api/prompts")[1]
            if len(out["prompts"]) == 2:
                break
            time.sleep(0.1)
        self.assertEqual(out["running"], 2)
        by_app = {p["app"]: p for p in out["prompts"]}
        self.assertEqual(set(by_app), {None, "jupyter"})
        self.assertEqual(by_app[None]["title"], "agent tunnel")
        self.assertEqual(by_app[None]["prompt"]["host"], "maboyer@chem-entr-c01")
        for p in out["prompts"]:
            self.assertEqual(self.call("POST", "/api/" + p["answer"], {"answer": "node-pass"})[0], 200, p)
        self.assertEqual(self.call("GET", "/api/prompts")[1]["prompts"], [])


class TestProxy(ConsoleTestCase):

    def test_owner_token_by_default(self):
        status, out = self.call("GET", f"/api/clusters/{self.live}/rest/health")
        self.assertEqual((status, out["token"]["name"]), (200, "owner"))
        status, out = self.call("GET", f"/api/clusters/{self.live}/rest/health?as=agent")
        self.assertEqual(out["token"]["name"], "llm")
        self.assertEqual(self.call("GET", f"/api/clusters/{self.live}/rest/admin/tokens?as=agent")[0], 403)

    def test_post_body_and_query(self):
        builder = self.tmp / "agents" / self.live / "agent_token"
        builder.write_text(self.builder_token + "\n")
        status, out = self.call("POST", f"/api/clusters/{self.live}/rest/templates/propose?as=agent",
                                {"name": "xtb", "template": SPEC, "script": SCRIPT})
        self.assertEqual(status, 201, out)
        status, out = self.call("GET", f"/api/clusters/{self.live}/rest/admin/proposals/diff?name=xtb")
        self.assertEqual(status, 200)
        self.assertIn("script.sh", out["diff"])
        status, out = self.call("POST", f"/api/clusters/{self.live}/rest/admin/proposals/approve", {"name": "xtb"})
        self.assertEqual((status, out["approved"]), (200, "xtb"))

    def test_file_download_is_streamed(self):
        data = os.urandom(3 << 20) + b"end"
        (self.llm_root / "big.bin").write_bytes(data)
        with mock.patch.object(agent_console.Clusters, "call", side_effect=AssertionError("buffered")):
            status, content, headers = self.call(
                "GET", f"/api/clusters/{self.live}/rest/files/content?path={self.llm_root / 'big.bin'}", raw=True)
        self.assertEqual((status, content), (200, data))
        self.assertEqual(headers["Content-Length"], str(len(data)))
        self.assertIn("big.bin", headers["Content-Disposition"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, content, _ = self.call("GET", f"/api/clusters/{self.live}/rest/files/content?path=/etc/passwd",
                                       raw=True)
        self.assertEqual(status, 403)
        self.assertIn(b"outside", content)
        status, _, _ = self.call("GET", f"/api/clusters/{self.live}/rest/files/content?path={self.llm_root}/nope",
                                 raw=True)
        self.assertEqual(status, 404)
        self.assertEqual(self.call("GET", f"/api/clusters/{self.dead}/rest/files/content?path=x")[0], 502)

    def test_dead_tunnel(self):
        status, out = self.call("GET", f"/api/clusters/{self.dead}/rest/health")
        self.assertEqual(status, 502)
        self.assertIn("tunnel/start", out["error"])

    def test_revoked_token_is_not_a_console_401(self):
        (self.agents / self.live / "owner_token").write_text("revoked\n")
        status, out = self.call("GET", f"/api/clusters/{self.live}/rest/health")
        self.assertEqual((status, out["cluster_status"]), (502, 401))


class TestAggregates(ConsoleTestCase):

    def test_jobs(self):
        status, out = self.call("GET", "/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(out["clusters"][self.live], {"ok": True})
        self.assertEqual(out["clusters"][self.dead], {"error": "tunnel down"})
        self.assertIsInstance(out["jobs"], list)

    def test_proposals(self):
        from rest_client import RESTClient
        RESTClient(self.url, token=self.builder_token).propose_template("xtb", SPEC, SCRIPT)
        status, out = self.call("GET", "/api/proposals")
        self.assertEqual(status, 200)
        self.assertEqual([(p["name"], p["cluster"]) for p in out["proposals"]], [("xtb", self.live)])
        self.assertEqual(out["proposals"][0]["policy"]["review"], "owner")


if __name__ == "__main__":
    unittest.main()
