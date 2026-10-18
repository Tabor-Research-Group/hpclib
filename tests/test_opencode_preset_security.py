"""login_user/password_regex metadata stacks with public OpenCode presets and credential rejection."""
import copy
import json
from pathlib import Path

import test_agent_console as console_tests
import test_console_packages as package_tests
from test_agent_console import ConsoleTestCase
from test_cluster_preset_settings import PORTABLE
import agent_console
import agent_profiles
import console_packages

OPENCODE = {"format": 1, "name": "opencode-login-test", "version": "1.0.0",
    "apps": {"opencode": {"title": "OpenCode", "tunnel": "opencode-test",
        "password_regex": r"^opencode password: (\S+)\s*$", "login_user": "opencode",
        "settings": [{"name": name, "label": name} for name in
                     ("OPENCODE_ROOT_DIR", "OPENCODE_WORK_DIRS", "OPENCODE_MODELS", "OPENCODE_CONTEXT",
                      "OPENCODE_URL", "OPENCODE_MCP_SCOPES")],
        "secrets": [{"name": "OPENCODE_API_KEY", "label": "API key"}, {"name": "HF_TOKEN", "label": "HF token"}]}},
    "tunnels": {"opencode-test": "tunnels/opencode-test"}}
FILES = {"tunnels/opencode-test/sbatch_script.sh": "#!/bin/bash\ntrue\n"}


def preset():
    manifest = copy.deepcopy(PORTABLE)
    manifest["clusters"]["entropy"]["defaults"]["app_settings"] = {"opencode": {"settings": {
        "OPENCODE_ROOT_DIR": "/scratch/user/{username}/opencode",
        "OPENCODE_WORK_DIRS": "/scratch/user/{username}/opencode/projects",
        "OPENCODE_MODELS": "granite4.2:30b", "OPENCODE_CONTEXT": "65536",
        "OPENCODE_URL": "http://gpu07:8000/v1", "OPENCODE_MCP_SCOPES": "read,submit"}}}
    return manifest


class TestOpenCodePresetSecurity(ConsoleTestCase):
    def install_opencode(self):
        self.clusters.package_add(package_tests.make_zip(OPENCODE, FILES))

    def test_public_preset_preserves_opencode_login_metadata_and_configuration(self):
        self.install_opencode()
        self.clusters.package_add(package_tests.make_zip(preset()))
        status, out = self.call("POST", "/api/cluster-presets/entropy", {"username": "clusteruser"})
        self.assertEqual(status, 201, out)
        name = out["name"]
        self.assertEqual(agent_console.APPS["opencode"]["login_user"], "opencode")
        self.assertEqual(agent_console.APPS["opencode"]["password_re"].pattern, OPENCODE["apps"]["opencode"]["password_regex"])
        profile = agent_profiles.load(name)
        self.assertEqual(profile["apps"]["opencode"]["settings"]["OPENCODE_ROOT_DIR"], "/scratch/user/clusteruser/opencode")
        self.assertEqual(profile["setup_config"]["limits"]["max_concurrent_jobs"], 8)
        self.assertNotIn("password", profile["apps"]["opencode"])
        self.assertFalse(self.shell.calls)

        base = f"/api/apps/opencode/{name}"
        settings = self.call("GET", base + "/settings")[1]
        self.assertEqual(settings["settings"]["OPENCODE_MCP_SCOPES"], "read,submit")
        port = agent_profiles.load(name)["apps"]["opencode"]["port"]
        console_tests.TestApps.serve(self, port)  # local fixture, no OpenCode process or remote cluster
        logs = self.console / "logs"; logs.mkdir(parents=True, exist_ok=True)
        log = logs / f"{name}.opencode.log"
        log.write_text("\n=== launch ===\nstarting\n")
        state = self.call("GET", base)[1]
        self.assertEqual((state["state"], state["username"], state["password"]), ("up", "opencode", None))
        # Synthetic output exercises the exact login-user behavior. This is not a real credential.
        log.write_text("\n=== launch ===\nopencode password: test/not@real\n")
        state = self.call("GET", base)[1]
        self.assertEqual(state["url"], f"http://opencode:test%2Fnot%40real@127.0.0.1:{port}/")
        self.assertNotIn("test/not@real", Path(agent_profiles.profile_path(name)).read_text())

    def test_opencode_credential_fields_are_refused_as_preset_defaults(self):
        self.install_opencode()
        before = self.clusters.names()
        for field in ("OPENCODE_API_KEY", "HF_TOKEN", "OPENCODE_SERVER_PASSWORD"):
            manifest = preset()
            manifest["clusters"]["entropy"]["defaults"]["app_settings"]["opencode"]["settings"][field] = "synthetic-not-a-real-secret"
            with self.assertRaises(agent_console.ConsoleError) as error:
                self.clusters.package_add(package_tests.make_zip(manifest))
            self.assertEqual(error.exception.status, 422)
            self.assertNotIn("synthetic-not-a-real-secret", str(error.exception))
        self.assertEqual(self.clusters.names(), before)
        self.assertFalse((Path(self.clusters.packages.root) / "entropy-cluster").exists())

    def test_opencode_public_settings_work_and_credentials_cannot_enter_local_profile(self):
        self.install_opencode()
        base = f"/api/apps/opencode/{self.dead}/settings"
        status, out = self.call("PUT", base, {"settings": {"OPENCODE_MODELS": "granite4.2:30b", "OPENCODE_URL": "http://gpu07:8000/v1"}})
        self.assertEqual(status, 200, out)
        before = Path(agent_profiles.profile_path(self.dead)).read_bytes()
        for values in ({"OPENCODE_API_KEY": "synthetic-not-a-real-secret"},
                       {"OPENCODE_URL": "https://user:synthetic-not-a-real-secret@models.example/v1"}):
            status, out = self.call("PUT", base, {"settings": values})
            self.assertEqual(status, 422, out)
            self.assertNotIn("synthetic-not-a-real-secret", json.dumps(out))
        self.assertEqual(Path(agent_profiles.profile_path(self.dead)).read_bytes(), before)
