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
        for f in ("workspace_server.py", "script_templates.py", "ui/index.html", "ui/workspace.js",
                  "script_templates/python-uv.sh", "sbatch_script.sh", "install.sh", "token_auth.py"):
            self.assertTrue((unpacked / f).is_file(), f)
        self.assertEqual((unpacked / "token_auth.py").read_bytes(), (ROOT / "hpclib/servers/token_auth.py").read_bytes())

    def test_token_regex_finds_what_the_server_prints(self):
        import re
        manifest = json.loads((PACKAGE / "hpclib-package.json").read_text())
        pattern = re.compile(manifest["apps"]["workspace"]["token_regex"])
        line = "hpclib workspace 1.0.0 on login1: http://127.0.0.1:8640/?token=Ab-c_123456789012345678901234"
        self.assertEqual(pattern.findall(line), ["Ab-c_123456789012345678901234"])

    def test_tunnel_scripts_parse_and_python36(self):
        for script in TUNNEL.glob("*.sh"):
            self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0, script)
        for py in ("workspace_server.py", "script_templates.py"):
            text = (TUNNEL / py).read_text()
            # the login node's python3 may be 3.6: no walrus, capture_output, text=, removeprefix, f"{x=}"
            for newer in (":=", "capture_output", "text=True", "removeprefix", "removesuffix", "=}"):
                self.assertNotIn(newer, text, f"{py}: {newer}")


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
        # jobs, from a login shell's squeue
        status, jobs = self.call("GET", "/api/jobs")
        self.assertEqual(status, 200, jobs)
        self.assertEqual([(j["id"], j["state"], j["dir"]) for j in jobs["jobs"]],
                         [("4242", "RUNNING", "/home/x/proj"), ("4243", "PENDING", "/home/x")])
        self.assertEqual(self.call("POST", "/api/cancel", {"job": "4243"})[0], 200)
        self.assertEqual((self.home / "scancel.log").read_text().strip(), "4243")
        self.assertEqual(self.call("POST", "/api/cancel", {"job": "1; rm -rf ~"})[0], 400)


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


if __name__ == "__main__":
    unittest.main()
