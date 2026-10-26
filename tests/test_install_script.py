"""install.sh, the one-line installer for your machine: run as `curl ... | bash` would run it (the script on
stdin), against a clone of this repository, in a temporary home. The MCP SDK step is skipped (--no-mcp) except in
a dry run, so no network is needed."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"


class TestInstallScript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.home = self.tmp / "home"
        self.home.mkdir()
        # a repository to clone: this one's files, committed (the working tree may have changes)
        self.repo = self.tmp / "repo"
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(ROOT), str(self.repo)], check=True)
        for name in ("install.sh", "hpclib/launch-tunnel-manager", "hpclib/hpclib.sh"):
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_bytes((ROOT / name).read_bytes())
        git = ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "--quiet", "--allow-empty", "-m", "test"], check=True)

    def install(self, *args, shell="/bin/bash"):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "SHELL": shell, "NO_COLOR": "1"}
        return subprocess.run(["bash", "-s", "--", "--repo", str(self.repo), *args], input=INSTALL.read_text(),
                              capture_output=True, text=True, env=env, timeout=120)

    def test_install_then_update(self):
        out = self.install("--no-mcp", shell="/bin/zsh")
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        link = self.home / ".local" / "bin" / "launch-tunnel-manager"
        self.assertEqual(Path(os.readlink(link)), self.home / "hpclib" / "hpclib" / "launch-tunnel-manager")
        bashrc = (self.home / ".bashrc").read_text()
        self.assertIn('source "$HOME/hpclib/hpclib/hpclib.sh"', bashrc)
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', bashrc)
        zshrc = (self.home / ".zshrc").read_text()
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', zshrc)
        self.assertNotIn("hpclib.sh", zshrc)                      # hpclib's functions are bash
        # a new bash shell has the functions and the launcher
        shell = subprocess.run(["bash", "-ic", "type -t launch_tunnel; launch-tunnel-manager --where"],
                               capture_output=True, text=True, timeout=60,
                               env={"HOME": str(self.home), "PATH": "/usr/bin:/bin", "TERM": "dumb"})
        self.assertIn("function", shell.stdout)
        self.assertIn(f"hpclib:   {self.home / 'hpclib' / 'hpclib'}", shell.stdout)
        # again: an update, with the startup lines replaced, not added twice
        (self.home / ".bashrc").write_text(bashrc + "alias ll='ls -l'\n")
        again = self.install("--no-mcp")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("updating", again.stdout)
        bashrc = (self.home / ".bashrc").read_text()
        self.assertEqual(bashrc.count("# >>> hpclib (install.sh) >>>"), 1)
        self.assertIn("alias ll='ls -l'", bashrc)

    def test_local_changes_are_left_alone(self):
        self.assertEqual(self.install("--no-mcp").returncode, 0)
        readme = self.home / "hpclib" / "README.md"
        readme.write_text("mine\n")
        out = self.install("--no-mcp")
        self.assertEqual(out.returncode, 0)
        self.assertIn("local changes", out.stdout)
        self.assertEqual(readme.read_text(), "mine\n")

    def test_dry_run_and_refusals(self):
        out = self.install("--dry-run", "--no-shell")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("would run: git clone", out.stdout)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])
        (self.home / "hpclib").mkdir()
        (self.home / "hpclib" / "notes.txt").write_text("x")
        out = self.install("--no-mcp")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("isn't a git clone", out.stderr)
        self.assertNotEqual(self.install("--bogus").returncode, 0)

    def test_a_cut_download_runs_nothing(self):
        text = INSTALL.read_text()
        cut = text[:text.index("hpclib_install_main \"$@\"")]     # everything but the last line
        out = subprocess.run(["bash", "-s", "--", "--repo", str(self.repo)], input=cut[: len(cut) // 2],
                             capture_output=True, text=True, timeout=60,
                             env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"})
        self.assertFalse((self.home / "hpclib").exists())
        self.assertNotIn("cloning", out.stdout)

    def test_powershell_installer_runs_this_one(self):
        ps1 = (ROOT / "install.ps1").read_text()
        self.assertIn("/install.sh", ps1)
        self.assertTrue(ps1.isascii())                # Windows PowerShell 5.1 reads scripts as ANSI
        self.assertIn("Install-Hpclib @args", ps1.splitlines()[-1])
        self.assertTrue((ROOT / "agent-console" / "icon.ico").is_file())


if __name__ == "__main__":
    unittest.main()
