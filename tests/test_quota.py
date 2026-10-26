"""Quota checks: lib/quota.sh (hpclib_quota, its settings and environment variables) and the REST server's GET /quota,
with fake showquota and myproject commands."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_rest_jobs import JobServerTestCase  # noqa: E402
import rest_server  # noqa: E402
from rest_client import RESTClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
QUOTA = ROOT / "hpclib" / "lib" / "quota.sh"


def fake_bin(directory, counter=None):
    """showquota and myproject that print something recognizable (myproject counts its runs in COUNTER)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "showquota").write_text("#!/bin/sh\necho 'Disk quotas for user me'\necho '/scratch 1T 512G'\n")
    count = f'echo run >> "{counter}"\n' if counter else ""
    (directory / "myproject").write_text(f"#!/bin/sh\n{count}echo 'Project 1228000 balance 5000 SU'\nexit 0\n")
    for name in ("showquota", "myproject"):
        (directory / name).chmod(0o755)


class QuotaShell(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.bin = self.tmp / "bin"
        fake_bin(self.bin)
        self.data = self.tmp / "data"

    def run_quota(self, *args, env=None, path=None):
        full = {"PATH": f"{path or self.bin}:/usr/bin:/bin", "HOME": str(self.tmp), "HPCTUNNELS_DATA_DIR": str(self.data)}
        full.update(env or {})
        return subprocess.run(["bash", str(QUOTA), *args], capture_output=True, text=True, env=full, timeout=60)

    def json(self, *args, **kw):
        out = self.run_quota(*args, "--json", **kw)
        self.assertEqual(out.returncode, 0, out.stderr)
        line = [ln for ln in out.stdout.splitlines() if ln.startswith("HPCLIB_QUOTA ")][0]
        return json.loads(line.split(" ", 1)[1])

    def test_defaults_are_showquota_and_myproject(self):
        out = self.json()
        self.assertEqual((out["storage"]["command"], out["storage"]["source"], out["storage"]["exit_code"]),
                         ("showquota", "default", 0))
        self.assertEqual(out["storage"]["output"], ["Disk quotas for user me", "/scratch 1T 512G"])
        self.assertEqual((out["project"]["command"], out["project"]["output"]),
                         ("myproject", ["Project 1228000 balance 5000 SU"]))
        self.assertEqual(out["settings_file"], str(self.data / "settings" / "quota.sh"))
        text = self.run_quota("project").stdout
        self.assertIn("== project quota: myproject (default)", text)
        self.assertNotIn("storage", text)

    def test_nothing_found(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        out = self.json("project", path=empty)
        self.assertEqual((out["project"]["command"], out["project"]["source"]), (None, "default"))
        self.assertNotIn("storage", out)
        self.assertIn("none found", self.run_quota("project", path=empty).stdout)

    def test_settings_then_environment_win(self):
        self.assertEqual(self.run_quota("set", "storage", "echo 'from settings'; exit 3").returncode, 0)
        self.assertEqual(self.run_quota("set", "project", "none").returncode, 0)
        settings = self.data / "settings" / "quota.sh"
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        out = self.json()
        self.assertEqual((out["storage"]["source"], out["storage"]["exit_code"], out["storage"]["output"]),
                         ("settings", 3, ["from settings"]))
        self.assertEqual(out["storage"]["saved"], "echo 'from settings'; exit 3")
        self.assertEqual((out["project"]["source"], out["project"]["command"]), ("none", None))
        self.assertIn("turned off", self.run_quota("project").stdout)
        # the environment wins over the settings
        out = self.json(env={"HPCLIB_PROJECT_QUOTA_CHECK": "echo mine", "HPCLIB_STORAGE_QUOTA_CHECK": "df -P /"})
        self.assertEqual((out["project"]["source"], out["project"]["output"]), ("env", ["mine"]))
        self.assertEqual(out["storage"]["source"], "env")
        self.assertEqual(out["project"]["saved"], "none")
        # `default` forgets a setting; the other one stays
        self.run_quota("set", "project", "default")
        out = self.json()
        self.assertEqual(out["project"]["command"], "myproject")
        self.assertEqual(out["storage"]["source"], "settings")

    def test_set_refuses_what_isnt_one_line(self):
        for bad in (["set", "storage", "a\nb"], ["set", "storage", "x" * 1001], ["set", "disk", "x"]):
            self.assertNotEqual(self.run_quota(*bad).returncode, 0, bad)
        self.assertFalse((self.data / "settings" / "quota.sh").exists())
        self.assertEqual(self.run_quota("bogus").returncode, 2)

    def test_settings_are_quoted(self):
        tricky = "echo \"$HOME\" '$(touch pwned)'; echo `printf x`"
        self.run_quota("set", "storage", tricky)
        out = self.json("storage")
        self.assertEqual(out["storage"]["command"], tricky)
        self.assertEqual(out["storage"]["output"], [f"{self.tmp} $(touch pwned)", "x"])
        self.assertFalse(Path("pwned").exists())

    def test_cluster_quota_sends_the_functions_and_your_settings(self):
        # pssh stand-in: runs the command it would run on the cluster, here
        pssh = self.bin / "pssh"
        pssh.write_text('#!/bin/bash\nwhile [ "$#" -gt 1 ]; do shift; done\nexec bash -c "$1"\n')
        pssh.chmod(0o755)
        script = f'. "{QUOTA}"; cluster_quota project --json -p 22 me@cluster'
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                             env={"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.tmp),
                                  "HPCTUNNELS_DATA_DIR": str(self.data), "HPCLIB_PROJECT_QUOTA_CHECK": "echo 'from here'"})
        self.assertEqual(out.returncode, 0, out.stderr)
        line = json.loads(out.stdout.split("HPCLIB_QUOTA ", 1)[1])
        self.assertEqual((line["project"]["source"], line["project"]["output"]), ("env", ["from here"]))
        usage = subprocess.run(["bash", "-c", f'. "{QUOTA}"; cluster_quota storage'], capture_output=True, text=True)
        self.assertEqual(usage.returncode, 2)


class QuotaServer(JobServerTestCase):
    def setUp(self):
        super().setUp()
        self.counter = self.tmp / "myproject-runs"
        fake_bin(self.tmp / "qbin", self.counter)
        env = dict(os.environ, PATH=f"{self.tmp / 'qbin'}:/usr/bin:/bin", HPCTUNNELS_DATA_DIR=str(self.data))
        self.server.quota = rest_server.QuotaChecks(env=env)
        self.reader = RESTClient(self.url, token=self.reader_token)

    def test_any_reader_checks_and_answers_are_cached(self):
        out = self.reader.quota()
        self.assertEqual(out["project"]["output"], ["Project 1228000 balance 5000 SU"])
        self.assertEqual(out["storage"]["command"], "showquota")
        self.assertFalse(out["cached"])
        again = self.llm.quota()
        self.assertTrue(again["cached"])
        self.assertEqual(self.counter.read_text().count("run"), 1)
        self.assertFalse(self.llm.quota(refresh=True)["cached"])
        self.assertEqual(self.counter.read_text().count("run"), 2)
        only = self.llm.quota("storage")
        self.assertNotIn("project", only)
        self.expect_error(422, self.llm.quota, "disk")

    def test_settings_written_on_the_cluster_are_used(self):
        settings = self.data / "settings"
        settings.mkdir(parents=True, exist_ok=True)
        (settings / "quota.sh").write_text("HPCLIB_PROJECT_QUOTA_CHECK='echo custom'\n")
        out = self.owner.quota("project")
        self.assertEqual((out["project"]["source"], out["project"]["output"]), ("settings", ["custom"]))

    def test_no_answer_is_an_error(self):
        self.server.quota = rest_server.QuotaChecks(script=str(self.tmp / "missing.sh"))
        self.expect_error(500, self.llm.quota)


if __name__ == "__main__":
    unittest.main()
