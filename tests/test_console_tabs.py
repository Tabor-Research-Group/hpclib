"""Native package tab validation and console API regression tests (stdlib only)."""
import copy
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hpclib" / "servers"))
import agent_console
import console_packages


def manifest():
    return {"format": 1, "name": "tab-demo", "version": "1.0.0", "apps": {
        "preview": {"title": "Preview service", "tunnel": "preview",
                    "tab": {"parent": "demo", "id": "preview", "label": "Preview"}},
        "demo": {"title": "Demo", "tunnel": "demo"},
        "rehearsal": {"title": "Rehearsal service", "tunnel": "rehearsal",
                      "tab": {"parent": "demo", "id": "rehearsal", "label": "Release Rehearsal"}}},
        "tunnels": {name: "tunnels/" + name for name in ("demo", "preview", "rehearsal")}}


def package(spec):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(console_packages.MANIFEST, json.dumps(spec))
        for name in spec["tunnels"]:
            archive.writestr("tunnels/" + name + "/sbatch_script.sh", "#!/bin/bash\ntrue\n")
    return data.getvalue()


class TestPackageTabs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = console_packages.PackageStore(str(Path(self.tmp.name) / "packages"), {}, set())

    def test_install_reload_and_summary_with_child_before_parent(self):
        out = self.store.install(package(manifest()))
        self.assertEqual(out["problems"], [])
        apps = self.store.apps()
        self.assertEqual(apps["preview"][1]["tab"], manifest()["apps"]["preview"]["tab"])
        summary = console_packages.summary(out["package"])
        self.assertIn("tab Preview in Demo (preview), tunnel preview", summary)
        self.assertIn("app Demo (demo), tunnel demo", summary)

    def test_bad_tab_shapes_fail_during_inspection(self):
        for tab in (True, [], {}, {"parent": "demo", "id": "preview"},
                    {"parent": "demo", "id": "preview", "label": "Preview", "extra": 1}):
            with self.subTest(tab=tab):
                spec = manifest()
                spec["apps"]["preview"]["tab"] = tab
                with self.assertRaises(console_packages.PackageError):
                    self.store.inspect(package(spec))

    def test_bad_parent_and_page_ids_fail(self):
        for key, value in (("parent", 3), ("parent", "../demo"), ("parent", "absent"),
                           ("parent", "preview"), ("parent", "rehearsal"), ("parent", "agents"),
                           ("id", "sessions"), ("id", "../preview"), ("id", 3),
                           ("label", ""), ("label", "x" * 41), ("label", 3)):
            with self.subTest(key=key, value=value):
                spec = manifest()
                spec["apps"]["preview"]["tab"][key] = value
                with self.assertRaises(console_packages.PackageError):
                    self.store.inspect(package(spec))

    def test_cycles_and_duplicate_pages_fail(self):
        spec = manifest()
        spec["apps"]["demo"]["tab"] = {"parent": "preview", "id": "demo", "label": "Demo"}
        with self.assertRaises(console_packages.PackageError):
            self.store.inspect(package(spec))
        spec = manifest()
        spec["apps"]["rehearsal"]["tab"]["id"] = "preview"
        with self.assertRaises(console_packages.PackageError):
            self.store.inspect(package(spec))

    def test_same_page_id_in_different_parents_is_valid(self):
        spec = manifest()
        spec["apps"]["other"] = {"title": "Other", "tunnel": "demo"}
        spec["apps"]["rehearsal"]["tab"].update(parent="other", id="preview")
        self.assertEqual(self.store.inspect(package(spec))["problems"], [])

    def test_ungrouped_upgrade_keeps_service_ids_and_tunnels(self):
        spec = manifest()
        old = copy.deepcopy(spec)
        old["version"] = "0.9.0"
        for app in old["apps"].values():
            app.pop("tab", None)
        self.store.install(package(old))
        before = {key: value[1]["tunnel"] for key, value in self.store.apps().items()}
        out = self.store.install(package(spec))
        self.assertEqual(out["replaces"], "0.9.0")
        self.assertEqual(before, {key: value[1]["tunnel"] for key, value in self.store.apps().items()})

    def test_backend_load_preserves_all_service_records_and_clears_removed_tabs(self):
        self.store.install(package(manifest()))
        with mock.patch.object(agent_console, "APPS", dict(agent_console.BUILTIN_APPS)):
            agent_console.load_package_apps(self.store)
            self.assertEqual(agent_console.APPS["preview"]["tab"]["parent"], "demo")
            self.assertEqual(agent_console.APPS["preview"]["tunnel"], "preview")
            self.store.remove("tab-demo")
            agent_console.load_package_apps(self.store)
            self.assertNotIn("preview", agent_console.APPS)

    def test_real_http_apps_list_and_child_sessions(self):
        self.store.install(package(manifest()))
        console = Path(self.tmp.name) / "console"
        with mock.patch.dict(os.environ, {"HPCLIB_CONSOLE_DIR": str(console),
                                          "HPCLIB_AGENTS_DIR": str(Path(self.tmp.name) / "agents")}), \
             mock.patch.object(agent_console, "package_store", return_value=self.store), \
             mock.patch.object(agent_console, "APPS", dict(agent_console.BUILTIN_APPS)), \
             mock.patch.object(agent_console.ConsoleHandler, "log_message", lambda *args: None):
            server = agent_console.ConsoleServer(("127.0.0.1", 0), "test-key", agent_console.Clusters())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def get(endpoint):
                    request = urllib.request.Request("http://127.0.0.1:" + str(server.port) + "/api/" + endpoint,
                                                     headers={"Authorization": "Bearer test-key"})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return json.load(response)
                listed = {app["id"]: app for app in get("apps")["apps"]}
                self.assertEqual(listed["preview"]["tab"], manifest()["apps"]["preview"]["tab"])
                self.assertEqual(get("apps/preview")["title"], "Preview service")
                self.assertEqual(get("apps/preview")["sessions"], [])
            finally:
                server.shutdown()
                thread.join(5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
