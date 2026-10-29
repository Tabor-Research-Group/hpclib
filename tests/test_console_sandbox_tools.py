import json
from unittest import mock
from test_agent_console import ConsoleTestCase
import agent_console


class TestConsoleSandboxTools(ConsoleTestCase):
    def setUp(self):
        super().setUp()
        self.clusters.logins.alive = lambda profile: True
        self.ran = []
        def runner(profile, script, args=(), wait=60):
            self.ran.append((script, args, wait))
            return 0, 'HPCLIB_SANDBOX_TOOLS ' + json.dumps({"ok": True, "tools": {}, "root": "/home/me/.local/share/hpclib/sandbox-tools"}), ''
        self.clusters.script_runner = runner

    def tools_url(self, suffix=""):
        return f"/api/clusters/{self.dead}/sandbox-tools{suffix}"

    def test_report_and_install_without_rest_tunnel(self):
        status, answer = self.call("GET", self.tools_url())
        self.assertEqual(status, 200, answer)
        status, answer = self.call("POST", self.tools_url("/install"), {"tool": "charliecloud", "modules": ["GCC/13.3", "WebProxy"]})
        self.assertEqual(status, 200, answer)
        self.assertTrue(answer["install"]["ok"])
        script, args, timeout = self.ran[-1]
        self.assertIn("module load GCC/13.3 WebProxy", script)
        self.assertIn("python3 - --install charliecloud", script)
        self.assertEqual(timeout, 900)
        self.assertNotIn("config.json", script)

    def test_validate_tool_modules_and_login(self):
        for request in ({}, {"tool": "x;touch /tmp/bad"}, {"tool": "bubblewrap", "modules": ["GCC;echo bad"]}):
            status, _ = self.call("POST", self.tools_url("/install"), request)
            self.assertEqual(status, 400)
        self.assertEqual(self.ran, [])
        self.clusters.logins.alive = lambda profile: False
        status, _ = self.call("GET", self.tools_url())
        self.assertEqual(status, 409)

    def test_build_error_is_reported_as_install_failure(self):
        self.clusters.script_runner = lambda *args: (1, 'HPCLIB_SANDBOX_TOOLS {"ok":false,"error":"missing cc"}', '')
        status, answer = self.call("POST", self.tools_url("/install"), {"tool": "bubblewrap"})
        self.assertEqual(status, 200)
        self.assertFalse(answer["install"]["ok"])
        self.assertEqual(answer["install"]["error"], "missing cc")
