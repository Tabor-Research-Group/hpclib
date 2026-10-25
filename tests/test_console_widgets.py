"""Console widgets: the packages' `widgets` section, its routes, and the page's sandbox (with Playwright)."""
import json
import time
import unittest
from pathlib import Path

import test_agent_console_ui as console_ui
from test_agent_console import ConsoleTestCase  # noqa: E402
import console_packages  # noqa: E402
from test_console_packages import make_zip  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "console-widgets" / "gpu-queue"


def widget_package(widgets=None, files=None, name="dash"):
    manifest = {"format": 1, "name": name, "version": "0.1.0",
                "widgets": widgets if widgets is not None else [
                    {"id": "queue", "title": "Queue", "slot": "home", "entry": "w/queue.html", "reads": ["jobs"]}]}
    return make_zip(manifest, files if files is not None else {"w/queue.html": "<p>queue</p>"})


class TestWidgetManifest(unittest.TestCase):
    def check(self, widgets, files=None):
        data = widget_package(widgets, files)
        raw, members = console_packages.read_zip(data)
        return console_packages.check_manifest(raw, members, set())

    def test_valid_and_summarized(self):
        m = self.check(None)
        self.assertEqual(m["widgets"], [{"id": "queue", "title": "Queue", "slot": "home", "entry": "w/queue.html",
                                         "reads": ["jobs"], "height": 200, "description": ""}])
        self.assertIn("widget Queue (queue) on the clusters page; reads jobs", console_packages.summary(m)[0])

    def test_refused(self):
        good = {"id": "q", "title": "Q", "slot": "home", "entry": "w/queue.html"}
        for bad in ([dict(good, slot="sidebar")], [dict(good, entry="w/missing.html")], [dict(good, entry="w/x.js")],
                    [dict(good, reads=["files"])], [dict(good, reads=["jobs", "jobs"])], [dict(good, height=10)],
                    [dict(good, id="Bad Id")], [good, good], [dict(good, script="x")], "not a list",
                    [dict(good, id=f"w{i}") for i in range(21)]):
            with self.subTest(bad=bad), self.assertRaises(console_packages.PackageError):
                self.check(bad)
        with self.assertRaises(console_packages.PackageError):
            self.check([good], {"w/queue.html": b"\xff\xfe not utf-8"})

    def test_example_package_builds(self):
        manifest = json.loads((EXAMPLE / "hpclib-package.json").read_text())
        files = {p.relative_to(EXAMPLE).as_posix(): p.read_bytes() for p in EXAMPLE.rglob("*") if p.is_file()
                 and p.name != "hpclib-package.json"}
        raw, members = console_packages.read_zip(make_zip(manifest, files))
        m = console_packages.check_manifest(raw, members, set())
        self.assertEqual([w["slot"] for w in m["widgets"]], ["home", "cluster.overview"])


class TestWidgetRoutes(ConsoleTestCase):
    def test_installed_widgets_and_their_html(self):
        self.assertEqual(self.call("GET", "/api/widgets")[1]["widgets"], [])
        self.clusters.package_add(widget_package())
        status, out = self.call("GET", "/api/widgets")
        self.assertEqual(status, 200)
        self.assertEqual([(w["key"], w["slot"], w["reads"]) for w in out["widgets"]], [("dash/queue", "home", ["jobs"])])
        status, out = self.call("GET", "/api/widgets/dash/queue")
        self.assertEqual((status, out["html"]), (200, "<p>queue</p>"))
        self.assertEqual(self.call("GET", "/api/widgets/dash/nope")[0], 404)
        self.assertEqual(self.call("GET", "/api/widgets/dash/queue", key=None)[0], 401)
        # shown or hidden, and their order: a preference
        self.assertEqual(self.call("PUT", "/api/preferences", {"widgets": {"hidden": ["dash/queue"]}})[0], 200)
        self.assertEqual(self.call("PUT", "/api/preferences", {"widgets": {"hidden": ["../x"]}})[0], 422)


WIDGET = """<!doctype html><div id="out">waiting</div><script type="module">
  const hpc = await hpclibWidget();
  document.getElementById("out").dataset.cluster = hpc.context.cluster || "";
  hpc.subscribe("jobs", (v) => { document.getElementById("out").textContent = "jobs " + Object.keys(v.byCluster).join(","); });
  hpc.subscribe("storage", () => { document.getElementById("out").dataset.storage = "leaked"; });
  addEventListener("hpclib-error", (e) => { document.getElementById("out").dataset.error = e.detail.topic; });
  window.go = (h) => hpc.navigate(h);
</script>"""


@unittest.skipUnless(console_ui.sync_playwright, "optional Playwright dependency is not installed")
class TestWidgetsUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        console_ui.TestConsoleUI.setUpClass.__func__(cls)

    def setUp(self):
        console_ui.TestConsoleUI.setUp(self)
        (self.console / "preferences.json").unlink(missing_ok=True)
        self.jobs_calls = 0
        self.widgets = [{"key": "dash/queue", "id": "queue", "package": "dash", "version": "0.1.0", "title": "Queue",
                         "slot": "home", "entry": "w/queue.html", "reads": ["jobs"], "height": 120, "description": ""},
                        {"key": "dash/tab", "id": "tab", "package": "dash", "version": "0.1.0", "title": "Queue tab",
                         "slot": "cluster.tab", "entry": "w/queue.html", "reads": ["jobs"], "height": 120,
                         "description": ""}]

    def handler(self, route, path):
        if path == "preferences":
            route.continue_()
            return True
        demo = {"name": "demo", "host": "me@demo", "login": {"state": "connected"}, "tunnel": {"state": "down"}}
        if path == "clusters/demo/jobs":
            self.jobs_calls += 1
        answers = {"widgets": {"widgets": self.widgets}, "clusters": {"clusters": [demo]}, "clusters/demo": demo,
                   "health": {"hpclib_version": "1.0.0"}, "apps": {"apps": []},
                   "clusters/demo/jobs": {"at": time.time(), "queued": [], "running": [], "finished": [],
                                          "counts": {"queued": 0, "running": 0, "finished": 0, "failed": 0}},
                   "widgets/dash/queue": {"html": WIDGET}, "widgets/dash/tab": {"html": WIDGET},
                   "widgets/dash/second": {"html": WIDGET}}
        if path in answers:
            route.fulfill(body=json.dumps(answers[path]), content_type="application/json")
            return True
        return False

    def wait_in(self, frame, expression, timeout=10):
        # frame.wait_for_function evaluates a string, which the widget's CSP refuses (no unsafe-eval)
        end = time.time() + timeout
        while time.time() < end:
            try:
                if frame.evaluate(expression):
                    return
            except Exception:       # noqa: BLE001  (still about:blank, or navigating)
                pass
            self.page.wait_for_timeout(50)
        self.fail(f"not true in the widget: {expression}: " + str([f.evaluate("document.documentElement.outerHTML.slice(-300)") for f in self.page.frames[1:]]))

    def widget_frame(self, n=0):
        self.page.wait_for_function(f"document.querySelectorAll('iframe.widget-frame').length > {n}")
        for _ in range(100):
            frames = [f for f in self.page.frames if f != self.page.main_frame]
            if len(frames) > n:
                return frames[n]
            self.page.wait_for_timeout(50)
        self.fail("no widget frame")

    def test_home_widget_gets_its_topics_and_nothing_else(self):
        console_ui.TestConsoleUI.load_console(self, self.handler)
        frame = self.widget_frame()
        self.wait_in(frame, "document.getElementById('out')?.textContent === 'jobs demo'")
        self.wait_in(frame, "document.getElementById('out')?.dataset.error === 'storage'")   # not in its reads
        self.assertIsNone(frame.evaluate("document.getElementById('out').dataset.storage || null"))
        # sandboxed: no storage (the session key), no parent, no network
        self.assertEqual(frame.evaluate("(() => { try { return localStorage.getItem('agent-console-key'); } "
                                        "catch (e) { return 'blocked'; } })()"), "blocked")
        self.assertEqual(frame.evaluate("(() => { try { return parent.document.title; } catch (e) { return 'blocked'; } })()"),
                         "blocked")
        self.assertEqual(frame.evaluate("fetch('/api/clusters').then(() => 'reached', () => 'blocked')"), "blocked")
        # navigation within the console only, never a #key= address
        frame.evaluate("go('#key=attacker')")
        self.page.wait_for_timeout(200)
        self.assertNotIn("key=", self.page.url)
        frame.evaluate("go('#/c/demo/jobs')")
        self.page.wait_for_function("location.hash === '#/c/demo/jobs'")
        self.assertEqual(self.errors, [])

    def test_one_fetch_for_every_widget_and_a_tab_and_hiding(self):
        self.widgets.append(dict(self.widgets[0], key="dash/second", id="second", title="Second"))
        console_ui.TestConsoleUI.load_console(self, self.handler)
        self.page.wait_for_function("document.querySelectorAll('iframe.widget-frame').length === 2")
        for f in [self.widget_frame(0), self.widget_frame(1)]:
            self.wait_in(f, "document.getElementById('out')?.textContent === 'jobs demo'")
        self.assertEqual(self.jobs_calls, 2)          # the page's own Jobs column, and one for both widgets
        self.page.goto(self.base + "/#/c/demo/overview")
        self.page.locator("#pages").get_by_role("link", name="Queue tab", exact=True).click()
        tab = self.widget_frame(0)
        self.wait_in(tab, "document.getElementById('out')?.dataset.cluster === 'demo'")
        self.assertEqual(self.page.locator("#pages a.active").inner_text(), "Queue tab")
        # hidden on Interface Settings: gone from its place
        self.page.goto(self.base + "/#/interface")
        self.page.get_by_label("Show Second", exact=True).uncheck()
        self.page.wait_for_function("document.querySelector('[aria-label=\"Show Second\"]') && "
                                    "!document.querySelector('[aria-label=\"Show Second\"]').checked")
        self.page.wait_for_timeout(300)
        prefs = json.loads((self.console / "preferences.json").read_text())
        self.assertEqual(prefs["widgets"]["hidden"], ["dash/second"])
        self.page.goto(self.base + "/#/c")
        self.page.wait_for_function("document.querySelectorAll('iframe.widget-frame').length === 1")
        self.assertEqual(self.page.locator("[data-widget]").get_attribute("data-widget"), "dash/queue")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
