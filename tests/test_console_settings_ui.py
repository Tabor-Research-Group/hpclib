"""Browser regressions for the console menu's pages: the theme switch, Interface Settings, Console Settings,
About and notifications. The preferences go to the real console (a temporary directory); the rest are fixtures."""
import json
import time
import unittest

import test_agent_console_ui as console_ui

# Notifications the page shows, recorded instead of shown; the browser's permission is "granted".
NOTIFICATIONS = """
window.shown = [];
window.Notification = class { constructor(title, opts) { window.shown.push({ title, body: (opts || {}).body }); }
  static get permission() { return "granted"; } static requestPermission() { return Promise.resolve("granted"); } };
"""


@unittest.skipUnless(console_ui.sync_playwright, "optional Playwright dependency is not installed")
class TestConsoleSettingsUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        console_ui.TestConsoleUI.setUpClass.__func__(cls)

    def setUp(self):
        console_ui.TestConsoleUI.setUp(self)
        (self.console / "preferences.json").unlink(missing_ok=True)
        self.calls = []
        self.tunnels = [{"cluster": "demo", "app": None, "title": "agent tunnel", "up": True, "started_here": False,
                         "stopped": None}]
        self.jobs = [{"cluster": "demo", "job_id": "7", "label": "xtb opt", "state": "RUNNING", "terminal": False,
                      "token": "llm"}]
        self.clients = [{"id": "cursor", "title": "Cursor", "path": "/home/me/.cursor/mcp.json", "available": True,
                         "enabled": False, "after": "", "error": None, "stale": [],
                         "entries": [{"name": "hpclib-demo", "state": "missing"}]}]

    def handler(self, route, path):
        self.calls.append((route.request.method, path, route.request.post_data_json if route.request.post_data else None))
        if path in ("preferences", "styles/open"):
            route.continue_()            # the real console: preferences.json in a temporary directory
            return True
        answers = {
            "clusters": {"clusters": []}, "health": {"hpclib_version": "0.2.79"},
            "watch": {"at": time.time(), "tunnels": self.tunnels},
            "jobs": {"jobs": self.jobs, "clusters": {"demo": {"ok": True}}},
            "ports": {"range": [20000, 32000], "default_range": [20000, 32000], "ports": [
                {"cluster": "demo", "app": None, "title": "agent tunnel", "which": "port", "port": 20735, "status": "taken"},
                {"cluster": "demo", "app": None, "title": "agent tunnel", "which": "process_port", "port": 20736, "status": "free"}]},
            "clusters/demo/new-ports": {"cluster": "demo", "app": None, "port": 21000, "process_port": 21001, "mcp_updated": False},
            "llm-clients": {"clients": self.clients},
            "llm-clients/cursor/write": {"changed": ["hpclib-demo"], "client": {}},
            "about": {"hpclib_version": "0.2.79", "hpclib_version_on_disk": "0.2.80", "restart_for": "0.2.80",
                      "console_version": "0.1", "hpclib_dir": "/opt/hpclib/hpclib", "git": None, "python": "3.12.1",
                      "python_path": "/usr/bin/python3", "platform": "Linux", "console_dir": "/c", "agents_dir": "/a",
                      "pid": 1, "started": time.time(), "clusters": 1, "packages": [], "url": self.base, "static": True},
            "diagnostics": {"about": {"hpclib_version": "0.2.79"}, "clusters": []},
        }
        if path in answers:
            route.fulfill(body=json.dumps(answers[path]), content_type="application/json")
            return True
        return False

    def open(self, hash_, notifications=False):
        if notifications:
            self.page.add_init_script(NOTIFICATIONS)
        console_ui.TestConsoleUI.load_console(self, self.handler)
        self.page.goto(self.base + "/" + hash_)

    def test_menu_theme_switch_and_links(self):
        self.open("#/c")
        self.page.locator("#console-menu > summary").click()
        switch = self.page.get_by_role("switch", name="Dark mode")
        self.assertEqual(switch.get_attribute("aria-checked"), "false")      # the browser's scheme is light
        switch.click()
        self.assertEqual(self.page.evaluate("document.documentElement.dataset.theme"), "dark")
        self.assertEqual(self.page.evaluate("getComputedStyle(document.body).backgroundColor"), "rgb(24, 24, 26)")
        self.page.reload()
        self.page.wait_for_function("document.querySelector('#apps a') !== null")
        self.assertEqual(self.page.evaluate("document.documentElement.dataset.theme"), "dark")   # kept, before any paint
        self.page.locator("#console-menu > summary").click()
        self.page.get_by_role("button", name="Match system", exact=True).click()
        self.assertIsNone(self.page.evaluate("document.documentElement.dataset.theme || null"))
        self.assertEqual(self.page.locator("#console-menu-items a").all_text_contents(),
                         ["Interface Settings", "Console Settings", "About"])
        self.page.get_by_role("link", name="Interface Settings", exact=True).click()
        self.assertFalse(self.page.locator("#console-menu").evaluate("e => e.open"))
        self.page.get_by_role("heading", name="The complete stylesheet").wait_for()
        self.assertEqual(self.page.locator("#app-title").inner_text(), "Interface Settings")
        self.assertEqual(self.errors, [])

    def test_interface_settings_preview_save_and_reset(self):
        self.open("#/interface")
        light = self.page.get_by_label("--accent (light)", exact=True)
        light.wait_for()
        self.assertEqual(light.get_attribute("placeholder"), "#2f6fdb")
        self.assertEqual(self.page.get_by_label("--accent (dark)", exact=True).get_attribute("placeholder"), "#6ea0ff")
        light.fill("#7b3fbf")
        accent = "getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()"
        self.assertEqual(self.page.evaluate(accent), "#7b3fbf")                    # previewed
        self.page.get_by_label("--radius-control", exact=True).fill("2px")
        with self.page.expect_response(lambda r: r.url.endswith("/api/preferences") and r.request.method == "PUT"):
            self.page.get_by_role("button", name="Save", exact=True).click()
        saved = json.loads((self.console / "preferences.json").read_text())["interface"]
        self.assertEqual(saved, {"common": {"--radius-control": "2px"}, "light": {"--accent": "#7b3fbf"}, "dark": {}})
        self.page.reload()
        self.page.get_by_label("--accent (light)", exact=True).wait_for()
        self.assertEqual(self.page.evaluate(accent), "#7b3fbf")                    # from /interface.css
        self.assertEqual(self.page.get_by_label("--accent (light)", exact=True).input_value(), "#7b3fbf")
        self.page.evaluate("document.documentElement.dataset.theme = 'dark'")
        self.assertEqual(self.page.evaluate(accent), "#6ea0ff")                    # the dark default still applies
        self.page.get_by_role("radio", name="Light", exact=True).click()           # the page's own theme choice
        self.assertEqual(self.page.evaluate("document.documentElement.dataset.theme"), "light")
        self.assertEqual(self.page.get_by_role("link", name="View the default stylesheet").get_attribute("href"),
                         "default-styles.css")
        self.assertEqual(self.errors, [])

    def test_console_settings_save_ports_and_clients(self):
        self.open("#/console")
        self.page.get_by_role("heading", name="LLM clients").wait_for()
        self.page.get_by_label("An agent's job finishes", exact=False).check()
        notes = self.page.locator("#notifications")
        with self.page.expect_response(lambda r: r.url.endswith("/api/preferences") and r.request.method == "PUT"):
            notes.get_by_role("button", name="Save", exact=True).click()
        self.assertTrue(json.loads((self.console / "preferences.json").read_text())["notifications"]["job"])
        ports = self.page.locator("#ports")
        ports.get_by_text("taken by another program").wait_for()
        ports.get_by_role("button", name="New ports", exact=True).click()
        self.page.get_by_text("agent tunnel on demo: now 21000 and 21001.", exact=False).wait_for()
        self.assertIn(("POST", "clusters/demo/new-ports", {"app": None}), self.calls)
        clients = self.page.locator("#llm-clients")
        clients.get_by_text("1 missing").wait_for()
        clients.get_by_role("button", name="Write entries", exact=True).click()
        self.page.get_by_text("Cursor: wrote hpclib-demo.", exact=False).wait_for()
        defaults = self.page.locator("#new-cluster")
        defaults.locator('hpc-field[label="Work directory"] input').fill("/scratch/user/{user}/llm")
        with self.page.expect_response(lambda r: r.url.endswith("/api/preferences") and r.request.method == "PUT"):
            defaults.get_by_role("button", name="Save", exact=True).click()
        prefs = json.loads((self.console / "preferences.json").read_text())
        self.assertEqual(prefs["new_cluster"]["work_dir_pattern"], "/scratch/user/{user}/llm")
        # the Add cluster form suggests it, with the login's user
        self.page.goto(self.base + "/#/c")
        self.page.get_by_role("button", name="Add cluster", exact=True).click()
        self.page.get_by_label("Cluster configuration", exact=True).wait_for()
        card = self.page.locator("section.card").filter(has_text="Add a cluster")
        card.locator('hpc-field[label="Login"] input').fill("ana@grace.example.edu")
        self.assertEqual(card.locator('hpc-field[label="Work directory for agents"] input').input_value(),
                         "/scratch/user/ana/llm")
        self.assertEqual(self.errors, [])

    def test_cluster_mcp_name_is_renamed_on_its_agents_tab(self):
        demo = {"name": "demo", "host": "me@demo.example", "mcp_name": "hpclib-demo", "set_up": True, "port": 20735,
                "has_owner_token": True, "login": {"state": "connected"}, "tunnel": {"state": "down"}}
        renamed = []

        def handler(route, path):
            if path == "clusters/demo/mcp-name":
                renamed.append(route.request.post_data_json)
                route.fulfill(body=json.dumps({"mcp_name": renamed[-1]["name"], "previous": "hpclib-demo",
                                               "llm_clients": {"chatgpt": ["grace-gpu"]}}), content_type="application/json")
                return True
            if path in ("clusters", "clusters/demo"):
                route.fulfill(body=json.dumps({"clusters": [demo]} if path == "clusters" else demo),
                              content_type="application/json")
                return True
            return self.handler(route, path)
        console_ui.TestConsoleUI.load_console(self, handler)
        self.page.goto(self.base + "/#/c/demo/agents")
        name = self.page.get_by_label("MCP server name", exact=True)
        self.assertEqual(name.input_value(), "hpclib-demo")
        name.fill("grace-gpu")
        self.page.get_by_role("button", name="Rename", exact=True).click()
        self.page.get_by_text("Now grace-gpu. Updated in 1 LLM client.", exact=False).wait_for()
        self.assertEqual(renamed, [{"name": "grace-gpu"}])
        self.assertEqual(self.errors, [])

    def test_tunnel_actions_are_turned_on_and_put_needs_its_own_yes(self):
        demo = {"name": "demo", "host": "me@demo.example", "mcp_name": "hpclib-demo", "set_up": True, "port": 20735,
                "has_owner_token": True, "login": {"state": "connected"}, "tunnel": {"state": "up"}}
        config = {"tunnel_actions": {}}
        puts = []

        def actions():
            entry = config["tunnel_actions"].get("data-transfer", {})
            on = entry.get("enabled", False)
            chosen = entry.get("actions", {})
            acts = [{"name": n, "title": t, "description": d, "mode": m, "scope": "submit", "writes": w, "default": dflt,
                     "enabled": on and chosen.get(n, dflt), "permitted": True,
                     "params": {"remote": {"type": "remote-path"}}}
                    for n, t, d, m, w, dflt in (("list", "List a folder", "l", "sync", None, True),
                                                ("get", "Copy from the SMB server", "g", "job", "cluster", True),
                                                ("put", "Copy to the SMB server", "p", "job", "remote", False))]
            t = {"tunnel": "data-transfer", "title": "SMB server", "description": "files", "enabled": on, "actions": acts}
            if on:
                t["availability"] = {"available": True, "detail": {"host": "files.example.edu"}}
            return {"tunnels": [t]}

        def handler(route, path):
            if path == "clusters/demo/rest/admin/config":
                if route.request.method == "PUT":
                    puts.append(route.request.post_data_json["changes"]["tunnel_actions"])
                    config["tunnel_actions"] = puts[-1]
                route.fulfill(body=json.dumps({"config": config}), content_type="application/json")
                return True
            if path == "clusters/demo/rest/actions":
                route.fulfill(body=json.dumps(actions()), content_type="application/json")
                return True
            if path in ("clusters", "clusters/demo"):
                route.fulfill(body=json.dumps({"clusters": [demo]} if path == "clusters" else demo),
                              content_type="application/json")
                return True
            return self.handler(route, path)
        console_ui.TestConsoleUI.load_console(self, handler)
        self.page.goto(self.base + "/#/c/demo/agents")
        card = self.page.locator("#tunnel-actions")
        put = card.get_by_label("Turn on data-transfer/put", exact=True)
        put.wait_for()
        self.assertTrue(put.is_disabled())                        # the tunnel is off
        card.get_by_label("Turn on SMB server actions", exact=True).check()
        card.get_by_text("available", exact=True).wait_for()
        self.assertEqual(puts[-1], {"data-transfer": {"enabled": True}})
        self.assertTrue(card.get_by_label("Turn on data-transfer/get", exact=True).is_checked())
        self.assertFalse(card.get_by_label("Turn on data-transfer/put", exact=True).is_checked())
        # writing outside the cluster asks first: Cancel leaves it off
        card.get_by_label("Turn on data-transfer/put", exact=True).click()
        dialog = self.page.locator("hpc-dialog[heading^='Let agents copy to the smb server']")
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        self.page.wait_for_function("!document.querySelector('[aria-label=\"Turn on data-transfer/put\"]').checked")
        self.assertEqual(len(puts), 1)
        self.assertFalse(card.get_by_label("Turn on data-transfer/put", exact=True).is_checked())
        card.get_by_label("Turn on data-transfer/put", exact=True).click()
        dialog.get_by_role("button", name="Turn on", exact=True).click()
        card.get_by_text("data-transfer/put: on.").wait_for()
        self.assertEqual(puts[-1], {"data-transfer": {"enabled": True, "actions": {"put": True}}})
        self.assertTrue(card.get_by_label("Turn on data-transfer/put", exact=True).is_checked())
        self.assertEqual(self.errors, [])

    def test_clusters_page_without_widgets_and_its_add_form(self):
        self.open("#/c")
        self.page.get_by_text("No clusters yet", exact=False).wait_for()
        self.assertNotIn("null", self.page.locator("main").inner_text())        # no widget strip: nothing, not "null"
        self.page.get_by_role("button", name="Add cluster", exact=True).click()
        form = self.page.locator(".add-cluster")
        form.get_by_label("Cluster configuration", exact=True).wait_for()
        # the form is under the toolbar, as wide as the page, not beside the heading
        toolbar = self.page.locator(".toolbar").first.bounding_box()
        box = form.bounding_box()
        self.assertGreaterEqual(box["y"], toolbar["y"] + toolbar["height"] - 1)
        self.assertLess(box["x"], toolbar["x"] + 50)
        self.assertEqual(self.errors, [])

    def test_client_paths_are_changed_in_place(self):
        self.open("#/console")
        clients = self.page.locator("#llm-clients")
        clients.get_by_text("/home/me/.cursor/mcp.json").wait_for()
        clients.get_by_role("button", name="Change", exact=True).click()
        self.assertNotIn("null", clients.inner_text())
        clients.get_by_label("Cursor configuration file").fill("/opt/cursor/mcp.json")
        with self.page.expect_response(lambda r: r.url.endswith("/api/preferences") and r.request.method == "PUT"):
            clients.get_by_role("button", name="Save", exact=True).click()
        prefs = json.loads((self.console / "preferences.json").read_text())
        self.assertEqual(prefs["llm_client_paths"]["cursor"], "/opt/cursor/mcp.json")
        self.assertEqual(self.errors, [])

    def test_about_copies_the_link(self):
        self.page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        self.open("#/about")
        self.page.get_by_text("Restart to update:", exact=False).wait_for()
        self.page.get_by_role("button", name="Copy link", exact=True).click()
        self.page.get_by_text("Copied.").first.wait_for()
        self.assertEqual(self.page.evaluate("navigator.clipboard.readText()"), self.base + "/#key=ui-test-key")
        self.page.get_by_role("button", name="Copy diagnostics", exact=True).click()
        self.page.wait_for_function("navigator.clipboard.readText().then(t => t.includes('hpclib_version'))")
        self.assertEqual(self.errors, [])

    def test_notifications_for_a_dropped_tunnel_and_a_finished_job(self):
        (self.console / "preferences.json").write_text(json.dumps({"notifications": {"tunnel": True, "job": True}}))
        self.page.clock.install()
        self.open("#/c", notifications=True)
        self.page.wait_for_function("window.Notification && document.querySelector('#apps a') !== null")
        self.page.clock.run_for(2000)
        self.tunnels[0]["up"] = False
        self.jobs[0].update(state="COMPLETED", terminal=True)
        self.page.clock.run_for(61000)
        self.page.wait_for_function("window.shown.length >= 2")
        titles = [n["title"] for n in self.page.evaluate("window.shown")]
        self.assertIn("agent tunnel on demo stopped", titles)
        self.assertIn("xtb opt finished", titles)
        # a tunnel stopped from the page is not reported
        self.tunnels[0].update(up=True)
        self.page.clock.run_for(21000)
        self.tunnels[0].update(up=False, stopped=time.time() + 100)
        self.page.clock.run_for(21000)
        self.page.wait_for_timeout(300)
        self.assertEqual(sum(t == "agent tunnel on demo stopped" for t in
                             (n["title"] for n in self.page.evaluate("window.shown"))), 1)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
