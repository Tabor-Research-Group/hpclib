"""Local reproduction of delayed Slurm cancellation; no SSH, real jobs, sockets or signals."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HPCLIB = Path(__file__).resolve().parents[1] / "hpclib"
SCRIPT = r'''
source "$HPCLIB_DIR/hpclib.sh" >/dev/null
hostname() { echo fixture-node; }
pkill() { return 1; }
kill() { return 1; }
sleep() { :; }
_hpclib_port_free() { echo checked >> "$HOME/port-checks"; return 0; }
squeue() {
  local n
  n=$(cat "$HOME/count" 2>/dev/null || echo 0)
  n=$((n+1)); echo "$n" > "$HOME/count"
  case "$TEST_CASE" in
    query-error) echo 'controller unavailable' >&2; return 1 ;;
    invalid) echo 'slurm_load_jobs error: Invalid job id specified' >&2; return 1 ;;
    ended) return 0 ;;
    delayed)
      if [ -f "$HOME/cancelled" ] && [ "$n" -ge 4 ]; then touch "$HOME/ended"; return 0; fi ;;
    after-cancel-error)
      if [ -f "$HOME/cancelled" ]; then echo 'controller unavailable' >&2; return 1; fi ;;
  esac
  echo 3207636
}
scancel() {
  echo "$1" > "$HOME/cancelled"
  if [ "$TEST_CASE" = cancel-error ]; then echo 'permission denied' >&2; return 1; fi
}
_hpclib_record_port 21893 99999999 3207636
if [ "${TEST_STOP_CLI:-0}" = 1 ]; then
  python3() { return 0; }   # the remote port probe; no real socket or alternate Python is used
  export -f hostname pkill kill sleep squeue scancel python3
  export TEST_CASE
  pssh() {
    case " $* " in *" -O cancel "*) touch "$HOME/forward-cancelled"; return 0 ;; esac
    local command arg
    for arg in "$@"; do command="$arg"; done
    bash -c "$command"
  }
  stop_tunnel -P 21893 fixture.host
else
  _hpclib_clear_port 21893
fi
'''


class TestTunnelRestart(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def clear(self, case, cli=False):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.root), "HPCLIB_DIR": str(HPCLIB),
               "HPCSESSIONS_DIR": str(self.root / "sessions"), "TEST_CASE": case,
               "HPCLIB_TUNNEL_STOP_WAIT": "2", "TEST_STOP_CLI": str(int(cli))}
        return subprocess.run(["bash", "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=10)

    def record_exists(self):
        return (self.root / "sessions/ports/fixture-node-21893").exists()

    def test_delayed_cancellation_finishes_before_port_is_declared_free(self):
        result = self.clear("delayed")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "ended").exists(), "port declared free while the old compute job was still active")
        self.assertEqual((self.root / "cancelled").read_text().strip(), "3207636")
        self.assertFalse(self.record_exists())

    def test_timeout_keeps_the_record_and_does_not_report_a_free_port(self):
        result = self.clear("never-ends")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.record_exists())
        self.assertFalse((self.root / "port-checks").exists())
        self.assertIn("3207636", result.stderr)

    def test_stop_command_transmits_waiter_and_only_closes_forward_after_shutdown(self):
        result = self.clear("delayed", cli=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "ended").exists())
        self.assertTrue((self.root / "forward-cancelled").exists())

    def test_failed_remote_shutdown_is_not_hidden_by_local_forward_cleanup(self):
        result = self.clear("never-ends", cli=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.record_exists())
        self.assertFalse((self.root / "forward-cancelled").exists())

    def test_queue_query_failures_do_not_discard_the_record(self):
        for case in ("query-error", "after-cancel-error"):
            with self.subTest(case=case):
                (self.root / "cancelled").unlink(missing_ok=True)
                result = self.clear(case)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(self.record_exists())
                self.assertFalse((self.root / "port-checks").exists())

    def test_cancel_failure_keeps_the_record(self):
        result = self.clear("cancel-error")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.record_exists())

    def test_already_ended_or_invalid_job_is_safe_to_forget(self):
        for case in ("ended", "invalid"):
            with self.subTest(case=case):
                result = self.clear(case)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(self.record_exists())
                self.assertFalse((self.root / "cancelled").exists())

    def test_launcher_retains_the_job_record_when_cancellation_is_still_pending(self):
        tunnel = self.root / "tunnels/demo"
        tunnel.mkdir(parents=True)
        (self.root / ".bashrc").write_text("")
        (tunnel / "sbatch_script.sh").write_text("true\n")
        (tunnel / "tunnel_config.sh").write_text(r'''
START_GIT_SERVER=false
START_SLURM_SERVER=false
JOB_INITIALIZATION_PAUSE=0
python3() { return 0; }
_hpclib_clear_port() { return 0; }
hostname() { echo fixture-node; }
sbatch() { echo 3207639; }
scancel() { echo "$1" >> "$HOME/cancelled"; }
get_job_node() { echo fc041; }
connect_to_job() { return 0; }
pkill() { return 1; }
''')
        env = {"PATH": os.environ["PATH"], "HOME": str(self.root), "HPCLIB_DIR": str(HPCLIB),
               "HPCSESSIONS_DIR": str(self.root / "sessions"), "HPCLIB_TUNNEL_PATH": str(self.root / "tunnels")}
        result = subprocess.run(["bash", str(HPCLIB / "tunnels/start_tunnel.sh"), "demo", "-P", "21893"],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.root / "sessions/ports/fixture-node-21893"
        self.assertTrue(record.exists(), "launcher forgot an allocation that was only asked to stop")
        self.assertEqual(record.read_text().split()[1], "3207639")


if __name__ == "__main__":
    unittest.main()
