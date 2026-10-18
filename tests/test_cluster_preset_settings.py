"""Portable preset defaults reach native setup without personal credentials or stale app state."""
import argparse
import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_console_packages as packages_tests
from test_agent_console import ConsoleTestCase
import agent_profiles
import console_packages
import rest_jobs
import rest_server

PORTABLE = {"format": 1, "name": "entropy-cluster", "version": "1.0.0", "clusters": {"entropy": {
    "title": "Entropy", "host": "entropy.chem.tamu.edu", "defaults": {
        "work_dir": "/scratch/user/{username}/llm",
        "app_settings": {"jupyter": {"conda_env": "default", "modules": [], "project": ""},
                         "transfer": {"settings": {"SMB_USER": "{username}", "SMB_DOMAIN": "AUTH"}}},
        "rest_config": {"limits": {"max_concurrent_jobs": 8, "max_time": "1-00:00:00", "max_mem": "64G"},
                        "sandbox": {"method": "auto", "binds": ["/software"]},
                        "cluster_notes": "Installed settings"}}}}}


class TestPortablePresetValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def store(self):
        return console_packages.PackageStore(tempfile.mkdtemp(dir=self.tmp.name),
            {"jupyter": ("jupyter", []), "transfer": ("data-transfer", ["SMB_USER", "SMB_DOMAIN"])}, {"rest"})

    def test_native_validation_rejects_invalid_or_personal_server_fields(self):
        invalid = [{"limits": {"max_concurrent_jobs": True}}, {"limits": {"unknown": 8}},
                   {"sandbox": {"method": "invalid"}}, {"tokens_file": "/home/other/tokens.json"},
                   {"jobs_db": "/home/other/jobs.sqlite"}, {"poll_interval": 0}]
        for config in invalid:
            with self.subTest(config=config):
                manifest = copy.deepcopy(PORTABLE)
                manifest["clusters"]["entropy"]["defaults"]["rest_config"] = config
                with self.assertRaises(console_packages.PackageError):
                    self.store().inspect(packages_tests.make_zip(manifest))
        for settings in ({"token": "secret"}, {"port": 20735}, {"tunnel_args": ["--wrap=evil"]}):
            manifest = copy.deepcopy(PORTABLE)
            manifest["clusters"]["entropy"]["defaults"]["app_settings"] = {"jupyter": settings}
            with self.assertRaises(console_packages.PackageError):
                self.store().inspect(packages_tests.make_zip(manifest))


class TestPortablePresetSetup(ConsoleTestCase):
    def create(self):
        self.clusters.package_add(packages_tests.make_zip(PORTABLE))
        status, out = self.call("POST", "/api/cluster-presets/entropy", {"username": "newuser"})
        self.assertEqual(status, 201, out)
        return out["name"]

    def test_profile_defaults_personalized_without_stale_ports_or_credentials(self):
        name = self.create()
        profile = agent_profiles.load(name)
        self.assertEqual(profile["work_dirs"], ["/scratch/user/newuser/llm"])
        self.assertEqual(profile["apps"]["transfer"]["settings"]["SMB_USER"], "newuser")
        self.assertNotIn("{username}", json.dumps(profile))
        self.assertFalse(self.shell.calls)
        self.assertEqual(self.call("GET", f"/api/clusters/{name}")[1]["setup_config"]["limits"]["max_concurrent_jobs"], 8)
        jupyter = self.call("GET", f"/api/apps/jupyter/{name}/settings")[1]
        self.assertEqual(jupyter["conda_env"], "default")
        self.assertNotEqual(jupyter["port"], profile["port"])
        self.clusters.package_remove("entropy-cluster")
        self.assertEqual(agent_profiles.load(name)["setup_config"]["limits"]["max_concurrent_jobs"], 8)

    def test_setup_passes_config_to_native_init_and_preserves_existing_remote_config(self):
        name = self.create()
        with mock.patch.object(self.clusters.logins, "alive", return_value=True):
            status, out = self.call("POST", f"/api/clusters/{name}/setup", {"work_dirs": ["/scratch/user/newuser/llm"]})
        self.assertEqual(status, 202, out)
        command = self.shell.calls[-1]
        path = Path(command[command.index("--config") + 1])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())["limits"]["max_concurrent_jobs"], 8)
        remote = self.tmp / "simulated-cluster" / "config.json"
        opts = argparse.Namespace(config=str(remote), config_base=str(path), rebuild=False,
                                  no_sandbox=False, sandbox_bind=[])
        with mock.patch.object(rest_server, "_has_slurm", return_value=True), \
                mock.patch.object(rest_server.rest_sandbox, "_module_roots", return_value=(None, [])), \
                contextlib.redirect_stdout(io.StringIO()):
            rest_server.init_config(opts)
            saved = json.loads(remote.read_text())
            self.assertEqual(saved["limits"]["max_concurrent_jobs"], 8)
            self.assertEqual(saved["sandbox"], {"method": "auto", "binds": ["/software"]})
            saved["limits"]["max_concurrent_jobs"] = 6
            remote.write_text(json.dumps(saved))
            rest_server.init_config(opts)
            self.assertEqual(json.loads(remote.read_text())["limits"]["max_concurrent_jobs"], 6)
        self.jobs.limits = rest_jobs.ResourceLimits(**json.loads(path.read_text())["limits"])
        for index in range(8):
            self.llm.submit_job("hello", idempotency_key=f"preset-limit-{index}")
        self.expect_error(429, lambda: self.llm.submit_job("hello", idempotency_key="preset-limit-ninth"))

    def test_invalid_app_defaults_leave_no_partial_profile(self):
        manifest = copy.deepcopy(PORTABLE)
        manifest["clusters"]["entropy"]["defaults"]["app_settings"]["transfer"]["settings"]["SMB_AUTH"] = "invalid"
        self.clusters.package_add(packages_tests.make_zip(manifest))
        before = self.clusters.names()
        status, _ = self.call("POST", "/api/cluster-presets/entropy", {"username": "newuser"})
        self.assertEqual(status, 422)
        self.assertEqual(self.clusters.names(), before)
