"""Tunnel actions (rest_actions.py), their safe file moves (action_paths.py), and data-transfer's actions.json."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_rest_jobs import JobServerTestCase, SERVERS  # noqa: E402
import action_paths  # noqa: E402
import rest_actions  # noqa: E402
import rest_server  # noqa: E402
from rest_client import RESTClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA_TRANSFER = ROOT / "hpclib" / "tunnels" / "data-transfer"

# a tunnel with actions: status says whether it is ready ($DEMO_READY), list echoes its arguments, submit sbatches
SCRIPT = """#!/bin/bash
case "$1" in
  status) echo "noise"; echo "DEMO_STATUS {\\"ready\\": ${DEMO_READY:-true}, \\"who\\": \\"me\\"}" ;;
  list) shift; python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@" ;;
  submit) shift; opts=(); while [ "${1#-}" != "$1" ]; do opts+=("$1"); shift; done
          printf '%s\\n' "$@" > "$DEMO_ARGS"; sbatch --parsable --job-name=demo "${opts[@]}" <<< "echo hi" ;;
esac
"""
MANIFEST = {
    "title": "Demo", "description": "a test tunnel",
    "available": {"run": ["act.sh", "status"], "line": "DEMO_STATUS", "require": ["ready"], "show": ["who"],
                  "message": "sign in first", "cache": 0},
    "actions": {
        "list": {"description": "list", "params": {"remote": {"type": "remote-path", "default": ""},
                                                   "count": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1},
                                                   "fast": {"type": "boolean", "default": False},
                                                   "label": {"type": "string", "pattern": "[a-z]{1,8}", "default": "x"}},
                 "run": ["act.sh", "list", "{remote}", "{count}", "{fast}", "{label}"]},
        "get": {"description": "copy in", "mode": "job", "scope": "submit", "writes": "cluster",
                "params": {"remote": {"type": "remote-path"}, "local": {"type": "cluster-path", "access": "write"}},
                "run": ["act.sh", "submit", "{job_options}", "get", "--scope", "{scope_file}", "{remote}", "{local}"],
                "output": "{data_dir}/demo-{job_id}.log"},
        "put": {"description": "copy out", "mode": "job", "scope": "submit", "writes": "remote", "default": False,
                "params": {"local": {"type": "cluster-path", "access": "read"}, "remote": {"type": "remote-path"}},
                "run": ["act.sh", "submit", "{job_options}", "put", "{local}", "{remote}"]},
    },
}


class TestManifest(unittest.TestCase):
    def test_data_transfer_actions(self):
        m = rest_actions.check_manifest(json.loads((DATA_TRANSFER / "actions.json").read_text()), "data-transfer")
        self.assertEqual({n: (a["mode"], a["default"], a["writes"]) for n, a in m["actions"].items()},
                         {"list": ("sync", True, None), "get": ("job", True, "cluster"), "put": ("job", False, "remote")})
        self.assertTrue((DATA_TRANSFER / m["actions"]["get"]["run"][0]).is_file())
        self.assertEqual(m["available"]["any"], ["ticket"])

    def test_refused(self):
        def bad(**change):
            spec = json.loads(json.dumps(MANIFEST))
            for key, value in change.items():
                target, name = spec, key.split("__")
                for part in name[:-1]:
                    target = target[part]
                target[name[-1]] = value
            with self.assertRaises(ValueError, msg=change):
                rest_actions.check_manifest(spec, "demo")
        bad(actions__list__run=["act.sh", "--x={remote}"])                 # a placeholder is a whole argument
        bad(actions__list__run=["act.sh", "{nope}"])                        # not a parameter
        bad(actions__list__run=["../act.sh"])                               # not in the tunnel's directory
        bad(actions__get__run=["act.sh", "submit", "{remote}", "{local}"])  # a job without {job_options}
        bad(actions__list__run=["act.sh", "{job_options}"])                 # job options in a sync action
        bad(actions__list__params={"label": {"type": "string"}})            # a string without a pattern
        bad(actions__list__scope="*")
        bad(actions__list__mode="background")
        bad(actions__BadName={"description": "x", "run": ["act.sh"]})

    def test_config_section(self):
        self.assertEqual(rest_actions.check_config(None), {})
        for bad in ([], {"../x": {}}, {"demo": {"enabled": "yes"}}, {"demo": {"actions": {"put": 1}}},
                    {"demo": {"extra": True}}):
            with self.assertRaises(ValueError):
                rest_actions.check_config(bad)
        new, built = rest_server.updated_config({}, {"tunnel_actions": {"demo": {"enabled": True, "actions": {"put": True}}}})
        self.assertEqual(built["tunnel_actions"]["demo"]["actions"], {"put": True})
        self.assertIn("tunnel_actions", rest_server.EDITABLE_CONFIG)


class TestActionsServer(JobServerTestCase):
    def setUp(self):
        super().setUp()
        tunnels = self.tmp / "tunnels"
        (tunnels / "demo").mkdir(parents=True)
        (tunnels / "demo" / "actions.json").write_text(json.dumps(MANIFEST))
        (tunnels / "demo" / "act.sh").write_text(SCRIPT)
        (tunnels / "broken").mkdir()
        (tunnels / "broken" / "actions.json").write_text("{")
        self.args_file = self.tmp / "submitted-args"
        env = mock.patch.dict(os.environ, {"DEMO_ARGS": str(self.args_file), "DEMO_READY": "true"})
        env.start()
        self.addCleanup(env.stop)
        self.server.actions = rest_actions.TunnelActions({}, dirs=[str(tunnels)], data_dir=str(self.data))
        self.reader = RESTClient(self.url, token=self.reader_token)

    def turn_on(self, **actions):
        self.server.actions.config = {"demo": {"enabled": True, "actions": actions}}

    def test_off_until_the_owner_turns_it_on(self):
        self.assertEqual(self.llm.actions(), {"tunnels": []})
        owner = self.owner.actions()["tunnels"]
        self.assertEqual([t["tunnel"] for t in owner], ["broken", "demo"])
        self.assertIn("error", owner[0])
        demo = owner[1]
        self.assertFalse(demo["enabled"])
        self.assertEqual({a["name"]: a["enabled"] for a in demo["actions"]}, {"list": False, "get": False, "put": False})
        self.expect_error(403, self.llm.run_action, "demo", "list")
        self.turn_on()
        listed = self.llm.actions()["tunnels"][0]
        self.assertEqual(sorted(a["name"] for a in listed["actions"]), ["get", "list"])     # put: off by default
        self.assertEqual(listed["availability"], {"available": True, "detail": {"who": "me"}})
        self.expect_error(403, self.llm.run_action, "demo", "put", {"local": ".", "remote": "x"})
        # a reader may list, not run a job
        self.assertEqual([a["name"] for a in self.reader.actions()["tunnels"][0]["actions"]], ["list"])
        self.expect_error(403, self.reader.run_action, "demo", "get", {"remote": "a", "local": "new"})

    def test_sync_action_with_checked_parameters(self):
        self.turn_on()
        out = self.llm.run_action("demo", "list", {"remote": "proj/raw data", "count": 3, "fast": True})
        self.assertEqual(out["result"], ["proj/raw data", "3", "true", "x"])
        for bad in ({"remote": "../etc"}, {"remote": "a/../../b"}, {"remote": "/abs"}, {"remote": "-rf"},
                    {"remote": "//other-host/share"}, {"remote": "a\nb"}, {"count": 9}, {"count": "3"},
                    {"fast": "yes"}, {"label": "UPPER"}, {"label": "-x"}, {"other": 1}):
            with self.subTest(params=bad):
                self.expect_error(422, self.llm.run_action, "demo", "list", bad)
        entries = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
        ran = [e for e in entries if e["path"] == "/actions/run" and e["status"] == 200]
        self.assertEqual(ran[-1]["detail"]["action"], "list")
        self.assertEqual(ran[-1]["detail"]["params"]["remote"], "proj/raw data")

    def test_job_action_recorded_with_the_jobs(self):
        self.turn_on()
        (self.llm_root / "inputs").mkdir()
        self.expect_error(403, self.llm.run_action, "demo", "get", {"remote": "a", "local": str(self.outside / "new")})
        self.expect_error(409, self.llm.run_action, "demo", "get", {"remote": "a", "local": "inputs"})   # exists
        out = self.llm.run_action("demo", "get", {"remote": "proj/raw", "local": "copied"})
        job = out["job"]
        self.assertEqual((job["template"], job["state"], job["token"]), ("demo/get", "PENDING", "llm"))
        self.assertEqual(job["output"], f"{self.data}/demo-{job['job_id']}.log")
        submitted = json.loads((self.slurm / f"{job['job_id']}.json").read_text())
        self.assertIn("--comment=hpclib-rest:llm:demo/get", submitted["args"])
        self.assertFalse(submitted["token_in_env"])
        args = self.args_file.read_text().splitlines()
        self.assertEqual(args[0], "get")
        self.assertEqual(args[-2:], ["proj/raw", str(self.llm_root / "copied")])     # the resolved path
        scope = json.loads(Path(args[2]).read_text())
        self.assertEqual(scope["roots"], [str(self.llm_root)])
        self.assertEqual(self.llm.job_status(job["job_id"])["template"], "demo/get")
        self.assertIn(job["job_id"], [j["job_id"] for j in self.llm.jobs()["jobs"]])
        # the concurrent-job cap covers action jobs (it is 2 here)
        self.llm.run_action("demo", "get", {"remote": "p", "local": "second"})
        self.expect_error(429, self.llm.run_action, "demo", "get", {"remote": "p", "local": "third"})

    def test_put_turned_on_and_availability(self):
        self.turn_on(put=True)
        (self.llm_root / "results").mkdir()
        out = self.llm.run_action("demo", "put", {"local": "results", "remote": "group/results"})
        self.assertEqual(out["job"]["template"], "demo/put")
        self.expect_error(404, self.llm.run_action, "demo", "put", {"local": "missing", "remote": "x"})
        os.environ["DEMO_READY"] = "false"
        payload = self.expect_error(409, self.llm.run_action, "demo", "list")
        self.assertIn("sign in first", payload["error"])
        self.assertFalse(self.llm.actions()["tunnels"][0]["availability"]["available"])

    def test_sandbox_hides_the_smb_login(self):
        sandbox = mock.Mock()
        config = {"templates_dir": "/t", "jobs_db": "/j", "tokens_file": "/k", "path": "/c", "proposals_dir": "/p"}
        rest_server.protect_sandbox(sandbox, config)
        hidden = sandbox.protect.call_args.kwargs["hidden"]
        self.assertIn(os.path.expanduser("~/.config/hpclib/smb"), hidden)


class TestActionPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.agent = self.tmp / "agent"
        self.secret = self.tmp / "secret"
        self.staging = self.tmp / "staging"
        for d in (self.agent, self.secret, self.staging):
            d.mkdir()
        (self.secret / "id_ed25519").write_text("KEY")
        self.scope = {"roots": [str(self.agent)], "deny": [str(self.agent / "protected")]}

    def test_place_into_a_new_directory_only(self):
        src = self.staging / "data"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_text("a")
        (src / "sub" / "b.txt").write_text("bb")
        os.symlink(self.secret, src / "link")
        out = action_paths.place(str(src), str(self.agent / "new"), self.scope)
        self.assertEqual((out["files"], out["bytes"], out["skipped"]), (2, 3, ["new/link"]))
        self.assertEqual((self.agent / "new" / "sub" / "b.txt").read_text(), "bb")
        with self.assertRaises(action_paths.Refused):
            action_paths.place(str(src), str(self.agent / "new"), self.scope)            # exists
        os.symlink(self.secret, self.agent / "swapped")
        with self.assertRaises(action_paths.Refused):
            action_paths.place(str(src), str(self.agent / "swapped" / "x"), self.scope)  # through a link
        with self.assertRaises(action_paths.Refused):
            action_paths.place(str(src), str(self.secret / "x"), self.scope)             # outside
        (self.agent / "protected").mkdir()
        with self.assertRaises(action_paths.Refused):
            action_paths.place(str(src), str(self.agent / "protected" / "x"), self.scope)
        self.assertEqual(sorted(p.name for p in self.secret.iterdir()), ["id_ed25519"])

    def test_stage_in_never_follows_links(self):
        (self.agent / "results").mkdir()
        (self.agent / "results" / "r.txt").write_text("r")
        os.symlink(self.secret / "id_ed25519", self.agent / "results" / "key")
        out = action_paths.stage_in(str(self.agent / "results"), str(self.staging), self.scope)
        self.assertEqual((out["files"], out["skipped"]), (1, ["results/key"]))
        self.assertEqual(sorted(p.name for p in (self.staging / "results").iterdir()), ["r.txt"])
        os.symlink(self.secret, self.agent / "looks-fine")
        for bad in (self.agent / "looks-fine", self.agent / "looks-fine" / "id_ed25519", self.secret / "id_ed25519"):
            with self.subTest(src=bad), self.assertRaises(action_paths.Refused):
                action_paths.stage_in(str(bad), str(self.staging), self.scope)

    def test_command_line(self):
        scope = self.tmp / "scope.json"
        scope.write_text(json.dumps(self.scope))
        (self.staging / "f.txt").write_text("f")
        helper = str(SERVERS / "action_paths.py")
        ok = subprocess.run([sys.executable, helper, "place", str(self.staging), str(self.agent / "in"), "--scope",
                             str(scope)], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["files"], 1)
        refused = subprocess.run([sys.executable, helper, "place", str(self.staging), str(self.secret / "in"),
                                  "--scope", str(scope)], capture_output=True, text=True)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("outside", refused.stderr)


if __name__ == "__main__":
    unittest.main()
