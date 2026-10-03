#!/usr/bin/env python3
"""
The Agent Console backend: a small HTTP API on your own machine for a front
end (or curl) to watch and manage the clusters set up with `setup_agents`.

    agent_console                     # http://127.0.0.1:27180, prints the session key
    agent_console --static ui/dist    # also serve a built front end from the same origin
    agent_console --allow-origin http://127.0.0.1:5173   # a front end served elsewhere

It binds 127.0.0.1 only. Every /api call needs `Authorization: Bearer KEY`,
where KEY is made fresh at each launch, printed, and written to
~/.config/hpclib/console/session (mode 600). Cluster tokens never leave this
process: calls to a cluster go through /api/clusters/NAME/rest/<route>, which
adds the profile's owner token (or its agent token, with ?as=agent or when
there is no owner token).

The front end depends only on the routes below; nothing here depends on it.

  GET  /api/health
  GET  /api/clusters                          profiles + tunnel state, no token values
  GET  /api/clusters/NAME
  GET  /api/clusters/NAME/mcp                 the MCP client entry setup_agents wrote
  GET  /api/clusters/NAME/login               the cluster login: state (none, starting, password_sent,
                                              push_sent, connected, expired, failed) and message
  POST /api/clusters/NAME/login               {"password": ...}: log in (password, then a Duo push), keeping
                                              the ssh connection pssh and agent_tunnel reuse
  POST /api/clusters/NAME/logout              close that connection
  POST /api/clusters                          {"host": "user@host", "port"?, "jump"?}: a profile for a new cluster
  POST /api/clusters/NAME/install             install_hpclib over the login ({"force": true} reinstalls)
  POST /api/clusters/NAME/setup               setup_agents ({"work_dirs": [...], "binds": [...],
                                              "templates": "...", "rebuild": false})
  GET  /api/clusters/NAME/operation           the running or last install/setup, with its log
  GET  /api/clusters/NAME/settings            this machine's tunnel settings for the cluster
  PUT  /api/clusters/NAME/settings            {"auto_approve_templates": all|new|review,
                                               "tunnel_args": ["--time=12:00:00", ...],
                                               "connection_hours": 12}
  POST /api/clusters/NAME/tunnel/start        runs agent_tunnel NAME ({"auto_approve_templates": all|new|review})
  POST /api/clusters/NAME/tunnel/stop         runs agent_stop NAME
  GET  /api/clusters/NAME/tunnel/log?lines=N  the console's log of that tunnel
  *    /api/clusters/NAME/rest/<route>        proxied to the cluster's REST server
  GET  /api/jobs?active=1&limit=N             GET /jobs on every live cluster
  GET  /api/proposals                         GET /admin/proposals on every live cluster

Answers are JSON: `{"ok": true, ...}`, or `{"error": "..."}` with an HTTP
error status. Aggregate routes report each cluster under "clusters" as
{"ok": true} or {"error": "..."}, so one dead tunnel doesn't fail the call.
Proxied calls return the cluster's own status and body unchanged.

Standard library only.
"""
import sys

if sys.version_info < (3, 7):
    sys.exit("agent_console needs Python 3.7 or newer")

import argparse
import concurrent.futures
import contextlib
import io
import hmac
import http.server
import json
import mimetypes
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HPCLIB_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HPCLIB_DIR, "lib"))
import agent_profiles  # noqa: E402

VERSION = "0.1"
DEFAULT_PORT = 27180
CLUSTER_NAME_RE = re.compile(r"[A-Za-z0-9._@-]{1,120}")
STREAMED_ROUTES = ("files/content",)   # passed through in chunks rather than read whole
LOGIN_HOST_RE = re.compile(r"[A-Za-z0-9._-]{1,64}@[A-Za-z0-9.-]{1,253}")
JUMP_HOST_RE = re.compile(r"([A-Za-z0-9._-]{1,64}@)?[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?")
CLUSTER_PATH_RE = re.compile(r"/[^\0\n\r]{0,1023}")
TEMPLATES_RE = re.compile(r"all|[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(,[A-Za-z0-9][A-Za-z0-9_.-]{0,63})*")


def local_hpclib_version():
    """HPCLIB_VERSION of the hpclib this console runs from: what install_hpclib would install."""
    try:
        with open(os.path.join(HPCLIB_DIR, "hpclib.sh")) as f:
            m = re.search(r'^HPCLIB_VERSION="([^"]+)"', f.read(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None
REST_ROUTE_RE = re.compile(r"[A-Za-z0-9_./-]{1,256}")
PROFILE_KEYS = ("name", "host", "port", "process_port", "work_dirs", "binds", "local_roots", "token_name",
                "mcp_name", "created", "updated")


class ConsoleError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.payload = dict(error=message, **extra)


def console_dir():
    return os.path.expanduser(os.environ.get("HPCLIB_CONSOLE_DIR") or "~/.config/hpclib/console")


def write_private(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp.")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def read_private(path):
    """A token file's contents, or None if it's missing, empty or readable by others."""
    try:
        info = os.stat(path)
        if info.st_mode & 0o077:
            return None
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


################################################################################
##
##  Clusters: profiles, tokens, tunnels
##

def ssh_options(profile):
    """
    The ssh options from a profile's `login`. setup_agents stores its whole
    login (`[options...] user@host`), so the host itself is dropped here.
    """
    login = list(profile.get("login") or [])
    while login and login[-1] == profile.get("host"):
        login.pop()
    return login


class Logins:
    """
    Cluster logins: an ssh master connection on the socket pssh uses
    (~/.ssh/connections/%r@%h:%p), so agent_tunnel and pssh reuse it with no
    prompts until it has been idle for the cluster's `connection_hours`.

    ssh asks for the password and the second factor through SSH_ASKPASS
    (console_askpass.py), which relays each prompt here. For now the console
    answers a password prompt with the password given to `connect` and a Duo
    prompt with "1" (a push to the phone); any other prompt ends the attempt
    with its text, so it can be handled later. The password is kept in memory
    only until ssh has used it.
    """

    PASSWORD_RE = re.compile(r"pass(word|phrase)", re.I)
    PUSH_RE = re.compile(r"passcode|option|duo|two-factor|second factor", re.I)
    HOST_KEY_RE = re.compile(r"yes/no|fingerprint|authenticity", re.I)
    CONTROL_PATH = "~/.ssh/connections/%r@%h:%p"
    TIMEOUT = 150
    DEFAULT_HOURS = 12

    def __init__(self, ssh="ssh"):
        self.ssh = ssh
        self.attempts = {}      # cluster name -> the latest attempt
        self.by_nonce = {}
        self.lock = threading.Lock()
        self.socket_path = None

    # -- the askpass side ----------------------------------------------------

    def _serve(self):
        """Start the socket console_askpass.py talks to (once)."""
        with self.lock:
            if self.socket_path is not None:
                return self.socket_path
            d = console_dir()
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)
            path = os.path.join(d, f"askpass-{os.getpid()}.sock")
            if os.path.exists(path):
                os.remove(path)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            os.chmod(path, 0o600)
            server.listen(8)
            threading.Thread(target=self._accept, args=(server,), daemon=True).start()
            self.socket_path = path
            return path

    def _accept(self, server):
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn):
        with conn:
            try:
                conn.settimeout(10)
                data = b""
                while not data.endswith(b"\n") and len(data) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data or b"{}")
                reply = self.handle_prompt(request.get("nonce"), str(request.get("prompt", "")))
                conn.sendall(json.dumps(reply).encode() + b"\n")
            except (OSError, ValueError):
                pass

    def handle_prompt(self, nonce, prompt):
        """What to tell ssh for one prompt: {"answer": ...} or {"error": ...}."""
        with self.lock:
            attempt = self.by_nonce.get(nonce) if isinstance(nonce, str) else None
            if attempt is None or attempt["state"] in ("connected", "failed"):
                return {"error": "no login in progress"}
            shown = " ".join(prompt.split())[-300:]
            attempt["prompts"].append(shown)
            if self.HOST_KEY_RE.search(prompt):
                return self._fail(attempt, "ssh doesn't know this host's key yet; connect once in a terminal "
                                           "(ssh " + attempt["host"] + ") to accept it, then try again", shown)
            if self.PASSWORD_RE.search(prompt):
                if attempt["password_used"]:
                    return self._fail(attempt, "the password was not accepted", shown)
                if not attempt["password"]:
                    return self._fail(attempt, "the cluster asks for a password; enter it and connect again", shown)
                answer, attempt["password"], attempt["password_used"] = attempt["password"], None, True
                attempt.update(state="password_sent", message="password sent")
                return {"answer": answer}
            if self.PUSH_RE.search(prompt):
                if attempt["push_sent"]:
                    return self._fail(attempt, "the push was not approved", shown)
                attempt.update(push_sent=True, state="push_sent",
                               message="Duo push sent: approve it on your phone")
                return {"answer": "1"}
            return self._fail(attempt, "the cluster asked something the console doesn't answer yet", shown)

    def _fail(self, attempt, message, prompt=None):
        attempt.update(state="failed", message=message, password=None)
        if prompt:
            attempt["prompt"] = prompt
        return {"error": message}

    # -- ssh -----------------------------------------------------------------

    def _askpass_program(self):
        """A tiny wrapper ssh can run as SSH_ASKPASS (it must be one executable path)."""
        path = os.path.join(console_dir(), "askpass")
        helper = os.path.join(HERE, "console_askpass.py")
        text = f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(helper)} \"$@\"\n"
        try:
            with open(path) as f:
                if f.read() == text:
                    return path
        except OSError:
            pass
        write_private(path, text)
        os.chmod(path, 0o700)
        return path

    def _base(self, profile):
        return [self.ssh] + ssh_options(profile) + ["-o", f"ControlPath={self.CONTROL_PATH}"]

    def alive(self, profile):
        try:
            res = subprocess.run(self._base(profile) + ["-O", "check", profile["host"]], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return res.returncode == 0

    def status(self, profile, check=True):
        with self.lock:
            attempt = dict(self.attempts.get(profile["name"]) or {})
        hours = profile.get("connection_hours") or self.DEFAULT_HOURS
        out = {"cluster": profile["name"], "connection_hours": hours}
        if attempt:
            out.update({k: attempt.get(k) for k in ("state", "message", "started", "finished", "prompt")})
        if attempt.get("state") in ("starting", "password_sent", "push_sent"):
            return out
        if check:
            connected = self.alive(profile)
            out["connected"] = connected
            if connected and out.get("state") != "connected":
                out.update(state="connected", message="logged in (outside the console, or before it started)")
            elif not connected and out.get("state") == "connected":
                out.update(state="expired", message=f"the login has ended (idle for {hours} h, or the network "
                                                    f"changed); log in again")
            elif not connected and not attempt:
                out.update(state="none", message="not logged in")
        return out

    def connect(self, profile, password=None):
        if password is not None and (not isinstance(password, str) or len(password) > 1024 or "\n" in password):
            raise ConsoleError(400, "`password` must be one line of text")
        name = profile["name"]
        with self.lock:
            current = self.attempts.get(name)
            if current and current["state"] in ("starting", "password_sent", "push_sent"):
                raise ConsoleError(409, f"a login to {name} is already in progress")
        if self.alive(profile):
            with self.lock:
                self.attempts[name] = {"state": "connected", "message": "already logged in", "started": time.time(),
                                       "finished": time.time(), "prompts": [], "password": None}
            return self.status(profile, check=False)
        hours = int(profile.get("connection_hours") or self.DEFAULT_HOURS)
        socket_path = self._serve()
        nonce = secrets.token_urlsafe(24)
        attempt = {"state": "starting", "message": "connecting", "started": time.time(), "finished": None,
                   "prompts": [], "password": password or None, "password_used": False, "push_sent": False,
                   "host": profile["host"]}
        with self.lock:
            old = self.attempts.get(name)
            if old is not None:
                self.by_nonce = {k: v for k, v in self.by_nonce.items() if v is not old}
            self.attempts[name] = attempt
            self.by_nonce[nonce] = attempt
        os.makedirs(os.path.expanduser("~/.ssh/connections"), mode=0o700, exist_ok=True)
        env = dict(os.environ, SSH_ASKPASS=self._askpass_program(), SSH_ASKPASS_REQUIRE="force",
                   HPCLIB_ASKPASS_SOCKET=socket_path, HPCLIB_ASKPASS_NONCE=nonce)
        env.setdefault("DISPLAY", ":0")   # older ssh only uses SSH_ASKPASS with a DISPLAY
        command = self._base(profile) + [
            "-M", "-N", "-f", "-o", f"ControlPersist={hours}h", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=4", "-o", "NumberOfPasswordPrompts=1", profile["host"]]
        log_path = os.path.join(console_dir(), "logs", f"{name}.login.log")
        os.makedirs(os.path.dirname(log_path), mode=0o700, exist_ok=True)
        log = open(log_path, "wb")
        os.chmod(log_path, 0o600)
        try:
            # with -f, ssh goes to the background once logged in and this process exits 0
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                    start_new_session=True)
        except OSError as e:
            log.close()
            with self.lock:
                self._fail(attempt, f"could not run ssh: {e}")
            return self.status(profile, check=False)
        threading.Thread(target=self._wait, args=(profile, attempt, proc, log, log_path, nonce), daemon=True).start()
        return self.status(profile, check=False)

    def _wait(self, profile, attempt, proc, log, log_path, nonce):
        try:
            code = proc.wait(self.TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            code = None
        log.close()
        with self.lock:
            self.by_nonce.pop(nonce, None)
            attempt["password"] = None
            attempt["finished"] = time.time()
            if attempt["state"] == "failed":
                return
            if code == 0:
                attempt.update(state="connected", message="logged in")
                return
        try:
            with open(log_path, errors="replace") as f:
                said = [line.strip() for line in f.read().splitlines() if line.strip()][-3:]
        except OSError:
            said = []
        with self.lock:
            reason = "timed out waiting for the login" if code is None else f"ssh exited with status {code}"
            self._fail(attempt, reason + (": " + " / ".join(said) if said else ""))

    def disconnect(self, profile):
        try:
            res = subprocess.run(self._base(profile) + ["-O", "exit", profile["host"]], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ConsoleError(502, f"could not run ssh: {e}")
        with self.lock:
            self.attempts.pop(profile["name"], None)
        return {"logged_out": res.returncode == 0, "detail": (res.stderr or res.stdout).strip()}


class Clusters:
    """The agent profiles, plus the tunnels this console started."""

    def __init__(self, shell_runner=None, timeout=20, logins: Logins = None):
        self.timeout = timeout
        self.shell_runner = shell_runner or self.run_hpclib
        self.procs = {}
        self.lock = threading.Lock()
        self.logins = logins or Logins()
        self.ops = {}

    # profiles
    def names(self):
        return [p["name"] for p in agent_profiles.all_profiles()]

    def profile(self, name):
        if not CLUSTER_NAME_RE.fullmatch(name or ""):
            raise ConsoleError(400, f"invalid cluster name {name!r}")
        profile = agent_profiles.load(name)
        if profile is None:
            found = [p for p in agent_profiles.all_profiles() if name in (p.get("host"), p.get("mcp_name"))]
            if not found:
                raise ConsoleError(404, f"no agent profile {name!r}; run setup_agents for it",
                                   clusters=self.names())
            profile = found[0]
        return profile

    @staticmethod
    def token(profile, as_agent=False):
        """(token, which) for calls to this cluster, owner first unless as_agent."""
        owner = None if as_agent else read_private(profile.get("owner_token_file") or "")
        if owner:
            return owner, "owner"
        agent = read_private(profile.get("token_file") or "")
        if agent:
            return agent, "agent"
        raise ConsoleError(409, f"no usable token for {profile['name']}: the token files are missing or readable "
                                f"by other users; run setup_agents --rebuild")

    def log_path(self, name):
        return os.path.join(console_dir(), "logs", f"{name}.log")

    # tunnels
    @staticmethod
    def port_open(port, timeout=0.5):
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
                return True
        except OSError:
            return False

    def call(self, profile, verb, route, query=None, body=None, headers=None, as_agent=False, timeout=None):
        """(status, content_type, bytes) from the cluster's REST server."""
        token, _ = self.token(profile, as_agent)
        url = f"http://127.0.0.1:{profile['port']}{route}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        hdrs = {"Authorization": f"Bearer {token}"}
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=body, method=verb, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as res:
                return res.status, res.headers.get("Content-Type", ""), res.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()
        except (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout) as e:
            raise ConsoleError(502, f"the tunnel to {profile['name']} is not answering on port {profile['port']} "
                                    f"({getattr(e, 'reason', e)}); start it with POST "
                                    f"/api/clusters/{profile['name']}/tunnel/start")

    def open_stream(self, profile, route, query=None, as_agent=False, timeout=60):
        """A GET on the cluster's REST server as an open response, for streaming a download through."""
        token, _ = self.token(profile, as_agent)
        url = f"http://127.0.0.1:{profile['port']}{route}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            return e
        except (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout) as e:
            raise ConsoleError(502, f"the tunnel to {profile['name']} is not answering on port {profile['port']} "
                                    f"({getattr(e, 'reason', e)})")

    def call_json(self, profile, verb, route, query=None, as_agent=False, timeout=None):
        status, ctype, content = self.call(profile, verb, route, query, as_agent=as_agent, timeout=timeout)
        if "text/html" in ctype:
            raise ConsoleError(503, f"the tunnel to {profile['name']} is up but its REST server job is still "
                                    f"queued or starting")
        try:
            payload = json.loads(content) if content else {}
        except ValueError:
            raise ConsoleError(502, f"{profile['name']} sent a non-JSON answer ({status})")
        if status >= 400:
            raise ConsoleError(502 if status == 401 else status, payload.get("error", f"HTTP {status}"),
                               cluster=profile["name"], cluster_status=status)
        return payload

    def tunnel_state(self, profile):
        """'down' (nothing on the port), 'starting' (the waiting page), 'up', or 'error'."""
        name = profile["name"]
        with self.lock:
            proc = self.procs.get(name)
        out = {"port": profile["port"], "started_here": proc is not None and proc.poll() is None,
               "log": os.path.exists(self.log_path(name))}
        if proc is not None and proc.poll() is not None:
            out["last_exit"] = proc.returncode
        if not self.port_open(profile["port"]):
            out["state"] = "starting" if out["started_here"] else "down"
            return out
        try:
            status, ctype, content = self.call(profile, "GET", "/health", timeout=5)
        except ConsoleError as e:
            return dict(out, state="error", error=e.payload["error"])
        if "text/html" in ctype:
            return dict(out, state="starting")
        try:
            health = json.loads(content)
        except ValueError:
            return dict(out, state="error", error=f"non-JSON health answer ({status})")
        if status >= 400:
            return dict(out, state="error", error=health.get("error", f"HTTP {status}"))
        return dict(out, state="up", server=health.get("server"), hostname=health.get("hostname"),
                    slurm_job_id=health.get("slurm_job_id"), token=(health.get("token") or {}).get("name"),
                    hpclib_version=health.get("hpclib_version"))

    def describe(self, profile, tunnel=True):
        out = {k: profile.get(k) for k in PROFILE_KEYS}
        out["has_owner_token"] = read_private(profile.get("owner_token_file") or "") is not None
        out["has_agent_token"] = read_private(profile.get("token_file") or "") is not None
        out["connection_hours"] = profile.get("connection_hours") or Logins.DEFAULT_HOURS
        out["set_up"] = bool(profile.get("work_dirs")) and out["has_agent_token"]
        with self.lock:
            op = self.ops.get(profile["name"])
            out["operation"] = {k: op[k] for k in ("kind", "state", "exit_code", "started", "finished")} if op else None
        if tunnel:
            out["tunnel"] = self.tunnel_state(profile)
            out["login"] = self.logins.status(profile)
        return out

    def run_hpclib(self, args, log):
        """Start an hpclib shell function (e.g. agent_tunnel NAME) in its own session, logging to `log`."""
        script = 'HPCLIB_DIR="$1"; . "$1/hpclib.sh" > /dev/null 2>&1 || exit 97; shift; "$@"'
        return subprocess.Popen(["bash", "-c", script, "agent_console", HPCLIB_DIR] + list(args),
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True,
                                env=dict(os.environ, HPCLIB_ECHO_COMMANDS="", NO_COLOR="1", HPCLIB_COLOR="never"))

    def _open_log(self, name, title, path=None):
        path = path or self.log_path(name)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        log = open(path, "ab", buffering=0)
        os.chmod(path, 0o600)
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {title} ===\n".encode())
        return log

    # -- installing and setting up ------------------------------------------

    OPERATIONS = ("install", "setup")

    def ops_log_path(self, name):
        return os.path.join(console_dir(), "logs", f"{name}.ops.log")

    def run_operation(self, profile, kind, args):
        """Run install_hpclib or setup_agents for a cluster in the background, over its ssh login."""
        name = profile["name"]
        with self.lock:
            current = self.ops.get(name)
            if current and current["state"] == "running":
                raise ConsoleError(409, f"{current['kind']} is already running for {name}")
        if not self.logins.alive(profile):
            raise ConsoleError(409, f"log in to {name} first: {kind} runs over the console's ssh login, which "
                                    f"can't answer a password or 2FA prompt by itself")
        log = self._open_log(name, " ".join(args), self.ops_log_path(name))
        try:
            proc = self.shell_runner(args, log)
        finally:
            log.close()
        record = {"kind": kind, "command": args, "state": "running", "started": time.time(), "finished": None,
                  "exit_code": None, "pid": proc.pid}
        with self.lock:
            self.ops[name] = record
        threading.Thread(target=self._wait_operation, args=(record, proc), daemon=True).start()
        return dict(record)

    def _wait_operation(self, record, proc):
        code = proc.wait()
        with self.lock:
            record.update(state="succeeded" if code == 0 else "failed", exit_code=code, finished=time.time())

    def operation(self, name, lines=200):
        with self.lock:
            record = dict(self.ops.get(name) or {"state": "none"})
        try:
            with open(self.ops_log_path(name), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - (256 << 10)))
                text = f.read().decode(errors="replace")
            record["log"] = text.splitlines()[-lines:]
        except FileNotFoundError:
            record["log"] = []
        return record

    def install(self, profile, body):
        force = body.get("force", False)
        if not isinstance(force, bool) or set(body) - {"force"}:
            raise ConsoleError(400, "install takes {\"force\": true|false}")
        login = list(profile.get("login") or [])
        if profile.get("host") not in login:
            login.append(profile["host"])
        return self.run_operation(profile, "install", ["install_hpclib"] + (["--force"] if force else []) + login)

    def setup(self, profile, body):
        unknown = set(body) - {"work_dirs", "binds", "templates", "rebuild"}
        if unknown:
            raise ConsoleError(400, f"unknown setup options {sorted(unknown)}")
        args = ["setup_agents"]
        for key, flag in (("work_dirs", "--work-dir"), ("binds", "--bind")):
            values = body.get(key) or []
            if not isinstance(values, list) or not all(isinstance(v, str) and CLUSTER_PATH_RE.fullmatch(v)
                                                       for v in values):
                raise ConsoleError(422, f"{key} must be absolute paths on the cluster")
            for v in values:
                args += [flag, v]
        if not profile.get("work_dirs") and not body.get("work_dirs"):
            raise ConsoleError(422, "a new cluster needs at least one work directory (where agents may write)")
        templates = body.get("templates")
        if templates is not None:
            if not isinstance(templates, str) or not TEMPLATES_RE.fullmatch(templates):
                raise ConsoleError(422, "templates is `all` or a comma-separated list of template names")
            args += ["--templates", templates]
        rebuild = body.get("rebuild", False)
        if not isinstance(rebuild, bool):
            raise ConsoleError(400, "rebuild must be true or false")
        if rebuild:
            args.append("--rebuild")
        return self.run_operation(profile, "setup", args + [profile["name"]])

    def add(self, body):
        """A profile for a new cluster, so it can be logged in to and set up."""
        unknown = set(body) - {"host", "port", "jump"}
        if unknown:
            raise ConsoleError(400, f"unknown fields {sorted(unknown)}")
        host, port, jump = body.get("host"), body.get("port"), body.get("jump")
        if not isinstance(host, str) or not LOGIN_HOST_RE.fullmatch(host):
            raise ConsoleError(422, "host must be user@host, e.g. maboyer@grace.hprc.tamu.edu")
        login = []
        if port not in (None, "", 22):
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ConsoleError(422, "port must be a number from 1 to 65535")
            login += ["-p", str(port)]
        if jump:
            if not isinstance(jump, str) or not JUMP_HOST_RE.fullmatch(jump):
                raise ConsoleError(422, "jump must be [user@]host[:port]")
            login += ["-J", jump]
        name = agent_profiles.name_for(host)
        if agent_profiles.load(name) is not None:
            raise ConsoleError(409, f"there is already a profile {name}")
        with contextlib.redirect_stdout(io.StringIO()):
            agent_profiles.cmd_init(name, host)
        agent_profiles.cmd_set(name, "login=", *[f"login+={a}" for a in login + [host]])
        return self.describe(agent_profiles.load(name), tunnel=False)

    def start_tunnel(self, profile, auto_approve=None, extra=()):
        name = profile["name"]
        with self.lock:
            proc = self.procs.get(name)
            if proc is not None and proc.poll() is None:
                raise ConsoleError(409, f"this console already started the tunnel to {name} (pid {proc.pid})")
            if self.port_open(profile["port"]):
                raise ConsoleError(409, f"something already listens on port {profile['port']} for {name}; "
                                        f"stop it first")
            args = ["agent_tunnel", name]
            if auto_approve in ("new", "all"):   # all is agent_tunnel's default
                args.append(f"--auto-approve-templates={auto_approve}")
            elif auto_approve in ("review", "off"):
                args.append("--review-templates")
            elif auto_approve not in (None, "", False):
                raise ConsoleError(400, "`auto_approve_templates` must be all (the default), new or review")
            args += list(extra)
            log = self._open_log(name, " ".join(args))
            try:
                proc = self.shell_runner(args, log)
            finally:
                log.close()
            self.procs[name] = proc
        return {"started": name, "pid": proc.pid, "command": args}

    def stop_tunnel(self, profile, wait=30):
        name = profile["name"]
        log = self._open_log(name, f"agent_stop {name}")
        try:
            stopper = self.shell_runner(["agent_stop", name], log)
            try:
                code = stopper.wait(wait)
            except subprocess.TimeoutExpired:
                os.killpg(stopper.pid, signal.SIGTERM)
                code = None
        finally:
            log.close()
        with self.lock:
            proc = self.procs.pop(name, None)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        return {"stopped": name, "agent_stop_exit": code}

    def tail_log(self, name, lines=200):
        path = self.log_path(name)
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - (256 << 10)))
                text = f.read().decode(errors="replace")
        except FileNotFoundError:
            return {"cluster": name, "lines": [], "path": path}
        return {"cluster": name, "lines": text.splitlines()[-lines:], "path": path}

    def for_live(self, fn):
        """fn(profile) on every cluster whose tunnel port answers, in parallel."""
        results, status = [], {}
        live = []
        for p in agent_profiles.all_profiles():
            if self.port_open(p["port"]):
                live.append(p)
            else:
                status[p["name"]] = {"error": "tunnel down"}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, len(live)))) as pool:
            futures = {pool.submit(fn, p): p for p in live}
            for fut in concurrent.futures.as_completed(futures):
                name = futures[fut]["name"]
                try:
                    results.extend(fut.result())
                    status[name] = {"ok": True}
                except ConsoleError as e:
                    status[name] = {"error": e.payload["error"]}
        return results, status


################################################################################
##
##  HTTP
##

class ConsoleServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, key, clusters: Clusters, allow_origin=None, static_dir=None,
                 max_body=256 << 20):
        self.key = key
        self.clusters = clusters
        self.allow_origin = allow_origin.rstrip("/") if allow_origin else None
        self.static_dir = os.path.realpath(static_dir) if static_dir else None
        self.max_body = max_body
        super().__init__(address, ConsoleHandler)

    @property
    def port(self):
        return self.server_address[1]


class ConsoleHandler(http.server.BaseHTTPRequestHandler):
    server: ConsoleServer
    server_version = f"hpclib-console/{VERSION}"
    MAX_JSON = 1 << 20

    def log_message(self, format, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")

    def do_GET(self): self.dispatch("GET")
    def do_POST(self): self.dispatch("POST")
    def do_PUT(self): self.dispatch("PUT")
    def do_DELETE(self): self.dispatch("DELETE")

    def do_OPTIONS(self):
        if not self.host_ok():
            return self.send_json(421, {"error": "unexpected Host header"})
        if self.cors_origin() is None:
            return self.send_json(403, {"error": "cross-origin requests are not allowed; see --allow-origin"})
        self.send_response(204)
        self.cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # checks
    def host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def cors_origin(self):
        origin = self.headers.get("Origin")
        if origin and self.server.allow_origin and origin.rstrip("/") == self.server.allow_origin:
            return origin
        return None

    def cors_headers(self):
        origin = self.cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def authorized(self):
        scheme, _, value = (self.headers.get("Authorization") or "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), self.server.key.encode())

    # responses
    def send_json(self, status, payload):
        body = json.dumps(payload).encode() + b"\n"
        self.send_bytes(status, "application/json", body)

    def send_bytes(self, status, ctype, body, cache=False):
        self.send_response(status)
        self.cors_headers()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # request helpers
    def body(self, limit):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ConsoleError(400, "bad Content-Length")
        if length > limit:
            raise ConsoleError(413, f"request body larger than {limit} bytes")
        return self.rfile.read(length) if length > 0 else b""

    def json_body(self):
        raw = self.body(self.MAX_JSON)
        if not raw:
            return {}
        try:
            out = json.loads(raw)
        except ValueError as e:
            raise ConsoleError(400, f"invalid JSON body: {e}")
        if not isinstance(out, dict):
            raise ConsoleError(400, "JSON body must be an object")
        return out

    def flag(self, name):
        return (self.query.get(name, ["0"])[-1]).lower() in ("1", "true", "yes", "on")

    def int_arg(self, name, default, lo, hi):
        try:
            value = int(self.query.get(name, [default])[-1])
        except ValueError:
            raise ConsoleError(400, f"`{name}` must be an integer")
        return max(lo, min(hi, value))

    # dispatch
    def dispatch(self, verb):
        try:
            if not self.host_ok():
                raise ConsoleError(421, "unexpected Host header; use http://127.0.0.1:PORT")
            url = urllib.parse.urlsplit(self.path)
            self.query = urllib.parse.parse_qs(url.query)
            path = url.path
            if not path.startswith("/api/") and path != "/api":
                if verb == "GET" and self.server.static_dir:
                    return self.serve_static(path)
                raise ConsoleError(404, "not found; the API is under /api")
            if not self.authorized():
                raise ConsoleError(401, "missing or wrong session key (Authorization: Bearer KEY; agent_console "
                                        "prints it and writes it to ~/.config/hpclib/console/session)")
            status, payload = self.route(verb, path)
            if payload is not None:
                self.send_json(status, payload)
        except ConsoleError as e:
            self.send_json(e.status, e.payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self.send_json(500, {"error": f"{type(e).__name__}: {e}"})

    def route(self, verb, path):
        parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")[1:]]  # without "api"
        c = self.server.clusters
        if parts == ["health"] and verb == "GET":
            return 200, {"ok": True, "console": VERSION, "clusters": len(c.names()), "pid": os.getpid(),
                         "hpclib_version": local_hpclib_version()}
        if parts == ["clusters"] and verb == "POST":
            return 201, dict(c.add(self.json_body()), ok=True)
        if parts == ["clusters"] and verb == "GET":
            profiles = agent_profiles.all_profiles()
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, len(profiles)))) as pool:
                described = list(pool.map(c.describe, profiles))
            return 200, {"ok": True, "clusters": described}
        if parts == ["jobs"] and verb == "GET":
            return self.all_jobs()
        if parts == ["proposals"] and verb == "GET":
            return self.all_proposals()
        if len(parts) >= 2 and parts[0] == "clusters":
            profile = c.profile(parts[1])
            rest = parts[2:]
            if not rest and verb == "GET":
                return 200, dict(c.describe(profile), ok=True)
            if rest == ["install"] and verb == "POST":
                return 202, dict(c.install(profile, self.json_body()), ok=True)
            if rest == ["setup"] and verb == "POST":
                return 202, dict(c.setup(profile, self.json_body()), ok=True)
            if rest == ["operation"] and verb == "GET":
                return 200, dict(c.operation(profile["name"], self.int_arg("lines", 200, 1, 5000)), ok=True)
            if rest == ["login"] and verb == "GET":
                return 200, dict(c.logins.status(profile), ok=True)
            if rest == ["login"] and verb == "POST":
                return 202, dict(c.logins.connect(profile, self.json_body().get("password")), ok=True)
            if rest == ["logout"] and verb == "POST":
                return 200, dict(c.logins.disconnect(profile), ok=True)
            if rest == ["settings"] and verb == "GET":
                return 200, dict(self.tunnel_settings(profile), ok=True)
            if rest == ["settings"] and verb == "PUT":
                return 200, dict(self.save_tunnel_settings(profile, self.json_body()), ok=True)
            if rest == ["mcp"] and verb == "GET":
                return 200, self.mcp_entry(profile)
            if rest == ["tunnel", "start"] and verb == "POST":
                body = self.json_body()
                return 202, dict(c.start_tunnel(profile, body.get("auto_approve_templates")), ok=True)
            if rest == ["tunnel", "stop"] and verb == "POST":
                return 200, dict(c.stop_tunnel(profile), ok=True)
            if rest == ["tunnel", "log"] and verb == "GET":
                return 200, dict(c.tail_log(profile["name"], self.int_arg("lines", 200, 1, 5000)), ok=True)
            if rest[:1] == ["rest"] and len(rest) > 1:
                return self.proxy(verb, profile, "/".join(rest[1:]))
        raise ConsoleError(404 if verb in ("GET", "POST", "PUT", "DELETE") else 405, f"no route {verb} {path}")

    def proxy(self, verb, profile, route):
        if not REST_ROUTE_RE.fullmatch(route) or ".." in route.split("/"):
            raise ConsoleError(400, f"invalid cluster route {route!r}")
        query = {k: v for k, v in self.query.items() if k != "as"}
        as_agent = self.query.get("as", [""])[-1] == "agent"
        if verb == "GET" and route in STREAMED_ROUTES:
            return self.proxy_stream(profile, route, query, as_agent)
        data = self.body(self.server.max_body) if verb in ("POST", "PUT") else None
        headers = {}
        if self.headers.get("Content-Type"):
            headers["Content-Type"] = self.headers["Content-Type"]
        status, ctype, content = self.server.clusters.call(profile, verb, "/" + route, query, data, headers,
                                                           as_agent=as_agent)
        if "text/html" in ctype:
            raise ConsoleError(503, f"the tunnel to {profile['name']} is up but its REST server job is still "
                                    f"queued or starting")
        if status == 401:  # keep 401 for the console's own key, so a front end can tell them apart
            raise ConsoleError(502, f"{profile['name']} refused the stored token (HTTP 401); it may have been "
                                    f"revoked: rerun setup_agents --rebuild", cluster_status=401)
        self.send_bytes(status, ctype or "application/octet-stream", content)
        return status, None

    def proxy_stream(self, profile, route, query, as_agent):
        """Pass a file download through in chunks, so a large file never sits in memory here."""
        res = self.server.clusters.open_stream(profile, "/" + route, query, as_agent=as_agent)
        with res:
            status = getattr(res, "status", None) or res.code
            ctype = res.headers.get("Content-Type", "")
            if "text/html" in ctype:
                raise ConsoleError(503, f"the tunnel to {profile['name']} is up but its REST server job is still "
                                        f"queued or starting")
            if status == 401:
                raise ConsoleError(502, f"{profile['name']} refused the stored token (HTTP 401)", cluster_status=401)
            if status >= 400:
                self.send_bytes(status, ctype or "application/json", res.read())
                return status, None
            self.send_response(status)
            self.cors_headers()
            self.send_header("Content-Type", ctype or "application/octet-stream")
            for header in ("Content-Length", "Content-Disposition"):
                if res.headers.get(header):
                    self.send_header(header, res.headers[header])
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = res.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)
        return status, None

    def all_jobs(self):
        c = self.server.clusters
        query = {"limit": str(self.int_arg("limit", 100, 1, 1000))}
        if self.flag("active"):
            query["active"] = "1"
        wanted = self.query.get("cluster", [None])[-1]

        def jobs(profile):
            if wanted and wanted != profile["name"]:
                return []
            out = c.call_json(profile, "GET", "/jobs", query)
            return [dict(j, cluster=profile["name"]) for j in out.get("jobs", [])]
        found, status = c.for_live(jobs)
        found.sort(key=lambda j: j.get("submitted") or 0, reverse=True)
        return 200, {"ok": True, "jobs": found, "clusters": status}

    def all_proposals(self):
        c = self.server.clusters

        def proposals(profile):
            if c.token(profile)[1] != "owner":
                raise ConsoleError(409, "no owner token for this cluster")
            out = c.call_json(profile, "GET", "/admin/proposals")
            return [dict(p, cluster=profile["name"], policy=out.get("policy")) for p in out.get("proposals", [])]
        found, status = c.for_live(proposals)
        found.sort(key=lambda p: p.get("proposed") or 0, reverse=True)
        return 200, {"ok": True, "proposals": found, "clusters": status}

    @staticmethod
    def tunnel_settings(profile):
        return {"cluster": profile["name"],
                "auto_approve_templates": profile.get("auto_approve_templates") or "all",
                "tunnel_args": profile.get("tunnel_args") or [],
                "connection_hours": profile.get("connection_hours") or Logins.DEFAULT_HOURS,
                "modes": list(agent_profiles.APPROVE_MODES),
                "tunnel_arg_pattern": agent_profiles.TUNNEL_ARG_RE.pattern,
                "applies": "the next time the tunnel starts"}

    def save_tunnel_settings(self, profile, body):
        unknown = set(body) - {"auto_approve_templates", "tunnel_args", "connection_hours"}
        if unknown:
            raise ConsoleError(400, f"unknown settings {sorted(unknown)}")
        mode = body.get("auto_approve_templates", profile.get("auto_approve_templates") or "all")
        if mode not in agent_profiles.APPROVE_MODES:
            raise ConsoleError(422, f"auto_approve_templates must be one of {list(agent_profiles.APPROVE_MODES)}")
        args = body.get("tunnel_args", profile.get("tunnel_args") or [])
        if not isinstance(args, list) or not all(isinstance(a, str) and agent_profiles.TUNNEL_ARG_RE.fullmatch(a)
                                                 for a in args):
            raise ConsoleError(422, "tunnel_args must be sbatch options like --time=12:00:00 or --mem=2gb "
                                    "(time, mem, partition, account, qos, cpus-per-task, constraint)")
        hours = body.get("connection_hours", profile.get("connection_hours") or Logins.DEFAULT_HOURS)
        if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 168:
            raise ConsoleError(422, "connection_hours must be a whole number of hours from 1 to 168")
        fresh = agent_profiles.load(profile["name"]) or profile
        fresh.update(auto_approve_templates=mode, tunnel_args=args, connection_hours=hours)
        agent_profiles.save(fresh)
        return self.tunnel_settings(fresh)

    @staticmethod
    def mcp_entry(profile):
        path = os.path.join(agent_profiles.profile_dir(profile["name"]), "mcp.json")
        try:
            with open(path) as f:
                entry = json.load(f)
        except (OSError, ValueError):
            raise ConsoleError(404, f"no mcp.json for {profile['name']}; rerun setup_agents")
        return {"ok": True, "mcp": entry, "name": profile.get("mcp_name")}

    def serve_static(self, path):
        root = self.server.static_dir
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        target = os.path.realpath(os.path.join(root, rel))
        if target != root and not target.startswith(root + os.sep):
            raise ConsoleError(404, "not found")
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            target = os.path.join(root, "index.html")  # single-page app routes
            if not os.path.isfile(target):
                raise ConsoleError(404, "not found")
        with open(target, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        self.send_bytes(200, ctype, body, cache=False)


################################################################################
##
##  Command line
##

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=int(os.environ.get("HPCLIB_CONSOLE_PORT") or DEFAULT_PORT),
                   help=f"port on 127.0.0.1 (default {DEFAULT_PORT}, or $HPCLIB_CONSOLE_PORT)")
    p.add_argument("--allow-origin", help="one origin (e.g. http://127.0.0.1:5173) allowed to call the API "
                                          "from a browser; none by default")
    p.add_argument("--static", metavar="DIR", help="also serve a built front end from DIR (same origin)")
    p.add_argument("--key", help="use this session key instead of a fresh one (e.g. for scripts)")
    p.add_argument("--open", action="store_true", help="open the front end in the browser (with --static)")
    opts = p.parse_args(argv)
    if opts.allow_origin and (opts.allow_origin == "*" or not re.fullmatch(r"https?://[^/\s]+/?", opts.allow_origin)):
        p.error("--allow-origin takes one origin such as http://127.0.0.1:5173, never *")
    if opts.static and not os.path.isdir(opts.static):
        p.error(f"--static: {opts.static} is not a directory")
    return opts


def main(argv=None):
    opts = parse_args(argv)
    key = opts.key or secrets.token_urlsafe(32)
    session_file = os.path.join(console_dir(), "session")
    try:
        server = ConsoleServer(("127.0.0.1", opts.port), key, Clusters(), allow_origin=opts.allow_origin,
                               static_dir=opts.static)
    except OSError as e:
        sys.exit(f"agent_console: can't listen on 127.0.0.1:{opts.port} ({e}); is it already running? "
                 f"pick another with --port")
    write_private(session_file, json.dumps({"url": f"http://127.0.0.1:{server.port}", "key": key,
                                            "pid": os.getpid()}) + "\n")
    url = f"http://127.0.0.1:{server.port}"
    print(f"hpclib Agent Console on {url}")
    print(f"  session key: {key}")
    print(f"  (also in {session_file}; curl -H \"Authorization: Bearer $KEY\" {url}/api/clusters)")
    print(f"  clusters: {', '.join(server.clusters.names()) or 'none yet; run setup_agents'}")
    if opts.allow_origin:
        print(f"  browser calls allowed from: {opts.allow_origin}")
    if opts.static:
        print(f"  front end: {url}/#key={key}")
        if opts.open:
            import webbrowser
            webbrowser.open(f"{url}/#key={key}")
    sys.stdout.flush()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            with open(session_file) as f:
                if json.load(f).get("pid") == os.getpid():
                    os.remove(session_file)
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
