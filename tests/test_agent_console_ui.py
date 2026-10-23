"""Optional browser regression tests for console styling (Playwright + Chromium).

Run: PYTHONPATH=tests python -m unittest test_agent_console_ui -v
No cluster or external network is used; the API responses are test fixtures.
"""
import os
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hpclib" / "servers"))
import agent_console  # noqa: E402

FIXTURE = """<!doctype html><html><head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="/default-styles.css">
  <link rel="stylesheet" href="/style.css">
  <link rel="stylesheet" href="/user.css">
</head><body>
  <main><table id="sessions" class="rows"></table>
    <button id="sample" class="primary">Action</button><span id="hint" class="small">Hint</span>
    <div class="actions"><button>One</button><button>Two</button></div>
    <hpc-dialog id="dialog" heading="Settings"><p>Example settings</p></hpc-dialog>
  </main>
  <script type="module">
    import { configure, el } from "/components.js";
    const long = "x".repeat(10000);
    const lines = [long, ...Array.from({ length: 60 }, (_, n) => `line ${n}`)];
    const state = { cluster: "demo", host: "demo", port: 1234, state: "down", login: {state: "none"} };
    window.copyText = "=== 2026-10-18 10:08:54 agent_tunnel fixture@faster.example ===\\n" +
      Array.from({ length: 1000 }, (_, n) => `full block line ${n}`).join("\\n");
    window.copyRequests = [];
    configure({ api: async (path) => {
      if (path.includes("latest=1")) {
        window.copyRequests.push(path);
        if (window.copyError) throw new Error(window.copyError);
        return { copy_text: window.copyText };
      }
      return path.includes("/operation?") ? { state: "succeeded", title: "Installation log", log: lines }
        : path.includes("/log?") ? { lines } : state;
    }});
    // Insert the custom row programmatically, as app.js does. HTML table parsing
    // would otherwise move an unknown element outside the table before upgrade.
    window.row = el("hpc-app-session", { app: "vscode", "app-title": "VS Code", cluster: "demo" });
    row.data = state;
    document.getElementById("sessions").append(row);
    window.ready = true;
  </script>
</body></html>"""


@unittest.skipUnless(sync_playwright, "optional Playwright dependency is not installed")
class TestConsoleUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.console = Path(cls.tmp.name)
        env = mock.patch.dict(os.environ, {"HPCLIB_CONSOLE_DIR": str(cls.console)})
        env.start()
        cls.addClassCleanup(env.stop)
        log = mock.patch.object(agent_console.ConsoleHandler, "log_message", lambda *args: None)
        log.start()
        cls.addClassCleanup(log.stop)
        cls.server = agent_console.ConsoleServer(("127.0.0.1", 0), "ui-test-key", None,
                                                  static_dir=str(ROOT / "agent-console"))
        cls.addClassCleanup(cls.server.server_close)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.shutdown)
        cls.base = f"http://127.0.0.1:{cls.server.port}"
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        try:
            cls.browser = cls.playwright.chromium.launch()
        except Exception as exc:
            if "Executable doesn't exist" in str(exc):
                raise unittest.SkipTest("install Playwright's Chromium browser to run UI tests") from exc
            raise
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        (self.console / "user.css").unlink(missing_ok=True)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 900})
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("**/fixture.html", lambda route: route.fulfill(body=FIXTURE, content_type="text/html"))

    def load(self):
        self.page.goto(self.base + "/fixture.html")
        self.page.wait_for_function("window.ready === true")

    def transfer_row(self):
        self.load()
        self.page.evaluate("""async () => {
          const {configure, el} = await import('/components.js');
          const state = {cluster: 'demo', host: 'demo', port: 1234, state: 'up',
                         prepare_open: true, url: '/must-not-open-before-reset',
                         token_known: true, login: {state: 'connected'}, install: {state: 'nothing'}};
          window.openRequests = [];
          configure({api: async (path, options) => {
            if (path.endsWith('/open')) {
              window.openRequests.push({path, method: options.method});
              return new Promise((resolve, reject) => {
                window.finishOpen = () => resolve({url: '/opened-transfer.html', cache_cleared: true});
                window.failOpen = () => reject(new Error('Data transfer is busy; wait for it to finish'));
              });
            }
            return state;
          }});
          row.remove();
          window.row = el('hpc-app-session', {app: 'transfer', 'app-title': 'Data transfer', cluster: 'demo'});
          row.data = state;
          document.getElementById('sessions').append(row);
        }""")

    def test_transfer_open_waits_for_cache_preparation_before_navigating(self):
        self.transfer_row()
        self.page.context.route("**/opened-transfer.html", lambda route:
                                route.fulfill(body="<title>Transfer opened</title>", content_type="text/html"))
        with self.page.expect_popup() as opened:
            self.page.get_by_role("button", name="Open Data transfer", exact=True).click()
        popup = opened.value
        self.addCleanup(popup.close)
        self.page.wait_for_function("!!window.finishOpen")
        self.assertEqual(popup.url, "about:blank")
        self.assertTrue(self.page.get_by_role("button", name="Open Data transfer", exact=True).is_disabled())
        self.assertIsNone(popup.evaluate("window.opener"))
        self.assertEqual(self.page.evaluate("window.openRequests"),
                         [{"path": "apps/transfer/demo/open", "method": "POST"}])
        self.page.evaluate("window.finishOpen()")
        popup.wait_for_url("**/opened-transfer.html")
        self.assertEqual(popup.title(), "Transfer opened")
        self.assertFalse(self.errors)

    def test_transfer_open_closes_reserved_tab_and_shows_failure(self):
        self.transfer_row()
        with self.page.expect_popup() as opened:
            self.page.get_by_role("button", name="Open Data transfer", exact=True).click()
        popup = opened.value
        self.page.wait_for_function("!!window.failOpen")
        with popup.expect_event("close"):
            self.page.evaluate("window.failOpen()")
        self.page.get_by_text("Data transfer is busy; wait for it to finish", exact=True).wait_for()
        self.assertFalse(self.page.get_by_role("button", name="Open Data transfer", exact=True).is_disabled())
        self.assertFalse(self.errors)

    def test_transfer_open_with_blocked_popup_leaves_cache_alone(self):
        self.transfer_row()
        self.page.evaluate("window.open = () => null")
        self.page.get_by_role("button", name="Open Data transfer", exact=True).click()
        self.page.get_by_text("Allow pop-ups for this console, then Open again.", exact=True).wait_for()
        self.assertEqual(self.page.evaluate("window.openRequests"), [])
        self.assertFalse(self.errors)

    def test_package_services_are_tabs_beside_sessions_with_independent_controls(self):
        apps = [{"id": "cmccdb", "title": "CMCC DB (dev)"},
                {"id": "cmccdb_preview", "title": "CMCCDB Preview",
                 "tab": {"parent": "cmccdb", "id": "preview", "label": "Preview"}},
                {"id": "cmccdb_rehearsal", "title": "CMCCDB Release Rehearsal",
                 "tab": {"parent": "cmccdb", "id": "rehearsal", "label": "Release Rehearsal"}}]
        requested = []
        state = {"cluster": "demo", "host": "demo", "port": 30933, "state": "down",
                 "login": {"state": "none"}, "install": {"state": "unchecked"}}

        def respond(route):
            endpoint = route.request.url.split("/api/", 1)[1].split("?", 1)[0]
            requested.append(endpoint)
            if endpoint == "apps":
                out = {"apps": apps}
            elif endpoint in ["apps/" + a["id"] for a in apps]:
                app = next(a for a in apps if endpoint == "apps/" + a["id"])
                out = {"title": app["title"], "sessions": [state]}
            elif endpoint.endswith("/settings"):
                out = {"tunnel_args": [], "conda_env": None, "modules": [], "install_modules": [],
                       "project": "", "python_env": False, "job": False, "installable": True,
                       "settings": {}, "defaults": {}, "fields": [], "secrets": [], "tokens": None}
            elif endpoint.startswith("apps/"):
                out = state
            else:
                out = {"prompts": [], "running": False, "proposals": [], "clusters": []}
            route.fulfill(body=json.dumps(out), content_type="application/json")

        self.page.route("**/api/**", respond)
        self.page.add_init_script("localStorage.setItem('agent-console-key', 'fixture-key')")
        self.page.goto(self.base + "/index.html#/cmccdb/sessions")
        self.page.locator('hpc-app-session[app="cmccdb"]').wait_for()
        self.assertEqual(self.page.locator("#apps a").all_text_contents(), ["Agents", "CMCC DB (dev)"])
        self.assertEqual(self.page.locator("#pages a").all_text_contents(),
                         ["Sessions", "Preview", "Release Rehearsal"])
        for label, service in (("Preview", "cmccdb_preview"), ("Release Rehearsal", "cmccdb_rehearsal")):
            self.page.locator("#pages").get_by_role("link", name=label, exact=True).click()
            self.page.locator('hpc-app-session[app="' + service + '"]').wait_for()
            self.assertEqual(self.page.locator("#app-title").inner_text(), "CMCC DB (dev)")
            self.assertEqual(self.page.locator("#pages a.active").inner_text(), label)
            self.page.get_by_role("button", name="Settings…", exact=True).click()
            self.page.locator("hpc-panel").wait_for()
            self.assertIn("apps/" + service + "/demo/settings", requested)
        self.page.goto(self.base + "/index.html#/cmccdb_preview/sessions")
        self.page.locator('hpc-app-session[app="cmccdb_preview"]').wait_for()
        self.assertEqual(self.page.locator("#pages a.active").inner_text(), "Preview")
        self.assertEqual(self.errors, [])

    def test_copy_last_block_fetches_complete_output_for_tunnel_and_operation_logs(self):
        self.page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        self.load()
        expected = self.page.evaluate("window.copyText")
        for kind in ("tunnel", "operation"):
            with self.subTest(kind=kind):
                self.page.evaluate("row.toggleLog()" if kind == "tunnel" else "row.showOperation('copy-test')")
                self.page.get_by_role("button", name="Copy last block", exact=True).click()
                self.page.wait_for_function("document.querySelector('hpc-log').textContent.includes('Copied.')")
                self.assertEqual(self.page.evaluate("navigator.clipboard.readText()"), expected)
        requests = self.page.evaluate("window.copyRequests")
        self.assertEqual(len(requests), 2)
        self.assertTrue(all("latest=1" in path for path in requests))
        self.assertIn("/log?", requests[0])
        self.assertIn("/operation?", requests[1])
        self.assertEqual(self.errors, [])

    def test_copy_errors_remain_visible_without_replacing_the_clipboard(self):
        self.page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        self.load()
        self.page.evaluate("navigator.clipboard.writeText('existing clipboard')")
        self.page.evaluate("row.toggleLog()")
        message = "The last log block is too large to copy (over 8 MiB); open the log file instead."
        self.page.evaluate("message => { window.copyError = message; }", message)
        self.page.get_by_role("button", name="Copy last block", exact=True).click()
        self.page.get_by_text(message, exact=True).wait_for()
        self.assertEqual(self.page.evaluate("navigator.clipboard.readText()"), "existing clipboard")
        self.assertEqual(self.errors, [])

    def test_installation_modules_are_editable_without_a_python_environment(self):
        """Use the actual app settings form, including a package's optional registry credential fields."""
        saved = []
        state = {"cluster": "demo", "host": "demo", "state": "down", "login": {"state": "none"},
                 "install": {"state": "unchecked"}}
        conf = {"tunnel_args": ["--time=12:00:00", "--nodelist=fc041"], "conda_env": None, "modules": [], "project": "", "python_env": False,
                "job": True, "installable": True, "install_modules": ["WebProxy"], "settings": {}, "defaults": {},
                "fields": [{"name": "PAI_GITHUB_USERNAME", "label": "GitHub username (optional)", "hint": "", "choices": None}],
                "secrets": [{"name": "PAI_GITHUB_PAT", "label": "GitHub PAT (optional)", "hint": "", "set": False}],
                "tokens": None}

        def respond(route):
            path = route.request.url.split("/api/", 1)[1].split("?", 1)[0]
            if path.endswith("/settings"):
                if route.request.method == "PUT":
                    saved.append(route.request.post_data_json)
                out = conf
            elif path == "apps":
                out = {"apps": [{"id": "pai", "title": "PAI"}]}
            elif path == "apps/pai":
                out = {"title": "PAI", "sessions": [state]}
            elif path == "apps/pai/demo":
                out = state
            else:
                out = {"prompts": [], "proposals": [], "clusters": []}
            route.fulfill(body=json.dumps(out), content_type="application/json")

        self.page.route("**/api/**", respond)
        self.page.add_init_script("localStorage.setItem('agent-console-key', 'fixture-key')")
        self.page.goto(self.base + "/index.html#/pai/sessions")
        self.page.locator("hpc-app-session").wait_for()
        self.page.evaluate("""() => document.dispatchEvent(new CustomEvent('hpc-app-settings', {
          detail: {app: 'pai', session: {cluster: 'demo'}, row: document.querySelector('hpc-app-session')}
        }))""")
        modules = self.page.locator('hpc-field[label="Installation modules"] input')
        self.assertEqual(modules.input_value(), "WebProxy")
        nodes = self.page.locator('hpc-field[label="Job: Node list (optional)"] input')
        self.assertEqual(nodes.input_value(), "fc041")
        nodes.fill("fc[041-042]")
        self.assertEqual(self.page.get_by_label("GitHub PAT (optional)", exact=True).get_attribute("type"), "password")
        modules.fill("GCC/12.2.0 WebProxy")
        with self.page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/settings")):
            self.page.get_by_role("button", name="Save", exact=True).click()
        self.assertEqual(saved[-1]["install_modules"], ["GCC/12.2.0", "WebProxy"])
        self.assertEqual(saved[-1]["tunnel_args"], ["--time=12:00:00", "--nodelist=fc[041-042]"])
        self.assertNotIn("PAI_GITHUB_PAT", saved[-1].get("settings", {}))
        modules.fill("")
        nodes.fill("")
        with self.page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/settings")):
            self.page.get_by_role("button", name="Save", exact=True).click()
        self.assertEqual(saved[-1]["install_modules"], [])
        self.assertEqual(saved[-1]["tunnel_args"], ["--time=12:00:00"])
        self.assertEqual(self.errors, [])

    def metrics(self):
        self.page.wait_for_function("document.querySelector('pre.log')?.textContent.startsWith('xxxx')")
        return self.page.evaluate("""() => {
          const panel = document.querySelector('hpc-panel.log-panel');
          const log = panel.querySelector('pre.log');
          return { page: document.documentElement.scrollWidth, viewport: innerWidth,
                   table: document.querySelector('table').getBoundingClientRect().width,
                   panel: panel.getBoundingClientRect().width,
                   log: log.getBoundingClientRect().width, client: log.clientWidth,
                   scroll: log.scrollWidth, height: log.getBoundingClientRect().height,
                   clientHeight: log.clientHeight, scrollHeight: log.scrollHeight };
        }""")

    def test_long_logs_do_not_widen_tables_or_the_page(self):
        for width in (1440, 900, 480):
            with self.subTest(viewport=width):
                self.page.set_viewport_size({"width": width, "height": 900})
                self.load()
                before = self.page.locator("table").bounding_box()["width"]
                for kind in ("tunnel", "operation"):
                    with self.subTest(kind=kind):
                        self.page.evaluate("row.toggleLog()" if kind == "tunnel" else "row.showOperation('test')")
                        sizes = self.metrics()
                        self.assertLessEqual(sizes["page"], width + 1, sizes)
                        self.assertLessEqual(sizes["table"], before + 1, sizes)
                        self.assertLessEqual(sizes["panel"], min(960, width - 32) + 1, sizes)
                        self.assertLessEqual(sizes["log"], sizes["panel"], sizes)
                        self.assertGreater(sizes["scroll"], sizes["client"], sizes)
                        self.assertGreater(sizes["scrollHeight"], sizes["clientHeight"], sizes)
                self.assertEqual(self.errors, [])

    def test_user_variables_reach_logs_and_shadow_components(self):
        (self.console / "user.css").write_text(""":root {
          --log-max-width: 540px; --log-max-height: 100px;
          --font-size-base: 16px; --font-size-small: 13px; --font-ui: monospace;
          --radius-control: 9px; --radius-panel: 13px; --radius-dialog: 15px;
          --close-size: 32px; --close-font-size: 22px; --dialog-max-width: 620px;
          --space-sm: 12px; --accent: #123456;
        }""")
        self.load()
        self.page.evaluate("row.toggleLog()")
        sizes = self.metrics()
        self.assertAlmostEqual(sizes["panel"], 540, delta=1)
        self.assertLessEqual(sizes["height"], 101)
        self.assertEqual(self.page.locator("body").evaluate("e => getComputedStyle(e).fontSize"), "16px")
        self.assertEqual(self.page.locator("#hint").evaluate("e => getComputedStyle(e).fontSize"), "13px")
        self.assertEqual(self.page.locator("#sample").evaluate("e => getComputedStyle(e).borderRadius"), "9px")
        self.assertEqual(self.page.locator("#sample").evaluate("e => getComputedStyle(e).backgroundColor"), "rgb(18, 52, 86)")
        self.assertEqual(self.page.locator("main > .actions").evaluate("e => getComputedStyle(e).gap"), "12px")
        self.assertEqual(self.page.locator("hpc-panel").evaluate("e => getComputedStyle(e.shadowRoot.querySelector('section')).borderRadius"), "13px")
        for selector in ("hpc-panel", "hpc-dialog"):
            close = self.page.locator(selector).evaluate("""e => {
              const s = getComputedStyle(e.shadowRoot.querySelector('button.close'));
              return { width: s.width, fontSize: s.fontSize, family: s.fontFamily };
            }""")
            self.assertEqual(close, {"width": "32px", "fontSize": "22px", "family": "monospace"})
        self.page.locator("#dialog").evaluate("e => e.showModal()")
        dialog = self.page.locator("#dialog").evaluate("""e => {
          const d = e.shadowRoot.querySelector('dialog');
          return { width: d.getBoundingClientRect().width, radius: getComputedStyle(d).borderRadius };
        }""")
        self.assertEqual(dialog, {"width": 620, "radius": "15px"})
        self.page.locator("#dialog").evaluate("e => e.close()")
        # Optional wrapping handles even an unbroken 10,000-character line.
        self.page.evaluate("""() => {
          document.documentElement.style.setProperty('--log-white-space', 'pre-wrap');
          document.documentElement.style.setProperty('--log-overflow-wrap', 'anywhere');
        }""")
        sizes = self.metrics()
        self.assertLessEqual(sizes["scroll"], sizes["client"] + 1, sizes)
        self.assertLessEqual(sizes["page"], sizes["viewport"] + 1, sizes)
        # An oversized configured cap still respects a smaller viewport.
        self.page.evaluate("document.documentElement.style.setProperty('--log-max-width', '9999px')")
        self.page.set_viewport_size({"width": 480, "height": 900})
        sizes = self.metrics()
        self.assertLessEqual(sizes["page"], 481, sizes)
        self.assertLessEqual(sizes["panel"], 449, sizes)
        self.assertEqual(self.errors, [])

    def load_console(self, handler=None):
        def api_fixture(route):
            path = route.request.url.split("/api/", 1)[1].split("?", 1)[0]
            if handler and handler(route, path):
                return
            if path == "styles/open":
                route.continue_()  # Exercise the real authenticated file-copy endpoint.
                return
            answers = {"apps": {"apps": [{"id": "agents", "title": "Agents"}]},
                       "clusters": {"clusters": []}, "proposals": {"proposals": []},
                       "prompts": {"prompts": [], "running": False}}
            route.fulfill(body=json.dumps(answers.get(path, {})), content_type="application/json")
        self.page.route("**/api/**", api_fixture)
        self.page.goto(self.base + "/#key=ui-test-key")
        self.page.wait_for_function("document.querySelector('#apps a') !== null")

    def test_ssh_keys_page_manages_public_keys_authorizations_and_host_bindings(self):
        from ssh_keys import SSHKeyError, SSHKeyStore
        store = SSHKeyStore(self.console / 'cluster shared home')
        mutations = []
        cluster = {'name': 'demo', 'host': 'me@login.example', 'set_up': True, 'port': 1234,
                   'has_owner_token': True, 'login': {'state': 'connected'}, 'tunnel': {'state': 'up'}}
        def handler(route, path):
            if path in ('clusters', 'clusters/demo'):
                route.fulfill(body=json.dumps({'clusters': [cluster]} if path == 'clusters' else cluster), content_type='application/json')
                return True
            if path == 'health':
                route.fulfill(body=json.dumps({'hpclib_version': '0.2.59'}), content_type='application/json')
                return True
            if path == 'clusters/demo/ssh/setup':
                mutations.append((route.request.method, path, route.request.post_data_json))
                route.fulfill(body=json.dumps({'ok': True}), content_type='application/json')
                return True
            prefix = 'clusters/demo/rest/admin/ssh/'
            if not path.startswith(prefix):
                return False
            verb = route.request.method
            resource = path[len(prefix):]
            data = route.request.post_data_json if verb != 'GET' else {}
            try:
                if verb != 'GET':
                    mutations.append((verb, resource, data))
                if resource == 'keys':
                    if verb == 'GET': result = store.keys()
                    elif verb == 'POST': result = store.create(data['name'], data['algorithm'], data['comment'])
                    elif verb == 'PUT': result = store.update_comment(data['name'], data['comment'])
                    else: result = store.delete(data['name'])
                elif resource == 'authorized':
                    result = store.authorized() if verb == 'GET' else store.change_authorized(
                        data.get('line'), data.get('id'), data.get('revision'), verb == 'DELETE')
                else:
                    result = store.hosts() if verb == 'GET' else store.set_host(
                        data['host'], data.get('key'), data.get('user', ''), data.get('hostname', ''),
                        data.get('port'), data.get('revision'), verb == 'DELETE')
                route.fulfill(body=json.dumps(result), content_type='application/json')
            except SSHKeyError as e:
                route.fulfill(status=e.status, body=json.dumps({'error': str(e)}), content_type='application/json')
            return True
        self.load_console(handler)
        def save(label):
            button = self.page.get_by_role('button', name=label, exact=True)
            old = button.element_handle()
            button.click()
            self.page.wait_for_function('(node) => !node.isConnected', arg=old)
        self.page.get_by_role('button', name='Set up SSH', exact=True).click()
        self.page.wait_for_function("document.querySelector('hpc-action-button[label=\"Set up SSH\"]')?.button.disabled === false")
        self.assertTrue(any(path.endswith('/ssh/setup') for _, path, _ in mutations))
        self.page.get_by_role('link', name='SSH keys', exact=True).click()
        self.page.get_by_label('Key name', exact=True).fill('github')
        self.page.get_by_label('New key comment').fill('Research GitHub')
        save('Create key')
        public = self.page.get_by_label('Public key github', exact=True)
        public.wait_for()
        self.assertTrue(public.input_value().startswith('ssh-ed25519 '))
        self.page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
        self.page.get_by_role('button', name='Copy public key', exact=True).click()
        self.page.wait_for_function("navigator.clipboard.readText().then(s => s.startsWith('ssh-ed25519 '))")
        self.page.get_by_label('Comment github', exact=True).fill('Edited comment')
        save('Save comment')
        self.page.wait_for_function("document.querySelector('input[aria-label=\"Comment github\"]')?.value === 'Edited comment'")
        self.page.get_by_label('New authorized key', exact=True).fill(store.key('github')['public_key'])
        save('Add authorized key')
        authorized = self.page.locator('textarea[aria-label^="Authorized key "]')
        authorized.wait_for()
        authorized.fill('restrict ' + store.key('github')['public_key'])
        save('Save authorized key')
        self.page.wait_for_function("document.querySelector('textarea[aria-label^=\"Authorized key \"]')?.value.startsWith('restrict ')")
        self.page.get_by_label('SSH host', exact=True).fill('github.com')
        self.page.get_by_label('SSH user', exact=True).fill('git')
        save('Save host binding')
        self.page.get_by_role('button', name='Edit binding', exact=True).wait_for()
        self.page.get_by_role('button', name='Edit binding', exact=True).click()
        self.assertEqual(self.page.get_by_label('SSH user', exact=True).input_value(), 'git')
        self.page.get_by_label('SSH port', exact=True).fill('2222')
        save('Save host binding')
        self.page.get_by_text('User git · Port 2222 · Managed binding', exact=True).wait_for()
        for button, title in (('Remove binding', 'Remove github.com?'), ('Remove authorization', 'Remove this authorization?'), ('Delete key', 'Delete github?')):
            self.page.get_by_role('button', name=button, exact=True).click()
            self.page.get_by_role('dialog').wait_for()
            self.page.get_by_role('button', name='OK', exact=True).click()
            self.page.get_by_role('button', name=button, exact=True).wait_for(state='detached')
        self.assertEqual(store.keys()['keys'], [])
        self.assertEqual(store.authorized()['entries'], [])
        self.assertEqual(self.errors, [])

    def test_settings_clear_leftover_ports_warns_about_extra_logins(self):
        cluster = {'name': 'demo', 'host': 'me@login.example', 'set_up': True, 'port': 20735,
                   'has_owner_token': True, 'login': {'state': 'connected'}, 'tunnel': {'state': 'down'}}
        posted, ops = [], iter([{'kind': 'clear_ports', 'state': 'running', 'log': ['login 1 of at most 3']},
                                {'kind': 'clear_ports', 'state': 'succeeded',
                                 'log': ['every port is clear on every login node that had a record of it']}])
        def handler(route, path):
            answers = {
                'clusters': {'clusters': [cluster]}, 'clusters/demo': cluster,
                'clusters/demo/settings': {'modes': ['all', 'new', 'review'], 'auto_approve_templates': 'new',
                                           'tunnel_args': [], 'connection_hours': 12, 'rest_on': 'job'},
                'clusters/demo/ports': {'ports': [{'port': 20735, 'what': 'agent tunnel', 'app': None, 'running': False},
                                                  {'port': 31000, 'what': 'PAI', 'app': 'pai', 'running': True}]},
            }
            if path == 'clusters/demo/ports/clear':
                posted.append(route.request.post_data_json)
                route.fulfill(status=202, body=json.dumps({'ok': True}), content_type='application/json')
                return True
            if path == 'clusters/demo/operation':
                route.fulfill(body=json.dumps(next(ops, {'kind': 'clear_ports', 'state': 'succeeded', 'log': []})),
                              content_type='application/json')
                return True
            if path in answers:
                route.fulfill(body=json.dumps(answers[path]), content_type='application/json')
                return True
            return False
        self.load_console(handler)
        self.page.goto(self.base + "/#/settings/demo")
        self.page.get_by_text('31000: PAI (running; left alone)').wait_for()
        self.page.get_by_role('button', name='Clear leftover ports…', exact=True).click()
        self.page.get_by_role('dialog').wait_for()
        dialog = self.page.locator('hpc-dialog[heading^="Clear leftover ports"]')
        dialog.get_by_text('this can take many logins', exact=False).wait_for()
        password = dialog.get_by_label('Password (for the extra logins)')
        self.assertTrue(password.is_disabled())
        self.assertIn('20735', dialog.inner_text())
        self.assertNotIn('31000', dialog.inner_text())
        dialog.get_by_label('Allow extra logins to reach the other nodes').check()
        password.fill('s3cret')
        dialog.get_by_label('At most this many logins').fill('3')
        dialog.get_by_role('button', name='Clear', exact=True).click()
        self.page.get_by_text('Done: nothing is left on the ports.').wait_for(timeout=15000)
        self.assertEqual(posted, [{'fresh_logins': True, 'password': 's3cret', 'max_logins': 3}])
        self.assertIn('every port is clear', self.page.locator('pre.file').inner_text())
        self.assertEqual(self.errors, [])

    def test_console_menu_opens_and_preserves_user_stylesheet(self):
        with mock.patch.object(agent_console, "open_with_default_application") as opener:
            self.load_console()
            toggle = self.page.locator("#console-menu > summary")
            self.assertLess(toggle.bounding_box()["x"], self.page.locator(".brand").bounding_box()["x"])
            toggle.focus()
            self.page.keyboard.press("Enter")
            self.assertTrue(self.page.locator("#console-menu").evaluate("e => e.open"))
            self.page.keyboard.press("Tab")
            self.assertEqual(self.page.evaluate("document.activeElement.id"), "open-stylesheet")
            self.page.keyboard.press("Escape")
            self.assertFalse(self.page.locator("#console-menu").evaluate("e => e.open"))
            self.assertEqual(self.page.evaluate("document.activeElement.tagName"), "SUMMARY")
            toggle.click()
            self.page.locator(".brand").click()
            self.assertFalse(self.page.locator("#console-menu").evaluate("e => e.open"))
            toggle.click()
            with self.page.expect_response(lambda r: r.url.endswith("/api/styles/open")) as result:
                self.page.get_by_role("button", name="Open Stylesheet", exact=True).click()
            self.assertEqual(result.value.status, 200)
            self.assertTrue(result.value.json()["created"])
            destination = self.console / "user.css"
            self.assertEqual(destination.read_bytes(), (ROOT / "agent-console/default-styles.css").read_bytes())
            opener.assert_called_once_with(str(destination))
            self.assertFalse(self.page.locator("#console-menu").evaluate("e => e.open"))
            destination.write_text(":root { --log-max-width: 720px; }")
            toggle.click()
            with self.page.expect_response(lambda r: r.url.endswith("/api/styles/open")) as result:
                self.page.get_by_role("button", name="Open Stylesheet", exact=True).click()
            self.assertFalse(result.value.json()["created"])
            self.assertEqual(destination.read_text(), ":root { --log-max-width: 720px; }")
            self.assertEqual(opener.call_count, 2)
            snapshot = os.environ.get("HPCLIB_UI_TEST_SCREENSHOT")
            if snapshot:
                toggle.click()
                self.page.screenshot(path=snapshot, full_page=True)
            self.assertEqual(self.errors, [])

    def test_console_menu_reports_os_open_failure(self):
        with mock.patch.object(agent_console, "open_with_default_application", side_effect=OSError("no CSS association")):
            self.load_console()
            self.page.locator("#console-menu > summary").click()
            with self.page.expect_response(lambda r: r.url.endswith("/api/styles/open")) as result:
                self.page.get_by_role("button", name="Open Stylesheet", exact=True).click()
            self.assertEqual(result.value.status, 503)
            self.page.get_by_text("could not open user.css with the OS default application", exact=False).wait_for()
            self.assertFalse(self.page.locator("#open-stylesheet").is_disabled())
            self.assertTrue((self.console / "user.css").is_file())
            self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
