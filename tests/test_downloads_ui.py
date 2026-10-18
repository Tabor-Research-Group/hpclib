"""Browser test for an app's download toolbar (a package's `downloads` section: OpenCode's Model Download)."""
import json
import unittest

import test_agent_console_ui as console_ui

DOWNLOADS = {"label": "Model Download", "description": "Downloaded on the login node.",
             "kinds": [{"id": "ollama", "label": "Ollama", "placeholder": "granite4.2:30b", "pattern": ".*"},
                       {"id": "vllm", "label": "vLLM (Hugging Face)", "placeholder": "org/model", "pattern": ".*"}]}


@unittest.skipUnless(console_ui.sync_playwright, "optional Playwright dependency is not installed")
class TestDownloadsUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        console_ui.TestConsoleUI.setUpClass.__func__(cls)

    def setUp(self):
        console_ui.TestConsoleUI.setUp(self)
        self.posts = []
        self.items = [{"kind": "ollama", "name": "granite4.2:30b", "size": 19_500_000_000, "in_use": True, "note": ""},
                      {"kind": "vllm", "name": "ibm-granite/granite-4.2-30b", "size": 61_000_000_000, "in_use": False,
                       "note": ""}]
        self.operation = None
        self.session = {"app": "opencode", "cluster": "demo", "host": "me@login", "port": 8960, "state": "down",
                        "install": {"state": "installed", "message": "installed: opencode"}, "shared": False,
                        "kind": "tunnel", "tools": [], "controls": [], "downloads": DOWNLOADS,
                        "login": {"state": "connected"}}

    def fixture(self, route, path):
        verb = route.request.method
        if path == "apps":
            result = {"apps": [{"id": "agents", "title": "Agents"},
                               {"id": "opencode", "title": "OpenCode", "kind": "tunnel", "description": "OpenCode"}]}
        elif path == "apps/opencode":
            result = {"title": "OpenCode", "sessions": [dict(self.session, operation=self.operation)]}
        elif path == "apps/opencode/demo":
            result = dict(self.session, operation=self.operation)
        elif path == "apps/opencode/demo/downloads" and verb == "GET":
            result = {"items": self.items, "note": "4.0 TB free in /scratch/user/me/opencode"}
        elif path == "apps/opencode/demo/downloads" and verb == "POST":
            self.posts.append(("download", route.request.post_data_json))
            self.operation = {"kind": "download", "title": "download x", "app": "opencode", "state": "running",
                              "started": 1.0}
            result = self.operation
        elif path == "apps/opencode/demo/downloads/remove":
            body = route.request.post_data_json
            self.posts.append(("remove", body))
            self.items = [i for i in self.items if i["name"] != body["name"]]
            result = {"removed": body}
        elif path == "clusters/demo/operation":
            result = dict(self.operation or {"state": "none"}, log=["pulling manifest", "pulling 4c3a: 42%"])
        else:
            return False
        route.fulfill(body=json.dumps(result), content_type="application/json")
        return True

    def test_download_and_manage(self):
        console_ui.TestConsoleUI.load_console(self, self.fixture)
        self.page.goto(self.base + "/#/opencode/sessions")
        tool = self.page.locator("hpclib-toolbar-tool.download-tool")
        tool.wait_for()
        self.assertEqual(tool.locator(".tool-label").inner_text().strip().lower(), "model download")
        name = tool.get_by_label("Model Download: name")
        self.assertEqual(name.get_attribute("placeholder"), "e.g. granite4.2:30b")
        tool.get_by_label("Kind").select_option("vllm")
        self.assertEqual(name.get_attribute("placeholder"), "e.g. org/model")
        # Download: the request, then its log under the row, as for Install
        name.fill("ibm-granite/granite-4.0-h-small")
        name.press("Enter")
        self.page.locator("hpc-panel.log-panel").wait_for()
        self.assertEqual(self.posts, [("download", {"kind": "vllm", "name": "ibm-granite/granite-4.0-h-small"})])
        self.page.wait_for_function("document.querySelector('hpc-panel.log-panel hpc-log') && "
                                    "document.querySelector('hpc-panel.log-panel').textContent.includes('42%')")
        # while it runs, no second download
        self.page.wait_for_function("document.querySelector('.download-tool hpc-action-button[label=Download]').disabled")
        # what is there, with sizes; Remove asks first
        self.operation = dict(self.operation, state="succeeded")
        tool.locator('hpc-action-button[label="Downloaded…"]').click()
        panel = self.page.locator("hpc-panel.downloads-panel")
        panel.locator("table.downloads").wait_for()
        text = panel.inner_text()
        for words in ("granite4.2:30b", "19.5 GB", "ibm-granite/granite-4.2-30b", "61 GB", "in use", "4.0 TB free"):
            self.assertIn(words, text)
        panel.locator("tbody tr").nth(1).locator('hpc-action-button[label="Remove"]').click()
        dialog = self.page.locator("hpc-dialog")
        dialog.get_by_role("button", name="Remove").click()
        self.page.wait_for_function("document.querySelectorAll('table.downloads tbody tr').length === 1")
        self.assertEqual(self.posts[-1], ("remove", {"kind": "vllm", "name": "ibm-granite/granite-4.2-30b"}))


if __name__ == "__main__":
    unittest.main()
