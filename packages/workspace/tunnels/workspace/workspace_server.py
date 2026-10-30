#!/usr/bin/env python3
"""
The Workspace: your files on the cluster in the browser, an editor, viewers, terminals, and job scripts built from
templates and submitted with sbatch. It runs on the login node as you (the workspace tunnel's sbatch_script.sh starts
it), listens on 127.0.0.1 only, and answers only with the token it prints at start (the console's Open adds it):
a login node is shared, and anyone on it could reach the port.

The token is a credential for the API, so it travels only where page JavaScript can set a header: every /api/ call
sends it as X-Workspace-Token, and nothing accepts it in a URL. The two things a header can't carry get their own,
short-lived credentials: the terminal's WebSocket opens with a single-use ticket (POST /api/term/ticket), and the
viewer's files (<img>, <iframe>, <video>) come from signed URLs that expire and cover one directory
(POST /api/raw-url). Requests that change something must be JSON (or, for PUT /api/file, a body with the token
header), and a request that names an Origin other than the page's own is refused.

  workspace_server.py --port PORT [--root DIR] [--cdn URL] [--edit-limit-mb N] [--confirm-mb N]

The page (ui/) loads CodeMirror (the editor) and xterm.js (the terminal) from --cdn, an npm mirror (jsDelivr's by
default). Routes, all but /health and the page's own files needing the token (X-Workspace-Token):

  GET  /health                       {"ok", "version"}
  GET  /api/info                     home, user, host, hpclib.sh's path, places (home, scratch), limits, the CDN
  GET  /api/list?path=[&hidden=1]    a directory: {path, parent, entries: [{name, type, size, mtime, mode, target?}]}
  GET  /api/stat?path=
  GET  /api/file?path=               the file's bytes for the editor (X-Mtime-Ns), up to the editor's limit
  PUT  /api/file?path=[&mtime_ns=][&how=create|replace|any]   the body to the file through a temporary file and a
                                     rename, keeping its mode; refused when it changed since mtime_ns (409)
  POST /api/mkdir {path}  /api/rename {from, to}  /api/delete {path}   (delete: a file, link or empty directory)
  GET  /api/templates                the script templates (the Workspace's and yours), parsed, with the values last used
  POST /api/templates/edit {name, from?}   your copy to edit, in $HPCTUNNELS_DATA_DIR/script_templates
  POST /api/render {template, text?, input, values}   -> {script, name, dir, values, fields}
  POST /api/submit {script, name, dir, overwrite?, template?, values?}   write the job script, sbatch it -> {job_id}
  GET  /api/usage?path=[&limit=]     a directory's folders by size (du -k) and file count (du --inodes, else a walk),
                                     at low priority, up to limit seconds (120; at most 600; then partial)
  GET  /api/jobs                     your queued and running jobs (squeue): id, name, state, time, limit, directory
  POST /api/cancel {job}             scancel one of them
  POST /api/raw-url {path}           {url}: a signed URL for the file (below), good for an hour, for the files in
                                     its directory
  GET  /raw/EXPIRES.DEPTH.SIGNATURE/PATH   a file for an <img>, <iframe> or <video>, with Range; HTML and SVG with a
                                     sandboxing Content-Security-Policy (an opaque origin: no access to the page).
                                     DEPTH is how many leading parts of PATH the signature covers: a page's own
                                     images and scripts load, other directories don't
  POST /api/term/ticket              {ticket}: opens one terminal, within ten seconds
  GET  /terminal?ticket=&cwd=&cols=&rows=   WebSocket: your login shell in a pseudo-terminal (binary frames both
                                     ways; text frames JSON: {"type": "resize", "cols", "rows"} in, {"type": "exit"} out)

Python 3.6+, standard library only.
"""
import argparse
import base64
import hashlib
import http.server
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

from token_auth import ServiceToken, CapabilityTickets, ResourceSigner

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import script_templates  # noqa: E402

VERSION = "1.0.2"
UI_DIR = os.path.join(HERE, "ui")
MAX_ENTRIES = 5000
MAX_JSON = 1 << 20
MAX_TEMPLATE = 256 * 1024
CHUNK = 1 << 16
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# a page the viewer shows is somebody's file, not the Workspace's: it may run, open links and offer downloads,
# and nothing more (no forms, no dialogs), in an origin of its own
SANDBOX_CSP = "sandbox allow-scripts allow-popups allow-downloads"
RAW_URL_SECONDS = 3600       # how long a viewer's file URL works (a movie keeps requesting ranges while it plays)
TICKET_SECONDS = 10          # how long a terminal's ticket works: the page uses it at once
SANDBOXED_TYPES = ("text/html", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/xml")
JOB_RE = re.compile(r"Submitted batch job (\d+)")
TEMPLATE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
SCRIPT_NAME_RE = re.compile(r"[A-Za-z0-9_.+@-][A-Za-z0-9_.+@ -]{0,200}")
for _type, _ext in (("text/markdown", ".md"), ("text/x-python", ".py"), ("application/x-sh", ".sh"),
                    ("video/mp4", ".m4v"), ("video/quicktime", ".mov"), ("image/webp", ".webp")):
    mimetypes.add_type(_type, _ext)


class Refused(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status


def data_dir():
    return os.path.expanduser(os.environ.get("HPCTUNNELS_DATA_DIR") or "~/.local/tunnels")


def templates_dir():
    return os.path.join(data_dir(), "script_templates")


def resolve(path):
    if not path or not isinstance(path, str):
        raise Refused(400, "no path")
    if "\0" in path or len(path) > 4096:
        raise Refused(400, "that isn't a path")
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(os.path.expanduser("~"), path)
    return os.path.normpath(path)


def kind(st):
    if stat.S_ISDIR(st.st_mode):
        return "directory"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "other"


def oserror(e, what):
    if isinstance(e, FileNotFoundError):
        return Refused(404, "{} doesn't exist".format(what))
    if isinstance(e, PermissionError):
        return Refused(403, "{}: permission denied".format(what))
    if isinstance(e, NotADirectoryError):
        return Refused(400, "{} isn't a directory".format(what))
    if isinstance(e, IsADirectoryError):
        return Refused(400, "{} is a directory".format(what))
    if getattr(e, "errno", None) in (39, 66):
        return Refused(409, "{} isn't empty".format(what))
    return Refused(500, "{}: {}".format(what, e.strerror or e))


def entry(path):
    lst = os.lstat(path)
    out = {"name": os.path.basename(path) or path, "size": lst.st_size, "mtime": lst.st_mtime,
           "mode": stat.filemode(lst.st_mode)}
    if stat.S_ISLNK(lst.st_mode):
        out["type"] = "link"
        try:
            out["target"] = os.readlink(path)
            st = os.stat(path)
            out["target_type"] = kind(st)
            out["size"] = st.st_size
        except OSError:
            out["target_type"] = "missing"
    else:
        out["type"] = kind(lst)
    return out


def _umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def write_atomic(path, stream, length, mode=None):
    """`length` bytes of `stream` to path through a temporary file beside it and a rename."""
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".workspace-tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            left = length
            while left > 0:
                data = stream.read(min(CHUNK, left))
                if not data:
                    raise Refused(400, "the upload ended early")
                f.write(data)
                left -= len(data)
        os.chmod(tmp, mode if mode is not None else (0o666 & ~_umask()))
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def login_shell_run(command, cwd=None, timeout=60):
    """A command in a login shell (bash -l), where sites put sbatch and squeue on the PATH: (code, output)."""
    try:
        res = subprocess.run(["bash", "-lc", command], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Refused(504, "it took over {} s".format(timeout))
    return res.returncode, res.stdout or ""


# -- the workspace's operations ------------------------------------------------------------------------------------

class Workspace:
    def __init__(self, root=None, cdn="https://cdn.jsdelivr.net/npm", edit_limit_mb=800, confirm_mb=20):
        self.root = resolve(root) if root else os.path.expanduser("~")
        self.cdn = cdn.rstrip("/")
        self.edit_limit = int(edit_limit_mb * (1 << 20))
        self.confirm = int(confirm_mb * (1 << 20))

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

    def list(self, path, hidden=False):
        path = resolve(path)
        try:
            names = os.listdir(path)
        except OSError as e:
            raise oserror(e, path)
        entries, truncated = [], False
        for name in sorted(names, key=lambda n: n.lower()):
            if name.startswith(".") and not hidden:
                continue
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                break
            try:
                entries.append(entry(os.path.join(path, name)))
            except OSError:
                continue
        return {"path": path, "parent": os.path.dirname(path) if path != "/" else None, "entries": entries,
                "truncated": truncated, "hidden": sum(1 for n in names if n.startswith(".")),
                "writable": os.access(path, os.W_OK)}

    def stat(self, path):
        path = resolve(path)
        try:
            st = os.stat(path)
        except OSError as e:
            raise oserror(e, path)
        return {"path": path, "type": kind(st), "size": st.st_size, "mtime": st.st_mtime,
                "mtime_ns": str(st.st_mtime_ns), "writable": os.access(path, os.W_OK)}

    def write(self, path, stream, length, mtime_ns=None, how="any"):
        path = resolve(path)
        if how not in ("create", "replace", "any"):
            raise Refused(400, "how: create, replace or any")
        try:
            st = os.stat(path)
        except FileNotFoundError:
            st = None
        except OSError as e:
            raise oserror(e, path)
        if st is not None and not stat.S_ISREG(st.st_mode):
            raise Refused(400, "{} isn't a regular file".format(path))
        if how == "create" and st is not None:
            raise Refused(409, "{} already exists".format(path))
        if how == "replace" and st is None:
            raise Refused(404, "{} doesn't exist (any more)".format(path))
        if mtime_ns and st is not None and str(st.st_mtime_ns) != mtime_ns:
            raise Refused(409, "{} changed since it was opened".format(path))
        if length > max(self.edit_limit, 1 << 30):
            raise Refused(413, "that's larger than the Workspace takes")
        target = os.path.realpath(path) if os.path.islink(path) else path    # keep a link, write its file
        try:
            write_atomic(target, stream, length, stat.S_IMODE(st.st_mode) if st is not None else None)
        except OSError as e:
            raise oserror(e, path)
        return self.stat(path)

    def mkdir(self, path):
        path = resolve(path)
        if os.path.lexists(path):
            raise Refused(409, "{} already exists".format(path))
        try:
            os.makedirs(path)
        except OSError as e:
            raise oserror(e, path)
        return self.stat(path)

    def rename(self, src, dst):
        src, dst = resolve(src), resolve(dst)
        if os.path.lexists(dst):
            raise Refused(409, "{} already exists".format(dst))
        if not os.path.lexists(src):
            raise Refused(404, "{} doesn't exist".format(src))
        try:
            os.rename(src, dst)
        except OSError as e:
            raise oserror(e, src)
        return {"from": src, "to": dst}

    def delete(self, path):
        path = resolve(path)
        if path in ("/", os.path.expanduser("~"), self.root):
            raise Refused(400, "not that one")
        try:
            if stat.S_ISDIR(os.lstat(path).st_mode):
                os.rmdir(path)
            else:
                os.unlink(path)
        except OSError as e:
            raise oserror(e, path)
        return {"deleted": path}

    # -- script templates ----------------------------------------------------------------------------------------

    def _yours(self):
        d, out = templates_dir(), {}
        try:
            names = sorted(os.listdir(d))
        except OSError:
            names = []
        for name in names:
            if not name.endswith(".sh") or not TEMPLATE_NAME_RE.fullmatch(name[:-3]):
                continue
            p = os.path.join(d, name)
            try:
                if os.path.getsize(p) > MAX_TEMPLATE:
                    continue
                with open(p, encoding="utf-8", errors="replace") as f:
                    out[name[:-3]] = (p, f.read())
            except OSError:
                continue
        return out

    def _remembered_path(self):
        return os.path.join(data_dir(), "workspace", "remembered.json")

    def remembered(self):
        try:
            with open(self._remembered_path()) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def remember(self, template, values, fields):
        data = self.remembered()
        data.setdefault("templates", {})[template] = {k: v for k, v in values.items()
                                                      if k in fields and k not in ("job_name", "args", "project")}
        data["last_template"] = template
        path = self._remembered_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(data, f, indent=2)
        os.replace(path + ".tmp", path)

    def templates(self):
        found = {}
        for name, text in script_templates.builtins().items():
            found[name] = {"text": text, "source": "workspace", "path": os.path.join(script_templates.BUILTIN_DIR, name + ".sh")}
        for name, (path, text) in self._yours().items():
            found[name] = {"text": text, "source": "yours", "path": path, "replaces": name in found}
        out = [dict(script_templates.parse(t["text"], name), **t) for name, t in sorted(found.items())]
        remembered = self.remembered()
        return {"dir": templates_dir(), "templates": out, "remembered": remembered.get("templates") or {},
                "last": remembered.get("last_template")}

    def template_text(self, name, text=None):
        if text is not None:
            if not isinstance(text, str) or len(text) > MAX_TEMPLATE:
                raise Refused(400, "text: a template, at most 256 KiB")
            return text
        for t in self.templates()["templates"]:
            if t["name"] == name:
                return t["text"]
        raise Refused(404, "no script template {!r}".format(name))

    def render(self, body):
        name, given = body.get("template"), body.get("values") or {}
        if not isinstance(given, dict) or not all(isinstance(v, str) for v in given.values()):
            raise Refused(400, "values: {field: text}")
        inp = body.get("input")
        if not isinstance(inp, str) or not inp.startswith("/"):
            raise Refused(400, "input: the file's absolute path")
        text = self.template_text(name, body.get("text"))
        context = script_templates.context_for(inp, self.info())
        try:
            script, values = script_templates.render(text, given, context, name or "")
        except script_templates.TemplateError as e:
            raise Refused(400, "the template: {}".format(e))
        parsed = script_templates.parse(text, name or "")
        return {"script": script, "name": script_templates.script_name(parsed, values), "values": values,
                "dir": context["dir"], "fields": parsed["fields"]}

    def submit(self, body):
        script, name, directory = body.get("script"), body.get("name"), body.get("dir")
        if not isinstance(script, str) or not script.strip() or len(script) > MAX_JSON:
            raise Refused(400, "script: the job script")
        if not isinstance(name, str) or not SCRIPT_NAME_RE.fullmatch(name) or name in (".", ".."):
            raise Refused(400, "name: the job script's file name (letters, digits, spaces and _ . + @ -)")
        directory = resolve(directory)
        if not os.path.isdir(directory):
            raise Refused(404, "{} isn't a directory".format(directory))
        path = os.path.join(directory, name)
        if os.path.lexists(path) and not body.get("overwrite"):
            raise Refused(409, "{} already exists".format(path))
        data = script.encode()
        import io
        try:
            write_atomic(path, io.BytesIO(data), len(data), 0o777 & ~_umask() & 0o755)
        except OSError as e:
            raise oserror(e, path)
        code, out = login_shell_run("sbatch " + shlex.quote(name), cwd=directory, timeout=120)
        m = JOB_RE.search(out)
        if code != 0 or not m:
            raise Refused(400, "sbatch: " + (out.strip()[-1500:] or "exit {}".format(code)))
        template, values = body.get("template"), body.get("values")
        if isinstance(template, str) and isinstance(values, dict):
            try:
                fields = {f["name"] for f in script_templates.parse(self.template_text(template))["fields"]}
                self.remember(template, {k: v for k, v in values.items() if isinstance(v, str)}, fields)
            except (Refused, OSError):
                pass
        return {"path": path, "job_id": m.group(1), "output": out.strip()[-1500:]}

    def edit_template(self, body):
        name = body.get("name")
        source = body.get("from") or name
        if not isinstance(name, str) or not TEMPLATE_NAME_RE.fullmatch(name):
            raise Refused(400, "name: letters, digits, _ . -")
        mine = self._yours()
        if name in mine:
            return {"path": mine[name][0], "created": False}
        text = self.template_text(source)
        os.makedirs(templates_dir(), exist_ok=True)
        path = os.path.join(templates_dir(), name + ".sh")
        with open(path, "x") as f:
            f.write(text)
        return {"path": path, "created": True}

    # -- disk usage ------------------------------------------------------------------------------------------------

    USAGE_LIMIT = 600

    @staticmethod
    def _du(args, path, limit):
        """{path: number} from du --max-depth=1 (and whether it finished), at low priority with a time limit."""
        try:
            res = subprocess.run(["timeout", str(limit), "nice", "-n", "19", "du", "--max-depth=1"] + args + ["--", path],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                 universal_newlines=True, timeout=limit + 30)
        except (OSError, subprocess.TimeoutExpired):
            return {}, False, ""
        out = {}
        for line in res.stdout.splitlines():
            value, _, p = line.partition("\t")
            if value.isdigit():
                out[p] = int(value)
        return out, res.returncode == 0, res.stderr

    @staticmethod
    def _count(path, deadline):
        """{path: entries} for path and each folder in it by walking (du too old for --inodes): files, folders and
        links, each folder counting itself, as du --inodes does."""
        out, done = {}, True

        def walk(top):
            n = 1
            for base, dirs, files in os.walk(top, onerror=lambda e: None):
                n += len(dirs) + len(files)
                if time.time() > deadline:
                    return n, False
            return n, True
        total = 1
        try:
            names = os.listdir(path)
        except OSError as e:
            raise oserror(e, path)
        for name in names:
            p = os.path.join(path, name)
            if os.path.isdir(p) and not os.path.islink(p):
                n, ok = walk(p)
                out[p] = n
                total += n
                done = done and ok
            else:
                total += 1
            if not done:
                break
        out[path] = total
        return out, done

    def usage(self, path, limit=120):
        """A directory's folders by size (du -k) and by file count (du --inodes, else a walk): the Storage tab's
        scan, for any directory. {path, kb, files, partial, children: [{name, path, kb, files}], here: {...}}."""
        path = resolve(path)
        if not os.path.isdir(path):
            raise Refused(404, "{} isn't a directory".format(path))
        limit = max(5, min(int(limit), self.USAGE_LIMIT))
        start = time.time()
        sizes, sized, _ = self._du(["-k"], path, limit)
        counts, counted, err = self._du(["--inodes"], path, max(5, int(limit - (time.time() - start))))
        method = "du --inodes"
        if not counts and ("inodes" in err or "unrecognized" in err or "invalid option" in err):
            counts, counted = self._count(path, time.time() + max(5, limit - (time.time() - start)))
            method = "walk"
        children = []
        for p in sorted(set(sizes) | set(counts)):
            if os.path.normpath(p) == path:
                continue
            children.append({"name": os.path.basename(p), "path": os.path.normpath(p), "kb": sizes.get(p),
                             "files": counts.get(p)})
        kb, files = sizes.get(path), counts.get(path)
        here = None
        if kb is not None and files is not None:
            # what is directly in the directory: the total less its folders (and the folder itself, for files)
            here = {"kb": max(0, kb - sum(c["kb"] or 0 for c in children)),
                    "files": max(0, files - 1 - sum(c["files"] or 0 for c in children))}
        return {"path": path, "kb": kb, "files": files, "partial": not (sized and counted), "children": children,
                "here": here, "count_method": method, "seconds": round(time.time() - start, 1), "limit": limit}

    # -- jobs ------------------------------------------------------------------------------------------------------

    def jobs(self):
        code, out = login_shell_run("squeue -h -u \"$USER\" -o '%i|%j|%T|%M|%l|%R|%Z'", timeout=60)
        if code != 0:
            raise Refused(502, "squeue: " + (out.strip()[-500:] or "exit {}".format(code)))
        jobs = []
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) == 7 and parts[0].strip():
                jobs.append(dict(zip(("id", "name", "state", "time", "limit", "reason", "dir"),
                                     [p.strip() for p in parts])))
        return {"jobs": jobs, "at": time.time()}

    def cancel(self, job):
        if not isinstance(job, str) or not re.fullmatch(r"\d+(_\d+)?", job):
            raise Refused(400, "job: a job id")
        code, out = login_shell_run("scancel " + job, timeout=60)
        if code != 0:
            raise Refused(400, "scancel: " + (out.strip()[-500:] or "exit {}".format(code)))
        return {"cancelled": job}


# -- the terminal ----------------------------------------------------------------------------------------------------

class Terminal:
    """Your login shell in a pseudo-terminal of its own (a session whose controlling terminal it is)."""

    def __init__(self, cwd, cols, rows):
        import fcntl
        import pty
        import termios
        master, slave = pty.openpty()
        self._resize(slave, cols, rows)
        shell = os.environ.get("SHELL") or "/bin/bash"

        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
        for k in ("WORKSPACE_TOKEN",):
            env.pop(k, None)
        try:
            self.proc = subprocess.Popen([shell, "-l"], stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                                         close_fds=True, preexec_fn=controlling_terminal)
        finally:
            os.close(slave)
        self.master = master

    @staticmethod
    def _resize(fd, cols, rows):
        import fcntl
        import termios
        cols = max(10, min(int(cols), 1000))
        rows = max(2, min(int(rows), 500))
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def resize(self, cols, rows):
        try:
            self._resize(self.master, cols, rows)
        except (OSError, ValueError, TypeError):
            pass

    def close(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGHUP)
            except OSError:
                pass
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            os.close(self.master)
        except OSError:
            pass


def ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_send(wfile, opcode, payload, lock):
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 1 << 16:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    with lock:
        wfile.write(head + payload)
        wfile.flush()


def _read_exact(rfile, n):
    data = b""
    while len(data) < n:
        chunk = rfile.read(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def ws_read(rfile, limit=1 << 20):
    """(opcode, payload) of the next whole message."""
    opcode, parts = None, []
    while True:
        b0, b1 = _read_exact(rfile, 2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _read_exact(rfile, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _read_exact(rfile, 8))[0]
        if n > limit:
            raise EOFError
        mask = _read_exact(rfile, 4) if b1 & 0x80 else None
        data = _read_exact(rfile, n)
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        op = b0 & 0x0F
        if op >= 0x8:
            return op, data
        if op:
            opcode = op
        parts.append(data)
        if b0 & 0x80:
            return opcode, b"".join(parts)


def bridge(term, rfile, wfile):
    lock = threading.Lock()

    def out():
        while True:
            try:
                data = os.read(term.master, 65536)
            except OSError:
                break
            if not data:
                break
            try:
                ws_send(wfile, 0x2, data, lock)
            except OSError:
                return
        code = term.proc.wait()
        try:
            ws_send(wfile, 0x1, json.dumps({"type": "exit", "code": code}).encode(), lock)
            ws_send(wfile, 0x8, struct.pack(">H", 1000), lock)
        except OSError:
            pass
    reader = threading.Thread(target=out, daemon=True)
    reader.start()
    try:
        while True:
            op, data = ws_read(rfile)
            if op == 0x8:
                break
            if op == 0x9:
                ws_send(wfile, 0xA, data, lock)
            elif op == 0x2:
                os.write(term.master, data)
            elif op == 0x1:
                try:
                    msg = json.loads(data.decode())
                except ValueError:
                    continue
                if msg.get("type") == "resize":
                    term.resize(msg.get("cols", 100), msg.get("rows", 30))
    except (EOFError, OSError):
        pass
    finally:
        term.close()
        reader.join(2)


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
        if path == "/api/info" and verb == "GET":
            return 200, ws.info()
        if path == "/api/list" and verb == "GET":
            return 200, ws.list(q("path") or ws.root, q("hidden") == "1")
        if path == "/api/stat" and verb == "GET":
            return 200, ws.stat(q("path") or ws.root)
        if path == "/api/file" and verb in ("GET", "HEAD"):
            return self.send_file(q("path") or "", editing=True)
        if path == "/api/file" and verb == "PUT":
            length = int(self.headers.get("Content-Length") or 0)
            return 200, ws.write(q("path") or "", self.rfile, length, q("mtime_ns") or None, q("how") or "any")
        if path == "/api/mkdir" and verb == "POST":
            return 200, ws.mkdir(self.json_body().get("path"))
        if path == "/api/rename" and verb == "POST":
            body = self.json_body()
            return 200, ws.rename(body.get("from"), body.get("to"))
        if path == "/api/delete" and verb == "POST":
            return 200, ws.delete(self.json_body().get("path"))
        if path == "/api/raw-url" and verb == "POST":
            return 200, {"url": self.server.raw_url(ws.stat(self.json_body().get("path"))["path"])}
        if path == "/api/term/ticket" and verb == "POST":
            return 200, {"ticket": self.server.new_ticket()}
        if path == "/api/templates" and verb == "GET":
            return 200, ws.templates()
        if path == "/api/templates/edit" and verb == "POST":
            return 200, ws.edit_template(self.json_body())
        if path == "/api/render" and verb == "POST":
            return 200, ws.render(self.json_body())
        if path == "/api/submit" and verb == "POST":
            return 200, ws.submit(self.json_body())
        if path == "/api/usage" and verb == "GET":
            try:
                limit = int(q("limit") or 120)
            except ValueError:
                raise Refused(400, "limit: seconds")
            return 200, ws.usage(q("path") or ws.root, limit)
        if path == "/api/jobs" and verb == "GET":
            return 200, ws.jobs()
        if path == "/api/cancel" and verb == "POST":
            return 200, ws.cancel(self.json_body().get("job"))
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
        st = ws.stat(path)
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
        if (self.headers.get("Upgrade") or "").lower() != "websocket" or not self.headers.get("Sec-WebSocket-Key"):
            raise Refused(400, "the terminal is a WebSocket")
        origin = (self.headers.get("Origin") or "").lower()
        if origin and urllib.parse.urlsplit(origin).netloc != (self.headers.get("Host") or "").lower():
            raise Refused(403, "the terminal opens from the Workspace's own page")
        q = lambda k, d: self.query.get(k, [d])[-1]          # noqa: E731
        try:
            cols, rows = int(q("cols", "100")), int(q("rows", "30"))
        except ValueError:
            cols, rows = 100, 30
        cwd = q("cwd", "") or self.server.workspace.root
        try:
            cwd = resolve(cwd)
            if not os.path.isdir(cwd):
                cwd = os.path.expanduser("~")
        except Refused:
            cwd = os.path.expanduser("~")
        term = Terminal(cwd, cols, rows)
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws_accept(self.headers["Sec-WebSocket-Key"]))
        self.end_headers()
        self.wfile.flush()
        bridge(term, self.rfile, self.wfile)
        self.close_connection = True
        return None


def main(argv=None):
    p = argparse.ArgumentParser(description="The hpclib Workspace server (run by the workspace tunnel).")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--root", default=os.environ.get("WORKSPACE_ROOT") or None)
    p.add_argument("--cdn", default=os.environ.get("WORKSPACE_CDN") or "https://cdn.jsdelivr.net/npm")
    p.add_argument("--edit-limit-mb", type=float, default=float(os.environ.get("WORKSPACE_EDIT_LIMIT_MB") or 800))
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
