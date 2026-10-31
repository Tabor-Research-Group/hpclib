#!/usr/bin/env python3
"""
The Workspace: your files on the cluster in the browser, an editor, viewers, terminals that outlive the tab, guided job
submission, and the runs you have made. It runs on the login node as you (the workspace tunnel's sbatch_script.sh starts
it), listens on 127.0.0.1 only, and answers only with the token it prints at start (the console's Open adds it):
a login node is shared, and anyone on it could reach the port.

The token is a credential for the API, so it travels only where page JavaScript can set a header: every /api/ call
sends it as X-Workspace-Token, and nothing accepts it in a URL. The two things a header can't carry get their own,
short-lived credentials: the terminal's WebSocket opens with a single-use ticket (POST /api/term/ticket), and the
viewer's files (<img>, <iframe>, <video>) come from signed URLs that expire and cover one directory
(POST /api/raw-url). Requests that change something must be JSON (or, for PUT /api/file, a body with the token
header), and a request that names an Origin other than the page's own is refused.

  workspace_server.py --port PORT [--root DIR] [--cdn URL] [--edit-limit-mb N] [--confirm-mb N]

This file is the HTTP side: the token, the routes, the page's own files, the signed file URLs. The work is in ws/
(common, shellenv, fs, terminal, cluster, envs, jobspec, jobs, profiles, templates), which know nothing of HTTP.

The page (ui/) carries CodeMirror 6 (the editor) itself and loads xterm.js (the terminal) and Wunderbaum (the file tree) from --cdn, an npm
mirror (jsDelivr's by default). Routes, all but /health and the page's own files needing the token (X-Workspace-Token):

  GET  /health                       {"ok", "version"}
  GET  /api/info                     home, user, host, hpclib.sh's path, places (home, scratch), limits, the CDN
  GET  /api/list?path=[&hidden=1]    a directory: {path, parent, entries: [{name, type, size, mtime, mode, target?}]}
  GET  /api/stat?path=
  GET  /api/file?path=               the file's bytes for the editor (X-Mtime-Ns), up to the editor's limit
  PUT  /api/file?path=[&mtime_ns=][&how=create|replace|any]   the body to the file through a temporary file and a
                                     rename, keeping its mode; refused when it changed since mtime_ns (409)
  POST /api/mkdir {path}  /api/rename {from, to}  /api/delete {path}   (delete: a file, link or empty directory)
  GET  /api/fs/tail?path=[&offset=][&id=]   the bytes a file has gained since offset (the last 16 KiB first), with an
                                     id that changes when the file is replaced: following a log, a job's output, a file
                                     open in the editor
  GET  /api/usage?path=[&limit=]     a directory's folders by size (du -k) and file count (du --inodes, else a walk),
                                     at low priority, up to limit seconds (120; at most 600; then partial)
  POST /api/raw-url {path}           {url}: a signed URL for the file (below), good for an hour, for the files in
                                     its directory
  GET  /raw/EXPIRES.DEPTH.SIGNATURE/PATH   a file for an <img>, <iframe> or <video>, with Range; HTML and SVG with a
                                     sandboxing Content-Security-Policy (an opaque origin: no access to the page).
                                     DEPTH is how many leading parts of PATH the signature covers: a page's own
                                     images and scripts load, other directories don't
  POST /api/term/ticket              {ticket}: opens one terminal connection, within ten seconds
  GET  /api/terms                    the terminals that are running: [{id, cwd, title, created, alive, attached}]
  POST /api/term/close {id}          hang one up
  GET  /terminal?ticket=[&session=ID][&cwd=&cols=&rows=]   WebSocket: your login shell in a pseudo-terminal that keeps
                                     running when the page leaves (binary frames both ways; text frames JSON: {"type":
                                     "resize", "cols", "rows"} in, {"type": "session", "id"} first and {"type": "exit",
                                     "code"} out). With session, it attaches to that terminal and replays its last 256 KiB
  GET  /api/templates                the classic script templates (the Workspace's and yours), with the values last used
  POST /api/templates/edit {name, from?}   your copy to edit, in $HPCTUNNELS_DATA_DIR/script_templates
  POST /api/render {template, text?, input, values}   -> {script, name, dir, values, fields}
  POST /api/submit {script, name, dir, overwrite?, template?, values?}   write the job script, sbatch it -> {job_id}
  GET  /api/cluster[?refresh=1]      partitions, GPU types and accounts as Slurm reports them (cached five minutes)
  GET  /api/modules?q=               module names containing q (module -t avail, cached ten minutes)
  GET  /api/env?path=                the environment card for a file or directory: project, uv or pixi, status, tools
  GET  /api/env/settings             uv's and pixi's directories (hpclib_pytools --json: slow, so the card asks when that part opens)
  POST /api/env/prepare {project, update?}   uv sync / pixi install as you, in the background -> {id, ...}
  POST /api/env/tool {tool}          install uv or pixi (hpclib_pytools install)
  POST /api/env/dirs {var, dir}      remember uv's or pixi's cache/python directory for this cluster (or "default")
  GET  /api/env/operation?id=        state and log of one of those
  POST /api/jobs/render {spec}       the job spec -> {script, spec, errors, warnings, environment}
  GET  /api/jobs/import?path=        a job script for the form: its spec (embedded, or read from the #SBATCH lines), the
                                     form's version of it, and whether they differ
  POST /api/jobs/adopt {path, spec}  write the form's version over the file (the original is kept as NAME.bak)
  POST /api/jobs/interactive {spec}  the srun --pty line for an interactive session with these resources -> {command, workdir}
  POST /api/jobs/check {spec | script, dir?}   sbatch --test-only: {ok, message, start}
  POST /api/jobs/submit {spec | script, dir?, script_dir?, name?, overwrite?, as_is?, allow_blocked?}   writes the script, sbatch, keeps a run
                                     record in $HPCTUNNELS_DATA_DIR/workspace/runs -> {job_id, path, output_path}; 409
                                     while the environment is not prepared, stale, busy or failed
  GET  /api/job?id=                  one job's state (squeue, else sacct)
  GET  /api/runs[?dir=]              the runs the Workspace made, newest first, with state (one squeue, one sacct);
                                     dir keeps those whose working directory is it or below it
  GET  /api/profiles[?dir=]           the profiles: last used, the project's .hpclib/job.json, and the saved ones
  POST /api/profiles {name, spec}     keep a profile;  POST /api/profiles/delete {name}
  POST /api/runs/ID/cancel           scancel one of them

Python 3.6+, standard library only.
"""
import argparse
import http.server
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

from token_auth import ServiceToken, CapabilityTickets, ResourceSigner

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ws import cluster, envs, fs, jobs, profiles, shellenv, templates, terminal  # noqa: E402
from ws.common import MAX_JSON, Refused, data_dir, oserror, resolve, templates_dir  # noqa: E402,F401
from ws.terminal import ws_accept, ws_read, ws_send  # noqa: E402,F401  (the tests speak the protocol)

VERSION = "1.7.0"
UI_DIR = os.path.join(HERE, "ui")
CHUNK = 1 << 16
# a page the viewer shows is somebody's file, not the Workspace's: it may run, open links and offer downloads,
# and nothing more (no forms, no dialogs), in an origin of its own
SANDBOX_CSP = "sandbox allow-scripts allow-popups allow-downloads"
RAW_URL_SECONDS = 3600       # how long a viewer's file URL works (a movie keeps requesting ranges while it plays)
TICKET_SECONDS = 10          # how long a terminal's ticket works: the page uses it at once
SANDBOXED_TYPES = ("text/html", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/xml")
DEFAULT_EDIT_MB = 64
for _type, _ext in (("text/markdown", ".md"), ("text/x-python", ".py"), ("application/x-sh", ".sh"),
                    ("video/mp4", ".m4v"), ("video/quicktime", ".mov"), ("image/webp", ".webp")):
    mimetypes.add_type(_type, _ext)


class Workspace:
    """What the server serves: one object holding the parts in ws/ and the facts about this login node."""

    def __init__(self, root=None, cdn="https://cdn.jsdelivr.net/npm", edit_limit_mb=DEFAULT_EDIT_MB, confirm_mb=20):
        self.root = resolve(root) if root else os.path.expanduser("~")
        self.cdn = cdn.rstrip("/")
        self.edit_limit = int(edit_limit_mb * (1 << 20))
        self.confirm = int(confirm_mb * (1 << 20))
        home = os.path.expanduser("~")
        self.shell = shellenv.ShellEnv()
        info = self.info()
        self.files = fs.Files(self.root, self.edit_limit, self.confirm)
        self.terminals = terminal.Sessions()
        self.cluster = cluster.Cluster(self.shell, info["user"], info["hpclib"])
        self.envs = envs.Environments(self.shell.login, info["hpclib"], home, lambda: os.path.join(data_dir(), "workspace"), self.shell.env)
        self.jobs = jobs.Jobs(self.shell, self.cluster, self.envs, home)
        self.profiles = profiles.Profiles(home)
        self.jobs.on_submit = self.profiles.remember
        self.templates = templates.Templates(self.info, self.jobs)

    def info(self):
        home = os.path.expanduser("~")
        user = os.environ.get("USER") or os.path.basename(home)
        hpclib = None
        for candidate in (os.environ.get("HPCLIB_DIR"),
                          os.path.join(home, os.environ.get("HPCLIB_REMOTE_INSTALL_LOCATION") or "hpclib")):
            if candidate and os.path.isfile(os.path.join(candidate, "hpclib.sh")):
                hpclib = os.path.join(os.path.abspath(candidate), "hpclib.sh")
                break
        places = [("Home", home)]
        if self.root != home:
            places.insert(0, ("Start", self.root))
        for label, p in (("Scratch", os.environ.get("SCRATCH")), ("Scratch", "/scratch/user/" + user),
                         ("Scratch", "/scratch/" + user), ("Work", os.environ.get("WORK"))):
            if p and os.path.isdir(p) and p not in [x[1] for x in places]:
                places.append((label, p))
        return {"home": home, "user": user, "host": socket.gethostname(), "hpclib": hpclib, "root": self.root,
                "templates_dir": templates_dir(), "places": [{"label": l, "path": p} for l, p in places],
                "limits": {"edit_bytes": self.edit_limit, "confirm_bytes": self.confirm}, "cdn": self.cdn,
                "version": VERSION, "python": sys.version.split()[0]}

    def env_card(self, path):
        try:
            return self.envs.card(resolve(path))
        except envs.envinfo.EnvironmentError_ as e:
            raise Refused(e.status, str(e))

    def env_do(self, what, body):
        error = envs.envinfo.EnvironmentError_
        try:
            if what == "prepare":
                return self.envs.prepare(resolve(body.get("project")), bool(body.get("update")))
            if what == "tool":
                return self.envs.install_tool(body.get("tool"))
            if what == "dirs":
                return self.envs.set_dir(body.get("var"), body.get("dir"))
            if what == "operation":
                return self.envs.operation(body.get("id"))
        except error as e:
            raise Refused(e.status, str(e))
        raise Refused(404, "no such environment action")

    def close(self):
        self.terminals.close_all()


# -- HTTP ------------------------------------------------------------------------------------------------------------

# -- HTTP ------------------------------------------------------------------------------------------------------------

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, token, workspace):
        self.auth = ServiceToken(token, "hpclib:workspace")
        self.ticket_store = CapabilityTickets("hpclib:workspace")
        self.raw_signer = ResourceSigner(token, "hpclib:workspace")
        self.workspace = workspace
        http.server.HTTPServer.__init__(self, address, Handler)

    # -- terminal tickets: a WebSocket can't carry the token in a header, so the page asks for one use of it

    def new_ticket(self):
        return self.ticket_store.issue("terminal", "login-shell", TICKET_SECONDS)

    def redeem_ticket(self, ticket):
        return self.ticket_store.redeem(ticket, "terminal", "login-shell")

    # -- signed file URLs: /raw/EXPIRES.DEPTH.SIGNATURE/PATH, where the signature covers the first DEPTH parts of
    # PATH (the file's directory) and the expiry, so a URL reads that directory for an hour and is no credential

    def sign_raw(self, expires, depth, directory):
        return self.raw_signer.sign("raw", expires, [depth, directory])

    def raw_url(self, path):
        directory = os.path.dirname(path)
        parts = [p for p in directory.split("/") if p]
        if not parts:
            raise Refused(400, "files directly in / can't be shown")
        expires = int(time.time()) + RAW_URL_SECONDS
        return "/raw/{}.{}.{}{}".format(expires, len(parts), self.sign_raw(expires, len(parts), directory),
                                        urllib.parse.quote(path))

    def verify_raw(self, prefix, path):
        """The absolute path a signed URL's `prefix` allows `path` (already unquoted) to be, else Refused."""
        wrong = Refused(401, "this file's link has expired or isn't valid (open the file again)")
        try:
            expires, depth, signature = prefix.split(".")
            expires, depth = int(expires), int(depth)
        except ValueError:
            raise wrong
        path = os.path.normpath(path)             # a ".." can't leave the signed directory: it is signed as it is
        parts = [p for p in path.split("/") if p]
        if depth < 1 or depth >= len(parts) or expires < time.time():
            raise wrong
        directory = "/" + "/".join(parts[:depth])
        if not self.raw_signer.check(signature, "raw", expires, [depth, directory]):
            raise wrong
        return path

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):    # a page that went away
            http.server.HTTPServer.handle_error(self, request, client_address)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "hpclib-workspace/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("WORKSPACE_LOG_REQUESTS"):
            sys.stderr.write("[workspace] " + (fmt % args) + "\n")

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    # -- answers -------------------------------------------------------------------------------------------------

    def send_json(self, status, payload):
        body = (json.dumps(payload) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        return re.fullmatch(r"(127\.0\.0\.1|localhost|\[::1\])(:\d+)?", host) is not None

    def authorized(self):
        """Whether the request carries the token, in its header: never in a URL, where it would be kept in
        histories and handed to every page the viewer shows."""
        given = self.headers.get("X-Workspace-Token") or ""
        return self.server.auth.check(given, "hpclib:workspace")

    def same_origin(self):
        """Whether the request comes from the Workspace's own page: a request that names another Origin (a page the
        viewer shows has the Origin `null`) or is marked cross-site is refused."""
        host = (self.headers.get("Host") or "").lower()
        origin = self.headers.get("Origin")
        if origin is not None and urllib.parse.urlsplit(origin.lower()).netloc != host:
            return False
        return (self.headers.get("Sec-Fetch-Site") or "same-origin") in ("same-origin", "none")

    def json_body(self):
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise Refused(415, "send JSON (Content-Type: application/json)")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_JSON:
            raise Refused(413, "the request is too large")
        raw = self.rfile.read(length) if length else b""
        try:
            out = json.loads(raw.decode() or "{}")
        except ValueError:
            raise Refused(400, "not JSON")
        if not isinstance(out, dict):
            raise Refused(400, "a JSON object")
        return out

    def dispatch(self, verb):
        url = urllib.parse.urlsplit(self.path)
        self.query = urllib.parse.parse_qs(url.query)
        path = url.path
        try:
            if not self.host_ok():
                raise Refused(421, "unexpected Host header: open the Workspace through its tunnel")
            if path == "/health":
                return self.send_json(200, {"ok": True, "version": VERSION, "service": "hpclib-workspace"})
            if verb in ("GET", "HEAD") and (path == "/" or path.startswith("/ui/")):
                return self.static(path)
            if path.startswith("/raw/") and verb in ("GET", "HEAD"):
                prefix, _, rest = path[len("/raw/"):].partition("/")
                return self.send_file(self.server.verify_raw(prefix, urllib.parse.unquote("/" + rest)), editing=False)
            if not self.same_origin():
                raise Refused(403, "the Workspace answers only its own page")
            if path == "/terminal" and verb == "GET":
                if not self.server.redeem_ticket(self.query.get("ticket", [""])[-1]):
                    raise Refused(401, "the terminal's ticket is missing, used or too old")
                return self.terminal()
            if not self.authorized():
                raise Refused(401, "the Workspace's token is missing or wrong (Open it from the console again)")
            status, payload = self.route(verb, path)
            if payload is not None:
                self.send_json(status, payload)
        except Refused as e:
            self.close_connection = True       # a body may be left unread
            self.send_json(e.status, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self.close_connection = True
            self.send_json(500, {"error": "{}: {}".format(type(e).__name__, e)})

    def route(self, verb, path):
        ws = self.server.workspace
        q = lambda k, d=None: self.query.get(k, [d])[-1]      # noqa: E731

        def number(name, what):
            value = q(name)
            if value in (None, ""):
                return None
            try:
                return int(value)
            except ValueError:
                raise Refused(400, "{}: {}".format(name, what))
        if path == "/api/info" and verb == "GET":
            return 200, ws.info()
        if path == "/api/list" and verb == "GET":
            return 200, ws.files.list(q("path") or ws.root, q("hidden") == "1")
        if path == "/api/stat" and verb == "GET":
            return 200, ws.files.stat(q("path") or ws.root)
        if path == "/api/file" and verb in ("GET", "HEAD"):
            return self.send_file(q("path") or "", editing=True)
        if path == "/api/file" and verb == "PUT":
            length = int(self.headers.get("Content-Length") or 0)
            return 200, ws.files.write(q("path") or "", self.rfile, length, q("mtime_ns") or None, q("how") or "any")
        if path == "/api/mkdir" and verb == "POST":
            return 200, ws.files.mkdir(self.json_body().get("path"))
        if path == "/api/rename" and verb == "POST":
            body = self.json_body()
            return 200, ws.files.rename(body.get("from"), body.get("to"))
        if path == "/api/delete" and verb == "POST":
            return 200, ws.files.delete(self.json_body().get("path"))
        if path == "/api/fs/tail" and verb == "GET":
            return 200, ws.files.tail(q("path") or "", number("offset", "a byte count"), q("id") or None)
        if path == "/api/usage" and verb == "GET":
            return 200, ws.files.usage(q("path") or ws.root, number("limit", "seconds") or 120)
        if path == "/api/raw-url" and verb == "POST":
            return 200, {"url": self.server.raw_url(ws.files.stat(self.json_body().get("path"))["path"])}
        if path == "/api/term/ticket" and verb == "POST":
            return 200, {"ticket": self.server.new_ticket()}
        if path == "/api/terms" and verb == "GET":
            return 200, {"terminals": ws.terminals.listing()}
        if path == "/api/term/close" and verb == "POST":
            return 200, ws.terminals.close(self.json_body().get("id"))
        if path == "/api/templates" and verb == "GET":
            return 200, ws.templates.list()
        if path == "/api/templates/edit" and verb == "POST":
            return 200, ws.templates.edit(self.json_body())
        if path == "/api/render" and verb == "POST":
            return 200, ws.templates.render(self.json_body())
        if path == "/api/submit" and verb == "POST":
            return 200, ws.templates.submit(self.json_body())
        if path == "/api/cluster" and verb == "GET":
            return 200, ws.cluster.get(q("refresh") == "1")
        if path == "/api/modules" and verb == "GET":
            return 200, ws.cluster.modules(q("q"))
        if path == "/api/env" and verb == "GET":
            return 200, ws.env_card(q("path") or ws.root)
        if path == "/api/env/settings" and verb == "GET":
            return 200, ws.envs.dirs(fresh=True)
        if path == "/api/env/prepare" and verb == "POST":
            return 200, ws.env_do("prepare", self.json_body())
        if path == "/api/env/tool" and verb == "POST":
            return 200, ws.env_do("tool", self.json_body())
        if path == "/api/env/dirs" and verb == "POST":
            return 200, ws.env_do("dirs", self.json_body())
        if path == "/api/env/operation" and verb == "GET":
            return 200, ws.env_do("operation", {"id": q("id")})
        if path == "/api/jobs/render" and verb == "POST":
            return 200, ws.jobs.render(self.json_body())
        if path == "/api/jobs/import" and verb == "GET":
            return 200, ws.jobs.import_script(q("path") or "")
        if path == "/api/jobs/adopt" and verb == "POST":
            return 200, ws.jobs.adopt(self.json_body())
        if path == "/api/jobs/interactive" and verb == "POST":
            return 200, ws.jobs.interactive(self.json_body())
        if path == "/api/jobs/check" and verb == "POST":
            return 200, ws.jobs.check(self.json_body())
        if path == "/api/jobs/submit" and verb == "POST":
            return 200, ws.jobs.submit(self.json_body())
        if path == "/api/profiles" and verb == "GET":
            return 200, ws.profiles.list(q("dir") or None)
        if path == "/api/profiles" and verb == "POST":
            body = self.json_body()
            return 200, ws.profiles.save(body.get("name"), body.get("spec"))
        if path == "/api/profiles/delete" and verb == "POST":
            return 200, ws.profiles.delete(self.json_body().get("name"))
        if path == "/api/job" and verb == "GET":
            return 200, ws.jobs.state(q("id") or "")
        if path == "/api/runs" and verb == "GET":
            return 200, ws.jobs.runs(q("dir") or None)
        cancel = re.fullmatch(r"/api/runs/(\d+)/cancel", path)
        if cancel and verb == "POST":
            self.json_body()
            return 200, ws.jobs.cancel(cancel.group(1))
        raise Refused(404, "no route {} {}".format(verb, path))

    def static(self, path):
        name = "index.html" if path == "/" else path[len("/ui/"):]
        full = os.path.normpath(os.path.join(UI_DIR, name))
        if not full.startswith(UI_DIR + os.sep) or not os.path.isfile(full):
            raise Refused(404, "not found")
        with open(full, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return None

    def send_file(self, path, editing):
        """A file, streamed: with Range (a video seeks); for the editor with its mtime_ns, up to the limit."""
        ws = self.server.workspace
        st = ws.files.stat(path)
        if st["type"] != "file":
            raise Refused(400, "{} isn't a file".format(st["path"]))
        size = st["size"]
        if editing and size > ws.edit_limit:
            raise Refused(413, "{} is {:.0f} MB, over the editor's limit ({:.0f} MB)".format(
                st["path"], size / (1 << 20), ws.edit_limit / (1 << 20)))
        start, end, partial = 0, size - 1, False
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if rng and not editing and size:
            a, b = rng.groups()
            if a:
                start = int(a)
                end = min(int(b), size - 1) if b else size - 1
            elif b:
                start = max(0, size - int(b))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */{}".format(size))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return 416, None
            partial = True
        length = max(0, end - start + 1)
        ctype = "application/octet-stream" if editing else (mimetypes.guess_type(st["path"])[0] or "application/octet-stream")
        if ctype.startswith("text/") or ctype in ("image/svg+xml", "application/json", "application/x-sh"):
            ctype += "; charset=utf-8"
        try:
            f = open(st["path"], "rb")
        except OSError as e:
            raise oserror(e, st["path"])
        with f:
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if partial:
                self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
            if editing:
                self.send_header("X-Mtime-Ns", st["mtime_ns"])
                self.send_header("X-Writable", "1" if st["writable"] else "0")
            elif ctype.split(";")[0] in SANDBOXED_TYPES:
                self.send_header("Content-Security-Policy", SANDBOX_CSP)
            self.end_headers()
            if self.command == "HEAD":
                return 200, None
            f.seek(start)
            left = length
            while left > 0:
                data = f.read(min(CHUNK, left))
                if not data:
                    break
                self.wfile.write(data)
                left -= len(data)
        return 200, None

    def terminal(self):
        """Open or re-attach a session, then serve the WebSocket until the page leaves (the session stays)."""
        if (self.headers.get("Upgrade") or "").lower() != "websocket" or not self.headers.get("Sec-WebSocket-Key"):
            raise Refused(400, "the terminal is a WebSocket")
        origin = (self.headers.get("Origin") or "").lower()
        if origin and urllib.parse.urlsplit(origin).netloc != (self.headers.get("Host") or "").lower():
            raise Refused(403, "the terminal opens from the Workspace's own page")
        ws = self.server.workspace
        q = lambda k, d: self.query.get(k, [d])[-1]          # noqa: E731
        if q("session", ""):
            session = ws.terminals.get(q("session", ""))        # 404 before the upgrade: the page then starts a new one
        else:
            try:
                cols, rows = int(q("cols", "100")), int(q("rows", "30"))
            except ValueError:
                cols, rows = 100, 30
            cwd = q("cwd", "") or ws.root
            try:
                cwd = resolve(cwd)
                if not os.path.isdir(cwd):
                    cwd = os.path.expanduser("~")
            except Refused:
                cwd = os.path.expanduser("~")
            session = ws.terminals.create(cwd, cols, rows)
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws_accept(self.headers["Sec-WebSocket-Key"]))
        self.end_headers()
        self.wfile.flush()
        terminal.bridge(session, self.rfile, self.wfile)
        self.close_connection = True
        return None


def main(argv=None):
    p = argparse.ArgumentParser(description="The hpclib Workspace server (run by the workspace tunnel).")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--root", default=os.environ.get("WORKSPACE_ROOT") or None)
    p.add_argument("--cdn", default=os.environ.get("WORKSPACE_CDN") or "https://cdn.jsdelivr.net/npm")
    p.add_argument("--edit-limit-mb", type=float, default=float(os.environ.get("WORKSPACE_EDIT_LIMIT_MB") or DEFAULT_EDIT_MB))
    p.add_argument("--confirm-mb", type=float, default=float(os.environ.get("WORKSPACE_CONFIRM_MB") or 20))
    p.add_argument("--token", default=os.environ.get("WORKSPACE_TOKEN") or None, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if sys.version_info < (3, 6):
        print("workspace: needs Python 3.6 or newer", file=sys.stderr)
        return 2
    token = args.token or secrets.token_urlsafe(24)
    workspace = Workspace(args.root, args.cdn, args.edit_limit_mb, args.confirm_mb)
    server = Server(("127.0.0.1", args.port), token, workspace)

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    # the console's Open finds the token here (the app's token_regex)
    print("hpclib workspace {} on {}: http://127.0.0.1:{}/?token={}".format(
        VERSION, socket.gethostname(), args.port, token), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    workspace.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
