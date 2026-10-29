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
        if path in ("preferences", "styles/open", "styles"):
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
            "update": self.update_answer(route.request.method),
        }
        if path in answers:
            route.fulfill(body=json.dumps(answers[path]), content_type="application/json")
            return True
        return False

    def update_answer(self, method):
        base = {"repo": "/home/me/hpclib", "git": True, "fetched": time.time(), "fetch_error": None, "branch": "master",
                "upstream": "origin/master", "remote_url": "https://github.com/Tabor-Research-Group/hpclib.git",
                "commit": "aaaa", "version": "1.4.1", "changed_files": [], "changed_count": 0, "behind": 2, "ahead": 0,
                "upstream_version": "1.5.0", "reason": None, "can_update": True, "up_to_date": False,
                "running": "1.4.1", "incoming": [{"commit": "bbbb", "time": 1, "subject": "Quota checks"},
                                                 {"commit": "cccc", "time": 1, "subject": "WSL"}]}
        if method == "POST":
            after = dict(base, commit="cccc", version="1.5.0", behind=0, can_update=False, up_to_date=True, incoming=[])
            return {"updated": True, "from": {"commit": "aaaa", "version": "1.4.1"},
                    "to": {"commit": "cccc", "version": "1.5.0"}, "commits": base["incoming"], "status": after}
        return base

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

    def test_interface_settings_edit_a_copied_user_css_in_place(self):
        defaults = (console_ui.ROOT / "agent-console" / "default-styles.css").read_text()
        (self.console / "user.css").write_text(defaults.replace("--muted: #6e6e73;", "--muted: #555555;"))
        self.open("#/interface")
        muted = self.page.get_by_label("--muted (light)", exact=True)
        muted.wait_for()
        self.assertEqual(muted.input_value(), "#555555")                        # what user.css sets
        self.assertEqual(self.page.get_by_label("--bg (light)", exact=True).input_value(), "")   # a copied default
        self.page.get_by_label("--bg (light)", exact=True).fill("#ff4435")
        with self.page.expect_response(lambda r: r.url.endswith("/api/styles") and r.request.method == "PUT"):
            self.page.get_by_role("button", name="Save", exact=True).click()
        text = (self.console / "user.css").read_text()
        self.assertIn("  --bg: #ff4435;", text.split(":root {", 1)[1].split("}", 1)[0])   # changed in place
        self.assertNotIn("Interface Settings <<<", text)                              # nothing new to add
        var = "name => getComputedStyle(document.documentElement).getPropertyValue(name).trim()"
        self.page.wait_for_function("getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() === '#ff4435'")
        self.assertEqual(self.page.evaluate(var, "--muted"), "#555555")
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
        with self.page.expect_response(lambda r: r.url.endswith("/api/styles") and r.request.method == "PUT"):
            self.page.get_by_role("button", name="Save", exact=True).click()
        block = (self.console / "user.css").read_text().split("Interface Settings")[1]   # a new user.css: the block
        self.assertIn("--accent: #7b3fbf;", block)
        self.assertIn("--radius-control: 2px;", block)
        self.page.reload()
        self.page.get_by_label("--accent (light)", exact=True).wait_for()
        self.assertEqual(self.page.evaluate(accent), "#7b3fbf")                    # from user.css
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

    def test_remove_a_cluster_from_its_settings_tab(self):
        demo = {"name": "demo", "host": "me@demo.example", "mcp_name": "hpclib-demo", "set_up": True, "port": 20735,
                "has_owner_token": True, "login": {"state": "connected"}, "tunnel": {"state": "down"}}
        removed = []

        def handler(route, path):
            if path == "clusters/demo" and route.request.method == "DELETE":
                removed.append(route.request.post_data_json)
                route.fulfill(body=json.dumps({"removed": "demo", "kept_in": "/home/me/.config/hpclib/agents/.removed/demo-1",
                                               "warnings": [], "tunnel_stopped": False,
                                               "llm_clients": {"claude_desktop": ["hpclib-demo"]}}),
                              content_type="application/json")
                return True
            if path in ("clusters", "clusters/demo"):
                route.fulfill(body=json.dumps({"clusters": [demo]} if path == "clusters" else demo),
                              content_type="application/json")
                return True
            if path == "clusters/demo/settings":
                route.fulfill(body=json.dumps({"modes": ["all", "new", "review"], "auto_approve_templates": "all",
                                               "tunnel_args": [], "connection_hours": 12, "rest_on": "job"}),
                              content_type="application/json")
                return True
            return self.handler(route, path)
        console_ui.TestConsoleUI.load_console(self, handler)
        self.page.goto(self.base + "/#/c/demo/settings")
        card = self.page.locator("#remove-cluster")
        card.wait_for()
        revoke = card.get_by_label("Revoke its agent token on the cluster first")
        self.assertTrue(revoke.is_disabled())                                    # the tunnel is down
        card.get_by_label("Log out").uncheck()
        card.get_by_role("button", name="Remove cluster…", exact=True).click()
        self.page.locator("hpc-dialog").get_by_role("button", name="Remove", exact=True).click()
        card.get_by_text("demo was removed", exact=False).wait_for()
        self.assertEqual(removed, [{"revoke_token": False, "stop_tunnels": True, "log_out": False, "llm_clients": True}])
        self.assertIn(".removed/demo-1", card.inner_text())
        self.assertIn("claude_desktop", card.inner_text())
        self.assertEqual(self.errors, [])

    def test_setup_panel_writes_the_mcp_entry(self):
        demo = {"name": "demo", "host": "me@demo.example", "mcp_name": "hpclib-demo", "set_up": True, "port": 20735,
                "has_owner_token": True, "login": {"state": "connected"}, "tunnel": {"state": "down"},
                "operation": {"kind": "setup", "state": "succeeded", "started": 1, "title": "setup_agents"}}
        state = {"cursor": "missing"}
        writes = []

        def handler(route, path):
            if path in ("clusters", "clusters/demo"):
                route.fulfill(body=json.dumps({"clusters": [demo]} if path == "clusters" else demo),
                              content_type="application/json")
                return True
            if path == "clusters/demo/operation":
                route.fulfill(body=json.dumps({"kind": "setup", "state": "succeeded", "title": "setup_agents",
                                               "log": ["Not done yet: add the MCP entry"]}), content_type="application/json")
                return True
            if path == "clusters/demo/llm-clients":
                route.fulfill(body=json.dumps({"mcp_name": "hpclib-demo", "set_up": True, "python": "/usr/bin/python3",
                                               "python_has_mcp": False, "clients": [
                    {"id": "cursor", "title": "Cursor", "available": True, "path": "/home/me/.cursor/mcp.json",
                     "error": None, "windows": False, "after": "Cursor reloads.", "enabled": False, "state": state["cursor"]},
                    {"id": "chatgpt", "title": "ChatGPT", "available": False, "path": "/x", "error": None,
                     "windows": False, "after": "", "enabled": False, "state": "missing"}]}),
                              content_type="application/json")
                return True
            if path == "llm-clients/cursor/write":
                writes.append(route.request.post_data_json)
                state["cursor"] = "current"
                route.fulfill(body=json.dumps({"changed": ["hpclib-demo"], "client": {}}), content_type="application/json")
                return True
            return self.handler(route, path)
        console_ui.TestConsoleUI.load_console(self, handler)
        self.page.goto(self.base + "/#/c/demo/agents")
        box = self.page.locator(".connect-clients")
        box.get_by_text("Connect your LLM clients to hpclib-demo").wait_for()
        self.assertIn("/usr/bin/python3 can't import the MCP SDK", box.inner_text())
        self.assertNotIn("ChatGPT", box.inner_text())                               # not installed here
        box.get_by_role("button", name="Write entry", exact=True).click()
        box.get_by_role("button", name="Write again", exact=True).wait_for()
        self.assertIn("has it", box.inner_text())
        self.assertEqual(writes, [{"names": ["hpclib-demo"], "force": True}])
        self.assertEqual(self.errors, [])

    def test_settings_tab_has_the_llm_clients_and_the_launcher_here(self):
        demo = {"name": "demo", "host": "me@demo.example", "mcp_name": "hpclib-demo", "set_up": True, "port": 20735,
                "has_owner_token": True, "login": {"state": "connected"}, "tunnel": {"state": "down"}}
        puts = []

        def handler(route, path):
            if path in ("clusters", "clusters/demo"):
                route.fulfill(body=json.dumps({"clusters": [demo]} if path == "clusters" else demo),
                              content_type="application/json")
                return True
            if path == "clusters/demo/settings":
                route.fulfill(body=json.dumps({"modes": ["all"], "auto_approve_templates": "all", "tunnel_args": [],
                                               "connection_hours": 12, "rest_on": "job"}), content_type="application/json")
                return True
            if path == "clusters/demo/local":
                puts.append(route.request.post_data_json)
                route.fulfill(body=json.dumps({"entry": {}, "llm_clients": {}, "local_roots": []}),
                              content_type="application/json")
                return True
            if path == "clusters/demo/llm-clients":
                route.fulfill(body=json.dumps({"mcp_name": "hpclib-demo", "set_up": True,
                    "python": "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3", "python_has_mcp": False,
                    "local": {"command": "", "args": [], "local_roots": ["/Users/me/Documents/Claude"]},
                    "clients": [{"id": "claude_desktop", "title": "Claude Desktop", "available": True, "path": "/x.json",
                                 "error": None, "windows": False, "after": "", "enabled": True, "state": "current",
                                 "launcher": "own", "launcher_argv": ["/usr/bin/arch", "-arm64", "/venv/my python/bin/python"]}]}),
                              content_type="application/json")
                return True
            return self.handler(route, path)
        console_ui.TestConsoleUI.load_console(self, handler)
        self.page.goto(self.base + "/#/c/demo/overview")
        self.page.locator(".cluster-head").first.wait_for()
        self.assertEqual(self.page.get_by_role("button", name="Connect LLM clients").count(), 0)   # not a next step
        self.page.goto(self.base + "/#/c/demo/settings")
        card = self.page.locator("#cluster-llm-clients")
        card.get_by_text("has it, with its own launcher").wait_for()
        self.assertIn("Claude Desktop starts it their own way", card.inner_text())
        # the tunnel and its server first; then ports and quotas; the LLM clients; removal last
        heads = self.page.eval_on_selector_all("main section.card > h3, main section.card > .toolbar > h3",
                                               "hs => hs.map(h => h.textContent.split(' ')[0])")
        order = [h for h in heads if h in ("Tunnel", "Server", "Login-node", "Quota", "LLM", "Remove")]
        self.assertEqual(order[0], "Tunnel", heads)
        self.assertEqual(order[-2:], ["LLM", "Remove"], heads)
        self.assertEqual(order, sorted(order, key=["Tunnel", "Server", "Login-node", "Quota", "LLM", "Remove"].index), heads)
        card.get_by_role("button", name="Use Claude Desktop's launcher", exact=True).click()
        self.page.wait_for_function("document.querySelector('#cluster-llm-clients') !== null")
        for _ in range(50):
            if puts:
                break
            self.page.wait_for_timeout(100)
        self.assertEqual(puts[0], {"command": "/usr/bin/arch", "args": "-arm64 '/venv/my python/bin/python'"})
        self.page.wait_for_load_state("networkidle")              # the card draws again after saving
        self.page.wait_for_timeout(300)
        card.get_by_label("MCP launch command").fill("/opt/py/bin/python")
        card.get_by_label("Launch arguments").fill("")
        card.get_by_role("button", name="Save", exact=True).click()
        for _ in range(50):
            if len(puts) > 1:
                break
            self.page.wait_for_timeout(100)
        self.assertEqual(puts[1], {"command": "/opt/py/bin/python", "args": "", "local_roots": ["/Users/me/Documents/Claude"]})
        self.assertEqual(self.errors, [])

    def test_uv_and_pixi_install_and_directories(self):
        demo = {"name": "demo", "host": "me@demo.example", "mcp_name": "hpclib-demo", "set_up": True, "port": 20735,
                "has_owner_token": True, "login": {"state": "connected"}, "tunnel": {"state": "down"}}
        calls = []
        state = {"uv": None}

        def info():
            return {"settings_file": "/home/me/.local/tunnels/settings/pytools.sh",
                    "tools": {"uv": {"path": state["uv"], "version": "0.9.9" if state["uv"] else None},
                              "pixi": {"path": None, "version": None}},
                    "dirs": {"UV_PYTHON_INSTALL_DIR": {"value": "/home/me/.local/share/uv/python", "source": "default", "saved": None},
                             "UV_CACHE_DIR": {"value": "/home/me/.cache/uv", "source": "default", "saved": None},
                             "PIXI_CACHE_DIR": {"value": "/home/me/.cache/rattler/cache", "source": "default", "saved": None}},
                    "shell": {"UV_CACHE_DIR": "/scratch/me/.uv/cache"}}

        def handler(route, path):
            if path in ("clusters", "clusters/demo"):
                route.fulfill(body=json.dumps({"clusters": [demo]} if path == "clusters" else demo),
                              content_type="application/json")
                return True
            if path == "clusters/demo/settings":
                route.fulfill(body=json.dumps({"modes": ["all"], "auto_approve_templates": "all", "tunnel_args": [],
                                               "connection_hours": 12, "rest_on": "job"}), content_type="application/json")
                return True
            if path.startswith("clusters/demo/python-tools"):
                calls.append((route.request.method, path, route.request.post_data_json if route.request.method != "GET" else None))
                out = info()
                if path.endswith("/install"):
                    state["uv"] = "/home/me/.local/bin/uv"
                    out = dict(info(), install={"tool": "uv", "ok": True, "exit_code": 0, "log": []})
                route.fulfill(body=json.dumps(out), content_type="application/json")
                return True
            return self.handler(route, path)
        console_ui.TestConsoleUI.load_console(self, handler)
        self.page.goto(self.base + "/#/c/demo/settings")
        card = self.page.locator("#python-tools")
        card.get_by_text("installs to ~/.local/bin/uv").wait_for()
        self.assertIn("your ~/.bashrc sets /scratch/me/.uv/cache", card.inner_text())
        card.get_by_role("button", name="Install", exact=True).first.click()
        card.get_by_text("uv 0.9.9 is installed.").wait_for()
        self.assertEqual(calls[-1][1:], ("clusters/demo/python-tools/install", {"tool": "uv"}))
        card.get_by_role("button", name="Fill in what ~/.bashrc sets").click()
        card.get_by_label("UV_PYTHON_INSTALL_DIR").fill("/scratch/me/.uv/python")
        card.get_by_role("button", name="Save directories").click()
        card.get_by_text("Saved on the cluster").wait_for()
        self.assertEqual(calls[-1], ("PUT", "clusters/demo/python-tools",
                                     {"UV_PYTHON_INSTALL_DIR": "/scratch/me/.uv/python", "UV_CACHE_DIR": "/scratch/me/.uv/cache"}))
        self.assertNotIn("null", card.inner_text())
        self.assertEqual(self.errors, [])

    def test_sandbox_installer_and_backend_selection(self):
        demo = {"name": "demo", "host": "me@demo.example", "mcp_name": "hpclib-demo", "set_up": True, "port": 20735,
                "has_owner_token": True, "login": {"state": "connected"}, "tunnel": {"state": "up"}}
        calls = []
        def handler(route, path):
            method = route.request.method
            body = route.request.post_data_json if route.request.post_data else None
            calls.append((method, path, body))
            if path in ("clusters", "clusters/demo"):
                out = {"clusters": [demo]} if path == "clusters" else demo
            elif path == "clusters/demo/settings":
                out = {"modes": ["all"], "auto_approve_templates": "all", "tunnel_args": [], "rest_on": "login"}
            elif path == "clusters/demo/rest/admin/config":
                out = ({"changed": ["sandbox"]} if method == "PUT" else
                       {"path": "/home/me/config.json", "editable": ["sandbox"],
                        "config": {"sandbox": {"method": "pyxis", "runtime": "/usr/bin/srun", "flags": ["--exclusive"]}},
                        "effective": {"limits": {}, "environments": {}, "sandbox": {"effective": "pyxis"}}})
            elif path.startswith("clusters/demo/sandbox-tools"):
                out = {"host": "login1", "root": "/home/me/.local/share/hpclib/sandbox-tools",
                       "namespaces": {"ok": True, "detail": "namespaces work"}, "tools": {}}
                if method == "POST":
                    out.update(install={"ok": True}, tools={"charliecloud": {"path": "/home/me/.local/share/hpclib/sandbox-tools/bin/ch-run",
                                                                          "version": "0.45.2"}})
            elif path.startswith("clusters/demo/rest/sandbox"):
                out = {"sandbox": {"effective": "charliecloud", "network_isolated": True}, "self_test": {"passed": True}}
            else:
                return self.handler(route, path)
            route.fulfill(body=json.dumps(out), content_type="application/json")
            return True
        console_ui.TestConsoleUI.load_console(self, handler)
        self.page.goto(self.base + "/#/c/demo/settings")
        card = self.page.locator("#sandbox-tools")
        card.get_by_label("Sandbox installation modules").fill("GCC WebProxy")
        card.get_by_role("row").filter(has_text="Charliecloud + Bubblewrap").get_by_role("button", name="Install", exact=True).click()
        card.get_by_text("Installed. Choose the method", exact=False).wait_for()
        install = [call for call in calls if call[1].endswith("/sandbox-tools/install")][-1]
        self.assertEqual(install[2], {"tool": "charliecloud", "modules": ["GCC", "WebProxy"]})
        self.page.get_by_label("Container runtime", exact=True).select_option("charliecloud")
        self.assertEqual(self.page.get_by_label("Runtime path", exact=True).input_value(), "")
        self.page.get_by_role("button", name="Save server settings", exact=True).click()
        self.page.get_by_text("Saved sandbox; in effect now.", exact=False).wait_for()
        save = [call for call in calls if call[0] == "PUT" and call[1].endswith("/admin/config")][-1]
        self.assertEqual(save[2]["changes"]["sandbox"]["method"], "charliecloud")
        self.assertNotIn("runtime", save[2]["changes"]["sandbox"])
        self.assertEqual(save[2]["changes"]["sandbox"]["flags"], [])
        card.get_by_role("button", name="Test active sandbox", exact=True).click()
        card.get_by_text('"passed": true', exact=False).wait_for()
        self.assertTrue(any(call[0] == "GET" and call[1].startswith("clusters/demo/rest/sandbox") for call in calls))
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

    def test_server_settings_offer_slurm_path_and_the_container_runtime(self):
        """A cluster whose server can't find SLURM: a plain notice with a one-click fix that lands in config.json,
        an Extra PATH box, and a Container runtime choice that keeps the other sandbox settings."""
        demo = {"name": "demo", "host": "me@demo.example", "mcp_name": "hpclib-demo", "set_up": True, "port": 20735,
                "has_owner_token": True, "login": {"state": "connected"}, "tunnel": {"state": "up"}}
        slurm = "/cm/local/apps/slurm/current/bin"
        config = {"sandbox": {"method": "auto", "binds": ["/sw", "/cm"]}}
        puts = []

        def effective():
            return {"limits": {}, "environment": {"jobs": {}, "syncs": {}}, "environments": {"managers": {}},
                    "extra_path": config.get("extra_path", []),
                    "slurm_missing": [] if "extra_path" in config else [slurm],
                    "sandbox": {"effective": "unavailable", "error": "jobs must run sandboxed but apptainer or "
                                "singularity, or podman was not found on the server's PATH"}}

        def handler(route, path):
            if path == "clusters/demo/rest/admin/config":
                if route.request.method == "PUT":
                    puts.append(route.request.post_data_json["changes"])
                    config.update({k: v for k, v in puts[-1].items() if v is not None})
                route.fulfill(body=json.dumps({"path": "/home/me/.local/tunnels/rest/config.json", "config": config,
                                               "editable": ["sandbox", "extra_path"], "rules": {},
                                               "effective": effective(), "backup": "config.json.replaced-1",
                                               "changed": sorted(puts[-1]) if puts else []}),
                              content_type="application/json")
                return True
            if path in ("clusters", "clusters/demo"):
                route.fulfill(body=json.dumps({"clusters": [demo]} if path == "clusters" else demo),
                              content_type="application/json")
                return True
            if path == "clusters/demo/settings":
                route.fulfill(body=json.dumps({"modes": ["all"], "auto_approve_templates": "all", "tunnel_args": [],
                                               "connection_hours": 12, "rest_on": "job"}), content_type="application/json")
                return True
            if path.startswith("clusters/demo/python-tools"):
                route.fulfill(body=json.dumps({"tools": {}, "dirs": {}, "shell": {}}), content_type="application/json")
                return True
            return self.handler(route, path)
        console_ui.TestConsoleUI.load_console(self, handler)
        self.page.goto(self.base + "/#/c/demo/settings")
        card = self.page.locator("section.card", has=self.page.get_by_text("config.json on the cluster"))
        notice = card.get_by_text("This server can't find SLURM's commands", exact=False)
        notice.wait_for()
        self.assertIn(slurm, card.inner_text())
        self.assertIn("Jobs can't run: jobs must run sandboxed", card.inner_text())
        self.assertEqual(card.get_by_label("Extra PATH directories").input_value(), "")
        # one click: saved on the cluster, in config.json, with nothing else changed
        card.get_by_role("button", name="Add it to the server's PATH").click()
        card.get_by_text("Saved to config.json; in effect now.").wait_for()
        self.assertEqual(puts, [{"extra_path": [slurm]}])
        self.assertEqual(card.get_by_label("Extra PATH directories").input_value(), slurm)
        # the runtime picker offers Pyxis, defaults to automatic, and a save sends the sandbox with its other keys
        runtime = card.get_by_label("Container runtime")
        self.assertEqual(runtime.input_value(), "auto")
        self.assertIn("Pyxis (SLURM with enroot)", runtime.inner_text())
        runtime.select_option("pyxis")
        card.get_by_role("button", name="Save server settings").click()
        card.get_by_text("Saved sandbox").wait_for()
        self.assertEqual(puts[-1], {"sandbox": {"method": "pyxis", "binds": ["/sw", "/cm"], "writable": []}})
        self.assertNotIn("extra_path", puts[-1])                  # unchanged sections aren't sent again
        self.assertNotIn("null", card.inner_text())

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

    def test_update_hpclib(self):
        self.open("#/console")
        card = self.page.locator("#update")
        card.get_by_text("2 new commits", exact=False).wait_for()
        self.assertIn("Quota checks", card.inner_text())
        card.get_by_role("button", name="Update hpclib", exact=True).click()
        card.get_by_text("Updated 1.4.1 → 1.5.0 (2 commits). Restart the console to run it.", exact=False).wait_for()
        self.assertIn("1.5.0 on disk (restart to run it)", card.inner_text())
        self.assertIn("POST", [m for m, p, _ in self.calls if p == "update"])
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
