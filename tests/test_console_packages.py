"""Tests for packages: apps and settings for the console from zip files (console_packages.py), how the console
installs and uses them, and how a cluster receives them (setup_tunnel.sh --receive).

Run with:  python -m unittest tests.test_console_packages -v
"""
import base64
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agent_console import ConsoleTestCase  # noqa: E402
import test_agent_console  # noqa: E402
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


def external_installer_package():
    """A tunnel whose installer and payload live elsewhere in the zip."""
    manifest = dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], install_files={
        "install.sh": "install/install.sh", "helpers/value.txt": "install/value.txt"})})
    files = {k: v for k, v in DEMO_FILES.items() if not k.endswith("/install.sh")}
    files.update({"install/install.sh": '#!/bin/bash\n# hpclib-install: --check\n'
                  'set -e\n[ "$(cat helpers/value.txt)" = payload ]\n'
                  '[ "$1" = --check ] || cp helpers/value.txt "$DEMO_DIR/installed.txt"\n'
                  '[ -f "$DEMO_DIR/installed.txt" ]\necho installed: payload\n',
                  "install/value.txt": "payload\n"})
    return manifest, files


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

    def test_replacing_keeps_other_packages(self):
        """Replacing a package never touches another package's directory (a package once named "demo.old")."""
        other = self.tmp / "packages" / "demo.old"
        other.mkdir(parents=True)
        (other / "package.json").write_text(json.dumps({"name": "demo.old", "version": "1", "apps": {}, "tunnels": {}, "settings": {}}))
        self.store.install(make_zip(DEMO, DEMO_FILES))
        self.store.install(make_zip(DEMO, DEMO_FILES))
        self.assertTrue((other / "package.json").is_file())
        self.assertEqual(sorted(p.name for p in (self.tmp / "packages").iterdir()), ["demo", "demo.old"])

    def test_a_zip_of_a_folder(self):
        self.store.install(make_zip(LAB, LAB_FILES, prefix="our-lab/"))
        self.assertEqual([m["name"] for m, _ in self.store.installed()], ["our-lab"])

    def test_install_files_stage_and_replace(self):
        manifest, files = external_installer_package()
        report = self.store.install(make_zip(manifest, files))
        self.assertTrue(report["package"]["tunnels"]["demo-tunnel"]["install"])
        root = str(self.tmp / "staging")
        stage, digest = self.store.stage("demo-tunnel", root)
        self.assertEqual((Path(stage) / "tunnel/helpers/value.txt").read_text(), "payload\n")
        self.assertTrue(os.access(Path(stage) / "tunnel/install.sh", os.X_OK))
        self.assertEqual(self.store.stage("demo-tunnel", root), (stage, digest))
        self.store.install(make_zip(dict(manifest, version="1.1"), dict(files, **{"install/value.txt": "new\n"})))
        changed, new_digest = self.store.stage("demo-tunnel", root)
        self.assertNotEqual(new_digest, digest)
        self.assertEqual((Path(changed) / "tunnel/helpers/value.txt").read_text(), "new\n")
        self.assertEqual((Path(stage) / "tunnel/helpers/value.txt").read_text(), "payload\n")
        # Removing a declaration removes its staged payload; legacy install.sh still goes along.
        self.store.install(make_zip(dict(DEMO, version="1.2"), DEMO_FILES))
        changed, _ = self.store.stage("demo-tunnel", root)
        self.assertFalse((Path(changed) / "tunnel/helpers").exists())
        self.assertTrue((Path(changed) / "tunnel/install.sh").is_file())

    def test_install_modules_defaults_and_validation(self):
        manifest, files = external_installer_package()
        manifest["apps"]["demo"]["install_modules"] = ["GCC/12.2.0", "WebProxy"]
        report = self.store.install(make_zip(manifest, files))
        self.assertEqual(report["package"]["apps"]["demo"]["install_modules"], ["GCC/12.2.0", "WebProxy"])
        for value in (None, "WebProxy", ["--force"], ["a;b"], [""], ["x"] * 33, ["a" * 129]):
            with self.subTest(value=value):
                manifest["apps"]["demo"]["install_modules"] = value
                with self.assertRaises(console_packages.PackageError):
                    self.store.inspect(make_zip(manifest, files))

    def test_install_files_list(self):
        manifest, files = external_installer_package()
        manifest["apps"]["demo"]["install_files"] = ["install.sh", "helpers/value.txt"]
        files["install.sh"] = files.pop("install/install.sh")
        files["helpers/value.txt"] = files.pop("install/value.txt")
        self.store.install(make_zip(manifest, files))
        stage, _ = self.store.stage("demo-tunnel", str(self.tmp / "staging"))
        self.assertEqual((Path(stage) / "tunnel/helpers/value.txt").read_text(), "payload\n")

    def test_install_files_refused(self):
        manifest, files = external_installer_package()
        files["package.json"] = "{}\n"
        for declaration in ("install.sh", [1], ["install/install.sh"] * 2, {"x": "missing"},
                            {"package.json": "package.json"},
                            {"../escape": "install/value.txt"}, {"/abs": "install/value.txt"},
                            {"a/../escape": "install/value.txt"}, {"a\\b": "install/value.txt"},
                            {"a/": "install/value.txt"}, {"a\n": "install/value.txt"},
                            {"helpers": "install/value.txt", "helpers/x": "install/value.txt"},
                            {"sbatch_script.sh/x": "install/value.txt"},
                            {"sbatch_script.sh": "install/value.txt"}):
            with self.subTest(declaration=declaration):
                app = dict(manifest["apps"]["demo"], install_files=declaration)
                with self.assertRaises(console_packages.PackageError):
                    self.store.inspect(make_zip(dict(manifest, apps={"demo": app}), files))
        app = dict(manifest["apps"]["demo"], tunnel="vscode")
        with self.assertRaisesRegex(console_packages.PackageError, "owned by this package"):
            self.store.inspect(make_zip(dict(manifest, apps={"demo": app}), files))
        app = dict(manifest["apps"]["demo"], install_files={"helpers/value.txt": "install/install.sh"})
        with self.assertRaisesRegex(console_packages.PackageError, "conflicting destination"):
            self.store.inspect(make_zip(dict(manifest, apps=dict(manifest["apps"], other=app)), files))

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
            (make_zip(dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], password_regex="(a)(b)")}), DEMO_FILES),
             "password_regex needs exactly one group"),
            (make_zip(dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], login_user="demo")}), DEMO_FILES),
             "login_user needs password_regex"),
            (make_zip(dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], password_regex="(x)", login_user="a b")}),
                      DEMO_FILES), "login_user is a user name"),
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

    def test_declared_installer_is_installable_and_pushed(self):
        manifest, files = external_installer_package()
        self.assertEqual(self._post(make_zip(manifest, files), dry_run=False)[0], 201)
        base = f"/api/apps/demo/{self.dead}"
        self.assertTrue(self.call("GET", base + "/settings")[1]["installable"])
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.assertEqual(self.call("POST", base + "/install")[0], 202)
        call = self.shell.calls[-1]
        self.assertIn("--install", call)
        pushed = Path(call[call.index("--push") + 1])
        self.assertTrue((pushed / "tunnel/install.sh").is_file())
        self.assertEqual((pushed / "tunnel/helpers/value.txt").read_text(), "payload\n")

    def test_password_regex(self):
        """an app that prints a password (OpenCode's basic-auth login) gets VS Code's Copy password"""
        manifest = dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], password_regex=r"^demo password: (\S+)$")})
        status, out = self._post(make_zip(manifest, DEMO_FILES), dry_run=False)
        self.assertEqual(status, 201, out)
        pattern = agent_console.APPS["demo"]["password_re"]
        self.assertEqual(pattern.findall("starting\ndemo password: s3cret\nready\n"), ["s3cret"])
        self.call("DELETE", "/api/packages/demo")

    def test_login_user(self):
        """with login_user, Open signs in to the app's basic-auth login itself"""
        manifest = dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], password_regex=r"^demo password: (\S+)$",
                                                 login_user="demo")})
        self.assertEqual(self._post(make_zip(manifest, DEMO_FILES), dry_run=False)[0], 201)
        base = f"/api/apps/demo/{self.dead}"
        self.call("GET", base)
        port = agent_profiles.load(self.dead)["apps"]["demo"]["port"]
        logs = self.console / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / f"{self.dead}.demo.log"
        log.write_text("\n=== launch ===\nstarting\n")
        test_agent_console.TestApps.serve(self, port)   # a fake app answering on its port
        state = self.call("GET", base)[1]
        self.assertEqual((state["state"], state["username"], state["password"]), ("up", "demo", None))
        self.assertEqual(state["url"], f"http://127.0.0.1:{port}/")       # nothing to sign in with yet
        log.write_text("\n=== launch ===\ndemo password: s3/cr@t\n")
        state = self.call("GET", base)[1]
        self.assertEqual(state["password"], "s3/cr@t")
        self.assertEqual(state["url"], f"http://demo:s3%2Fcr%40t@127.0.0.1:{port}/")
        self.call("DELETE", "/api/packages/demo")

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

    def test_tokens_panel(self):
        tokens = {"label": "Write tokens", "scopes": ["read", "write", "admin"], "default_scope": "write", "days": 90}
        manifest = dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], tokens=tokens)})
        files = dict(DEMO_FILES, **{"tunnels/demo-tunnel/tokens.sh": "#!/bin/bash\n"})
        self.assertEqual(self._post(make_zip(manifest, files), dry_run=False)[0], 201)
        base = f"/api/apps/demo/{self.dead}"
        panel = self.call("GET", base + "/settings")[1]["tokens"]
        self.assertEqual((panel["label"], panel["scopes"], panel["default_scope"], panel["days"]),
                         ("Write tokens", ["read", "write", "admin"], "write", 90))
        self.assertIsNone(self.call("GET", f"/api/apps/vscode/{self.dead}/settings")[1]["tokens"])
        self.assertEqual(self.call("GET", base + "/tokens")[0], 409)                       # needs the login
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            host = agent_profiles.load(self.dead)["host"]
            self.assertEqual(self.call("GET", base + "/tokens")[1]["tokens"], [])
            self.assertEqual(self.shell.calls[-1][-4:], [host, "demo-tunnel", "--tokens", "list"])
            # a token: its secret comes back once, in this answer
            status, out = self.call("POST", base + "/tokens", {"label": "jenga mcp", "scopes": ["write"]})
            self.assertEqual(status, 201, out)
            self.assertEqual(out["token"], "pai_t0000000_SECRETSECRET")
            self.assertEqual(out["record"]["scopes"], ["write"])
            self.assertEqual(self.shell.calls[-1][-8:], ["--tokens", "create", "--label", "jenga mcp", "--scopes", "write",
                                                         "--days", "90"])
            # ... and is never listed, logged or stored here
            listed = self.call("GET", base + "/tokens")[1]["tokens"]
            self.assertEqual([(t["id"], t["label"], t["revoked"]) for t in listed], [("t0000000", "jenga mcp", False)])
            self.assertNotIn("hash", listed[0])
            self.assertNotIn("must-never-reach-the-page", json.dumps(listed))
            everything = json.dumps(agent_profiles.load(self.dead)) + "".join(
                p.read_text(errors="replace") for p in (self.console).rglob("*") if p.is_file() and p.suffix != ".zip")
            self.assertNotIn("SECRETSECRET", everything)
            # saved to a file on the cluster: the answer has no token at all
            status, out = self.call("POST", base + "/tokens", {"label": "ci", "scopes": ["read", "write"], "days": 0,
                                                              "save_to": "/scratch/user/me/llm/jenga.token"})
            self.assertEqual((status, out["token"], out["saved_to"]), (201, None, "/scratch/user/me/llm/jenga.token"), out)
            self.assertEqual(self.shell.calls[-1][-4:], ["--days", "0", "--save-to", "/scratch/user/me/llm/jenga.token"])
            self.assertIsNone(out["record"]["expires"])
            for bad in ({"label": ""}, {"label": "x;rm"}, {"label": "x", "scopes": ["root"]}, {"label": "x", "scopes": []},
                        {"label": "x", "days": -1}, {"label": "x", "days": "9"}, {"label": "x", "days": True},
                        {"label": "x", "save_to": "relative"}, {"label": "x", "save_to": "/a/../b"},
                        {"label": "x", "save_to": "/a/b/"}, {"label": "x", "save_to": "/a b"},
                        {"label": "x", "save_to": "/a\nb"}, {"label": "x", "scopes": ["write", "write"]}):
                self.assertEqual(self.call("POST", base + "/tokens", bad)[0], 422, bad)
            self.assertEqual(self.call("POST", base + "/tokens", {"x": 1})[0], 400)
            calls = len(self.shell.calls)
            self.assertEqual(self.call("DELETE", base + "/tokens/t0000000")[1]["revoked"], "t0000000")
            self.assertTrue(self.call("GET", base + "/tokens")[1]["tokens"][0]["revoked"])
            self.assertEqual(self.call("DELETE", base + "/tokens/t0000000;x")[0], 400)       # not a token id
            self.assertEqual(self.call("DELETE", base + "/tokens/nope")[0], 502)             # tokens.sh refuses
            self.assertEqual(self.shell.calls[calls][-3:], ["--tokens", "revoke", "t0000000"])
            # a cluster without the tokens.sh yet: the reason, and no output quoted for a create
            self.shell.tokens_error = "setup_tunnel: demo-tunnel has no tokens.sh on this cluster"
            status, out = self.call("GET", base + "/tokens")
            self.assertEqual(status, 502)
            self.assertIn("no tokens.sh", out["error"])
            self.shell.tokens_error = "pai_t1_LEAKED traceback"
            status, out = self.call("POST", base + "/tokens", {"label": "x"})
            self.assertEqual(status, 502)
            self.assertNotIn("LEAKED", out["error"])
        # a login node that can't run tokens.sh: on the REST server's node, through the agent tunnel, with the owner token
        self.shell.tokens_error = "HPCLIB_TOKENS_ELSEWHERE neither singularity nor apptainer is on PATH"
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            with mock.patch.object(self.clusters, "tunnel_state", return_value={"state": "down"}):
                status, out = self.call("GET", base + "/tokens")
            self.assertEqual(status, 409)
            self.assertIn("login node (neither singularity nor apptainer is on PATH)", out["error"])
            self.assertIn("start the agent tunnel", out["error"])
            sent = []
            def rest(profile, verb, route, query=None, body=None, headers=None, as_agent=False, timeout=None):
                sent.append((verb, route, json.loads(body)))
                if json.loads(body)["action"] == "create":
                    return 200, "application/json", json.dumps({"exit_code": 0, "node": "c01", "stderr": "", "stdout":
                        'HPCLIB_TOKEN_CREATED {"id": "t9", "label": "x", "scopes": ["write"], "token": "pai_t9_S"}'}).encode()
                return 200, "application/json", json.dumps({"exit_code": 0, "node": "c01", "stderr": "",
                                                            "stdout": 'HPCLIB_TOKENS {"tokens": []}'}).encode()
            with mock.patch.object(self.clusters, "tunnel_state", return_value={"state": "up"}), \
                    mock.patch.object(self.clusters, "call", side_effect=rest), \
                    mock.patch.object(agent_console, "read_private", return_value="owner-secret"):
                self.assertEqual(self.call("GET", base + "/tokens")[1]["tokens"], [])
                out = self.call("POST", base + "/tokens", {"label": "x", "scopes": ["write"]})[1]
                self.assertEqual(out["token"], "pai_t9_S")
            self.assertEqual(sent[0], ("POST", "/admin/tunnel-tokens", {"tunnel": "demo-tunnel", "action": "list", "args": []}))
            self.assertEqual(sent[1][2]["args"], ["--label", "x", "--scopes", "write", "--days", "90"])
            # nor there: says so (setup_tunnel.sh there exits 3 without asking to go elsewhere again)
            elsewhere = lambda *a, **k: (200, "application/json", json.dumps(
                {"exit_code": 3, "node": "c01", "stdout": "", "stderr": "setup_tunnel: tokens.sh can't run on c01 "
                 "(the REST server's node) either: no image at /x"}).encode())
            with mock.patch.object(self.clusters, "tunnel_state", return_value={"state": "up"}), \
                    mock.patch.object(self.clusters, "call", side_effect=elsewhere), \
                    mock.patch.object(agent_console, "read_private", return_value="owner-secret"):
                status, out = self.call("GET", base + "/tokens")
            self.assertEqual(status, 502)
            self.assertIn("can't run on c01 either", out["error"])
            self.assertIn("no image at /x", out["error"])
            # the marker without setup_tunnel.sh's exit 3 (printed by tokens.sh itself, say) asks for nothing
            with mock.patch.object(self.clusters.logins, "alive", return_value=True), \
                    mock.patch.object(self.clusters, "call") as called:
                self.clusters.shell_runner = lambda args, log: (log.write(b"HPCLIB_TOKENS_ELSEWHERE spoofed\n"),
                                                                subprocess.Popen(["true"]))[1]
                self.call("GET", base + "/tokens")
                called.assert_not_called()
            self.clusters.shell_runner = self.shell
        self.shell.tokens_error = None
        # an app without a `tokens` section has no panel and no routes
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.assertEqual(self.call("GET", f"/api/apps/vscode/{self.dead}/tokens")[0], 404)
            self.assertEqual(self.call("POST", f"/api/apps/vscode/{self.dead}/tokens", {"label": "x"})[0], 404)

    DOWNLOADS = {"label": "Model Download", "description": "models for the job",
                 "kinds": [{"id": "ollama", "label": "Ollama", "placeholder": "granite4.2:30b",
                            "pattern": r"[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?"},
                           {"id": "vllm", "label": "vLLM", "placeholder": "org/model",
                            "pattern": r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"}]}

    def test_downloads(self):
        manifest = dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], downloads=self.DOWNLOADS)})
        files = dict(DEMO_FILES, **{"tunnels/demo-tunnel/downloads.sh": "#!/bin/bash\n"})
        status, out = self._post(make_zip(manifest, files), dry_run=False)
        self.assertEqual(status, 201, out)
        self.assertIn("app demo: Model Download (Ollama, vLLM)", out["summary"])
        base = f"/api/apps/demo/{self.dead}"
        state = self.call("GET", base)[1]
        self.assertEqual([k["id"] for k in state["downloads"]["kinds"]], ["ollama", "vllm"])
        self.assertNotIn("downloads", self.call("GET", f"/api/apps/vscode/{self.dead}")[1])
        self.assertEqual(self.call("GET", base + "/downloads")[0], 409)                    # needs the login
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            host = agent_profiles.load(self.dead)["host"]
            out = self.call("GET", base + "/downloads")[1]
            self.assertEqual((out["items"], out["note"]), ([], "4.0 TB free"))
            self.assertEqual(self.shell.calls[-1][-2:], ["--downloads", "list"])
            self.assertIn(host, self.shell.calls[-1])
            # a download is an operation with its log, followed by a check
            status, op = self.call("POST", base + "/downloads", {"kind": "ollama", "name": "granite4.2:30b"})
            self.assertEqual((status, op["kind"], op["title"], op["app"]), (202, "download", "download granite4.2:30b", "demo"))
            self.assertEqual(self.shell.calls[-1][-5:], ["--check", "--downloads", "pull", "ollama", "granite4.2:30b"])
            self.assertEqual(test_agent_console.TestTunnelInstall.wait_op(self, self.dead)["state"], "succeeded")
            self.assertEqual(self.call("GET", base)[1]["install"]["state"], "missing")          # the check ran
            items = self.call("GET", base + "/downloads")[1]["items"]
            self.assertEqual(items, [{"kind": "ollama", "name": "granite4.2:30b", "size": 1000, "in_use": False,
                                      "note": ""}])
            self.assertEqual(self.call("POST", base + "/downloads",
                                       {"kind": "vllm", "name": "ibm-granite/granite-4.2-30b"})[0], 202)
            test_agent_console.TestTunnelInstall.wait_op(self, self.dead)
            # names that don't fit the kind, or could reach outside it, are refused here
            for bad in ({"kind": "ollama", "name": "Bad Name"}, {"kind": "vllm", "name": "no-org"},
                        {"kind": "vllm", "name": "../x"}, {"kind": "ollama", "name": "a/../b"},
                        {"kind": "ollama", "name": "x; rm -rf ~"}, {"kind": "hf", "name": "a/b"},
                        {"kind": "ollama", "name": "-x"}, {"kind": "ollama", "name": 3}):
                self.assertEqual(self.call("POST", base + "/downloads", bad)[0], 422, bad)
            self.assertEqual(self.call("POST", base + "/downloads", {"name": "x"})[0], 400)
            # remove: gone from the list; what is in use is refused, with the reason
            out = self.call("POST", base + "/downloads/remove", {"kind": "ollama", "name": "granite4.2:30b"})[1]
            self.assertEqual(out["removed"], {"kind": "ollama", "name": "granite4.2:30b"})
            self.assertEqual([i["name"] for i in self.call("GET", base + "/downloads")[1]["items"]],
                             ["ibm-granite/granite-4.2-30b"])
            self.shell.downloads[0]["in_use"] = True
            status, out = self.call("POST", base + "/downloads/remove", {"kind": "vllm", "name": "ibm-granite/granite-4.2-30b"})
            self.assertEqual(status, 502)
            self.assertIn("in use by a running job", out["error"])
            # what downloads.sh lists is passed on field by field
            self.shell.downloads = [{"kind": "ollama", "name": "x", "size": "big", "in_use": "yes", "secret": 1},
                                    {"kind": "other", "name": "y"}, "junk"]
            self.assertEqual(self.call("GET", base + "/downloads")[1]["items"],
                             [{"kind": "ollama", "name": "x", "size": None, "in_use": False, "note": ""}])
            self.shell.downloads_error = "setup_tunnel: demo-tunnel has no downloads.sh on this cluster"
            status, out = self.call("GET", base + "/downloads")
            self.assertEqual(status, 502)
            self.assertIn("no downloads.sh", out["error"])
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            self.assertEqual(self.call("GET", f"/api/apps/vscode/{self.dead}/downloads")[0], 404)

    def test_downloads_manifest_checks(self):
        files = dict(DEMO_FILES, **{"tunnels/demo-tunnel/downloads.sh": "#!/bin/bash\n"})
        def check(downloads, files=files):
            return self._post(make_zip(dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], downloads=downloads)}),
                                       files), dry_run=True)
        self.assertEqual(check(self.DOWNLOADS)[0], 200)
        self.assertEqual(check({"kinds": [{"id": "model"}]})[0], 200)            # label and pattern have defaults
        for bad, words in (({"kinds": []}, "1 to 8"), ({"kinds": [{"id": "Bad"}]}, "kind ids"),
                           ({"kinds": [{"id": "a"}, {"id": "a"}]}, "kind ids"),
                           ({"kinds": [{"id": "a", "pattern": "("}]}, "pattern"),
                           ({"kinds": [{"id": "a", "url": "x"}]}, "each kind"), ({"kinds": [{"id": "a"}], "x": 1}, "downloads is")):
            status, out = check(bad)
            self.assertEqual(status, 422, bad)
            self.assertIn(words, out["error"], bad)
        status, out = check(self.DOWNLOADS, files=DEMO_FILES)
        self.assertIn("downloads.sh", out["error"])

    def test_tokens_manifest_checks(self):
        files = dict(DEMO_FILES, **{"tunnels/demo-tunnel/tokens.sh": "#!/bin/bash\n"})
        good = {"scopes": ["read", "write"], "default_scope": "write", "days": 30, "label": "Keys"}
        def check(tokens, files=files):
            return self._post(make_zip(dict(DEMO, apps={"demo": dict(DEMO["apps"]["demo"], tokens=tokens)}), files),
                              dry_run=True)
        self.assertEqual(check(good)[0], 200)
        self.assertEqual(check({"scopes": ["read"]})[0], 200)                  # defaults: first scope, 90 days
        for bad, why in (({"scopes": []}, "scopes"), ({"scopes": ["Read"]}, "scopes"), ({"scopes": ["a", "a"]}, "scopes"),
                         ({}, "scopes"), (dict(good, default_scope="admin"), "default_scope"),
                         (dict(good, days=-1), "days"), (dict(good, days=1.5), "days"), (dict(good, days=99999), "days"),
                         (dict(good, extra=1), "tokens is"), ("yes", "tokens is")):
            status, out = check(bad)
            self.assertEqual(status, 422, bad)
            self.assertIn(why, out["error"], bad)
        status, out = check(good, DEMO_FILES)                                  # the tunnel has no tokens.sh
        self.assertEqual(status, 422)
        self.assertIn("tokens.sh", out["error"])

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

    def test_declared_files_through_ssh_push(self):
        """Exercise the real tar/SSH pipeline and receiver, replacing only the remote SSH transport."""
        manifest, files = external_installer_package()
        store = console_packages.PackageStore(str(self.tmp / "packages"), {}, set())
        store.install(make_zip(manifest, files))
        stage, _ = store.stage("demo-tunnel", str(self.tmp / "staging"))
        script = '''
            source "$HPCLIB_DIR/hpclib.sh" >/dev/null
            pssh() { [ "$1" = test-host ] || return 9; bash -c "$2"; }
            tunnel_setup test-host demo-tunnel --push "$1" --set "DEMO_DIR=$2" --save --install --check
        '''
        env = dict(self.env, HPCLIB_REMOTE_INSTALL_LOCATION=str(HPCLIB))
        result = subprocess.run(["bash", "-c", script, "test", stage, str(self.tmp)],
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HPCLIB_TUNNEL_STATUS installed installed: payload", result.stdout)
        self.assertEqual((self.tmp / "installed.txt").read_text(), "payload\n")
        self.assertTrue((self.tmp / "installed/demo-tunnel/install.sh").is_file())

    def test_install_hardening(self):
        """Settings are yours alone; a secret sent earlier doesn't reach the installer's environment; an installer's
        exit 3 asks for the REST node only from the login node; one installation of a tunnel runs at a time."""
        tunnel = self.tmp / "tunnels" / "demo-tunnel"
        tunnel.mkdir(parents=True)
        (tunnel / "sbatch_script.sh").write_text("true\n")
        (tunnel / "tunnel_config.sh").write_text('TUNNEL_SETTINGS="DEMO_DIR"\nTUNNEL_SECRETS="DEMO_KEY"\n')
        (tunnel / "install.sh").write_text(textwrap.dedent("""\
            #!/bin/bash
            # hpclib-install: --check
            env > "$DEMO_DIR/installer-env"
            [ -n "${DEMO_SLOW:-}" ] && sleep 3
            exit "${DEMO_EXIT:-0}"
            """))
        env = dict(self.env, HPCLIB_TUNNEL_PATH=str(self.tmp / "tunnels"), DEMO_DIR=str(self.tmp))
        def run(*args, input=None, **extra):
            return subprocess.run(["bash", str(HPCLIB / "tunnels" / "setup_tunnel.sh"), "demo-tunnel", *args],
                                  capture_output=True, text=True, env=dict(env, **extra), input=input, timeout=60)
        out = run("--set", f"DEMO_DIR={self.tmp}", "--save")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(stat.S_IMODE((self.tmp / "data/settings/demo-tunnel.sh").stat().st_mode), 0o600)
        secret = base64.b64encode(b"s3cret-value").decode()
        self.assertEqual(run("--secrets", input=f"DEMO_KEY={secret}\n").returncode, 0)
        out = run("--secrets", "--install", input=f"DEMO_KEY={secret}\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn(secret, (self.tmp / "installer-env").read_text())
        out = run("--install", "--check", DEMO_EXIT="3")
        self.assertEqual(out.returncode, 3)
        self.assertIn("HPCLIB_INSTALL_ELSEWHERE", out.stdout)
        out = run("--install", "--check", DEMO_EXIT="3", HPCLIB_INSTALL_VIA="rest")
        self.assertEqual(out.returncode, 3)
        self.assertNotIn("HPCLIB_INSTALL_ELSEWHERE", out.stdout)
        self.assertIn("HPCLIB_TUNNEL_STATUS missing", out.stdout)
        if shutil.which("flock"):
            first = subprocess.Popen(["bash", str(HPCLIB / "tunnels" / "setup_tunnel.sh"), "demo-tunnel", "--install"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     env=dict(env, DEMO_SLOW="1"))
            time.sleep(1)
            second = run("--install")
            first.communicate(timeout=30)
            self.assertIn("waiting for another installation of demo-tunnel", second.stdout)
            self.assertEqual(second.returncode, 0, second.stderr)

    def test_tokens_option(self):
        """setup_tunnel.sh --tokens runs the tunnel's tokens.sh as `tokens.sh --json ACTION ...` and passes its JSON on."""
        tunnel = self.tmp / "tunnels" / "demo-tunnel"
        tunnel.mkdir(parents=True)
        (tunnel / "sbatch_script.sh").write_text("true\n")
        (tunnel / "tunnel_config.sh").write_text('TUNNEL_SETTINGS="DEMO_DIR"\n')
        (tunnel / "tokens.sh").write_text(textwrap.dedent("""\
            #!/bin/bash
            echo "noise on stdout"
            printf '%s\\n' "$*" > "$DEMO_DIR/args"
            case "$2" in
              list) [ -z "$DEMO_FAIL" ] || { echo "the directory of /x/y does not exist" >&2; exit 1; }
                    echo '{"tokens": []}' ;;
              create) echo '{"id": "ab", "token": "tok_secret"}' ;;
              revoke) echo '{"revoked": "ab"}' ;;
            esac
            """))
        env = dict(self.env, HPCLIB_TUNNEL_PATH=str(self.tmp / "tunnels") + ":" + str(HPCLIB / "tunnels"), DEMO_DIR=str(self.tmp))
        def run(*args, **extra):
            return subprocess.run(["bash", str(HPCLIB / "tunnels" / "setup_tunnel.sh"), "demo-tunnel", *args],
                                  capture_output=True, text=True, env=dict(env, **extra), timeout=60)
        out = run("--tokens", "list")
        self.assertEqual((out.returncode, out.stdout.strip()), (0, 'HPCLIB_TOKENS {"tokens": []}'), out.stderr)
        self.assertEqual((self.tmp / "args").read_text().strip(), "--json list")
        out = run("--tokens", "create", "--label", "a b", "--scopes", "write", "--days", "5")
        self.assertEqual(out.stdout.strip(), 'HPCLIB_TOKEN_CREATED {"id": "ab", "token": "tok_secret"}')
        self.assertEqual((self.tmp / "args").read_text().strip(), "--json create --label a b --scopes write --days 5")
        out = run("--tokens", "revoke", "ab")
        self.assertEqual(out.stdout.strip(), "HPCLIB_TOKEN_REVOKED ab")
        for args in (("--tokens", "nope"), ("--tokens",)):
            out = run(*args)
            self.assertNotEqual(out.returncode, 0, args)
            self.assertNotIn("tok_secret", out.stdout + out.stderr)
        # a failure says why, in tokens.sh's own words
        self.assertIn("setup_tunnel: tokens.sh list failed: the directory of /x/y does not exist", run("--tokens", "list", DEMO_FAIL="1").stderr)
        self.assertEqual(run("--tokens", "list", DEMO_FAIL="1").returncode, 1)
        # a tokens.sh that can't work on this node (exit 3): passed on, for the console to try the REST server's node
        (tunnel / "tokens.sh").write_text('#!/bin/bash\necho "neither singularity nor apptainer is on PATH" >&2\nexit 3\n')
        out = run("--tokens", "list")
        self.assertEqual((out.returncode, out.stdout.strip()),
                         (3, "HPCLIB_TOKENS_ELSEWHERE neither singularity nor apptainer is on PATH"))
        (tunnel / "tokens.sh").unlink()
        out = run("--tokens", "list")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no tokens.sh", out.stderr)

    def test_older_hpclib_in_bashrc(self):
        """~/.bashrc pointing HPCLIB_DIR at another copy: it is still used (as before), with a warning if it's older."""
        other = self.tmp / "scratch" / "hpclib"
        shutil.copytree(HPCLIB, other)
        hpclib_sh = other / "hpclib.sh"
        hpclib_sh.write_text(re.sub(r'HPCLIB_VERSION="[^"]*"', 'HPCLIB_VERSION="0.1.0"', hpclib_sh.read_text()))
        (self.tmp / "home" / ".bashrc").write_text(f"export HPCLIB_DIR={other}\n")
        tunnel = self.tmp / "tunnels" / "demo-tunnel"
        tunnel.mkdir(parents=True)
        (tunnel / "sbatch_script.sh").write_text("true\n")
        (tunnel / "tunnel_config.sh").write_text("")
        (tunnel / "install.sh").write_text('#!/bin/bash\n# hpclib-install: --check\necho "installed: $HPCLIB_DIR"\n')
        env = dict(self.env, HPCLIB_TUNNEL_PATH=str(self.tmp / "tunnels"))
        env.pop("HPCLIB_DIR")
        def check():
            return subprocess.run(["bash", str(HPCLIB / "tunnels" / "setup_tunnel.sh"), "demo-tunnel", "--check"],
                                  capture_output=True, text=True, env=env, timeout=60)
        out = check()
        self.assertIn(f"HPCLIB_TUNNEL_STATUS installed installed: {other}", out.stdout, out.stderr)   # used, as before
        self.assertIn(f"WARNING: running hpclib 0.1.0 from {other}", out.stderr)
        self.assertIn(f"older than", out.stderr)
        # the same version elsewhere, or this copy through a link: nothing to say
        hpclib_sh.write_text((HPCLIB / "hpclib.sh").read_text())
        self.assertNotIn("WARNING", check().stderr)
        link = self.tmp / "link"
        link.symlink_to(HPCLIB)
        (self.tmp / "home" / ".bashrc").write_text(f"export HPCLIB_DIR={link}\n")
        self.assertNotIn("WARNING", check().stderr)

    def test_downloads_option(self):
        """setup_tunnel.sh --downloads: list and remove pass downloads.sh's JSON on; pull shows its progress."""
        tunnel = self.tmp / "tunnels" / "demo-tunnel"
        tunnel.mkdir(parents=True)
        (tunnel / "sbatch_script.sh").write_text("true\n")
        (tunnel / "tunnel_config.sh").write_text('TUNNEL_SETTINGS="DEMO_DIR"\n')
        (tunnel / "install.sh").write_text("#!/bin/bash\n# hpclib-install: --check\necho installed: yes\n")
        (tunnel / "downloads.sh").write_text(textwrap.dedent("""\
            #!/bin/bash
            printf '%s\\n' "$*" > "$DEMO_DIR/args"
            case "$1:$2" in
              --json:list) echo "noise"; echo '{"items": [{"kind": "ollama", "name": "m:1", "size": 5}]}' ;;
              --json:remove) [ "$4" != busy ] || { echo "busy is in use by job 7" >&2; exit 1; }
                             echo '{"removed": "'"$4"'"}' ;;
              pull:*) echo "pulling $3: 50%"; [ "$3" != bad ] || exit 3; echo "pulling $3: done" ;;
            esac
            """))
        env = dict(self.env, HPCLIB_TUNNEL_PATH=str(self.tmp / "tunnels") + ":" + str(HPCLIB / "tunnels"), DEMO_DIR=str(self.tmp))
        def run(*args):
            return subprocess.run(["bash", str(HPCLIB / "tunnels" / "setup_tunnel.sh"), "demo-tunnel", *args],
                                  capture_output=True, text=True, env=env, timeout=60)
        out = run("--downloads", "list")
        self.assertEqual((out.returncode, out.stdout.strip()),
                         (0, 'HPCLIB_DOWNLOADS {"items": [{"kind": "ollama", "name": "m:1", "size": 5}]}'), out.stderr)
        out = run("--downloads", "remove", "ollama", "m:1")
        self.assertEqual(out.stdout.strip(), 'HPCLIB_DOWNLOAD_REMOVED {"removed": "m:1"}')
        self.assertEqual((self.tmp / "args").read_text().strip(), "--json remove ollama m:1")
        out = run("--downloads", "remove", "ollama", "busy")
        self.assertEqual(out.returncode, 1)
        self.assertIn("setup_tunnel: downloads.sh remove failed: busy is in use by job 7", out.stderr)
        # a pull: its progress as it comes, then the check asked for before it
        out = run("--check", "--downloads", "pull", "ollama", "m:1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("pulling m:1: 50%\npulling m:1: done", out.stdout)
        self.assertIn("HPCLIB_TUNNEL_STATUS installed installed: yes", out.stdout)
        self.assertEqual((self.tmp / "args").read_text().strip(), "pull ollama m:1")
        out = run("--check", "--downloads", "pull", "ollama", "bad")
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("HPCLIB_TUNNEL_STATUS", out.stdout)
        self.assertIn("downloading bad failed", out.stderr)
        for args in (("--downloads", "nope"), ("--downloads",), ("--downloads", "pull", "ollama"),
                     ("--downloads", "list", "x")):
            self.assertEqual(run(*args).returncode, 2, args)
        (tunnel / "downloads.sh").unlink()
        out = run("--downloads", "list")
        self.assertIn("no downloads.sh", out.stderr)

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
