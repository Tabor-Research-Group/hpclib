"""The Workspace package (packages/workspace): it builds and installs as a console package, its server's file,
template, submission and job routes, its terminal over a WebSocket, and its token. The "cluster" is a directory here,
with a login shell whose profile puts fake sbatch, squeue and scancel on the PATH."""
import base64
import http.client
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import zipfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages" / "workspace"
TUNNEL = PACKAGE / "tunnels" / "workspace"
sys.path.insert(0, str(ROOT / "hpclib" / "servers"))
sys.path.insert(0, str(TUNNEL))
import console_packages  # noqa: E402
import script_templates  # noqa: E402
import workspace_server  # noqa: E402


class TestPackage(unittest.TestCase):
    def test_builds_and_installs(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        out, _ = console_packages.build(str(PACKAGE), str(tmp / "workspace.zip"))
        import agent_console      # hpclib's own apps and tunnels, which a package can't replace
        with unittest.mock.patch.dict(os.environ, {"HPCLIB_CONSOLE_DIR": str(tmp / "console")}):
            store = agent_console.package_store()
        report = store.install(Path(out).read_bytes())
        self.assertEqual(report["package"]["name"], "workspace")
        apps = store.apps()
        self.assertIn("workspace", apps)
        _, app, _ = apps["workspace"]
        self.assertEqual(app["tunnel"], "workspace")
        unpacked = tmp / "console" / "packages" / "workspace" / "tunnels" / "workspace"
        for f in ("workspace_server.py", "script_templates.py", "ui/index.html", "ui/app.js", "ui/shell.js",
                  "ui/bridge.js", "ui/hpclib-ui/index.js", "ui/hpclib-ui/base.css", "ui/hpclib-ui/libs.json",
                  "ui/hpclib-ui/default-styles.css", "ui/hpclib-ui/theme.js", "ui/hpclib-ui/components/tabs.js",
                  "script_templates/python-uv.sh", "sbatch_script.sh", "install.sh", "token_auth.py", "envinfo.py", "clusterinfo.py",
                  "ws/__init__.py", "ws/common.py", "ws/shellenv.py", "ws/fs.py", "ws/terminal.py", "ws/cluster.py",
                  "ws/envs.py", "ws/jobspec.py", "ws/jobs.py", "ws/templates.py", "ui/features/runs.js"):
            self.assertTrue((unpacked / f).is_file(), f)
        self.assertEqual((unpacked / "token_auth.py").read_bytes(), (ROOT / "hpclib/servers/token_auth.py").read_bytes())
        self.assertFalse((unpacked / "ui" / "hpclib-ui" / "update_libs.py").exists())   # a maintainer's script stays behind

    def test_the_workspace_asks_for_the_consoles_theme(self):
        manifest = json.loads((PACKAGE / "hpclib-package.json").read_text())
        self.assertIs(manifest["apps"]["workspace"]["theme_link"], True)
        out, _ = console_packages.build(str(PACKAGE), tempfile.mkdtemp() + "/w.zip")
        manifest, _ = console_packages.read_zip(Path(out).read_bytes())
        self.assertIs(manifest["apps"]["workspace"]["theme_link"], True)

    def test_theme_link_is_true_or_false(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(PACKAGE, tmp / "pkg")
        manifest = json.loads((tmp / "pkg" / "hpclib-package.json").read_text())
        manifest["apps"]["workspace"]["theme_link"] = "yes"
        (tmp / "pkg" / "hpclib-package.json").write_text(json.dumps(manifest))
        with self.assertRaises(console_packages.PackageError):
            console_packages.build(str(tmp / "pkg"), str(tmp / "o.zip"))

    def test_token_regex_finds_what_the_server_prints(self):
        import re
        manifest = json.loads((PACKAGE / "hpclib-package.json").read_text())
        pattern = re.compile(manifest["apps"]["workspace"]["token_regex"])
        line = "hpclib workspace 1.0.0 on login1: http://127.0.0.1:8640/?token=Ab-c_123456789012345678901234"
        self.assertEqual(pattern.findall(line), ["Ab-c_123456789012345678901234"])

    def test_tunnel_scripts_parse_and_python36(self):
        for script in TUNNEL.glob("*.sh"):
            self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0, script)
        import ast
        for py in ("envinfo.py", "clusterinfo.py"):
            ast.parse((ROOT / "hpclib" / "servers" / py).read_text(), feature_version=(3, 6))
        sources = ["workspace_server.py", "script_templates.py"] + sorted("ws/" + p.name for p in (TUNNEL / "ws").glob("*.py"))
        self.assertIn("ws/terminal.py", sources)
        for py in sources:
            ast.parse((TUNNEL / py).read_text(), feature_version=(3, 6))
            text = (TUNNEL / py).read_text()
            # the login node's python3 may be 3.6: no walrus, capture_output, text=, removeprefix, f"{x=}"
            for newer in (":=", "capture_output", "text=True", "removeprefix", "removesuffix", "=}"):
                self.assertNotIn(newer, text, f"{py}: {newer}")


class TestShellStructure(unittest.TestCase):
    """The page is built from hpclib-ui's elements: what the shell replaced doesn't come back by hand."""

    def test_no_hand_rolled_tabs_menus_splitters_or_toasts(self):
        sources = {p.name: p.read_text() for p in (TUNNEL / "ui").rglob("*.js") if "hpclib-ui" not in p.parts}
        sources["workspace.css"] = (TUNNEL / "ui" / "workspace.css").read_text()
        sources["index.html"] = (TUNNEL / "ui" / "index.html").read_text()
        for name, text in sources.items():
            for old in ('class="tab"', 'class: "tab', "splitter", "term-splitter", 'id="menu"', 'id="toast"', "setPointerCapture",
                        "localStorage.setItem"):
                if name == "bridge.js" and old == "localStorage.setItem":
                    continue          # the theme bridge keeps the console's choice for the next visit
                self.assertNotIn(old, text, f"{name}: {old}")

    def test_the_page_has_no_colours_of_its_own(self):
        css = (TUNNEL / "ui" / "workspace.css").read_text()
        import re
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", css), [])
        self.assertNotIn("prefers-color-scheme", css)

    def test_features_use_the_registry_not_each_other(self):
        import re
        for path in (TUNNEL / "ui" / "features").glob("*.js"):
            for local in re.findall(r'^import .* from "(\.\.?/[^"]+)"', path.read_text(), re.M):
                self.assertIn(local, ("../client.js", "../shell.js"), f"{path.name} imports {local}")


class TestBundle(unittest.TestCase):
    """`bundle` in hpclib-package.json: files copied from hpclib's clone into the zip when it is built."""

    def build(self, bundle):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        pkg = tmp / "pkg"
        shutil.copytree(PACKAGE, pkg)
        manifest = json.loads((pkg / "hpclib-package.json").read_text())
        manifest["bundle"] = bundle
        (pkg / "hpclib-package.json").write_text(json.dumps(manifest))
        out, _ = console_packages.build(str(pkg), str(tmp / "out.zip"))
        return zipfile.ZipFile(out)

    def test_the_workspace_carries_hpclib_ui(self):
        names = set(zipfile.ZipFile(console_packages.build(str(PACKAGE), tempfile.mkdtemp() + "/w.zip")[0]).namelist())
        ui = {p.relative_to(ROOT / "hpclib" / "ui").as_posix() for p in (ROOT / "hpclib" / "ui").rglob("*")
              if p.is_file() and p.suffix not in (".py", ".pyc", ".md") and "__pycache__" not in p.parts
              and not any(part.startswith("_") for part in p.parts)}     # _build/ (what makes vendor/codemirror6.js) stays behind
        self.assertIn("vendor/codemirror6.js", ui)
        self.assertTrue(ui)
        for rel in ui:
            self.assertIn("tunnels/workspace/ui/hpclib-ui/" + rel, names)

    def test_the_codemirror_bundle_is_the_one_libs_json_pins(self):
        import base64, hashlib
        ui = ROOT / "hpclib" / "ui"
        lib = json.loads((ui / "libs.json").read_text())["codemirror"]
        digest = "sha384-" + base64.b64encode(hashlib.sha384((ui / lib["local"]).read_bytes()).digest()).decode()
        self.assertEqual(lib["integrity"][lib["local"]], digest, "run hpclib/ui/update_libs.py after building the bundle")

    def test_rejects_unsafe_or_missing_sources(self):
        for bundle in ({"../x": "hpclib/ui"}, {"/abs": "hpclib/ui"}, {"a/b": "../secret"}, {"a": "no/such/dir"}, {"a": ""}):
            with self.assertRaises(console_packages.PackageError, msg=str(bundle)):
                self.build(bundle)

    def test_a_bundle_cannot_replace_the_packages_own_files(self):
        with self.assertRaises(console_packages.PackageError):
            self.build({"tunnels/workspace/workspace_server.py": "hpclib/ui/index.js"})

    def test_same_input_same_bytes(self):
        a, b = self.build({"x/ui": "hpclib/ui"}), self.build({"x/ui": "hpclib/ui"})
        self.assertEqual([(i.filename, i.CRC) for i in a.infolist()], [(i.filename, i.CRC) for i in b.infolist()])


from test_agent_console import ConsoleTestCase  # noqa: E402


class TestBundled(ConsoleTestCase):
    """The console's Packages offers the packages in hpclib's clone (packages/), the Workspace among them."""

    def test_check_and_install(self):
        import agent_console
        apps = unittest.mock.patch.object(agent_console, "APPS", dict(agent_console.APPS))   # installing adds to it
        apps.start()
        self.addCleanup(apps.stop)
        status, out = self.call("GET", "/api/packages/bundled")
        self.assertEqual(status, 200, out)
        ws = [p for p in out["packages"] if p["name"] == "workspace"]
        self.assertEqual((len(ws), ws[0]["installed"]), (1, None))
        status, out = self.call("POST", "/api/packages/bundled/workspace?dry_run=1")
        self.assertEqual((status, out["problems"]), (200, []), out)
        self.assertNotIn("workspace", [a["id"] for a in self.call("GET", "/api/apps")[1]["apps"]])
        status, out = self.call("POST", "/api/packages/bundled/workspace")
        self.assertEqual(status, 201, out)
        self.assertIn("workspace", [a["id"] for a in self.call("GET", "/api/apps")[1]["apps"]])
        ws = [p for p in self.call("GET", "/api/packages/bundled")[1]["packages"] if p["name"] == "workspace"]
        self.assertEqual(ws[0]["installed"], ws[0]["version"])
        self.assertEqual(self.call("POST", "/api/packages/bundled/nope")[0], 404)


class TestTemplates(unittest.TestCase):
    def test_render_rules(self):
        text = ("#!/bin/bash\n#@ title: T\n#@ script: {{input_stem}}.job\n#@ field mem: Memory | 8G\n"
                "#@ field gres: GPUs |\n#@ field project: Project | {{dir}}\n"
                "#SBATCH --mem={{mem}}\n{{?gres}}#SBATCH --gres={{gres}}\n{{!gres}}echo no gpus\n"
                "cd {{project}} && python {{input}}\n")
        ctx = script_templates.context_for("/s/run/a.b.py", {"home": "/h", "user": "u"})
        script, values = script_templates.render(text, {"gres": ""}, ctx)
        self.assertEqual(script, "#!/bin/bash\n#SBATCH --mem=8G\necho no gpus\ncd /s/run && python a.b.py\n")
        script, _ = script_templates.render(text, {"gres": "gpu:1", "mem": "55G"}, ctx)
        self.assertIn("#SBATCH --gres=gpu:1\n", script)
        self.assertNotIn("no gpus", script)
        self.assertEqual(script_templates.script_name(script_templates.parse(text), values), "a.b.job")
        self.assertEqual(ctx["hpclib"], "/h/hpclib/hpclib.sh")
        with self.assertRaises(script_templates.TemplateError):
            script_templates.render(text + "{{typo}}\n", {}, ctx)

    def test_the_workspaces_templates(self):
        ctx = script_templates.context_for("/scratch/user/jdoe/run/train.py",
                                           {"home": "/home/jdoe", "user": "jdoe", "hpclib": "/scratch/user/jdoe/hpclib/hpclib.sh"})
        built = script_templates.builtins()
        self.assertEqual(sorted(built), ["bash-script", "python-pixi", "python-uv", "python-venv"])
        for name, text in built.items():
            parsed = script_templates.parse(text, name)
            names = {f["name"] for f in parsed["fields"]}
            for given in ({}, {"modules": "GCC/13.3.0 OpenMPI/5.0.3", "gres": "gpu:a100:1", "account": "1228",
                               "venv": "/scratch/user/jdoe/envs/enc", "project": "", "python": "3.12"}):
                given = {k: v for k, v in given.items() if k in names}
                script, _ = script_templates.render(text, given, ctx, name)
                self.assertNotIn("{{", script, name)
                self.assertNotIn("#@", script, name)
                self.assertTrue(script.startswith("#!/bin/bash\n"), name)
                self.assertEqual(subprocess.run(["bash", "-n"], input=script, universal_newlines=True).returncode, 0, script)
                self.assertIn("#SBATCH --time=6:00:00", script)
                self.assertIn('. "/scratch/user/jdoe/hpclib/hpclib.sh"', script)
                self.assertIn("slurm_job_footer", script)
        uv = script_templates.render(built["python-uv"], {}, ctx)[0]
        self.assertIn('--project "/scratch/user/jdoe/run" python -u "train.py"', uv)
        uv = script_templates.render(built["python-uv"], {"project": "", "python": "3.12"}, ctx)[0]
        self.assertIn('--script "train.py"', uv)
        self.assertIn('UV_ARGS+=(--python "3.12")', uv)
        venv = script_templates.render(built["python-venv"], {"venv": "/e", "modules": "GCC/13.3.0"}, ctx)[0]
        self.assertIn('source "/e/bin/activate"', venv)
        self.assertIn("module load GCC/13.3.0", venv)
        self.assertNotIn("conda activate", venv)


class ServerCase(unittest.TestCase):
    """A workspace server in this process, with HOME a temporary "cluster" home."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        (self.home / "proj").mkdir(parents=True)
        (self.home / "proj" / "train.py").write_text("print('hi')\n")
        (self.home / "proj" / ".hidden").write_text("x")
        (self.home / "proj" / "page.html").write_text("<p>hello <img src='fig.svg'></p>")
        (self.home / "proj" / "fig.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        (self.home / "proj" / "movie.mp4").write_bytes(bytes(range(256)) * 40)
        (self.home / "hpclib").mkdir()
        (self.home / "hpclib" / "hpclib.sh").write_text("# hpclib\n")
        fake = self.tmp / "slurm-bin"
        fake.mkdir()
        (fake / "sbatch").write_text('#!/bin/sh\necho "sbatch $* in $(pwd)" > "$HOME/sbatch.log"\n'
                                     'echo "Submitted batch job 4242"\n')
        (fake / "squeue").write_text("#!/bin/sh\necho '4242|train|RUNNING|1:02|6:00:00|c001|/home/x/proj'\n"
                                     "echo '4243|other|PENDING|0:00|1:00:00|(Priority)|/home/x'\n")
        (fake / "scancel").write_text('#!/bin/sh\necho "$1" >> "$HOME/scancel.log"\n')
        for f in fake.iterdir():
            f.chmod(0o755)
        # sites put SLURM on the PATH of a login shell: so does this one's profile
        (self.home / ".bash_profile").write_text(f'export PATH="{fake}:$PATH"\nexport PS1="$ "\n')
        env = {"HOME": str(self.home), "USER": "jdoe", "SHELL": "/bin/bash", "PATH": "/usr/bin:/bin",
               "HPCTUNNELS_DATA_DIR": str(self.home / ".local" / "tunnels")}
        patch = unittest.mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        self.token = "test-token-0123456789abcdef"
        ws = workspace_server.Workspace(None, "https://cdn.example/npm", 0.005, 0.001)
        self.server = workspace_server.Server(("127.0.0.1", 0), self.token, ws)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(ws.close)                 # the terminals outlive their pages, not the Workspace

    def call(self, method, path, body=None, headers=None, token=True, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = dict(headers or {})
        if token:
            h["X-Workspace-Token"] = self.token
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        if raw:
            return res.status, dict(res.getheaders()), data
        try:
            return res.status, json.loads(data.decode() or "null")
        except ValueError:
            return res.status, data

    def raw_base(self, name="movie.mp4"):
        """The signed URL the server gives for a file in proj/, without the file's name."""
        status, out = self.call("POST", "/api/raw-url", {"path": str(self.home / "proj" / name)})
        self.assertEqual(status, 200, out)
        return out["url"].rsplit("/", 1)[0]



class TestServer(ServerCase):
    def test_token_and_host(self):
        self.assertEqual(self.call("GET", "/health", token=False)[0], 200)
        self.assertEqual(self.call("GET", "/", token=False, raw=True)[0], 200)          # the page itself
        self.assertEqual(self.call("GET", "/api/info", token=False)[0], 401)
        self.assertEqual(self.call("GET", "/api/info", headers={"X-Workspace-Token": "nope"}, token=False)[0], 401)
        self.assertEqual(self.call("GET", f"/api/info?token={self.token}", token=False)[0], 401)   # a header only
        self.assertEqual(self.call("GET", "/api/info", headers={"Host": "evil.example:80"})[0], 421)
        self.assertEqual(self.call("GET", "/ui/../workspace_server.py", token=False, raw=True)[0], 404)

    def test_files(self):
        status, info = self.call("GET", "/api/info")
        self.assertEqual(status, 200, info)
        self.assertEqual((info["home"], info["user"], info["root"]), (str(self.home), "jdoe", str(self.home)))
        self.assertEqual(info["hpclib"], str(self.home / "hpclib" / "hpclib.sh"))
        self.assertEqual(info["cdn"], "https://cdn.example/npm")
        status, out = self.call("GET", "/api/list?path=proj")
        self.assertEqual(status, 200, out)
        self.assertEqual([e["name"] for e in out["entries"]], ["fig.svg", "movie.mp4", "page.html", "train.py"])
        self.assertEqual(out["hidden"], 1)
        self.assertIn(".hidden", [e["name"] for e in self.call("GET", "/api/list?path=~/proj&hidden=1")[1]["entries"]])
        self.assertEqual(self.call("GET", "/api/list?path=nowhere")[0], 404)
        path = urllib.parse.quote(str(self.home / "proj" / "train.py"))
        status, headers, data = self.call("GET", f"/api/file?path={path}", raw=True)
        self.assertEqual((status, data), (200, b"print('hi')\n"))
        mtime = headers["X-Mtime-Ns"]
        os.chmod(self.home / "proj" / "train.py", 0o750)
        status, out = self.call("PUT", f"/api/file?path={path}&mtime_ns={mtime}", body=b"print(2)\n")
        self.assertEqual(status, 200, out)
        self.assertEqual((self.home / "proj" / "train.py").read_text(), "print(2)\n")
        self.assertEqual(os.stat(self.home / "proj" / "train.py").st_mode & 0o777, 0o750)       # its mode kept
        status, out = self.call("PUT", f"/api/file?path={path}&mtime_ns={mtime}", body=b"old\n")
        self.assertEqual(status, 409, out)                                                     # changed since
        self.assertIn("changed since", out["error"])
        self.assertEqual((self.home / "proj" / "train.py").read_text(), "print(2)\n")
        # the editor's limit (5 KB here): the movie is too big to edit, not to view
        movie = urllib.parse.quote(str(self.home / "proj" / "movie.mp4"))
        self.assertEqual(self.call("GET", f"/api/file?path={movie}")[0], 413)
        new = urllib.parse.quote(str(self.home / "proj" / "new.py"))
        self.assertEqual(self.call("PUT", f"/api/file?path={new}&how=create", body=b"")[0], 200)
        self.assertEqual(self.call("PUT", f"/api/file?path={new}&how=create", body=b"")[0], 409)
        # two creates of one name at once: one wins, the others are told it exists, and nothing is left beside it
        import concurrent.futures
        twin = urllib.parse.quote(str(self.home / "proj" / "twin.slurm"))
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            codes = sorted(f.result()[0] for f in [pool.submit(self.call, "PUT", f"/api/file?path={twin}&how=create", body=b"x")
                                                     for _ in range(8)])
        self.assertEqual(codes, [200] + [409] * 7)
        self.assertEqual([p.name for p in (self.home / "proj").glob(".twin*")], [])
        self.assertEqual(self.call("POST", "/api/mkdir", {"path": "proj/sub"})[0], 200)
        self.assertEqual(self.call("POST", "/api/rename", {"from": "proj/new.py", "to": "proj/sub/new.py"})[0], 200)
        self.assertEqual(self.call("POST", "/api/rename", {"from": "proj/train.py", "to": "proj/sub/new.py"})[0], 409)
        self.assertEqual(self.call("POST", "/api/delete", {"path": "proj/sub"})[0], 409)          # not empty
        self.assertEqual(self.call("POST", "/api/delete", {"path": "proj/sub/new.py"})[0], 200)
        self.assertEqual(self.call("POST", "/api/delete", {"path": "proj/sub"})[0], 200)
        self.assertEqual(self.call("POST", "/api/delete", {"path": "~"})[0], 400)
        # the connection is still good after a refused upload (its body unread)
        self.assertEqual(self.call("PUT", f"/api/file?path={path}&how=create", body=b"x" * 100000)[0], 409)
        self.assertEqual(self.call("GET", "/api/info")[0], 200)

    def test_raw_ranges_and_sandboxed_html(self):
        base = self.raw_base()
        status, headers, data = self.call("GET", f"{base}/movie.mp4", token=False, raw=True)
        self.assertEqual((status, len(data), headers["Content-Type"]), (200, 10240, "video/mp4"))
        status, headers, data = self.call("GET", f"{base}/movie.mp4", headers={"Range": "bytes=256-511"}, token=False, raw=True)
        self.assertEqual((status, data, headers["Content-Range"]), (206, bytes(range(256)), "bytes 256-511/10240"))
        self.assertEqual(self.call("GET", f"{base}/movie.mp4", headers={"Range": "bytes=99999-"}, token=False, raw=True)[0], 416)
        status, headers, _ = self.call("GET", f"{base}/page.html", token=False, raw=True)
        self.assertEqual(status, 200)
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertNotIn("allow-forms", headers["Content-Security-Policy"])
        self.assertIn("sandbox", self.call("GET", f"{base}/fig.svg", token=False, raw=True)[1]["Content-Security-Policy"])
        # the page's own files load through the same URL, other directories don't
        self.assertEqual(self.call("GET", f"{base}/train.py", token=False, raw=True)[0], 200)
        self.assertEqual(self.call("GET", f"{base}/../hpclib/hpclib.sh", token=False, raw=True)[0], 401)

    def test_templates_render_submit_jobs(self):
        status, out = self.call("GET", "/api/templates")
        self.assertEqual(status, 200, out)
        names = {t["name"]: t for t in out["templates"]}
        self.assertEqual((names["python-uv"]["source"], names["python-uv"]["for"]), ("workspace", [".py"]))
        inp = str(self.home / "proj" / "train.py")
        status, r = self.call("POST", "/api/render", {"template": "python-venv", "input": inp,
                                                      "values": {"mem": "55G", "modules": "GCC/13.3.0"}})
        self.assertEqual(status, 200, r)
        self.assertEqual((r["name"], r["dir"]), ("train.slurm", str(self.home / "proj")))
        self.assertIn("module load GCC/13.3.0", r["script"])
        self.assertIn(f'. "{self.home}/hpclib/hpclib.sh"', r["script"])
        status, s = self.call("POST", "/api/submit", {"script": r["script"], "name": r["name"], "dir": r["dir"],
                                                      "template": "python-venv", "values": r["values"]})
        self.assertEqual(status, 200, s)
        self.assertEqual(s["job_id"], "4242")
        self.assertEqual((self.home / "sbatch.log").read_text().strip(), f"sbatch train.slurm in {self.home}/proj")
        self.assertIn("module load GCC/13.3.0", (self.home / "proj" / "train.slurm").read_text())
        self.assertEqual(self.call("POST", "/api/submit", {"script": "#!/bin/bash\n", "name": "train.slurm",
                                                           "dir": r["dir"]})[0], 409)
        self.assertEqual(self.call("POST", "/api/submit", {"script": "#!/bin/bash\n", "name": "../x", "dir": r["dir"]})[0], 400)
        out = self.call("GET", "/api/templates")[1]
        self.assertEqual(out["remembered"]["python-venv"]["mem"], "55G")
        self.assertNotIn("job_name", out["remembered"]["python-venv"])
        self.assertEqual(out["last"], "python-venv")
        # your own copy replaces the Workspace's
        status, e = self.call("POST", "/api/templates/edit", {"name": "python-uv"})
        self.assertEqual((status, e["created"]), (200, True), e)
        mine = Path(e["path"])
        self.assertEqual(mine, self.home / ".local" / "tunnels" / "script_templates" / "python-uv.sh")
        mine.write_text(mine.read_text().replace("#@ title: Python with uv", "#@ title: My uv"))
        uv = [t for t in self.call("GET", "/api/templates")[1]["templates"] if t["name"] == "python-uv"]
        self.assertEqual((len(uv), uv[0]["title"], uv[0]["source"], uv[0]["replaces"]), (1, "My uv", "yours", True))
        self.assertEqual(self.call("POST", "/api/templates/edit", {"name": "python-uv"})[1]["created"], False)
        self.assertTrue(Path(self.call("POST", "/api/templates/edit", {"name": "gpu", "from": "python-uv"})[1]["path"]).exists())
        self.assertEqual(self.call("POST", "/api/render", {"template": "nope", "input": inp})[0], 404)
        self.assertEqual(self.call("POST", "/api/render", {"template": "x", "input": inp, "text": "{{bad}}"})[0], 400)
        # a classic submit keeps no run, and "My jobs" is gone: the Runs page is the way to see and cancel
        self.assertEqual(self.call("GET", "/api/runs")[1]["runs"], [])
        self.assertEqual(self.call("GET", "/api/jobs")[0], 404)
        self.assertEqual(self.call("POST", "/api/cancel", {"job": "4243"})[0], 404)


class TestAuthentication(ServerCase):
    """What a request must carry, and what a page the viewer shows (an opaque origin, no token) can do."""

    def test_the_token_is_a_header_and_in_no_url(self):
        for path in ("/api/info", "/api/list?path=proj", f"/api/stat?path={self.home}"):
            self.assertEqual(self.call("GET", path)[0], 200, path)
            self.assertEqual(self.call("GET", f"{path}{'&' if '?' in path else '?'}token={self.token}", token=False)[0],
                             401, path)
        status, out = self.call("POST", "/api/raw-url", {"path": str(self.home / "proj" / "page.html")})
        self.assertEqual(status, 200, out)
        self.assertNotIn(self.token, out["url"])
        self.assertEqual(self.call("POST", "/api/raw-url", {"path": str(self.home / "proj" / "page.html")}, token=False)[0], 401)
        self.assertEqual(self.call("POST", "/api/raw-url", {"path": str(self.home / "nope.html")})[0], 404)

    def test_requests_that_change_things_are_json(self):
        body = json.dumps({"path": "proj/sub"}).encode()
        for kind in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", None):
            headers = {"Content-Type": kind} if kind else {}
            self.assertEqual(self.call("POST", "/api/mkdir", body=body, headers=headers)[0], 415, kind)
        self.assertFalse((self.home / "proj" / "sub").exists())
        self.assertEqual(self.call("POST", "/api/mkdir", body=body, headers={"Content-Type": "application/json; charset=utf-8"})[0], 200)
        self.assertTrue((self.home / "proj" / "sub").is_dir())

    def test_only_the_workspaces_own_page(self):
        host = f"127.0.0.1:{self.port}"
        for origin in ("null", "http://evil.example", "http://localhost:1", f"http://127.0.0.1:{self.port + 1}"):
            self.assertEqual(self.call("POST", "/api/mkdir", {"path": "proj/o"}, headers={"Origin": origin})[0], 403, origin)
            self.assertEqual(self.call("GET", "/api/info", headers={"Origin": origin})[0], 403, origin)
        self.assertEqual(self.call("GET", "/api/info", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.call("GET", "/api/info", headers={"Sec-Fetch-Site": "same-site"})[0], 403)
        self.assertFalse((self.home / "proj" / "o").exists())
        for headers in ({"Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}):
            self.assertEqual(self.call("GET", "/api/info", headers=headers)[0], 200, headers)

    def test_signed_file_urls(self):
        proj = str(self.home / "proj")
        status, out = self.call("POST", "/api/raw-url", {"path": f"{proj}/page.html"})
        url = out["url"]
        prefix = url.split("/")[2]
        expires, depth, signature = prefix.split(".")
        self.assertEqual((int(expires) - time.time() > 3500, int(depth)), (True, len((self.home / "proj").parts) - 1))
        quoted = urllib.parse.quote(proj)
        self.assertEqual(self.call("GET", url, token=False, raw=True)[0], 200)
        # tampered: another directory, another depth, another signature, another expiry
        for bad in (f"/raw/{prefix}{urllib.parse.quote(str(self.home / 'hpclib'))}/hpclib.sh",
                    f"/raw/{expires}.{int(depth) - 1}.{signature}{quoted}/page.html",
                    f"/raw/{expires}.{depth}.{'0' * 40}{quoted}/page.html",
                    f"/raw/{int(expires) + 99}.{depth}.{signature}{quoted}/page.html",
                    f"/raw/{prefix}{quoted}/../../etc/passwd",
                    f"/raw/{self.token}{quoted}/page.html", "/raw/", "/raw/x.y.z/etc/passwd"):
            self.assertEqual(self.call("GET", bad, token=False, raw=True)[0], 401, bad)
        # expired, with a signature that is right for it
        late = int(time.time()) - 5
        old = f"/raw/{late}.{depth}.{self.server.sign_raw(late, int(depth), proj)}{quoted}/page.html"
        self.assertEqual(self.call("GET", old, token=False, raw=True)[0], 401)
        # another run of the server (another token) can't use it
        other = workspace_server.Server(("127.0.0.1", 0), "another-token-0123456789abcdef", self.server.workspace)
        self.addCleanup(other.server_close)
        self.assertNotEqual(other.sign_raw(int(expires), int(depth), proj), signature)
        self.assertEqual(self.call("POST", "/api/raw-url", {"path": "/page.html"})[0], 404)

    def test_terminal_tickets_are_single_use_and_short(self):
        self.assertEqual(self.call("POST", "/api/term/ticket", token=False)[0], 401)
        status, out = self.call("POST", "/api/term/ticket")
        self.assertEqual(status, 200, out)
        ticket = out["ticket"]
        self.assertNotIn(self.token, ticket)
        self.assertTrue(self.server.redeem_ticket(ticket))
        self.assertFalse(self.server.redeem_ticket(ticket))                       # once
        self.assertFalse(self.server.redeem_ticket(""))
        ticket = self.call("POST", "/api/term/ticket")[1]["ticket"]
        clock = [time.time()]
        self.server.ticket_store.clock = lambda: clock[0]
        late = self.server.new_ticket()
        clock[0] += workspace_server.TICKET_SECONDS
        self.assertFalse(self.server.redeem_ticket(late))                          # too old
        self.server.ticket_store.clock = time.time
        self.assertTrue(self.server.redeem_ticket(ticket))
        self.assertEqual(len(self.server.ticket_store._tickets), 0)                              # the old ones are dropped

    def test_what_a_shown_page_can_send_changes_nothing(self):
        """A page in the viewer has its signed URL and nothing else: no token, the Origin `null`, no custom
        headers, and forms or simple requests as its only means. None of it reaches an API route."""
        (self.home / "proj" / "keep.txt").write_text("kept")
        url = self.raw_base("page.html")
        page_knows = {"prefix": url.split("/")[2]}
        bodies = {"/api/mkdir": {"path": "proj/made"}, "/api/delete": {"path": "proj/keep.txt"},
                  "/api/rename": {"from": "proj/keep.txt", "to": "proj/gone.txt"},
                  "/api/submit": {"script": "#!/bin/bash\ntouch ~/ran\n", "name": "ran.sh", "dir": str(self.home),
                                  "overwrite": True}}
        for route, body in bodies.items():
            for query in ("", f"?token={page_knows['prefix']}", f"?token={self.token[:-1]}"):
                for kind, raw in (("text/plain", json.dumps(body)), ("application/json", json.dumps(body))):
                    status, _ = self.call("POST", route + query, body=raw.encode(), token=False,
                                          headers={"Content-Type": kind, "Origin": "null", "Sec-Fetch-Site": "cross-site"})
                    self.assertIn(status, (401, 403, 415), (route, query, kind))
        self.assertEqual(self.call("PUT", f"/api/file?path={urllib.parse.quote(str(self.home / 'put.txt'))}&token=x",
                                   body=b"x", token=False, headers={"Origin": "null"})[0], 403)
        self.assertEqual(sorted(p.name for p in self.home.iterdir() if p.is_file() and p.name != ".bash_profile"), [])
        self.assertTrue((self.home / "proj" / "keep.txt").exists())
        self.assertFalse((self.home / "proj" / "made").exists())
        self.assertFalse((self.home / "sbatch.log").exists())


class TestUsage(ServerCase):
    def make_tree(self):
        tree = self.home / "proj" / "envs"
        for sub, n in (("big", 30), ("small", 3)):
            (tree / sub / "deep").mkdir(parents=True)
            for i in range(n):
                (tree / sub / "deep" / f"f{i}").write_text("x" * 5000)
        (tree / "note.txt").write_text("x")
        return tree

    def check(self, out, tree):
        kids = {c["name"]: c for c in out["children"]}
        self.assertEqual(sorted(kids), ["big", "small"])
        self.assertEqual((kids["big"]["files"], kids["small"]["files"]), (32, 5))     # files and folders, as du --inodes
        self.assertGreater(kids["big"]["kb"], kids["small"]["kb"])
        self.assertEqual((out["path"], out["files"], out["here"]["files"], out["partial"]), (str(tree), 39, 1, False))

    def test_sizes_and_file_counts(self):
        tree = self.make_tree()
        status, out = self.call("GET", f"/api/usage?path={urllib.parse.quote(str(tree))}")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["count_method"], "du --inodes")
        self.check(out, tree)
        self.assertEqual(self.call("GET", "/api/usage?path=nowhere")[0], 404)
        self.assertEqual(self.call("GET", f"/api/usage?path={urllib.parse.quote(str(tree / 'note.txt'))}")[0], 404)

    def test_counts_by_walking_where_du_is_old(self):
        tree = self.make_tree()
        old = self.tmp / "old-du"
        old.mkdir()
        (old / "du").write_text(f'#!/bin/bash\nfor a in "$@"; do [ "$a" = --inodes ] && {{ echo "du: unrecognized option \'--inodes\'" >&2; exit 1; }}; done\nexec {shutil.which("du")} "$@"\n')
        (old / "du").chmod(0o755)
        with unittest.mock.patch.dict(os.environ, {"PATH": f"{old}:{os.environ['PATH']}"}):
            status, out = self.call("GET", f"/api/usage?path={urllib.parse.quote(str(tree))}")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["count_method"], "walk")
        self.check(out, tree)


class TestTerminal(ServerCase):
    def connect(self, query, origin=None):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        origin = origin or f"http://127.0.0.1:{self.port}"
        sock.sendall((f"GET /terminal?{query} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"Origin: {origin}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(1)
        return sock, head.decode(), key

    @staticmethod
    def send(sock, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
        sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))

    def ticket(self):
        status, out = self.call("POST", "/api/term/ticket")
        self.assertEqual(status, 200, out)
        return out["ticket"]

    def test_shell(self):
        cwd = urllib.parse.quote(str(self.home / "proj"))
        sock, head, key = self.connect(f"ticket={self.ticket()}&cwd={cwd}&cols=90&rows=20")
        self.assertIn("101", head.splitlines()[0])
        self.assertIn(workspace_server.ws_accept(key), head)
        rfile = sock.makefile("rb")
        self.send(sock, 0x1, json.dumps({"type": "resize", "cols": 120, "rows": 40}).encode())
        self.send(sock, 0x2, b"pwd; stty size; command -v sbatch; echo done-$((6*7))\n")
        out, deadline = b"", time.time() + 15
        while b"done-42\r\n" not in out and time.time() < deadline:
            op, data = workspace_server.ws_read(rfile)
            if op == 0x2:
                out += data
        text = out.decode(errors="replace")
        self.assertIn(str(self.home / "proj"), text)
        self.assertIn("40 120", text)
        self.assertIn("slurm-bin/sbatch", text)               # a login shell: its profile ran
        self.send(sock, 0x2, b"exit 3\n")
        got = None
        while got is None and time.time() < deadline + 5:
            op, data = workspace_server.ws_read(rfile)
            if op == 0x1:
                got = json.loads(data.decode())
        self.assertEqual(got, {"type": "exit", "code": 3})
        sock.close()

    def test_refused(self):
        for query in ("ticket=wrong", "", f"token={self.token}"):                # the token is no way in
            sock, head, _ = self.connect(query)
            self.assertIn("401", head.splitlines()[0], query)
            sock.close()
        for origin in ("http://evil.example", "null"):
            sock, head, _ = self.connect(f"ticket={self.ticket()}", origin=origin)
            self.assertIn("403", head.splitlines()[0], origin)
            sock.close()
        ticket = self.ticket()                                                   # one use
        sock, head, _ = self.connect(f"ticket={ticket}")
        self.assertIn("101", head.splitlines()[0])
        sock.close()
        sock, head, _ = self.connect(f"ticket={ticket}")
        self.assertIn("401", head.splitlines()[0])
        sock.close()

    def read_until(self, rfile, needle, seconds=15):
        out, deadline = b"", time.time() + seconds
        while needle not in out and time.time() < deadline:
            op, data = workspace_server.ws_read(rfile)
            if op == 0x2:
                out += data
            elif op == 0x1 and b'"exit"' in data:
                break
        return out

    def first_frame(self, rfile):
        op, data = workspace_server.ws_read(rfile)
        self.assertEqual(op, 0x1)
        return json.loads(data.decode())

    def test_a_terminal_outlives_its_page_and_replays_what_it_printed(self):
        sock, head, _ = self.connect(f"ticket={self.ticket()}&cols=90&rows=20")
        rfile = sock.makefile("rb")
        hello = self.first_frame(rfile)
        self.assertEqual(hello["type"], "session")
        sid = hello["id"]
        self.send(sock, 0x2, b"export KEPT=remains; echo first-$((6*7))\n")
        self.assertIn(b"first-42", self.read_until(rfile, b"first-42\r\n"))
        rfile.close()
        sock.close()                                                       # the page goes away
        status, listing = self.call("GET", "/api/terms")
        self.assertEqual([(t["id"], t["alive"]) for t in listing["terminals"]], [(sid, True)])
        deadline = time.time() + 10
        while self.call("GET", "/api/terms")[1]["terminals"][0]["attached"] and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.call("GET", "/api/terms")[1]["terminals"][0]["attached"], 0)
        # ... and comes back: the buffer first, then the same shell
        sock, head, _ = self.connect(f"ticket={self.ticket()}&session={sid}")
        self.assertIn("101", head.splitlines()[0])
        rfile = sock.makefile("rb")
        self.assertEqual(self.first_frame(rfile)["id"], sid)
        self.assertIn(b"first-42", self.read_until(rfile, b"first-42\r\n"))
        self.send(sock, 0x2, b"echo again-$KEPT\n")
        self.assertIn(b"again-remains", self.read_until(rfile, b"again-remains\r\n"))
        self.send(sock, 0x2, b"exit 4\n")
        exit_seen = None
        deadline = time.time() + 10
        while exit_seen is None and time.time() < deadline:
            op, data = workspace_server.ws_read(rfile)
            if op == 0x1:
                exit_seen = json.loads(data.decode())
        self.assertEqual(exit_seen, {"type": "exit", "code": 4})
        rfile.close()
        sock.close()
        deadline = time.time() + 10
        while self.call("GET", "/api/terms")[1]["terminals"] and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.call("GET", "/api/terms")[1]["terminals"], [])
        sock, head, _ = self.connect(f"ticket={self.ticket()}&session={sid}")
        self.assertIn("404", head.splitlines()[0])                        # an ended terminal can't be attached
        sock.close()

    def test_closing_a_terminal_hangs_the_shell_up(self):
        sock, head, _ = self.connect(f"ticket={self.ticket()}")
        rfile = sock.makefile("rb")
        sid = self.first_frame(rfile)["id"]
        rfile.close()
        sock.close()
        self.assertEqual(self.call("POST", "/api/term/close", {"id": sid})[0], 200)
        self.assertEqual(self.call("POST", "/api/term/close", {"id": sid})[0], 404)
        self.assertEqual(self.call("GET", "/api/terms")[1]["terminals"], [])

    def test_two_pages_can_watch_one_terminal(self):
        a, head, _ = self.connect(f"ticket={self.ticket()}")
        ra = a.makefile("rb")
        sid = self.first_frame(ra)["id"]
        b, head, _ = self.connect(f"ticket={self.ticket()}&session={sid}")
        rb = b.makefile("rb")
        self.first_frame(rb)
        self.send(a, 0x2, b"echo both-$((1+1))\n")
        self.assertIn(b"both-2", self.read_until(ra, b"both-2\r\n"))
        self.assertIn(b"both-2", self.read_until(rb, b"both-2\r\n"))
        for f in (ra, rb, a, b):
            f.close()


if __name__ == "__main__":
    unittest.main()


class SubmissionCase(ServerCase):
    """A fake cluster for the guided path: sinfo, sacctmgr, sbatch --test-only, modules, and a uv that makes a .venv and
    lists one package, with a uv project in proj/."""

    def setUp(self):
        super().setUp()
        fake = self.tmp / "slurm-bin"
        (fake / "sinfo").write_text("#!/bin/sh\n[ \"$1\" = --version ] && { echo 'slurm 24.05.1'; exit 0; }\n"
                                    "echo 'gpu*|up|2-00:00:00|4|64|257000|gpu:a100:4(S:0-1)'\n"
                                    "echo 'cpu|up|7-00:00:00|100|48|190000|(null)'\n")
        (fake / "sacctmgr").write_text("#!/bin/sh\ncase \"$*\" in *defaultaccount*) echo acct1;; *) echo 'acct1|gpu|normal';; esac\n")
        (fake / "sbatch").write_text('#!/bin/sh\nif [ "$1" = --test-only ]; then cat > "$HOME/test-only.sh"; '
                                     'echo "sbatch: Job 99 to start at 2099-01-01T00:00:00 using 4 processors on nodes c1 in partition gpu" >&2; exit 0; fi\n'
                                     'echo "sbatch $* in $(pwd)" > "$HOME/sbatch.log"\necho "Submitted batch job 4242"\n')
        (fake / "module").write_text("#!/bin/sh\n")
        # squeue -j ID[,ID]: those jobs RUNNING, unless $HOME/squeue-empty says they have ended; sacct then answers from
        # $HOME/sacct.out (JobID|State|ExitCode|Elapsed lines)
        (fake / "squeue").write_text("#!/bin/sh\n[ -e \"$HOME/squeue-empty\" ] && exit 0\n"
                                     "ids=$(echo \"$*\" | sed 's/.*-j \\([^ ]*\\).*/\\1/')\n"
                                     "for i in $(echo \"$ids\" | tr , ' '); do echo \"$i|RUNNING|None|0:10\"; done\n")
        (fake / "sacct").write_text("#!/bin/sh\n[ -f \"$HOME/sacct.out\" ] && cat \"$HOME/sacct.out\"\nexit 0\n")
        (fake / "scancel").write_text('#!/bin/sh\necho "$1" >> "$HOME/scancel.log"\n')
        for f in fake.iterdir():
            f.chmod(0o755)
        (self.home / ".bash_profile").write_text(
            f'export PATH="{fake}:$PATH"\nexport PS1="$ "\nmodule() {{ printf "GCC/13.3.0\\nCUDA/12.6.0\\n" >&2; }}\n')
        (self.home / "hpclib" / "hpclib.sh").write_text(f". {ROOT / 'hpclib' / 'lib' / 'pytools.sh'}\n")
        uv = self.home / ".local" / "bin" / "uv"
        uv.parent.mkdir(parents=True)
        uv.write_text(f'#!/bin/sh\ncase "$1" in --version) echo "uv 0.9.9";; sync) mkdir -p .venv/bin; '
                      f'ln -sf {sys.executable} .venv/bin/python; echo synced;; '
                      f'pip) echo \'[{{"name":"numpy","version":"2.0"}}]\';; esac\n')
        uv.chmod(0o755)
        (self.home / "proj" / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\nrequires-python = ">=3.9"\n')
        self.script = str(self.home / "proj" / "train.py")
        self.server.workspace.cluster.forget()
        self.server.workspace.cluster.hpclib = self.server.workspace.envs.hpclib = str(self.home / "hpclib" / "hpclib.sh")
        self.spec = {"workdir": str(self.home / "proj"), "kind": "script", "command": self.script,
                     "resources": {"time": "2h", "memory": "4G", "partition": "gpu"}}


class TestSubmission(SubmissionCase):
    """The guided path: a script in a uv project goes from file to followed output with no text edited."""

    def prepare(self):
        status, op = self.call("POST", "/api/env/prepare", {"project": str(self.home / "proj")})
        self.assertEqual(status, 200, op)
        for _ in range(100):
            status, op = self.call("GET", "/api/env/operation?id=" + op["id"])
            if op["state"] != "running":
                return op
            time.sleep(0.1)
        self.fail("prepare didn't finish")

    def test_cluster_and_modules(self):
        status, cluster = self.call("GET", "/api/cluster")
        self.assertEqual(status, 200, cluster)
        self.assertEqual([p["name"] for p in cluster["partitions"]], ["gpu", "cpu"])
        self.assertEqual(cluster["accounts"], ["acct1"])
        self.assertEqual(cluster["default_account"], "acct1")
        status, mods = self.call("GET", "/api/modules?q=cuda")
        self.assertEqual(mods["modules"], ["CUDA/12.6.0"])

    def test_a_slow_slurm_is_reported_not_waited_for(self):
        (self.tmp / "slurm-bin" / "sinfo").write_text("#!/bin/sh\nsleep 5\n")
        ws = self.server.workspace
        ws.cluster.ASK_SECONDS = 1
        ws.cluster.forget()
        start = time.time()
        status, cluster = self.call("GET", "/api/cluster")
        self.assertEqual(status, 200, cluster)
        self.assertLess(time.time() - start, 4)
        self.assertIn("didn't answer", cluster["slurm_problem"])
        self.assertEqual(cluster["partitions"], [])

    def test_finding_uv_and_pixi_needs_no_login_shell(self):
        envs = self.server.workspace.envs
        calls = []
        real = envs._hpclib
        envs._hpclib = lambda command, timeout=60: (calls.append(command), real(command, timeout))[1]
        for _ in range(3):
            self.call("POST", "/api/jobs/render", {"spec": self.spec})
        status, card = self.call("GET", "/api/env?path=" + urllib.parse.quote(self.script))
        self.assertEqual((status, calls), (200, []))                 # not even the card asks hpclib_pytools
        self.assertEqual(card["tools"]["uv"]["path"], str(self.home / ".local" / "bin" / "uv"))
        self.assertEqual((card["tools"]["uv"]["version"], card["tools"]["pixi"]["path"], card["dirs_loaded"]), ("uv 0.9.9", "", False))
        status, settings = self.call("GET", "/api/env/settings")     # the directories are the slow part: asked when wanted
        self.assertEqual((status, len(calls)), (200, 1))
        self.assertIn("UV_CACHE_DIR", settings["dirs"])

    def test_a_script_goes_from_file_to_followed_output(self):
        status, card = self.call("GET", "/api/env?path=" + urllib.parse.quote(self.script))
        self.assertEqual((status, card["status"]), (200, "not prepared"), card)
        self.assertEqual(card["tools"]["uv"]["path"], str(self.home / ".local" / "bin" / "uv"))
        status, rendered = self.call("POST", "/api/jobs/render", {"spec": self.spec})
        self.assertEqual(status, 200, rendered)
        self.assertTrue(rendered["environment"]["blocks"])
        status, out = self.call("POST", "/api/jobs/submit", {"spec": self.spec})
        self.assertEqual(status, 409, out)
        op = self.prepare()
        self.assertEqual(op["state"], "succeeded", op)
        self.assertIn("synced", op["log"])
        status, card = self.call("GET", "/api/env?path=" + urllib.parse.quote(self.script))
        self.assertEqual(card["status"], "ready", card)
        self.assertEqual(card["info"]["packages"], 1)
        status, check = self.call("POST", "/api/jobs/check", {"spec": self.spec})
        self.assertTrue(check["ok"], check)
        self.assertIn("python -u", (self.home / "test-only.sh").read_text())
        status, done = self.call("POST", "/api/jobs/submit", {"spec": self.spec})
        self.assertEqual(status, 200, done)
        self.assertEqual(done["job_id"], "4242")
        self.assertEqual(done["output_path"], str(self.home / "proj" / "train-4242.out"))
        script = (self.home / "proj" / "train.slurm").read_text()
        self.assertIn("export VIRTUAL_ENV=", script)
        self.assertIn("trap hpc_term TERM", script)
        record = json.loads((self.home / ".local" / "tunnels" / "workspace" / "runs" / "4242.json").read_text())
        self.assertEqual(record["environment"]["packages"], 1)
        self.assertEqual(record["spec"]["resources"]["partition"], "gpu")
        (self.home / "proj" / "train-4242.out").write_text("epoch 1\n")
        status, first = self.call("GET", "/api/fs/tail?path=" + urllib.parse.quote(done["output_path"]))
        self.assertEqual(first["data"], "epoch 1\n")
        with open(done["output_path"], "a") as f:
            f.write("epoch 2\n")
        status, more = self.call("GET", "/api/fs/tail?path={}&offset={}".format(urllib.parse.quote(done["output_path"]), first["offset"]))
        self.assertEqual(more["data"], "epoch 2\n")
        self.assertEqual(self.call("GET", "/api/fs/tail?path=" + urllib.parse.quote(str(self.home / "nope.out")))[1]["exists"], False)

    def test_a_stale_environment_blocks_submit_unless_the_job_syncs(self):
        self.assertEqual(self.prepare()["state"], "succeeded")
        later = time.time() + 30
        os.utime(self.home / "proj" / "pyproject.toml", (later, later))
        status, card = self.call("GET", "/api/env?path=" + urllib.parse.quote(self.script))
        self.assertEqual(card["status"], "stale", card)
        status, out = self.call("POST", "/api/jobs/submit", {"spec": self.spec})
        self.assertEqual(status, 409, out)
        self.assertIn("stale", out["error"])
        synced = dict(self.spec, environment={"mode": "sync"})
        status, done = self.call("POST", "/api/jobs/submit", {"spec": synced})
        self.assertEqual(status, 200, done)
        self.assertIn("uv run --project", (self.home / "proj" / "train.slurm").read_text())

    def test_a_busy_environment_blocks_and_a_missing_tool_is_said(self):
        marker = self.home / "proj" / ".venv.hpclib-syncing"
        marker.write_text(json.dumps({"pid": os.getpid(), "started": time.time(), "kind": "sync", "id": "x"}))
        status, card = self.call("GET", "/api/env?path=" + urllib.parse.quote(self.script))
        self.assertEqual(card["status"], "busy", card)
        self.assertEqual(self.call("POST", "/api/jobs/submit", {"spec": self.spec})[0], 409)
        marker.unlink()
        (self.home / ".local" / "bin" / "uv").unlink()
        status, card = self.call("GET", "/api/env?path=" + urllib.parse.quote(self.script))
        self.assertEqual(card["status"], "tool missing", card)
        status, out = self.call("POST", "/api/env/prepare", {"project": str(self.home / "proj")})
        self.assertEqual(status, 412, out)

    def test_hand_edited_scripts_and_bad_specs(self):
        status, out = self.call("POST", "/api/jobs/render", {"spec": dict(self.spec, command="")})
        self.assertEqual(status, 200)
        self.assertTrue(out["errors"] and out["script"] is None)
        status, done = self.call("POST", "/api/jobs/submit", {"script": "#!/bin/bash\necho hi\n", "dir": str(self.home / "proj"),
                                                              "name": "hand.slurm"})
        self.assertEqual(status, 200, done)
        self.assertEqual(self.call("POST", "/api/jobs/render", {"spec": "x"}, headers={})[0], 400)
        status, tools = self.call("POST", "/api/env/dirs", {"var": "UV_CACHE_DIR", "dir": str(self.home / "uvcache")})
        self.assertEqual(status, 200, tools)
        self.assertEqual(tools["dirs"]["UV_CACHE_DIR"]["value"], str(self.home / "uvcache"))
        self.assertEqual(self.call("POST", "/api/env/dirs", {"var": "PATH", "dir": "/x"})[0], 400)

    def test_runs_are_listed_with_state_and_cancelled(self):
        self.assertEqual(self.prepare()["state"], "succeeded")
        ids, records = [], []
        for n, name in enumerate(("a", "b")):
            (self.home / "proj" / "sub").mkdir(exist_ok=True)
            spec = dict(self.spec, name=name, workdir=str(self.home / "proj" / ("sub" if n else "")).rstrip("/"))
            status, done = self.call("POST", "/api/jobs/submit", {"spec": spec, "overwrite": True})
            self.assertEqual(status, 200, done)
            ids.append(done["job_id"])
            # the fake sbatch always answers 4242: file each submission's record under an id of its own
            run = self.home / ".local" / "tunnels" / "workspace" / "runs" / "4242.json"
            record = json.loads(run.read_text())
            record["job_id"], record["submitted"] = str(4242 + n), time.time() + n
            records.append(record)
        for record in records:
            (run.parent / (record["job_id"] + ".json")).write_text(json.dumps(record))
        status, listing = self.call("GET", "/api/runs")
        self.assertEqual(status, 200, listing)
        self.assertEqual([(r["job_id"], r["state"], r["ended"]) for r in listing["runs"]],
                         [("4243", "RUNNING", False), ("4242", "RUNNING", False)])
        here = self.call("GET", "/api/runs?dir=" + urllib.parse.quote(str(self.home / "proj" / "sub")))[1]["runs"]
        self.assertEqual([r["job_id"] for r in here], ["4243"])
        everything = self.call("GET", "/api/runs?dir=" + urllib.parse.quote(str(self.home / "proj")))[1]["runs"]
        self.assertEqual(len(everything), 2)
        # the jobs end: squeue forgets them, sacct says how; the answer is kept in the record
        (self.home / "squeue-empty").write_text("")
        (self.home / "sacct.out").write_text("4242|COMPLETED|0:0|00:01:00\n4243|FAILED|1:0|00:00:05\n")
        states = {r["job_id"]: (r["state"], r["exit_code"], r["ended"]) for r in self.call("GET", "/api/runs")[1]["runs"]}
        self.assertEqual(states, {"4242": ("COMPLETED", "0:0", True), "4243": ("FAILED", "1:0", True)})
        (self.home / "sacct.out").unlink()
        again = {r["job_id"]: r["state"] for r in self.call("GET", "/api/runs")[1]["runs"]}
        self.assertEqual(again, {"4242": "COMPLETED", "4243": "FAILED"})        # remembered: no scheduler call needed
        # cancelling: a run of ours, not anything else
        self.assertEqual(self.call("POST", "/api/runs/4243/cancel", {})[0], 200)
        self.assertEqual((self.home / "scancel.log").read_text().strip(), "4243")
        self.assertEqual(self.call("POST", "/api/runs/9999/cancel", {})[0], 404)
        self.assertEqual(self.call("POST", "/api/runs/4243;ls/cancel", {})[0], 404)

    def test_an_array_or_sweep_is_one_run_with_its_tasks_counted(self):
        spec = dict(self.spec, name="sweep", environment={"mode": "none", "shell_init": "none"},
                    sweep={"LR": ["0.1", "0.01"], "SEED": ["1", "2"]}, array={"limit": 2},
                    dependency={"type": "afterok", "jobs": ["77"]}, arguments="--lr {LR}")
        status, rendered = self.call("POST", "/api/jobs/render", {"spec": spec})
        self.assertEqual((status, rendered["errors"]), (200, []), rendered)
        for line in ("#SBATCH --array=0-3%2", "#SBATCH --dependency=afterok:77", "#SBATCH --output=%x-%A_%a.out", '--lr "${LR}"'):
            self.assertIn(line, rendered["script"])
        status, done = self.call("POST", "/api/jobs/submit", {"spec": spec})
        self.assertEqual(status, 200, done)
        self.assertEqual(done["output_path"], str(self.home / "proj" / "sweep-4242_0.out"))   # the first task's file
        self.assertEqual(done["run"]["tasks"], 4)
        # squeue shows two tasks running and a range pending: one run, one state, the counts beside it
        fake = self.tmp / "slurm-bin" / "squeue"
        fake.write_text("#!/bin/sh\necho '4242_0|RUNNING|None|0:10'\necho '4242_1|RUNNING|None|0:09'\n"
                        "echo '4242_[2-3%2]|PENDING|JobArrayTaskLimit|0:00'\n")
        run = self.call("GET", "/api/runs")[1]["runs"][0]
        self.assertEqual((run["state"], run["tasks"], run["task_total"], run["ended"]), ("RUNNING", {"RUNNING": 2, "PENDING": 2}, 4, False))
        # all over: sacct lists the tasks; one failure makes the run FAILED, and it is kept
        fake.write_text("#!/bin/sh\nexit 0\n")
        (self.home / "sacct.out").write_text("4242_0|COMPLETED|0:0|00:01:00\n4242_1|FAILED|1:0|00:00:05\n"
                                             "4242_2|COMPLETED|0:0|00:01:00\n4242_3|COMPLETED|0:0|00:01:00\n")
        run = self.call("GET", "/api/runs")[1]["runs"][0]
        self.assertEqual((run["state"], run["ended"], run["tasks"]), ("FAILED", True, {"COMPLETED": 3, "FAILED": 1}))
        bad = self.call("POST", "/api/jobs/render", {"spec": dict(spec, sweep={"X": ["a;b"]})})[1]
        self.assertTrue(bad["errors"])

    def test_a_hand_written_script_is_imported_diffed_adopted_or_kept(self):
        mine = self.home / "proj" / "mine.slurm"
        original = "#!/bin/bash\n#SBATCH -J mine\n#SBATCH --time=01:00:00\n#SBATCH --mail-type=END\n\nmodule load GCC\npython train.py\n"
        mine.write_text(original)
        status, got = self.call("GET", "/api/jobs/import?path=" + urllib.parse.quote(str(mine)))
        self.assertEqual((status, got["source"], got["differs"], got["errors"]), (200, "imported", True, []), got)
        self.assertEqual((got["spec"]["name"], got["spec"]["kind"], got["spec"]["extra_sbatch"]), ("mine", "body", ["#SBATCH --mail-type=END"]))
        self.assertIn("python train.py", got["script"])
        self.assertEqual(got["text"], original)
        # keep it as it is: sbatch the file where it lies, but only if it is what the page was shown
        status, done = self.call("POST", "/api/jobs/submit", {"script": original, "dir": str(self.home / "proj"), "name": "mine.slurm", "as_is": True})
        self.assertEqual((status, done["job_id"], mine.read_text()), (200, "4242", original), done)
        self.assertEqual((self.home / "sbatch.log").read_text().split(" in ")[0], "sbatch mine.slurm")
        mine.write_text(original + "# edited\n")
        self.assertEqual(self.call("POST", "/api/jobs/submit", {"script": original, "dir": str(self.home / "proj"), "name": "mine.slurm", "as_is": True})[0], 409)
        mine.write_text(original)
        # adopt the form's version: the file is rewritten, the original kept beside it
        status, out = self.call("POST", "/api/jobs/adopt", {"path": str(mine), "spec": got["spec"]})
        self.assertEqual(status, 200, out)
        self.assertEqual((self.home / "proj" / "mine.slurm.bak").read_text(), original)
        written = mine.read_text()
        self.assertIn("#HPCLIB-SPEC", written)
        self.assertIn("python train.py", written)
        again = self.call("GET", "/api/jobs/import?path=" + urllib.parse.quote(str(mine)))[1]
        self.assertEqual((again["source"], again["differs"]), ("embedded", False))        # the form's own file: nothing to decide
        self.assertEqual(self.call("GET", "/api/jobs/import?path=" + urllib.parse.quote(str(self.home / "proj")))[0], 400)

    def test_an_interactive_session_line(self):
        spec = dict(self.spec, name="bash", resources={"time": "2h", "memory": "4G", "partition": "gpu", "gpus": 1}, modules=["GCC/13.3.0"],
                    sweep={"A": ["1", "2"]})
        status, out = self.call("POST", "/api/jobs/interactive", {"spec": spec})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["command"], "srun --job-name=interactive --time=2:00:00 --nodes=1 --ntasks-per-node=1 --mem=4G --gres=gpu:1 "
                                         "--partition=gpu --pty bash -lc 'module load GCC/13.3.0; exec bash -il'")
        self.assertEqual(out["workdir"], str(self.home / "proj"))
        self.assertTrue(any("sweep" in w for w in out["warnings"]))
        self.assertEqual(self.call("POST", "/api/jobs/interactive", {"spec": dict(spec, resources={"time": "soon"})})[0], 400)

    def test_profiles_last_used_project_and_saved(self):
        self.assertEqual(self.call("GET", "/api/profiles")[1], {"last": None, "project": None, "profiles": []})
        spec = dict(self.spec, environment={"mode": "none", "shell_init": "none"})
        self.assertEqual(self.call("POST", "/api/jobs/submit", {"spec": spec})[0], 200)
        listing = self.call("GET", "/api/profiles")[1]
        self.assertEqual(listing["last"]["resources"]["partition"], "gpu")           # a submission is remembered, server side
        self.assertNotIn("command", listing["last"])                                  # but never what was run
        status, saved = self.call("POST", "/api/profiles", {"name": "big gpu", "spec": dict(spec, resources={"time": "1d", "gpus": 4})})
        self.assertEqual((status, saved["spec"]["resources"]["time"], saved["spec"]["resources"]["gpus"]), (200, "1-00:00:00", 4), saved)
        self.assertEqual([p["name"] for p in self.call("GET", "/api/profiles")[1]["profiles"]], ["big gpu"])
        self.assertEqual(self.call("POST", "/api/profiles", {"name": "../x", "spec": {}})[0], 400)
        self.assertEqual(self.call("POST", "/api/profiles", {"name": "bad", "spec": {"resources": {"time": "soon"}}})[0], 400)
        project = self.home / "proj"
        (project / ".hpclib").mkdir()
        (project / ".hpclib" / "job.json").write_text('{"resources": {"time": "30m", "partition": "cpu"}, "modules": ["GCC/13.3.0"]}')
        here = self.call("GET", "/api/profiles?dir=" + urllib.parse.quote(str(project / "sub" / "deeper")))[1]["project"]
        self.assertEqual((here["path"], here["spec"]["resources"]["partition"], here["spec"]["modules"]),
                         (str(project / ".hpclib" / "job.json"), "cpu", ["GCC/13.3.0"]))
        (project / ".hpclib" / "job.json").write_text("[1]")
        self.assertIn("problem", self.call("GET", "/api/profiles?dir=" + urllib.parse.quote(str(project)))[1]["project"])
        self.assertEqual(self.call("POST", "/api/profiles/delete", {"name": "big gpu"})[0], 200)
        self.assertEqual(self.call("POST", "/api/profiles/delete", {"name": "big gpu"})[0], 404)

    def test_fs_tail_follows_a_file_and_notices_a_replacement(self):
        log = self.home / "proj" / "log.txt"
        quote = urllib.parse.quote(str(log))
        self.assertFalse(self.call("GET", "/api/fs/tail?path=" + quote)[1]["exists"])
        log.write_text("one\n")
        first = self.call("GET", "/api/fs/tail?path=" + quote)[1]
        self.assertEqual((first["data"], first["restarted"]), ("one\n", False))
        with open(log, "a") as f:
            f.write("two\n")
        more = self.call("GET", f"/api/fs/tail?path={quote}&offset={first['offset']}&id={first['id']}")[1]
        self.assertEqual((more["data"], more["restarted"]), ("two\n", False))
        # replaced by a new file (a rotated log): a new id, so the follower starts again, saying so
        log.rename(self.home / "proj" / "log.old")
        log.write_text("three-longer-than-before\n" * 3)
        fresh = self.call("GET", f"/api/fs/tail?path={quote}&offset={more['offset']}&id={more['id']}")[1]
        self.assertTrue(fresh["restarted"])
        self.assertTrue(fresh["data"].startswith("three"))
        self.assertEqual(self.call("GET", "/api/fs/tail?path=" + urllib.parse.quote(str(self.home / "proj")))[0], 400)
        self.assertEqual(self.call("GET", "/api/fs/tail?path=" + quote + "&offset=x")[0], 400)


class TestShellEnv(unittest.TestCase):
    """The login shell is asked once; programs then run directly with what it said."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "hello").write_text('#!/bin/sh\necho "hello $* $FROM_PROFILE"; echo oops >&2\n')
        (self.tmp / "bin" / "hello").chmod(0o755)
        (self.tmp / ".bash_profile").write_text(f'echo "a banner that must not leak"\nexport FROM_PROFILE=yes\n'
                                                f'export PATH="{self.tmp}/bin:$PATH"\n')
        patch = unittest.mock.patch.dict(os.environ, {"HOME": str(self.tmp), "WORKSPACE_TOKEN": "secret"})
        patch.start()
        self.addCleanup(patch.stop)
        self.shell = workspace_server.shellenv.ShellEnv()

    def test_runs_programs_with_the_profile_environment(self):
        code, out = self.shell.run(["hello", "there"])
        self.assertEqual((code, out), (0, "hello there yes\noops\n"))        # stdout and stderr, no banner
        self.assertNotIn("WORKSPACE_TOKEN", self.shell.env())
        self.assertEqual(self.shell.run(["no-such-program-here"])[0], 127)

    def test_a_program_that_appears_later_is_found_after_a_fresh_look(self):
        self.shell.env()
        (self.tmp / "bin" / "late").write_text("#!/bin/sh\necho late\n")
        (self.tmp / "bin" / "late").chmod(0o755)
        self.assertEqual(self.shell.run(["late"]), (0, "late\n"))

    def test_arguments_are_not_parsed_by_a_shell(self):
        code, out = self.shell.run(["hello", "$(echo injected); x"])
        self.assertIn("$(echo injected); x", out)

    def test_login_runs_shell_functions(self):
        code, out = self.shell.login("f() { echo fn-$1; }; f 7")
        self.assertEqual((code, out), (0, "fn-7\n"))
