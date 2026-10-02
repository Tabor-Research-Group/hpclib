#!/usr/bin/env python3
"""
A small, dependency-free REST server for driving SLURM and moving
files on an HPC system from the far side of an hpclib tunnel.

Only the standard library is used, so this runs on any node with
Python >= 3.10 - no conda environment or `pip install` required.

Protections
  - Every request (including /health) must carry
    `Authorization: Bearer <token>`. The owner token comes from, in
    order, `$HPC_REST_TOKEN`, `--token-file`/`$HPC_REST_TOKEN_FILE`, or
    `$HPCTUNNELS_DATA_DIR/rest_token` (default ~/.local/tunnels/rest_token).
    A missing token file is created with a fresh random token and
    mode 600; an existing one must be owned by this user and not
    readable by group/other or the server refuses to start. The file
    may hold `sha256:<hex>` instead of the token itself
    (`--hash-token-file` converts it), so nothing on the cluster - a
    job included - can read the owner token back.
  - Scoped tokens (`--add-token`) are for clients with less than full
    access, such as an LLM. They are stored only as hashes in
    `$HPCTUNNELS_DATA_DIR/rest/tokens.json`, carry a subset of the
    scopes below, and must be limited to a list of directories.
    Revocation (`--revoke-token`) applies immediately.
        read         health, cluster info, templates, job status,
                     listing and reading files
        submit       submit and cancel template jobs
        propose      propose new templates for the owner to review
        files:write  upload, mkdir, delete
        slurm        the raw /slurm routes, and every API job
        *            everything (the owner token)
  - Binds 127.0.0.1 by default, so it is only reachable through the
    tunnel's port forward (and by other users on the same node, which
    is what the token is for).
  - File endpoints can be restricted to a whitelist of directories (and
    their children) with `--allow DIR` / `$HPC_REST_ALLOWED_DIRS`
    (colon-separated). With no whitelist, file access is unrestricted
    (i.e. limited only by this user's own permissions). Symlinks and
    `..` are resolved before the check, so neither can escape a root.
    The whitelist governs the file endpoints and the `cwd` of SLURM
    commands. It sandboxes template jobs only when the config has a
    `sandbox` section (see rest_sandbox.py): then each template body runs
    in Singularity/Apptainer and can write only to the submitting token's
    directories. GET /sandbox reports what the node supports.
    `$HPCTUNNELS_DATA_DIR` (tokens, templates, the job registry, the
    audit log, session files) is never reachable through the API.
  - Template jobs (see rest_jobs.py) can't add sbatch options or
    script text; their resources are checked against `limits` in the
    config file, including a cap on concurrently active API jobs.
  - SLURM commands are run as argument lists (never through a shell)
    with a timeout, and without the token in their environment.
  - `--disable-file-changes` (or `$HPC_REST_DISABLE_FILE_CHANGES=1`)
    turns off the routes that create, overwrite, or delete files
    (upload, mkdir, delete); they answer 403. Reading files and all
    SLURM routes stay available - note a job submitted through sbatch
    can still write files when it runs.
  - Every request is appended to an audit log (JSON lines).

Endpoints (all responses are JSON unless noted)    scope
  GET    /health                                    any token
  GET    /cluster                                   read
  GET    /sandbox[?refresh=1]   security features of this node, a self-test   read
                                 of the sandbox, and a recommended config
  GET    /templates                                 read
  GET    /templates/guide?name=T                    read
  GET    /templates/proposals                       read
  POST   /templates/propose  {"name", "template", "script", "guide", "rationale"}   propose
  GET    /modules/avail[?query=Q]                   read
  GET    /modules/spider[?query=Q]                  read
  POST   /jobs          {"template", "params", "resources", "workdir",
                         "idempotency_key", "dry_run", "label",
                         "tasks" | "tasks_from", "throttle"}   submit
  GET    /jobs[?active=1][&limit=N][&label=L]       read
  GET    /jobs/status?id=J[&tasks=1]                read
  GET    /jobs/wait?id=J[&timeout=S]                read
  POST   /jobs/cancel?id=J                          submit
  POST   /slurm/{sbatch,squeue,sacct,scontrol,scancel}
           body: {"args": [...], "cwd": "...", "input": "..."}   slurm
  GET    /slurm/{squeue,sacct}?arg=...&arg=...&cwd=...          slurm
  GET    /files?path=P                       list a directory / stat a file   read
  GET    /files/content?path=P               download (raw bytes)             read
  GET    /files/read?path=P[&offset=N][&length=N]   bounded text read         read
  GET    /files/tail?path=P[&lines=N]        last lines of a text file        read
  PUT    /files/content?path=P[&overwrite=1][&parents=1]
                                             upload (raw request body)  files:write
  POST   /files/mkdir?path=P[&parents=1]                                files:write
  DELETE /files?path=P                       file, symlink, or empty dir  files:write

Usage: rest_server.py [--host H] [--port P] [--allow DIR ...] [--config F]
                      [--token-file F] [--command-timeout S] [--max-upload N]
                      [--disable-file-changes]
       rest_server.py --add-token NAME --scopes read,submit --token-allow DIR [...]
       rest_server.py --revoke-token NAME | --revoke-token-hash SHA256 | --list-tokens | --hash-token-file
       rest_server.py --list-proposals | --approve-template NAME [--replace] | --reject-template NAME
       rest_server.py --init-config [--config-base F] [--rebuild] [--no-sandbox] [--sandbox-bind DIR ...]
       rest_server.py --probe-sandbox
"""
import abc
import argparse
import hashlib
import hmac
import http.server
import json
import os
import re
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

try:
    from . import rest_jobs, rest_sandbox
except ImportError:  # run as a script from the servers directory
    import rest_jobs
    import rest_sandbox
RESTError = rest_jobs.RESTError  # re-exported

__all__ = [
    "RESTError",
    "PathWhitelist",
    "TokenIdentity",
    "TokenAuth",
    "AuditLog",
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

def tunnels_data_dir():
    return os.path.expanduser(os.environ.get("HPCTUNNELS_DATA_DIR", "~/.local/tunnels"))

def rest_data_dir():
    return os.path.join(tunnels_data_dir(), "rest")


class PathWhitelist:
    """
    A set of allowed root directories. A path is allowed if it, after
    resolving `~`, `..`, and symlinks, lies inside one of the roots and
    inside none of the `deny` paths. `roots=None` means no restrictions;
    an empty list means nothing is allowed.
    Relative paths are taken relative to `base_dir` (the server's
    launch directory by default).
    """
    def __init__(self, roots=None, base_dir=None, deny=()):
        if roots is None:
            self.roots = None
        else:
            self.roots = tuple(os.path.realpath(os.path.expanduser(r)) for r in roots if r)
        self.deny = tuple(sorted({os.path.realpath(os.path.expanduser(d)) for d in deny if d}))
        if base_dir is None:
            base_dir = os.getcwd()
        self.base_dir = os.path.realpath(os.path.expanduser(base_dir))

    @property
    def restricted(self):
        return self.roots is not None

    @staticmethod
    def _inside(root, path):
        return os.path.commonpath([root, path]) == root

    def contains(self, real_path):
        if any(self._inside(d, real_path) for d in self.deny):
            return False
        if self.roots is None:
            return True
        return any(self._inside(root, real_path) for root in self.roots)

    def is_root(self, real_path):
        return self.roots is not None and real_path in self.roots

    def with_base(self, base_dir):
        """The same whitelist, resolving relative paths against `base_dir`."""
        wl = PathWhitelist.__new__(PathWhitelist)
        wl.roots, wl.deny = self.roots, self.deny
        wl.base_dir = os.path.realpath(os.path.expanduser(base_dir))
        return wl

    def restrict(self, roots):
        """A narrower whitelist: only those `roots` that this one allows."""
        if roots is None:
            return self
        roots = [os.path.realpath(os.path.expanduser(r)) for r in roots]
        kept = [r for r in roots if self.contains(r)]
        return PathWhitelist(kept, base_dir=kept[0] if kept else self.base_dir, deny=self.deny)

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
        if any(self._inside(d, p) for d in self.deny for p in (logical, real)):
            raise PermissionError(f"{path} is a protected server path")
        if not (self.contains(logical) and self.contains(real)):
            raise PermissionError(f"{path} is outside the allowed directories")
        return logical, real


class TokenIdentity:
    def __init__(self, name, scopes, allow=None):
        self.name = name
        self.scopes = tuple(scopes)
        self.allow = allow

    def has_scope(self, scope):
        return scope is None or "*" in self.scopes or scope in self.scopes

    def to_json(self):
        return {"name": self.name, "scopes": list(self.scopes), "allow": self.allow}


class TokenAuth:
    """Bearer-token checking plus loading/creating the tokens themselves."""

    TOKEN_ENV_VAR = 'HPC_REST_TOKEN'
    TOKEN_FILE_ENV_VAR = 'HPC_REST_TOKEN_FILE'
    TOKEN_FILE_NAME = 'rest_token'
    TOKEN_BYTES = 32
    HASH_PREFIX = "sha256:"
    OWNER = "owner"
    SCOPES = ("read", "submit", "propose", "files:write", "slurm", "*")
    NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

    def __init__(self, token, source=None, tokens_file=None):
        if not token:
            raise ValueError("an empty token would allow anyone in")
        if token.startswith(self.HASH_PREFIX):
            self.owner_hash = token[len(self.HASH_PREFIX):].strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", self.owner_hash):
                raise ValueError("owner token hash must be 64 hex digits")
        else:
            self.owner_hash = self.hash(token)
        self.source = source
        self.tokens_file = tokens_file
        self._tokens_cache = (None, [])
        self._lock = threading.Lock()

    @staticmethod
    def hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def scoped_tokens(self):
        """Entries of the tokens file, re-read whenever it changes."""
        if not self.tokens_file:
            return []
        try:
            mtime = os.stat(self.tokens_file).st_mtime_ns
        except FileNotFoundError:
            return []
        with self._lock:
            if self._tokens_cache[0] != mtime:
                entries = self.read_tokens_file(self.tokens_file)
                self._tokens_cache = (mtime, [e for e in entries if self.valid_entry(e)])
            return self._tokens_cache[1]

    @classmethod
    def valid_entry(cls, entry):
        scopes = entry.get("scopes", [])
        if not isinstance(entry.get("sha256"), str) or not cls.NAME_RE.fullmatch(str(entry.get("name", ""))):
            return False
        if not scopes or any(s not in cls.SCOPES for s in scopes):
            return False
        # anything short of full access has to be pinned to directories
        if "*" not in scopes and not entry.get("allow"):
            print(f"ignoring token {entry['name']}: scoped tokens need an `allow` list", file=sys.stderr)
            return False
        return True

    def identify(self, header):
        if not header:
            return None
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            return None
        digest = self.hash(value.strip())
        if hmac.compare_digest(digest, self.owner_hash):
            return TokenIdentity(self.OWNER, ("*",))
        for entry in self.scoped_tokens():
            if hmac.compare_digest(digest, entry["sha256"]):
                return TokenIdentity(entry["name"], entry["scopes"], entry.get("allow"))
        return None

    def check(self, header) -> bool:
        return self.identify(header) is not None

    @classmethod
    def default_token_file(cls):
        return os.path.join(tunnels_data_dir(), cls.TOKEN_FILE_NAME)

    @classmethod
    def default_tokens_file(cls):
        return os.path.join(rest_data_dir(), "tokens.json")

    @staticmethod
    def check_private(path):
        info = os.stat(path)
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise PermissionError(f"{path} is not owned by the current user")
        if info.st_mode & 0o077:
            raise PermissionError(f"{path} is accessible by other users; run `chmod 600 {path}`")

    @classmethod
    def read_token_file(cls, token_file):
        cls.check_private(token_file)
        with open(token_file) as f:
            token = f.read().strip()
        if not token:
            raise ValueError(f"token file {token_file} is empty")
        return token

    @staticmethod
    def write_private(path, text):
        os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tokens.")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)  # mkstemp files are already 600

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
    def read_tokens_file(cls, tokens_file):
        cls.check_private(tokens_file)
        with open(tokens_file) as f:
            data = json.load(f)
        return data.get("tokens", [])

    @classmethod
    def add_token(cls, tokens_file, name, scopes, allow=None):
        if not cls.NAME_RE.fullmatch(name) or name == cls.OWNER:
            raise ValueError(f"invalid token name {name!r}")
        bad = [s for s in scopes if s not in cls.SCOPES]
        if not scopes or bad:
            raise ValueError(f"unknown scopes {bad}; choose from {cls.SCOPES}")
        if "*" not in scopes and not allow:
            raise ValueError("scoped tokens must be limited to directories with --token-allow")
        entries = cls.read_tokens_file(tokens_file) if os.path.exists(tokens_file) else []
        if any(e.get("name") == name for e in entries):
            raise ValueError(f"a token named {name!r} already exists; revoke it first")
        token = secrets.token_urlsafe(cls.TOKEN_BYTES)
        entry = {"name": name, "sha256": cls.hash(token), "scopes": list(scopes), "created": time.time()}
        if allow:
            entry["allow"] = [os.path.realpath(os.path.expanduser(a)) for a in allow]
        cls.write_private(tokens_file, json.dumps({"tokens": entries + [entry]}, indent=2) + "\n")
        return token

    @classmethod
    def revoke_token(cls, tokens_file, name):
        entries = cls.read_tokens_file(tokens_file) if os.path.exists(tokens_file) else []
        kept = [e for e in entries if e.get("name") != name]
        if len(kept) == len(entries):
            raise ValueError(f"no token named {name!r}")
        cls.write_private(tokens_file, json.dumps({"tokens": kept}, indent=2) + "\n")

    @classmethod
    def revoke_token_hash(cls, tokens_file, digest):
        """Revoke the token whose sha256 is `digest`; returns its name."""
        entries = cls.read_tokens_file(tokens_file) if os.path.exists(tokens_file) else []
        names = [e.get("name") for e in entries if hmac.compare_digest(str(e.get("sha256", "")), digest)]
        if not names:
            raise ValueError("no token has that hash")
        cls.write_private(tokens_file, json.dumps({"tokens": [e for e in entries if e.get("name") not in names]},
                                                  indent=2) + "\n")
        return names[0]

    @classmethod
    def hash_token_file(cls, token_file):
        token = cls.read_token_file(token_file)
        if token.startswith(cls.HASH_PREFIX):
            return False
        cls.write_private(token_file, cls.HASH_PREFIX + cls.hash(token) + "\n")
        return True

    @classmethod
    def load(cls, token_file=None, tokens_file=None):
        token = os.environ.pop(cls.TOKEN_ENV_VAR, None)  # keep it out of child processes
        if token:
            return cls(token, source=f"${cls.TOKEN_ENV_VAR}", tokens_file=tokens_file)
        if token_file is None:
            token_file = os.environ.get(cls.TOKEN_FILE_ENV_VAR) or cls.default_token_file()
        token_file = os.path.expanduser(token_file)
        try:
            token = cls.create_token_file(token_file)
        except FileExistsError:
            token = cls.read_token_file(token_file)
        return cls(token, source=token_file, tokens_file=tokens_file)


class AuditLog:
    """Append-only JSON-lines record of every request."""
    def __init__(self, path):
        self.path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), mode=0o700, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, entry):
        line = json.dumps(entry, sort_keys=True) + "\n"
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(line)


class RESTServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_class, auth: TokenAuth, whitelist: PathWhitelist,
                 command_timeout=120, max_upload=1 << 30, disable_file_changes=False,
                 jobs: 'rest_jobs.JobManager' = None, audit: AuditLog = None):
        self.auth = auth
        self.whitelist = whitelist
        self.command_timeout = command_timeout
        self.max_upload = max_upload
        self.disable_file_changes = disable_file_changes
        self.jobs = jobs
        self.audit = audit
        self.sandbox_prober = None
        if jobs is not None:
            probe_dir = os.path.join(jobs.sandbox.data_dir or tempfile.gettempdir(), "sandbox", "probe")
            self.sandbox_prober = rest_sandbox.Prober(jobs.sandbox, probe_dir)
        umask = os.umask(0)
        os.umask(umask)
        self.file_mode = 0o666 & ~umask
        super().__init__(address, handler_class)


class RESTHandler(http.server.BaseHTTPRequestHandler):
    """
    Minimal JSON router. Subclasses supply `get_routes`, a dict mapping
    `(verb, path)` to a zero-argument method returning `(status, payload)`,
    or `None` if the method already wrote its own response.
    `ROUTE_SCOPES` maps `(verb, path)` to the token scope a route needs
    (`None` for any valid token); routes not listed need `*`.
    Routes listed in `FILE_CHANGE_ROUTES` are refused when the server
    was started with `disable_file_changes`.
    """

    server_version = "hpclib-rest/0.2"
    MAX_JSON_BODY = 1 << 20
    FILE_CHANGE_ROUTES = ()
    ROUTE_SCOPES = {}
    server: RESTServer

    @abc.abstractmethod
    def get_routes(self) -> 'dict[tuple[str,str],method]':
        ...

    def do_GET(self): self.handle_rest_request("GET")
    def do_POST(self): self.handle_rest_request("POST")
    def do_PUT(self): self.handle_rest_request("PUT")
    def do_DELETE(self): self.handle_rest_request("DELETE")

    @property
    def whitelist(self) -> PathWhitelist:
        """The server's whitelist, narrowed to the token's directories."""
        return self.server.whitelist.restrict(self.identity.allow if self.identity else None)

    def handle_rest_request(self, verb):
        self.responded = False
        self.response_status = None
        self.identity = None
        self.audit_detail = None
        try:
            url = urllib.parse.urlsplit(self.path)
            self.query = urllib.parse.parse_qs(url.query)
            route = url.path.rstrip("/") or "/"
            # auth before routing so unauthenticated clients can't probe routes
            self.identity = self.server.auth.identify(self.headers.get("Authorization"))
            if self.identity is None:
                raise RESTError(401, "missing or invalid bearer token")
            routes = self.get_routes()
            caller = routes.get((verb, route))
            if caller is None:
                allowed = sorted(v for v, p in routes if p == route)
                if allowed:
                    raise RESTError(405, f"{verb} not supported on {route}", allowed=allowed)
                raise RESTError(404, f"unknown route {route}")
            scope = self.ROUTE_SCOPES.get((verb, route), "*")
            if not self.identity.has_scope(scope):
                raise RESTError(403, f"token {self.identity.name!r} lacks the `{scope}` scope "
                                     f"needed for {verb} {route}", scopes=list(self.identity.scopes))
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
        finally:
            self.write_audit(verb)

    def write_audit(self, verb):
        if self.server.audit is None:
            return
        entry = {
            "time": time.time(),
            "client": self.client_address[0] if self.client_address else None,
            "token": self.identity.name if self.identity else None,
            "verb": verb,
            "path": self.path,
            "status": self.response_status,
        }
        if self.audit_detail:
            entry["detail"] = self.audit_detail
        try:
            self.server.audit.write(entry)
        except OSError:
            traceback.print_exc(limit=1)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode() + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.responded = True

    def send_response(self, code, message=None):
        self.response_status = code
        super().send_response(code, message)

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

    def query_int(self, name, default, minimum=0, maximum=None):
        value = self.query_value(name)
        if value is None:
            return default
        try:
            value = int(value)
        except ValueError:
            raise RESTError(400, f"`{name}` must be an integer")
        if value < minimum or (maximum is not None and value > maximum):
            raise RESTError(400, f"`{name}` must be between {minimum} and {maximum}")
        return value

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
    ROUTE_SCOPES = {
        **{
            ("GET", "/health"): None,
            ("GET", "/cluster"): "read",
            ("GET", "/sandbox"): "read",
            ("GET", "/templates"): "read",
            ("GET", "/templates/guide"): "read",
            ("GET", "/templates/proposals"): "read",
            ("POST", "/templates/propose"): "propose",
            ("GET", "/modules/avail"): "read",
            ("GET", "/modules/spider"): "read",
            ("GET", "/jobs"): "read",
            ("GET", "/jobs/status"): "read",
            ("GET", "/jobs/wait"): "read",
            ("POST", "/jobs"): "submit",
            ("POST", "/jobs/cancel"): "submit",
            ("GET", "/files"): "read",
            ("GET", "/files/content"): "read",
            ("GET", "/files/read"): "read",
            ("GET", "/files/tail"): "read",
            ("PUT", "/files/content"): "files:write",
            ("POST", "/files/mkdir"): "files:write",
            ("DELETE", "/files"): "files:write",
        },
        **{("POST", f"/slurm/{c}"): "slurm" for c in SLURM_COMMANDS},
        **{("GET", f"/slurm/{c}"): "slurm" for c in READ_ONLY_COMMANDS},
    }
    MAX_READ = 1 << 20
    DEFAULT_READ = 64 << 10

    def get_routes(self) -> 'dict[tuple[str,str],method]':
        routes = {
            ("GET", "/health"): self.do_health,
            ("GET", "/cluster"): self.do_cluster,
            ("GET", "/sandbox"): self.do_sandbox,
            ("GET", "/templates"): self.do_templates,
            ("GET", "/templates/guide"): self.do_template_guide,
            ("GET", "/templates/proposals"): self.do_list_proposals,
            ("POST", "/templates/propose"): self.do_propose_template,
            ("GET", "/modules/avail"): self.do_modules_avail,
            ("GET", "/modules/spider"): self.do_modules_spider,
            ("GET", "/jobs"): self.do_list_jobs,
            ("POST", "/jobs"): self.do_submit_job,
            ("GET", "/jobs/status"): self.do_job_status,
            ("GET", "/jobs/wait"): self.do_wait_job,
            ("POST", "/jobs/cancel"): self.do_cancel_job,
            ("GET", "/files"): self.do_list_files,
            ("DELETE", "/files"): self.do_delete_file,
            ("GET", "/files/content"): self.do_download,
            ("PUT", "/files/content"): self.do_upload,
            ("GET", "/files/read"): self.do_read_file,
            ("GET", "/files/tail"): self.do_tail_file,
            ("POST", "/files/mkdir"): self.do_mkdir,
        }
        for cmd in self.SLURM_COMMANDS:
            routes[("POST", f"/slurm/{cmd}")] = self._slurm_route(cmd)
            if cmd in self.READ_ONLY_COMMANDS:
                routes[("GET", f"/slurm/{cmd}")] = self._slurm_route(cmd)
        return routes

    def do_health(self):
        wl = self.whitelist
        return 200, {
            "status": "ok",
            "server": self.server_version,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
            "base_dir": wl.base_dir,
            "allowed_dirs": list(wl.roots) if wl.restricted else None,
            "file_changes": not self.server.disable_file_changes,
            "jobs_enabled": self.server.jobs is not None,
            "token": self.identity.to_json(),
        }

    # template jobs
    @property
    def jobs(self) -> 'rest_jobs.JobManager':
        if self.server.jobs is None:
            raise RESTError(503, "template jobs are not configured on this server")
        return self.server.jobs

    @property
    def see_all_jobs(self):
        return self.identity.has_scope("slurm")

    def do_cluster(self):
        return 200, self.jobs.cluster_info(self.identity.name, self.whitelist, self.see_all_jobs)

    def do_sandbox(self):
        prober = self.server.sandbox_prober
        if prober is None:
            raise RESTError(503, "the sandbox probe is not configured on this server")
        return 200, prober.get(refresh=self.query_flag("refresh"))

    def do_templates(self):
        return 200, self.jobs.describe_templates()

    def do_template_guide(self):
        return 200, self.jobs.guide(self.query_value("name", required=True))

    def do_list_proposals(self):
        return 200, self.jobs.list_proposals()

    def do_propose_template(self):
        status, payload = self.jobs.propose_template(self.identity.name, self.read_json())
        self.audit_detail = {"proposed": payload.get("name")}
        return status, payload

    def do_modules_avail(self):
        return 200, self.jobs.modules.query("avail", self.query_value("query", ""))

    def do_modules_spider(self):
        return 200, self.jobs.modules.query("spider", self.query_value("query", ""))

    def do_submit_job(self):
        status, payload = self.jobs.submit(self.identity.name, self.read_json(), self.whitelist)
        self.audit_detail = {k: payload.get(k) for k in ("template", "job_id", "dry_run", "duplicate")
                             if k in payload}
        return status, payload

    def do_list_jobs(self):
        limit = self.query_int("limit", 50, minimum=1, maximum=1000)
        jobs = self.jobs.list_jobs(self.identity.name, self.see_all_jobs, limit=limit,
                                   active_only=self.query_flag("active"), label=self.query_value("label"))
        return 200, {"jobs": jobs}

    def do_job_status(self):
        return 200, self.jobs.status(self.query_value("id", required=True), self.identity.name,
                                     self.see_all_jobs, include_tasks=self.query_flag("tasks"))

    def do_wait_job(self):
        timeout = self.query_int("timeout", 60, minimum=0, maximum=self.jobs.MAX_WAIT)
        return 200, self.jobs.wait(self.query_value("id", required=True), self.identity.name,
                                   self.see_all_jobs, timeout)

    def do_cancel_job(self):
        job_id = self.query_value("id", required=True)
        self.audit_detail = {"job_id": job_id}
        return 200, self.jobs.cancel(job_id, self.identity.name, self.see_all_jobs)

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
        whitelist = self.whitelist
        if cwd is None:
            cwd = whitelist.base_dir
        _, cwd = whitelist.resolve(cwd)
        if not os.path.isdir(cwd):
            raise RESTError(400, f"cwd {cwd} is not a directory")
        return self.subprocess_response(command, args, cwd=cwd, input=stdin)

    def subprocess_response(self, command, args, cwd=None, input=None):
        call = [command, *args]
        res = rest_jobs.SlurmRunner(timeout=self.server.command_timeout).run(call, input=input, cwd=cwd)
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
        return self.whitelist.resolve(self.query_value(param, required=True))

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

    def _regular_file(self):
        _, real = self.resolve_path()
        if not os.path.isfile(real):
            raise RESTError(404, f"{real} is not a file")
        return real

    def do_download(self):
        real = self._regular_file()
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

    @staticmethod
    def _text(chunk):
        if b"\x00" in chunk:
            return None  # binary; use /files/content
        return chunk.decode("utf-8", errors="replace")

    def do_read_file(self):
        real = self._regular_file()
        offset = self.query_int("offset", 0)
        length = self.query_int("length", self.DEFAULT_READ, minimum=1, maximum=self.MAX_READ)
        with open(real, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(offset)
            chunk = f.read(length)
        text = self._text(chunk)
        out = {"path": real, "size": size, "offset": offset, "length": len(chunk),
               "eof": offset + len(chunk) >= size, "binary": text is None}
        if text is not None:
            out["text"] = text
        return 200, out

    def do_tail_file(self):
        real = self._regular_file()
        lines = self.query_int("lines", 100, minimum=1, maximum=5000)
        max_bytes = self.query_int("max_bytes", self.DEFAULT_READ, minimum=1, maximum=self.MAX_READ)
        with open(real, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            start = max(0, size - max_bytes)
            f.seek(start)
            chunk = f.read(max_bytes)
        text = self._text(chunk)
        out = {"path": real, "size": size, "binary": text is None}
        if text is not None:
            split = text.splitlines()
            if start > 0 and split:
                split = split[1:]  # first line is probably partial
            out["truncated"] = start > 0 or len(split) > lines
            out["lines"] = split[-lines:]
        return 200, out

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
        whitelist = self.whitelist
        if any(wl.is_root(p) for wl in (whitelist, self.server.whitelist) for p in (real, logical)):
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

CONFIG_KEYS = {"limits", "templates_dir", "jobs_db", "audit_log", "tokens_file", "cluster_notes",
               "poll_interval", "proposals_dir", "module_command", "sandbox"}

def load_config(path):
    """
    Optional JSON config, $HPCTUNNELS_DATA_DIR/rest/config.json by default:
      {"limits": {...ResourceLimits...}, "templates_dir": DIR, "jobs_db": FILE,
       "audit_log": FILE or null, "tokens_file": FILE, "cluster_notes": TEXT,
       "poll_interval": SECONDS, "proposals_dir": DIR,
       "module_command": ["bash", "-lc", "module \"$@\" 2>&1", "hpclib-module"],
       "sandbox": {...rest_sandbox.Sandbox...}}
    """
    explicit = path is not None
    path = os.path.expanduser(path or os.path.join(rest_data_dir(), "config.json"))
    config = {}
    if os.path.exists(path):
        with open(path) as f:
            config = json.load(f)
    elif explicit:
        raise FileNotFoundError(f"config file {path} does not exist")
    unknown = set(config) - CONFIG_KEYS
    if unknown:
        raise ValueError(f"unknown config keys {sorted(unknown)}; known: {sorted(CONFIG_KEYS)}")
    data = rest_data_dir()
    config.setdefault("templates_dir", os.path.join(data, "templates"))
    config.setdefault("jobs_db", os.path.join(data, "jobs.sqlite"))
    config.setdefault("audit_log", os.path.join(data, "audit.log"))
    config.setdefault("tokens_file", TokenAuth.default_tokens_file())
    config.setdefault("proposals_dir", os.path.join(data, "proposals"))
    command = config.get("module_command")
    if command is not None and (not isinstance(command, list) or not all(isinstance(c, str) for c in command)):
        raise ValueError("`module_command` must be a list of strings")
    sandbox = config.get("sandbox")
    if sandbox is not None and not isinstance(sandbox, dict):
        raise ValueError("`sandbox` must be an object")
    config["path"] = path
    return config

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
    parser.add_argument("--config", default=os.environ.get("HPC_REST_CONFIG"),
                        help=f"JSON config with limits, templates_dir, ... (default {rest_data_dir()}/config.json)")
    parser.add_argument("--token-file", default=None,
                        help=f"default: ${TokenAuth.TOKEN_FILE_ENV_VAR} or {TokenAuth.default_token_file()}")
    parser.add_argument("--command-timeout", type=float, default=120)
    parser.add_argument("--max-upload", type=parse_size, default="1G")
    parser.add_argument("--disable-file-changes", action="store_true",
                        default=os.environ.get("HPC_REST_DISABLE_FILE_CHANGES", "").lower()
                                in ("1", "true", "yes", "on"),
                        help="refuse uploads, mkdir, and deletes (also $HPC_REST_DISABLE_FILE_CHANGES=1)")
    tokens = parser.add_argument_group("token management (run on the cluster; exits afterwards)")
    tokens.add_argument("--add-token", metavar="NAME", help="mint a scoped token and print it once")
    tokens.add_argument("--scopes", default="read",
                        help=f"comma-separated, from {', '.join(TokenAuth.SCOPES)} (default: read)")
    tokens.add_argument("--token-allow", action="append", default=[], metavar="DIR",
                        help="directory the new token is limited to (repeatable; required unless --scopes '*')")
    tokens.add_argument("--revoke-token", metavar="NAME")
    tokens.add_argument("--revoke-token-hash", metavar="SHA256",
                        help="revoke the token with this sha256 (for a token file you have but whose name you don't)")
    tokens.add_argument("--list-tokens", action="store_true")
    tokens.add_argument("--hash-token-file", action="store_true",
                        help="replace the owner token file's contents with a hash of the token")
    review = parser.add_argument_group("template proposals (run on the cluster; exits afterwards)")
    review.add_argument("--list-proposals", action="store_true")
    review.add_argument("--approve-template", metavar="NAME", help="move a reviewed proposal into the templates")
    review.add_argument("--replace", action="store_true", help="with --approve-template: replace an existing "
                                                               "template (the old one is kept as NAME.replaced-TIME)")
    review.add_argument("--reject-template", metavar="NAME")
    setup = parser.add_argument_group("setup (run on the cluster; exits afterwards; used by setup_agents)")
    setup.add_argument("--init-config", action="store_true",
                       help="write the config file (with a job sandbox) if there is none; an existing one gets "
                            "a `sandbox` section if it lacks one, and is otherwise kept")
    setup.add_argument("--config-base", metavar="FILE", help="with --init-config: JSON to start from")
    setup.add_argument("--rebuild", action="store_true",
                       help="with --init-config: replace an existing config (kept as CONFIG.replaced-TIME) "
                            "and rebuild the sandbox's host image")
    setup.add_argument("--no-sandbox", action="store_true", help="with --init-config: don't add a sandbox")
    setup.add_argument("--sandbox-bind", action="append", default=[], metavar="DIR",
                       help="with --init-config: a directory jobs may read (repeatable); module trees found "
                            "on MODULEPATH are added automatically")
    setup.add_argument("--probe-sandbox", action="store_true",
                       help="print what this node supports for sandboxing, and test the configured sandbox")
    return parser.parse_args(argv)

def manage_tokens(opts, config):
    tokens_file = os.path.expanduser(config["tokens_file"])
    if opts.add_token:
        scopes = [s.strip() for s in opts.scopes.split(",") if s.strip()]
        token = TokenAuth.add_token(tokens_file, opts.add_token, scopes, opts.token_allow or None)
        print(f"token {opts.add_token!r} ({', '.join(scopes)}) - it is shown only this once:", file=sys.stderr)
        print(token)
    elif opts.revoke_token:
        TokenAuth.revoke_token(tokens_file, opts.revoke_token)
        print(f"revoked {opts.revoke_token!r}")
    elif opts.revoke_token_hash:
        print(f"revoked {TokenAuth.revoke_token_hash(tokens_file, opts.revoke_token_hash.strip().lower())!r}")
    elif opts.list_tokens:
        entries = TokenAuth.read_tokens_file(tokens_file) if os.path.exists(tokens_file) else []
        for e in entries:
            print(json.dumps({k: e.get(k) for k in ("name", "scopes", "allow", "created")}))
    elif opts.hash_token_file:
        token_file = os.path.expanduser(opts.token_file or os.environ.get(TokenAuth.TOKEN_FILE_ENV_VAR)
                                        or TokenAuth.default_token_file())
        changed = TokenAuth.hash_token_file(token_file)
        print(f"{token_file} {'now holds a hash' if changed else 'already holds a hash'}")
    else:
        return manage_proposals(opts, config)
    return True

def manage_proposals(opts, config):
    store = rest_jobs.ProposalStore(config["proposals_dir"], rest_jobs.TemplateStore(config["templates_dir"]))
    if opts.list_proposals:
        for meta in store.list():
            print(json.dumps(meta))
            print(f"  review: {os.path.join(store.directory, meta['name'])}/")
    elif opts.approve_template:
        print(f"approved: {store.approve(opts.approve_template, replace=opts.replace)}")
    elif opts.reject_template:
        store.reject(opts.reject_template)
        print(f"rejected {opts.reject_template!r}")
    else:
        return False
    return True

DEFAULT_CONFIG = {
    "limits": {"max_time": "1-00:00:00", "max_mem": "64G", "max_cpus": 16, "max_nodes": 1, "max_gpus": 0,
               "max_concurrent_jobs": 4, "max_array_tasks": 500},
    "cluster_notes": "Describe this cluster for clients: partitions to prefer, module names, scratch rules.",
}

def _sandbox_section(given, binds):
    """
    `given` (a sandbox section, or None) completed for this node: method
    auto unless set, and reading `binds` and the module trees on MODULEPATH.
    """
    section = dict(given or {})
    section.setdefault("method", "auto")
    _, roots = rest_sandbox._module_roots()
    found = []
    for path in list(section.get("binds", [])) + list(binds) + roots:
        path = os.path.normpath(os.path.expanduser(path))
        if path not in found and path not in rest_sandbox.HOST_IMAGE_BINDS:
            found.append(path)
    section["binds"] = found
    return section

def _detached_delete(path):
    """Delete `path` in the background, so a slow filesystem can't hold up the caller."""
    try:
        subprocess.Popen(["rm", "-rf", path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass

def init_config(opts):
    """--init-config: write (or with --rebuild, replace) the config file; prints what it did."""
    path = os.path.expanduser(opts.config or os.path.join(rest_data_dir(), "config.json"))
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    base = dict(DEFAULT_CONFIG)
    if opts.config_base:
        with open(opts.config_base) as f:
            given = json.load(f)
        if not isinstance(given, dict):
            raise ValueError(f"{opts.config_base} must hold a JSON object")
        base.update(given)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    if os.path.exists(path) and not opts.rebuild:
        with open(path) as f:
            config = json.load(f)
        if "sandbox" in config or opts.no_sandbox:
            print(f"kept the existing {path}")
            return
        shutil.copy2(path, f"{path}.replaced-{stamp}")
        config["sandbox"] = _sandbox_section(base.get("sandbox"), opts.sandbox_bind)
        action = f"added a job sandbox to {path} (the old file is {path}.replaced-{stamp})"
    else:
        config = base
        if opts.no_sandbox:
            config["sandbox"] = {"method": "none"}
        else:
            config["sandbox"] = _sandbox_section(config.get("sandbox"), opts.sandbox_bind)
        action = f"wrote {path}"
        if os.path.exists(path):
            os.replace(path, f"{path}.replaced-{stamp}")
            action = f"rebuilt {path} (the old file is {path}.replaced-{stamp})"
    rest_sandbox.Sandbox(config.get("sandbox"))   # validate before writing
    unknown = set(config) - CONFIG_KEYS
    if unknown:
        raise ValueError(f"unknown config keys {sorted(unknown)}; known: {sorted(CONFIG_KEYS)}")
    TokenAuth.write_private(path, json.dumps(config, indent=2) + "\n")
    sandbox = config.get("sandbox", {})
    print(action)
    if sandbox.get("method") == "none":
        print("  job sandbox: off")
    else:
        print(f"  job sandbox: {sandbox.get('method')}, reading {', '.join(sandbox.get('binds') or []) or '(only /usr, /etc, /opt)'}")
    if opts.rebuild:
        image = os.path.join(rest_data_dir(), "sandbox", "host")
        if os.path.exists(image):
            old = os.path.join(rest_data_dir(), "sandbox", f".host.old-{stamp}")
            os.replace(image, old)
            _detached_delete(old)
            print("  the sandbox's host image will be rebuilt by the next job")

def probe_sandbox(config):
    """--probe-sandbox: a short report, from this node, of the sandbox the config describes."""
    sandbox = rest_sandbox.Sandbox(config.get("sandbox"), data_dir=rest_data_dir())
    probe_dir = os.path.join(rest_data_dir(), "sandbox", "probe")
    os.makedirs(probe_dir, mode=0o700, exist_ok=True)
    info = rest_sandbox.probe(sandbox, probe_dir)
    print(f"sandbox probe on {info['node']} ({info['os']}, kernel {info['kernel']}):")
    for r in info["container_runtimes"]:
        mode = "setuid" if r.get("setuid_installed") and r.get("setuid_allowed") else "user namespaces"
        print(f"  {r['version']} at {r['path']} ({mode})")
    if not info["container_runtimes"]:
        print("  no Singularity or Apptainer on PATH")
    described = info["sandbox"]
    print(f"  configured sandbox: {described['method']} -> {described['effective']}"
          + (f" ({described.get('error') or described.get('reason')})" if described.get("error") or described.get("reason") else ""))
    test = info.get("self_test")
    if test:
        if test.get("ran"):
            failed = [k for k, ok in test["checks"].items() if not ok]
            print(f"  test container: {'passed' if test['passed'] else 'FAILED'}"
                  + (f" ({', '.join(failed)})" if failed else "") + f" in {test['seconds']} s")
            if not test["passed"] and test.get("stderr"):
                print("    " + test["stderr"].strip().replace("\n", "\n    "))
        else:
            print(f"  test container: not run ({test.get('reason')})")
    for note in info["notes"]:
        print(f"  note: {note}")
    if not os.environ.get("SLURM_JOB_ID"):
        print("  (probed on this node; jobs run on compute nodes, where GET /sandbox reports again)")
    return test is None or test.get("passed", False)

def build_jobs(config, command_timeout):
    runner = rest_jobs.SlurmRunner(timeout=command_timeout)
    templates = rest_jobs.TemplateStore(config["templates_dir"])
    return rest_jobs.JobManager(
        templates=templates,
        proposals=rest_jobs.ProposalStore(config["proposals_dir"], templates),
        modules=rest_jobs.ModuleSystem(runner, config.get("module_command")),
        registry=rest_jobs.JobRegistry(config["jobs_db"]),
        limits=rest_jobs.ResourceLimits(**config.get("limits", {})),
        runner=runner,
        cluster_notes=config.get("cluster_notes"),
        poll_interval=config.get("poll_interval", 10),
        sandbox=rest_sandbox.Sandbox(config.get("sandbox"), data_dir=rest_data_dir()),
    )

def protected_paths(config, auth):
    paths = [tunnels_data_dir(), config["templates_dir"], config["jobs_db"], config["tokens_file"],
             config["path"], config["proposals_dir"]]
    if config.get("audit_log"):
        paths.append(config["audit_log"])
    if auth.source and not auth.source.startswith("$"):
        paths.append(auth.source)
    return [os.path.expanduser(p) for p in paths]

def main(argv=None, handler_class=HPCRESTHandler):
    opts = parse_args(argv)
    if opts.init_config:
        return init_config(opts)
    config = load_config(opts.config)
    if opts.probe_sandbox:
        sys.exit(0 if probe_sandbox(config) else 1)
    if manage_tokens(opts, config):
        return

    ppid = os.environ.get("PARENT_PROCESS_ID")
    if ppid is not None:
        setup_parent_terminated_listener(ppid)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    auth = TokenAuth.load(opts.token_file, tokens_file=os.path.expanduser(config["tokens_file"]))
    base_dir = opts.base_dir
    if base_dir is None and opts.allow:
        base_dir = os.path.expanduser(opts.allow[0])
    whitelist = PathWhitelist(opts.allow or None, base_dir=base_dir, deny=protected_paths(config, auth))
    jobs = build_jobs(config, opts.command_timeout)
    audit = AuditLog(config["audit_log"]) if config.get("audit_log") else None

    if opts.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {opts.host} exposes this server beyond the local node", file=sys.stderr)
    print(f"hpclib REST server on http://{opts.host}:{opts.port}")
    print(f"  token: {auth.source}; scoped tokens: {config['tokens_file']}")
    print(f"  allowed dirs: {', '.join(whitelist.roots) if whitelist.restricted else 'unrestricted'}")
    print(f"  file changes: {'disabled' if opts.disable_file_changes else 'enabled'}")
    print(f"  templates: {jobs.templates.directory}")
    print(f"  audit log: {config.get('audit_log') or 'off'}")
    sandbox = jobs.sandbox.describe()
    if sandbox["effective"] == "singularity":
        print(f"  job sandbox: {sandbox['runtime_path']}")
    elif sandbox["effective"] == "unavailable":
        print(f"  job sandbox: UNAVAILABLE, template jobs will be refused: {sandbox['error']}", file=sys.stderr)
    else:
        print(f"WARNING: template jobs are not sandboxed ({sandbox['reason']}); GET /sandbox recommends a "
              f"config", file=sys.stderr)
    print(f"  base dir: {whitelist.base_dir}", flush=True)

    try:
        handler_class.start_server(
            opts.host, opts.port,
            auth=auth, whitelist=whitelist,
            command_timeout=opts.command_timeout, max_upload=opts.max_upload,
            disable_file_changes=opts.disable_file_changes,
            jobs=jobs, audit=audit,
        )
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
