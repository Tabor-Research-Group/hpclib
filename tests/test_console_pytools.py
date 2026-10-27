"""uv and pixi on a cluster from the console (lib/pytools.sh; GET/PUT /api/clusters/NAME/python-tools and POST
.../install), with the script run here standing in for the login node, and installers served from files."""
import os
import subprocess
import unittest
from pathlib import Path

from test_agent_console import ConsoleTestCase  # noqa: E402
import agent_console  # noqa: E402
import rest_envs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PYTOOLS_SH = ROOT / "hpclib" / "lib" / "pytools.sh"

FAKE_UV_INSTALLER = """#!/bin/sh
echo "downloading uv 0.9.9"
mkdir -p "$UV_INSTALL_DIR"
printf '#!/bin/sh\\ncase "$1" in --version) echo "uv 0.9.9 (abc 2026-10-01)";; python) echo "$HOME/.local/share/uv/python";; cache) echo "$HOME/.cache/uv";; esac\\n' > "$UV_INSTALL_DIR/uv"
chmod +x "$UV_INSTALL_DIR/uv"
echo "installed uv to $UV_INSTALL_DIR (path update: ${UV_NO_MODIFY_PATH:+no})"
"""


class TestPythonTools(ConsoleTestCase):
    def setUp(self):
        super().setUp()
        self.clusters.logins.alive = lambda profile: True
        self.home = Path(self.tmp) / "remote-home"
        self.home.mkdir()
        (self.home / ".bashrc").write_text("export UV_CACHE_DIR=/scratch/user/me/.uv/cache\necho hi from bashrc\n"
                                           "cd /\n")
        self.remote_data = Path(self.tmp) / "remote-data"
        installers = Path(self.tmp) / "installers"
        installers.mkdir()
        (installers / "uv.sh").write_text(FAKE_UV_INSTALLER)
        (installers / "pixi.sh").write_text("#!/bin/bash\necho 'pixi: no network here' >&2\nexit 7\n")
        self.ran = []

        def runner(profile, script, args=(), wait=60):
            self.ran.append(tuple(args))
            env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "HPCTUNNELS_DATA_DIR": str(self.remote_data),
                   "HPCLIB_UV_INSTALLER": f"file://{installers}/uv.sh",
                   "HPCLIB_PIXI_INSTALLER": f"file://{installers}/pixi.sh"}
            res = subprocess.run(["bash", "-s", "--", *args], input=script, capture_output=True, text=True,
                                 env=env, timeout=wait)
            return res.returncode, res.stdout, res.stderr
        self.clusters.script_runner = runner

    def tools_url(self, rest=""):
        return f"/api/clusters/{self.dead}/python-tools{rest}"

    def test_report_install_and_directories(self):
        status, out = self.call("GET", self.tools_url())
        self.assertEqual(status, 200, out)
        self.assertEqual(out["tools"], {"uv": {"path": None, "version": None}, "pixi": {"path": None, "version": None}})
        self.assertEqual(out["shell"]["UV_CACHE_DIR"], "/scratch/user/me/.uv/cache")       # what ~/.bashrc sets
        self.assertIsNone(out["shell"]["PIXI_CACHE_DIR"])
        self.assertEqual(out["dirs"]["UV_CACHE_DIR"]["source"], "default")
        # install uv: into ~/.local/bin, without touching ~/.bashrc
        bashrc = (self.home / ".bashrc").read_text()
        status, out = self.call("POST", self.tools_url("/install"), {"tool": "uv"})
        self.assertEqual(status, 200, out)
        self.assertTrue(out["install"]["ok"], out)
        self.assertIn("path update: no", " ".join(out["install"]["log"]))
        self.assertEqual(out["tools"]["uv"], {"path": f"{self.home}/.local/bin/uv", "version": "0.9.9"})
        self.assertEqual((self.home / ".bashrc").read_text(), bashrc)
        # a failing installer says why
        status, out = self.call("POST", self.tools_url("/install"), {"tool": "pixi"})
        self.assertEqual(status, 200, out)
        self.assertEqual((out["install"]["ok"], out["install"]["exit_code"]), (False, 7))
        self.assertIn("no network here", out["install"]["error"])
        self.assertEqual(self.call("POST", self.tools_url("/install"), {"tool": "conda"})[0], 400)
        # directories: saved on the cluster and created; "" goes back to the default
        py = f"{self.tmp}/scratch/.uv/python"
        status, out = self.call("PUT", self.tools_url(), {"UV_PYTHON_INSTALL_DIR": py, "PIXI_CACHE_DIR": f"{self.tmp}/scratch/.pixi"})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["dirs"]["UV_PYTHON_INSTALL_DIR"], {"value": py, "source": "settings", "saved": py})
        self.assertTrue(os.path.isdir(py))
        saved = (self.remote_data / "settings" / "pytools.sh").read_text()
        self.assertIn(f"UV_PYTHON_INSTALL_DIR={py}", saved)
        status, out = self.call("PUT", self.tools_url(), {"PIXI_CACHE_DIR": ""})
        self.assertEqual(out["dirs"]["PIXI_CACHE_DIR"]["source"], "default")
        self.assertEqual(out["dirs"]["UV_PYTHON_INSTALL_DIR"]["source"], "settings")      # the other is kept
        for bad in ({"UV_CACHE_DIR": "relative/dir"}, {"PATH": "/x"}, {}, {"UV_CACHE_DIR": "/a\nb"}):
            self.assertEqual(self.call("PUT", self.tools_url(), bad)[0], 400, bad)
        # the REST server's syncs use the saved directories where the environment doesn't set them
        self.assertEqual(rest_envs.saved_tool_dirs({"HPCTUNNELS_DATA_DIR": str(self.remote_data)}),
                         {"UV_PYTHON_INSTALL_DIR": py})
        envs = rest_envs.EnvironmentManager(environ={"HPCTUNNELS_DATA_DIR": str(self.remote_data),
                                                     "UV_PYTHON_INSTALL_DIR": "/mine"})
        self.assertEqual(envs.env()["UV_PYTHON_INSTALL_DIR"], "/mine")
        envs = rest_envs.EnvironmentManager(environ={"HPCTUNNELS_DATA_DIR": str(self.remote_data)})
        self.assertEqual(envs.env()["UV_PYTHON_INSTALL_DIR"], py)

    def test_sourcing_hpclib_exports_the_saved_directories(self):
        data = Path(self.tmp) / "d"
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "HPCTUNNELS_DATA_DIR": str(data)}
        subprocess.run(["bash", str(PYTOOLS_SH), "set", "UV_CACHE_DIR", f"{self.tmp}/c"], env=env, check=True)
        show = '. "$1"; printf "%s|%s" "${UV_CACHE_DIR:-}" "${PIXI_CACHE_DIR:-}"'
        out = subprocess.run(["bash", "-c", show, "x", str(PYTOOLS_SH)], env=env, capture_output=True, text=True).stdout
        self.assertEqual(out, f"{self.tmp}/c|")
        out = subprocess.run(["bash", "-c", show, "x", str(PYTOOLS_SH)], env=dict(env, UV_CACHE_DIR="/from/bashrc"),
                             capture_output=True, text=True).stdout
        self.assertEqual(out, "/from/bashrc|")                          # your environment wins

    def test_needs_the_login(self):
        self.clusters.logins.alive = lambda profile: False
        self.assertEqual(self.call("GET", self.tools_url())[0], 409)

    def test_script_parses(self):
        self.assertEqual(subprocess.run(["bash", "-n"], input=agent_console.pytools_script("hpclib_pytools --json"),
                                        text=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
