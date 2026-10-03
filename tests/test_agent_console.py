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
        agent_profiles.cmd_set(self.live, "auto_approve_templates=new", "tunnel_args=--time=12:00:00")
        script = (f'source {repo / "hpclib" / "hpclib.sh"}; launch_tunnel() {{ printf "%s\\n" "$*"; }}; '
                  f'agent_tunnel {self.live}; agent_tunnel {self.live} --review-templates --time=2:00:00')
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, HPCLIB_AGENTS_DIR=str(self.agents)))
        first, second = res.stdout.splitlines()
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
