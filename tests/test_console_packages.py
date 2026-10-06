"""Tests for packages: apps and settings for the console from zip files (console_packages.py), how the console
installs and uses them, and how a cluster receives them (setup_tunnel.sh --receive).

Run with:  python -m unittest tests.test_console_packages -v
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agent_console import ConsoleTestCase  # noqa: E402
import agent_console  # noqa: E402
import agent_profiles  # noqa: E402
import console_packages  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
HPCLIB = REPO / "hpclib"

DEMO = {
    "format": 1, "name": "demo", "version": "1.0.0", "description": "A demo app.",
    "apps": {"demo": {"title": "Demo", "tunnel": "demo-tunnel", "open_path": "/", "health_path": "/health",
                      "settings": [{"name": "DEMO_DIR", "label": "Directory", "hint": "where"},
                                   {"name": "DEMO_MODE", "label": "Mode", "hint": "", "choices": ["a", "b"]}]}},
    "tunnels": {"demo-tunnel": "tunnels/demo-tunnel"},
    "settings": {"demo-tunnel": {"values": {"DEMO_DIR": "/data/demo"}}},
}
LAB = {
    "format": 1, "name": "our-lab", "version": "0.1",
    "settings": {"data-transfer": {"values": {"SMB_HOST": "files.example.edu", "SMB_DOMAIN": "EXAMPLE"},
                                   "files": {"rclone.conf": "settings/rclone.conf"}}},
}
DEMO_FILES = {"tunnels/demo-tunnel/sbatch_script.sh": "#!/bin/bash\necho demo\n",
              "tunnels/demo-tunnel/tunnel_config.sh": 'TUNNEL_SETTINGS="DEMO_DIR DEMO_MODE"\n',
              "tunnels/demo-tunnel/install.sh": "#!/bin/bash\n# hpclib-install: --check\necho installed: yes\n"}
LAB_FILES = {"settings/rclone.conf": "[ours]\ntype = alias\nremote = smb:research/our_group\n"}


def make_zip(manifest, files=(), links=(), prefix=""):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if manifest is not None:
            zf.writestr(prefix + console_packages.MANIFEST, json.dumps(manifest))
        for path, text in dict(files).items():
            zf.writestr(prefix + path, text)
        for path, target in links:
            info = zipfile.ZipInfo(prefix + path)
            info.external_attr = (0o120777) << 16
            zf.writestr(info, target)
    return buf.getvalue()


class TestPackageStore(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        builtin = {k: (v["tunnel"], [f[0] for f in v.get("settings", [])]) for k, v in agent_console.BUILTIN_APPS.items()}
        self.store = console_packages.PackageStore(str(self.tmp / "packages"), builtin,
                                                   set(os.listdir(HPCLIB / "tunnels")))

    def test_install_use_and_remove(self):
        out = self.store.install(make_zip(DEMO, DEMO_FILES))
        self.assertEqual((out["package"]["name"], out["replaces"], out["problems"]), ("demo", None, []))
        self.assertEqual(list(self.store.apps()), ["demo"])
        tunnel = Path(self.store.tunnel_dir("demo-tunnel"))
        self.assertTrue((tunnel / "sbatch_script.sh").is_file())
        self.assertTrue(os.access(tunnel / "install.sh", os.X_OK))
        self.assertEqual(self.store.defaults("demo-tunnel"), {"DEMO_DIR": ("/data/demo", "demo")})
        # settings for one of hpclib's own tunnels, with a file
        self.store.install(make_zip(LAB, LAB_FILES))
        self.assertEqual(self.store.defaults("data-transfer")["SMB_DOMAIN"], ("EXAMPLE", "our-lab"))
        stage, digest = self.store.stage("data-transfer", str(self.tmp / "staging"))
        self.assertEqual(os.listdir(stage), ["settings.d"])
        self.assertIn("[ours]", (Path(stage) / "settings.d" / "rclone.conf").read_text())
        self.assertEqual(self.store.stage("data-transfer", str(self.tmp / "staging")), (stage, digest))  # stable
        stage, digest = self.store.stage("demo-tunnel", str(self.tmp / "staging"))
        self.assertEqual(sorted(os.listdir(stage)), ["settings.d", "tunnel"])
        empty, none = self.store.stage("vscode", str(self.tmp / "staging"))
        self.assertEqual((os.listdir(empty), none), (["settings.d"], None))
        # a new version replaces the old one
        out = self.store.install(make_zip(dict(DEMO, version="1.1.0"), DEMO_FILES))
        self.assertEqual(out["replaces"], "1.0.0")
        self.assertEqual([m["version"] for m, _ in self.store.installed() if m["name"] == "demo"], ["1.1.0"])
        self.store.remove("demo")
        self.assertEqual(self.store.apps(), {})
        with self.assertRaises(console_packages.PackageError):
            self.store.remove("demo")

    def test_a_zip_of_a_folder(self):
        self.store.install(make_zip(LAB, LAB_FILES, prefix="our-lab/"))
        self.assertEqual([m["name"] for m, _ in self.store.installed()], ["our-lab"])

    def test_refused(self):
        bad = [
            (b"not a zip", "not a zip"),
            (make_zip(None, LAB_FILES), "no hpclib-package.json"),
            (make_zip(DEMO, DEMO_FILES, links=[("tunnels/demo-tunnel/evil", "/etc/passwd")]), "symbolic link"),
            (make_zip(DEMO, dict(DEMO_FILES, **{"../escape.sh": "x"})), "outside"),
            (make_zip(DEMO, dict(DEMO_FILES, **{"/abs.sh": "x"})), "absolute"),
            (make_zip(dict(DEMO, surprise=1), DEMO_FILES), "unknown keys"),
            (make_zip(dict(DEMO, format=2), DEMO_FILES), "format"),
            (make_zip(dict(DEMO, name="../x"), DEMO_FILES), "name"),
            (make_zip(DEMO, {k: v for k, v in DEMO_FILES.items() if "sbatch" not in k}), "sbatch_script.sh"),
            (make_zip(dict(DEMO, tunnels={"vscode": "tunnels/demo-tunnel"}), DEMO_FILES), "hpclib's own tunnels"),
            (make_zip(dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], tunnel="nowhere")}), DEMO_FILES),
             "neither in this package"),
            (make_zip(dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], token_regex="(")}), DEMO_FILES),
             "token_regex"),
            (make_zip(dict(LAB, settings={"data-transfer": {"files": {"rclone.conf": "missing.conf"}}}), LAB_FILES),
             "not in the zip"),
            (make_zip(dict(LAB, settings={"data-transfer": {"values": {"SMB_HOST": "a\nb"}}}), LAB_FILES),
             "one-line"),
        ]
        for data, words in bad:
            with self.assertRaises(console_packages.PackageError, msg=words) as ctx:
                self.store.install(data)
            self.assertIn(words, str(ctx.exception))
        # conflicts with hpclib's apps, and between packages
        for manifest, words in ((dict(DEMO, apps={"vscode": DEMO["apps"]["demo"]}), "one of hpclib's own"),
                                (dict(LAB, settings={"data-transfer": {"values": {"PATH": "/evil"}}}),
                                 "not a setting of its apps")):
            report = self.store.inspect(make_zip(manifest, dict(DEMO_FILES, **LAB_FILES)))
            self.assertTrue(any(words in p for p in report["problems"]), report["problems"])
        self.store.install(make_zip(LAB, LAB_FILES))
        rival = dict(LAB, name="rival-lab")
        report = self.store.inspect(make_zip(rival, LAB_FILES))
        self.assertTrue(any("already sets SMB_HOST" in p for p in report["problems"]), report["problems"])
        with self.assertRaises(console_packages.PackageError):
            self.store.install(make_zip(rival, LAB_FILES))
        self.assertFalse((self.tmp / "packages" / "rival-lab").exists())

    def test_build(self):
        src = self.tmp / "src" / "demo"
        for path, text in dict(DEMO_FILES, **{console_packages.MANIFEST: json.dumps(DEMO)}).items():
            (src / path).parent.mkdir(parents=True, exist_ok=True)
            (src / path).write_text(text)
        (src / "tunnels" / "demo-tunnel" / "install.sh").chmod(0o755)
        res = subprocess.run([sys.executable, str(HPCLIB / "servers" / "console_packages.py"), "build", str(src)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        built = self.tmp / "src" / "demo-1.0.0.zip"
        self.assertIn("app Demo (demo)", res.stdout)
        self.store.install(built.read_bytes())
        self.assertTrue(os.access(Path(self.store.tunnel_dir("demo-tunnel")) / "install.sh", os.X_OK))
        (src / "tunnels" / "demo-tunnel" / "link").symlink_to("/etc/passwd")
        res = subprocess.run([sys.executable, str(HPCLIB / "servers" / "console_packages.py"), "build", str(src)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 1)
        self.assertIn("symbolic link", res.stderr)


class TestConsolePackages(ConsoleTestCase):

    def _post(self, data, dry_run):
        req = urllib.request.Request(self.base + "/api/packages" + ("?dry_run=1" if dry_run else ""), data=data,
                                     method="POST", headers={"Authorization": "Bearer console-key",
                                                             "Content-Type": "application/zip"})
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_install_use_remove(self):
        data = make_zip(DEMO, DEMO_FILES)
        status, out = self._post(data, dry_run=True)
        self.assertEqual((status, out["installed"], out["problems"]), (200, False, []), out)
        self.assertIn("app Demo (demo), tunnel demo-tunnel", out["summary"])
        self.assertNotIn("demo", [a["id"] for a in self.call("GET", "/api/apps")[1]["apps"]])   # not yet
        status, out = self._post(data, dry_run=False)
        self.assertEqual((status, out["installed"]), (201, True), out)
        apps = {a["id"]: a for a in self.call("GET", "/api/apps")[1]["apps"]}
        self.assertEqual((apps["demo"]["title"], apps["demo"]["package"]), ("Demo", "demo"))
        self.assertIsNone(apps["vscode"]["package"])
        self.assertEqual([p["name"] for p in self.call("GET", "/api/packages")[1]["packages"]], ["demo"])
        # its settings: the package's value where you've set none, yours where you have
        base = f"/api/apps/demo/{self.dead}"
        settings = self.call("GET", base + "/settings")[1]
        self.assertEqual(settings["defaults"], {"DEMO_DIR": {"value": "/data/demo", "package": "demo"}})
        self.assertTrue(settings["installable"])
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.shell.setup_status = "installed installed: yes"
            self.call("POST", base + "/check")
            call = self.shell.calls[-1]
            self.assertIn("--push", call)
            pushed = Path(call[call.index("--push") + 1])
            self.assertTrue((pushed / "tunnel" / "sbatch_script.sh").is_file())
            self.assertIn("DEMO_DIR=/data/demo", call)
            self.call("PUT", base + "/settings", {"settings": {"DEMO_DIR": "/mine", "DEMO_MODE": "b"}})
            self.call("POST", base + "/check")
            self.assertIn("DEMO_DIR=/mine", self.shell.calls[-1])
            # Start sends nothing more while the package is unchanged
            n = len(self.shell.calls)
            self.assertEqual(self.call("POST", base + "/start")[0], 202)
            self.assertEqual(self.shell.calls[n][0], "launch_tunnel")
            self.call("POST", base + "/stop")
        status, out = self.call("DELETE", "/api/packages/demo")
        self.assertEqual((status, out["apps"]), (200, ["demo"]), out)
        self.assertNotIn("demo", [a["id"] for a in self.call("GET", "/api/apps")[1]["apps"]])
        self.assertEqual(self.call("DELETE", "/api/packages/demo")[0], 404)

    def test_secrets(self):
        manifest = dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], secrets=[
            {"name": "DEMO_TOKEN", "label": "Token", "hint": "the API token"}])})
        files = dict(DEMO_FILES, **{"tunnels/demo-tunnel/tunnel_config.sh":
                                    'TUNNEL_SETTINGS="DEMO_DIR DEMO_MODE"\nTUNNEL_SECRETS="DEMO_TOKEN"\n'})
        self.assertEqual(self._post(make_zip(manifest, files), dry_run=False)[0], 201)
        base = f"/api/apps/demo/{self.dead}"
        self.assertEqual(self.call("GET", base + "/settings")[1]["secrets"],
                         [{"name": "DEMO_TOKEN", "label": "Token", "hint": "the API token", "set": None}])
        sent = []

        def runner(args, data, wait):
            sent.append((args, data))
            return 0, "HPCLIB_TUNNEL_SECRETS DEMO_TOKEN:set\n"
        self.clusters.secret_runner = runner
        self.assertEqual(self.call("PUT", base + "/secrets", {"values": {"DEMO_TOKEN": "x"}})[0], 409)   # the login
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            status, out = self.call("PUT", base + "/secrets", {"values": {"DEMO_TOKEN": " t0k=n "}})
            self.assertEqual((status, out["secrets"][0]["set"]), (200, True), out)
            args, data = sent[-1]
            self.assertEqual(args[-2:], ["demo-tunnel", "--secrets"])
            self.assertNotIn("t0k=n", " ".join(args))                 # never on a command line ...
            self.assertEqual(data, b"DEMO_TOKEN=dDBrPW4=\n")         # ... only on standard input, base64
            for bad in ({"values": {"OTHER": "x"}}, {"values": {"DEMO_TOKEN": "a\nb"}}, {"values": {}}, {"x": 1}):
                self.assertEqual(self.call("PUT", base + "/secrets", bad)[0], 422, bad)
            self.clusters.secret_runner = lambda args, data, wait: (1, "setup_tunnel: usage\n")
            self.assertEqual(self.call("PUT", base + "/secrets", {"values": {"DEMO_TOKEN": ""}})[0], 502)
            self.assertEqual(self.call("PUT", f"/api/apps/vscode/{self.dead}/secrets", {"values": {"X": "y"}})[0], 404)
        # the console keeps whether it is set, never the value
        stored = json.dumps(agent_profiles.load(self.dead))
        self.assertNotIn("t0k=n", stored)
        self.assertEqual(agent_profiles.load(self.dead)["apps"]["demo"]["secrets"], {"DEMO_TOKEN": True})
        # a Check reports them too
        self.shell.setup_status = "installed installed: yes\nHPCLIB_TUNNEL_SECRETS DEMO_TOKEN:unset"
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.call("POST", base + "/check")
        self.assertFalse(self.call("GET", base + "/settings")[1]["secrets"][0]["set"])
        bad = dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], secrets=[{"name": "DEMO_DIR", "label": "x"}])})
        status, out = self._post(make_zip(bad, files), dry_run=True)
        self.assertEqual(status, 422)
        self.assertIn("both a setting and a secret", out["error"])

    def test_settings_package_for_an_hpclib_app(self):
        self.assertEqual(self._post(make_zip(LAB, LAB_FILES), dry_run=False)[0], 201)
        settings = self.call("GET", f"/api/apps/transfer/{self.dead}/settings")[1]
        self.assertEqual(settings["defaults"]["SMB_HOST"], {"value": "files.example.edu", "package": "our-lab"})
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.call("POST", f"/api/apps/transfer/{self.dead}/check")
            call = self.shell.calls[-1]
            self.assertIn("SMB_HOST=files.example.edu", call)
            stage = Path(call[call.index("--push") + 1])
            self.assertEqual(os.listdir(stage), ["settings.d"])
            # Rclone shares the tunnel: its Check sends the same files (it has no settings of its own)
            self.call("POST", f"/api/apps/rclone/{self.dead}/check")
            self.assertIn("--push", self.shell.calls[-1])
            # once removed, the next Check sends an empty settings.d, clearing the cluster's copy
            self.call("DELETE", "/api/packages/our-lab")
            self.call("POST", f"/api/apps/transfer/{self.dead}/check")
            call = self.shell.calls[-1]
            self.assertEqual(os.listdir(Path(call[call.index("--push") + 1]) / "settings.d"), [])
            self.call("POST", f"/api/apps/transfer/{self.dead}/check")
            self.assertNotIn("--push", self.shell.calls[-1])          # and after that, nothing
        self.assertNotIn("SMB_HOST", self.call("GET", f"/api/apps/transfer/{self.dead}/settings")[1]["defaults"])

    def test_refusals(self):
        status, out = self._post(b"PK not really", dry_run=True)
        self.assertEqual(status, 422, out)
        status, out = self._post(make_zip(dict(DEMO, apps={"jupyter": DEMO["apps"]["demo"]}), DEMO_FILES), False)
        self.assertEqual(status, 422)
        self.assertIn("one of hpclib's own", out["error"])
        status, out = self.call("POST", "/api/packages", key=None)
        self.assertEqual(status, 401)


class TestClusterReceives(unittest.TestCase):
    """setup_tunnel.sh --receive, as tunnel_setup --push feeds it over ssh."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "home").mkdir()
        (self.tmp / "home" / ".bashrc").write_text("")
        self.env = dict(os.environ, HOME=str(self.tmp / "home"), HPCLIB_DIR=str(HPCLIB),
                        HPCTUNNELS_DATA_DIR=str(self.tmp / "data"),
                        HPCLIB_TUNNEL_INSTALL_LOCATION=str(self.tmp / "installed"))
        self.env.pop("HPCLIB_TUNNEL_PATH", None)

    def send(self, tunnel, tree, *args):
        src = self.tmp / "stage"
        shutil.rmtree(src, ignore_errors=True)
        for path, text in tree.items():
            (src / path).parent.mkdir(parents=True, exist_ok=True)
            if text is None:
                (src / path).mkdir()
            else:
                (src / path).write_text(text)
        tar = subprocess.run(["tar", "-c", "-f", "-", "-C", str(src), "."], capture_output=True, check=True).stdout
        return subprocess.run(["bash", str(HPCLIB / "tunnels" / "setup_tunnel.sh"), tunnel, "--receive", *args],
                              input=tar, capture_output=True, env=self.env, timeout=120)

    def test_tunnel_and_settings_files(self):
        res = self.send("demo-tunnel", {"tunnel/sbatch_script.sh": "#!/bin/bash\n", "tunnel/tunnel_config.sh": "",
                                        "tunnel/install.sh": "#!/bin/bash\n# hpclib-install: --check\necho installed: ok\n",
                                        "settings.d/extra.conf": "x=1\n"}, "--check")
        out = res.stdout.decode()
        self.assertEqual(res.returncode, 0, res.stderr.decode() + out)
        self.assertTrue((self.tmp / "installed" / "demo-tunnel" / "sbatch_script.sh").is_file())
        self.assertEqual((self.tmp / "data" / "settings" / "demo-tunnel.d" / "extra.conf").read_text(), "x=1\n")
        self.assertIn("HPCLIB_TUNNEL_STATUS installed installed: ok", out)
        # an empty settings.d clears them; no tunnel/ leaves the installed tunnel as it is
        res = self.send("demo-tunnel", {"settings.d": None})
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertFalse((self.tmp / "data" / "settings" / "demo-tunnel.d").exists())
        self.assertTrue((self.tmp / "installed" / "demo-tunnel" / "sbatch_script.sh").is_file())

    def test_secrets(self):
        tunnel = {"tunnel/sbatch_script.sh": "#!/bin/bash\n", "tunnel/tunnel_config.sh": 'TUNNEL_SECRETS="TOKEN"\n'}
        self.assertEqual(self.send("sec-tunnel", tunnel).returncode, 0)

        def secrets(text, *args):
            return subprocess.run(["bash", str(HPCLIB / "tunnels" / "setup_tunnel.sh"), "sec-tunnel", "--secrets",
                                   *args], input=text.encode(), capture_output=True, env=self.env, timeout=60)
        res = secrets("TOKEN=" + "c2VjcmV0IHZhbHVl" + "\n", "--check")      # "secret value"
        out = res.stdout.decode()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertIn("HPCLIB_TUNNEL_SECRETS TOKEN:set", out)
        self.assertNotIn("secret value", out + res.stderr.decode())
        path = self.tmp / "data" / "secrets" / "sec-tunnel" / "TOKEN"
        self.assertEqual(path.read_text(), "secret value")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), "0o700")
        self.assertEqual(secrets("OTHER=eA==\n").returncode, 2)
        self.assertEqual(secrets("TOKEN=not*base64\n").returncode, 2)
        self.assertEqual(path.read_text(), "secret value")               # a refused value changes nothing
        res = secrets("TOKEN=\n")
        self.assertIn(b"TOKEN:unset", res.stdout)
        self.assertFalse(path.exists())
        res = subprocess.run(["bash", str(HPCLIB / "tunnels" / "setup_tunnel.sh"), "vscode", "--secrets"],
                             input=b"X=eA==\n", capture_output=True, env=self.env, timeout=60)
        self.assertEqual(res.returncode, 2)
        self.assertIn(b"has no secrets", res.stderr)

    def test_refused(self):
        res = self.send("vscode", {"tunnel/sbatch_script.sh": "#!/bin/bash\n"})
        self.assertEqual(res.returncode, 1)
        self.assertIn(b"one of hpclib's own tunnels", res.stderr)
        src = self.tmp / "evil"
        (src / "settings.d").mkdir(parents=True)
        (src / "settings.d" / "link").symlink_to("/etc/passwd")
        tar = subprocess.run(["tar", "-c", "-f", "-", "-C", str(src), "."], capture_output=True, check=True).stdout
        res = subprocess.run(["bash", str(HPCLIB / "tunnels" / "setup_tunnel.sh"), "x-tunnel", "--receive"],
                             input=tar, capture_output=True, env=self.env, timeout=60)
        self.assertEqual(res.returncode, 1)
        self.assertIn(b"not a plain tar archive", res.stderr)
        self.assertFalse((self.tmp / "data" / "settings" / "x-tunnel.d").exists())


if __name__ == "__main__":
    unittest.main()
