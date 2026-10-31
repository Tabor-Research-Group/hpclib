"""The Workspace's page in a browser, against its real server (test_workspace_package's "cluster") and the page as the
package ships it (the package is built and unpacked first, so hpclib-ui/ is the bundled copy). The CDN is unreachable
here, so terminals say why they can't open (the editor is CodeMirror 6, which the package carries itself); with
WORKSPACE_TEST_NPM=DIR (a folder holding the packages libs.json pins, laid out as on the CDN: @xterm/xterm@6.0.0/,
@xterm/addon-fit@0.11.0/, @xterm/addon-web-links@0.12.0/ and wunderbaum@0.14.1/) the page gets those instead, and the
terminal and the file tree are tried too."""
import json
import os
import re
import tempfile
import time
import unittest
import unittest.mock
import zipfile
from pathlib import Path

import test_workspace_package as pkg

try:
    from playwright.sync_api import sync_playwright
except ImportError:    # pragma: no cover
    sync_playwright = None

NPM = os.environ.get("WORKSPACE_TEST_NPM")
TYPES = {".js": "application/javascript", ".css": "text/css"}


class TestWorkspaceUI(pkg.SubmissionCase):
    @classmethod
    def setUpClass(cls):
        cls.built = tempfile.TemporaryDirectory()
        out, _ = pkg.console_packages.build(str(pkg.PACKAGE), str(Path(cls.built.name) / "workspace.zip"))
        with zipfile.ZipFile(out) as z:
            z.extractall(Path(cls.built.name) / "unpacked")
        cls.ui_dir = str(Path(cls.built.name) / "unpacked" / "tunnels" / "workspace" / "ui")
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.built.cleanup()

    def setUp(self):
        super().setUp()
        patch = unittest.mock.patch.object(pkg.workspace_server, "UI_DIR", self.ui_dir)
        patch.start()
        self.addCleanup(patch.stop)
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
        self.page.get_by_role("button", name="Details", exact=True).click()
        self.page.locator("#files tr").first.wait_for()

    def test_browse_edit_save_and_view(self):
        self.open()
        self.assertNotIn("token=", self.page.url)                       # kept in the tab, not the address
        self.page.locator("#files tr", has_text="proj/").click()
        self.page.locator("#files tr", has_text="train.py").wait_for()
        self.assertIn("dir=", self.page.url)
        self.page.locator("#files tr", has_text="train.py").click()
        editor = self.page.locator("hpc-code .cm-content")
        editor.wait_for()
        editor.click()
        self.page.keyboard.press("End")
        self.page.keyboard.type("  # edited")
        self.page.locator(".hpc-tab.dirty").wait_for()
        self.assertNotIn("null", self.page.locator(".view:not([hidden]) .bar").first.inner_text())   # a missing part isn't text
        self.page.keyboard.press("Control+s")
        self.page.locator(".hpc-tab.active:not(.dirty)").wait_for()
        text = (self.home / "proj" / "train.py").read_text()
        self.assertIn("edited", text)
        # someone else changes it: saving again asks first, and Cancel keeps theirs
        (self.home / "proj" / "train.py").write_text("theirs\n")
        os.utime(self.home / "proj" / "train.py", (1, 1))
        self.page.locator("hpc-code .cm-content").click()
        self.page.keyboard.type("!")
        self.page.get_by_role("button", name="Save", exact=True).click()
        self.page.get_by_text("changed on the cluster").wait_for()
        self.page.locator("hpc-dialog").get_by_role("button", name="Cancel").click()
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
        self.assertEqual(len(self.page.locator("#tabs .hpc-tab").all()), 4)
        self.assertEqual(self.errors, [])

    def test_a_link_to_a_file_opens_it_with_its_folder_shown(self):
        (self.home / "proj" / "lines.py").write_text("".join(f"x{i} = {i}\n" for i in range(1, 80)))
        self.page.goto(f"http://127.0.0.1:{self.port}/?token={self.token}#file={self.home}/proj/lines.py&line=40")
        self.page.locator(".hpc-tab.active", has_text="lines.py").wait_for()      # its tab, in front
        self.page.get_by_role("button", name="Details", exact=True).click()
        self.page.locator("#files tr", has_text="train.py").wait_for()             # its folder, in the browser
        self.page.wait_for_function("document.querySelector('.view:not([hidden]) hpc-code .cm-content')")
        line = self.page.evaluate("""() => { const v = document.querySelector('.view:not([hidden]) hpc-code').view;
            return v.state.doc.lineAt(v.state.selection.main.head).number; }""")
        self.assertEqual(line, 40)
        # a file that isn't there says so in its tab and the page carries on
        self.page.goto(f"http://127.0.0.1:{self.port}/?token={self.token}#file={self.home}/proj/gone.py")
        self.page.locator(".view:not([hidden]) .status.error").wait_for()
        self.assertEqual(self.errors, [])

    def test_the_editor_highlights_and_undo_returns_to_saved(self):
        self.open()
        self.page.locator("#files tr", has_text="proj/").click()
        self.page.locator("#files tr", has_text="train.py").wait_for()
        self.page.locator("#files tr", has_text="train.py").click()
        editor = self.page.locator(".view:not([hidden]) hpc-code .cm-content")
        editor.wait_for()
        self.assertFalse(self.page.evaluate("document.querySelector('.view:not([hidden]) hpc-code').plain"))
        # Python is coloured: some words are in spans the highlighter classed
        self.page.wait_for_function("document.querySelectorAll('.view:not([hidden]) hpc-code .cm-line span').length > 0")
        before = self.page.evaluate("document.querySelector('.view:not([hidden]) hpc-code').value")
        editor.click()
        self.page.keyboard.press("End")
        self.page.keyboard.type("  # edited")
        self.page.locator(".hpc-tab.dirty").wait_for()
        for _ in range(len("  # edited")):
            self.page.keyboard.press("Control+z")
        self.page.locator(".hpc-tab.active:not(.dirty)").wait_for()        # back to what was saved: clean again
        self.assertEqual(self.page.evaluate("document.querySelector('.view:not([hidden]) hpc-code').value"), before)
        # the theme follows the page
        light = self.page.evaluate("getComputedStyle(document.querySelector('.view:not([hidden]) hpc-code .cm-editor')).backgroundColor")
        self.page.emulate_media(color_scheme="dark")
        self.page.wait_for_timeout(300)
        dark = self.page.evaluate("getComputedStyle(document.querySelector('.view:not([hidden]) hpc-code .cm-editor')).backgroundColor")
        self.assertNotEqual(light, dark)
        self.assertEqual(self.errors, [])

    def test_submit_a_job_from_a_classic_template(self):
        self.open()
        self.page.locator("#files tr", has_text="proj/").click()
        row = self.page.locator("#files tr", has_text="train.py")
        row.wait_for()
        row.hover()
        row.get_by_role("button", name="More for train.py").click()
        self.page.get_by_role("menuitem", name="Submit job…").click()
        self.page.get_by_role("button", name="Classic templates…").click()
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
            return v.querySelector('hpc-code').value; }""")
        self.assertIn("#SBATCH --mem=55G", script)
        self.assertIn("module load GCC/13.3.0 OpenMPI/5.0.3", script)
        form.get_by_role("button", name="Submit", exact=True).click()
        form.get_by_text("Submitted job 4242").wait_for()
        self.assertIn("#SBATCH --mem=55G", (self.home / "proj" / "train.slurm").read_text())
        # remembered for the next file
        self.assertEqual(self.errors, [])

    def open_submit(self, name="train.py"):
        self.open()
        self.page.locator("#files tr", has_text="proj/").click()
        row = self.page.locator("#files tr", has_text=name)
        row.wait_for()
        row.hover()
        row.get_by_role("button", name=f"More for {name}").click()
        self.page.get_by_role("menuitem", name="Submit job…").click()
        form = self.page.locator(".view:not([hidden]) .submit")
        form.get_by_label("How the job gets its environment").select_option("none")
        return form

    def preview_text(self):
        return self.page.evaluate("""() => { const v = document.querySelector('.view:not([hidden]) .preview');
            return v.querySelector('hpc-code').value; }""")

    def test_submit_a_sweep_after_another_job_and_keep_a_profile(self):
        form = self.open_submit()
        form.locator("summary", has_text="Many tasks").click()
        form.get_by_role("button", name="Add a variable").click()
        form.get_by_label("Variable name").fill("LR")
        form.get_by_label("Values").fill("0.1 0.01 0.001")
        form.get_by_text("3 tasks").wait_for()
        form.get_by_label("Arguments").fill("--lr {LR}")
        form.get_by_label("Tasks at once").fill("2")
        form.locator("summary", has_text="Start after").click()
        form.get_by_label("Job numbers").fill("777")
        self.page.wait_for_timeout(700)
        script = self.preview_text()
        for line in ("#SBATCH --array=0-2%2", "#SBATCH --dependency=afterok:777", '--lr "${LR}"'):
            self.assertIn(line, script)
        # a profile keeps the resources, never the script, the sweep or the dependency
        form.get_by_label("Time limit").fill("3h")
        form.get_by_role("button", name="Save as profile…").click()
        dialog = self.page.locator("hpc-dialog")
        dialog.get_by_role("textbox").fill("three hours")
        dialog.get_by_role("button", name="OK").click()
        form.get_by_label("Profile").locator("option", has_text="three hours").wait_for(state="attached")
        form.get_by_role("button", name="Submit", exact=True).click()
        form.get_by_text("Submitted job 4242").wait_for()
        self.assertIn("#SBATCH --array=0-2%2", (self.home / "proj" / "train.slurm").read_text())
        self.assertTrue((self.home / ".local" / "tunnels" / "workspace" / "profiles" / "three hours.json").exists())
        self.assertNotIn("sweep", (self.home / ".local" / "tunnels" / "workspace" / "profiles" / "last.json").read_text())
        self.assertEqual(self.errors, [])

    HAND_SCRIPT = "#!/bin/bash\n#SBATCH -J mine\n#SBATCH --time=01:00:00\n#SBATCH --mail-type=END\n\nmodule load GCC\npython train.py\n"

    def open_job_form(self, name="mine.slurm"):
        (self.home / "proj" / name).write_text(self.HAND_SCRIPT)
        self.open()
        self.page.locator("#files tr", has_text="proj/").click()
        row = self.page.locator("#files tr", has_text=name)
        row.wait_for()
        row.hover()
        row.get_by_role("button", name=f"More for {name}").click()
        self.page.get_by_role("menuitem", name="Open as job form…").click()
        return self.page.locator(".view:not([hidden]) .submit")

    def test_a_hand_written_script_opens_in_the_form_and_can_be_kept(self):
        form = self.open_job_form()
        form.get_by_text("This script was written by hand").wait_for()
        self.assertEqual(form.get_by_label("Job name").input_value(), "mine")
        self.assertEqual(form.get_by_label("Time limit").input_value(), "1:00:00")
        diff = form.locator("hpc-diff")
        diff.locator(".cm-content").wait_for()
        self.assertIn("#SBATCH --chdir=", " ".join(diff.locator(".cm-changedLine").all_inner_texts()))   # what the form's version adds
        self.assertIn("#SBATCH -J mine", " ".join(diff.locator(".cm-deletedChunk").all_inner_texts()))   # and what it words differently
        self.assertEqual(form.get_by_label("Arguments").count(), 0)         # the script is its own program
        form.get_by_role("button", name="Keep mine").click()
        self.assertIn("python train.py", self.preview_text())
        form.get_by_role("button", name="Submit", exact=True).click()
        form.get_by_text("Submitted job 4242").wait_for()
        self.assertEqual((self.home / "proj" / "mine.slurm").read_text(), self.HAND_SCRIPT)       # sent as it lies
        self.assertTrue((self.home / "sbatch.log").read_text().startswith("sbatch mine.slurm"))
        self.assertEqual(self.errors, [])

    def test_a_hand_written_script_can_be_adopted_into_the_form(self):
        form = self.open_job_form()
        form.get_by_label("Time limit").fill("3h")
        form.get_by_role("button", name="Adopt the form's version").click()
        form.get_by_text("original is mine.slurm.bak").wait_for()
        self.assertEqual((self.home / "proj" / "mine.slurm.bak").read_text(), self.HAND_SCRIPT)
        written = (self.home / "proj" / "mine.slurm").read_text()
        for line in ("#HPCLIB-SPEC", "#SBATCH --time=3:00:00", "#SBATCH --mail-type=END", "python train.py"):
            self.assertIn(line, written)
        self.assertEqual(self.errors, [])

    @unittest.skipUnless(NPM, "terminals need xterm.js: provide WORKSPACE_TEST_NPM")
    def test_an_interactive_session_opens_a_terminal_with_the_srun_line(self):
        form = self.open_submit()
        form.get_by_label("Time limit").fill("2h")
        form.get_by_role("button", name="Interactive session").click()
        self.page.locator(".xterm-rows", has_text="srun --job-name=train --time=2:00:00").wait_for(timeout=20000)
        self.assertEqual(self.errors, [])

    def test_guided_submit_from_file_to_followed_output(self):
        self.open()
        self.page.locator("#files tr", has_text="proj/").click()
        row = self.page.locator("#files tr", has_text="train.py")
        row.wait_for()
        row.hover()
        row.get_by_role("button", name="More for train.py").click()
        self.page.get_by_role("menuitem", name="Submit job…").click()
        form = self.page.locator(".view:not([hidden]) .submit")
        form.locator("hpc-state", has_text="not prepared").wait_for()
        form.get_by_text("prepare it above").wait_for()
        self.assertTrue(form.get_by_role("button", name="Submit", exact=True).is_disabled())
        form.get_by_role("button", name="Prepare", exact=True).click()
        form.locator("hpc-state", has_text="ready").wait_for()
        form.get_by_label("Time limit").fill("1d 2h")
        form.get_by_text("= 1-02:00:00").wait_for()
        form.get_by_label("Memory", exact=True).fill("16G")
        form.get_by_label("Partition").fill("gpu")
        form.get_by_label("GPUs", exact=True).fill("1")
        form.get_by_label("GPU type").fill("a100")
        form.get_by_role("button", name="Check", exact=True).click()
        form.get_by_text("Slurm would take it").wait_for()
        form.get_by_role("button", name="Submit and follow").click()
        form.get_by_text("Submitted job 4242").wait_for()
        script = (self.home / "proj" / "train.slurm").read_text()
        for line in ("#SBATCH --time=1-02:00:00", "#SBATCH --mem=16G", "#SBATCH --gres=gpu:a100:1", "trap hpc_term TERM"):
            self.assertIn(line, script)
        (self.home / "proj" / "train-4242.out").write_text("epoch 1\n")
        self.page.locator(".follow-log", has_text="epoch 1").wait_for()
        self.page.locator("#dock-views hpc-state", has_text="running").wait_for()
        # the run appears under the file list, and a second line of output follows into the open tab
        run = self.page.locator("#runs-slot .run", has_text="4242")
        run.wait_for()
        with open(self.home / "proj" / "train-4242.out", "a") as f:
            f.write("epoch 2\n")
        self.page.locator(".follow-log", has_text="epoch 2").wait_for(timeout=15000)
        self.assertEqual(self.errors, [])

    def test_runs_from_here_follow_cancel_and_scope(self):
        (self.home / "other").mkdir()
        record_dir = self.home / ".local" / "tunnels" / "workspace" / "runs"
        record_dir.mkdir(parents=True)
        for job, workdir, name in (("4242", self.home / "proj", "train"), ("4300", self.home / "other", "elsewhere")):
            (record_dir / f"{job}.json").write_text(json.dumps({
                "job_id": job, "submitted": time.time() - 60, "script": str(workdir / f"{name}.slurm"), "workdir": str(workdir),
                "output": str(workdir / f"{name}-{job}.out"), "spec": {"name": name, "resources": {"partition": "gpu"}}}))
        (self.home / "proj" / "train-4242.out").write_text("epoch 1\n")
        self.page.goto(f"http://127.0.0.1:{self.port}/?token={self.token}#dir={self.home / 'proj'}")
        self.page.locator("#runs-slot .run", has_text="train").wait_for()
        self.assertEqual(self.page.locator("#runs-slot .run").count(), 1)             # from here: proj/ and below
        self.page.get_by_role("button", name="All", exact=True).click()
        self.page.locator("#runs-slot .run", has_text="elsewhere").wait_for()
        self.assertEqual(self.page.locator("#runs-slot .run").count(), 2)
        self.page.locator("#runs-slot .run", has_text="train").get_by_role("button", name="Follow").click()
        self.page.locator(".follow-log", has_text="epoch 1").wait_for()
        self.page.locator("#runs-slot .run", has_text="elsewhere").get_by_role("button", name="Cancel").click()
        self.page.locator("hpc-dialog").get_by_role("button", name="Cancel job").click()
        deadline = time.time() + 10
        while not (self.home / "scancel.log").exists() and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual((self.home / "scancel.log").read_text().strip(), "4300")
        self.assertEqual(self.errors, [])

    def test_a_followed_file_notices_when_it_is_replaced(self):
        log = self.home / "proj" / "build.log"
        log.write_text("first run\n" * 5)
        self.page.goto(f"http://127.0.0.1:{self.port}/?token={self.token}")
        self.page.wait_for_function("window.ws && window.ws.dock")
        self.page.evaluate("path => window.ws.dockOpen('follow', { output: path })", str(log))
        self.page.locator(".follow-log", has_text="first run").wait_for()
        log.rename(self.home / "proj" / "build.old")
        log.write_text("second run, a fresh file\n")
        self.page.locator(".follow-log", has_text="second run").wait_for(timeout=15000)
        self.assertNotIn("first run", self.page.locator(".follow-log").inner_text())
        self.assertEqual(self.errors, [])

    @unittest.skipUnless(NPM, "terminals need xterm.js: provide WORKSPACE_TEST_NPM")
    def test_a_terminal_survives_a_reload_and_a_dropped_connection(self):
        self.open()
        self.page.get_by_role("button", name="Terminal here").click()
        self.page.locator(".xterm").wait_for()
        self.page.keyboard.type("export KEPT=still-here; echo before-$((6*7))\n")
        self.page.locator(".xterm-rows", has_text="before-42").wait_for()
        self.page.reload()                                                      # the page goes, the shell stays
        self.page.locator(".xterm-rows", has_text="before-42").wait_for(timeout=20000)
        self.assertEqual(len(self.server.workspace.terminals.listing()), 1)
        self.page.locator(".xterm").click()
        self.page.keyboard.type("echo after-$KEPT\n")
        self.page.locator(".xterm-rows", has_text="after-still-here").wait_for()
        # the connection drops (a tunnel that fell over): the terminal connects again by itself
        self.page.evaluate("window.__before = document.querySelector('hpc-terminal').ws; window.__before.close()")
        self.page.wait_for_function("""() => { const t = document.querySelector('hpc-terminal');
            return t.ws !== window.__before && t.ws.readyState === 1 && t.getAttribute('state') === 'connected'; }""", timeout=20000)
        self.page.locator(".xterm").click()
        self.page.keyboard.type("echo back-$((7*6))\n")
        self.page.locator(".xterm-rows", has_text="back-42").wait_for(timeout=20000)
        self.assertEqual(len(self.server.workspace.terminals.listing()), 1)
        # closing the tab ends the shell
        self.page.locator("#dock-tabs .hpc-tab").first.get_by_role("button", name="Close").click()
        deadline = time.time() + 10
        while self.server.workspace.terminals.listing() and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.server.workspace.terminals.listing(), [])
        self.assertEqual(self.errors, [])

    def test_unsaved_changes_come_back_after_a_reload(self):
        self.open()
        self.page.locator("#files tr", has_text="proj/").click()
        self.page.locator("#files tr", has_text="train.py").click()
        editor = self.page.locator("hpc-code .cm-content")
        editor.wait_for()
        editor.click()
        self.page.keyboard.press("End")
        self.page.keyboard.type("  # not saved yet")
        self.page.locator(".hpc-tab.dirty").wait_for()
        self.page.wait_for_timeout(1500)                                        # kept a second after the last key
        self.page.on("dialog", lambda d: d.accept())                           # the "leave this page?" question
        self.page.reload()
        self.page.locator(".hpc-tab.dirty").wait_for(timeout=20000)
        self.page.get_by_text("restored the unsaved changes").wait_for()
        self.assertEqual((self.home / "proj" / "train.py").read_text(), "print('hi')\n")     # never written
        # saving it makes it the file's, and nothing is restored the next time
        self.page.keyboard.press("Control+s")
        self.page.locator(".hpc-tab.active:not(.dirty)").wait_for()
        self.assertNotEqual((self.home / "proj" / "train.py").read_text(), "print('hi')\n")
        self.page.reload()
        self.page.locator("#tabs .hpc-tab.active", has_text="train.py").wait_for(timeout=20000)
        self.page.wait_for_timeout(800)
        self.assertEqual(self.page.locator(".hpc-tab.dirty").count(), 0)
        self.assertEqual(self.errors, [])

    def test_layout_is_kept_per_user_and_host(self):
        self.open()
        self.page.locator("#files tr", has_text="proj/").click()
        self.page.locator("#files tr", has_text="fig.svg").click()
        self.page.locator("#tabs .hpc-tab", has_text="fig.svg").wait_for()
        keys = self.page.evaluate("Object.keys(localStorage).filter((k) => k.startsWith('ws.tabs'))")
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].startswith("ws.tabs:jdoe@"), keys)

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
        self.page.locator("#tabs .hpc-tab", has_text="Usage: big").wait_for()
        self.assertEqual(self.errors, [])

    def test_terminal(self):
        self.open()
        self.page.get_by_role("button", name="Terminal here").click()
        if not NPM:
            self.page.locator("hpc-terminal .terminal-note", has_text="didn't load").wait_for()
            return
        self.page.locator(".xterm").wait_for()
        self.page.keyboard.type("echo hi-$((6*7))\n")
        self.page.locator(".xterm-rows", has_text="hi-42").wait_for()
        self.assertEqual(self.errors, [])

    def test_theme_comes_from_the_consoles_link(self):
        console = f"http://127.0.0.1:{self.port + 1}"          # nothing listens: a stylesheet that fails changes nothing
        self.page.goto(f"http://127.0.0.1:{self.port}/?token={self.token}#theme=dark&console={console}")
        self.page.locator("#files .file-controls").wait_for()
        self.assertEqual(self.page.evaluate("document.documentElement.dataset.theme"), "dark")
        self.assertEqual(self.page.evaluate("getComputedStyle(document.body).colorScheme"), "dark")
        self.assertNotIn("theme=", self.page.url)                # the address doesn't carry it on
        self.assertEqual(self.page.locator("#console-css").get_attribute("href"), console + "/user.css")
        # the choice is kept for the next visit, and "system" forgets it
        self.page.goto(f"http://127.0.0.1:{self.port}/?again=1")
        self.assertEqual(self.page.evaluate("document.documentElement.dataset.theme"), "dark")
        self.page.goto(f"http://127.0.0.1:{self.port}/?again=2#theme=system")
        self.assertIsNone(self.page.evaluate("document.documentElement.dataset.theme"))
        self.assertEqual(self.errors, [])

    def test_only_a_loopback_console_gives_a_stylesheet(self):
        self.page.goto(f"http://127.0.0.1:{self.port}/#console=https://evil.example")
        self.assertEqual(self.page.locator("#console-css").count(), 0)
        self.page.goto(f"http://127.0.0.1:{self.port}/?again=4#console=http://127.0.0.1.evil.example:80")
        self.assertEqual(self.page.locator("#console-css").count(), 0)

    def test_the_page_follows_the_themes_variables(self):
        self.page.goto(f"http://127.0.0.1:{self.port}/?token={self.token}#theme=dark")
        self.page.locator("#files .file-controls").wait_for()
        dark = self.page.evaluate("getComputedStyle(document.body).backgroundColor")
        self.page.goto(f"http://127.0.0.1:{self.port}/?again=3#theme=light")
        self.page.locator("#files .file-controls").wait_for()
        self.assertNotEqual(dark, self.page.evaluate("getComputedStyle(document.body).backgroundColor"))

    def test_opening_twice_shows_one_tab(self):
        self.open()
        self.page.locator("#files tr", has_text="proj/").click()
        row = self.page.locator("#files tr", has_text="fig.svg")
        row.wait_for()
        row.dblclick()                                           # two opens before the first has finished
        self.page.locator("#views .view:not([hidden]) img").wait_for()
        self.assertEqual(len(self.page.locator("#tabs .hpc-tab").all()), 1)
        self.assertEqual(self.errors, [])

    def test_tabs_are_keyboard_reachable_and_restored(self):
        self.open()
        self.page.locator("#files tr", has_text="proj/").click()
        self.page.locator("#files tr", has_text="train.py").click()
        self.page.locator("#files tr", has_text="fig.svg").click()
        self.page.locator("#tabs .hpc-tab", has_text="fig.svg").wait_for()
        self.assertEqual(self.page.locator("#tabs [role=tablist]").get_attribute("aria-label"), "Open files")
        self.page.locator("#tabs .hpc-tab.active").focus()
        self.page.keyboard.press("ArrowLeft")
        self.page.locator("#tabs .hpc-tab.active", has_text="train.py").wait_for()
        self.page.reload()
        self.page.locator("#tabs .hpc-tab", has_text="fig.svg").wait_for()
        self.assertEqual(len(self.page.locator("#tabs .hpc-tab").all()), 2)
        self.page.locator("#tabs .hpc-tab.active", has_text="train.py").wait_for()

    def test_the_menu_is_a_popover_with_the_registered_actions(self):
        self.open()
        self.page.locator("#files tr", has_text="proj/").click()
        row = self.page.locator("#files tr", has_text="train.py")
        row.wait_for()
        row.click(button="right")
        menu = self.page.locator("hpc-menu:popover-open")
        menu.wait_for()
        names = [b.inner_text() for b in menu.locator("[role=menuitem]").all()]
        for wanted in ("Open", "Open as text", "Submit job…", "Terminal in its directory", "Download", "Delete…"):
            self.assertIn(wanted, names)
        self.assertNotIn("Disk usage", names)                    # that one is for folders
        self.page.keyboard.press("Escape")
        self.page.wait_for_selector("hpc-menu:popover-open", state="detached")

    def test_the_dock_splitter_and_side_splitter_resize_with_the_keyboard(self):
        self.open()
        divider = self.page.locator("#side-split > .hpc-split-divider")
        before = self.page.locator("#side-split > aside").bounding_box()["width"]
        divider.focus()
        self.page.keyboard.press("ArrowRight")
        after = self.page.locator("#side-split > aside").bounding_box()["width"]
        self.assertGreater(after, before)
        self.page.reload()                                       # the size is remembered
        self.page.locator("#files tr").first.wait_for()
        self.assertEqual(self.page.locator("#side-split > aside").bounding_box()["width"], after)

    def test_every_shared_element_can_be_created_by_script(self):
        # document.createElement refuses an element whose constructor sets attributes: it must not, or el() breaks
        self.open()
        bad = self.page.evaluate("""() => ['hpc-panel', 'hpc-log', 'hpc-dialog', 'hpc-state', 'hpc-field', 'hpc-action-button',
            'hpc-env-editor', 'hpc-tabs', 'hpc-split', 'hpc-menu', 'hpc-toasts', 'hpc-code', 'hpc-terminal']
            .filter((tag) => { try { return document.createElement(tag).constructor === HTMLElement
                                          || document.createElement(tag) instanceof HTMLUnknownElement; }
                               catch (e) { return true; } })""")
        self.assertEqual(bad, [])

    def test_toasts_are_one_live_region(self):
        self.open()
        self.page.evaluate("import('/ui/hpclib-ui/index.js').then((m) => m.toast('hello there'))")
        self.page.locator("hpc-toasts .toast", has_text="hello there").wait_for()
        self.assertEqual(self.page.locator("hpc-toasts").get_attribute("aria-live"), "polite")

    def test_wrong_token(self):
        self.page.goto(f"http://127.0.0.1:{self.port}/?token=wrong")
        self.page.get_by_text("token is wrong").wait_for()

    def test_basic_tree_preserves_branches_and_switches_to_details(self):
        self.page.goto(f"http://127.0.0.1:{self.port}/?token={self.token}")
        if NPM:
            self.page.get_by_role("button", name="Tree", exact=True).click()
        self.page.locator("#files summary, #files .wb-title", has_text="proj").first.click()
        self.page.get_by_role("button", name="train.py", exact=True).or_(self.page.locator("#files .wb-title", has_text="train.py")).first.click()
        self.page.locator("#tabs .hpc-tab.active", has_text="train.py").wait_for()
        self.assertTrue(self.page.locator("#path").input_value().endswith("/proj"))
        self.page.get_by_role("button", name="Details", exact=True).click()
        self.page.locator("#files tr", has_text="train.py").wait_for()
        self.page.get_by_label("Hidden files").check()
        self.page.locator("#files tr", has_text=".hidden").wait_for()
        self.page.get_by_role("button", name="Tree", exact=True).click()
        self.assertEqual(self.errors, [])

    @unittest.skipUnless(NPM and (Path(NPM) / "wunderbaum@0.14.1/dist/wunderbaum.umd.min.js").is_file(),
                         "provide the pinned Wunderbaum package in WORKSPACE_TEST_NPM")
    def test_file_filter_takes_wildcards_for_files_and_folders(self):
        proj = self.home / "proj"
        for d in ("sub", "empty", "docs", "deep/a"):
            (proj / d).mkdir(parents=True, exist_ok=True)
        for name in ("run_1.sh", "run_2.py", "notes.md", "data.json", "sub/inner.log", "docs/readme.md", "deep/a/model.py"):
            (proj / name).write_text("x")
        self.page.goto(f"http://127.0.0.1:{self.port}/?token={self.token}#dir={proj}")
        self.page.get_by_role("button", name="Tree", exact=True).click()
        title = lambda n: self.page.locator("#files .wb-title", has_text=re.compile(f"^{re.escape(n)}$"))
        title("notes.md").wait_for()
        self.page.fill("#file-filter", "run_*  *.json")
        self.page.wait_for_function("!document.body.innerText.includes('notes.md')")
        for name in ("run_1.sh", "run_2.py", "data.json"):
            self.assertEqual(title(name).count(), 1, name)
        # a folder with nothing that matches, inside it at any depth, goes (and one with nothing in it at all)
        for gone in ("sub", "empty", "docs", "deep"):
            self.page.wait_for_function(f"![...document.querySelectorAll('#files .wb-title')].some((e) => e.textContent === '{gone}')")
        self.assertIn("match", self.page.locator("#files .file-status").inner_text())
        # a folder whose name matches stays when it isn't empty; one holding a match deeper down stays, whatever its name
        self.page.fill("#file-filter", "su*")
        title("sub").wait_for()
        self.assertEqual(title("run_1.sh").count(), 0)
        self.page.fill("#file-filter", "*.py")
        title("deep").wait_for()
        self.page.wait_for_function("![...document.querySelectorAll('#files .wb-title')].some((e) => ['docs', 'sub', 'empty'].includes(e.textContent))")
        self.page.fill("#file-filter", "run_?.s[hx]")
        self.page.wait_for_function("!document.body.innerText.includes('run_2.py')")
        self.assertEqual(title("run_1.sh").count(), 1)
        self.page.press("#file-filter", "Escape")
        title("notes.md").wait_for()
        for back in ("sub", "empty", "docs", "deep"):
            title(back).wait_for()
        self.page.get_by_role("button", name="Details", exact=True).click()
        self.page.fill("#file-filter", "*.md")
        self.page.wait_for_function("!document.body.innerText.includes('data.json')")
        self.page.wait_for_function("!document.body.innerText.includes('empty/')")
        self.assertEqual(self.errors, [])

    def test_tree_nests_visibly_and_a_folder_can_become_the_top(self):
        for d in ("a/inner", "b"):
            (self.home / "proj" / d).mkdir(parents=True)
        (self.home / "proj" / "a" / "file1.txt").write_text("x")
        (self.home / "proj" / "a" / "inner" / "deep.txt").write_text("x")
        self.page.goto(f"http://127.0.0.1:{self.port}/?token={self.token}#dir={self.home / 'proj'}")
        exact = lambda name: re.compile("^" + re.escape(name) + "$")                           # noqa: E731
        title = lambda name: self.page.locator("#files .wb-title", has_text=exact(name)).first  # noqa: E731
        x = lambda name: title(name).bounding_box()["x"]                                       # noqa: E731
        title("proj").wait_for()
        # a folder's contents are indented under its name, not level with it
        self.page.locator("#files .wb-row", has=self.page.locator(".wb-title", has_text=exact("a"))).locator(".file-expander").click()
        title("file1.txt").wait_for()
        self.assertGreater(x("file1.txt") - x("a"), 14)
        self.assertLess(x("file1.txt") - x("a"), 40)
        self.assertEqual(self.page.locator("#files .file-icon svg").count() >= 4, True)         # drawn icons, no font needed
        self.page.screenshot(path=os.environ.get("WORKSPACE_TEST_SHOTS", "/tmp") + "/tree-nested.png",
                             clip={"x": 0, "y": 230, "width": 420, "height": 260})
        # a click on the folder that is shown folds it, and another opens it again
        self.page.wait_for_timeout(500)
        self.assertNotIn("…", self.page.locator("#files .wb-row", has=self.page.locator(".wb-title", has_text=exact("a"))).inner_text())
        title("a").click()
        self.page.wait_for_function("!document.body.innerText.includes('file1.txt')")
        title("a").click()
        title("file1.txt").wait_for()
        # double-click makes the folder the whole view: the folder above is gone from the tree, the crumbs say where it is
        title("inner").wait_for() if False else None
        title("a").dblclick()
        self.page.wait_for_function("document.querySelector('#path').value.endsWith('/proj/a')")
        self.assertEqual(self.page.locator("#files .wb-title", has_text=exact("proj")).count(), 0)
        self.assertEqual(self.page.locator("#files .wb-title").first.inner_text(), "a")
        self.assertEqual(self.page.locator("#files .file-crumbs button").last.inner_text(), "a")
        title("inner").wait_for()
        # Enter does the same on the folder that is active, and the breadcrumbs lead back out
        title("inner").click()
        self.page.keyboard.press("Enter")
        self.page.wait_for_function("document.querySelector('#path').value.endsWith('/proj/a/inner')")
        self.assertEqual(self.page.locator("#files .wb-title").first.inner_text(), "inner")
        self.page.locator("#files .file-crumbs button", has_text="proj").click()
        self.page.wait_for_function("document.querySelector('#path').value.endsWith('/proj')")
        title("proj").wait_for()
        self.assertEqual(self.errors, [])

    @unittest.skipUnless(NPM and (Path(NPM) / "wunderbaum@0.14.1/dist/wunderbaum.umd.min.js").is_file(),
                         "provide the pinned Wunderbaum package in WORKSPACE_TEST_NPM")
    def test_wunderbaum_expands_and_opens_existing_editor(self):
        self.page.goto(f"http://127.0.0.1:{self.port}/?token={self.token}")
        self.page.get_by_role("button", name="Tree", exact=True).click()
        self.page.locator("#files .wb-title", has_text="proj").click()
        self.page.locator("#files .wb-title", has_text="train.py").click()
        self.page.locator("#tabs .hpc-tab.active", has_text="train.py").wait_for()
        self.page.get_by_role("button", name="Details", exact=True).click()
        self.page.locator("#files tr", has_text="train.py").wait_for()
        self.page.get_by_role("button", name="Tree", exact=True).click()
        self.page.locator("#files .wb-title", has_text="train.py").wait_for()
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
