"""clear_tunnel_ports / _hpclib_clear_ports with fake Slurm, hostname and ssh (stdlib only; no real cluster)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HPCLIB = Path(__file__).resolve().parents[1] / "hpclib"

# Fakes put on PATH: hostname reports FAKE_HOSTNAME; squeue reports a job until scancel ended it; ssh runs the hop
# locally as the node it names, or fails (FAKE_UNREACHABLE), as a BatchMode ssh without keys there would.
FAKES = {
    "hostname": '#!/bin/bash\necho "${FAKE_HOSTNAME:-login1}"\n',
    "squeue": '#!/bin/bash\nwhile [ "$#" -gt 0 ]; do [ "$1" = -j ] && job="$2"; shift; done\n'
              '[ -f "$FAKE_DIR/ended-$job" ] || echo "$job"\n',
    "scancel": '#!/bin/bash\necho "$1" >> "$FAKE_DIR/scancel.log"\ntouch "$FAKE_DIR/ended-$1"\n',
    "ssh": '#!/bin/bash\nargs=("$@")\nnode=""\nwhile [ "$#" -gt 1 ]; do\n'
           '  case "$1" in -o) shift 2 ;; -*) shift ;; *) node="$1"; shift; break ;; esac\ndone\n'
           'echo "$node" >> "$FAKE_DIR/ssh.log"\n'
           'case " $FAKE_UNREACHABLE " in *" $node "*) exit 255 ;; esac\n'
           'FAKE_HOSTNAME="$node" exec bash -c "$1"\n',
}


class TestClearPorts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, text in FAKES.items():
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.ports = self.tmp / "data" / "sessions" / "ports"
        self.ports.mkdir(parents=True)

    def record(self, node, port, pid="99999999", job=""):
        (self.ports / f"{node}-{port}").write_text(f"{pid} {job}\n")

    def run_clear(self, *args, **env):
        script = 'source "$HPCLIB_DIR/hpclib.sh" > /dev/null; _hpclib_clear_ports "$@"'
        return subprocess.run(["bash", "-c", script, "test", *args], capture_output=True, text=True, timeout=120,
                              env=dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", HOME=str(self.tmp),
                                       HPCLIB_DIR=str(HPCLIB), HPCTUNNELS_DATA_DIR=str(self.tmp / "data"),
                                       FAKE_DIR=str(self.tmp), HPCLIB_TUNNEL_STOP_WAIT="3", **env))

    def test_other_nodes_cleared_by_hop_or_reported(self):
        self.record("login1", 20735, job="")          # this node's own record (as a start would find it)
        self.record("login2", 20735, job="111")       # stale: its job still runs
        self.record("login3", 20735, job="222")       # stale, on a node the hop can't reach
        self.record("login2", 30000, job="333")       # another port, not being cleared: its job is kept
        self.record("login3", 30001, job="444")       # (a job named by a port not being cleared is never cancelled)
        out = self.run_clear("20735", FAKE_UNREACHABLE="login3")
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertIn("HPCLIB_PORT_HERE login1", lines)
        self.assertIn("HPCLIB_PORT_CLEARED login1 20735", lines)
        self.assertIn("HPCLIB_PORT_CLEARED login2 20735", lines)
        self.assertIn("HPCLIB_PORT_PENDING login3 20735", lines)
        cancelled = (self.tmp / "scancel.log").read_text().split()
        self.assertEqual(sorted(cancelled), ["111", "222"])   # 333 and 444 belong to ports not being cleared
        left = sorted(p.name for p in self.ports.iterdir())
        self.assertEqual(left, ["login2-30000", "login3-20735", "login3-30001"])
        self.assertNotIn("clear it there if it is stale", out.stderr)

    def test_no_hop_clears_only_here(self):
        self.record("login2", 20735, job="111")
        out = self.run_clear("--no-hop", "20735", FAKE_HOSTNAME="login2")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("HPCLIB_PORT_CLEARED login2 20735", out.stdout.splitlines())
        self.assertFalse((self.ports / "login2-20735").exists())
        self.assertFalse((self.tmp / "ssh.log").exists())
        self.assertEqual((self.tmp / "scancel.log").read_text().split(), ["111"])    # by its own clear, here

    def test_bad_arguments(self):
        self.assertEqual(self.run_clear("20735;rm").returncode, 2)
        self.record("bad;node", 20735, job="555")
        out = self.run_clear("20735")
        self.assertNotIn("HPCLIB_PORT_PENDING", out.stdout)
        self.assertIn("not a node name", out.stderr)
        self.assertFalse((self.tmp / "ssh.log").exists())
        self.assertFalse((self.tmp / "scancel.log").exists())

    def test_cli_sends_the_script(self):
        script = ('source "$HPCLIB_DIR/hpclib.sh" > /dev/null; '
                  'pssh() { printf "%s\\n" "$@" > "$FAKE_DIR/pssh.args"; bash -c "${@: -1}"; }; '
                  'clear_tunnel_ports -P 20735 -P 20736 --no-hop me@cluster.example')
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", HOME=str(self.tmp),
                                      HPCLIB_DIR=str(HPCLIB), HPCTUNNELS_DATA_DIR=str(self.tmp / "data"),
                                      FAKE_DIR=str(self.tmp)))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines()[:3],
                         ["HPCLIB_PORT_HERE login1", "HPCLIB_PORT_CLEARED login1 20735", "HPCLIB_PORT_CLEARED login1 20736"])
        self.assertEqual((self.tmp / "pssh.args").read_text().splitlines()[0], "me@cluster.example")
        self.assertEqual(subprocess.run(["bash", "-c", 'source "$HPCLIB_DIR/hpclib.sh" >/dev/null; clear_tunnel_ports host'],
                                        env=dict(os.environ, HPCLIB_DIR=str(HPCLIB)), capture_output=True).returncode, 2)


if __name__ == "__main__":
    unittest.main()
