"""smbshell.sh's action-get and action-put (what data-transfer's tunnel actions run in their jobs), with a fake
singularity whose rclone copies from and to a directory standing in for the SMB server."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SMBSHELL = ROOT / "hpclib" / "tunnels" / "data-transfer" / "smbshell.sh"
HELPER = ROOT / "hpclib" / "servers" / "action_paths.py"

# singularity exec [--bind X]... IMAGE rclone (copy|lsjson|...) ...: smb:PATH is $FAKE_SMB/PATH
FAKE_SINGULARITY = """#!/usr/bin/env python3
import os, shutil, sys
args = sys.argv[2:]
while args and args[0].startswith("--"):
    args = args[2:]
args = args[1:]                      # the image
if args[:2] == ["rclone", "help"]:
    sys.exit(0)
if args[0] != "rclone" or args[1] != "copy":
    sys.exit(f"fake singularity: {args}")
paths = [a for a in args[2:] if not a.startswith("-")]
src, dst = paths[-2:]
smb = os.environ["FAKE_SMB"]
real = lambda p: os.path.join(smb, p[4:].lstrip("/")) if p.startswith("smb:") else p
src, dst = real(src), real(dst)
os.makedirs(dst, exist_ok=True)
if os.path.isdir(src):
    shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
else:
    shutil.copy(src, dst)
"""


class TestSmbshellActions(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "singularity").write_text(FAKE_SINGULARITY)
        (self.bin / "singularity").chmod(0o755)
        self.smb = self.tmp / "smb"
        (self.smb / "group" / "raw").mkdir(parents=True)
        (self.smb / "group" / "raw" / "a.dat").write_text("A")
        self.agent = self.tmp / "agent"
        self.agent.mkdir()
        self.home = self.tmp / "home"
        (self.home / ".config" / "hpclib" / "smb").mkdir(parents=True)
        (self.home / ".config" / "hpclib" / "smb" / "credentials").write_text("pass=x\n")
        self.image = self.tmp / "image.sif"
        self.image.write_text("")
        self.data = self.tmp / "data"

    def scope(self):
        path = self.tmp / f"scope-{len(list(self.tmp.glob('scope-*')))}.json"
        path.write_text(json.dumps({"roots": [str(self.agent)], "deny": []}))
        return path

    def run_smbshell(self, *args):
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", HOME=str(self.home), FAKE_SMB=str(self.smb),
                   HPCTUNNELS_DATA_DIR=str(self.data), SMB_HOST="files.example.edu", SMB_ROOT="",
                   SMB_IMAGE=str(self.image), SMB_AUTH="password", SMB_NONINTERACTIVE="1", SMB_KRB5_CONF="/nonexistent")
        return subprocess.run(["bash", str(SMBSHELL), *args], capture_output=True, text=True, env=env, timeout=60)

    def test_get_goes_through_staging_into_a_new_directory(self):
        scope = self.scope()
        out = self.run_smbshell("action-get", "--scope", str(scope), "--helper", str(HELPER), "group/raw",
                                str(self.agent / "raw-copy"))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((self.agent / "raw-copy" / "a.dat").read_text(), "A")
        self.assertFalse(scope.exists())                                          # used once
        self.assertEqual(list((self.data / "sessions" / "data-transfer" / "actions").iterdir()), [])   # cleaned up
        # an existing destination, or one outside the agent's directories, is refused
        again = self.run_smbshell("action-get", "--scope", str(self.scope()), "--helper", str(HELPER), "group/raw",
                                  str(self.agent / "raw-copy"))
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("exists", again.stderr)
        outside = self.run_smbshell("action-get", "--scope", str(self.scope()), "--helper", str(HELPER), "group/raw",
                                    str(self.tmp / "elsewhere"))
        self.assertNotEqual(outside.returncode, 0)
        self.assertFalse((self.tmp / "elsewhere").exists())

    def test_put_copies_a_snapshot_without_links(self):
        (self.agent / "results").mkdir()
        (self.agent / "results" / "r.txt").write_text("R")
        os.symlink(self.home / ".config" / "hpclib" / "smb" / "credentials", self.agent / "results" / "creds")
        out = self.run_smbshell("action-put", "--scope", str(self.scope()), "--helper", str(HELPER),
                                str(self.agent / "results"), "group/out")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(sorted(p.name for p in (self.smb / "group" / "out").iterdir()), ["r.txt"])
        os.symlink(self.home / ".config" / "hpclib" / "smb", self.agent / "innocent")
        refused = self.run_smbshell("action-put", "--scope", str(self.scope()), "--helper", str(HELPER),
                                    str(self.agent / "innocent"), "group/stolen")
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.smb / "group" / "stolen").exists())

    def test_status_brief(self):
        out = self.run_smbshell("status", "--json", "--brief")
        self.assertEqual(out.returncode, 0, out.stderr)
        line = [ln for ln in out.stdout.splitlines() if ln.startswith("HPCLIB_SMB_STATUS ")][0]
        status = json.loads(line.split(" ", 1)[1])
        self.assertEqual((status["host"], status["credentials"], status["image"]), ("files.example.edu", True, True))


if __name__ == "__main__":
    unittest.main()
