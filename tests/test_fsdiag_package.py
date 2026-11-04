"""The filesystem diagnostics package (packages/fsdiag): it builds and installs as a console package, the probe measures
(here, a temporary directory), the rules say what a slow file system looks like, the request log is read for what slow
requests were waiting for, and its server answers only with its token. Compute-node jobs are checked as scripts: there is
no SLURM here."""
import ast
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages" / "fsdiag"
TUNNEL = PACKAGE / "tunnels" / "fsdiag"
sys.path.insert(0, str(ROOT / "hpclib" / "servers"))
sys.path.insert(0, str(TUNNEL))
import console_packages  # noqa: E402
import fsdiag_server  # noqa: E402
import fsfindings  # noqa: E402
import fsprobe  # noqa: E402
import fsrequests  # noqa: E402
import fsruns  # noqa: E402
import rest_actions  # noqa: E402,F401


def stat_block(median, p95=None):
    return {"n": 10, "min": median, "median": median, "p95": p95 if p95 is not None else median, "max": p95 if p95 is not None else median,
            "mean": median, "samples_ms": [median]}


def result(where, node, label, ops, **extra):
    place = {"label": label, "path": "/x/" + label, "real": "/x/" + label, "ops": {k: stat_block(v) for k, v in ops.items()}, "errors": []}
    place.update(extra)
    control = {"label": "control", "path": "/dev/shm", "ops": {k: stat_block(0.01) for k in ops}, "errors": []}
    return {"where": where, "node": node, "host": node, "paths": [place, control], "context": {}, "surveys": [], "quota": []}


class TestPackage(unittest.TestCase):
    def test_builds_and_installs(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        out, _ = console_packages.build(str(PACKAGE), str(tmp / "fsdiag.zip"))
        import agent_console
        with unittest.mock.patch.dict(os.environ, {"HPCLIB_CONSOLE_DIR": str(tmp / "console")}):
            store = agent_console.package_store()
        report = store.install(Path(out).read_bytes())
        self.assertEqual(report["package"]["name"], "fsdiag")
        _, app, _ = store.apps()["fsdiag"]
        self.assertEqual(app["tunnel"], "fsdiag")
        unpacked = tmp / "console" / "packages" / "fsdiag" / "tunnels" / "fsdiag"
        for f in ("fsdiag_server.py", "fsprobe.py", "fsrequests.py", "fsfindings.py", "fsruns.py", "fsdiag_cli.py", "token_auth.py",
                  "sbatch_script.sh", "install.sh", "tunnel_config.sh", "user.sh", "actions.json", "fsdiag_action.sh", "ui/index.html", "ui/app.js", "ui/client.js",
                  "ui/bridge.js", "ui/views/probe.js", "ui/views/results.js", "ui/views/requests.js", "ui/views/directories.js",
                  "ui/hpclib-ui/index.js", "ui/hpclib-ui/default-styles.css", "ui/hpclib-ui/theme.js", "ui/hpclib-ui/components/tabs.js"):
            self.assertTrue((unpacked / f).is_file(), f)

    def test_manifest_matches_the_tunnel_settings(self):
        manifest = json.loads((PACKAGE / "hpclib-package.json").read_text())
        app = manifest["apps"]["fsdiag"]
        self.assertIs(app["theme_link"], True)
        config = (TUNNEL / "tunnel_config.sh").read_text()
        listed = re.search(r'TUNNEL_SETTINGS="([^"]*)"', config).group(1).split()
        self.assertEqual(listed, [s["name"] for s in app["settings"]])
        defaults = (TUNNEL / "user.sh").read_text()
        for name in listed:
            self.assertIn("export " + name + "=", defaults)
        self.assertEqual(re.search(r"DEFAULT_PORT=(\d+)", config).group(1), re.search(r"PROCESS_PORT=(\d+)", config).group(1))

    def test_token_regex_finds_what_the_server_prints(self):
        pattern = re.compile(json.loads((PACKAGE / "hpclib-package.json").read_text())["apps"]["fsdiag"]["token_regex"])
        line = "hpclib fsdiag 1.0.0 on login1: http://127.0.0.1:8650/?token=Ab-c_123456789012345678901234"
        self.assertEqual(pattern.findall(line), ["Ab-c_123456789012345678901234"])

    def test_tunnel_scripts_parse_and_python36(self):
        for script in TUNNEL.glob("*.sh"):
            self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0, script)
        for py in sorted(TUNNEL.glob("*.py")):
            text = py.read_text()
            ast.parse(text, feature_version=(3, 6))
            # the login node's python3 may be 3.6: no walrus, capture_output, text=, removeprefix, f"{x=}"
            for newer in (":=", "capture_output", "text=True", "removeprefix", "removesuffix", "=}"):
                self.assertNotIn(newer, text, "{}: {}".format(py.name, newer))

    def test_the_page_has_no_colours_of_its_own(self):
        css = (TUNNEL / "ui" / "fsdiag.css").read_text()
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", css), [])
        self.assertNotIn("prefers-color-scheme", css)

    def test_the_page_uses_hpclib_ui_not_its_own_widgets(self):
        sources = {p.name: p.read_text() for p in (TUNNEL / "ui").rglob("*.js") if "hpclib-ui" not in p.parts}
        sources["index.html"] = (TUNNEL / "ui" / "index.html").read_text()
        self.assertIn("hpc-tabs", sources["index.html"])
        for name, text in sources.items():
            if name == "bridge.js":
                continue
            for old in ("setPointerCapture", "localStorage.setItem", "alert(", "confirm("):
                self.assertNotIn(old, text, "{}: {}".format(name, old))


class TestFindings(unittest.TestCase):
    def test_a_slow_place_is_a_problem_and_a_fast_one_is_ok(self):
        slow = result("login", "login1", "home", {"stat_missing": 400.0, "stat_existing": 350.0, "create": 120.0})
        found = fsfindings.findings([slow])
        self.assertEqual(found[0]["severity"], "problem")
        self.assertIn("home", found[0]["title"])
        fast = fsfindings.findings([result("login", "login1", "home", {"stat_missing": 0.2, "create": 0.5})])
        self.assertEqual([f["severity"] for f in fast], ["ok"])

    def test_a_node_that_differs_from_the_others_is_named(self):
        good = result("compute", "c01", "home", {"stat_missing": 0.3, "create": 0.5})
        bad = result("compute", "c02", "home", {"stat_missing": 300.0, "create": 400.0})
        found = fsfindings.findings([good, bad])
        text = " ".join(f["title"] + f["detail"] for f in found)
        self.assertIn("c02", text)
        self.assertEqual(found[0]["severity"], "problem")

    def test_slow_fsync_is_reported(self):
        found = fsfindings.findings([result("login", "l", "home", {"stat_missing": 0.2, "fsync": 400.0})])
        self.assertTrue(any("fsync" in f["title"] for f in found), found)

    def test_report_is_text_and_names_the_findings(self):
        slow = [result("login", "login1", "home", {"stat_missing": 400.0})]
        found = fsfindings.findings(slow)
        text = fsfindings.report_text("Title", slow, found, None)
        self.assertTrue(text.startswith("# Title"))
        self.assertIn("[PROBLEM]", text)


class TestRequests(unittest.TestCase):
    def audit(self, n, slow_every=5):
        t = time.time()
        out = []
        for i in range(n):
            slow = i % slow_every == 0
            out.append(json.dumps({"time": t - i, "verb": "GET", "path": "/v1/jobs/%d" % i if slow else "/v1/health",
                                   "status": 200, "duration_ms": 4000.0 if slow else 3.0, "cpu_ms": 5.0 if slow else 2.0}))
        return out

    def test_route_wait_and_slow_share(self):
        a = fsrequests.analyze(fsrequests.parse_audit(self.audit(50)), [], [], 1000.0)
        self.assertEqual((a["requests"]["count"], a["requests"]["slow"]), (50, 10))
        self.assertGreater(a["slow_wait_share"], 99)
        top = a["routes"][0]
        self.assertEqual(top["route"], "/v1/jobs/:id")
        self.assertEqual(top["slow"], 10)

    def test_a_stack_waiting_in_a_filesystem_call_is_classified(self):
        log = ["[2026-11-02 10:00:00] request still running after 6.0 s: GET /v1/jobs/3 (thread 5); it is waiting here:",
               '  File "/h/hpclib/servers/rest_server.py", line 10, in registry',
               "    conn.execute(sql)",
               '  File "/usr/lib/python3.6/os.py", line 1, in lstat',
               "    return lstat(path)"]
        slow, stacks = fsrequests.parse_session_log(log)
        self.assertEqual((len(slow), len(stacks)), (0, 1))
        category, frame = fsrequests.classify(stacks[0]["frames"])
        self.assertTrue(category)
        a = fsrequests.analyze([], [], stacks, 1000.0)
        self.assertEqual(a["stacks"]["count"], 1)

    def test_load_reads_the_audit_log_and_a_missing_one_is_not_an_error(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        empty = fsrequests.load(str(tmp), 24.0, 1000.0)
        self.assertEqual(empty["requests"]["count"], 0)
        (tmp / "rest").mkdir()
        (tmp / "rest" / "audit.log").write_text("\n".join(self.audit(20)) + "\nnot json\n")
        got = fsrequests.load(str(tmp), 24.0, 1000.0)
        self.assertEqual(got["requests"]["count"], 20)
        self.assertEqual(got["sources"]["audit_entries_with_timing"], 20)


class TestProbe(unittest.TestCase):
    def test_probe_a_directory_and_clean_up_after(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for i in range(30):
            (tmp / ("f%d" % i)).write_text("x")
        out = tmp / "out.json"
        code = fsprobe.main(["--path", "mine=" + str(tmp), "--repeats", "3", "--threads", "2", "--survey", str(tmp), "--no-quota", "--out", str(out)])
        self.assertEqual(code, 0)
        data = json.loads(out.read_text())
        mine = [p for p in data["paths"] if p["label"] == "mine"][0]
        for op in ("stat_missing", "create", "fsync", "rename", "sqlite_commit"):
            self.assertIn(op, mine["ops"], op)
        self.assertEqual(data["surveys"][0]["entries"], 30)
        self.assertEqual([p.name for p in tmp.iterdir() if p.name.startswith(".hpclib-fsdiag")], [])

    def test_a_read_only_place_gets_the_read_only_calls(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        tmp.chmod(0o500)
        self.addCleanup(tmp.chmod, 0o700)
        out = Path(tempfile.mkdtemp()) / "o.json"
        self.addCleanup(shutil.rmtree, str(out.parent), True)
        self.assertEqual(fsprobe.main(["--path", "ro=" + str(tmp), "--repeats", "3", "--no-quota", "--out", str(out)]), 0)
        mine = [p for p in json.loads(out.read_text())["paths"] if p["label"] == "ro"][0]
        self.assertTrue(mine["read_only"])
        self.assertNotIn("create", mine["ops"])

    def test_survey_stops_at_its_limits_and_says_so(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for i in range(20):                  # a flat folder is counted whole; stopping shows when folders are left over
            (tmp / ("d%d" % i)).mkdir()
            for j in range(5):
                (tmp / ("d%d" % i) / ("f%d" % j)).write_text("x")
        s = fsprobe.survey(str(tmp), max_entries=30, max_seconds=5.0)
        self.assertTrue(s["truncated"])
        self.assertLess(s["entries"], 120)
        whole = fsprobe.survey(str(tmp))
        self.assertEqual((whole["entries"], whole["truncated"]), (120, False))


class TestRuns(unittest.TestCase):
    def test_spec_is_validated(self):
        ok = fsruns.validate({"where": "compute", "nodes": ["c01", "c02"], "paths": ["/scratch/me"], "repeats": 5})
        self.assertEqual((ok["where"], ok["nodes"], ok["repeats"], ok["latency"]), ("compute", ["c01", "c02"], 5, True))
        for bad in ({"where": "moon"}, {"nodes": ["a b"]}, {"nodes": ["x; rm -rf /"]}, {"partition": "$(id)"}, {"mem": "1G; id"},
                    {"time_limit": "soon"}, "text", {"nodes": ["n%d" % i for i in range(500)]}):
            with self.assertRaises(fsruns.Refused, msg=str(bad)[:40]):
                fsruns.validate(bad)

    def test_hostlists_expand(self):
        self.assertEqual(fsruns.expand_hostlist("n[01-03,05],x"), ["n01", "n02", "n03", "n05", "x"])

    def test_the_batch_script_carries_the_probe_and_quotes_its_arguments(self):
        spec = fsruns.validate({"where": "compute", "nodes": ["c01"], "paths": ["/a b/c"], "partition": "gpu", "account": "acct"})
        script = fsruns.batch_script(spec, "node-c01", "c01", "/runs/r1", "print('probe')\n")
        self.assertIn("#SBATCH --nodelist=c01", script)
        self.assertIn("#SBATCH --partition=gpu", script)
        self.assertIn("print('probe')", script)
        self.assertIn("'/a b/c'", script)       # a path with a space reaches the probe as one argument
        subprocess.run(["bash", "-n", "/dev/stdin"], input=script.encode(), check=True)
        with self.assertRaises(fsruns.Refused):
            fsruns.batch_script(spec, "k", "c01", "/r", "x\n" + fsruns.MARKER + "\nrm -rf ~\n")


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.patch = unittest.mock.patch.dict(os.environ, {"HPCTUNNELS_DATA_DIR": str(cls.tmp), "FSDIAG_PATHS": ""})
        cls.patch.start()
        cls.token = "t" * 30
        cls.server = fsdiag_server.Server(("127.0.0.1", 0), cls.token, fsdiag_server.Diagnostics())
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.patch.stop()
        shutil.rmtree(cls.tmp, True)

    def call(self, verb, path, body=None, token=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = dict(headers or {})
        if token:
            h["X-Fsdiag-Token"] = self.token
        data = None
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
        conn.request(verb, path, body=data, headers=h)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        try:
            return res.status, json.loads(raw)
        except ValueError:
            return res.status, raw.decode("utf-8", "replace")

    def test_health_needs_no_token_and_everything_else_does(self):
        self.assertEqual(self.call("GET", "/health", token=False)[0], 200)
        for path in ("/api/info", "/api/runs", "/api/requests", "/api/nodes", "/api/report?id=x"):
            self.assertEqual(self.call("GET", path, token=False)[0], 401, path)
        # the page itself carries no data (a browser can't send a header with its first request): the token goes in with its API calls
        self.assertEqual(self.call("GET", "/", token=False)[0], 200)
        self.assertEqual(self.call("GET", "/api/info", headers={"X-Fsdiag-Token": "wrong"}, token=False)[0], 401)

    def test_a_request_from_another_origin_is_refused(self):
        status, _ = self.call("POST", "/api/runs", {"where": "login"}, headers={"Origin": "http://evil.example"})
        self.assertIn(status, (403, 400))
        status, _ = self.call("POST", "/api/runs", None)
        self.assertGreaterEqual(status, 400)

    def test_a_login_run_from_start_to_report(self):
        probe_dir = self.tmp / "place"
        probe_dir.mkdir()
        status, run = self.call("POST", "/api/runs", {"where": "login", "paths": ["mine=" + str(probe_dir)], "repeats": 3, "quota": False,
                                                       "survey_roots": [str(probe_dir)], "survey_seconds": 5})
        self.assertEqual(status, 201, run)
        for _ in range(120):
            status, detail = self.call("GET", "/api/run?id=" + run["id"])
            if detail["run"]["targets"][0]["state"] in fsruns.FINAL:
                break
            time.sleep(0.5)
        self.assertEqual(detail["run"]["targets"][0]["state"], "done", detail)
        self.assertEqual(detail["results"][0]["target"], "login")
        self.assertTrue(detail["findings"])
        status, report = self.call("GET", "/api/report?id=" + run["id"])
        self.assertEqual(status, 200)
        self.assertIn("File system diagnostics", report)
        self.assertEqual(self.call("GET", "/api/runs")[1]["runs"][0]["id"], run["id"])
        self.assertEqual(self.call("POST", "/api/run/delete", {"id": run["id"]})[0], 200)
        self.assertEqual(self.call("GET", "/api/run?id=" + run["id"])[0], 404)

    def test_a_bad_spec_and_a_bad_id_are_refused(self):
        self.assertEqual(self.call("POST", "/api/runs", {"where": "moon"})[0], 400)
        self.assertEqual(self.call("GET", "/api/run?id=../../etc")[0], 404)

    def test_the_page_is_served_and_unknown_files_are_not(self):
        status, page = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("hpc-tabs", page)
        self.assertEqual(self.call("GET", "/ui/../fsprobe.py")[0], 404)
        self.assertEqual(self.call("GET", "/ui/nothing.js")[0], 404)

    def test_requests_analysis_with_no_log_says_so(self):
        status, out = self.call("GET", "/api/requests")
        self.assertEqual(status, 200)
        self.assertEqual(out["analysis"]["requests"]["count"], 0)
        self.assertTrue(out["findings"])


class TestCli(unittest.TestCase):
    def run_cli(self, *args):
        env = dict(os.environ, HPCTUNNELS_DATA_DIR=tempfile.mkdtemp())
        return subprocess.run([sys.executable, str(TUNNEL / "fsdiag_cli.py")] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                              universal_newlines=True)

    def test_probe_prints_a_report(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        r = self.run_cli("probe", "--path", "t=" + tmp, "--repeats", "3", "--no-quota")
        self.assertIn(r.returncode, (0, 1), r.stderr)
        self.assertIn("## Findings", r.stdout)

    def test_requests_and_bad_commands(self):
        self.assertEqual(self.run_cli("requests").returncode, 0)
        self.assertEqual(self.run_cli("nonsense").returncode, 2)
        self.assertEqual(self.run_cli().returncode, 2)


class TestActions(unittest.TestCase):
    """actions.json: what an agent can run through the REST server (list_tunnel_actions, run_tunnel_action)."""

    class Identity:
        name = "agent"

        def __init__(self, scopes=("read", "submit")):
            self.scopes = scopes

        def has_scope(self, scope):
            return scope in self.scopes

    class Whitelist:
        restricted = False
        roots = None
        deny = ()

        def resolve(self, value):
            return value, os.path.realpath(value)

    def actions(self, enabled=True, **config):
        import rest_actions
        cfg = {"fsdiag": dict({"enabled": enabled}, **config)}
        return rest_actions.TunnelActions(config=cfg, dirs=[str(TUNNEL.parent)], data_dir=self.data)

    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data, True)
        patch = unittest.mock.patch.dict(os.environ, {"HPCTUNNELS_DATA_DIR": self.data, "HPCTUNNELS_SETTINGS_DIR": self.data})
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_manifest_is_valid_and_listed(self):
        import rest_actions
        manifest = rest_actions.check_manifest(json.loads((TUNNEL / "actions.json").read_text()), "fsdiag")
        self.assertEqual(sorted(manifest["actions"]), ["probe", "probe_node", "probe_report", "requests", "survey"])
        self.assertEqual(manifest["actions"]["probe_node"]["mode"], "job")
        listing = self.actions().describe(self.Identity(), owner=False)
        self.assertEqual(sorted(a["name"] for a in listing["tunnels"][0]["actions"]), ["probe", "probe_node", "probe_report", "requests", "survey"])
        # off until the owner turns the tunnel on
        self.assertEqual(self.actions(enabled=False).describe(self.Identity(), owner=False)["tunnels"], [])

    def test_probe_returns_findings_and_a_report(self):
        status, out = self.actions().run(self.Identity(), "fsdiag", "probe", {"repeats": 3}, self.Whitelist(), True)
        self.assertEqual(status, 200)
        result = out["result"]
        self.assertTrue(result["findings"])
        self.assertIn("## Findings", result["report"])
        self.assertIsInstance(result["problems"], int)

    def test_survey_counts_a_folder(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        for i in range(12):
            Path(folder, "f%d" % i).write_text("x")
        status, out = self.actions().run(self.Identity(), "fsdiag", "survey", {"folder": folder, "seconds": 5}, self.Whitelist(), True)
        self.assertEqual(status, 200)
        self.assertIn("12 entries", out["result"]["report"])
        self.assertNotIn("stat_missing", out["result"]["report"])      # counting only: no latency table

    def test_requests_reads_the_audit_log(self):
        (Path(self.data) / "rest").mkdir()
        (Path(self.data) / "rest" / "audit.log").write_text("\n".join(
            json.dumps({"time": time.time() - i, "verb": "GET", "path": "/v1/jobs/1", "status": 200, "duration_ms": 3000.0, "cpu_ms": 4.0})
            for i in range(10)) + "\n")
        status, out = self.actions().run(self.Identity(), "fsdiag", "requests", {"hours": 1, "slow_ms": 1000}, self.Whitelist(), True)
        self.assertEqual(status, 200)
        self.assertIn("10 of 10", " ".join(f["title"] for f in out["result"]["findings"]))

    def test_parameters_are_checked(self):
        import rest_actions
        acts = self.actions()
        for name, given in (("probe", {"repeats": 1000}), ("probe", {"repeats": "x"}), ("probe", {"nope": 1}), ("probe_node", {"node": "a b"}),
                            ("probe_node", {"node": "-x"}), ("probe_node", {"node": "n1; id"}), ("requests", {"hours": 0})):
            with self.assertRaises(rest_actions.RESTError, msg=str((name, given))):
                acts.run(self.Identity(), "fsdiag", name, given, self.Whitelist(), True)

    def test_scope_and_the_owners_switch_are_enforced(self):
        import rest_actions
        with self.assertRaises(rest_actions.RESTError) as ctx:
            self.actions().run(self.Identity(scopes=("submit",)), "fsdiag", "probe", {}, self.Whitelist(), True)
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(rest_actions.RESTError) as ctx:
            self.actions(actions={"probe": False}).run(self.Identity(), "fsdiag", "probe", {}, self.Whitelist(), True)
        self.assertEqual(ctx.exception.status, 403)

    def test_the_job_is_submitted_with_the_agents_comment_and_the_node(self):
        bin_dir = Path(self.data) / "bin"
        bin_dir.mkdir()
        sbatch = bin_dir / "sbatch"
        sbatch.write_text('#!/bin/bash\nprintf "%%s\\n" "$@" > "%s/sbatch.args"\necho 4242\n' % self.data)
        sbatch.chmod(0o755)
        with unittest.mock.patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "FSDIAG_PARTITION": "short"}):
            script = TUNNEL / "fsdiag_action.sh"
            done = subprocess.run(["bash", str(script), "submit", "--comment=hpclib-rest:agent:fsdiag/probe_node", "c07", "5"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "4242"), done.stderr)
        args = (Path(self.data) / "sbatch.args").read_text().splitlines()
        self.assertIn("--nodelist=c07", args)
        self.assertIn("--partition=short", args)
        self.assertIn("--comment=hpclib-rest:agent:fsdiag/probe_node", args)
        self.assertIn("--output=%s/sessions/fsdiag/probe-%%j.txt" % self.data, args)
        self.assertIn("fsdiag_cli.py' probe --repeats '5'", args[-1])
        # 'any' leaves the node to the scheduler
        with unittest.mock.patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}):
            subprocess.run(["bash", str(script), "submit", "--comment=c", "any", "5"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertFalse([a for a in (Path(self.data) / "sbatch.args").read_text().splitlines() if a.startswith("--nodelist")])


    def test_a_jobs_report_is_read_by_its_id_only(self):
        import rest_actions
        out_dir = Path(self.data) / "sessions" / "fsdiag"
        out_dir.mkdir(parents=True)
        (out_dir / "probe-4242.txt").write_text("# the report\n")
        status, out = self.actions().run(self.Identity(), "fsdiag", "probe_report", {"job_id": 4242}, self.Whitelist(), True)
        self.assertEqual((status, out["result"]), (200, "# the report"))
        for bad in ({"job_id": 4243}, {"job_id": "../x"}, {"job_id": -1}):
            with self.assertRaises(rest_actions.RESTError, msg=str(bad)):
                self.actions().run(self.Identity(), "fsdiag", "probe_report", bad, self.Whitelist(), True)


from test_agent_console import ConsoleTestCase  # noqa: E402


class TestBundled(ConsoleTestCase):
    """The console's Packages offers it under "Come with hpclib", and installing it adds the app."""

    def test_check_and_install(self):
        import agent_console
        apps = unittest.mock.patch.object(agent_console, "APPS", dict(agent_console.APPS))
        apps.start()
        self.addCleanup(apps.stop)
        status, out = self.call("GET", "/api/packages/bundled")
        self.assertEqual(status, 200, out)
        found = [p for p in out["packages"] if p["name"] == "fsdiag"]
        self.assertEqual((len(found), found[0]["installed"]), (1, None))
        status, out = self.call("POST", "/api/packages/bundled/fsdiag?dry_run=1")
        self.assertEqual((status, out["problems"]), (200, []), out)
        status, out = self.call("POST", "/api/packages/bundled/fsdiag")
        self.assertEqual(status, 201, out)
        self.assertIn("fsdiag", [a["id"] for a in self.call("GET", "/api/apps")[1]["apps"]])


if __name__ == "__main__":
    unittest.main()
