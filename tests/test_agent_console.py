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
import stat
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
import console_packages  # noqa: E402
import agent_profiles  # noqa: E402
import local_platform  # noqa: E402

KEY = "console-key"
SPEC = {"description": "Run xtb on an xyz file", "parameters": {"xyz": {"type": "path", "kind": "file"}},
        "resources": {"time": "00:10:00", "mem": "1G", "cpus_per_task": 1}}
SCRIPT = "#!/bin/bash\nset -euo pipefail\nxtb \"$HPC_PARAM_XYZ\" --opt\n"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def shared_app_package(tmp):
    """A package like PAI's: a shared app (one database job serves everyone) with its own tunnel and an Update."""
    root = Path(tmp) / "shared-package"
    tunnel = root / "tunnels" / "pai"
    tunnel.mkdir(parents=True)
    (tunnel / "sbatch_script.sh").write_text("#!/bin/bash\ntrue\n")
    (tunnel / "tunnel_config.sh").write_text("SHARED_INSTANCE=true\nKEEP_INSTANCE=true\n"
                                             "TUNNEL_SETTINGS=\"PAI_ROOT_DIR PAI_IMAGE PAI_BIND_SOURCE\"\n")
    (tunnel / "install.sh").write_text("#!/usr/bin/env bash\n# hpclib-install: --check --force\n")
    (root / "hpclib-package.json").write_text(json.dumps({
        "format": 1, "name": "pai", "version": "0.1.0",
        "apps": {"pai": {
            "title": "PAI", "tunnel": "pai", "shared": True, "instance_label": "database",
            "reinstall": {"label": "Update", "title": "fast-forward the checkout and pull the image again"},
            "settings": [
                {"name": "PAI_ROOT_DIR", "label": "Install directory", "hint": "holds proto-auto-interface/"},
                {"name": "PAI_IMAGE", "label": "Image", "hint": "where Install and Update pull the app's image from"},
                {"name": "PAI_BIND_SOURCE", "label": "Bind source", "hint": "1 or 0", "choices": ["1", "0"]}]}},
        "tunnels": {"pai": "tunnels/pai"}}))
    out = Path(tmp) / "pai-0.1.0.zip"
    console_packages.build(str(root), str(out))
    return out.read_bytes()


class FakeShell:
    """Stands in for `bash -c '. hpclib.sh; agent_tunnel ...'`."""
    def __init__(self):
        self.calls = []
        self.setup_status = "missing not installed: no image at /x/vscode.sif"   # what tunnel_setup --check says
        self.stop_answer = None                                                  # what --stop-instance says
        self.install_elsewhere = None
        self.tokens = []                                                         # the app's tokens, for --tokens
        self.tokens_error = None                                                 # what tunnel_setup --tokens says instead
        self.secret_seen = []                                                    # whatever it printed with a token
        self.downloads = []                                                      # what --downloads list says is there
        self.downloads_error = None                                              # what --downloads says instead
        self.clear_output = ""                                                   # what clear_tunnel_ports prints
        self.smb_status = {"host": "files.example.edu", "user": "me", "auth": "auto", "kinit": "host",
                           "rclone_kerberos": True, "ticket": None, "credentials": False, "image": True}

    def __call__(self, args, log):
        self.calls.append(list(args))
        log.write(f"ran {' '.join(args)}\n".encode())
        if args[0] == "tunnel_setup":   # like tunnels/setup_tunnel.sh
            if "--save" in args:
                log.write(f"saved {args.count('--set')} setting(s) for the tunnel\n".encode())
            if "--install" in args and self.install_elsewhere:
                log.write(f"HPCLIB_INSTALL_ELSEWHERE {self.install_elsewhere}\n".encode())
                return subprocess.Popen(["bash", "-c", "exit 3"], start_new_session=True)
            if "--check" in args:
                log.write(f"HPCLIB_TUNNEL_STATUS {self.setup_status}\n".encode())
            if "--instances" in args:
                log.write(b"HPCLIB_TUNNEL_INSTANCE 152400 chem-entr-c04 3104 RUNNING maboyer\n")
            if "--stop-instance" in args:
                job = args[args.index("--stop-instance") + 1]
                log.write((self.stop_answer or f"HPCLIB_TUNNEL_INSTANCE_STOPPED {job}").encode() + b"\n")
        if args[0] == "tunnel_setup" and "--tokens" in args:   # like setup_tunnel.sh --tokens over tokens.sh --json
            rest = args[args.index("--tokens") + 1:]
            if self.tokens_error:
                log.write(self.tokens_error.encode() + b"\n")
                if self.tokens_error.startswith("HPCLIB_TOKENS_ELSEWHERE"):   # setup_tunnel.sh exits 3 with it
                    return subprocess.Popen(["bash", "-c", "exit 3"], start_new_session=True)
            elif rest[0] == "list":
                log.write(b"banner\nHPCLIB_TOKENS " + json.dumps({"tokens": self.tokens}).encode() + b"\n")
            elif rest[0] == "create":
                opt = dict(zip(rest[1::2], rest[2::2]))
                rec = {"id": f"t{len(self.tokens):07d}", "label": opt["--label"], "scopes": opt["--scopes"].split(","),
                       "created": 1.0, "expires": None if opt["--days"] == "0" else 2e9, "revoked": False,
                       "hash": "must-never-reach-the-page"}
                self.tokens.append(rec)
                made = {"id": rec["id"], "label": rec["label"], "scopes": rec["scopes"], "expires": rec["expires"]}
                if "--save-to" in opt:
                    made.update(token=None, saved_to=opt["--save-to"])
                else:
                    made["token"] = "pai_" + rec["id"] + "_SECRETSECRET"
                log.write(b"HPCLIB_TOKEN_CREATED " + json.dumps(made).encode() + b"\n")
            elif rest[0] == "revoke":
                hit = [t for t in self.tokens if t["id"] == rest[1]]
                if hit:
                    hit[0]["revoked"] = True
                    log.write(f"HPCLIB_TOKEN_REVOKED {rest[1]}\n".encode())
                else:
                    log.write(b"setup_tunnel: tokens.sh revoke failed\n")
        if args[0] == "tunnel_setup" and "--downloads" in args:   # like setup_tunnel.sh --downloads over downloads.sh
            rest = args[args.index("--downloads") + 1:]
            if self.downloads_error:
                log.write(self.downloads_error.encode() + b"\n")
            elif rest[0] == "list":
                log.write(b"HPCLIB_DOWNLOADS " + json.dumps({"items": self.downloads, "note": "4.0 TB free"}).encode() + b"\n")
            elif rest[0] == "pull":
                log.write(f"pulling {rest[2]}\n".encode())
                self.downloads.append({"kind": rest[1], "name": rest[2], "size": 1000, "in_use": False})
            elif rest[0] == "remove":
                if any(d["name"] == rest[2] and d["in_use"] for d in self.downloads):
                    log.write(b"setup_tunnel: downloads.sh remove failed: it is in use by a running job\n")
                else:
                    self.downloads = [d for d in self.downloads if d["name"] != rest[2]]
                    log.write(b"HPCLIB_DOWNLOAD_REMOVED " + json.dumps({"removed": rest[2]}).encode() + b"\n")
        if args[0] == "clear_tunnel_ports":
            log.write(self.clear_output.encode())
        if args[0] == "smbshell" and "status" in args:      # like tunnels/data-transfer/smbshell.sh status --json
            log.write(("HPCLIB_SMB_STATUS " + json.dumps(self.smb_status) + "\n").encode())
        return subprocess.Popen(["sleep", "30" if args[0] in ("agent_tunnel", "launch_tunnel") else "0"], start_new_session=True)


class ConsoleTestCase(JobServerTestCase):

    def setUp(self):
        super().setUp()
        self.agents = self.tmp / "agents"
        self.console = self.tmp / "console"
        # HPCLIB_PLATFORM: tests see Linux paths for the LLM clients, even run under WSL (test_local_platform
        # covers WSL)
        env = mock.patch.dict(os.environ, {"HPCLIB_AGENTS_DIR": str(self.agents),
                                           "HPCLIB_CONSOLE_DIR": str(self.console), "HPCLIB_PLATFORM": "linux"})
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

    def test_user_stylesheet_optional_and_reloaded(self):
        # No file yet: CSS, not the HTML SPA fallback, and no session key needed.
        status, content, headers = self.call("GET", "/user.css", key=None, raw=True)
        self.assertEqual((status, content), (200, b""))
        self.assertEqual(headers.get("Content-Type"), "text/css; charset=utf-8")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

        # The fixture uses HPCLIB_CONSOLE_DIR; a file in --static must not replace it.
        (Path(self.console_server.static_dir) / "user.css").write_text("body { color: red; }")
        self.console.mkdir(parents=True, exist_ok=True)
        stylesheet = self.console / "user.css"
        for css in (b":root { --accent: purple; }\n", b"main { max-width: 1500px; }\n"):
            stylesheet.write_bytes(css)
            status, content, headers = self.call("GET", "/user.css", key=None, raw=True)
            self.assertEqual((status, content), (200, css))
            self.assertEqual(headers.get("Cache-Control"), "no-store")
        stylesheet.unlink()
        self.assertEqual(self.call("GET", "/user.css", key=None, raw=True)[:2], (200, b""))

    def test_user_stylesheet_keeps_console_files_private(self):
        self.console.mkdir(parents=True, exist_ok=True)
        (self.console / "session").write_text("PRIVATE_SESSION_KEY")
        css = b":root { --accent: purple; }"
        (self.console / "user.css").write_bytes(css)
        self.assertEqual(self.call("GET", "/user.css?file=session", key=None, raw=True)[:2], (200, css))
        for route in ("/session", "/console/session", "/user.css/session", "/user.css/../session",
                      "/user.css%2f..%2fsession", "/../console/session"):
            status, content, _ = self.call("GET", route, key=None, raw=True)
            self.assertIn(status, (200, 404))
            self.assertNotIn(b"PRIVATE_SESSION_KEY", content)
        self.assertEqual(self.call("GET", "/user.css", key=None, raw=True,
                                   headers={"Host": "evil.example:80"})[0], 421)
        self.assertEqual(self.call("GET", "/api/health", key=None)[0], 401)

    def test_serves_the_bundled_front_end(self):
        ui = Path(__file__).resolve().parents[1] / "agent-console"
        self.console_server.static_dir = str(ui)
        status, content, _ = self.call("GET", "/", key=None, raw=True)
        self.assertIn(b'src="app.js"', content)
        self.assertLess(content.index(b'href="default-styles.css"'), content.index(b'href="style.css"'))
        self.assertGreater(content.index(b'href="/user.css"'), content.index(b'href="style.css"'))
        status, asset, headers = self.call("GET", "/default-styles.css", key=None, raw=True)
        self.assertEqual((status, headers.get("Content-Type")), (200, "text/css"))
        self.assertIn(b"--log-max-width: 960px", asset)
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


class TestStylesheets(ConsoleTestCase):

    def test_open_creates_exact_defaults_and_preserves_existing_file(self):
        ui = Path(__file__).resolve().parents[1] / "agent-console"
        self.console_server.static_dir = str(ui)
        destination = self.console / "user.css"
        with mock.patch.object(agent_console, "open_with_default_application") as opener:
            status, out = self.call("POST", "/api/styles/open")
            self.assertEqual(status, 200)
            self.assertEqual(out, {"ok": True, "path": str(destination), "created": True})
            self.assertEqual(destination.read_bytes(), (ui / "default-styles.css").read_bytes())
            opener.assert_called_once_with(str(destination))
            self.assertEqual(self.call("GET", "/user.css", key=None, raw=True)[1], destination.read_bytes())
            for existing in (b":root { --accent: purple; }", b""):
                destination.write_bytes(existing)
                status, out = self.call("POST", "/api/styles/open", {"path": "elsewhere.css"})
                self.assertEqual((status, out["created"]), (200, False))
                self.assertEqual(out["path"], str(destination))
                self.assertEqual(destination.read_bytes(), existing)
                self.assertFalse((self.console / "elsewhere.css").exists())
            self.assertEqual(opener.call_count, 3)

    def test_open_requires_key_and_post(self):
        with mock.patch.object(agent_console, "open_with_default_application") as opener:
            for key in (None, "wrong"):
                self.assertEqual(self.call("POST", "/api/styles/open", key=key)[0], 401)
            self.assertEqual(self.call("GET", "/api/styles/open")[0], 404)
            self.assertEqual(self.call("POST", "/api/styles/open", headers={"Host": "evil.example:80"})[0], 421)
            self.assertFalse((self.console / "user.css").exists())
            opener.assert_not_called()

    def test_missing_defaults_does_not_create_an_empty_stylesheet(self):
        with mock.patch.object(agent_console, "open_with_default_application") as opener:
            self.assertEqual(self.call("POST", "/api/styles/open")[0], 503)
            self.assertFalse((self.console / "user.css").exists())
            self.console_server.static_dir = None
            self.assertEqual(self.call("POST", "/api/styles/open")[0], 503)
            opener.assert_not_called()

    def test_existing_stylesheet_can_open_without_static_assets(self):
        self.console.mkdir(parents=True, exist_ok=True)
        destination = self.console / "user.css"
        destination.write_bytes(b"/* personal styles */")
        self.console_server.static_dir = None
        with mock.patch.object(agent_console, "open_with_default_application") as opener:
            status, out = self.call("POST", "/api/styles/open")
            self.assertEqual((status, out["created"]), (200, False))
            opener.assert_called_once_with(str(destination))

    def test_opener_error_preserves_new_file_and_reports_failure(self):
        self.console_server.static_dir = str(Path(__file__).resolve().parents[1] / "agent-console")
        with mock.patch.object(agent_console, "open_with_default_application", side_effect=OSError("no CSS association")):
            status, out = self.call("POST", "/api/styles/open")
            self.assertEqual(status, 503)
            self.assertIn("OS default application", out["error"])
            self.assertTrue((self.console / "user.css").is_file())
            self.assertEqual(out["path"], str(self.console / "user.css"))

    def test_non_file_destination_is_preserved(self):
        (self.console / "user.css").mkdir(parents=True)
        with mock.patch.object(agent_console, "open_with_default_application") as opener:
            self.assertEqual(self.call("POST", "/api/styles/open")[0], 409)
            self.assertTrue((self.console / "user.css").is_dir())
            opener.assert_not_called()


class TestDefaultApplication(unittest.TestCase):

    def setUp(self):   # the platform from sys.platform alone (not WSL, whatever runs the tests)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in ("HPCLIB_PLATFORM", "WSL_DISTRO_NAME", "WSL_INTEROP"):
            os.environ.pop(var, None)
        for patch in (mock.patch.object(local_platform, "settings", lambda: {}),
                      mock.patch.object(local_platform, "OSRELEASE", "/nonexistent")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_delegates_to_os_without_selecting_an_editor(self):
        path = "/tmp/styles with spaces/user.css"
        for platform, command in (("darwin", "/usr/bin/open"), ("linux", "xdg-open")):
            with self.subTest(platform=platform), mock.patch.object(agent_console.sys, "platform", platform), \
                    mock.patch.object(agent_console.subprocess, "Popen") as launch:
                launch.return_value.wait.return_value = 0
                agent_console.open_with_default_application(path)
                self.assertEqual(launch.call_args.args, ([command, path],))
                self.assertNotIn("shell", launch.call_args.kwargs)
        with mock.patch.object(agent_console.sys, "platform", "win32"), \
                mock.patch.object(agent_console.os, "startfile", create=True) as startfile, \
                mock.patch.object(agent_console.subprocess, "Popen") as launch:
            agent_console.open_with_default_application(path)
            startfile.assert_called_once_with(path)
            launch.assert_not_called()

    def test_running_handler_is_not_terminated_and_failure_is_reported(self):
        with mock.patch.object(agent_console.sys, "platform", "linux"), \
                mock.patch.object(agent_console.subprocess, "Popen") as launch:
            launch.return_value.wait.side_effect = subprocess.TimeoutExpired("xdg-open", 2)
            agent_console.open_with_default_application("/tmp/user.css")
            launch.return_value.terminate.assert_not_called()
            launch.return_value.kill.assert_not_called()
            launch.return_value.wait.side_effect = None
            launch.return_value.wait.return_value = 1
            with self.assertRaises(OSError):
                agent_console.open_with_default_application("/tmp/user.css")


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


class TestClearPorts(ConsoleTestCase):
    """Settings → Clear leftover ports: clear_tunnel_ports over the login, then (if allowed) extra logins."""

    def setUp(self):
        super().setUp()
        self.clusters.logins.alive = lambda profile: True
        self.port = agent_profiles.load(self.dead)["port"]

    def wait_op(self):
        for _ in range(100):
            out = self.call("GET", f"/api/clusters/{self.dead}/operation")[1]
            if out.get("state") != "running":
                return out
            time.sleep(0.05)
        return out

    def test_ports_and_running_ones_left_alone(self):
        ports = self.call("GET", f"/api/clusters/{self.live}/ports")[1]["ports"]
        self.assertEqual([(p["what"], p["running"]) for p in ports], [("agent tunnel", True)])   # its server answers
        self.assertEqual(self.call("POST", f"/api/clusters/{self.live}/ports/clear", {})[0], 409)
        ports = self.call("GET", f"/api/clusters/{self.dead}/ports")[1]["ports"]
        self.assertEqual(ports, [{"port": self.port, "what": "agent tunnel", "app": None, "running": False}])

    def test_validation(self):
        for body in ({"fresh_logins": "yes"}, {"max_logins": 0}, {"max_logins": 11}, {"password": "a\nb"},
                     {"fresh_logins": True, "max_logins": True}):
            self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/ports/clear", body)[0], 422, body)
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/ports/clear", {"other": 1})[0], 400)

    def test_cleared_through_the_login(self):
        self.shell.clear_output = (f"HPCLIB_PORT_HERE login1\nHPCLIB_PORT_CLEARED login1 {self.port}\n"
                                   f"HPCLIB_PORT_CLEARED login2 {self.port}\n")
        status, out = self.call("POST", f"/api/clusters/{self.dead}/ports/clear", {})
        self.assertEqual(status, 202, out)
        self.assertEqual(self.shell.calls[-1], ["clear_tunnel_ports", "-P", str(self.port), "me@dead.example.edu"])
        op = self.wait_op()
        self.assertEqual((op["kind"], op["state"]), ("clear_ports", "succeeded"), op)
        self.assertIn("every port is clear", "\n".join(op["log"]))

    def test_pending_without_extra_logins(self):
        self.shell.clear_output = (f"HPCLIB_PORT_HERE login1\nHPCLIB_PORT_CLEARED login1 {self.port}\n"
                                   f"HPCLIB_PORT_PENDING login3 {self.port}\n")
        with mock.patch.object(self.clusters.logins, "run_fresh") as fresh:
            self.call("POST", f"/api/clusters/{self.dead}/ports/clear", {"fresh_logins": False})
            op = self.wait_op()
        fresh.assert_not_called()
        self.assertEqual(op["state"], "failed")
        log = "\n".join(op["log"])
        self.assertIn(f"HPCLIB_PORTS_LEFT login3:{self.port}", log)
        self.assertIn("allow extra logins", log)

    def test_extra_logins_until_the_node_is_reached(self):
        self.shell.clear_output = (f"HPCLIB_PORT_HERE login1\nHPCLIB_PORT_PENDING login3 {self.port}\n"
                                   f"HPCLIB_PORT_PENDING login2 {self.port}\n")
        landings = iter(["login1", "login3", "login1", "login2", "login2"])
        calls = []

        def fresh(profile, password, remote, script, extra=()):
            calls.append((password, remote, list(extra)))
            self.assertIn("_hpclib_clear_ports", script)
            node = next(landings)
            return 0, f"HPCLIB_PORT_HERE {node}\nHPCLIB_PORT_CLEARED {node} {self.port}\n", "logged in"

        with mock.patch.object(self.clusters.logins, "run_fresh", side_effect=fresh), \
                mock.patch.object(self.clusters, "_login_addresses", return_value=["10.0.0.1", "10.0.0.2"]), \
                mock.patch.object(self.clusters, "_clear_ports_script", return_value="_hpclib_clear_ports \"$@\""):
            status, out = self.call("POST", f"/api/clusters/{self.dead}/ports/clear",
                                    {"fresh_logins": True, "password": "s3cret", "max_logins": 6})
            self.assertEqual(status, 202, out)
            self.assertNotIn("s3cret", json.dumps(out))
            op = self.wait_op()
        self.assertEqual(op["state"], "succeeded", op)
        self.assertNotIn("s3cret", json.dumps(op))
        self.assertEqual(len(calls), 4)                                   # stopped once both nodes were reached
        self.assertEqual(calls[0], ("s3cret", f"bash -s -- --no-hop {self.port}", ["-o", "HostName=10.0.0.1",
                                                                                 "-o", "HostKeyAlias=dead.example.edu"]))
        self.assertEqual(calls[2][2], [])                                 # then the name itself
        log = "\n".join(op["log"])
        self.assertIn("landed on login3: cleared", log)
        self.assertIn("approve the Duo push", log)
        self.assertNotIn("s3cret", log)
        self.assertEqual(self.clusters._clear_requests, {})

    def test_refused_login_stops(self):
        self.shell.clear_output = f"HPCLIB_PORT_HERE login1\nHPCLIB_PORT_PENDING login3 {self.port}\n"
        fresh = mock.Mock(return_value=(None, "", "the password was not accepted"))
        with mock.patch.object(self.clusters.logins, "run_fresh", fresh), \
                mock.patch.object(self.clusters, "_login_addresses", return_value=[]), \
                mock.patch.object(self.clusters, "_clear_ports_script", return_value="x _hpclib_clear_ports"):
            self.call("POST", f"/api/clusters/{self.dead}/ports/clear", {"fresh_logins": True, "password": "bad"})
            op = self.wait_op()
        self.assertEqual(fresh.call_count, 1)
        self.assertEqual(op["state"], "failed")
        self.assertIn("the login itself was refused", "\n".join(op["log"]))

    def test_fresh_login_command_and_prompts(self):
        """run_fresh: its own connection (no master), the password once, a push; a typed code is refused."""
        logins = agent_console.Logins(ssh="ssh")
        seen = {}

        def run(command, **kwargs):
            seen["command"], seen["env"] = command, kwargs["env"]
            nonce = kwargs["env"]["HPCLIB_ASKPASS_NONCE"]
            seen["answers"] = [logins.handle_prompt(nonce, "Password: "), logins.handle_prompt(nonce, "Duo two-factor")]
            return subprocess.CompletedProcess(command, 0, b"HPCLIB_PORT_HERE login2\n", b"")

        profile = agent_profiles.load(self.dead)
        with mock.patch.object(subprocess, "run", side_effect=run), \
                mock.patch.object(logins, "_askpass_program", return_value="/bin/true"):
            code, out, message = logins.run_fresh(profile, "pw", "bash -s -- --no-hop 1", "script", ["-o", "HostName=h"])
        self.assertEqual((code, message), (0, "logged in"))
        self.assertEqual(seen["answers"], [{"answer": "pw"}, {"answer": "1"}])
        self.assertEqual(seen["command"][:5], ["ssh", "-o", "ControlMaster=no", "-o", "ControlPath=none"])
        self.assertEqual(seen["command"][-2:], ["me@dead.example.edu", "bash -s -- --no-hop 1"])
        self.assertEqual(logins.by_nonce, {})
        with mock.patch.object(subprocess, "run", side_effect=lambda command, **kw: (
                seen.update(code=logins.handle_prompt(kw["env"]["HPCLIB_ASKPASS_NONCE"], "Enter verification code:")),
                subprocess.CompletedProcess(command, 255, b"", b"Permission denied"))[1]), \
                mock.patch.object(logins, "_askpass_program", return_value="/bin/true"):
            code, out, message = logins.run_fresh(profile, "pw", "true", "")
        self.assertIn("error", seen["code"])
        self.assertIn("typed code", message)


class TestClusterJobsAndStorage(ConsoleTestCase):
    """The cluster page's Jobs and Storage: squeue/sacct and du over the login, labelled and parsed."""

    SQUEUE = ("Q|101|hpcrest-xtb_opt|PENDING|Priority|cpu|0:00|1:00:00|1|4|N/A|8G||N/A|hpclib-rest:llm:xtb_opt\n"
              "Q|102|rest-k2v9x|RUNNING|None|cpu|3:12:40|8:00:00|1|1|N/A|1G|g042|2026-10-23T07:00:00|\n"
              "Q|103|vscode-p0c3a|RUNNING|None|cpu|0:10:00|8:00:00|1|4|N/A|16G|g010|2026-10-23T10:00:00|\n"
              "Q|104|orca_scan|RUNNING|None|cpu|0:12:02|12:00:00|1|24|N/A|96G|g088|2026-10-23T10:02:00|\n"
              "A|90|hpcrest-jax_bench|FAILED|1:0|00:02:41|2026-10-23T10:31:57|gpu||64G|hpclib-rest:llm:jax_bench\n"
              "A|90.batch|batch|FAILED|1:0|00:02:41|2026-10-23T10:31:57||5242880K|64G|\n"
              "A|91|vscode-a1|TIMEOUT|0:15|08:00:12|2026-10-23T06:12:40|cpu||16G|\n"
              "A|101|hpcrest-xtb_opt|PENDING|0:0|00:00:00|Unknown|cpu||8G|\n")

    def setUp(self):
        super().setUp()
        self.clusters.logins.alive = lambda profile: True
        self.ran = []

        def runner(profile, script, args=(), wait=60):
            self.ran.append((script, tuple(args)))
            if "squeue" in script:
                return 0, self.SQUEUE, ""
            if "scancel" in script:
                return 0, "", ""
            return 0, ("D|ok|/scratch/me/llm\nS|2048|/scratch/me/llm/models\nS|512|/scratch/me/llm/runs\n"
                       "S|2600|/scratch/me/llm\nI|1234|/scratch/me/llm\nM|/home/me/.local/tunnels\n"
                       'HPCLIB_QUOTA {"host": "login1", "settings_file": "/home/me/.local/tunnels/settings/quota.sh", '
                       '"storage": {"command": "showquota", "source": "default", "exit_code": 0, '
                       '"output": ["Disk  Usage  Limit", "/scratch  612G  1T"], "saved": null}, '
                       '"project": {"command": "myproject", "source": "default", "exit_code": 0, '
                       '"output": ["Project 122800 5000 SU"], "saved": null}}\n'), ""
        self.clusters.script_runner = runner

    def test_jobs_grouped_and_labelled(self):
        status, out = self.call("GET", f"/api/clusters/{self.dead}/jobs")
        self.assertEqual(status, 200, out)
        self.assertEqual([j["id"] for j in out["queued"]], ["101"])
        self.assertEqual(out["queued"][0]["source"], {"kind": "agent", "label": "agent: xtb_opt", "token": "llm"})
        self.assertEqual([j["source"]["kind"] for j in out["running"]], ["agent-tunnel", "app", "other"])
        self.assertEqual([j["id"] for j in out["finished"]], ["90", "91"])          # 101 is still queued
        self.assertEqual(out["finished"][0]["peak_memory_mb"], 5120.0)
        self.assertEqual(out["counts"], {"queued": 1, "running": 3, "finished": 2, "failed": 1})   # the VS Code timeout isn't

    def test_cancel_only_agent_and_other_jobs(self):
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/jobs/cancel", {"job": "103"})[0], 409)   # an app
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/jobs/cancel", {"job": "102"})[0], 409)   # the tunnel
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/jobs/cancel", {"job": "999"})[0], 404)
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/jobs/cancel", {"job": "1;rm"})[0], 400)
        status, out = self.call("POST", f"/api/clusters/{self.dead}/jobs/cancel", {"job": "104"})
        self.assertEqual((status, out["cancelled"]), (200, "104"))
        self.assertEqual(self.ran[-1][1], ("104",))

    def test_storage_scan_and_cache(self):
        self.assertIsNone(self.call("GET", f"/api/clusters/{self.dead}/storage")[1]["scanned"])
        self.assertEqual(self.call("POST", f"/api/clusters/{self.dead}/storage/scan")[0], 202)
        for _ in range(50):
            out = self.call("GET", f"/api/clusters/{self.dead}/storage")[1]
            if out["scan"]["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(out["scan"]["state"], "done", out)
        llm, tunnels = out["dirs"]
        self.assertEqual((llm["path"], llm["kb"], llm["files"], llm["partial"]), ("/scratch/me/llm", 2600, 1234, False))
        self.assertEqual([c["kb"] for c in llm["children"]], [2048, 512])
        self.assertTrue(tunnels["missing"])
        self.assertEqual(out["quota"], {"command": "showquota", "lines": ["Disk  Usage  Limit", "/scratch  612G  1T"]})
        self.assertEqual(out["quotas"]["project"]["output"], ["Project 122800 5000 SU"])
        self.assertIn("hpclib_quota all --json", self.ran[-1][0])         # lib/quota.sh goes along
        self.assertEqual(self.ran[-1][1][1:], ("/scratch/me/llm", "~/.local/tunnels"))
        self.assertEqual(stat.S_IMODE(os.stat(self.clusters.storage_path(self.dead)).st_mode), 0o600)

    def test_scripts_parse(self):
        for script in (agent_console.CLUSTER_JOBS_SCRIPT, agent_console.CLUSTER_STORAGE_SCRIPT):
            self.assertEqual(subprocess.run(["bash", "-n"], input=script, text=True).returncode, 0)


class TestQuota(ConsoleTestCase):
    """The quota routes, with lib/quota.sh run here (standing in for the login node) against fake site commands."""

    def setUp(self):
        super().setUp()
        self.clusters.logins.alive = lambda profile: True
        self.remote_data = Path(self.tmp) / "remote-data"
        bin_dir = Path(self.tmp) / "site-bin"
        bin_dir.mkdir()
        (bin_dir / "myproject").write_text("#!/bin/sh\necho 'Project 122800 balance 5000 SU'\n")
        (bin_dir / "myproject").chmod(0o755)
        self.ran = []

        def runner(profile, script, args=(), wait=60):
            self.ran.append(tuple(args))
            env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(self.tmp),
                   "HPCTUNNELS_DATA_DIR": str(self.remote_data)}
            res = subprocess.run(["bash", "-s", "--", *args], input=script, capture_output=True, text=True,
                                 env=env, timeout=wait)
            return res.returncode, res.stdout, res.stderr
        self.clusters.script_runner = runner
        for var in agent_console.QUOTA_ENV:
            patch = mock.patch.dict(os.environ)
            patch.start()
            self.addCleanup(patch.stop)
            os.environ.pop(var, None)

    def test_check_and_configure(self):
        status, out = self.call("GET", f"/api/clusters/{self.dead}/quota")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["quotas"]["project"]["output"], ["Project 122800 balance 5000 SU"])
        self.assertEqual(out["quotas"]["project"]["source"], "default")
        # a cluster without showquota: set the storage check, and turn the project one off
        status, out = self.call("PUT", f"/api/clusters/{self.dead}/quota/settings",
                                {"storage": "echo 'scratch 10%'", "project": "none"})
        self.assertEqual(status, 200, out)
        self.assertEqual((out["quotas"]["storage"]["source"], out["quotas"]["storage"]["output"]),
                         ("settings", ["scratch 10%"]))
        self.assertEqual(out["quotas"]["project"]["source"], "none")
        saved = (self.remote_data / "settings" / "quota.sh").read_text()
        self.assertIn("HPCLIB_PROJECT_QUOTA_CHECK=none", saved)
        # kept with the storage scan, for the Storage tab
        storage = self.call("GET", f"/api/clusters/{self.dead}/storage")[1]
        self.assertEqual(storage["quota"], {"command": "echo 'scratch 10%'", "lines": ["scratch 10%"]})
        # "" goes back to the default; a check of one kind keeps the other
        self.call("PUT", f"/api/clusters/{self.dead}/quota/settings", {"project": ""})
        status, out = self.call("GET", f"/api/clusters/{self.dead}/quota?kind=project")
        self.assertEqual(out["quotas"]["project"]["command"], "myproject")
        self.assertEqual(out["quotas"]["storage"]["source"], "settings")
        self.assertEqual(self.ran[-1], ("project", "--json"))

    def test_console_environment_wins(self):
        os.environ["HPCLIB_PROJECT_QUOTA_CHECK"] = "echo from-console"
        out = self.call("GET", f"/api/clusters/{self.dead}/quota")[1]
        self.assertEqual((out["quotas"]["project"]["source"], out["quotas"]["project"]["output"]),
                         ("env", ["from-console"]))
        self.assertEqual(out["quotas"]["environment"], ["HPCLIB_PROJECT_QUOTA_CHECK"])

    def test_refused(self):
        for body in ({}, {"disk": "x"}, {"storage": "a\nb"}, {"storage": 3}, {"project": "x" * 1001}):
            self.assertEqual(self.call("PUT", f"/api/clusters/{self.dead}/quota/settings", body)[0], 400, body)
        self.assertEqual(self.call("GET", f"/api/clusters/{self.dead}/quota?kind=disk")[0], 400)
        self.assertEqual(self.ran, [])
        self.clusters.logins.alive = lambda profile: False
        self.assertEqual(self.call("GET", f"/api/clusters/{self.dead}/quota")[0], 409)


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

    def test_rest_on_login_node(self):
        self.assertEqual(self.call("GET", f"/api/clusters/{self.live}/settings")[1]["rest_on"], "job")
        status, out = self.call("PUT", f"/api/clusters/{self.live}/settings", {"rest_on": "login"})
        self.assertEqual((status, out["rest_on"]), (200, "login"), out)
        self.assertEqual(self.call("PUT", f"/api/clusters/{self.live}/settings", {"rest_on": "node1"})[0], 422)
        agent_profiles.cmd_set(self.live, "tunnel_args=--time=12:00:00")
        repo = Path(__file__).resolve().parents[1]
        script = (f'source {repo / "hpclib" / "hpclib.sh"}; launch_tunnel() {{ printf "%s\\n" "$*"; }}; '
                  f'agent_tunnel {self.live}')
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, HPCLIB_AGENTS_DIR=str(self.agents)))
        self.assertIn(" rest --process-port=", res.stdout)
        self.assertIn(" --login-node -- ", res.stdout)
        self.assertNotIn("--time=12:00:00", res.stdout)                            # no job, no sbatch options
        self.assertEqual(agent_profiles.cmd_set(self.live, "rest_on=elsewhere"), 2)

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
    if mode in ("duo", "tacc"):
        code, answer = ask("(me@" + host + ") Password: ")
        if code or answer != os.environ["FAKE_SSH_PASSWORD"]:
            ask("(me@" + host + ") Password: ")
            print("Permission denied (keyboard-interactive).", file=sys.stderr)
            sys.exit(255)
        prompt = "(me@" + host + ") TACC Token Code: " if mode == "tacc" else (
            "Duo two-factor login for me\\n\\nEnter a passcode or select one of the following "
            "options:\\n\\n 1. Duo Push to XXX-XXX-1234\\n\\nPasscode or option (1-1): ")
        code, answer = ask(prompt)
        if code or answer != ("123456" if mode == "tacc" else "1"):
            sys.exit(255)
        time.sleep(float(os.environ.get("FAKE_PUSH_SECONDS", "0.6")))   # the phone
    open(master, "w").close()
    sys.exit(0)
""")


class TestLogin(ConsoleTestCase):

    def test_tacc_code_entry_is_one_time_and_never_in_status_or_logs(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_MODE": "tacc"}):
            out, _ = self.login({"password": "hunter2"}, until=("code_required", "failed"))
            self.assertEqual(out["state"], "code_required", out)
            route = f"/api/clusters/{self.dead}/login/answer"
            self.assertEqual(self.call("POST", route, {"code": "123456", "challenge": out["challenge"]}, key=None)[0], 401)
            self.assertEqual(self.call("POST", route, {"code": "123456", "challenge": "stale"})[0], 409)
            self.assertEqual(self.call("POST", route, {"code": "bad", "challenge": out["challenge"]})[0], 422)
            status, answer = self.call("POST", route, {"code": "123456", "challenge": out["challenge"]})
            self.assertEqual(status, 202, answer)
            self.assertNotIn("123456", json.dumps(answer))
            self.assertEqual(self.call("POST", route, {"code": "123456", "challenge": out["challenge"]})[0], 409)
            for _ in range(40):
                done = self.call("GET", f"/api/clusters/{self.dead}/login")[1]
                if done["state"] == "connected": break
                time.sleep(0.1)
            self.assertEqual(done["state"], "connected", done)
            self.assertNotIn("123456", json.dumps(done))
            for path in self.console.rglob("*.log"):
                self.assertNotIn("123456", path.read_text())

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

    def test_setup_cluster_ssh_uses_saved_login_without_a_rest_tunnel(self):
        route = f"/api/clusters/{self.dead}/ssh/setup"
        self.assertEqual(self.call("POST", route, {}, key=None)[0], 401)
        self.assertEqual(self.call("POST", route, {})[0], 409)
        agent_profiles.cmd_set(self.dead, "login=-p", "login+=2222", "login+=-i", "login+=/keys/my key", f"login+={self.host}")
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.assertEqual(self.call("POST", route, {"path": "/arbitrary"})[0], 422)
            status, out = self.call("POST", route, {})
            self.assertEqual((status, out["kind"]), (202, "ssh_setup"), out)
            self.assertEqual(self.shell.calls[-1], ["setup_cluster_ssh", "-p", "2222", "-i", "/keys/my key", self.host])
            done = self.wait_op(self.dead)
            self.assertEqual((done["state"], done["exit_code"]), ("succeeded", 0))

    def test_versions(self):
        local = agent_console.local_hpclib_version()
        self.assertRegex(local, r"^\d+\.\d+\.\d+$")
        self.assertEqual(self.call("GET", "/api/health")[1]["hpclib_version"], local)
        tunnel = self.call("GET", f"/api/clusters/{self.live}")[1]["tunnel"]
        self.assertEqual(tunnel["hpclib_version"], local)                # the test server runs from this repo


class FakeApp(http.server.BaseHTTPRequestHandler):
    """Stands in for JupyterLab (JSON at /api) or for the tunnel's waiting page while the job is queued."""
    waiting = False
    answer = b'{"version": "4.2.5"}'

    def do_GET(self):
        if self.waiting:
            body, ctype = (b"<html><h2>Waiting for jupyter&hellip;</h2>\n<p>job 123 queued (Priority) - poll #4</p>"
                           b"</html>", "text/html")
        else:
            body, ctype = self.answer, "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class TestApps(ConsoleTestCase):

    def test_optional_node_list_can_be_saved_and_cleared(self):
        base = f"/api/apps/jupyter/{self.dead}/settings"
        for node in ("fc041", "fc041,fc042", "fc[041-042]"):
            with self.subTest(node=node):
                args = ["--time=12:00:00", f"--nodelist={node}"]
                status, out = self.call("PUT", base, {"tunnel_args": args})
                self.assertEqual(status, 200, out)
                self.assertEqual(self.call("GET", base)[1]["tunnel_args"], args)
        for bad in ("--nodelist=", "--nodelist=fc041;id", "--nodelist=$(id)", "--nodelist=fc041 --wrap=x"):
            self.assertEqual(self.call("PUT", base, {"tunnel_args": [bad]})[0], 422, bad)
        status, out = self.call("PUT", base, {"tunnel_args": ["--time=12:00:00"]})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["tunnel_args"], ["--time=12:00:00"])

    def serve(self, port, waiting=False, answer=FakeApp.answer):
        handler = type("H", (FakeApp,), {"waiting": waiting, "answer": answer})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def port(self, name=None):
        return agent_profiles.load(name or self.dead)["apps"]["jupyter"]["port"]

    def test_health_answer_that_is_not_an_object(self):
        # a health path may answer with any JSON: CMCC DB's /api/fetch_datasets is a list
        self.call("GET", f"/api/apps/jupyter/{self.dead}")
        self.serve(self.port(), answer=b'[{"name": "dataset"}]')
        status, out = self.call("GET", f"/api/apps/jupyter/{self.dead}")
        self.assertEqual((status, out["state"], out["version"]), (200, "up", None), out)

    def test_listing_and_ports(self):
        out = self.call("GET", "/api/apps")[1]
        self.assertEqual([a["id"] for a in out["apps"]], ["agents", "jupyter", "vscode", "transfer"])   # PAI is a package now
        self.assertEqual({a["id"]: a.get("kind") for a in out["apps"]}["transfer"], "login")
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
    """vscode and a packaged shared app (PAI's shape): settings saved on the cluster, Check and Install with
    install.sh, shared instances."""
    serve = TestApps.serve

    def setUp(self):
        super().setUp()
        self.clusters.package_add(shared_app_package(self.tmp))

    def wait_op(self, name):
        for _ in range(50):
            out = self.call("GET", f"/api/clusters/{name}/operation")[1]
            if out.get("state") != "running":
                return out
            time.sleep(0.1)
        return out

    def test_install_continues_over_rest_with_logs_and_force(self):
        self.clusters.save_app_settings(agent_profiles.load(self.dead), "pai",
                                        {"install_modules": ["GCC/12.2.0", "WebProxy"]})
        self.shell.install_elsewhere = "no Singularity on login"
        sent = []
        ident = "a" * 32
        chunks = ["pulling image: 50%\n", "HPCLIB_TUNNEL_STATUS installed installed: remote image\n"]
        offset = 0

        def rest(profile, verb, route, query=None, body=None, **kwargs):
            nonlocal offset
            sent.append((verb, route, query, json.loads(body) if body else None))
            if verb == "POST":
                out = {"id": ident, "node": "compute01", "state": "running"}
            else:
                self.assertEqual(query, {"id": ident, "offset": offset})
                text = chunks.pop(0)
                offset += len(text.encode())
                out = {"state": "running" if chunks else "succeeded", "output": text,
                       "offset": offset, "more": False, "exit_code": None if chunks else 0}
            return 202 if verb == "POST" else 200, "application/json", json.dumps(out).encode()

        with mock.patch.object(self.clusters.logins, "alive", return_value=True), \
                mock.patch.object(self.clusters, "tunnel_state", return_value={"state": "up"}), \
                mock.patch.object(self.clusters, "call", side_effect=rest), \
                mock.patch.object(self.clusters, "INSTALL_POLL_SECONDS", 0.001), \
                mock.patch.object(agent_console, "read_private", return_value="owner-secret"):
            status, out = self.call("POST", f"/api/apps/pai/{self.dead}/install", {"force": True})
            self.assertEqual(status, 202, out)
            result = self.wait_op(self.dead)
        self.assertEqual((result["state"], result["exit_code"]), ("succeeded", 0), result)
        self.assertEqual(result["rest_install_id"], ident)
        self.assertEqual(sent[0], ("POST", "/admin/tunnel-install", None, {"tunnel": "pai", "force": True}))
        setup = [call for call in self.shell.calls if call[0] == "tunnel_setup"][-1]
        self.assertIn("HPCLIB_INSTALL_MODULES=GCC/12.2.0:WebProxy", setup)
        self.assertIn("pulling image: 50%", "\n".join(result["log"]))
        self.assertEqual(agent_profiles.load(self.dead)["apps"]["pai"]["install"]["state"], "installed")

    def test_install_module_defaults_overrides_and_validation(self):
        spec = agent_console.APPS["pai"]
        with mock.patch.dict(spec, {"install_modules": ["WebProxy"]}):
            profile = agent_profiles.load(self.dead)
            self.assertEqual(self.clusters.app_settings(profile, "pai")["install_modules"], ["WebProxy"])
            for modules in (["GCC/12.2.0", "WebProxy"], [], None):
                out = self.clusters.save_app_settings(profile, "pai", {"install_modules": modules})
                expected = ["WebProxy"] if modules is None else modules
                self.assertEqual(out["install_modules"], expected)
                self.assertIn("HPCLIB_INSTALL_MODULES=" + ":".join(expected),
                              self.clusters.setup_args(profile, "pai", "--install"))
            for value in ("WebProxy", ["a;b"], ["--force"], [1], ["x"] * 33):
                with self.subTest(value=value), self.assertRaises(agent_console.ConsoleError):
                    self.clusters.save_app_settings(profile, "pai", {"install_modules": value})

    def test_install_rest_prerequisites_and_failures(self):
        self.shell.install_elsewhere = "no runtime on login"
        cases = [("down", "owner", None, "start the agent tunnel"),
                 ("up", None, None, "owner token"),
                 ("up", "owner", (404, "application/json", b'{"error":"unknown route"}'), "0.2.71"),
                 ("up", "owner", (200, "text/html", b'<html>queued</html>'), "queued or starting")]
        for state, owner, response, message in cases:
            with self.subTest(message=message), \
                    mock.patch.object(self.clusters.logins, "alive", return_value=True), \
                    mock.patch.object(self.clusters, "tunnel_state", return_value={"state": state}), \
                    mock.patch.object(agent_console, "read_private", return_value=owner), \
                    mock.patch.object(self.clusters, "call", return_value=response):
                self.assertEqual(self.call("POST", f"/api/apps/pai/{self.dead}/install")[0], 202)
                out = self.wait_op(self.dead)
                self.assertEqual(out["state"], "failed", out)
                self.assertIn(message, "\n".join(out["log"]))

    def test_remote_installer_failure_is_not_success(self):
        self.shell.install_elsewhere = "no runtime"
        for code in (1, 3):
            with self.subTest(code=code), \
                    mock.patch.object(self.clusters.logins, "alive", return_value=True), \
                    mock.patch.object(self.clusters, "tunnel_state", return_value={"state": "up"}), \
                    mock.patch.object(agent_console, "read_private", return_value="owner"), \
                    mock.patch.object(self.clusters, "call", side_effect=[
                        (202, "application/json", json.dumps({"id": "b" * 32}).encode()),
                        (200, "application/json", json.dumps({"state": "failed", "output": "pull failed\n",
                            "offset": 12, "more": False, "exit_code": code}).encode())]):
                self.assertEqual(self.call("POST", f"/api/apps/pai/{self.dead}/install")[0], 202)
                out = self.wait_op(self.dead)
                self.assertEqual(out["state"], "failed", out)
                self.assertIn("pull failed", "\n".join(out["log"]))
                if code == 3:
                    self.assertIn("REST server's node either", "\n".join(out["log"]))

    def test_rest_install_answers_are_bounded(self):
        """A REST server that never progresses, or sends too much, can't spin the console or fill its log."""
        self.shell.install_elsewhere = "no runtime"
        started = (202, "application/json", json.dumps({"id": "c" * 32, "node": "bad\nnode"}).encode())
        stuck = (200, "application/json", json.dumps({"state": "running", "output": "", "offset": 0,
                                                       "more": True, "exit_code": None}).encode())
        huge = (200, "application/json", json.dumps({"state": "running", "output": "x" * (2 << 20),
                                                      "offset": 2 << 20, "more": True, "exit_code": None}).encode())
        for answers, message in (([started] + [stuck] * 1000, "ran over"), ([started, huge], "invalid installation")):
            calls = []
            def rest(*a, **k):
                calls.append(a)
                return answers[min(len(calls) - 1, len(answers) - 1)]
            with self.subTest(message=message), \
                    mock.patch.object(self.clusters.logins, "alive", return_value=True), \
                    mock.patch.object(self.clusters, "tunnel_state", return_value={"state": "up"}), \
                    mock.patch.object(agent_console, "read_private", return_value="owner"), \
                    mock.patch.object(self.clusters, "INSTALL_POLL_SECONDS", 0.05), \
                    mock.patch.object(self.clusters, "INSTALL_DEADLINE", 0.5), \
                    mock.patch.object(self.clusters, "call", side_effect=rest):
                self.assertEqual(self.call("POST", f"/api/apps/pai/{self.dead}/install")[0], 202)
                out = self.wait_op(self.dead)
                self.assertEqual(out["state"], "failed", out)
                log = "\n".join(out["log"])
                self.assertIn(message, log)
                self.assertLess(len(calls), 40)                        # waited between answers without progress
                self.assertNotIn("bad\nnode", log)
                self.assertIsNone(out.get("rest_node"))

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
                                                    "HPCLIB_INSTALL_MODULES=", "--set",
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

    def test_a_packaged_app_can_be_shared_and_updatable(self):
        apps = {a["id"]: a for a in self.call("GET", "/api/apps")[1]["apps"]}
        self.assertEqual(apps["pai"]["package"], "pai")
        self.assertEqual(agent_console.APPS["pai"]["instance_label"], "database")
        self.assertTrue(agent_console.APPS["pai"]["shared"])
        # refused when inconsistent
        for bad in ({"shared": "yes"}, {"instance_label": "x"}, {"reinstall": {"label": ""}}, {"reinstall": []}):
            members = {"hpclib-package.json": b"", "t/sbatch_script.sh": b"x"}
            manifest = {"format": 1, "name": "bad", "apps": {"b": dict({"title": "B", "tunnel": "t"}, **bad)},
                        "tunnels": {"t": "t"}}
            with self.assertRaises(console_packages.PackageError, msg=bad):
                console_packages.check_manifest(manifest, members, set())

    def test_pai_update(self):
        # PAI, once installed, updates: its checkout fast-forwarded and its image pulled again (install.sh --force)
        base = f"/api/apps/pai/{self.dead}"
        state = self.call("GET", base)[1]
        self.assertEqual(state["reinstall"]["label"], "Update")
        self.assertNotIn("reinstall", self.call("GET", f"/api/apps/vscode/{self.dead}")[1])
        self.assertIn("PAI_IMAGE", [f["name"] for f in self.call("GET", base + "/settings")[1]["fields"]])
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.shell.setup_status = "installed installed: /s/pai/proto-auto-interface"
            status, out = self.call("POST", base + "/install", {"force": True})
            self.assertEqual((status, out["title"]), (202, "update PAI"), out)
            self.assertEqual(self.shell.calls[-1][-3:], ["--install", "--force", "--check"])
            self.assertEqual(self.wait_op(self.dead)["state"], "succeeded")

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
    serve = TestApps.serve

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
        self.assertEqual((state["kind"], state["auth"], state["state"], state["signing_in"]), ("login", None, "down", False))
        self.assertEqual([c["id"] for c in state["controls"]], ["status"])
        self.assertEqual(self.call("POST", self.route + "/start")[0], 409)                    # needs the login
        settings = self.call("GET", self.route + "/settings")[1]
        self.assertFalse(settings["job"])                                  # the login node's: no sbatch options
        self.assertIn("SMB_GUI", [f["name"] for f in settings["fields"]])
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
            self.shell.smb_status.update(host="10.0.0.12", kinit="host", credentials=False,
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
        for given in ("//10.0.0.12/research/our_group", "\\\\10.0.0.12\\research\\our_group"):
            out = self.call("PUT", self.route + "/settings", {"settings": {"SMB_HOST": given}})[1]
            self.assertEqual(out["settings"], {"SMB_HOST": "10.0.0.12", "SMB_ROOT": "research/our_group"}, given)
        # and they reach the cluster before the next status
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.call("POST", self.route + "/control/status", {})
        pushed = self.shell.calls[-2]
        self.assertEqual(pushed[:3], ["tunnel_setup", self.host, "data-transfer"])
        self.assertIn("SMB_ROOT=research/our_group", pushed)

    def test_password_mode_controls_with_kerberos_available(self):
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            for host in ("files.example.edu", "10.0.0.12"):
                for ticket in (None, {"principal": "me@AUTH.EXAMPLE.EDU", "expires": "later"}):
                    for saved in (False, True):
                        with self.subTest(host=host, ticket=bool(ticket), saved=saved):
                            self.shell.smb_status.update(host=host, auth="password", kinit="host",
                                                         rclone_kerberos=True, ticket=ticket, credentials=saved)
                            status, out = self.call("POST", self.route + "/control/status", {})
                            self.assertEqual(status, 200, out)
                            state = self.call("GET", self.route)[1]
                            control = "forget_credentials" if saved else "save_credentials"
                            self.assertEqual([c["id"] for c in state["controls"]], [control, "status"])
                            signin = {t["id"]: t["text"] for t in state["tools"]}["signin"]
                            if saved:
                                self.assertIn("a saved password is used", signin)
                                self.assertIn("password sign-in is selected", state["controls"][0]["confirm"])
                            else:
                                self.assertIn("password sign-in selected", signin)
                                self.assertEqual(state["controls"][0]["label"], "Save password for sync jobs")
                                self.assertTrue(state["controls"][0]["primary"])

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


class TestTransferGui(ConsoleTestCase):
    """Data transfer's Start: a web GUI on the login node (rclone's, or SMB_GUI's), its port forwarded over the login."""
    serve = TestApps.serve

    def setUp(self):
        super().setUp()
        self.route = f"/api/apps/transfer/{self.dead}"
        self.call("GET", self.route)          # picks its ports
        self.shell.setup_status = "installed /x/data-transfer-tools.sif"    # Start checks first: settings unsent
        self.port = agent_profiles.load(self.dead)["apps"]["transfer"]["port"]
        self.host = agent_profiles.load(self.dead)["host"]
        self.log = self.console / "logs" / f"{self.dead}.transfer.log"

    def start(self):
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            status, out = self.call("POST", self.route + "/start")
        self.assertEqual(status, 202, out)
        self.assertEqual(self.shell.calls[-1], ["smbshell", "--on", "-L", f"127.0.0.1:{self.port}:127.0.0.1:{self.port}",
                                                self.host, "gui", "--port", str(self.port)])

    def printed(self, line):
        self.log.write_text(self.log.read_text() + line + "\r\n")

    def serve_rclone(self):
        self.start()
        calls = []
        state = {"stats": {"transferring": [], "checking": []}, "fail": {}}

        class Rclone(FakeApp):
            def do_POST(self):
                calls.append((self.path, self.headers.get("Authorization"),
                              json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))))
                status, body = state["fail"].get(self.path, (200, state["stats"] if self.path == "/core/stats" else {}))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if status == 302:
                    self.send_header("Location", "/must-not-follow")
                self.end_headers()
                self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

        server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Rclone)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.printed("HPCLIB_TRANSFER_GUI port=%d user=hpclib pass=0a1b2c query=login_token=fixture" % self.port)
        return calls, state

    def test_open_clears_idle_cache_once_and_polls_do_not(self):
        calls, _ = self.serve_rclone()
        for _ in range(2):
            state = self.call("GET", self.route)[1]
            self.assertTrue(state["prepare_open"])
        self.assertEqual(calls, [])
        self.assertEqual(self.call("POST", self.route + "/open", {}, key=None)[0], 401)
        self.assertEqual(calls, [])
        status, out = self.call("POST", self.route + "/open", {})
        self.assertEqual(status, 200, out)
        self.assertTrue(out["cache_cleared"])
        self.assertEqual(out["url"], state["url"])
        self.assertEqual(calls, [("/core/stats", "Basic aHBjbGliOjBhMWIyYw==", {}),
                                 ("/fscache/clear", "Basic aHBjbGliOjBhMWIyYw==", {})])
        self.call("GET", self.route)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("0a1b2c", json.dumps(agent_profiles.load(self.dead)))

    def test_open_preserves_active_transfers_and_checks(self):
        calls, state = self.serve_rclone()
        for active in ("transferring", "checking"):
            with self.subTest(active=active):
                calls.clear()
                state["stats"] = {active: [{"name": "private-name"}]}
                status, out = self.call("POST", self.route + "/open", {})
                self.assertEqual(status, 409, out)
                self.assertIn("wait for it to finish", out["error"])
                self.assertNotIn("private-name", json.dumps(out))
                self.assertEqual([c[0] for c in calls], ["/core/stats"])

    def test_open_propagates_reset_failures_without_secrets_or_redirects(self):
        calls, state = self.serve_rclone()
        cases = [("/core/stats", 401, {"error": "0a1b2c private response"}),
                 ("/core/stats", 200, b"not json"),
                 ("/core/stats", 200, []),
                 ("/core/stats", 302, {}),
                 ("/fscache/clear", 500, {"error": "0a1b2c private response"})]
        for route, code, body in cases:
            with self.subTest(route=route, code=code, body=body):
                calls.clear()
                state["fail"] = {route: (code, body)}
                status, out = self.call("POST", self.route + "/open", {})
                self.assertEqual(status, 502, out)
                self.assertIn(route.lstrip("/"), out["error"])
                self.assertNotIn("0a1b2c", json.dumps(out))
                self.assertNotIn("private response", json.dumps(out))
                self.assertEqual([c[0] for c in calls], ["/core/stats"] +
                                 (["/fscache/clear"] if route == "/fscache/clear" else []))

    def test_open_requires_running_gui_and_stays_out_of_other_apps(self):
        self.assertEqual(self.call("POST", self.route + "/open", {})[0], 409)
        self.assertEqual(self.call("POST", f"/api/apps/jupyter/{self.dead}/open", {})[0], 404)

    def test_start_and_open(self):
        self.assertEqual(self.call("GET", self.route)[1]["state"], "down")
        self.start()
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            # Check sends its settings, as for any app that has them
            self.call("POST", self.route + "/check")
            self.assertEqual(self.shell.calls[-1][:3], ["tunnel_setup", self.host, "data-transfer"])
            self.assertEqual(self.shell.calls[-1][-2:], ["--save", "--check"])
        self.serve(self.port)
        state = self.call("GET", self.route)[1]
        self.assertEqual((state["state"], state["url"]), ("starting", None), state)    # nothing printed yet
        # once it says where, Open goes straight in: rclone's GUI with its login in the address and login_token
        self.printed("HPCLIB_TRANSFER_GUI port=%d user=hpclib pass=0a1b2c query=login_token=aHBjbGliOjBhMWIyYw%%3D%%3D"
                     % self.port)
        state = self.call("GET", self.route)[1]
        self.assertEqual(state["state"], "up", state)
        self.assertEqual(state["url"], f"http://hpclib:0a1b2c@127.0.0.1:{self.port}/?login_token=aHBjbGliOjBhMWIyYw%3D%3D")
        self.assertEqual((state["username"], state["password"]), ("hpclib", "0a1b2c"))
        self.call("POST", self.route + "/stop")
        self.assertEqual(self.shell.calls[-1], ["stop_tunnel", "-P", str(self.port), self.host])

    def test_other_guis(self):
        self.start()
        self.serve(self.port)
        # a GUI of your own (SMB_GUI): where it starts, and no login in the address if it gives none
        self.printed("HPCLIB_TRANSFER_GUI port=%d path=/browse query=root=research%%2Fours" % self.port)
        state = self.call("GET", self.route)[1]
        self.assertEqual(state["url"], f"http://127.0.0.1:{self.port}/browse?root=research%2Fours")
        self.assertNotIn("username", state)
        self.assertFalse(state["prepare_open"])
        status, out = self.call("POST", self.route + "/open", {})
        self.assertEqual(status, 200, out)
        self.assertEqual(out, {"url": state["url"], "cache_cleared": False, "ok": True})
        # an hpclib on the cluster from before SMB_GUI: rclone's line, opened as before
        self.printed("HPCLIB_RCLONE_GUI port=%d user=hpclib pass=0a1b2c" % self.port)
        self.assertEqual(self.call("GET", self.route)[1]["url"],
                         f"http://hpclib:0a1b2c@127.0.0.1:{self.port}/?login_token=aHBjbGliOjBhMWIyYw%3D%3D")
        self.assertTrue(self.call("GET", self.route)[1]["prepare_open"])

    def test_signing_in_beside_the_gui(self):
        script = self.tmp / "fake-kinit"
        script.write_text(FAKE_KINIT)
        script.chmod(0o755)
        self.start()
        self.serve(self.port)
        self.printed("HPCLIB_TRANSFER_GUI port=%d user=hpclib pass=0a1b2c" % self.port)
        self.clusters.tunnel_runner = lambda args, log_path: agent_console.PtySession([str(script)], log_path, dict(os.environ))
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.assertEqual(self.call("POST", self.route + "/control/kinit", {})[0], 200)
            for _ in range(50):
                state = self.call("GET", self.route)[1]
                if state["prompt"]:
                    break
                time.sleep(0.1)
            # the GUI runs on: its process, and the login it printed, are its own
            self.assertEqual((state["state"], state["signing_in"]), ("up", True), state)
            self.assertTrue(state["url"].startswith("http://hpclib:0a1b2c@"))
            self.assertIn("Kerberos", state["prompt"]["note"])
            self.assertEqual(self.call("POST", self.route + "/answer", {"answer": "kerberos-pass"})[0], 200)
            for _ in range(50):
                if not self.call("GET", self.route)[1]["signing_in"]:
                    break
                time.sleep(0.1)
        state = self.call("GET", self.route)[1]
        self.assertEqual((state["state"], state["signing_in"]), ("up", False), state)
        self.assertTrue(state["url"].startswith("http://hpclib:0a1b2c@"))
        self.assertIsNotNone(self.clusters.app_procs.get((self.dead, "transfer")))
        self.assertIn("signed in: me@AUTH.EXAMPLE.EDU", self.log.read_text())


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

    def test_delete_ssh_keys_forwards_the_targeted_body(self):
        from ssh_keys import SSHKeyStore
        self.server.ssh_keys = SSHKeyStore(self.tmp / "shared-home")
        base = f"/api/clusters/{self.live}/rest/admin/ssh/"
        status, out = self.call("POST", base + "keys", {"name": "github"})
        self.assertEqual(status, 200, out)
        status, authorized = self.call("POST", base + "authorized", {"line": out["key"]["public_key"]})
        self.assertEqual(status, 200, authorized)
        status, hosts = self.call("PUT", base + "hosts", {"host": "github.com", "key": "github", "user": "git"})
        self.assertEqual(status, 200, hosts)
        for route, body in (("hosts", {"host": "github.com", "revision": hosts["revision"]}),
                            ("authorized", {"id": authorized["entries"][0]["id"], "revision": authorized["revision"]}),
                            ("keys", {"name": "github"})):
            status, out = self.call("DELETE", base + route, body)
            self.assertEqual(status, 200, out)
        self.assertEqual(self.server.ssh_keys.keys()["keys"], [])
        self.assertEqual(self.server.ssh_keys.authorized()["entries"], [])
        self.assertFalse(any(h["managed"] for h in self.server.ssh_keys.hosts()["hosts"]))

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
