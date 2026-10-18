"""Native package cluster presets use isolated profiles and fake SSH, never TACC."""
import copy
import json
import os
import unittest
from pathlib import Path
from unittest import mock
import test_console_packages as packages_tests
from test_agent_console import ConsoleTestCase
import agent_profiles
import console_packages

PRESET = {"format": 1, "name": "lonestar-cluster", "version": "1.0.0", "clusters": {"lonestar": {
    "title": "Lonestar6 (TACC)", "host": "ls6.tacc.utexas.edu", "port": 22, "ssh_keys": "site-managed",
    "documentation_url": "https://docs.tacc.utexas.edu/hpc/lonestar6/",
    "defaults": {"rest_on": "job", "tunnel_args": ["--partition=normal", "--time=01:00:00", "--mem=4G"]}}}}


class TestClusterPresetPackages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)

    def store(self):
        import tempfile
        return console_packages.PackageStore(tempfile.mkdtemp(dir=self.tmp.name), {}, {"rest"})

    def test_package_install_upgrade_conflict_and_removal(self):
        store = self.store()
        report = store.install(packages_tests.make_zip(PRESET))
        self.assertEqual(report["problems"], [])
        self.assertEqual(store.cluster_presets()["lonestar"]["host"], "ls6.tacc.utexas.edu")
        self.assertIn("cluster preset", console_packages.summary(report["package"])[0])
        other = dict(PRESET, name="other")
        self.assertTrue(store.inspect(packages_tests.make_zip(other))["problems"])
        updated = dict(PRESET, version="1.1.0")
        self.assertEqual(store.install(packages_tests.make_zip(updated))["replaces"], "1.0.0")
        store.remove("lonestar-cluster")
        self.assertEqual(store.cluster_presets(), {})

    def test_invalid_preset_data_is_refused(self):
        bad = [("host", "-oProxyCommand=evil"), ("host", "user@ls6.tacc.utexas.edu"), ("port", True),
               ("documentation_url", "javascript:alert(1)"), ("documentation_url", "https://user:secret@example.edu"),
               ("documentation_url", "https://[broken"), ("ssh_keys", "guess"),
               ("defaults", {"tunnel_args": ["--wrap=evil"]}), ("defaults", {"rest_on": "unknown"})]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                manifest = copy.deepcopy(PRESET); manifest["clusters"]["lonestar"][key] = value
                with self.assertRaises(console_packages.PackageError):
                    self.store().inspect(packages_tests.make_zip(manifest))


class TestClusterPresetAPI(ConsoleTestCase):
    def setUp(self):
        super().setUp()
        self.data_zip = packages_tests.make_zip(PRESET)

    def install(self):
        self.clusters.package_add(self.data_zip)

    def test_preset_create_preserves_local_choices_and_removal_keeps_profile(self):
        self.assertEqual(self.call("GET", "/api/cluster-presets", key=None)[0], 401)
        before = set(self.clusters.names())
        self.clusters.package_add(self.data_zip, dry_run=True)
        self.assertEqual(self.call("GET", "/api/cluster-presets")[1]["presets"], [])
        self.install()
        self.assertEqual(set(self.clusters.names()), before)
        presets = self.call("GET", "/api/cluster-presets")[1]["presets"]
        self.assertEqual(presets[0]["id"], "lonestar")
        body = {"username": "testuser", "work_dir": "/work/my agents", "account": "Project-123",
                "tunnel_args": ["--partition=normal", "--time=02:00:00"]}
        status, out = self.call("POST", "/api/cluster-presets/lonestar", body)
        self.assertEqual(status, 201, out)
        profile = agent_profiles.load(out["name"])
        self.assertEqual(profile["login"], ["testuser@ls6.tacc.utexas.edu"])
        self.assertEqual(profile["work_dirs"], ["/work/my agents"])
        self.assertEqual(profile["tunnel_args"], ["--partition=normal", "--time=02:00:00", "--account=Project-123"])
        self.assertEqual(profile["rest_on"], "job")
        self.assertEqual(out["preset"]["ssh_keys"], "site-managed")
        self.assertEqual(self.shell.calls, [])
        self.assertEqual(self.call("POST", "/api/cluster-presets/lonestar", body)[0], 409)
        self.clusters.package_remove("lonestar-cluster")
        self.assertEqual(agent_profiles.load(out["name"])["tunnel_args"], profile["tunnel_args"])

    def test_invalid_creation_leaves_no_partial_profile(self):
        self.install()
        before = set(self.clusters.names())
        for fields in ({"username": "x;evil"}, {"work_dir": "relative"}, {"work_dir": "/x\n/x"},
                       {"account": "a;evil"}, {"tunnel_args": ["--wrap=evil"]}, {"host": "override.example"}):
            body = {"username": "testuser", "work_dir": "/work/test"}; body.update(fields)
            self.assertIn(self.call("POST", "/api/cluster-presets/lonestar", body)[0], (400, 422))
        self.assertEqual(set(self.clusters.names()), before)

    def test_site_managed_keys_are_read_only_through_console(self):
        self.install()
        status, out = self.call("POST", "/api/cluster-presets/lonestar", {"username": "testuser", "work_dir": "/work/test"})
        name = out["name"]
        self.assertEqual(self.call("POST", f"/api/clusters/{name}/ssh/setup", {})[0], 409)
        for verb, resource in (("POST", "keys"), ("POST", "setup"), ("PUT", "authorized"), ("DELETE", "hosts")):
            self.assertEqual(self.call(verb, f"/api/clusters/{name}/rest/admin/ssh/{resource}", {})[0], 409)
        self.assertEqual(self.shell.calls, [])
