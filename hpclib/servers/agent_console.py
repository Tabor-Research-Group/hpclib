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
  POST /api/styles/open                      create console/user.css from default-styles.css if absent,
                                              then ask the OS to open it with its default CSS application
  GET  /api/clusters                          profiles + tunnel state, no token values
  GET  /api/clusters/NAME
  GET  /api/clusters/NAME/mcp                 the MCP client entry setup_agents wrote
  GET  /api/clusters/NAME/login               the cluster login: state (none, starting, password_sent,
                                              push_sent, connected, expired, failed) and message
  POST /api/clusters/NAME/login               {"password": ...}: log in (password, then a Duo push), keeping
                                              the ssh connection pssh and agent_tunnel reuse
  POST /api/clusters/NAME/logout              close that connection
  POST /api/clusters                          {"host": "user@host", "port"?, "jump"?}: a profile for a new cluster
  GET  /api/cluster-presets                   configuration choices from installed packages
  POST /api/cluster-presets/ID               create a profile from a preset: username, work_dir,
                                              account?, tunnel_args?, jump?; no remote action
  POST /api/clusters/NAME/login/answer        {"code", "challenge"}: answer the current MFA code prompt;
                                              codes are used once and never saved
  POST /api/clusters/NAME/install             install_hpclib over the login ({"force": true} reinstalls)
  POST /api/clusters/NAME/setup               setup_agents ({"work_dirs": [...], "binds": [...],
                                              "templates": "...", "rebuild": false})
  GET  /api/clusters/NAME/operation           the running or last install/setup/SSH setup, with its log
  GET  /api/apps                              the console's apps: agents, and tunnel apps (JupyterLab), with
                                              the package each came from (null: hpclib's own)
  GET  /api/packages                          apps and settings installed from packages (console_packages.py)
  POST /api/packages[?dry_run=1]              body: a package zip; checks it (dry_run) or installs it
  DELETE /api/packages/NAME                   removes one (its apps go; what it sent to clusters stays)
  GET  /api/apps/APP                          that app's session on every cluster: state (down, starting,
                                              queued, up), the job's queue status, and the URL to open
  POST /api/apps/APP/NAME/start|stop          launch_tunnel / stop_tunnel for it, over the login
  GET  /api/apps/APP/NAME/log                 its tunnel's output
  GET  /api/apps/APP/NAME/settings, PUT ...   {"tunnel_args": [...], "conda_env": ..., "modules": [...],
                                              "project": "/path/to/uv-or-pixi-project",
                                              "settings": {"VSCODE_CONTAINER": "/path/...", ...}}: the tunnel's
                                              settings, saved on the cluster with the next check/install/start
  PUT  /api/apps/APP/NAME/secrets             {"values": {NAME: value or ""}}: a packaged app's secrets, sent to the
                                              cluster over ssh on standard input (setup_tunnel.sh --secrets); only
                                              whether each is set is kept here (GET .../settings shows it)
  GET  /api/apps/APP/NAME/tokens              a packaged app's own bearer tokens (its `tokens` section), listed on the
                                              cluster by the tunnel's tokens.sh: id, label, scopes, expiry, revoked
  POST /api/apps/APP/NAME/tokens              {"label", "scopes": [...], "days"?, "save_to"?}: make one. The answer has
                                              the token once ("token"), or, with save_to (an absolute path on the
                                              cluster), only that the file holds it: never kept or logged here
  DELETE /api/apps/APP/NAME/tokens/ID         revoke one
  GET  /api/apps/APP/NAME/downloads           what a packaged app has downloaded to the cluster (its `downloads`
                                              section: OpenCode's models), listed by the tunnel's downloads.sh:
                                              {"items": [{"kind", "name", "size", "in_use", "note"}], "note"}
  POST /api/apps/APP/NAME/downloads           {"kind", "name"}: download one, on the login node, as an operation with
                                              its log (GET /api/clusters/NAME/operation), like Install
  POST /api/apps/APP/NAME/downloads/remove    {"kind", "name"}: remove one (downloads.sh refuses what is in use)
  POST /api/apps/APP/NAME/check               is the tunnel installed? (its install.sh --check, on the cluster)
  GET  /api/apps/APP/NAME/instances           a shared app's running instances on the cluster, with owners
  POST /api/apps/APP/NAME/control/ID          one of the session's `controls`, e.g. stop_instance {"job": ...}:
                                              end the job serving a shared app (only yours, only a registered one);
                                              Data transfer's kinit or save_credentials, beside its GUI
  POST /api/apps/APP/NAME/install             its install.sh ({"force": true}: e.g. pull the image again), as
                                              the cluster's operation (GET /api/clusters/NAME/operation)
  GET  /api/clusters/NAME/settings            this machine's tunnel settings for the cluster
  PUT  /api/clusters/NAME/settings            {"auto_approve_templates": all|new|review,
                                               "tunnel_args": ["--time=12:00:00", ...],
                                               "connection_hours": 12, "rest_on": job|login}
  POST /api/clusters/NAME/tunnel/start        runs agent_tunnel NAME ({"auto_approve_templates": all|new|review})
  POST /api/clusters/NAME/ssh/setup           initialize shared-home compute SSH via the active login;
                                              tracked ssh_setup operation, body {}
  POST /api/clusters/NAME/tunnel/stop         runs agent_stop NAME
  GET  /api/clusters/NAME/tunnel/log?lines=N  the console's log of that tunnel
  POST /api/clusters/NAME/tunnel/answer       {"answer": ...}: a password the tunnel's ssh asks for (its
                                              "prompt" in the tunnel state), e.g. the login node's ssh to
                                              the compute node; POST /api/apps/APP/NAME/answer likewise
  GET  /api/prompts                           every tunnel or app session started here that waits for a
                                              password now ({"prompts": [{"cluster", "app", "title", "answer":
                                              its answer route, "prompt"}], "running": N}); local only
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
import base64
from html import unescape as html_unescape
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
import shutil
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
sys.path.insert(0, HERE)
import console_packages  # noqa: E402

VERSION = "0.1"
DEFAULT_PORT = 27180
CLUSTER_NAME_RE = re.compile(r"[A-Za-z0-9._@-]{1,120}")
STREAMED_ROUTES = ("files/content",)   # passed through in chunks rather than read whole

# smbshell gui's line once its GUI serves: HPCLIB_TRANSFER_GUI port=P [path=/x] [user=U pass=P] [query=a=b&c=d]
# (HPCLIB_RCLONE_GUI port=P user=U pass=P from an hpclib before SMB_GUI: rclone's, opened with its login_token)
TRANSFER_GUI_RE = re.compile(r"^HPCLIB_(TRANSFER|RCLONE)_GUI((?: [a-z]+=\S*)+)\s*$", re.M)

# Tunnel apps the console runs besides the agents' REST tunnel, one panel each in the front end. Each is an
# hpclib tunnel (hpclib/tunnels/NAME) started with launch_tunnel over the cluster's ssh login, on ports of its
# own kept in the profile's "apps" section.
# The console's tunnel apps. Each runs `launch_tunnel ... TUNNEL` over the console's login and is reached on a
# port of its own. Optional parts:
#   token_re      the app's access token in the job's log (streamed back over ssh), added to the URL to open
#   password_re   a password it prints there instead, shown next to Open
#   login_user    with password_re: the user name of a basic-auth login; Open signs in with both (user:password@ in
#                 the URL, as for Data transfer's GUI), and the row shows the name in case the page asks again
#   env_settings  JupyterLab's conda environment, modules and uv/pixi project fields
#   settings      [(NAME, label, hint[, choices])]: the tunnel's TUNNEL_SETTINGS (tunnel_config.sh) the page
#                 edits (with choices: a select of those values); they are saved on the cluster
#                 (tunnels/setup_tunnel.sh --save) for its job and install.sh
#   kind "login"  runs on the cluster's login node over the console's ssh login, its port forwarded there,
#                 rather than in a SLURM job (Data transfer: `smbshell gui`)
#   gui_re        the line a login app prints once it serves (HPCLIB_TRANSFER_GUI key=value ...): where to open
#                 it, and the login that gets in (see transfer_gui_url)
#   smb           the row also signs you in to the SMB server for smbshell (TRANSFER_CONTROLS): Kerberos, or a
#                 password saved for sync jobs. These run beside the GUI, never in its place
#   shared        the tunnel attaches to a running instance another job serves (SHARED_INSTANCE), and keeps
#                 the one it starts running after Stop (KEEP_INSTANCE); `instance_label` names that service,
#                 and its row gets a toolbar tool describing the job and a control that ends it
# A session's state carries `tools` (for the row's <hpclib-toolbar>: {"id", "kind": "info"|"select", "label",
# "text" | "options"/"value"/"control"}) and `controls` (for its <hpclib-extra-controls>: {"id", "label",
# "title", "confirm", "args"}), run with POST /api/apps/APP/NAME/control/ID.
# A tunnel with an install.sh can be checked and installed from the page (tunnels/setup_tunnel.sh).
APPS = {
    "jupyter": {
        "title": "JupyterLab",
        "tunnel": "jupyter",
        "open_path": "/lab",
        "health_path": "/api",   # Jupyter answers {"version": ...} here without a token once it runs
        # Jupyter prints its URL with the token into the job's log, which the tunnel streams back over ssh
        "token_re": re.compile(r"[?&]token=([0-9A-Za-z]{16,})"),
        "env_settings": {"modules": "HPCLIB_JUPYTER_MODULES", "project": "HPCLIB_JUPYTER_PROJECT"},
        "settings": [],
    },
    "vscode": {
        "title": "VS Code",
        "tunnel": "vscode",
        "open_path": "/",
        "health_path": "/healthz",   # code-server: {"status": "alive", ...}
        # postconnect.sh prints code-server's config.yaml (its login password) into the session log
        "password_re": re.compile(r"^password:\s*(\S+)\s*$", re.M),
        "settings": [
            ("VSCODE_CONTAINER", "Container image", "where the code-server image is pulled to and run from; "
                                                    "default /scratch/user/USER/vscode.sif"),
            ("VSCODE_ROOT_DIR", "Start directory", "where VS Code opens and keeps its config; default /scratch/user/USER"),
            ("VSCODE_BIND_PATHS", "Directories in the container", "singularity --bind list, e.g. "
                                                                 "/scratch/user/me:/scratch/user/me,/home/me:/home/me"),
        ],
    },
    "transfer": {
        "title": "Data transfer",
        "tunnel": "data-transfer",
        "kind": "login",
        "smb": True,
        "description": "Files between each cluster and an SMB server, with rclone from the data-transfer-tools "
                       "image. Start serves a web GUI on the cluster's login node, through the console's ssh "
                       "login: rclone's by default (remotes smb, the server, and cluster, its files, plus any a "
                       "settings package adds), or the one Settings… → GUI names. Sign in once beside it: "
                       "Kerberos (kinit) where the cluster has it, or a password saved for sync jobs; without "
                       "either, Start asks for the password and keeps it only until Stop. On the cluster: "
                       "smbshell (smbshell --help), and smbshell submit for SLURM jobs.",
        "open_path": "/",
        "health_path": "/",                 # anything that answers (rclone rcd: 401 without its login) is up
        # smbshell gui prints where to open the GUI, and its own random login, once (SMB_GUI picks which GUI)
        "gui_re": TRANSFER_GUI_RE,
        "note": "The SMB password for this session's GUI, on the cluster's login node: it is kept (in rclone's "
                "reversible encoding) in a private in-memory file until you Stop it. Nothing is saved for later.",
        "settings": [
            ("SMB_HOST", "SMB server", "just the host, e.g. files.example.edu; //HOST/SHARE/FOLDER fills in the folder too"),
            ("SMB_ROOT", "Folder", "SHARE/FOLDER that smbshell paths are relative to, e.g. research/our_group"),
            ("SMB_USER", "User name", "on the server, as smbclient --user takes it, e.g. me@example.edu; default your "
                                      "cluster user name"),
            ("SMB_DOMAIN", "Domain", "for a password, the domain if User name has none (rclone's default is "
                                     "WORKGROUP), e.g. example.edu"),
            ("SMB_REALM", "Kerberos realm", "for kinit (the user name without its @domain, in this realm), e.g. "
                                            "AUTH.EXAMPLE.EDU; default krb5.conf's"),
            ("SMB_SPN", "Kerberos name of the server", "if Kerberos says cifs/SERVER is \"not found in Kerberos "
                                                       "database\": smbshell find-spn shows the one it knows"),
            ("SMB_AUTH", "Sign in with", "auto: a Kerberos ticket if you have one, else the saved password",
             ("auto", "kerberos", "password")),
            ("SMB_GUI", "GUI", "what Start opens: rclone (rclone's web GUI), NAME for NAME.gui.sh from a settings "
                               "package, or a program on the cluster (smbshell --help); default rclone"),
            ("SMB_IMAGE", "Image", "where data-transfer-tools.sif is kept; default /scratch/user/USER/images/"
                                   "data-transfer-tools.sif"),
            ("SMB_IMAGE_SOURCE", "Image source", "what Install pulls or builds it from (oras://, docker://, a .def "
                                                 "or a .sif); default docker://ghcr.io/tabor-research-group/"
                                                 "data-transfer-tools:latest"),
        ],
    },
}
# hpclib's own apps; installed packages add theirs to APPS (load_package_apps)
BUILTIN_APPS = dict(APPS)
SETTING_VALUE_RE = re.compile(r"[^\0\n\r]{0,1024}")


def package_store():
    """The apps and settings installed from packages (console_packages), in the console's directory."""
    tunnels = os.path.join(HPCLIB_DIR, "tunnels")
    builtin_tunnels = {d for d in os.listdir(tunnels) if os.path.isdir(os.path.join(tunnels, d))}
    return console_packages.PackageStore(
        os.path.join(console_dir(), "packages"),
        {k: (v["tunnel"], [f[0] for f in v.get("settings", [])]) for k, v in BUILTIN_APPS.items()},
        builtin_tunnels)


def load_package_apps(store=None):
    """APPS: hpclib's own, then each installed package's (as they are now)."""
    store = store or package_store()
    for app_id in [k for k, v in APPS.items() if v.get("package")]:
        del APPS[app_id]
    for app_id, (manifest, app, _) in store.apps().items():
        spec = {"title": app["title"], "tunnel": app["tunnel"], "open_path": app["open_path"],
                "health_path": app["health_path"], "package": manifest["name"],
                "package_version": manifest["version"],
                "settings": [(f["name"], f["label"], f["hint"]) + ((tuple(f["choices"]),) if f["choices"] else ())
                             for f in app["settings"]]}
        if app.get("description"):
            spec["description"] = app["description"]
        if app.get("shared"):
            spec["shared"] = True
            spec["instance_label"] = app.get("instance_label") or "instance"
        if app.get("reinstall"):
            spec["reinstall"] = dict(app["reinstall"])
        if app.get("token_regex"):
            spec["token_re"] = re.compile(app["token_regex"])
        if app.get("password_regex"):   # shown beside Open, with Copy password (VS Code's password_re)
            spec["password_re"] = re.compile(app["password_regex"], re.M)
        if app.get("login_user"):
            spec["login_user"] = app["login_user"]
        if app.get("secrets"):
            spec["secrets"] = [(f["name"], f["label"], f["hint"]) for f in app["secrets"]]
        if app.get("tokens"):
            spec["tokens"] = dict(app["tokens"])
        if app.get("downloads"):
            spec["downloads"] = dict(app["downloads"])
        APPS[app_id] = spec
    return store
SECRETS_STATUS_RE = re.compile(r"^HPCLIB_TUNNEL_SECRETS((?: [A-Z_][A-Z0-9_]*:(?:set|unset))*)\s*$", re.M)
SECRET_VALUE_RE = re.compile(r"[^\0\n\r]{1,4096}")
TOKENS_LINE_RE = re.compile(r"^HPCLIB_TOKENS (\{.*\})\s*$", re.M)
TOKENS_ELSEWHERE_RE = re.compile(r"^HPCLIB_TOKENS_ELSEWHERE (.*)$", re.M)
INSTALL_ELSEWHERE_RE = re.compile(r"^HPCLIB_INSTALL_ELSEWHERE (.*)$", re.M)
DOWNLOADS_LINE_RE = re.compile(r"^HPCLIB_DOWNLOADS (\{.*\})\s*$", re.M)
DOWNLOAD_REMOVED_RE = re.compile(r"^HPCLIB_DOWNLOAD_REMOVED (\{.*\})\s*$", re.M)
TOKEN_CREATED_RE = re.compile(r"^HPCLIB_TOKEN_CREATED (\{.*\})\s*$", re.M)
TOKEN_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@-]{0,63}")
TOKEN_ID_RE = re.compile(r"[A-Za-z0-9]{1,32}")
TOKEN_FILE_RE = re.compile(r"/[A-Za-z0-9_./@+-]{1,1023}")
TUNNEL_STATUS_RE = re.compile(r"^HPCLIB_TUNNEL_STATUS (installed|missing|unknown|nothing) ?(.*)$", re.M)
ATTACHED_RE = re.compile(r"attaching to the running \S+ instance: job (\d+) on (\S+), port (\d+)")
OWN_INSTANCE_RE = re.compile(r"job (\d+) serves \S+ on (\S+), port (\d+)")
SMB_STATUS_RE = re.compile(r"^HPCLIB_SMB_STATUS (\{.*\})\s*$", re.M)
ENDED_INSTANCE_RE = re.compile(r"^hpclib console: ended job (\d+)", re.M)
INSTANCE_LINE_RE = re.compile(r"^HPCLIB_TUNNEL_INSTANCE (\d+) (\S+) (\d+) (\S+) (\S+)$", re.M)
APP_PROJECT_RE = re.compile(r"/[A-Za-z0-9_./@+-]{1,1023}")
APP_MODULE_RE = re.compile(r"[A-Za-z0-9_.+-][A-Za-z0-9_.+/-]{0,127}")
CONDA_ENV_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
SHIM_STATUS_RE = re.compile(r"<h2>Waiting for[^<]*</h2>\s*<p>(.*?)</p>", re.S)
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
PROFILE_KEYS = ("name", "host", "port", "process_port", "work_dirs", "binds", "local_roots", "token_name", "preset",
                "mcp_name", "created", "updated", "setup_config")


class ConsoleError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.payload = dict(error=message, **extra)


def console_dir():
    return os.path.expanduser(os.environ.get("HPCLIB_CONSOLE_DIR") or "~/.config/hpclib/console")


def open_with_default_application(path):
    """Use the OS file association; never choose or look for an editor ourselves."""
    if sys.platform == "win32":
        os.startfile(path)
        return
    command = "/usr/bin/open" if sys.platform == "darwin" else "xdg-open"
    proc = subprocess.Popen([command, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        status = proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        # Some desktop handlers stay running with the application. Leave them running.
        return
    if status:
        raise OSError(f"the OS file opener exited with status {status}")


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

class PtySession:
    """
    A tunnel launcher (agent_tunnel, launch_tunnel) run in a pseudo-terminal, as in a terminal: its ssh, and
    the ssh the login node then makes to the compute node, can ask for a password there. Its output goes to
    the log; a prompt waiting for input is noticed (`prompt`) so the console can ask you, and the answer is
    written to the terminal, where ssh has turned echo off, so it never reaches the log. A Duo prompt gets
    "1" (a push), as at login. Has the parts of Popen the console uses (pid, poll, wait, returncode).
    """

    PROMPT_RE = re.compile(r"(pass(word|phrase|code)|option \(\d|verification code|one-time)[^\n]*:\s*$", re.I)
    # ssh meeting a host it has no key for (the login node's first ssh to a compute node)
    HOST_KEY_RE = re.compile(r"continue connecting \(yes/no(/\[fingerprint\])?\)\?\s*$", re.I)
    HOST_RE = re.compile(r"authenticity of host '([^' ]+)(?: \(([^)]*)\))?' can't be established", re.I)
    FINGERPRINT_RE = re.compile(r"(\w+) key fingerprint is (\S+?)\.?\s*$", re.M)
    PUSH_RE = re.compile(r"passcode|option|duo|two-factor|second factor", re.I)
    DENIED_RE = re.compile(r"permission denied|incorrect|authentication failed", re.I)

    def __init__(self, argv, log_path, env=None):
        import pty
        import termios
        import fcntl
        import struct
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))   # no wrapped lines

        def controlling_terminal():   # what a terminal gives: a session whose terminal is this pty
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        try:
            self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
                                         preexec_fn=controlling_terminal)
        finally:
            os.close(slave)
        self.master = master
        self.log_path = log_path
        self.prompt = None
        self.tail = ""
        self.lock = threading.Lock()
        self.pushes = 0
        threading.Thread(target=self._read, daemon=True).start()

    # Popen's interface, for the console
    pid = property(lambda self: self.proc.pid)
    returncode = property(lambda self: self.proc.returncode)

    def poll(self):
        return self.proc.poll()

    def wait(self, timeout=None):
        return self.proc.wait(timeout)

    def kill(self):
        self.proc.kill()

    def _read(self):
        with open(self.log_path, "ab", buffering=0) as log:
            while True:
                try:
                    data = os.read(self.master, 4096)
                except OSError:      # the launcher, and everything holding the terminal, has exited
                    break
                if not data:
                    break
                log.write(data)
                text = data.decode(errors="replace").replace("\r", "")
                with self.lock:
                    self.tail = (self.tail + text)[-2000:]
                    self._check_prompt()
        with self.lock:
            self.prompt = None
        try:
            os.close(self.master)
        except OSError:
            pass

    def _check_prompt(self):
        last = self.tail.rsplit("\n", 1)[-1]
        if self.HOST_KEY_RE.search(last):
            # yes adds the key to ~/.ssh/known_hosts on the login node, as typing it in a terminal does; asked
            # in the page with the fingerprint, never answered here
            before = self.tail[-1500:]
            host = self.HOST_RE.findall(before)
            key = self.FINGERPRINT_RE.findall(before)
            self.prompt = {"text": last.strip()[-200:], "kind": "hostkey",
                           "host": host[-1][0] if host else None, "address": host[-1][1] if host else None,
                           "key_type": key[-1][0] if key else None, "fingerprint": key[-1][1] if key else None,
                           "retry": False}
            return
        if not self.PROMPT_RE.search(last):
            self.prompt = None          # new output after a prompt: ssh has moved on (or gave up)
            return
        before = self.tail[:-len(last)] if last else self.tail
        if self.PUSH_RE.search(last) and not re.search(r"password", last, re.I):
            # a Duo menu: ask for a push, once per prompt
            self.pushes += 1
            self.prompt = None
            self._write("1")
            return
        host = re.search(r"([\w.-]+@[\w.-]+)'s password", last)
        self.prompt = {"text": last.strip()[-200:], "kind": "password",
                       "host": host.group(1) if host else None,
                       "retry": bool(self.DENIED_RE.search(before[-400:]))}

    def _write(self, text):
        os.write(self.master, (text + "\n").encode())

    def answer(self, text):
        if not isinstance(text, str) or len(text) > 1024 or "\n" in text or "\r" in text:
            raise ConsoleError(400, "the answer must be one line of text")
        with self.lock:
            if self.prompt is None:
                raise ConsoleError(409, "nothing is waiting for an answer")
            if self.prompt["kind"] == "hostkey" and text not in ("yes", "no"):
                raise ConsoleError(400, "a host key question takes yes or no")
            self._write(text)
            self.prompt = None
        return {"answered": True}

    def waiting(self):
        with self.lock:
            if self.prompt is None:
                return None
            out = dict(self.prompt)
            if getattr(self, "note", None) and out["kind"] == "password":
                out["note"] = self.note         # what the password is for, when it isn't a tunnel's second login
            return out


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
    prompt with "1" (a push to the phone). Numeric code prompts wait for the
    authenticated login/answer endpoint. Other prompts end the attempt with
    their text. Passwords and codes stay in memory only until ssh uses them.
    """

    PASSWORD_RE = re.compile(r"pass(word|phrase)", re.I)
    CODE_RE = re.compile(r"(?:TACC token code|verification code|authentication code|security code|token code|one[- ]time(?: password| code)?)\s*:\s*$", re.I)
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
            if self.CODE_RE.search(prompt):
                event = threading.Event()
                challenge = secrets.token_urlsafe(18)
                attempt.update(state="code_required", message="Enter the code from your authenticator or SMS",
                               prompt=shown, challenge=challenge, code_event=event, code_answer=None)
            else:
                event = None
            if event is None:
                return self._answer_standard_prompt(attempt, prompt, shown)
        # The askpass process waits here; the HTTP login/answer request signals it.
        event.wait(self.TIMEOUT)
        with self.lock:
            answer = attempt.pop("code_answer", None)
            attempt.pop("code_event", None)
            attempt.pop("challenge", None)
            if answer is None or attempt["state"] == "failed":
                return self._fail(attempt, "code entry expired or login was stopped", shown)
            attempt.update(state="starting", message="Code sent; completing login", prompt=None)
            return {"answer": answer}

    def answer_code(self, profile, body):
        if set(body) - {"code", "challenge"}:
            raise ConsoleError(400, "code entry takes code and challenge")
        code = body.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6,12}", code):
            raise ConsoleError(422, "enter the numeric MFA code")
        with self.lock:
            attempt = self.attempts.get(profile["name"])
            if not attempt or attempt["state"] != "code_required" or body.get("challenge") != attempt.get("challenge"):
                raise ConsoleError(409, "this code prompt is no longer waiting; refresh the login state")
            attempt["code_answer"] = code
            attempt["state"] = "starting"
            attempt["message"] = "Code sent; completing login"
            attempt["code_event"].set()
        return self.status(profile, check=False)

    def _answer_standard_prompt(self, attempt, prompt, shown):
        """Called with the attempt lock held; password and Duo behavior is unchanged."""
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
        attempt["code_answer"] = None
        if attempt.get("code_event"):
            attempt["code_event"].set()
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
            out.update({k: attempt.get(k) for k in ("state", "message", "started", "finished", "prompt", "challenge")})
        if attempt.get("state") in ("starting", "password_sent", "push_sent", "code_required"):
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
            if current and current["state"] in ("starting", "password_sent", "push_sent", "code_required"):
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
            attempt["code_answer"] = None
            if attempt.get("code_event"):
                attempt["code_event"].set()
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
            attempt = self.attempts.pop(profile["name"], None)
            if attempt:
                self._fail(attempt, "login was stopped")
        return {"logged_out": res.returncode == 0, "detail": (res.stderr or res.stdout).strip()}


class Clusters:
    """The agent profiles, plus the tunnels this console started."""

    def __init__(self, shell_runner=None, timeout=20, logins: Logins = None, tunnel_runner=None):
        self.timeout = timeout
        self.shell_runner = shell_runner or self.run_hpclib
        # tunnels run in a pseudo-terminal (PtySession), so a second login can be answered; a given
        # shell_runner (tests) is used as it is
        if tunnel_runner is None:
            tunnel_runner = self.run_hpclib_pty if shell_runner is None else self._plain_tunnel_runner
        self.tunnel_runner = tunnel_runner
        self.procs = {}
        self.lock = threading.Lock()
        self.logins = logins or Logins()
        self.ops = {}
        self.app_procs = {}
        self.control_procs = {}      # (name, app): a row's control that asks for a password (kinit), beside its GUI
        self.packages = load_package_apps()
        self.secret_runner = self._run_with_input

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
        """
        'down' (nothing on the port), 'starting' (the waiting page, or a tunnel started here that nothing answers
        behind yet), 'up', or 'error'.
        """
        name = profile["name"]
        with self.lock:
            proc = self.procs.get(name)
        out = {"port": profile["port"], "started_here": proc is not None and proc.poll() is None,
               "log": os.path.exists(self.log_path(name)), "prompt": self.prompt_of(proc)}
        if proc is not None and proc.poll() is not None:
            out["last_exit"] = proc.returncode
        if not self.port_open(profile["port"]):
            out["state"] = "starting" if out["started_here"] else "down"
            return out
        try:
            status, ctype, content = self.call(profile, "GET", "/health", timeout=5)
        except ConsoleError as e:
            if out["started_here"]:
                # the forward is open but nothing answers behind it yet: the job is starting, or the login
                # node's ssh to the compute node waits (perhaps for a password)
                return dict(out, state="starting", error="connecting: nothing answers behind the port yet")
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
            out["operation"] = ({k: op.get(k) for k in ("kind", "title", "app", "state", "exit_code", "started",
                                                         "finished")} if op else None)
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

    HPCLIB_ENV = {"HPCLIB_ECHO_COMMANDS": "", "NO_COLOR": "1", "HPCLIB_COLOR": "never"}

    def hpclib_argv(self, args):
        script = 'HPCLIB_DIR="$1"; . "$1/hpclib.sh" > /dev/null 2>&1 || exit 97; shift; "$@"'
        return ["bash", "-c", script, "agent_console", HPCLIB_DIR] + list(args)

    def run_hpclib_pty(self, args, log_path):
        return PtySession(self.hpclib_argv(args), log_path, env=dict(os.environ, TERM="dumb", **self.HPCLIB_ENV))

    def _plain_tunnel_runner(self, args, log_path):
        with open(log_path, "ab", buffering=0) as log:
            return self.shell_runner(args, log)

    def answer_prompt(self, proc, text):
        if not isinstance(proc, PtySession) or proc.poll() is not None:
            raise ConsoleError(409, "nothing is waiting for an answer")
        return proc.answer(text)

    @staticmethod
    def prompt_of(proc):
        return proc.waiting() if isinstance(proc, PtySession) and proc.poll() is None else None

    def app_waiting(self, name, app):
        """The app session's process that waits for an answer now: its control's (kinit), else its own."""
        with self.lock:
            procs = [self.control_procs.get((name, app)), self.app_procs.get((name, app))]
        return next((p for p in procs if self.prompt_of(p)), procs[1])

    def prompts(self):
        """
        Tunnels and app sessions started here that wait for a password now, and how many are still running.
        Local only (no calls to the clusters), so the page can ask often wherever it is.
        """
        with self.lock:
            procs = [(name, None, proc) for name, proc in self.procs.items()]
            procs += [(name, app, proc) for (name, app), proc in self.app_procs.items()]
            procs += [(name, app, proc) for (name, app), proc in self.control_procs.items()]
        waiting, running = [], 0
        for name, app, proc in procs:
            if proc.poll() is not None:
                continue
            running += 1
            prompt = self.prompt_of(proc)
            if prompt:
                quoted = urllib.parse.quote(name, safe="")
                waiting.append({"cluster": name, "app": app,
                                "title": "agent tunnel" if app is None else APPS[app]["title"],
                                "answer": f"clusters/{quoted}/tunnel/answer" if app is None
                                          else f"apps/{app}/{quoted}/answer",
                                "prompt": prompt})
        return {"prompts": waiting, "running": running}

    def _open_log(self, name, title, path=None, mark="==="):
        """
        The log, with a header for what runs next: === for a launch (_last_launch reads from the newest one), ---
        for a command run beside it (a sign-in control), which leaves the launch's lines where they are.
        """
        path = path or self.log_path(name)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        log = open(path, "ab", buffering=0)
        os.chmod(path, 0o600)
        log.write(f"\n{mark} {time.strftime('%Y-%m-%d %H:%M:%S')} {title} {mark}\n".encode())
        return log

    # -- installing and setting up ------------------------------------------

    OPERATIONS = ("install", "setup", "ssh_setup")

    def ops_log_path(self, name):
        return os.path.join(console_dir(), "logs", f"{name}.ops.log")

    def run_operation(self, profile, kind, args, title=None, app=None):
        """Run a cluster setup/installation operation in the background over its ssh login."""
        name = profile["name"]
        with self.lock:
            current = self.ops.get(name)
            if current and current["state"] == "running":
                raise ConsoleError(409, f"{current['kind']} is already running for {name}")
        if not self.logins.alive(profile):
            raise ConsoleError(409, f"log in to {name} first: {kind} runs over the console's ssh login, which "
                                    f"can't answer a password or 2FA prompt by itself")
        log = self._open_log(name, " ".join(args), self.ops_log_path(name))
        log_offset = log.tell()
        try:
            proc = self.shell_runner(args, log)
        finally:
            log.close()
        record = {"kind": kind, "title": title or {"install": "install_hpclib", "setup": "setup_agents"}.get(kind, kind),
                  "app": app, "command": args, "state": "running", "started": time.time(), "finished": None,
                  "exit_code": None, "pid": proc.pid, "log_offset": log_offset}
        with self.lock:
            self.ops[name] = record
        threading.Thread(target=self._wait_operation, args=(record, proc, profile), daemon=True).start()
        return dict(record)

    def _wait_operation(self, record, proc, profile=None):
        code = proc.wait()
        if record["kind"] == "tunnel_install" and profile is not None and code == 3:
            elsewhere = INSTALL_ELSEWHERE_RE.findall(self._operation_text(profile, record))
            if elsewhere:
                with open(self.ops_log_path(profile["name"]), "ab", buffering=0) as log:
                    try:
                        code = self._install_via_rest(profile, record, elsewhere[-1], log)
                    except Exception as e:
                        log.write(f"setup_tunnel: {e}\nHPCLIB_TUNNEL_STATUS missing installation could not continue: {e}\n".encode())
                        code = 1
        if record.get("app") and profile is not None:
            # what the install found, from the end of its log
            try:
                self._record_setup(profile, record["app"], self._operation_text(profile, record))
            except Exception:    # the log or the profile is gone: the next Check says
                pass
        with self.lock:
            record.update(state="succeeded" if code == 0 else "failed", exit_code=code, finished=time.time())

    def _operation_text(self, profile, record):
        """Only this operation's output, so a prior installation cannot supply its status or fallback marker."""
        with open(self.ops_log_path(profile["name"]), "rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(record.get("log_offset", 0), log.tell() - (256 << 10)))
            return log.read().decode(errors="replace")

    INSTALL_POLL_SECONDS = 1

    def _install_via_rest(self, profile, record, reason, log):
        spec = self.app_spec(record["app"])
        needs = f"{spec['title']}'s installation needs the REST server's node ({reason})"
        if self.tunnel_state(profile)["state"] != "up":
            raise ConsoleError(409, f"{needs}: start the agent tunnel first (Agents → Clusters → Start)")
        if not read_private(profile.get("owner_token_file") or ""):
            raise ConsoleError(409, f"{needs}: this console needs its owner token; rerun setup_agents")

        def request(verb, body=None, query=None):
            status, ctype, content = self.call(profile, verb, "/admin/tunnel-install", query=query,
                body=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"} if body is not None else None, timeout=30)
            try:
                out = json.loads(content)
            except ValueError:
                out = None
            if "text/html" in ctype or not isinstance(out, dict):
                raise ConsoleError(503, f"{needs}: the REST server job is still queued or starting")
            if status == 404 and verb == "POST":
                raise ConsoleError(502, f"{needs}: update HPCLIB on the cluster to 0.2.71 or newer, "
                                        "then restart the agent tunnel")
            if status >= 400:
                raise ConsoleError(502, f"{needs}: {out.get('error', f'HTTP {status}')}")
            return out

        log.write(f"Continuing {spec['title']} installation through the REST API: {reason}\n".encode())
        out = request("POST", {"tunnel": spec["tunnel"], "force": "--force" in record["command"]})
        ident = out.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise ConsoleError(502, "the REST server returned no installation ID")
        with self.lock:
            record["rest_install_id"] = ident
            record["rest_node"] = out.get("node")
        log.write(f"Installation {ident} on {out.get('node', 'the REST node')}\n".encode())
        offset = 0
        while True:
            out = request("GET", query={"id": ident, "offset": offset})
            text, following = out.get("output"), out.get("offset")
            if (not isinstance(text, str) or isinstance(following, bool) or not isinstance(following, int) or
                    following < offset or out.get("state") not in ("running", "succeeded", "failed")):
                raise ConsoleError(502, "the REST server returned an invalid installation status")
            log.write(text.encode())
            offset = following
            if out.get("state") != "running" and not out.get("more"):
                code = out.get("exit_code")
                if isinstance(code, bool) or not isinstance(code, int):
                    raise ConsoleError(502, "the REST server returned no installer exit code")
                if code == 3:
                    raise ConsoleError(502, f"{spec['title']} cannot be installed on the REST server's node either; "
                                            "see the installer output above")
                return code
            if not out.get("more"):
                time.sleep(self.INSTALL_POLL_SECONDS)

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

    def setup_ssh(self, profile, body):
        if (profile.get("preset") or {}).get("ssh_keys") == "site-managed":
            raise ConsoleError(409, "this cluster's SSH keys are managed by the site; hpclib key setup is disabled")
        if body:
            raise ConsoleError(422, "SSH setup takes no key paths or private-key contents")
        login = list(profile.get("login") or [])
        if profile.get("host") not in login:
            login.append(profile["host"])
        return self.run_operation(profile, "ssh_setup", ["setup_cluster_ssh"] + login,
                                  title="Set up cluster SSH")

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
        config = profile.get("setup_config")
        if config is not None:
            try:
                config = console_packages.check_cluster_config(config)
            except console_packages.PackageError as e:
                raise ConsoleError(422, str(e)) from e
            path = os.path.join(agent_profiles.profile_dir(profile["name"]), "setup-config.json")
            write_private(path, json.dumps(config, indent=2) + "\n")
            args += ["--config", path]
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

    def preset_specs(self):
        try:
            return self.packages.cluster_presets()
        except console_packages.PackageError as e:
            raise ConsoleError(422, str(e)) from e

    def add_preset(self, preset_id, body):
        spec = self.preset_specs().get(preset_id)
        if spec is None:
            raise ConsoleError(404, "no installed cluster preset with this ID")
        if set(body) - {"username", "work_dir", "account", "tunnel_args", "jump"}:
            raise ConsoleError(400, "preset fields are username, work_dir, account, tunnel_args and jump")
        username = body.get("username")
        work = body.get("work_dir", spec["defaults"].get("work_dir"))
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", username):
            raise ConsoleError(422, "enter the cluster's username")
        if isinstance(work, str):
            work = work.replace("{username}", username)
        if not isinstance(work, str) or not os.path.isabs(work) or len(work) > 1024 or any(c in work for c in "\0\r\n"):
            raise ConsoleError(422, "work_dir must be one absolute path on the cluster")
        args = body.get("tunnel_args", spec["defaults"]["tunnel_args"])
        if not isinstance(args, list) or len(args) > 16 or not all(isinstance(a, str) and agent_profiles.TUNNEL_ARG_RE.fullmatch(a) for a in args):
            raise ConsoleError(422, "invalid tunnel resource options")
        args = list(args)
        account = body.get("account") or ""
        if not isinstance(account, str) or (account and not re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", account)):
            raise ConsoleError(422, "invalid allocation/account name")
        if account:
            args = [a for a in args if not a.startswith("--account=")] + ["--account=" + account]
        def personalize(value):
            if isinstance(value, str):
                return value.replace("{username}", username)
            if isinstance(value, list):
                return [personalize(v) for v in value]
            if isinstance(value, dict):
                return {k: personalize(v) for k, v in value.items()}
            return value
        apps = personalize(spec["defaults"].get("app_settings", {}))
        for app, settings in apps.items():
            if app not in APPS:
                raise ConsoleError(422, f"install app {app} first so its non-secret settings can be checked")
            apps[app] = self.checked_app_settings(APPS[app], {}, settings)
        config = spec["defaults"].get("rest_config")
        if config is not None:
            try:
                config = console_packages.check_cluster_config(personalize(config))
            except console_packages.PackageError as e:
                raise ConsoleError(422, str(e)) from e
        profile = self.add({"host": username + "@" + spec["host"], "port": spec["port"], "jump": body.get("jump")})
        fresh = agent_profiles.load(profile["name"])
        fresh.update(work_dirs=[work], tunnel_args=args, rest_on=spec["defaults"]["rest_on"],
                     connection_hours=spec["defaults"]["connection_hours"],
                     preset={k: spec[k] for k in ("id", "package", "package_version", "title", "description", "documentation_url", "ssh_keys")})
        if apps:
            fresh["apps"] = apps
        if config is not None:
            fresh["setup_config"] = config
        agent_profiles.save(fresh)
        return self.describe(fresh, tunnel=False)

    # -- tunnel apps (JupyterLab, ...) ----------------------------------------

    @staticmethod
    def app_spec(app):
        spec = APPS.get(app)
        if spec is None:
            raise ConsoleError(404, f"no app {app!r}; the console has {sorted(APPS)}")
        return spec

    def app_config(self, profile, app, save=True):
        """The profile's settings for an app, with its own two ports picked the first time."""
        fresh = agent_profiles.load(profile["name"]) or profile
        apps = fresh.setdefault("apps", {})
        conf = apps.setdefault(app, {})
        if not conf.get("port") or not conf.get("process_port"):
            taken = [fresh.get("port"), fresh.get("process_port")]
            taken += [v for c in apps.values() for v in (c.get("port"), c.get("process_port"))]
            conf["port"] = agent_profiles.random_port(taken=taken)
            conf["process_port"] = agent_profiles.random_port(taken=taken + [conf["port"]])
            if save:
                agent_profiles.save(fresh)
        return fresh, conf

    def app_settings(self, profile, app):
        spec = self.app_spec(app)
        _, conf = self.app_config(profile, app)
        return {"app": app, "cluster": profile["name"], "port": conf["port"],
                "tunnel_args": conf.get("tunnel_args") or [],
                "job": spec.get("kind", "tunnel") == "tunnel",    # a SLURM job: its sbatch options apply
                "python_env": "env_settings" in spec,       # JupyterLab's conda environment, modules and project
                "conda_env": conf.get("conda_env"),        # None: the tunnel's default ("default"); "": none
                "modules": conf.get("modules") or [],
                "project": conf.get("project") or "",
                "fields": [{"name": f[0], "label": f[1], "hint": f[2], "choices": list(f[3]) if len(f) > 3 else None}
                           for f in spec.get("settings", [])],
                "settings": dict(conf.get("settings") or {}),
                # what installed packages fill in where you haven't set a value
                "defaults": {k: {"value": v, "package": p} for k, (v, p) in self.packages.defaults(spec["tunnel"]).items()
                             if k in {f[0] for f in spec.get("settings", [])}},
                "package": spec.get("package"),
                # secrets: whether each is set on the cluster (as of the last Check or send), never their values
                "secrets": [{"name": f[0], "label": f[1], "hint": f[2],
                             "set": (conf.get("secrets") or {}).get(f[0])} for f in spec.get("secrets", [])],
                "tokens": spec.get("tokens"),               # the Tokens panel (None: the app has no tokens of its own)
                "installable": self.app_installable(app),
                "applies": "the next time it starts; settings go to the cluster with the next Check, Install or Start"}

    @staticmethod
    def checked_app_settings(spec, conf, body):
        """Shared validation for app settings forms and personalized cluster defaults."""
        try:
            console_packages.check_public_defaults(body, "app settings")
        except console_packages.PackageError as e:
            raise ConsoleError(422, str(e)) from e
        secret_names = {f[0] for f in spec.get("secrets", [])}
        values = body.get("settings") or {}
        if isinstance(values, dict) and set(values) & secret_names:
            raise ConsoleError(422, "secret fields cannot be stored in plaintext app settings")
        conf = json.loads(json.dumps(conf))
        unknown = set(body) - {"tunnel_args", "conda_env", "modules", "project", "settings"}
        if unknown:
            raise ConsoleError(400, f"unknown settings {sorted(unknown)}")
        if "tunnel_args" in body:
            args = body["tunnel_args"]
            if not isinstance(args, list) or not all(isinstance(a, str) and agent_profiles.TUNNEL_ARG_RE.fullmatch(a)
                                                     for a in args):
                raise ConsoleError(422, "tunnel_args must be sbatch options like --time=8:00:00 or --mem=16gb "
                                        "(time, mem, partition, account, qos, cpus-per-task, constraint)")
            conf["tunnel_args"] = args
        if "conda_env" in body:
            env = body["conda_env"]
            if env is not None and (not isinstance(env, str) or (env and not CONDA_ENV_RE.fullmatch(env))):
                raise ConsoleError(422, "conda_env is an environment name, \"\" for none, or null for the default")
            conf["conda_env"] = env
        if "modules" in body:
            mods = body["modules"]
            if not isinstance(mods, list) or not all(isinstance(m, str) and APP_MODULE_RE.fullmatch(m) for m in mods):
                raise ConsoleError(422, "modules must be module names like JupyterLab/4.2.0")
            conf["modules"] = mods
        if "project" in body:
            project = body["project"] or ""
            if project and (not isinstance(project, str) or not APP_PROJECT_RE.fullmatch(project)):
                raise ConsoleError(422, "project is an absolute path on the cluster (letters, digits, _ . / @ + -)")
            conf["project"] = project
        if "settings" in body:
            values = body["settings"]
            fields = {f[0]: f for f in spec.get("settings", [])}
            if not isinstance(values, dict) or set(values) - set(fields):
                raise ConsoleError(422, f"settings must be an object with keys from {sorted(fields)}")
            for k, v in values.items():
                if not isinstance(v, str) or not SETTING_VALUE_RE.fullmatch(v):
                    raise ConsoleError(422, f"{k} must be one line of text (at most 1024 characters)")
                choices = fields[k][3] if len(fields[k]) > 3 else None
                if choices and v.strip() and v.strip() not in choices:
                    raise ConsoleError(422, f"{k} is one of {', '.join(choices)} (or empty for the default)")
            settings = {k: v.strip() for k, v in values.items() if v.strip()}
            host = settings.get("SMB_HOST", "")
            if "SMB_HOST" in fields and ("/" in host or "\\" in host):
                # //HOST/SHARE/FOLDER (or \\HOST\SHARE\FOLDER, as Windows writes it): the host, and the folder
                parts = [x for x in host.replace("\\", "/").split("/") if x]
                settings["SMB_HOST"] = parts[0] if parts else ""
                if len(parts) > 1 and not settings.get("SMB_ROOT"):
                    settings["SMB_ROOT"] = "/".join(parts[1:])
            conf["settings"] = settings
        conf["pushed"] = None              # the cluster's copy is out of date until the next Check, Install or Start
        return conf

    def save_app_settings(self, profile, app, body):
        spec = self.app_spec(app)
        fresh, conf = self.app_config(profile, app, save=False)
        fresh["apps"][app] = self.checked_app_settings(spec, conf, body)
        agent_profiles.save(fresh)
        return self.app_settings(fresh, app)

    # -- what a tunnel needs on the cluster (its settings, and its install.sh) -----------------

    def app_installable(self, app):
        """Whether the app's tunnel has an install.sh (in this machine's hpclib, or the package it came from)."""
        tunnel = APPS[app]["tunnel"]
        if "install.sh" in self.packages.install_files(tunnel):
            return True
        directory = self.packages.tunnel_dir(tunnel) or os.path.join(HPCLIB_DIR, "tunnels", tunnel)
        return os.path.isfile(os.path.join(directory, "install.sh"))

    def app_vars(self, spec, conf):
        """
        The tunnel's settings as environment variables, as saved on the cluster: yours, over the defaults
        installed packages give its fields.
        """
        fields = {f[0] for f in spec.get("settings", [])}
        out = {k: v for k, (v, _) in self.packages.defaults(spec["tunnel"]).items() if k in fields}
        out.update(conf.get("settings") or {})
        if "env_settings" in spec:
            if conf.get("conda_env") is not None:
                out["CONDA_ENVIRONMENT"] = conf["conda_env"]
            if conf.get("modules"):
                out[spec["env_settings"]["modules"]] = ":".join(conf["modules"])
            if conf.get("project"):
                out[spec["env_settings"]["project"]] = conf["project"]
        return out

    def setup_args(self, profile, app, *actions):
        """tunnel_setup over the console's login, saving the app's settings on the cluster first."""
        spec = self.app_spec(app)
        _, conf = self.app_config(profile, app)
        args = ["tunnel_setup"] + ssh_options(profile) + [profile["host"], spec["tunnel"]]
        # what packages give the tunnel (itself, settings files) goes along; and once more after they stop
        # giving anything, to clear what an earlier push left there
        stage, digest = self.package_stage(spec["tunnel"])
        if digest is not None or conf.get("pushed_package"):
            args += ["--push", stage]
        if not spec.get("settings") and "env_settings" not in spec:
            return args + list(actions)            # no settings of its own: the cluster's stay as they are
        for k, v in sorted(self.app_vars(spec, conf).items()):
            args += ["--set", f"{k}={v}"]
        return args + ["--save"] + list(actions)

    # -- packages: apps and settings installed from zip files (console_packages) ---------------------

    def packages_list(self):
        return [{"name": m["name"], "version": m["version"], "description": m["description"],
                 "installed": m.get("installed"), "sha256": m.get("sha256"), "apps": sorted(m["apps"]),
                 "summary": console_packages.summary(m)} for m, _ in self.packages.installed()]

    def package_add(self, data, dry_run=False):
        """Check (dry_run) or install a package zip; its apps are there at once."""
        if not data:
            raise ConsoleError(400, "send the package's zip file as the request body")
        try:
            if dry_run:
                report = self.packages.inspect(data)
            else:
                with self.lock:
                    report = self.packages.install(data)
                    load_package_apps(self.packages)
                    shutil.rmtree(os.path.join(console_dir(), "staging"), ignore_errors=True)
        except console_packages.PackageError as e:
            raise ConsoleError(422, str(e))
        return dict(report, summary=console_packages.summary(report["package"]), installed=not dry_run)

    def package_remove(self, name):
        apps = [a for a, v in APPS.items() if v.get("package") == name]
        with self.lock:
            running = sorted({a for (_, a), p in list(self.app_procs.items()) + list(self.control_procs.items())
                              if a in apps and p.poll() is None})
            if running:
                raise ConsoleError(409, f"stop {', '.join(APPS[a]['title'] for a in running)} first")
            try:
                self.packages.remove(name)
            except console_packages.PackageError as e:
                raise ConsoleError(404, str(e))
            load_package_apps(self.packages)
            shutil.rmtree(os.path.join(console_dir(), "staging"), ignore_errors=True)
        return {"removed": name, "apps": apps,
                "note": "what it sent to clusters stays there; their settings files go with each app's next Check"}

    def send_secrets(self, profile, app, body, wait=120):
        """
        Put an app's secrets on the cluster ({"values": {NAME: value, ...}}; "" removes one). The values go to
        setup_tunnel.sh --secrets on standard input, base64-encoded, over the console's ssh login; the console
        keeps only whether each is set.
        """
        spec = self.app_spec(app)
        names = [f[0] for f in spec.get("secrets", [])]
        values = body.get("values") if isinstance(body, dict) else None
        if not names:
            raise ConsoleError(404, f"{spec['title']} has no secrets")
        if not isinstance(values, dict) or not values or set(body) - {"values"} or set(values) - set(names):
            raise ConsoleError(422, f"send {{\"values\": {{NAME: value}}}} with names from {names}")
        for k, v in values.items():
            if not isinstance(v, str) or (v and not SECRET_VALUE_RE.fullmatch(v)):
                raise ConsoleError(422, f"{k} must be one line of text (at most 4096 characters), or \"\" to remove it")
        if not self.logins.alive(profile):
            raise ConsoleError(409, f"log in to {profile['name']} first: secrets go over the console's ssh login")
        data = "".join(f"{k}={base64.b64encode(v.strip().encode()).decode() if v.strip() else ''}\n"
                       for k, v in sorted(values.items())).encode()
        args = ["tunnel_setup"] + ssh_options(profile) + [profile["host"], spec["tunnel"], "--secrets"]
        code, text = self.secret_runner(args, data, wait)
        found = SECRETS_STATUS_RE.findall(text)
        if code != 0 or not found:
            tail = " ".join(line for line in text.strip().splitlines()[-3:])
            raise ConsoleError(502, f"the cluster didn't take the secrets (exit {code}); update hpclib there "
                                    f"(Agents → Clusters → Update hpclib) if it is older than this console: {tail}")
        self._record_setup(profile, app, text)
        return {"app": app, "cluster": profile["name"], "secrets": self.app_settings(profile, app)["secrets"]}

    # -- an app's own bearer tokens (a package's `tokens` section; see console_packages) --------

    def _tokens_run(self, profile, app, args, wait=120):
        """
        The app's tokens.sh on the cluster's login node, over the console's login; where it can't run there
        (HPCLIB_TOKENS_ELSEWHERE: no Singularity on the login node, say), on the REST server's node through the agent
        tunnel instead (POST /admin/tunnel-tokens, owner only), which must then be up.
        """
        spec = self.app_spec(app)
        if not spec.get("tokens"):
            raise ConsoleError(404, f"{spec['title']} has no tokens to manage")
        text = self._run_setup(profile, ["tunnel_setup"] + ssh_options(profile) +
                               [profile["host"], spec["tunnel"], "--tokens"] + args, wait)
        elsewhere = TOKENS_ELSEWHERE_RE.findall(text)
        if elsewhere:
            text = self._tokens_via_rest(profile, spec, args, elsewhere[-1].strip())
        return spec, text

    def _tokens_via_rest(self, profile, spec, args, reason):
        needs = (f"{spec['title']}'s tokens can't be managed on {profile['name']}'s login node ({reason}), so they "
                 f"are managed where its REST server runs, through the agent tunnel")
        if self.tunnel_state(profile)["state"] != "up":
            raise ConsoleError(409, f"{needs}: start the agent tunnel first (Agents → Clusters → Start)")
        if not read_private(profile.get("owner_token_file") or ""):
            raise ConsoleError(409, f"{needs}, with the owner token, which this console doesn't have for it: rerun "
                                    f"setup_agents")
        status, ctype, content = self.call(profile, "POST", "/admin/tunnel-tokens", body=json.dumps(
            {"tunnel": spec["tunnel"], "action": args[0], "args": args[1:]}).encode(),
            headers={"Content-Type": "application/json"}, timeout=240)
        try:
            out = json.loads(content)
        except ValueError:
            out = None
        if "text/html" in ctype or not isinstance(out, dict):
            raise ConsoleError(503, f"{needs}: its REST server job is still queued or starting")
        if status == 404:
            raise ConsoleError(502, f"{needs}: update hpclib there (0.2.70 or newer), then restart the agent tunnel")
        if status >= 400:
            raise ConsoleError(502, f"{needs}: {out.get('error', f'HTTP {status}')}")
        text = str(out.get("stdout", "")) + "\n" + str(out.get("stderr", ""))
        if TOKENS_ELSEWHERE_RE.search(text):
            raise ConsoleError(502, f"{needs}, but it can't run on {out.get('node', 'that node')} either: "
                                    f"{TOKENS_ELSEWHERE_RE.findall(text)[-1].strip()}")
        return text

    @staticmethod
    def _tokens_failure(what, text, quote=True):
        """Why it failed, from setup_tunnel's own message or (quote) the last lines; a create never quotes the
        output, since that could hold the token."""
        reason = next((l.split(": ", 1)[1] for l in text.splitlines() if l.startswith("setup_tunnel: ")),
                      " ".join(l for l in text.strip().splitlines()[-2:]) if quote else "see the tunnel's tokens.sh")
        return ConsoleError(502, f"{what}: {reason}")

    @staticmethod
    def _token_record(rec):
        """The fields of a token the panel shows, nothing else (never a hash or a secret)."""
        if not isinstance(rec, dict):
            return None
        scopes = rec.get("scopes")
        out = {"id": str(rec.get("id", ""))[:32], "label": str(rec.get("label") or "")[:64],
               "scopes": [str(x)[:20] for x in scopes] if isinstance(scopes, list) else [],
               "created": rec.get("created") if isinstance(rec.get("created"), (int, float)) else None,
               "expires": rec.get("expires") if isinstance(rec.get("expires"), (int, float)) else None,
               "revoked": rec.get("revoked") is True}
        return out if TOKEN_ID_RE.fullmatch(out["id"]) else None

    def app_tokens(self, profile, app):
        """The app's tokens on the cluster, from its tokens.sh."""
        spec, text = self._tokens_run(profile, app, ["list"])
        found = TOKENS_LINE_RE.findall(text)
        if not found:
            raise self._tokens_failure("couldn't list the tokens", text)
        try:
            records = json.loads(found[-1]).get("tokens", [])
        except (ValueError, AttributeError):
            raise ConsoleError(502, "the tunnel's tokens.sh didn't print valid JSON")
        tokens = [t for t in map(self._token_record, records if isinstance(records, list) else []) if t]
        return {"app": app, "cluster": profile["name"], "tokens": tokens}

    def create_token(self, profile, app, body):
        """
        Make a token ({"label", "scopes", "days"?, "save_to"?}). The secret comes back once, in this answer, or goes
        into the cluster file `save_to` and doesn't come back at all. It is neither stored nor logged here.
        """
        spec = self.app_spec(app)
        conf = spec.get("tokens")
        if not conf:
            raise ConsoleError(404, f"{spec['title']} has no tokens to manage")
        if not isinstance(body, dict) or set(body) - {"label", "scopes", "days", "save_to"}:
            raise ConsoleError(400, "send {\"label\", \"scopes\", \"days\"?, \"save_to\"?}")
        label = body.get("label")
        scopes = body["scopes"] if "scopes" in body else [conf["default_scope"]]
        if not isinstance(label, str) or not TOKEN_LABEL_RE.fullmatch(label):
            raise ConsoleError(422, "label: letters, digits, space and . _ @ - (at most 64), starting with a letter or digit")
        if (not isinstance(scopes, list) or not scopes or len(set(scopes)) != len(scopes) or
                not all(x in conf["scopes"] for x in scopes)):
            raise ConsoleError(422, f"scopes: some of {conf['scopes']}")
        days = body.get("days", conf["days"])
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 3650:
            raise ConsoleError(422, "days: a whole number, 0 for a token that never expires")
        save_to = body.get("save_to") or None
        if save_to is not None and (not isinstance(save_to, str) or not TOKEN_FILE_RE.fullmatch(save_to) or
                                    ".." in save_to.split("/") or save_to.endswith("/")):
            raise ConsoleError(422, "save_to: an absolute path of a new file on the cluster (letters, digits, _ . / @ + -)")
        args = ["create", "--label", label, "--scopes", ",".join(scopes), "--days", str(days)]
        if save_to:
            args += ["--save-to", save_to]
        _, text = self._tokens_run(profile, app, args)
        found = TOKEN_CREATED_RE.findall(text)
        if not found:
            raise self._tokens_failure("the token wasn't made", text, quote=False)
        try:
            made = json.loads(found[-1])
        except ValueError:
            raise ConsoleError(502, "the tunnel's tokens.sh didn't print valid JSON")
        rec = self._token_record(dict(made, label=made.get("label", label), scopes=made.get("scopes", scopes)))
        if rec is None:
            raise ConsoleError(502, "the tunnel's tokens.sh answered without a token id")
        out = {"app": app, "cluster": profile["name"], "token": None, "saved_to": None, "record": rec}
        if isinstance(made.get("token"), str) and made["token"]:
            out["token"] = made["token"]
        elif isinstance(made.get("saved_to"), str):
            out["saved_to"] = made["saved_to"]
        else:
            raise ConsoleError(502, "the tunnel's tokens.sh printed neither a token nor a file")
        return out

    def revoke_token(self, profile, app, ident):
        if not TOKEN_ID_RE.fullmatch(ident or ""):
            raise ConsoleError(400, "a token id is letters and digits")
        _, text = self._tokens_run(profile, app, ["revoke", ident])
        if f"HPCLIB_TOKEN_REVOKED {ident}" not in text:
            raise self._tokens_failure(f"token {ident} wasn't revoked", text)
        return {"app": app, "cluster": profile["name"], "revoked": ident}

    # -- what an app downloads to the cluster (a package's `downloads` section: OpenCode's models) ----------------

    def _downloads_spec(self, app):
        spec = self.app_spec(app)
        if not spec.get("downloads"):
            raise ConsoleError(404, f"{spec['title']} has nothing to download")
        return spec

    def _download_target(self, spec, body):
        """(kind, name) from {"kind", "name"}, checked against the app's kinds."""
        if not isinstance(body, dict) or set(body) != {"kind", "name"}:
            raise ConsoleError(400, "send {\"kind\", \"name\"}")
        kinds = {k["id"]: k for k in spec["downloads"]["kinds"]}
        kind = kinds.get(body["kind"]) if isinstance(body["kind"], str) else None
        if kind is None:
            raise ConsoleError(422, f"kind: one of {sorted(kinds)}")
        name = body["name"].strip() if isinstance(body["name"], str) else None
        if not console_packages.download_name_ok(kind, name):
            hint = f", e.g. {kind['placeholder']}" if kind["placeholder"] else ""
            raise ConsoleError(422, f"name: not a {kind['label']} name{hint}")
        return kind["id"], name

    @staticmethod
    def _download_record(rec, kinds):
        if not isinstance(rec, dict) or rec.get("kind") not in kinds or not isinstance(rec.get("name"), str):
            return None
        size = rec.get("size")
        return {"kind": rec["kind"], "name": rec["name"][:200],
                "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
                "in_use": rec.get("in_use") is True, "note": str(rec.get("note") or "")[:200]}

    def app_downloads(self, profile, app):
        """What the app has downloaded to the cluster, from its downloads.sh."""
        spec = self._downloads_spec(app)
        if not self.logins.alive(profile):
            raise ConsoleError(409, f"log in to {profile['name']} first: the list comes over the console's ssh login")
        text = self._run_setup(profile, self.setup_args(profile, app, "--downloads", "list"), 180)
        found = DOWNLOADS_LINE_RE.findall(text)
        if not found:
            raise self._tokens_failure("couldn't list the downloads", text)
        try:
            listing = json.loads(found[-1])
        except ValueError:
            raise ConsoleError(502, "the tunnel's downloads.sh didn't print valid JSON")
        kinds = {k["id"] for k in spec["downloads"]["kinds"]}
        records = listing.get("items") if isinstance(listing, dict) else None
        items = [r for r in map(lambda r: self._download_record(r, kinds), records if isinstance(records, list) else [])
                 if r]
        return {"app": app, "cluster": profile["name"], "items": items,
                "note": str(listing.get("note") or "")[:400] if isinstance(listing, dict) else ""}

    def app_download(self, profile, app, body):
        """Download one on the cluster's login node: an operation with its log (it can take hours), then a Check."""
        spec = self._downloads_spec(app)
        kind, name = self._download_target(spec, body)
        args = self.setup_args(profile, app, "--check", "--downloads", "pull", kind, name)
        return self.run_operation(profile, "download", args, title=f"download {name}", app=app)

    def app_download_remove(self, profile, app, body):
        spec = self._downloads_spec(app)
        kind, name = self._download_target(spec, body)
        if not self.logins.alive(profile):
            raise ConsoleError(409, f"log in to {profile['name']} first")
        with self.lock:
            op = self.ops.get(profile["name"])
            if op and op["state"] == "running":
                raise ConsoleError(409, f"{op['title']} is running on {profile['name']}; remove it when that is done")
        text = self._run_setup(profile, self.setup_args(profile, app, "--downloads", "remove", kind, name), 300)
        if not DOWNLOAD_REMOVED_RE.findall(text):
            raise self._tokens_failure(f"{name} wasn't removed", text)
        return {"app": app, "cluster": profile["name"], "removed": {"kind": kind, "name": name}}

    def _run_with_input(self, args, data, wait):
        """An hpclib shell function with `data` on its standard input; (exit code, output). Nothing is logged."""
        try:
            res = subprocess.run(self.hpclib_argv(args), input=data, capture_output=True, timeout=wait,
                                 env=dict(os.environ, **self.HPCLIB_ENV), start_new_session=True)
        except subprocess.TimeoutExpired:
            raise ConsoleError(504, f"sending the secrets took over {wait} s")
        return res.returncode, (res.stdout + res.stderr).decode(errors="replace")

    def package_stage(self, tunnel):
        """(directory, hash) of what the packages send a cluster for `tunnel` (see PackageStore.stage)."""
        return self.packages.stage(tunnel, os.path.join(console_dir(), "staging"))

    def _record_setup(self, profile, app, text):
        """Note what a tunnel_setup run said: the settings it saved, and the install status it found."""
        fresh, conf = self.app_config(profile, app, save=False)
        if "setting(s) for" in text:
            conf["pushed"] = time.time()
        secrets = SECRETS_STATUS_RE.findall(text)
        if secrets:
            conf["secrets"] = {n: state == "set" for n, state in re.findall(r"([A-Z_][A-Z0-9_]*):(set|unset)",
                                                                             secrets[-1])}
        found = TUNNEL_STATUS_RE.findall(text)
        if found:
            # it ran, so what was sent with it (if anything) arrived
            conf["pushed_package"] = self.package_stage(self.app_spec(app)["tunnel"])[1]
            state, message = found[-1]
            conf["install"] = {"state": state, "message": message.strip(), "checked": time.time()}
        agent_profiles.save(fresh)
        return conf.get("install")

    def app_check(self, profile, app, wait=180):
        """Ask the cluster whether the app's tunnel is installed (saving its settings there on the way)."""
        spec = self.app_spec(app)
        if not self.logins.alive(profile):
            raise ConsoleError(409, f"log in to {profile['name']} first: the check runs over the console's ssh login")
        args = self.setup_args(profile, app, "--check")
        with tempfile.TemporaryFile() as out:
            proc = self.shell_runner(args, out)
            try:
                code = proc.wait(wait)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                raise ConsoleError(504, f"checking {spec['title']} on {profile['name']} took over {wait} s")
            out.seek(0)
            text = out.read().decode(errors="replace")
        status = self._record_setup(profile, app, text)
        if not status:
            tail = " ".join(text.strip().splitlines()[-3:])
            raise ConsoleError(502, f"no answer from the cluster's setup_tunnel.sh (exit {code}); update hpclib there "
                                    f"(Agents → Clusters → Update hpclib) if it is older than this console: {tail}")
        return status

    def app_install(self, profile, app, body):
        """The tunnel's install.sh on the cluster, as an operation with its log (like install_hpclib)."""
        spec = self.app_spec(app)
        force = body.get("force", False)
        if not isinstance(force, bool) or set(body) - {"force"}:
            raise ConsoleError(400, "install takes {\"force\": true|false}")
        if not self.app_installable(app):
            raise ConsoleError(409, f"{spec['title']} has nothing to install")
        args = self.setup_args(profile, app, "--install", *(["--force"] if force else []), "--check")
        verb = (spec.get("reinstall") or {}).get("label", "install").lower() if force else "install"
        record = self.run_operation(profile, "tunnel_install", args, title=f"{verb} {spec['title']}", app=app)
        return record

    def app_log_path(self, name, app):
        return os.path.join(console_dir(), "logs", f"{name}.{app}.log")

    def _last_launch(self, name, app):
        """The app's log since its newest launch."""
        try:
            with open(self.app_log_path(name, app), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - (512 << 10)))
                text = f.read().decode(errors="replace")
        except FileNotFoundError:
            return ""
        return text.rsplit("\n=== ", 1)[-1].replace("\r", "")

    def _app_token(self, name, app, key="token_re"):
        """The token (or password) from the newest launch in the app's log, if the app printed one yet."""
        pattern = APPS[app].get(key)
        if pattern is None:
            return None
        found = pattern.findall(self._last_launch(name, app))
        return found[-1] if found else None

    def _app_instance(self, name, app):
        """For a shared app: the job serving it, and whether this tunnel attached to it or started it."""
        text = self._last_launch(name, app)
        ended = set(ENDED_INSTANCE_RE.findall(text))
        for pattern, how in ((ATTACHED_RE, "attached"), (OWN_INSTANCE_RE, "started")):
            found = pattern.findall(text)
            if found:
                job, node, port = found[-1]
                if job in ended:
                    return None
                return {"job": job, "node": node, "port": int(port), "how": how}
        return None

    def app_extras(self, spec, state):
        """The row's toolbar tools and extra controls (see APPS)."""
        if spec.get("smb"):
            return self.transfer_extras(state)
        tools, controls = [], []
        inst = state.get("instance")
        if spec.get("shared"):
            what = spec.get("instance_label", "instance")
            if inst:
                tools.append({"id": "instance", "kind": "info", "label": what.capitalize(),
                              "text": f"job {inst['job']} on {inst['node']}, port {inst['port']}; " +
                                      ("this tunnel started it, and it keeps running after Stop"
                                       if inst["how"] == "started" else
                                       "another tunnel started it; Stop only disconnects")})
                controls.append({"id": "stop_instance", "label": f"End {what} job",
                                 "title": f"scancel {inst['job']}: ends the {what} for every tunnel using it",
                                 "confirm": f"End job {inst['job']}, the {what} on {inst['node']}? Every tunnel "
                                            f"connected to it loses it, and this tunnel stops too.",
                                 "args": {"job": inst["job"]}})
            else:
                tools.append({"id": "instance", "kind": "info", "label": what.capitalize(),
                              "text": f"none seen yet: Start connects to a running {what} job, or starts one"})
        return tools, controls

    # -- Data transfer (smbshell): signing in to the SMB server, beside its GUI ----------------------

    TRANSFER_CONTROLS = {
        # id: (smbshell command, runs in a terminal that may ask for a password, note for the password dialog)
        "status": ("status", False, None),
        "kinit": ("login", True, "Kerberos sign-in for the SMB server (kinit, on the cluster): your password "
                                  "goes to kinit, which keeps a ticket on the cluster; nothing is stored here."),
        "kdestroy": ("logout", False, None),
        "save_credentials": ("save-credentials", True,
                             "The SMB server's password, saved on the cluster (mode 600, in rclone's reversible "
                             "encoding) so sync jobs can sign in without you: there is no Kerberos there."),
        "forget_credentials": ("forget-credentials", False, None),
    }

    def transfer_extras(self, state):
        auth = state.get("auth")
        tools, controls = [], []
        if not auth:
            tools.append({"id": "signin", "kind": "info", "label": "Signing in",
                          "text": "not checked yet" + ("" if state.get("install", {}).get("state") == "installed"
                                                       else "; install the image first")})
            controls.append({"id": "status", "label": "Check sign-in"})
            return tools, controls
        tools.append({"id": "server", "kind": "info", "label": "Server",
                      "text": f"{auth['host']} as {auth.get('user')}" if auth.get("host")
                              else "not set: SMB server in Settings…"})
        if auth.get("host"):
            tools.append({"id": "folder", "kind": "info", "label": "Folder",
                          "text": auth.get("root") or "none: smbshell paths are SHARE/PATH"})
        kerberos = bool(auth.get("kinit")) and auth.get("rclone_kerberos")
        ticket = auth.get("ticket")
        by_address = bool(re.fullmatch(r"[0-9.]+|.*:.*", auth.get("host") or ""))
        if kerberos and by_address:
            # tickets are for cifs/NAME: an address can't be signed in to with Kerberos
            text = ("the SMB server is an address, so Kerberos can't be used (it needs the server's DNS name); "
                    + ("a saved password signs in" if auth.get("credentials") else "save a password, or use its name"))
            tools.append({"id": "signin", "kind": "info", "label": "Signing in", "text": text})
            controls.append({"id": "forget_credentials", "label": "Forget saved password",
                             "confirm": "Remove the password saved on the cluster?"} if auth.get("credentials")
                            else {"id": "save_credentials", "label": "Save password for sync jobs", "primary": True})
            if ticket:
                controls.append({"id": "kdestroy", "label": "Kerberos log out"})
            controls.append({"id": "status", "label": "Refresh"})
            return tools, controls
        if ticket:
            text = f"Kerberos ticket for {ticket.get('principal')}, until {ticket.get('expires')}"
        elif auth.get("credentials"):
            text = "a saved password (used by sync jobs, and by smbshell when there is no ticket)"
        elif kerberos:
            text = "kinit is available: sign in with Kerberos"
        elif auth.get("kinit"):
            text = "kinit is available, but this image's rclone has no Kerberos: save a password for jobs"
        else:
            text = "no kinit here: save a password for sync jobs (smbshell asks for it otherwise)"
        tools.append({"id": "signin", "kind": "info", "label": "Signing in", "text": text})
        if kerberos:
            controls.append({"id": "kdestroy", "label": "Kerberos log out"} if ticket else
                            {"id": "kinit", "label": "Kerberos log in (kinit)", "primary": True})
        if not kerberos or auth.get("credentials"):
            controls.append({"id": "forget_credentials", "label": "Forget saved password",
                             "confirm": "Remove the password saved on the cluster? Sync jobs can't sign in "
                                        "without it (or a Kerberos ticket)."} if auth.get("credentials") else
                            {"id": "save_credentials", "label": "Save password for sync jobs"})
        controls.append({"id": "status", "label": "Refresh"})
        return tools, controls

    def transfer_control(self, profile, app, control):
        spec = self.app_spec(app)
        if control not in self.TRANSFER_CONTROLS:
            raise ConsoleError(404, f"{spec['title']} has no control {control!r}")
        command, interactive, note = self.TRANSFER_CONTROLS[control]
        name = profile["name"]
        _, conf = self.app_config(profile, app)
        if not conf.get("pushed"):          # settings saved since: the cluster's copy first
            self._record_setup(profile, app, self._run_setup(profile, self.setup_args(profile, app), 120))
        args = ["smbshell", "--on"] + ssh_options(profile) + [profile["host"]]
        if not interactive:
            text = self._run_setup(profile, args + ([command, "--json"] if command == "status" else [command]), 120)
            if command != "status":
                text = self._run_setup(profile, args + ["status", "--json"], 120)
            return {"auth": self._record_auth(profile, app, text), "message": "Done."}
        if not self.logins.alive(profile):
            raise ConsoleError(409, f"log in to {name} first: this runs over the console's ssh login")
        with self.lock:
            proc = self.control_procs.get((name, app))
            if proc is not None and proc.poll() is None:
                raise ConsoleError(409, f"{spec['title']} on {name} is already signing in")
        self._open_log(name, " ".join(args + [command]), self.app_log_path(name, app), mark="---").close()
        proc = self.tunnel_runner(args + [command], self.app_log_path(name, app))
        proc.note = note
        with self.lock:
            self.control_procs[(name, app)] = proc

        def then():                                   # once it's done, what the cluster says now
            proc.wait()
            try:
                self._record_auth(profile, app, self._run_setup(profile, args + ["status", "--json"], 120))
            except Exception:
                pass
        threading.Thread(target=then, daemon=True).start()
        return {"started": command, "message": "Started; answer the password dialog."}

    def _record_auth(self, profile, app, text):
        found = SMB_STATUS_RE.findall(text)
        if not found:
            tail = " ".join(text.strip().splitlines()[-2:])
            raise ConsoleError(502, f"no answer from smbshell on {profile['name']} (update hpclib there if it is "
                                    f"older than this console): {tail}")
        auth = json.loads(found[-1])
        fresh, conf = self.app_config(profile, app, save=False)
        conf["auth"] = dict(auth, checked=time.time())
        agent_profiles.save(fresh)
        return conf["auth"]

    def app_instances(self, profile, app, wait=60):
        """The shared app's registered running instances on the cluster, with their owners."""
        spec = self.app_spec(app)
        text = self._run_setup(profile, ["tunnel_setup"] + ssh_options(profile) +
                               [profile["host"], spec["tunnel"], "--instances"], wait)
        return [{"job": j, "node": n, "port": int(p), "state": st, "owner": u}
                for j, n, p, st, u in INSTANCE_LINE_RE.findall(text)]

    def _run_setup(self, profile, args, wait):
        if not self.logins.alive(profile):
            raise ConsoleError(409, f"log in to {profile['name']} first: this runs over the console's ssh login")
        with tempfile.TemporaryFile() as out:
            proc = self.shell_runner(args, out)
            try:
                code = proc.wait(wait)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                raise ConsoleError(504, f"no answer from {profile['name']} within {wait} s")
            out.seek(0)
            text = out.read().decode(errors="replace")
        return text if code == 0 else text + f"\n(exit {code})"

    def app_control(self, profile, app, control, body):
        """One of the row's extra controls."""
        spec = self.app_spec(app)
        if spec.get("smb"):
            return self.transfer_control(profile, app, control)
        if control != "stop_instance" or not spec.get("shared"):
            raise ConsoleError(404, f"{spec['title']} has no control {control!r}")
        job = str(body.get("job") or "")
        if not job.isdigit() or set(body) - {"job"}:
            raise ConsoleError(400, "stop_instance takes {\"job\": \"JOB ID\"}")
        text = self._run_setup(profile, ["tunnel_setup"] + ssh_options(profile) +
                               [profile["host"], spec["tunnel"], "--stop-instance", job], 120)
        if f"HPCLIB_TUNNEL_INSTANCE_STOPPED {job}" in text:
            result = "stopped"
        elif f"HPCLIB_TUNNEL_INSTANCE_GONE {job}" in text:
            result = "already ended"
        else:
            reason = next((l.split(": ", 1)[1] for l in text.splitlines() if l.startswith("setup_tunnel: ")),
                          " ".join(text.strip().splitlines()[-2:]))
            raise ConsoleError(409, f"job {job} was not ended: {reason}")
        with open(self.app_log_path(profile["name"], app), "a") as log:
            log.write(f"hpclib console: ended job {job} ({result})\n")
        # the tunnel to it has nothing left to reach
        with self.lock:
            proc = self.app_procs.get((profile["name"], app))
        _, conf = self.app_config(profile, app)
        if (proc is not None and proc.poll() is None) or self.port_open(conf["port"]):
            self.app_stop(profile, app)
        return {"job": job, "result": result, "message": f"job {job} {result}"}

    def app_state(self, profile, app):
        state = self._app_state(profile, app)
        state["tools"], state["controls"] = self.app_extras(APPS[app], state)
        if APPS[app].get("reinstall"):     # what Install does once installed (PAI: Update), if not Reinstall
            state["reinstall"] = APPS[app]["reinstall"]
        return state

    def _app_state(self, profile, app):
        spec = self.app_spec(app)
        name = profile["name"]
        _, conf = self.app_config(profile, app)
        port = conf["port"]
        with self.lock:
            proc = self.app_procs.get((name, app))
        out = {"app": app, "cluster": name, "host": profile.get("host"), "port": port,
               "started_here": proc is not None and proc.poll() is None, "url": None, "prompt": self.prompt_of(proc),
               "install": (conf.get("install") or {"state": "unchecked"}) if self.app_installable(app)
                          else {"state": "nothing"},
               "shared": bool(spec.get("shared")), "kind": spec.get("kind", "tunnel")}
        if spec.get("downloads"):    # the row's download toolbar (a package's `downloads` section)
            out["downloads"] = spec["downloads"]
        if spec.get("smb"):
            with self.lock:
                control = self.control_procs.get((name, app))
            out["auth"] = conf.get("auth")
            out["signing_in"] = control is not None and control.poll() is None
            out["prompt"] = self.prompt_of(control) or out["prompt"]
        with self.lock:
            op = self.ops.get(name)
            if op and op.get("app") == app:
                out["operation"] = {k: op.get(k) for k in ("kind", "title", "app", "state", "exit_code", "started",
                                                           "finished")}
        if spec.get("shared") and (out["started_here"] or self.port_open(port)):
            out["instance"] = self._app_instance(name, app)
        if not self.port_open(port):
            return dict(out, state="starting" if out["started_here"] else "down")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{spec['health_path']}", timeout=5) as res:
                ctype, body = res.headers.get("Content-Type", ""), res.read(65536)
        except urllib.error.HTTPError as e:
            ctype, body = e.headers.get("Content-Type", ""), e.read(65536)
        except (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout) as e:
            if out["started_here"]:   # still connecting (see tunnel_state)
                return dict(out, state="starting", error="connecting: nothing answers behind the port yet")
            return dict(out, state="error", error=str(getattr(e, "reason", e)))
        if "text/html" in ctype and b"Waiting for" in body:
            m = SHIM_STATUS_RE.search(body.decode(errors="replace"))
            return dict(out, state="queued", status=html_unescape(m.group(1)) if m else "queued")
        try:
            info = json.loads(body)
        except ValueError:
            info = {}
        if not isinstance(info, dict):   # a health path may answer with any JSON (CMCC DB: a list of datasets)
            info = {}
        token = self._app_token(name, app)
        url = f"http://127.0.0.1:{port}{spec['open_path']}" + (f"?token={token}" if token else "")
        if spec.get("gui_re") is not None:
            found = spec["gui_re"].findall(self._last_launch(name, app))
            if not found:
                return dict(out, state="starting", error="waiting for it to say where to open it")
            return dict(out, state="up", token_known=True, **self.transfer_gui_url(port, spec, *found[-1]))
        extra = {}
        if spec.get("password_re") is not None:
            extra["password"] = password = self._app_token(name, app, "password_re")
            if spec.get("login_user"):
                # a basic-auth login: Open answers it (the browser keeps it for the page's own requests)
                extra["username"] = spec["login_user"]
                if password:
                    q = urllib.parse.quote
                    url = url.replace("http://", f"http://{q(spec['login_user'], safe='')}:{q(password, safe='')}@", 1)
        return dict(out, state="up", version=info.get("version"), url=url,
                    token_known=token is not None or "token_re" not in spec, **extra)

    @staticmethod
    def transfer_gui_url(port, spec, which, fields):
        """
        Where Open goes for a GUI's HPCLIB_TRANSFER_GUI line (TRANSFER_GUI_RE): path= (default the app's
        open_path), and query= as given (already URL-encoded); user= and pass= go in the address, answering the
        server's basic-auth prompt for the page, and are shown in case it asks again. The port is the console's.
        """
        given = dict(f.split("=", 1) for f in fields.split())
        user, password = given.get("user"), given.get("pass")
        path, query = given.get("path") or spec["open_path"], given.get("query")
        path = "/" + path.lstrip("/")             # on this port, whatever the GUI printed
        if which == "RCLONE":       # an older smbshell: rclone's GUI takes the same login for its API calls
            query = "login_token=" + urllib.parse.quote(
                base64.urlsafe_b64encode(f"{user}:{password}".encode()).decode(), safe="")
        q = urllib.parse.quote
        login = f"{q(user, safe='')}:{q(password or '', safe='')}@" if user else ""
        url = f"http://{login}127.0.0.1:{port}{path}" + (f"?{query}" if query else "")
        return dict(url=url, **({"username": user, "password": password} if user else {}))

    def app_sessions(self, app):
        self.app_spec(app)
        profiles = agent_profiles.all_profiles()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, len(profiles)))) as pool:
            states = list(pool.map(lambda p: dict(self.app_state(p, app), login=self.logins.status(p)), profiles))
        return states

    def app_start(self, profile, app):
        spec = self.app_spec(app)
        name = profile["name"]
        _, conf = self.app_config(profile, app)
        with self.lock:
            proc = self.app_procs.get((name, app))
            if proc is not None and proc.poll() is None:
                raise ConsoleError(409, f"{spec['title']} on {name} is already starting or running")
        if self.port_open(conf["port"]):
            raise ConsoleError(409, f"something already listens on port {conf['port']} for {spec['title']} on {name}")
        if not self.logins.alive(profile):
            raise ConsoleError(409, f"log in to {name} first: the tunnel runs over the console's ssh login")
        if (conf.get("install") or {}).get("state") == "missing" and self.app_installable(app):
            raise ConsoleError(409, f"{spec['title']} isn't installed on {name} ({conf['install'].get('message')}); "
                                    f"install it, or Check again if you installed it yourself")
        stale_package = self.package_stage(spec["tunnel"])[1] != conf.get("pushed_package")
        if (spec.get("settings") and not conf.get("pushed")) or stale_package:
            self.app_check(profile, app)       # sends the settings (and a package's files), so the job sees them
            _, conf = self.app_config(profile, app)
            if (conf.get("install") or {}).get("state") == "missing" and self.app_installable(app):
                raise ConsoleError(409, f"{spec['title']} isn't installed on {name} ({conf['install'].get('message')})")
        if spec.get("kind") == "login":
            # smbshell gui (the GUI SMB_GUI names) on the login node; the console's ssh forwards the port
            # (stop_tunnel cancels it)
            port = str(conf["port"])
            args = (["smbshell", "--on", "-L", f"127.0.0.1:{port}:127.0.0.1:{port}"] + ssh_options(profile) +
                    [profile["host"], "gui", "--port", port])
            self._open_log(name, " ".join(args), self.app_log_path(name, app)).close()
            proc = self.tunnel_runner(args, self.app_log_path(name, app))
            proc.note = spec.get("note")
            with self.lock:
                self.app_procs[(name, app)] = proc
            return {"started": name, "app": app, "pid": proc.pid, "command": args}
        env = []
        if conf.get("conda_env") is not None:
            env.append(f"CONDA_ENVIRONMENT={conf['conda_env']}")
        if conf.get("modules"):
            env.append(f"{spec['env_settings']['modules']}={':'.join(conf['modules'])}")
        if conf.get("project"):
            env.append(f"{spec['env_settings']['project']}={conf['project']}")
        args = ["launch_tunnel", "-A", "none", "-P", str(conf["port"]), profile["host"], spec["tunnel"],
                f"--process-port={conf['process_port']}"] + list(conf.get("tunnel_args") or [])
        if env:
            args.append("--env=" + ",".join(env))
        self._open_log(name, " ".join(args), self.app_log_path(name, app)).close()
        proc = self.tunnel_runner(args, self.app_log_path(name, app))
        with self.lock:
            self.app_procs[(name, app)] = proc
        return {"started": name, "app": app, "pid": proc.pid, "command": args}

    def app_stop(self, profile, app, wait=30):
        self.app_spec(app)
        name = profile["name"]
        _, conf = self.app_config(profile, app)
        log = self._open_log(name, f"stop_tunnel -P {conf['port']} {profile['host']}", self.app_log_path(name, app))
        try:
            stopper = self.shell_runner(["stop_tunnel", "-P", str(conf["port"]), profile["host"]], log)
            try:
                code = stopper.wait(wait)
            except subprocess.TimeoutExpired:
                os.killpg(stopper.pid, signal.SIGTERM)
                code = None
        finally:
            log.close()
        with self.lock:
            proc = self.app_procs.pop((name, app), None)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        return {"stopped": name, "app": app, "stop_exit": code}

    def app_log(self, name, app, lines=200):
        self.app_spec(app)
        try:
            with open(self.app_log_path(name, app), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - (256 << 10)))
                text = f.read().decode(errors="replace")
        except FileNotFoundError:
            return {"lines": []}
        return {"lines": text.splitlines()[-lines:]}

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
            if not self.logins.alive(profile):
                raise ConsoleError(409, f"log in to {name} first: the tunnel runs over the console's ssh login")
            self._open_log(name, " ".join(args)).close()
            proc = self.tunnel_runner(args, self.log_path(name))
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
        self.stylesheet_lock = threading.Lock()
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
        if parts == ["styles", "open"] and verb == "POST":
            return 200, self.open_stylesheet()
        if parts == ["health"] and verb == "GET":
            return 200, {"ok": True, "console": VERSION, "clusters": len(c.names()), "pid": os.getpid(),
                         "hpclib_version": local_hpclib_version()}
        if parts == ["packages"] and verb == "GET":
            return 200, {"ok": True, "packages": c.packages_list()}
        if parts == ["packages"] and verb == "POST":
            data = self.body(console_packages.MAX_ZIP)
            return (200 if self.flag("dry_run") else 201), dict(c.package_add(data, self.flag("dry_run")), ok=True)
        if len(parts) == 2 and parts[0] == "packages" and verb == "DELETE":
            return 200, dict(c.package_remove(parts[1]), ok=True)
        if parts == ["apps"] and verb == "GET":
            return 200, {"ok": True, "apps": [{"id": "agents", "title": "Agents"}] +
                         [{"id": k, "title": v["title"], "kind": v.get("kind", "tunnel"),
                           "description": v.get("description"), "package": v.get("package")}
                          for k, v in APPS.items()]}
        if len(parts) >= 2 and parts[0] == "apps":
            return self.app_route(verb, parts[1], parts[2:])
        if parts == ["clusters"] and verb == "POST":
            return 201, dict(c.add(self.json_body()), ok=True)
        if parts == ["cluster-presets"] and verb == "GET":
            return 200, {"ok": True, "presets": list(c.preset_specs().values())}
        if len(parts) == 2 and parts[0] == "cluster-presets" and verb == "POST":
            return 201, dict(c.add_preset(parts[1], self.json_body()), ok=True)
        if parts == ["clusters"] and verb == "GET":
            profiles = agent_profiles.all_profiles()
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, len(profiles)))) as pool:
                described = list(pool.map(c.describe, profiles))
            return 200, {"ok": True, "clusters": described}
        if parts == ["prompts"] and verb == "GET":
            return 200, dict(c.prompts(), ok=True)
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
            if rest == ["ssh", "setup"] and verb == "POST":
                return 202, dict(c.setup_ssh(profile, self.json_body()), ok=True)
            if rest == ["operation"] and verb == "GET":
                return 200, dict(c.operation(profile["name"], self.int_arg("lines", 200, 1, 5000)), ok=True)
            if rest == ["login"] and verb == "GET":
                return 200, dict(c.logins.status(profile), ok=True)
            if rest == ["login"] and verb == "POST":
                return 202, dict(c.logins.connect(profile, self.json_body().get("password")), ok=True)
            if rest == ["login", "answer"] and verb == "POST":
                return 202, dict(c.logins.answer_code(profile, self.json_body()), ok=True)
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
            if rest == ["tunnel", "answer"] and verb == "POST":
                with c.lock:
                    proc = c.procs.get(profile["name"])
                return 200, dict(c.answer_prompt(proc, self.json_body().get("answer")), ok=True)
            if rest == ["tunnel", "stop"] and verb == "POST":
                return 200, dict(c.stop_tunnel(profile), ok=True)
            if rest == ["tunnel", "log"] and verb == "GET":
                return 200, dict(c.tail_log(profile["name"], self.int_arg("lines", 200, 1, 5000)), ok=True)
            if rest[:1] == ["rest"] and len(rest) > 1:
                return self.proxy(verb, profile, "/".join(rest[1:]))
        raise ConsoleError(404 if verb in ("GET", "POST", "PUT", "DELETE") else 405, f"no route {verb} {path}")

    def app_route(self, verb, app, rest):
        c = self.server.clusters
        c.app_spec(app)
        if not rest and verb == "GET":
            return 200, {"ok": True, "app": app, "title": APPS[app]["title"], "sessions": c.app_sessions(app)}
        profile = c.profile(rest[0])
        action = rest[1:]
        if not action and verb == "GET":
            return 200, dict(c.app_state(profile, app), login=c.logins.status(profile), ok=True)
        if action == ["start"] and verb == "POST":
            return 202, dict(c.app_start(profile, app), ok=True)
        if action == ["stop"] and verb == "POST":
            return 200, dict(c.app_stop(profile, app), ok=True)
        if action == ["answer"] and verb == "POST":
            proc = c.app_waiting(profile["name"], app)
            return 200, dict(c.answer_prompt(proc, self.json_body().get("answer")), ok=True)
        if action == ["log"] and verb == "GET":
            return 200, dict(c.app_log(profile["name"], app, self.int_arg("lines", 200, 1, 5000)), ok=True)
        if action == ["check"] and verb == "POST":
            return 200, dict(install=c.app_check(profile, app), ok=True)
        if action == ["install"] and verb == "POST":
            return 202, dict(c.app_install(profile, app, self.json_body()), ok=True)
        if action == ["instances"] and verb == "GET":
            return 200, dict(instances=c.app_instances(profile, app), ok=True)
        if len(action) == 2 and action[0] == "control" and verb == "POST":
            return 200, dict(c.app_control(profile, app, action[1], self.json_body()), ok=True)
        if action == ["settings"] and verb == "GET":
            return 200, dict(c.app_settings(profile, app), ok=True)
        if action == ["tokens"] and verb == "GET":
            return 200, dict(c.app_tokens(profile, app), ok=True)
        if action == ["tokens"] and verb == "POST":
            return 201, dict(c.create_token(profile, app, self.json_body()), ok=True)
        if len(action) == 2 and action[0] == "tokens" and verb == "DELETE":
            return 200, dict(c.revoke_token(profile, app, action[1]), ok=True)
        if action == ["downloads"] and verb == "GET":
            return 200, dict(c.app_downloads(profile, app), ok=True)
        if action == ["downloads"] and verb == "POST":
            return 202, dict(c.app_download(profile, app, self.json_body()), ok=True)
        if action == ["downloads", "remove"] and verb == "POST":
            return 200, dict(c.app_download_remove(profile, app, self.json_body()), ok=True)
        if action == ["secrets"] and verb == "PUT":
            return 200, dict(c.send_secrets(profile, app, self.json_body()), ok=True)
        if action == ["settings"] and verb == "PUT":
            return 200, dict(c.save_app_settings(profile, app, self.json_body()), ok=True)
        raise ConsoleError(404, f"no route {verb} /api/apps/{app}/{'/'.join(rest)}")

    def proxy(self, verb, profile, route):
        if (profile.get("preset") or {}).get("ssh_keys") == "site-managed" and route.startswith("admin/ssh/") and verb != "GET":
            raise ConsoleError(409, "this cluster's SSH files are managed by the site; only read operations are available")
        if not REST_ROUTE_RE.fullmatch(route) or ".." in route.split("/"):
            raise ConsoleError(400, f"invalid cluster route {route!r}")
        query = {k: v for k, v in self.query.items() if k != "as"}
        as_agent = self.query.get("as", [""])[-1] == "agent"
        if verb == "GET" and route in STREAMED_ROUTES:
            return self.proxy_stream(profile, route, query, as_agent)
        data = self.body(self.server.max_body) if verb in ("POST", "PUT", "DELETE") else None
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
                "rest_on": profile.get("rest_on") or "job",
                "rest_on_choices": list(agent_profiles.REST_ON),
                "modes": list(agent_profiles.APPROVE_MODES),
                "tunnel_arg_pattern": agent_profiles.TUNNEL_ARG_RE.pattern,
                "applies": "the next time the tunnel starts"}

    def save_tunnel_settings(self, profile, body):
        unknown = set(body) - {"auto_approve_templates", "tunnel_args", "connection_hours", "rest_on"}
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
        rest_on = body.get("rest_on", profile.get("rest_on") or "job")
        if rest_on not in agent_profiles.REST_ON:
            raise ConsoleError(422, f"rest_on must be one of {list(agent_profiles.REST_ON)}")
        fresh = agent_profiles.load(profile["name"]) or profile
        fresh.update(auto_approve_templates=mode, tunnel_args=args, connection_hours=hours, rest_on=rest_on)
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

    def open_stylesheet(self):
        path = os.path.abspath(os.path.join(console_dir(), "user.css"))
        created = False
        with self.server.stylesheet_lock:
            if not os.path.lexists(path):
                if not self.server.static_dir:
                    raise ConsoleError(503, "default-styles.css is unavailable; run the console with --static")
                defaults = os.path.join(self.server.static_dir, "default-styles.css")
                try:
                    with open(defaults, "rb") as f:
                        content = f.read()
                except OSError as e:
                    raise ConsoleError(503, "could not read the bundled default-styles.css") from e
                os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
                try:
                    # Exclusive creation also preserves a file another process just created.
                    with open(path, "xb") as f:
                        f.write(content)
                    created = True
                except FileExistsError:
                    pass
            if not os.path.isfile(path):
                raise ConsoleError(409, "user.css exists but is not a file")
        try:
            open_with_default_application(path)
        except (OSError, NotImplementedError) as e:
            raise ConsoleError(503, f"could not open user.css with the OS default application: {e}", path=path) from e
        return {"ok": True, "path": path, "created": created}

    def serve_static(self, path):
        # One optional user file outside --static; never expose the console's other files.
        # Handle it before the SPA fallback, which would otherwise return HTML when it is absent.
        if path == "/user.css":
            try:
                with open(os.path.join(console_dir(), "user.css"), "rb") as f:
                    body = f.read()
            except FileNotFoundError:
                body = b""
            return self.send_bytes(200, "text/css; charset=utf-8", body, cache=False)
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
