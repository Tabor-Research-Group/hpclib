"""The Workspace's page in a browser, against its real server (test_workspace_package's "cluster"). The CDN is
unreachable here, so the editor falls back to a plain text box and terminals say why they can't open; with
WORKSPACE_TEST_NPM=DIR (a folder holding codemirror@5.65.18/, @xterm/xterm@5.5.0/ and @xterm/addon-fit@0.10.0/,
as on the CDN) the page gets those instead, and the terminal is tried too."""
import os
import unittest
from pathlib import Path

import test_workspace_package as pkg

try:
    from playwright.sync_api import sync_playwright
except ImportError:    # pragma: no cover
    sync_playwright = None

NPM = os.environ.get("WORKSPACE_TEST_NPM")
TYPES = {".js": "application/javascript", ".css": "text/css"}


@unittest.skipUnless(sync_playwright, "optional Playwright dependency is not installed")
class TestWorkspaceUI(pkg.ServerCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        super().setUp()
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 860})
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.route("https://cdn.example/npm/**", self.cdn)

    def cdn(self, route):
        path = route.request.url.split("/npm/", 1)[1]
        local = Path(NPM, path) if NPM else None
        if local and local.is_file():
            route.fulfill(body=local.read_bytes(), content_type=TYPES.get(local.suffix, "text/plain"))
        else:
            route.abort()

    def open(self):
        self.page.goto(f"http://127.0.0.1:{self.port}/?token={self.token}")
        self.page.locator("#files tr").first.wait_for()

    def test_browse_edit_save_and_view(self):
        self.open()
        self.assertNotIn("token=", self.page.url)                       # kept in the tab, not the address
        self.page.locator("#files tr", has_text="proj/").click()
        self.page.locator("#files tr", has_text="train.py").wait_for()
        self.assertIn("dir=", self.page.url)
        self.page.locator("#files tr", has_text="train.py").click()
        if NPM:
            editor = self.page.locator(".CodeMirror")
            editor.wait_for()
            editor.click()
            self.page.keyboard.press("End")
            self.page.keyboard.type("  # edited")
        else:
            box = self.page.locator("textarea.plain")
            box.wait_for()
            box.fill("print('edited')\n")
        self.page.locator(".tab.dirty").wait_for()
        self.page.keyboard.press("Control+s")
        self.page.locator(".tab.active:not(.dirty)").wait_for()
        text = (self.home / "proj" / "train.py").read_text()
        self.assertIn("edited", text)
        # someone else changes it: saving again asks first, and Cancel keeps theirs
        (self.home / "proj" / "train.py").write_text("theirs\n")
        os.utime(self.home / "proj" / "train.py", (1, 1))
        if NPM:
            self.page.locator(".CodeMirror").click()
            self.page.keyboard.type("!")
        else:
            self.page.locator("textarea.plain").fill("mine\n")
        self.page.get_by_role("button", name="Save", exact=True).click()
        self.page.locator("#ask[open]").wait_for()
        self.assertIn("changed on the cluster", self.page.locator("#ask-text").inner_text())
        self.page.locator("#ask button[value=cancel]").click()
        self.assertEqual((self.home / "proj" / "train.py").read_text(), "theirs\n")
        # viewers: an image, an HTML page in a sandboxed frame, a movie
        self.page.locator("#files tr", has_text="fig.svg").click()
        self.page.locator(".view:not([hidden]) img").wait_for()
        self.page.locator("#files tr", has_text="page.html").click()
        frame = self.page.locator(".view:not([hidden]) iframe")
        frame.wait_for()
        self.assertEqual(frame.get_attribute("sandbox"), "allow-scripts allow-popups")
        self.page.locator("#files tr", has_text="movie.mp4").click()
        self.page.locator(".view:not([hidden]) video").wait_for()
        self.assertEqual(len(self.page.locator(".tabs .tab").all()), 4)
        self.assertEqual(self.errors, [])

    def test_submit_a_job_from_a_template(self):
        self.open()
        self.page.locator("#files tr", has_text="proj/").click()
        row = self.page.locator("#files tr", has_text="train.py")
        row.wait_for()
        row.hover()
        row.locator("button.more").click()
        self.page.get_by_role("menuitem", name="Submit job…").click()
        form = self.page.locator(".view:not([hidden]) .submit")
        form.get_by_label("Template").wait_for()
        self.assertEqual(form.get_by_label("Template").input_value(), "python-uv")
        form.get_by_label("Job script name").wait_for()
        self.page.wait_for_function("document.querySelector('.view:not([hidden]) .submit input.name').value === 'train.slurm'")
        form.get_by_label("Template").select_option("python-venv")
        form.get_by_label("Memory per node").wait_for()
        form.get_by_label("Memory per node").fill("55G")
        form.get_by_label("Modules").fill("GCC/13.3.0 OpenMPI/5.0.3")
        self.page.wait_for_timeout(600)                      # the preview follows the form
        script = self.page.evaluate("""() => { const v = document.querySelector('.view:not([hidden]) .preview');
            const cm = v.querySelector('.CodeMirror'); return cm ? cm.CodeMirror.getValue() : v.querySelector('textarea').value; }""")
        self.assertIn("#SBATCH --mem=55G", script)
        self.assertIn("module load GCC/13.3.0 OpenMPI/5.0.3", script)
        form.get_by_role("button", name="Submit", exact=True).click()
        form.get_by_text("Submitted job 4242").wait_for()
        self.assertIn("#SBATCH --mem=55G", (self.home / "proj" / "train.slurm").read_text())
        # remembered for the next file
        self.page.locator("#jobs-list", has_text="4242").wait_for()
        self.assertEqual(self.errors, [])

    def test_disk_usage(self):
        for sub, n in (("big", 12), ("small", 2)):
            (self.home / "proj" / sub).mkdir()
            for i in range(n):
                (self.home / "proj" / sub / f"f{i}").write_text("x" * 9000)
        self.open()
        self.page.locator("#files tr", has_text="proj/").click()
        self.page.locator("#files tr", has_text="train.py").wait_for()
        self.page.get_by_role("button", name="Usage", exact=True).click()
        view = self.page.locator(".view:not([hidden]) .usage")
        view.locator(".usage-table tbody tr", has_text="big/").wait_for()
        self.assertIn("(files directly here)", view.inner_text())
        view.get_by_role("button", name="By files").click()
        first = view.locator(".usage-table tbody tr").first
        self.assertIn("big/", first.inner_text())
        self.assertIn("13", first.inner_text())                          # 12 files and the folder
        first.get_by_role("button", name="Usage").click()
        self.page.locator(".tabs .tab", has_text="Usage: big").wait_for()
        self.assertEqual(self.errors, [])

    def test_terminal(self):
        self.open()
        self.page.get_by_role("button", name="Terminal here").click()
        if not NPM:
            self.page.locator(".term-views .note", has_text="didn't load").wait_for()
            return
        self.page.locator(".xterm").wait_for()
        self.page.keyboard.type("echo hi-$((6*7))\n")
        self.page.locator(".xterm-rows", has_text="hi-42").wait_for()
        self.assertEqual(self.errors, [])

    def test_wrong_token(self):
        self.page.goto(f"http://127.0.0.1:{self.port}/?token=wrong")
        self.page.get_by_text("token is wrong").wait_for()


if __name__ == "__main__":
    unittest.main()
