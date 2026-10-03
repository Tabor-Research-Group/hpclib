"""Tests for hpclib/servers/agent_console.py, against a real REST server
(fake SLURM, from test_rest_jobs) reached through a profile's port.

Run with:  python -m unittest tests.test_agent_console -v
"""
import contextlib
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

    def __call__(self, args, log):
        self.calls.append(list(args))
        log.write(f"ran {' '.join(args)}\n".encode())
        return subprocess.Popen(["sleep", "30" if args[0] == "agent_tunnel" else "0"], start_new_session=True)


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
