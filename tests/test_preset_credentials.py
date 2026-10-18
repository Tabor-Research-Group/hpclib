"""Credential defaults must be rejected before plaintext package/profile persistence."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_console_packages as packages_tests
from test_agent_console import ConsoleTestCase
from test_cluster_preset_settings import PORTABLE
import agent_console
import agent_profiles
import console_packages

SENTINEL = "synthetic-password-for-this-regression-only"


class TestCredentialPresetValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = console_packages.PackageStore(self.tmp.name,
            {"jupyter": ("jupyter", []), "transfer": ("data-transfer", ["SMB_USER", "SMB_DOMAIN", "SMB_AUTH", "SMB_HOST"])},
            {"rest", "jupyter", "data-transfer"})

    def rejected(self, manifest):
        data = packages_tests.make_zip(manifest)
        for operation in (self.store.inspect, self.store.install):
            with self.assertRaises(console_packages.PackageError) as error:
                operation(data)
            self.assertNotIn(SENTINEL, str(error.exception))
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_direct_password_and_nested_app_credentials_are_refused(self):
        for key in ("password", "secret", "token"):
            manifest = copy.deepcopy(PORTABLE)
            manifest["clusters"]["entropy"][key] = SENTINEL
            self.rejected(manifest)
        for key in ("DB_PASSWORD", "SMB_PASS", "SMB_PASSWD", "DB_PWD", "PASSPHRASE", "API_TOKEN", "CLIENT_SECRET", "PRIVATE_KEY", "AUTHORIZATION"):
            manifest = copy.deepcopy(PORTABLE)
            manifest["clusters"]["entropy"]["defaults"]["app_settings"]["transfer"]["settings"][key] = SENTINEL
            self.rejected(manifest)

    def test_rest_environment_credentials_are_refused(self):
        for scope in ("all", "jobs", "syncs"):
            for key in ("DB_PASSWORD", "clientSecret", "API_KEY", "AWS_SECRET_ACCESS_KEY"):
                manifest = copy.deepcopy(PORTABLE)
                manifest["clusters"]["entropy"]["defaults"]["rest_config"]["environment"] = {scope: {key: SENTINEL}}
                self.rejected(manifest)

    def test_embedded_credentials_and_private_key_material_are_refused(self):
        for value in ("https://user:" + SENTINEL + "@example.edu", "https://example.edu?password=" + SENTINEL,
                      "-----BEGIN OPENSSH PRIVATE KEY-----" + SENTINEL, "Bearer " + SENTINEL,
                      "Basic dTpw", json.dumps({"password": SENTINEL}), json.dumps(json.dumps({"password": SENTINEL})),
                      "[" * 33 + json.dumps({"password": SENTINEL}) + "]" * 33,
                      "[" * 1100 + json.dumps({"password": SENTINEL}) + "]" * 1100):
            manifest = copy.deepcopy(PORTABLE)
            manifest["clusters"]["entropy"]["defaults"]["app_settings"]["transfer"]["settings"]["SMB_HOST"] = value
            self.rejected(manifest)

    def test_undeclared_and_uninstalled_app_values_are_refused(self):
        manifest = copy.deepcopy(PORTABLE)
        manifest["clusters"]["entropy"]["defaults"]["app_settings"]["unknown"] = {"settings": {"VALUE": SENTINEL}}
        self.rejected(manifest)
        manifest = copy.deepcopy(PORTABLE)
        manifest["clusters"]["entropy"]["defaults"]["app_settings"]["transfer"]["settings"]["VALUE"] = SENTINEL
        self.rejected(manifest)

    def test_custom_declared_secret_is_refused_even_without_a_secret_like_name(self):
        manifest = copy.deepcopy(PORTABLE)
        manifest.update(apps=copy.deepcopy(packages_tests.DEMO["apps"]), tunnels=copy.deepcopy(packages_tests.DEMO["tunnels"]))
        manifest["apps"]["demo"]["secrets"] = [{"name": "SESSION_VALUE", "label": "Sign-in value"}]
        manifest["clusters"]["entropy"]["defaults"]["app_settings"]["demo"] = {"settings": {"SESSION_VALUE": SENTINEL}}
        for operation in (self.store.inspect, self.store.install):
            with self.assertRaises(console_packages.PackageError) as error:
                operation(packages_tests.make_zip(manifest, packages_tests.DEMO_FILES))
            self.assertNotIn(SENTINEL, str(error.exception))
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_password_authentication_mode_is_not_a_password(self):
        manifest = copy.deepcopy(PORTABLE)
        manifest["clusters"]["entropy"]["defaults"]["app_settings"]["transfer"]["settings"]["SMB_AUTH"] = "password"
        report = self.store.install(packages_tests.make_zip(manifest))
        self.assertEqual(report["problems"], [])
        settings = self.store.cluster_presets()["entropy"]["defaults"]["app_settings"]["transfer"]["settings"]
        self.assertEqual(settings["SMB_AUTH"], "password")


class TestCredentialPresetAPI(ConsoleTestCase):
    def test_legacy_cached_credential_default_is_not_exposed_or_copied(self):
        self.clusters.package_add(packages_tests.make_zip(PORTABLE))
        path = Path(self.clusters.packages._dir("entropy-cluster")) / "package.json"
        manifest = json.loads(path.read_text())
        manifest["clusters"]["entropy"]["defaults"]["app_settings"]["transfer"]["settings"]["DB_PASSWORD"] = SENTINEL
        path.write_text(json.dumps(manifest))  # synthetic old cache, within this test's temporary home
        before = self.clusters.names()
        for verb, route, body in (("GET", "/api/cluster-presets", None),
                                  ("POST", "/api/cluster-presets/entropy", {"username": "newuser"})):
            status, out = self.call(verb, route, body)
            self.assertEqual(status, 422, out)
            self.assertNotIn(SENTINEL, json.dumps(out))
        self.assertEqual(self.clusters.names(), before)
        self.assertFalse(self.shell.calls)

    def test_direct_profile_password_is_refused(self):
        self.clusters.package_add(packages_tests.make_zip(PORTABLE))
        before = self.clusters.names()
        status, out = self.call("POST", "/api/cluster-presets/entropy", {"username": "newuser", "password": SENTINEL})
        self.assertEqual(status, 400)
        self.assertNotIn(SENTINEL, json.dumps(out))
        self.assertEqual(self.clusters.names(), before)

    def test_setup_rejects_credential_environment_before_creating_config_file(self):
        profile = agent_profiles.load(self.dead)
        profile["setup_config"] = {"environment": {"jobs": {"DB_PASSWORD": SENTINEL}}}
        agent_profiles.save(profile)  # synthetic manual modification in the temporary test profile
        status, out = self.call("POST", f"/api/clusters/{self.dead}/setup", {})
        self.assertEqual(status, 422)
        self.assertNotIn(SENTINEL, json.dumps(out))
        self.assertFalse((Path(agent_profiles.profile_dir(self.dead)) / "setup-config.json").exists())
        self.assertFalse(self.shell.calls)

    def test_manual_app_settings_cannot_persist_a_password_declared_as_a_setting(self):
        spec = copy.deepcopy(agent_console.APPS["transfer"])
        spec["settings"].append(("DB_PASSWORD", "Password", ""))
        before = Path(agent_profiles.profile_path(self.dead)).read_bytes()
        with mock.patch.dict(agent_console.APPS, {"transfer": spec}):
            status, out = self.call("PUT", f"/api/apps/transfer/{self.dead}/settings", {"settings": {"DB_PASSWORD": SENTINEL}})
        self.assertEqual(status, 422)
        self.assertNotIn(SENTINEL, json.dumps(out))
        self.assertEqual(Path(agent_profiles.profile_path(self.dead)).read_bytes(), before)
        self.assertFalse(self.shell.calls)
