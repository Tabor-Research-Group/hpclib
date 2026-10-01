#!/usr/bin/env python3
"""
A small, dependency-free REST server for driving SLURM and moving
files on an HPC system from the far side of an hpclib tunnel.

Only the standard library is used, so this runs on any node with
Python >= 3.10 - no conda environment or `pip install` required.

Protections
  - Every request (including /health) must carry
    `Authorization: Bearer <token>`. The token comes from, in order,
    `$HPC_REST_TOKEN`, `--token-file`/`$HPC_REST_TOKEN_FILE`, or
    `$HPCTUNNELS_DATA_DIR/rest_token` (default ~/.local/tunnels/rest_token).
    A missing token file is created with a fresh random token and
    mode 600; an existing one must be owned by this user and not
    readable by group/other or the server refuses to start.
  - Binds 127.0.0.1 by default, so it is only reachable through the
    tunnel's port forward (and by other users on the same node, which
    is what the token is for).
  - File endpoints can be restricted to a whitelist of directories (and
    their children) with `--allow DIR` / `$HPC_REST_ALLOWED_DIRS`
    (colon-separated). With no whitelist, file access is unrestricted
    (i.e. limited only by this user's own permissions). Symlinks and
    `..` are resolved before the check, so neither can escape a root.
    The whitelist governs the file endpoints and the `cwd` of SLURM
    commands; it does NOT sandbox the jobs those commands submit.
  - SLURM commands are run as argument lists (never through a shell)
    with a timeout.
  - `--disable-file-changes` (or `$HPC_REST_DISABLE_FILE_CHANGES=1`)
    turns off the routes that create, overwrite, or delete files
    (upload, mkdir, delete); they answer 403. Reading files and all
    SLURM routes stay available - note a job submitted through sbatch
    can still write files when it runs.

Endpoints (all responses are JSON unless noted)
  GET    /health
  POST   /slurm/{sbatch,squeue,sacct,scontrol,scancel}
           body: {"args": [...], "cwd": "...", "input": "..."}
  GET    /slurm/{squeue,sacct}?arg=...&arg=...&cwd=...
  GET    /files?path=P                       list a directory / stat a file
  GET    /files/content?path=P               download (raw bytes)
  PUT    /files/content?path=P[&overwrite=1][&parents=1]
                                             upload (raw request body)
  POST   /files/mkdir?path=P[&parents=1]
  DELETE /files?path=P                       file, symlink, or empty dir

Usage: rest_server.py [--host H] [--port P] [--allow DIR ...]
                      [--token-file F] [--command-timeout S] [--max-upload N]
                      [--disable-file-changes]
"""
import abc
import argparse
import hmac
import http.server
import json
import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse

__all__ = [
    "RESTError",
    "PathWhitelist",
    "TokenAuth",
    "RESTServer",
    "RESTHandler",
    "HPCRESTHandler",
    "setup_parent_terminated_listener",
]


# Stdlib-only version of node_comm's listener (node_comm needs psutil)
def listen_for_proc(w_pid, polling_time=5):
    while True:
        try:
            os.kill(w_pid, 0)
        except ProcessLookupError:
            os._exit(1)
        except PermissionError:
            pass  # exists, just not ours
        time.sleep(polling_time)

def setup_parent_terminated_listener(PARENT_PID):
    thread = threading.Thread(target=listen_for_proc, args=(int(PARENT_PID),), daemon=True)
    thread.start()
    return thread


class RESTError(Exception):
    """Raised by a route to send a JSON error response with `status`."""
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.payload = dict({"error": message}, **extra)


class PathWhitelist:
    """
    A set of allowed root directories. A path is allowed if it, after
    resolving `~`, `..`, and symlinks, lies inside one of the roots.
    `roots=None` (or empty) means no restrictions.
    Relative paths are taken relative to `base_dir` (the server's
    launch directory by default).
    """
    def __init__(self, roots=None, base_dir=None):
        if roots is not None:
            roots = [r for r in roots if r]
        if not roots:
            self.roots = None
        else:
            self.roots = tuple(os.path.realpath(os.path.expanduser(r)) for r in roots)
        if base_dir is None:
            base_dir = os.getcwd()
        self.base_dir = os.path.realpath(base_dir)

    @property
    def restricted(self):
        return self.roots is not None

    def contains(self, real_path):
        if self.roots is None:
            return True
        return any(os.path.commonpath([root, real_path]) == root for root in self.roots)

    def is_root(self, real_path):
        return self.roots is not None and real_path in self.roots

    def resolve(self, path):
        """
        Returns `(logical, real)`: `logical` has its parent directory
        resolved but keeps the final component (so a symlink can be
        deleted rather than its target); `real` is fully resolved.
        Raises `PermissionError` if either lies outside the whitelist.
        """
        path = os.path.join(self.base_dir, os.path.expanduser(path))
        path = os.path.normpath(path)
        parent, name = os.path.split(path)
        logical = os.path.join(os.path.realpath(parent), name) if name else os.path.realpath(path)
        real = os.path.realpath(path)
        if not (self.contains(logical) and self.contains(real)):
            raise PermissionError(f"{path} is outside the allowed directories")
        return logical, real


class TokenAuth:
    """Bearer-token checking plus loading/creating the token itself."""

    TOKEN_ENV_VAR = 'HPC_REST_TOKEN'
    TOKEN_FILE_ENV_VAR = 'HPC_REST_TOKEN_FILE'
    TOKEN_FILE_NAME = 'rest_token'
    TOKEN_BYTES = 32

    def __init__(self, token, source=None):
        if not token:
            raise ValueError("an empty token would allow anyone in")
        self.token = token
        self.source = source

    def check(self, header) -> bool:
        if not header:
            return False
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(value.strip().encode(), self.token.encode())

    @classmethod
    def default_token_file(cls):
        data_dir = os.environ.get("HPCTUNNELS_DATA_DIR", os.path.expanduser("~/.local/tunnels"))
        return os.path.join(data_dir, cls.TOKEN_FILE_NAME)

    @classmethod
    def read_token_file(cls, token_file):
        info = os.stat(token_file)
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise PermissionError(f"token file {token_file} is not owned by the current user")
        if info.st_mode & 0o077:
            raise PermissionError(
                f"token file {token_file} is accessible by other users; run `chmod 600 {token_file}`"
            )
        with open(token_file) as f:
            token = f.read().strip()
        if not token:
            raise ValueError(f"token file {token_file} is empty")
        return token

    @classmethod
    def create_token_file(cls, token_file):
        token = secrets.token_urlsafe(cls.TOKEN_BYTES)
        os.makedirs(os.path.dirname(os.path.abspath(token_file)), mode=0o700, exist_ok=True)
        # O_EXCL so two servers starting at once can't clobber each other
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        return token

    @classmethod
    def load(cls, token_file=None):
        token = os.environ.get(cls.TOKEN_ENV_VAR)
        if token:
            return cls(token, source=f"${cls.TOKEN_ENV_VAR}")
        if token_file is None:
            token_file = os.environ.get(cls.TOKEN_FILE_ENV_VAR) or cls.default_token_file()
        token_file = os.path.expanduser(token_file)
        try:
            token = cls.create_token_file(token_file)
        except FileExistsError:
            token = cls.read_token_file(token_file)
        return cls(token, source=token_file)


class RESTServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_class, auth: TokenAuth, whitelist: PathWhitelist,
                 command_timeout=120, max_upload=1 << 30, disable_file_changes=False):
        self.auth = auth
        self.whitelist = whitelist
        self.command_timeout = command_timeout
        self.max_upload = max_upload
        self.disable_file_changes = disable_file_changes
        umask = os.umask(0)
        os.umask(umask)
        self.file_mode = 0o666 & ~umask
        super().__init__(address, handler_class)


class RESTHandler(http.server.BaseHTTPRequestHandler):
    """
    Minimal JSON router. Subclasses supply `get_routes`, a dict mapping
    `(verb, path)` to a zero-argument method returning `(status, payload)`,
    or `None` if the method already wrote its own response.
    Routes listed in `FILE_CHANGE_ROUTES` are refused when the server
    was started with `disable_file_changes`.
    """

    server_version = "hpclib-rest/0.1"
    MAX_JSON_BODY = 1 << 20
    FILE_CHANGE_ROUTES = ()
    server: RESTServer

    @abc.abstractmethod
    def get_routes(self) -> 'dict[tuple[str,str],method]':
        ...

    def do_GET(self): self.handle_rest_request("GET")
    def do_POST(self): self.handle_rest_request("POST")
    def do_PUT(self): self.handle_rest_request("PUT")
    def do_DELETE(self): self.handle_rest_request("DELETE")

    def handle_rest_request(self, verb):
        self.responded = False
        try:
            url = urllib.parse.urlsplit(self.path)
            self.query = urllib.parse.parse_qs(url.query)
            route = url.path.rstrip("/") or "/"
            # auth before routing so unauthenticated clients can't probe routes
            if not self.server.auth.check(self.headers.get("Authorization")):
                raise RESTError(401, "missing or invalid bearer token")
            routes = self.get_routes()
            caller = routes.get((verb, route))
            if caller is None:
                allowed = sorted(v for v, p in routes if p == route)
                if allowed:
                    raise RESTError(405, f"{verb} not supported on {route}", allowed=allowed)
                raise RESTError(404, f"unknown route {route}")
            if self.server.disable_file_changes and (verb, route) in self.FILE_CHANGE_ROUTES:
                raise RESTError(403, f"{verb} {route} is disabled: server started with --disable-file-changes")
            response = caller()
            if response is not None:
                self.send_json(*response)
        except RESTError as e:
            self.send_error_json(e.status, e.payload)
        except PermissionError as e:
            self.send_error_json(403, {"error": str(e)})
        except FileNotFoundError as e:
            self.send_error_json(404, {"error": str(e)})
        except Exception:
            traceback.print_exc(limit=3)
            self.send_error_json(500, {"error": traceback.format_exc(limit=1)})

    def send_json(self, status, payload):
        body = json.dumps(payload).encode() + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.responded = True

    def send_error_json(self, status, payload):
        if self.responded:  # mid-stream failure; nothing sane left to send
            self.close_connection = True
            return
        try:
            self.send_json(status, payload)
        except OSError:
            pass  # client went away

    # request helpers
    def content_length(self, required=False):
        length = self.headers.get("Content-Length")
        if length is None:
            if required:
                raise RESTError(411, "Content-Length is required")
            return 0
        try:
            length = int(length)
        except ValueError:
            raise RESTError(400, f"bad Content-Length {length!r}")
        if length < 0:
            raise RESTError(400, f"bad Content-Length {length!r}")
        return length

    def read_json(self):
        length = self.content_length()
        if length == 0:
            return {}
        if length > self.MAX_JSON_BODY:
            raise RESTError(413, f"JSON body larger than {self.MAX_JSON_BODY} bytes")
        try:
            body = json.loads(self.rfile.read(length).decode())
        except ValueError as e:
            raise RESTError(400, f"invalid JSON body: {e}")
        if not isinstance(body, dict):
            raise RESTError(400, "JSON body must be an object")
        return body

    def query_value(self, name, default=None, required=False):
        values = self.query.get(name)
        if not values:
            if required:
                raise RESTError(400, f"missing query parameter `{name}`")
            return default
        return values[-1]

    def query_flag(self, name):
        return self.query_value(name, "0").lower() in ("1", "true", "yes", "on")

    def log_message(self, format, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}\n")

    @classmethod
    def start_server(cls, host="127.0.0.1", port=5000, **server_opts):
        with RESTServer((host, port), cls, **server_opts) as server:
            server.serve_forever()


class HPCRESTHandler(RESTHandler):

    SLURM_COMMANDS = ('sbatch', 'squeue', 'sacct', 'scontrol', 'scancel')
    READ_ONLY_COMMANDS = ('squeue', 'sacct')  # also exposed over GET
    FILE_CHANGE_ROUTES = (
        ("PUT", "/files/content"),
        ("POST", "/files/mkdir"),
        ("DELETE", "/files"),
    )

    def get_routes(self) -> 'dict[tuple[str,str],method]':
        routes = {
            ("GET", "/health"): self.do_health,
            ("GET", "/files"): self.do_list_files,
            ("DELETE", "/files"): self.do_delete_file,
            ("GET", "/files/content"): self.do_download,
            ("PUT", "/files/content"): self.do_upload,
            ("POST", "/files/mkdir"): self.do_mkdir,
        }
        for cmd in self.SLURM_COMMANDS:
            routes[("POST", f"/slurm/{cmd}")] = self._slurm_route(cmd)
            if cmd in self.READ_ONLY_COMMANDS:
                routes[("GET", f"/slurm/{cmd}")] = self._slurm_route(cmd)
        return routes

    def do_health(self):
        wl = self.server.whitelist
        return 200, {
            "status": "ok",
            "server": self.server_version,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
            "base_dir": wl.base_dir,
            "allowed_dirs": list(wl.roots) if wl.restricted else None,
            "file_changes": not self.server.disable_file_changes,
        }

    # SLURM
    def _slurm_route(self, command):
        def route(_cmd=command):
            return self.run_slurm(_cmd)
        return route

    def run_slurm(self, command):
        if self.command == "GET":
            args = self.query.get("arg", [])
            cwd = self.query_value("cwd")
            stdin = None
        else:
            body = self.read_json()
            args = body.get("args", [])
            cwd = body.get("cwd")
            stdin = body.get("input")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise RESTError(400, "`args` must be a list of strings")
        if stdin is not None and not isinstance(stdin, str):
            raise RESTError(400, "`input` must be a string")
        if cwd is None:
            cwd = self.server.whitelist.base_dir
        _, cwd = self.server.whitelist.resolve(cwd)
        if not os.path.isdir(cwd):
            raise RESTError(400, f"cwd {cwd} is not a directory")
        return self.subprocess_response(command, args, cwd=cwd, input=stdin)

    def subprocess_response(self, command, args, cwd=None, input=None):
        call = [command, *args]
        try:
            res = subprocess.run(
                call, cwd=cwd, input=input, capture_output=True, text=True,
                timeout=self.server.command_timeout,
                stdin=subprocess.DEVNULL if input is None else None,
            )
        except FileNotFoundError:
            raise RESTError(503, f"`{command}` not found on PATH")
        except subprocess.TimeoutExpired as e:
            raise RESTError(504, f"`{command}` timed out after {e.timeout}s", command=call)
        # 200 whenever the command ran; a SLURM-level failure is reported
        # through `returncode`/`stderr`, not the HTTP status
        return 200, {
            "command": call,
            "cwd": cwd,
            "returncode": res.returncode,
            "stdout": res.stdout,
            "stderr": res.stderr,
        }

    # files
    def resolve_path(self, param="path"):
        return self.server.whitelist.resolve(self.query_value(param, required=True))

    @staticmethod
    def stat_entry(path, name=None):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            kind = "symlink"
        elif stat.S_ISDIR(info.st_mode):
            kind = "directory"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        else:
            kind = "other"
        return {
            "name": os.path.basename(path) if name is None else name,
            "path": path,
            "type": kind,
            "size": info.st_size,
            "mtime": info.st_mtime,
            "mode": oct(stat.S_IMODE(info.st_mode)),
        }

    def do_list_files(self):
        _, real = self.resolve_path()
        if not os.path.exists(real):
            raise RESTError(404, f"{real} does not exist")
        if not os.path.isdir(real):
            return 200, self.stat_entry(real)
        with os.scandir(real) as it:
            entries = [self.stat_entry(e.path, e.name) for e in sorted(it, key=lambda e: e.name)]
        return 200, {"path": real, "type": "directory", "entries": entries}

    def do_download(self):
        _, real = self.resolve_path()
        if not os.path.isfile(real):
            raise RESTError(404, f"{real} is not a file")
        with open(real, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{urllib.parse.quote(os.path.basename(real))}")
            self.end_headers()
            self.responded = True
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def do_upload(self):
        length = self.content_length(required=True)
        if length > self.server.max_upload:
            self.close_connection = True  # don't try to drain a huge body
            raise RESTError(413, f"upload of {length} bytes exceeds the {self.server.max_upload} byte limit")
        _, real = self.resolve_path()
        parent = os.path.dirname(real)
        if os.path.isdir(real):
            raise RESTError(409, f"{real} is a directory")
        existed = os.path.exists(real)
        if existed and not self.query_flag("overwrite"):
            raise RESTError(409, f"{real} exists; pass overwrite=1 to replace it")
        if not os.path.isdir(parent):
            if not self.query_flag("parents"):
                raise RESTError(404, f"directory {parent} does not exist; pass parents=1 to create it")
            os.makedirs(parent, exist_ok=True)

        # stream into a temp file next to the target, then atomically swap it in
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{os.path.basename(real)}.", suffix=".upload")
        try:
            remaining = length
            with os.fdopen(fd, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise RESTError(400, f"upload ended after {length - remaining} of {length} bytes")
                    f.write(chunk)
                    remaining -= len(chunk)
            os.chmod(tmp, self.server.file_mode)
            os.replace(tmp, real)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return (200 if existed else 201), self.stat_entry(real)

    def do_mkdir(self):
        _, real = self.resolve_path()
        if os.path.isdir(real) and self.query_flag("parents"):
            return 200, self.stat_entry(real)
        if os.path.exists(real):
            raise RESTError(409, f"{real} already exists")
        if self.query_flag("parents"):
            os.makedirs(real)
        else:
            os.mkdir(real)
        return 201, self.stat_entry(real)

    def do_delete_file(self):
        logical, real = self.resolve_path()
        if self.server.whitelist.is_root(real) or self.server.whitelist.is_root(logical):
            raise RESTError(403, f"refusing to delete allowed root {logical}")
        if not os.path.lexists(logical):
            raise RESTError(404, f"{logical} does not exist")
        if os.path.isdir(logical) and not os.path.islink(logical):
            try:
                os.rmdir(logical)
            except OSError:
                raise RESTError(409, f"{logical} is not empty; only empty directories can be deleted")
        else:
            os.remove(logical)
        return 200, {"deleted": logical}


################################################################################
##
##  CLI
##

def parse_size(size):
    size = str(size).strip().upper()
    scale = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    if size and size[-1] == "B":
        size = size[:-1]
    if size and size[-1] in scale:
        return int(float(size[:-1]) * scale[size[-1]])
    return int(size)

def parse_args(argv=None):
    env_dirs = os.environ.get("HPC_REST_ALLOWED_DIRS", "")
    parser = argparse.ArgumentParser(description="hpclib REST server for SLURM and file access")
    parser.add_argument("--host", default=os.environ.get("HPC_REST_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=os.environ.get("HPC_REST_PORT", os.environ.get("PROCESS_PORT", 5000)))
    parser.add_argument("--allow", action="append", default=[d for d in env_dirs.split(":") if d],
                        metavar="DIR",
                        help="restrict file access to DIR and its children (repeatable; "
                             "also $HPC_REST_ALLOWED_DIRS, colon-separated). Default: no restrictions")
    parser.add_argument("--base-dir", default=None,
                        help="directory relative paths are resolved against (default: the first --allow "
                             "directory if given, otherwise the current directory)")
    parser.add_argument("--token-file", default=None,
                        help=f"default: ${TokenAuth.TOKEN_FILE_ENV_VAR} or {TokenAuth.default_token_file()}")
    parser.add_argument("--command-timeout", type=float, default=120)
    parser.add_argument("--max-upload", type=parse_size, default="1G")
    parser.add_argument("--disable-file-changes", action="store_true",
                        default=os.environ.get("HPC_REST_DISABLE_FILE_CHANGES", "").lower()
                                in ("1", "true", "yes", "on"),
                        help="refuse uploads, mkdir, and deletes (also $HPC_REST_DISABLE_FILE_CHANGES=1)")
    return parser.parse_args(argv)

def main(argv=None, handler_class=HPCRESTHandler):
    opts = parse_args(argv)

    ppid = os.environ.get("PARENT_PROCESS_ID")
    if ppid is not None:
        setup_parent_terminated_listener(ppid)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    base_dir = opts.base_dir
    if base_dir is None and opts.allow:
        base_dir = os.path.expanduser(opts.allow[0])
    whitelist = PathWhitelist(opts.allow, base_dir=base_dir)
    auth = TokenAuth.load(opts.token_file)

    if opts.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {opts.host} exposes this server beyond the local node", file=sys.stderr)
    print(f"hpclib REST server on http://{opts.host}:{opts.port}")
    print(f"  token: {auth.source}")
    print(f"  allowed dirs: {', '.join(whitelist.roots) if whitelist.restricted else 'unrestricted'}")
    print(f"  file changes: {'disabled' if opts.disable_file_changes else 'enabled'}")
    print(f"  base dir: {whitelist.base_dir}", flush=True)

    try:
        handler_class.start_server(
            opts.host, opts.port,
            auth=auth, whitelist=whitelist,
            command_timeout=opts.command_timeout, max_upload=opts.max_upload,
            disable_file_changes=opts.disable_file_changes,
        )
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
