"""Portable regression tests for setup_tunnel.sh instance discovery and cancellation guards."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestSharedInstanceRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        fake = root / "bin"
        fake.mkdir()
        home = root / "home"
        home.mkdir()
        (home / ".bashrc").touch()
        tunnel = root / "tunnels" / "shared-demo"
        tunnel.mkdir(parents=True)
        (tunnel / "sbatch_script.sh").write_text("#!/bin/bash\ntrue\n")
        (tunnel / "tunnel_config.sh").write_text("SHARED_INSTANCE=true\nKEEP_INSTANCE=true\nPROCESS_PORT=3104\n")
        self.registry = root / "data" / "instances" / "shared-demo"
        self.registry.mkdir(parents=True)
        self.cancel_log = root / "cancelled"
        (fake / "squeue").write_text('''#!/bin/bash
case "${FAKE_QUERY_MODE:-running}" in
  error) echo 'slurm_load_jobs error: Socket timed out' >&2; exit 1 ;;
  invalid) echo 'slurm_load_jobs error: Invalid job id specified' >&2; exit 1 ;;
  empty) exit 0 ;;
esac
owner="${FAKE_OWNER:-$(id -un)}"
case "$*" in *%T*) printf 'RUNNING %s\\n' "$owner" ;; *) printf '%s\\n' "$owner" ;; esac
''')
        (fake / "scancel").write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$FAKE_CANCEL_LOG"\n')
        for path in fake.iterdir():
            path.chmod(0o755)
        self.env = dict(os.environ, HOME=str(home), HPCLIB_DIR=str(ROOT / "hpclib"),
                        HPCLIB_TUNNEL_PATH=str(root / "tunnels") + ":" + str(ROOT / "hpclib" / "tunnels"),
                        HPCTUNNELS_DATA_DIR=str(root / "data"), PATH=str(fake) + os.pathsep + os.environ["PATH"],
                        LC_ALL="C", FAKE_CANCEL_LOG=str(self.cancel_log))
        self.register()

    def register(self):
        for job, port in (("1001", 3104), ("1002", 3105)):
            (self.registry / job).write_text(f"node1 {port}\n")

    def run_setup(self, *args, **env):
        return subprocess.run(["bash", str(ROOT / "hpclib" / "tunnels" / "setup_tunnel.sh"), "shared-demo", *args],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=10)

    def test_scheduler_failure_preserves_records_and_is_an_error(self):
        result = self.run_setup("--instances", FAKE_QUERY_MODE="error")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("keeping its instance record", result.stderr)
        self.assertEqual({p.name for p in self.registry.iterdir()}, {"1001", "1002"})
        self.assertFalse(self.cancel_log.exists())

    def test_ownership_query_failure_does_not_claim_job_ended_or_cancel(self):
        result = self.run_setup("--stop-instance", "1001", FAKE_QUERY_MODE="error")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("HPCLIB_TUNNEL_INSTANCE_GONE", result.stdout)
        self.assertTrue((self.registry / "1001").exists())
        self.assertFalse(self.cancel_log.exists())

    def test_lists_live_registered_jobs(self):
        result = self.run_setup("--instances")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HPCLIB_TUNNEL_INSTANCE 1001 node1 3104 RUNNING", result.stdout)
        self.assertIn("HPCLIB_TUNNEL_INSTANCE 1002 node1 3105 RUNNING", result.stdout)

    def test_confirmed_ended_jobs_are_cleaned_up(self):
        for mode in ("empty", "invalid"):
            with self.subTest(mode=mode):
                self.register()
                result = self.run_setup("--instances", FAKE_QUERY_MODE=mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(list(self.registry.iterdir()), [])
                self.assertFalse(self.cancel_log.exists())

    def test_stop_requires_owner_and_registered_job(self):
        result = self.run_setup("--stop-instance", "9999")
        self.assertNotEqual(result.returncode, 0)
        result = self.run_setup("--stop-instance", "1001", FAKE_OWNER="someone-else")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.registry / "1001").exists())
        self.assertFalse(self.cancel_log.exists())
        result = self.run_setup("--stop-instance", "1001")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HPCLIB_TUNNEL_INSTANCE_STOPPED 1001", result.stdout)
        self.assertEqual(self.cancel_log.read_text(), "1001\n")
        self.assertFalse((self.registry / "1001").exists())
