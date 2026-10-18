"""Installer module environments through the real SSH receiver and REST worker, without a cluster."""
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hpclib/servers"))
import rest_installs


class TestInstallationModules(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        home = self.root / "home"
        home.mkdir()
        (home / ".bashrc").write_text(textwrap.dedent("""\
            module() {
              [ "$1" = load ] || return 9
              printf '%s\\n' "$2" >> "$MODULE_LOG"
              [ "${SIMULATE_LOGIN:-0}" = 0 ] || return 1
              case "$2" in
                Base/1.0) export MODULE_BASE=ready ;;
                WebProxy) [ "${MODULE_BASE:-}" = ready ] || return 2
                          export https_proxy=http://proxy.invalid:8080 ;;
                *) return 1 ;;
              esac
            }
            """))
        self.tunnel = self.root / "tunnels/demo"
        self.tunnel.mkdir(parents=True)
        (self.tunnel / "sbatch_script.sh").write_text("true\n")
        (self.tunnel / "tunnel_config.sh").write_text('HPCLIB_INSTALL_MODULES="${HPCLIB_INSTALL_MODULES-Base/1.0:WebProxy}"\n')
        (self.tunnel / "install.sh").write_text(textwrap.dedent("""\
            #!/bin/bash
            # hpclib-install: --check --force
            [ "$MODULE_BASE" = ready ] && [ "$https_proxy" = http://proxy.invalid:8080 ] || exit 4
            if [ "${1:-}" != --check ]; then echo "$*" > "$INSTALL_MARKER"; fi
            [ -f "$INSTALL_MARKER" ] || exit 1
            echo 'installed: modules reached the installer'
            """))
        self.env = {"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(self.root),
                    "HPCLIB_DIR": str(ROOT / "hpclib"), "HPCTUNNELS_DATA_DIR": str(self.root / "data"),
                    "HPCLIB_TUNNEL_PATH": str(self.root / "tunnels"),
                    "MODULE_LOG": str(self.root / "modules"), "INSTALL_MARKER": str(self.root / "installed")}

    def run_setup(self, *args, **env):
        return subprocess.run(["bash", str(ROOT / "hpclib/tunnels/setup_tunnel.sh"), "demo", *args],
                              env=dict(self.env, **env), text=True, capture_output=True, timeout=30)

    def test_modules_load_in_order_for_install_update_and_check(self):
        result = self.run_setup("--set", "HPCLIB_INSTALL_MODULES=Base/1.0:WebProxy", "--save", "--install", "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HPCLIB_TUNNEL_STATUS installed", result.stdout)
        self.assertEqual((self.root / "modules").read_text().splitlines(), ["Base/1.0", "WebProxy"] * 2)
        result = self.run_setup("--install", "--force", "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "installed").read_text().strip(), "--force")

    def test_failed_login_module_load_defers_and_rest_uses_saved_modules(self):
        result = self.run_setup("--set", "HPCLIB_INSTALL_MODULES=Base/1.0:WebProxy", "--save", "--install",
                                SIMULATE_LOGIN="1")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("HPCLIB_INSTALL_ELSEWHERE", result.stdout)
        self.assertFalse((self.root / "installed").exists())
        with mock.patch.dict(os.environ, self.env, clear=True):
            worker = rest_installs.TunnelInstalls()
            record = worker.start("demo", force=True)
            for _ in range(100):
                result = worker.status(record["id"])
                if result["state"] != "running":
                    break
                time.sleep(0.02)
        self.assertEqual((result["state"], result["exit_code"]), ("succeeded", 0), result)
        self.assertIn("HPCLIB_TUNNEL_STATUS installed", result["output"])
        self.assertEqual((self.root / "installed").read_text().strip(), "--force")

    def test_empty_list_clears_default_and_check_reports_unavailable_modules(self):
        (self.tunnel / "install.sh").write_text("# hpclib-install: --check\nprintf 'installed: plain\\n'\n")
        result = self.run_setup("--set", "HPCLIB_INSTALL_MODULES=", "--save", "--install", "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / "modules").exists())
        self.assertIn("installed: plain", self.run_setup("--check").stdout)
        result = self.run_setup("--set", "HPCLIB_INSTALL_MODULES=NoSuchModule", "--check")
        self.assertIn("HPCLIB_TUNNEL_STATUS unknown", result.stdout)
        self.assertIn("could not load installation module NoSuchModule", result.stdout)

    def test_invalid_modules_cannot_run_shell_or_replace_saved_settings(self):
        self.run_setup("--set", "HPCLIB_INSTALL_MODULES=Base/1.0:WebProxy", "--save")
        saved = self.root / "data/settings/demo.sh"
        before = saved.read_bytes()
        for value in ("WebProxy;touch BAD", "$(touch BAD)", "--force", ":WebProxy", "WebProxy:",
                      "WebProxy::Base", "a:" * 32 + "a", "a" * 129, "a\nb"):
            with self.subTest(value=value):
                result = self.run_setup("--set", "HPCLIB_INSTALL_MODULES=" + value, "--save", "--install")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(saved.read_bytes(), before)
        self.assertFalse((self.root / "modules").exists())
        self.assertFalse((self.tunnel / "BAD").exists())


if __name__ == "__main__":
    unittest.main()
