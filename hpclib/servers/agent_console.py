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
  GET  /api/about                            versions (running and on disk), paths, packages, this process
  GET  /api/update[?fetch=1]                  the hpclib clone this console runs from (console_update.py): branch,
                                              upstream, commits behind and ahead, what an update brings; fetch=1
                                              asks the remote first
  POST /api/update                            fetch and fast-forward it (refused when local changes or commits
                                              are in the way); the console runs the new code after a restart
  POST /api/restart                           start this console again (same port and session key), running the
                                              hpclib now on disk; refused while a login or an operation runs
  GET  /api/styles                            Interface Settings: what user.css sets default-styles.css's variables
                                              to ({"values": {"common", "light", "dark"}})
  PUT  /api/styles                            {"common", "light", "dark"}: write them into user.css (changed in
                                              place, defaults taken out, new ones in a block at the end;
                                              console_styles.py); the rest of user.css is left as it is
  GET  /api/diagnostics                      about, preferences and every cluster's state, for a bug report
                                              (token-like strings redacted)
  GET  /api/preferences                      this machine's preferences (console_prefs.py)
  GET  /api/platform                         this machine's platform (local_platform.py: linux, macos, wsl), where
                                              it came from, and under WSL the Windows profile folder the LLM
                                              clients' configuration is in
  PUT  /api/preferences                      {section: {...}}: notifications, new_cluster, llm_clients, widgets, system, ...
  GET  /api/ports                            every local port the profiles hold, free / its tunnel's / taken
  PUT  /api/ports/range                      {"low", "high"} (or {"reset": true}): where new ports are picked
  POST /api/clusters/NAME/new-ports          {"app": APP or null}: new local ports while it is stopped
                                              (the agent tunnel's also rewrites mcp.json and the LLM clients')
  PUT  /api/clusters/NAME/mcp-name           {"name"}: the cluster's MCP server name (mcp.json, kept-up-to-date clients)
  GET  /api/llm-clients                      Claude Desktop, Claude Code, Cursor, ChatGPT: which entries each has
  POST /api/llm-clients/ID/write             add or update every cluster's entry (mcp_clients.py); {"names": [...]}:
                                              only those MCP names
  PUT  /api/clusters/NAME/local               {"command", "args", "local_roots"} (any): what runs its MCP server here
                                              (a launch command, "" finds a Python, and the arguments before
                                              rest_mcp.py) and the agent's local folders; rewrites mcp.json and the
                                              LLM clients kept up to date, with nothing to run on the cluster
  GET  /api/clusters/NAME/llm-clients         this cluster's entry in each client (current, different, missing),
                                              and whether its Python has the MCP SDK
  POST /api/llm-clients/ID/remove            {"name"}: remove an hpclib entry no cluster has any more
  GET  /api/watch                            each tunnel's port open or not, and Stops asked for here
  GET  /api/widgets                          the installed packages' widgets (slot, topics they read, height)
  GET  /api/widgets/PACKAGE/ID               {"html"}: a widget's page, which the front end shows sandboxed
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
  DELETE /api/clusters/NAME                   forget the cluster here: {"revoke_token"?: false (its agent token, on
                                              the cluster, first), "stop_tunnels"?, "log_out"?, "llm_clients"?:
                                              true}; the profile moves to agents/.removed/ (kept_in)
  POST /api/clusters/NAME/install             install_hpclib over the login ({"force": true} reinstalls)
  POST /api/clusters/NAME/setup               setup_agents ({"work_dirs": [...], "binds": [...], "local_roots": [...]
                                              (folders here for push_files/pull_files; [] none; created),
                                              "mcp_command" and "mcp_args" (what starts the MCP server here: a
                                              program, "" finds a Python, and the arguments before rest_mcp.py,
                                              space-separated), "remote_python" (the cluster's: a path, "" finds one) or
                                              "python_modules" (modules that provide it),
                                              "templates": "...", "rebuild": false})
  GET  /api/clusters/NAME/operation           the running or last install/setup/SSH setup, with its log
  GET  /api/clusters/NAME/jobs                every job of yours on the cluster (squeue, and sacct for the last day) over
                                              the login: queued, running and finished, each labelled with what started it
  POST /api/clusters/NAME/jobs/cancel         {"job": ID}: scancel an agent's or another job (apps stop with their Stop)
  GET  /api/clusters/NAME/storage             the last storage scan of the work directories (du), and the quota
  POST /api/clusters/NAME/storage/scan        start a new scan over the login (in the background), with the quotas
  GET  /api/clusters/NAME/quota[?kind=]       the quota checks (lib/quota.sh: storage and project) over the login,
                                              kept with the storage scan
  PUT  /api/clusters/NAME/quota/settings      {"storage"?, "project"?}: the cluster's checks (a command; "" for
                                              the default, "none" to turn one off), saved on the cluster
                                              ($HPCTUNNELS_DATA_DIR/settings/quota.sh), then checked
  PUT  /api/clusters/NAME/app-view            {"pinned": [APP...], "hidden": [APP...]}: which apps the cluster's Apps
                                              tab and Overview show always (pinned), not at all (hidden), or under
                                              "More apps" (the rest); the profile's app_view
  GET  /api/clusters/NAME/python-tools        uv and pixi on the cluster over the login (lib/pytools.sh): where each
                                              is and its version, and their directories (Pythons, caches) with where
                                              each value comes from (your ~/.bashrc, hpclib's settings, the default)
  PUT  /api/clusters/NAME/python-tools        {"UV_PYTHON_INSTALL_DIR"?, "UV_CACHE_DIR"?, "PIXI_CACHE_DIR"?}: save
                                              directories on the cluster ($HPCTUNNELS_DATA_DIR/settings/pytools.sh;
                                              "" for the tool's default); hpclib.sh exports them, ~/.bashrc wins
  POST /api/clusters/NAME/python-tools/install  {"tool": "uv"|"pixi"}: run its official installer over the login
  GET  /api/clusters/NAME/sandbox-tools       installed runtimes and namespace/build prerequisites on the login node
  POST /api/clusters/NAME/sandbox-tools/install {"tool": "bubblewrap"|"charliecloud", "modules": [NAME, ...]}

                                              (~/.local/bin/uv, ~/.pixi/bin/pixi; shell startup files untouched)
  GET  /api/clusters/NAME/ports               the login-node ports this console's tunnels use, and which run now
  POST /api/clusters/NAME/ports/clear         {"fresh_logins": false, "password"?, "max_logins"?}: clear what earlier
                                              tunnels left on those ports on every login node (clear_tunnel_ports,
                                              an operation); with fresh_logins, nodes the login can't reach are
                                              cleared by logging in again until one lands there (a password and a
                                              Duo push each time)
  GET  /api/apps                              the console's apps: agents, and tunnel apps (JupyterLab), with
                                              the package each came from (null: hpclib's own)
  GET  /api/packages                          apps and settings installed from packages (console_packages.py)
  POST /api/packages[?dry_run=1]              body: a package zip; checks it (dry_run) or installs it
  GET  /api/packages/bundled                  the packages that come with this hpclib (its clone's packages/:
                                              the Workspace), with the version installed
  POST /api/packages/bundled/NAME[?dry_run=1]  build that one and check it (dry_run) or install it
  DELETE /api/packages/NAME                   removes one (its apps go; what it sent to clusters stays)
  GET  /api/apps/APP                          that app's session on every cluster: state (down, starting,
                                              queued, up), the job's queue status, and the URL to open
  POST /api/apps/APP/NAME/start|stop          launch_tunnel / stop_tunnel for it, over the login
  POST /api/apps/APP/NAME/open                Data transfer: clear idle rclone filesystem clients, then return its URL
  GET  /api/apps/APP/NAME/log                 its tunnel's output
  GET  /api/apps/APP/NAME/settings, PUT ...   {"tunnel_args": [...], "install_modules": [...], "conda_env": ..., "modules": [...],
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
import http.client
import http.server
import json
import mimetypes
import os
import platform
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
import local_platform  # noqa: E402
sys.path.insert(0, HERE)
import console_packages  # noqa: E402
import console_prefs  # noqa: E402
import console_styles  # noqa: E402
import console_update  # noqa: E402
import mcp_clients  # noqa: E402

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
                "package_version": manifest["version"], "install_modules": app.get("install_modules", []),
                "settings": [(f["name"], f["label"], f["hint"]) + ((tuple(f["choices"]),) if f["choices"] else ())
                             for f in app["settings"]]}
        if app.get("description"):
            spec["description"] = app["description"]
        if app.get("tab"):
            spec["tab"] = dict(app["tab"])
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


STARTED_VERSION = local_hpclib_version()     # what this process loaded; the file may be newer (About says so)
STARTED_AT = time.time()
REDACT_RE = re.compile(r"[A-Za-z0-9_+=-]{32,}")      # token_urlsafe(32) and the like; not paths


def git_info(directory):
    """The checkout's branch and commit, read from .git (no git command), or None outside one."""
    git = os.path.join(directory, ".git")
    try:
        with open(os.path.join(git, "HEAD")) as f:
            head = f.read().strip()
    except OSError:
        return None
    if not head.startswith("ref: "):
        return {"branch": None, "commit": head[:12]}
    ref = head[5:]
    commit = None
    try:
        with open(os.path.join(git, ref)) as f:
            commit = f.read().strip()
    except OSError:
        try:
            with open(os.path.join(git, "packed-refs")) as f:
                for line in f:
                    if line.rstrip().endswith(" " + ref):
                        commit = line.split()[0]
        except OSError:
            pass
    return {"branch": ref.rsplit("/", 1)[-1], "commit": commit[:12] if commit else None}


def redact(value):
    """Strings for diagnostics: token-like runs hidden, the home directory as ~."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return REDACT_RE.sub("[redacted]", value.replace(os.path.expanduser("~"), "~"))
    return value


REST_ROUTE_RE = re.compile(r"[A-Za-z0-9_./-]{1,256}")
PROFILE_KEYS = ("name", "host", "port", "process_port", "work_dirs", "binds", "local_roots", "token_name", "preset",
                "mcp_name", "created", "updated", "setup_config", "mcp_python", "mcp_command", "mcp_args", "python_spec",
                "remote_python", "app_view")


class ConsoleError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.payload = dict(error=message, **extra)


def console_dir():
    return os.path.expanduser(os.environ.get("HPCLIB_CONSOLE_DIR") or "~/.config/hpclib/console")


# this machine's platform: the console's own Console Settings -> This machine (local_platform.py)
local_platform.settings = lambda: console_prefs.load(console_dir())["system"]
mcp_clients.overrides = lambda: console_prefs.load(console_dir())["llm_client_paths"]   # Console Settings -> LLM clients


LOG_BLOCK_HEADER = re.compile(rb"(?m)^=== \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [^\r\n]* ===\r?$")
TERMINAL_ESCAPE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")


def last_log_block(path, max_bytes=8 << 20):
    """Read back to the newest console header, independently of the short display tail."""
    try:
        with open(path, "rb") as handle:
            position = handle.seek(0, os.SEEK_END)
            data = b""
            while position:
                start = max(0, position - (64 << 10))
                handle.seek(start)
                data = handle.read(position - start) + data
                position = start
                headers = [m for m in LOG_BLOCK_HEADER.finditer(data) if position == 0 or m.start() > 0]
                if headers:
                    data = data[headers[-1].start():]
                    break
                if len(data) > max_bytes:
                    raise ConsoleError(413, "The last log block is too large to copy (over 8 MiB); open the log file instead.")
    except FileNotFoundError:
        return ""
    if len(data) > max_bytes:
        raise ConsoleError(413, "The last log block is too large to copy (over 8 MiB); open the log file instead.")
    # PTYs add CRLF, colors and terminal-title sequences. Copy readable text, without terminal controls.
    text = TERMINAL_ESCAPE.sub("", data.decode(errors="replace").replace("\r", ""))
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)


def open_with_default_application(path):
    """Use the OS file association (local_platform: Windows' under WSL); never choose or look for an editor ourselves."""
    local_platform.open_target(path)


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
            if self.CODE_RE.search(prompt) and attempt.get("no_code"):
                return self._fail(attempt, "the cluster asks for a typed code, which these extra logins can't "
                                           "answer", shown)
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

    def run_fresh(self, profile, password, remote, stdin_text, extra=(), timeout=None):
        """
        One more login, of its own (not the console's ssh master, so it may land on another login node), that runs
        `remote` with `stdin_text` on its standard input: the password and a Duo push are answered as for a
        login; a typed code is not. Returns (exit code or None, stdout, the login's message). The password is
        dropped once ssh has used it, or when this returns.
        """
        socket_path = self._serve()
        nonce = secrets.token_urlsafe(24)
        attempt = {"state": "starting", "message": "connecting", "started": time.time(), "finished": None,
                   "prompts": [], "password": password or None, "password_used": False, "push_sent": False,
                   "host": profile["host"], "no_code": True}
        with self.lock:
            self.by_nonce[nonce] = attempt
        env = dict(os.environ, SSH_ASKPASS=self._askpass_program(), SSH_ASKPASS_REQUIRE="force",
                   HPCLIB_ASKPASS_SOCKET=socket_path, HPCLIB_ASKPASS_NONCE=nonce)
        env.setdefault("DISPLAY", ":0")
        # ssh takes the first value of each option, so these come before the profile's own
        command = ([self.ssh, "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "NumberOfPasswordPrompts=1",
                    "-o", "ConnectTimeout=20", "-T"] + list(extra) + ssh_options(profile) + [profile["host"], remote])
        try:
            res = subprocess.run(command, input=stdin_text.encode(), capture_output=True, env=env,
                                 timeout=timeout or (self.TIMEOUT + 300), start_new_session=True)
            code, out, err = res.returncode, res.stdout.decode(errors="replace"), res.stderr.decode(errors="replace")
        except subprocess.TimeoutExpired:
            code, out, err = None, "", "timed out"
        except OSError as e:
            code, out, err = None, "", f"could not run ssh: {e}"
        finally:
            with self.lock:
                self.by_nonce.pop(nonce, None)
                attempt["password"] = None
        if attempt["state"] == "failed":
            message = attempt["message"]
        elif code == 255 or code is None:
            said = [line.strip() for line in err.splitlines() if line.strip()]
            message = f"ssh failed{': ' + said[-1][:300] if said else ''}"
        else:
            message = "logged in"
        return code, out, message

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




def parse_rss(value):
    """sacct's MaxRSS ("123456K", "1.5G") in KiB."""
    m = re.fullmatch(r"([0-9.]+)([KMGT]?)", (value or "").strip())
    if not m:
        return 0.0
    return float(m.group(1)) * {"": 1 / 1024, "K": 1, "M": 1024, "G": 1024 ** 2, "T": 1024 ** 3}[m.group(2)]


def parse_storage(text):
    """CLUSTER_STORAGE_SCRIPT's lines: {"dirs": [{path, kb, files, partial, files_partial, missing, children:
    [{path, kb, files}]}], "quota"}. Children are kept largest first, at most STORAGE_CHILDREN of them."""
    dirs, current = [], None
    for line in text.splitlines():
        kind, _, rest = line.partition("|")
        if kind == "M":
            dirs.append({"path": rest, "missing": True, "kb": None, "files": None, "children": []})
            current = None
        elif kind == "D":
            state, _, path = rest.partition("|")
            current = {"path": path, "partial": state != "ok", "kb": None, "files": None, "children": []}
            dirs.append(current)
        elif kind in ("S", "F") and current is not None:
            value, _, path = rest.partition("|")
            if not value.isdigit():
                continue
            key = "kb" if kind == "S" else "files"
            if path.rstrip("/") == current["path"].rstrip("/"):
                current[key] = int(value)
                continue
            child = next((ch for ch in current["children"] if ch["path"] == path), None)
            if child is None:
                child = {"path": path, "kb": None, "files": None}
                current["children"].append(child)
            child[key] = int(value)
        elif kind == "C" and current is not None:
            state, _, _ = rest.partition("|")
            current["files_partial"] = state != "ok"
        elif kind == "I":           # a scan by an older console: the total file count only
            count, _, path = rest.partition("|")
            for d in dirs:
                if d["path"] == path and count.isdigit():
                    d["files"] = int(count)
    for d in dirs:
        d["children"] = sorted(d["children"], key=lambda c: (c["kb"] or 0, c["files"] or 0),
                               reverse=True)[:STORAGE_CHILDREN]
    quotas = parse_quota(text)
    return {"dirs": dirs, "quotas": quotas, "quota": storage_quota(quotas)}


STORAGE_CHILDREN = 200       # the folders kept per directory (the page shows the top ones, by size or by files)


def parse_quota(text):
    """lib/quota.sh's `hpclib_quota --json` line: {host, settings_file, storage?, project?}, or None."""
    for line in text.splitlines():
        if line.startswith("HPCLIB_QUOTA "):
            try:
                out = json.loads(line[len("HPCLIB_QUOTA "):])
            except ValueError:
                return None
            return out if isinstance(out, dict) else None
    return None


def storage_quota(quotas):
    """The storage check as the Storage tab's older {command, lines}."""
    check = (quotas or {}).get("storage") or {}
    return {"command": check["command"], "lines": check.get("output") or []} if check.get("command") else None


QUOTA_SH = os.path.join(HPCLIB_DIR, "lib", "quota.sh")
QUOTA_ENV = ("HPCLIB_STORAGE_QUOTA_CHECK", "HPCLIB_PROJECT_QUOTA_CHECK")
QUOTA_KINDS = ("all", "storage", "project")
PYTOOLS_SH = os.path.join(HPCLIB_DIR, "lib", "pytools.sh")
PYTOOLS_VARS = ("UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "PIXI_CACHE_DIR")
SANDBOX_TOOLS_PY = os.path.join(HPCLIB_DIR, "servers", "sandbox_tools.py")


def pytools_script(call):
    """lib/pytools.sh's functions and then `call`, for bash -s on a cluster (works before hpclib is installed there)."""
    with open(PYTOOLS_SH) as f:
        text = f.read()
    return f"{text}\n{call}\n"


def quota_script(call):
    """
    lib/quota.sh's functions and then `call`, for bash -s on a cluster (so it works before hpclib is installed
    there). Like cluster_quota, the console passes on its own HPCLIB_*_QUOTA_CHECK when they are set.
    """
    with open(QUOTA_SH) as f:
        text = f.read()
    env = "".join(f"export {v}={shlex.quote(os.environ[v])}\n" for v in QUOTA_ENV if os.environ.get(v))
    return f"{env}{text}\n{call}\n"

# -- every job of yours on a cluster, and its storage (the cluster page's Jobs and Storage tabs) -----------------

# Runs ON the login node: squeue (queued and running) and a day of sacct (finished), one marked line each.
CLUSTER_JOBS_SCRIPT = r"""
squeue -h -u "$USER" -o 'Q|%i|%j|%T|%r|%P|%M|%l|%D|%C|%b|%m|%N|%S|%k' 2>&1
sacct -n -P -u "$USER" -S now-1day -o JobID,JobName,State,ExitCode,Elapsed,End,Partition,MaxRSS,ReqMem,Comment \
  2>/dev/null | sed 's/^/A|/'
"""

# Runs ON the login node: STORAGE_TIMEOUT DIR...: each directory's size by its children (du -k --max-depth=1) and
# their file counts (du --inodes --max-depth=1: files, folders and links, what a file quota counts; find where du is
# too old for --inodes), at low priority and each with a time limit (then marked partial). The scan adds the quota
# checks (quota_script) after it.
CLUSTER_STORAGE_SCRIPT = r"""
limit="$1"; shift
tmp=$(mktemp) || exit 1
trap 'rm -f "$tmp"' EXIT
for d in "$@"; do
  case "$d" in "~/"*) d="$HOME/${d#\~/}" ;; esac
  if [ ! -d "$d" ]; then printf 'M|%s\n' "$d"; continue; fi
  state=ok
  timeout "$limit" nice -n 19 du -k --max-depth=1 -- "$d" > "$tmp" 2>/dev/null || state=partial
  printf 'D|%s|%s\n' "$state" "$d"
  sed 's/^\([0-9]*\)\t/S|\1|/' "$tmp"
  counted=ok
  : > "$tmp"
  if ! timeout "$limit" nice -n 19 du --inodes --max-depth=1 -- "$d" > "$tmp" 2>/dev/null; then
    counted=partial
    if [ ! -s "$tmp" ]; then
      # du without --inodes (coreutils before 8.22): find, folder by folder
      timeout "$limit" nice -n 19 bash -c '
        find "$1" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null | while IFS= read -r -d "" c; do
          printf "%s\t%s\n" "$(find "$c" 2>/dev/null | wc -l)" "$c"
        done
        printf "%s\t%s\n" "$(find "$1" 2>/dev/null | wc -l)" "$1"' hpclib-count "$d" > "$tmp" && counted=ok
    fi
  fi
  printf 'C|%s|%s\n' "$counted" "$d"
  sed 's/^\([0-9]*\)\t/F|\1|/' "$tmp"
done
"""


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
        self.instance_queries = {}   # (name, app): recent registry recovery when launch metadata is unavailable
        self._clear_requests = {}    # name -> clear_ports' options (its password too), until its operation ends
        self.script_runner = self._login_script
        self.storage_scans = {}      # name -> {"state": "running", "started": ...} while a storage scan runs
        self.storage_lock = threading.Lock()   # the storage cache file: a scan and quota checks both write it
        self.stops = {}              # (name, app or None) -> when Stop was asked for here (notifications skip those)
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
        if status == 401 and read_private(profile.get("owner_token_file") or "") and \
                read_private(profile.get("token_file") or ""):
            # A stale owner credential must not hide a working agent connection.
            try:
                status, ctype, content = self.call(profile, "GET", "/health", as_agent=True, timeout=5)
            except ConsoleError as e:
                return dict(out, state="error", error=e.payload["error"])
            if status < 400:
                out["owner_access"] = "rejected"
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
        if record["kind"] == "clear_ports" and profile is not None:
            try:
                code = self._finish_clear_ports(profile, record, code)
            except Exception as e:     # the log is gone, or something unexpected: say so, as a failure
                code = 1
                try:
                    with open(self.ops_log_path(profile["name"]), "ab") as log:
                        log.write(f"clear_tunnel_ports: {e}\n".encode())
                except OSError:
                    pass
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
        if (record["kind"] == "setup" and code == 0 and profile is not None and
                "--repair-owner-token" not in record.get("command", ())):
            # its MCP entry may be new or changed: give it to the LLM clients the console keeps up to date
            try:
                synced = self.sync_llm_clients()
                with open(self.ops_log_path(profile["name"]), "ab") as log:
                    for cid, result in synced.items():
                        log.write(f"MCP entries for {mcp_clients.CLIENTS[cid]['title']}: {result}\n".encode())
            except Exception:     # the clients' state shows on Console Settings
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
    INSTALL_DEADLINE = 6 * 3600          # s: an installation through the REST API that takes longer is given up on
    INSTALL_LOG_LIMIT = 64 << 20         # bytes of its output copied into the console's log
    INSTALL_CHUNK_LIMIT = 1 << 20        # bytes in one answer (the REST server sends at most 64 KiB)
    NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}")

    @staticmethod
    def _one_line(text, limit=300):
        """Cluster-supplied text for one line of a log: no line breaks or control characters, bounded."""
        return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text))[:limit]

    def _install_via_rest(self, profile, record, reason, log):
        spec = self.app_spec(record["app"])
        needs = f"{spec['title']}'s installation needs the REST server's node ({reason})"
        if self.tunnel_state(profile)["state"] != "up":
            raise ConsoleError(409, f"{needs}: start the agent tunnel first (Agents → Clusters → Start)")
        if not read_private(profile.get("owner_token_file") or ""):
            raise ConsoleError(409, f"{needs}: this console needs its owner credential; use Setup → Recover owner access")

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
                raise ConsoleError(502, f"{needs}: {self._one_line(out.get('error', f'HTTP {status}'))}")
            return out

        reason = self._one_line(reason)
        log.write(f"Continuing {spec['title']} installation through the REST API: {reason}\n".encode())
        out = request("POST", {"tunnel": spec["tunnel"], "force": "--force" in record["command"]})
        ident = out.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise ConsoleError(502, "the REST server returned no installation ID")
        node = out.get("node") if isinstance(out.get("node"), str) and self.NODE_RE.fullmatch(out["node"]) else None
        with self.lock:
            record["rest_install_id"] = ident
            record["rest_node"] = node
        log.write(f"Installation {ident} on {node or 'the REST node'}\n".encode())
        offset, copied, deadline = 0, 0, time.monotonic() + self.INSTALL_DEADLINE
        while True:
            if time.monotonic() > deadline:
                raise ConsoleError(504, f"the installation on the REST node ran over {self.INSTALL_DEADLINE // 3600} h;"
                                        f" it may still be running there (installation {ident})")
            out = request("GET", query={"id": ident, "offset": offset})
            text, following = out.get("output"), out.get("offset")
            if (not isinstance(text, str) or isinstance(following, bool) or not isinstance(following, int) or
                    following < offset or following - offset > self.INSTALL_CHUNK_LIMIT or
                    len(text) > self.INSTALL_CHUNK_LIMIT or
                    out.get("state") not in ("running", "succeeded", "failed")):
                raise ConsoleError(502, "the REST server returned an invalid installation status")
            if copied < self.INSTALL_LOG_LIMIT:
                data = text.encode()[:self.INSTALL_LOG_LIMIT - copied]
                log.write(data)
                copied += len(data)
                if copied >= self.INSTALL_LOG_LIMIT:
                    log.write(b"\n(the rest of the installer's output is in its log on the REST node)\n")
            progressed = following > offset
            offset = following
            if out.get("state") != "running" and not out.get("more"):
                code = out.get("exit_code")
                if isinstance(code, bool) or not isinstance(code, int):
                    raise ConsoleError(502, "the REST server returned no installer exit code")
                if code == 3:
                    raise ConsoleError(502, f"{spec['title']} cannot be installed on the REST server's node either; "
                                            "see the installer output above")
                return code
            if not (out.get("more") and progressed):    # only a server with more to send is asked again at once
                time.sleep(self.INSTALL_POLL_SECONDS)

    def operation(self, name, lines=200, latest=False):
        with self.lock:
            record = dict(self.ops.get(name) or {"state": "none"})
        if latest:
            return dict(record, copy_text=last_log_block(self.ops_log_path(name)))
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


    # -- every job of yours on a cluster, and its storage --------------------------------------------------------

    def _login_script(self, profile, script, args=(), wait=60):
        """`script` run with bash on the cluster's login node over the console's login: (exit code, stdout, stderr)."""
        argv = self.hpclib_argv(["pssh"] + ssh_options(profile) + [profile["host"], shlex.join(["bash", "-s", "--", *args])])
        try:
            res = subprocess.run(argv, input=script.encode(), capture_output=True, timeout=wait,
                                 env=dict(os.environ, **self.HPCLIB_ENV), start_new_session=True)
        except subprocess.TimeoutExpired:
            raise ConsoleError(504, f"{profile['name']} did not answer within {wait} s")
        return res.returncode, res.stdout.decode(errors="replace"), res.stderr.decode(errors="replace")

    def job_source(self, name, comment):
        """What started a job, from what hpclib stamps on its jobs: {kind, label, app?, token?}."""
        m = re.match(r"hpclib-rest:([^:]+):(.+)$", comment or "")
        if m or (name or "").startswith("hpcrest-"):
            template = m.group(2) if m else name[len("hpcrest-"):]
            return {"kind": "agent", "label": f"agent: {template}", "token": m.group(1) if m else None}
        tunnel = (name or "").rsplit("-", 1)[0] if "-" in (name or "") else None
        if tunnel == "rest":
            return {"kind": "agent-tunnel", "label": "agent tunnel"}
        for app, spec in APPS.items():
            if tunnel and spec.get("tunnel") == tunnel:
                if spec.get("shared"):
                    return {"kind": "shared", "label": f"{spec['title']} ({spec.get('instance_label') or 'shared'})",
                            "app": app}
                return {"kind": "app", "label": spec["title"], "app": app}
        return {"kind": "other", "label": "other"}

    QUEUED_STATES = {"PENDING", "REQUEUED", "SUSPENDED", "REQUEUE_HOLD", "RESV_DEL_HOLD", "SPECIAL_EXIT"}

    def cluster_jobs(self, profile):
        """Every job of yours on the cluster: squeue's queued and running jobs, and the last day's finished ones."""
        if not self.logins.alive(profile):
            raise ConsoleError(409, f"log in to {profile['name']} first: its jobs are read over the console's login")
        code, out, err = self.script_runner(profile, CLUSTER_JOBS_SCRIPT, (), 60)
        if code != 0 and "Q|" not in out:
            raise ConsoleError(502, f"squeue failed on {profile['name']}: "
                                    f"{self._one_line((err or out).strip().splitlines()[-1] if (err or out).strip() else code)}")
        queued, running, active = [], [], set()
        for line in out.splitlines():
            if not line.startswith("Q|"):
                continue
            f = line[2:].split("|", 13)
            if len(f) < 14:
                continue
            job = {"id": f[0], "name": f[1], "state": f[2], "reason": f[3], "partition": f[4], "elapsed": f[5],
                   "time_limit": f[6], "nodes": f[7], "cpus": f[8], "gres": "" if f[9] in ("N/A", "(null)") else f[9],
                   "memory": f[10], "node_list": f[11], "start": "" if f[12] in ("N/A", "Unknown") else f[12],
                   "source": self.job_source(f[1], f[13])}
            active.add(f[0].split("_")[0])
            (queued if f[2] in self.QUEUED_STATES else running).append(job)
        finished, steps = {}, {}
        for line in out.splitlines():
            if not line.startswith("A|"):
                continue
            f = line[2:].split("|", 9)
            if len(f) < 10:
                continue
            jid = f[0]
            if "." in jid:            # a step: its peak memory belongs to the job
                steps.setdefault(jid.split(".")[0], []).append(f[7])
                continue
            if jid.split("_")[0] in active or f[2].split()[0] in ("RUNNING", "PENDING"):
                continue
            finished[jid] = {"id": jid, "name": f[1], "state": f[2].split()[0], "exit_code": f[3], "elapsed": f[4],
                             "end": f[5], "partition": f[6], "requested_memory": f[8],
                             "source": self.job_source(f[1], f[9])}
        for jid, job in finished.items():
            peaks = [parse_rss(v) for v in steps.get(jid, []) if v]
            job["peak_memory_mb"] = round(max(peaks) / 1024, 1) if peaks else None
        done = sorted(finished.values(), key=lambda j: j["end"], reverse=True)
        # an app's session ends at its time limit or with Stop: only jobs that do work count as failed
        failed = sum(1 for j in done if j["state"] != "COMPLETED" and j["source"]["kind"] in ("agent", "other"))
        return {"cluster": profile["name"], "at": time.time(), "queued": queued, "running": running,
                "finished": done, "counts": {"queued": len(queued), "running": len(running),
                                             "finished": len(done), "failed": failed}}

    def cancel_job(self, profile, body):
        """scancel one job of yours that an agent or something else started; an app's job stops with the app."""
        job = str((body or {}).get("job") or "")
        if not re.fullmatch(r"[0-9]+(_[0-9]+)?", job) or set(body) - {"job"}:
            raise ConsoleError(400, 'send {"job": "JOB ID"}')
        listed = self.cluster_jobs(profile)
        found = next((j for j in listed["queued"] + listed["running"] if j["id"] == job), None)
        if found is None:
            raise ConsoleError(404, f"job {job} is not queued or running on {profile['name']} (as you)")
        kind = found["source"]["kind"]
        if kind in ("app", "shared", "agent-tunnel"):
            raise ConsoleError(409, {"app": f"stop {found['source']['label']} from its Apps row instead (Stop)",
                                     "shared": f"{found['source']['label']}: use End database job on its Apps row",
                                     "agent-tunnel": "stop the agent tunnel from Overview instead"}[kind])
        code, out, err = self.script_runner(profile, 'scancel "$1"\n', (job,), 60)
        if code != 0:
            raise ConsoleError(502, f"scancel {job} failed: {self._one_line(err or out)}")
        return {"cancelled": job}

    STORAGE_TIMEOUT = 300

    def storage_path(self, name):
        return os.path.join(console_dir(), "storage", f"{name}.json")

    def storage(self, profile):
        name = profile["name"]
        try:
            with open(self.storage_path(name)) as f:
                out = json.load(f)
        except (OSError, ValueError):
            out = {"scanned": None, "dirs": [], "quota": None}
        out.setdefault("quotas", None)
        with self.lock:
            scan = self.storage_scans.get(name)
        out["scan"] = dict(scan) if scan else {"state": "idle"}
        out["work_dirs"] = list(profile.get("work_dirs") or [])
        return out

    def start_storage_scan(self, profile):
        name = profile["name"]
        if not self.logins.alive(profile):
            raise ConsoleError(409, f"log in to {name} first: the scan runs over the console's login")
        dirs = list(profile.get("work_dirs") or []) + ["~/.local/tunnels"]
        with self.lock:
            if (self.storage_scans.get(name) or {}).get("state") == "running":
                return dict(self.storage_scans[name])
            scan = self.storage_scans[name] = {"state": "running", "started": time.time()}
        threading.Thread(target=self._storage_scan, args=(profile, dirs, scan), daemon=True).start()
        return dict(scan)

    def _storage_scan(self, profile, dirs, scan):
        try:
            script = CLUSTER_STORAGE_SCRIPT + quota_script("hpclib_quota all --json")
            code, out, err = self.script_runner(profile, script, (str(self.STORAGE_TIMEOUT), *dirs),
                                                2 * self.STORAGE_TIMEOUT + 240)
            result = parse_storage(out)
            result["scanned"] = time.time()
            if result["quotas"]:
                result["quotas"]["checked"] = result["scanned"]
            self._save_storage(profile["name"], result)
            state = {"state": "done", "finished": time.time()}
        except Exception as e:
            state = {"state": "failed", "finished": time.time(), "error": self._one_line(getattr(e, "payload", {}).get("error") or e)}
        with self.lock:
            scan.update(state)

    def _save_storage(self, name, result):
        with self.storage_lock:
            os.makedirs(os.path.dirname(self.storage_path(name)), mode=0o700, exist_ok=True)
            write_private(self.storage_path(name), json.dumps(result))

    # -- quota checks (lib/quota.sh), with the cluster's own settings ------------

    QUOTA_TIMEOUT = 200

    def quota(self, profile, kind="all"):
        """Runs the checks over the login; the answer replaces the storage scan's (for the kinds it has)."""
        if kind not in QUOTA_KINDS:
            raise ConsoleError(400, f"kind: one of {list(QUOTA_KINDS)}")
        return self._quota_call(profile, 'hpclib_quota "$@"', (kind, "--json"))

    def set_quota_checks(self, profile, body):
        """{"storage"?, "project"?}: saved in the cluster's $HPCTUNNELS_DATA_DIR/settings/quota.sh (what the REST
        server and hpclib_quota there use too); "" forgets one, "none" turns it off. Then both are checked."""
        if not isinstance(body, dict) or not body or set(body) - {"storage", "project"}:
            raise ConsoleError(400, 'send {"storage": COMMAND, "project": COMMAND} (either)')
        args = []
        for kind in ("storage", "project"):
            if kind not in body:
                continue
            value = body[kind]
            if not isinstance(value, str) or re.search(r"[\0\n\r]", value) or len(value) > 1000:
                raise ConsoleError(400, f"{kind}: one line of shell, at most 1000 characters")
            args.append(f"{kind}={value.strip() or 'default'}")
        call = 'for kv in "$@"; do _hpclib_quota_set "${kv%%=*}" "${kv#*=}" || exit 3; done; hpclib_quota all --json'
        return self._quota_call(profile, call, args)

    def _quota_call(self, profile, call, args):
        name = profile["name"]
        if not self.logins.alive(profile):
            raise ConsoleError(409, f"log in to {name} first: quotas are checked over the console's login")
        code, out, err = self.script_runner(profile, quota_script(call), tuple(args), self.QUOTA_TIMEOUT)
        quotas = parse_quota(out)
        if quotas is None:
            detail = self._one_line(err or out) if (err or out).strip() else f"exit {code}"
            raise ConsoleError(502, f"the quota checks on {name} gave no answer: {detail}")
        quotas["checked"] = time.time()
        quotas["environment"] = [v for v in QUOTA_ENV if os.environ.get(v)]
        with self.storage_lock:
            try:
                with open(self.storage_path(name)) as f:
                    stored = json.load(f)
            except (OSError, ValueError):
                stored = {"scanned": None, "dirs": []}
            merged = dict(stored.get("quotas") or {})
            merged.update(quotas)
            stored["quotas"] = merged
            stored["quota"] = storage_quota(merged)
            os.makedirs(os.path.dirname(self.storage_path(name)), mode=0o700, exist_ok=True)
            write_private(self.storage_path(name), json.dumps(stored))
        return {"quotas": merged}

    # -- uv and pixi on the cluster (lib/pytools.sh): installed?, install, and their directories ----------------

    PYTOOLS_TIMEOUT = 60
    PYTOOLS_INSTALL_TIMEOUT = 400

    def python_tools(self, profile):
        """uv and pixi on the cluster over the login: {tools: {uv|pixi: {path, version}}, dirs: {VAR: {value,
        source, saved}}, shell: {VAR: what ~/.bashrc sets}, settings_file, host}."""
        return self._pytools_call(profile, 'hpclib_pytools --json', ())

    def set_python_tools(self, profile, body):
        """{VAR: DIR or "" (the tool's default)} for VAR in PYTOOLS_VARS: saved on the cluster
        ($HPCTUNNELS_DATA_DIR/settings/pytools.sh; the directories are created), then reported again."""
        if not isinstance(body, dict) or not body or set(body) - set(PYTOOLS_VARS):
            raise ConsoleError(400, f"send {{VAR: DIR}} for any of {list(PYTOOLS_VARS)} (\"\" for the tool's default)")
        args = []
        for var in PYTOOLS_VARS:
            if var not in body:
                continue
            value = body[var]
            if not isinstance(value, str) or re.search(r"[\0\n\r]", value) or len(value) > 1000:
                raise ConsoleError(400, f"{var}: one line, at most 1000 characters")
            value = value.strip()
            if value and not (value.startswith("/") or value == "~" or value.startswith("~/")):
                raise ConsoleError(400, f"{var}: an absolute path on the cluster (or empty for the default)")
            args.append(f"{var}={value or 'default'}")
        call = ('for kv in "$@"; do _hpclib_pytools_set "${kv%%=*}" "${kv#*=}" || exit 3; done; '
                'hpclib_pytools --json')
        return self._pytools_call(profile, call, args)

    def install_python_tool(self, profile, body):
        """{"tool": "uv"|"pixi"}: its official installer, over the login (the login node needs the internet)."""
        tool = (body or {}).get("tool") if isinstance(body, dict) else None
        if tool not in ("uv", "pixi"):
            raise ConsoleError(400, 'send {"tool": "uv"} or {"tool": "pixi"}')
        call = ('out=$(_hpclib_pytools_install "$1" 2>&1); code=$?; printf "%s\\n" "$out" | tail -n 40 | '
                'sed "s/^/HPCLIB_PYTOOLS_LOG /"; hpclib_pytools --json; exit $code')
        return self._pytools_call(profile, call, (tool,), timeout=self.PYTOOLS_INSTALL_TIMEOUT, install=tool)

    def _pytools_call(self, profile, call, args, timeout=None, install=None):
        name = profile["name"]
        if not self.logins.alive(profile):
            raise ConsoleError(409, f"log in to {name} first: uv and pixi are checked over the console's login")
        code, out, err = self.script_runner(profile, pytools_script(call), tuple(args), timeout or self.PYTOOLS_TIMEOUT)
        found = re.search(r"^HPCLIB_PYTOOLS (\{.*\})\s*$", out or "", re.M)
        try:
            result = json.loads(found.group(1)) if found else None
        except ValueError:
            result = None
        log = [self._one_line(m) for m in re.findall(r"^HPCLIB_PYTOOLS_LOG (.*)$", out or "", re.M)]
        if result is None or (code and not install):
            detail = self._one_line((err or out or "").strip()[-300:]) if (err or out or "").strip() else f"exit {code}"
            raise ConsoleError(400 if code == 3 or result is not None else 502, f"uv and pixi on {name}: {detail}")
        if install:
            result["install"] = {"tool": install, "ok": code == 0, "exit_code": code, "log": log}
            if code:
                result["install"]["error"] = (log[-1] if log else self._one_line(err or "")) or f"exit {code}"
        return result

    # -- leftover tunnel ports on the cluster's login nodes ---------------------

    def sandbox_tools(self, profile, body=None):
        """Report or install the user's sandbox runtimes over the existing trusted login."""
        body = {} if body is None else body
        if not isinstance(body, dict) or set(body) - {"tool", "modules"}:
            raise ConsoleError(400, 'send {"tool": "bubblewrap"|"charliecloud", "modules": [NAME, ...]}')
        tool = body.get("tool")
        if tool is not None and tool not in ("bubblewrap", "charliecloud"):
            raise ConsoleError(400, "tool must be bubblewrap or charliecloud")
        modules = body.get("modules", [])
        if (not isinstance(modules, list) or len(modules) > 20 or
                any(not isinstance(m, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+/@:-]{0,127}", m) for m in modules)):
            raise ConsoleError(400, "modules must be a list of module names")
        if not self.logins.alive(profile):
            raise ConsoleError(409, "Log in first: sandbox tools are installed through the console's cluster login")
        with open(SANDBOX_TOOLS_PY) as source:
            code = source.read()
        prelude = ""
        if modules:
            prelude = ('type module >/dev/null 2>&1 || { echo "Load the site module initialization first" >&2; exit 3; }\n'
                       'module load ' + " ".join(shlex.quote(m) for m in modules) + ' || exit 3\n')
        args = " --install " + shlex.quote(tool) if tool else ""
        script = prelude + "python3 -" + args + " <<'HPCLIB_SANDBOX_INSTALLER'\n" + code + "\nHPCLIB_SANDBOX_INSTALLER\n"
        status, out, err = self.script_runner(profile, script, (), 900 if tool else 45)
        match = re.search(r"^HPCLIB_SANDBOX_TOOLS (\{.*\})\s*$", out or "", re.M)
        try:
            result = json.loads(match.group(1)) if match else None
        except ValueError:
            result = None
        if result is None:
            raise ConsoleError(502, "Sandbox tools: " + self._one_line((err or out or f"exit {status}")[-2000:]))
        if tool:
            result["install"] = {"tool": tool, "ok": status == 0 and result.get("ok") is True,
                                 "error": result.get("error")}
        elif status:
            raise ConsoleError(502, result.get("error") or "Could not inspect sandbox tools")
        return result

    PORT_LINE_RE = re.compile(r"^HPCLIB_PORT_(HERE|CLEARED|BUSY|PENDING) ([A-Za-z0-9][A-Za-z0-9._-]{0,62})(?: ([0-9]{1,5}))?\s*$", re.M)
    CLEAR_LOGINS_MAX = 10

    def login_ports(self, profile):
        """The ports this console's tunnels use on the login nodes: the agents' and each app's, with whether it runs."""
        name = profile["name"]
        with self.lock:
            agent_proc = self.procs.get(name)
            app_procs = {app: p for (n, app), p in self.app_procs.items() if n == name}
        out = []
        if profile.get("port"):
            running = (agent_proc is not None and agent_proc.poll() is None) or self.port_open(profile["port"])
            out.append({"port": int(profile["port"]), "what": "agent tunnel", "app": None, "running": running})
        fresh = agent_profiles.load(name) or profile
        for app, conf in sorted((fresh.get("apps") or {}).items()):
            if not conf.get("port"):
                continue
            proc = app_procs.get(app)
            running = (proc is not None and proc.poll() is None) or self.port_open(conf["port"])
            title = APPS[app]["title"] if app in APPS else app
            out.append({"port": int(conf["port"]), "what": title, "app": app, "running": running})
        return out

    def clear_ports(self, profile, body):
        """
        clear_tunnel_ports over the login for the ports of tunnels that aren't running; then, with fresh_logins,
        more logins until each node the login node couldn't reach has been reached (see _clear_ports_elsewhere).
        """
        if set(body) - {"fresh_logins", "password", "max_logins"}:
            raise ConsoleError(400, 'send {"fresh_logins": true|false, "password"?, "max_logins"?}')
        fresh = body.get("fresh_logins", False)
        password = body.get("password")
        limit = body.get("max_logins", self.CLEAR_LOGINS_MAX)
        if not isinstance(fresh, bool):
            raise ConsoleError(422, "fresh_logins: true or false")
        if password is not None and (not isinstance(password, str) or len(password) > 1024 or "\n" in password):
            raise ConsoleError(422, "`password` must be one line of text")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.CLEAR_LOGINS_MAX:
            raise ConsoleError(422, f"max_logins: 1 to {self.CLEAR_LOGINS_MAX}")
        ports = self.login_ports(profile)
        idle = [p["port"] for p in ports if not p["running"]]
        if not idle:
            raise ConsoleError(409, "every tunnel of this cluster's is running; stop the ones whose ports you want "
                                    "cleared (a running tunnel's port is left alone)")
        args = ["clear_tunnel_ports"] + [a for p in idle for a in ("-P", str(p))] + ssh_options(profile) + [profile["host"]]
        name = profile["name"]
        with self.lock:
            self._clear_requests[name] = {"fresh_logins": fresh, "password": password or None, "max_logins": limit}
        try:
            record = self.run_operation(profile, "clear_ports", args, title="clear leftover ports")
        except BaseException:
            with self.lock:
                self._clear_requests.pop(name, None)
            raise
        return dict(record, skipped=[p for p in ports if p["running"]], ports=idle)

    def _clear_ports_script(self):
        """The script clear_tunnel_ports sends, from this machine's hpclib (for the extra logins)."""
        res = subprocess.run(self.hpclib_argv(["_hpclib_clear_ports_script"]), capture_output=True, text=True,
                             timeout=30, env=dict(os.environ, **self.HPCLIB_ENV), stdin=subprocess.DEVNULL)
        if res.returncode != 0 or "_hpclib_clear_ports" not in res.stdout:
            raise ConsoleError(500, "could not make the clearing script from this machine's hpclib")
        return res.stdout

    @staticmethod
    def _login_addresses(host):
        """The addresses the login name has (several when the site rotates logins through DNS), or []."""
        name = host.rsplit("@", 1)[-1]
        try:
            infos = socket.getaddrinfo(name, 22, type=socket.SOCK_STREAM)
        except OSError:
            return []
        seen = []
        for family, _, _, _, addr in sorted(infos, key=lambda i: i[0] != socket.AF_INET):
            if addr[0] not in seen:
                seen.append(addr[0])
        return seen

    def _clear_ports_elsewhere(self, profile, pending, request, log):
        """
        Log in again, each time as a login of its own, until every node in `pending` ({node: {ports}}) has been
        landed on and cleared there (--no-hop), or `max_logins` logins: first one per address of the login name (a
        site that rotates its login nodes through DNS gives each its own), then the name itself. Stops at a
        refused password, push or typed code. Returns the nodes still pending.
        """
        script = self._clear_ports_script()
        host = profile["host"].rsplit("@", 1)[-1]
        addresses = self._login_addresses(profile["host"])
        targets = (addresses if len(addresses) > 1 else []) + [None] * self.CLEAR_LOGINS_MAX
        password = request.get("password")
        for attempt, address in enumerate(targets[:request["max_logins"]], 1):
            if not pending:
                break
            ports = sorted({p for ps in pending.values() for p in ps})
            extra = ["-o", f"HostName={address}", "-o", f"HostKeyAlias={host}"] if address else []
            log.write(f"login {attempt} of at most {request['max_logins']}{' via ' + address if address else ''}, "
                      f"for {', '.join(sorted(pending))}: approve the Duo push if one comes\n".encode())
            code, out, message = self.logins.run_fresh(
                profile, password, shlex.join(["bash", "-s", "--", "--no-hop"] + [str(p) for p in ports]), script, extra)
            lines = self.PORT_LINE_RE.findall(out)
            here = next((node for kind, node, _ in lines if kind == "HERE"), None)
            if here is None:
                log.write(f"  {self._one_line(message)}\n".encode())
                if message != "logged in" and not message.startswith("ssh failed"):
                    log.write(b"  stopping: the login itself was refused\n")
                    break
                continue
            cleared = {int(port) for kind, node, port in lines if kind == "CLEARED" and node == here and port}
            if here in pending:
                pending[here] -= cleared
                if not pending[here]:
                    del pending[here]
                    log.write(f"  landed on {here}: cleared {', '.join(map(str, sorted(cleared)))}\n".encode())
                else:
                    log.write(f"  landed on {here}: still in use there: "
                              f"{', '.join(map(str, sorted(pending[here])))}\n".encode())
            else:
                log.write(f"  landed on {here}, which was already done\n".encode())
        return pending

    def _finish_clear_ports(self, profile, record, code):
        name = profile["name"]
        with self.lock:
            request = self._clear_requests.pop(name, None) or {"fresh_logins": False, "max_logins": 0}
        try:
            lines = self.PORT_LINE_RE.findall(self._operation_text(profile, record))
            pending, busy = {}, []
            for kind, node, port in lines:
                if kind == "PENDING" and port:
                    pending.setdefault(node, set()).add(int(port))
                elif kind == "BUSY" and port:
                    busy.append(f"{node}:{port}")
            with open(self.ops_log_path(name), "ab", buffering=0) as log:
                if code == 0 and pending and request["fresh_logins"]:
                    try:
                        pending = self._clear_ports_elsewhere(profile, pending, request, log)
                    except ConsoleError as e:
                        log.write(f"clear_tunnel_ports: {e}\n".encode())
                if code != 0 or not lines:
                    log.write(b"clear_tunnel_ports did not finish on the login node; see above\n")
                    return code or 1
                left = [f"{node}:{port}" for node, ports in sorted(pending.items()) for port in sorted(ports)]
                if left:
                    log.write(f"HPCLIB_PORTS_LEFT {' '.join(left)}\n".encode())
                    log.write((f"still to clear on {', '.join(sorted(pending))}: "
                               + ("the logins didn't land there" if request["fresh_logins"] else
                                  "allow extra logins to reach those nodes") + "\n").encode())
                if busy:
                    log.write(f"in use by something that isn't one of your tunnels: {', '.join(busy)}\n".encode())
                if not left and not busy:
                    log.write(b"every port is clear on every login node that had a record of it\n")
                return 0 if not left and not busy else 1
        finally:
            request["password"] = None

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
        unknown = set(body) - {"work_dirs", "binds", "local_roots", "templates", "rebuild", "repair_owner_token", "mcp_python",
                               "mcp_command", "mcp_args", "remote_python", "python_modules"}
        if unknown:
            raise ConsoleError(400, f"unknown setup options {sorted(unknown)}")
        repair = body.get("repair_owner_token", False)
        if not isinstance(repair, bool):
            raise ConsoleError(400, "repair_owner_token must be true or false")
        if repair:
            if set(body) != {"repair_owner_token"}:
                raise ConsoleError(400, "owner recovery cannot be combined with other setup options")
            if not profile.get("work_dirs"):
                raise ConsoleError(422, "set this cluster up before recovering owner access")
            return self.run_operation(profile, "setup", ["setup_agents", "--repair-owner-token", profile["name"]],
                                      title="Recover owner access")
        args = ["setup_agents"]
        # the MCP server's launcher: a command and the arguments before rest_mcp.py (mcp_python: the older name)
        command = body.get("mcp_command", body.get("mcp_python"))
        if command is not None:
            args += ["--mcp-command", self._mcp_python(command)]
        if "mcp_args" in body:
            args += ["--mcp-args", shlex.join(self._mcp_args(body["mcp_args"]))]
        modules = body.get("python_modules") or []
        if not isinstance(modules, list) or len(modules) > 8 or not all(
                isinstance(m, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+/-]{0,127}", m) for m in modules):
            raise ConsoleError(422, "python_modules: module names, such as Python/3.11.5-GCCcore-13.2.0")
        if modules:
            args += [a for m in modules for a in ("--python-module", m)]
        elif "remote_python" in body:
            value = body["remote_python"]
            if not isinstance(value, str) or (value.strip() and not CLUSTER_PATH_RE.fullmatch(value.strip())):
                raise ConsoleError(422, "remote_python: an absolute path on the cluster, or empty to find one")
            args += ["--remote-python", value.strip() or "auto"]
        if "local_roots" in body:
            args += [a for d in self._local_roots(body["local_roots"]) for a in ("--local-root", d)] or ["--no-local-root"]
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

    @staticmethod
    def _mcp_args(value):
        """The launch arguments before rest_mcp.py: a list, or one string split like a shell's (quotes allowed)."""
        if isinstance(value, str):
            try:
                value = shlex.split(value)
            except ValueError as e:
                raise ConsoleError(422, f"mcp_args: {e}") from e
        if not isinstance(value, list) or len(value) > 32 or not all(
                isinstance(a, str) and a and len(a) <= 1024 and not re.search(r"[\0\n\r]", a) for a in value):
            raise ConsoleError(422, "mcp_args: the arguments before rest_mcp.py, separated by spaces")
        return value

    @staticmethod
    def _mcp_python(value):
        """The command that starts the cluster's MCP server here (a Python, or a launcher such as /usr/bin/arch):
        a program on this machine ("" finds a Python: "auto")."""
        if not isinstance(value, str) or len(value) > 1024 or re.search(r"[\0\n\r]", value):
            raise ConsoleError(422, "mcp_command: a program on this machine, or empty to find a Python")
        value = value.strip()
        if not value:
            return "auto"
        if re.match(r"[A-Za-z]:[\\/]", value) and local_platform.platform() == "wsl":
            value = local_platform.to_linux(value) or value
        path = os.path.expanduser(value) if os.sep in value or value.startswith("~") else shutil.which(value)
        if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise ConsoleError(422, f"mcp_command: {value} isn't a program on this machine")
        return os.path.abspath(path)

    @staticmethod
    def _local_roots(values):
        """Folders on this machine for the agent's push_files/pull_files: full paths (~/ is your home; under WSL a
        Windows path C:\\... means that folder), created if missing."""
        if not isinstance(values, list) or len(values) > 16:
            raise ConsoleError(422, "local_roots is a list of folders on this machine")
        out = []
        for v in values:
            if not isinstance(v, str) or not v.strip() or len(v) > 1024 or re.search(r"[\0\n\r]", v):
                raise ConsoleError(422, "local_roots: full paths of folders on this machine")
            v = v.strip()
            if re.match(r"[A-Za-z]:[\\/]", v) and local_platform.platform() == "wsl":
                v = local_platform.to_linux(v) or v
            v = os.path.expanduser(v)
            if not os.path.isabs(v):
                raise ConsoleError(422, f"local_roots: {v} isn't a full path (/... or ~/...)")
            try:
                os.makedirs(v, exist_ok=True)
            except OSError as e:
                raise ConsoleError(422, f"local_roots: can't create {v} ({e.strerror})") from e
            out.append(os.path.realpath(v))
        return out

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
        # this machine's defaults for new clusters (Console Settings); a preset's own values replace them
        prefs = console_prefs.load(console_dir())
        fresh = agent_profiles.load(name)
        fresh["connection_hours"] = prefs["new_cluster"]["connection_hours"]
        pattern = prefs["new_cluster"].get("mcp_name_pattern") or agent_profiles.DEFAULT_MCP_PATTERN
        taken = {p.get("mcp_name") for p in agent_profiles.all_profiles() if p["name"] != name}
        # not a name an LLM client already uses for something else
        first = agent_profiles.mcp_name_for(host, taken, pattern)
        if not self._owned_mcp_name(first) and any(mcp_clients.has_other(cid, first, {"command": None, "args": None})
                                                   for cid in mcp_clients.CLIENTS):
            taken.add(first)
        mcp_name = agent_profiles.mcp_name_for(host, taken, pattern)
        if mcp_name != fresh.get("mcp_name"):
            fresh["mcp_name"] = mcp_name
        config = console_prefs.cluster_config(prefs)
        if config is not None:
            fresh["setup_config"] = config
        agent_profiles.save(fresh)
        return self.describe(fresh, tunnel=False)

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
            fresh["setup_config"] = console_prefs.cluster_config(console_prefs.load(console_dir()), config)
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
                "install_modules": (conf.get("install_modules") if conf.get("install_modules") is not None
                                    else spec.get("install_modules", [])),
                "install_modules_default": spec.get("install_modules", []),
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
        unknown = set(body) - {"tunnel_args", "conda_env", "modules", "install_modules", "project", "settings"}
        if unknown:
            raise ConsoleError(400, f"unknown settings {sorted(unknown)}")
        if "tunnel_args" in body:
            args = body["tunnel_args"]
            if not isinstance(args, list) or not all(isinstance(a, str) and agent_profiles.TUNNEL_ARG_RE.fullmatch(a)
                                                     for a in args):
                raise ConsoleError(422, "tunnel_args must be sbatch options like --time=8:00:00 or --mem=16gb "
                                        "(time, mem, partition, account, qos, cpus-per-task, constraint, nodelist)")
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
        if "install_modules" in body:
            try:
                conf["install_modules"] = console_packages.check_install_modules(body["install_modules"],
                                                                               allow_default=True)
            except console_packages.PackageError as e:
                raise ConsoleError(422, str(e)) from e
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
        return self.tunnel_installable(APPS[app]["tunnel"])

    def tunnel_installable(self, tunnel):
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
        # Saved with all other tunnel settings so SSH and the REST installation use the same modules.
        # An explicit [] clears the package's defaults; None (or an absent field) restores them.
        if self.tunnel_installable(spec["tunnel"]):
            mods = conf.get("install_modules")
            out["HPCLIB_INSTALL_MODULES"] = ":".join(spec.get("install_modules", []) if mods is None else mods)
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
        if not spec.get("settings") and "env_settings" not in spec and not self.app_installable(app):
            return args + list(actions)            # no settings of its own: the cluster's stay as they are
        for k, v in sorted(self.app_vars(spec, conf).items()):
            args += ["--set", f"{k}={v}"]
        return args + ["--save"] + list(actions)

    # -- packages: apps and settings installed from zip files (console_packages) ---------------------

    def packages_list(self):
        return [{"name": m["name"], "version": m["version"], "description": m["description"],
                 "installed": m.get("installed"), "sha256": m.get("sha256"), "apps": sorted(m["apps"]),
                 "summary": console_packages.summary(m)} for m, _ in self.packages.installed()]

    # packages that come with hpclib's clone (packages/NAME: the Workspace), installed like any other
    BUNDLED_DIR = os.path.join(os.path.dirname(HPCLIB_DIR), "packages")

    def bundled_packages(self):
        installed = {m["name"]: m["version"] for m, _ in self.packages.installed()}
        out = []
        try:
            names = sorted(os.listdir(self.BUNDLED_DIR))
        except OSError:
            names = []
        for n in names:
            try:
                with open(os.path.join(self.BUNDLED_DIR, n, console_packages.MANIFEST)) as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(m, dict) and m.get("name") == n:
                out.append({"name": n, "version": m.get("version"), "description": m.get("description"),
                            "installed": installed.get(n)})
        return out

    def bundled_add(self, name, dry_run):
        if name not in {p["name"] for p in self.bundled_packages()}:
            raise ConsoleError(404, f"no package {name!r} comes with this hpclib")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                out, _ = console_packages.build(os.path.join(self.BUNDLED_DIR, name), os.path.join(tmp, "p.zip"))
            except console_packages.PackageError as e:
                raise ConsoleError(422, str(e)) from e
            with open(out, "rb") as f:
                data = f.read()
        return self.package_add(data, dry_run)

    def widgets(self):
        try:
            return self.packages.widgets()
        except console_packages.PackageError as e:
            raise ConsoleError(500, str(e)) from e

    def widget_html(self, package, widget_id):
        try:
            return {"html": self.packages.widget_html(package, widget_id)}
        except (console_packages.PackageError, OSError) as e:
            raise ConsoleError(404, f"no widget {package}/{widget_id}") from e

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
        # setup_tunnel.sh prints the marker only as it exits 3; anything else (tokens.sh's own output) is no request
        if elsewhere and text.rstrip().endswith("(exit 3)"):
            text = self._tokens_via_rest(profile, spec, args, self._one_line(elsewhere[-1].strip()))
        return spec, text

    def _tokens_via_rest(self, profile, spec, args, reason):
        needs = (f"{spec['title']}'s tokens can't be managed on {profile['name']}'s login node ({reason}), so they "
                 f"are managed where its REST server runs, through the agent tunnel")
        if self.tunnel_state(profile)["state"] != "up":
            raise ConsoleError(409, f"{needs}: start the agent tunnel first (Agents → Clusters → Start)")
        if not read_private(profile.get("owner_token_file") or ""):
            raise ConsoleError(409, f"{needs}, with the owner token, which this console doesn't have for it: "
                                    f"use Setup → Recover owner access")
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
            raise ConsoleError(502, f"{needs}: {self._one_line(out.get('error', f'HTTP {status}'))}")
        text = str(out.get("stdout", "")) + "\n" + str(out.get("stderr", ""))
        if out.get("exit_code") == 3:
            node = out.get("node") if isinstance(out.get("node"), str) and self.NODE_RE.fullmatch(out["node"]) else None
            last = [l for l in str(out.get("stderr", "")).splitlines() if l.strip()]
            raise ConsoleError(502, f"{needs}, but it can't run on {node or 'that node'} either"
                                    + (f": {self._one_line(last[-1])}" if last else ""))
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
            instances = state.get("registered_instances") or ([inst] if inst else [])
            for item in instances:
                job = item["job"]
                how = item.get("how", "registered")
                detail = ("this tunnel started it, and it keeps running after Stop" if how == "started" else
                          "another tunnel started it; Stop only disconnects" if how == "attached" else
                          f"registered on the cluster ({item['state']}); Stop only disconnects")
                tools.append({"id": "instance" if len(instances) == 1 else f"instance-{job}",
                              "kind": "info", "label": what.capitalize(),
                              "text": f"job {job} on {item['node']}, port {item['port']}; " + detail})
                controls.append({"id": "stop_instance", "key": f"stop_instance:{job}",
                                 "label": f"End {what} job" + (f" {job}" if len(instances) > 1 else ""),
                                 "title": f"scancel {job}: ends the {what} for every tunnel using it",
                                 "confirm": f"End job {job}, the {what} on {item['node']}? Every tunnel "
                                            f"connected to it loses it, and this tunnel stops too.",
                                 "args": {"job": job}})
            if not instances:
                text = (f"instance discovery failed: {state['instance_error']}" if state.get("instance_error") else
                        f"no registered {what} jobs on the cluster" if "registered_instances" in state else
                        "checking the cluster's instance registry" if state.get("instance_checking") else
                        f"none seen yet: Start connects to a running {what} job, or starts one")
                tools.append({"id": "instance", "kind": "info", "label": what.capitalize(),
                              "text": text})
            controls.append({"id": "refresh_instances", "label": "Refresh instances",
                             "disabled": bool(state.get("instance_checking"))})
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
                             "encoding) so sync jobs can sign in without prompting."),
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
        if auth.get("auth") == "password":
            text = ("a saved password is used by smbshell and sync jobs" if auth.get("credentials") else
                    "password sign-in selected: save a password for sync jobs (smbshell asks for it otherwise)")
            tools.append({"id": "signin", "kind": "info", "label": "Signing in", "text": text})
            controls.append({"id": "forget_credentials", "label": "Forget saved password",
                             "confirm": "Remove the password saved on the cluster? Sync jobs can't sign in "
                                        "without it while password sign-in is selected."} if auth.get("credentials") else
                            {"id": "save_credentials", "label": "Save password for sync jobs", "primary": True})
            controls.append({"id": "status", "label": "Refresh"})
            return tools, controls
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
                               [profile["host"], spec["tunnel"], "--instances"], wait, check=True)
        return [{"job": j, "node": n, "port": int(p), "state": st, "owner": u}
                for j, n, p, st, u in INSTANCE_LINE_RE.findall(text)]

    def recover_app_instances(self, profile, app):
        """Recover shared jobs from the cluster registry, independently of a local tunnel/log tail."""
        if not self.logins.alive(profile):
            return {"error": "log in to the cluster to discover running instances"}
        key = (profile["name"], app)
        now = time.monotonic()
        with self.lock:
            cached = self.instance_queries.get(key)
            if cached and now - cached["checked"] < 30:
                return cached
            self.instance_queries[key] = {"checked": now, "pending": True}
        try:
            instances = self.app_instances(profile, app, wait=5)
            out = {"checked": time.monotonic(), "instances": instances}
        except ConsoleError as error:
            out = {"checked": time.monotonic(), "error": str(error)}
        with self.lock:
            self.instance_queries[key] = out
        return out

    def _run_setup(self, profile, args, wait, check=False):
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
        if check and code != 0:
            raise ConsoleError(502, f"could not query {profile['name']} (exit {code}); check its login and hpclib installation")
        return text if code == 0 else text + f"\n(exit {code})"

    def app_control(self, profile, app, control, body):
        """One of the row's extra controls."""
        spec = self.app_spec(app)
        if spec.get("smb"):
            return self.transfer_control(profile, app, control)
        if control == "refresh_instances" and spec.get("shared"):
            if body:
                raise ConsoleError(400, "refresh_instances takes an empty object")
            instances = self.app_instances(profile, app, wait=5)
            with self.lock:
                self.instance_queries[(profile["name"], app)] = {"checked": time.monotonic(), "instances": instances}
            return {"instances": instances, "message": "Checked the cluster's running instances."}
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
        with self.lock:
            self.instance_queries.pop((profile["name"], app), None)
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
        with self.lock:
            queried_instances = (profile["name"], app) in self.instance_queries
        if APPS[app].get("shared") and (not state.get("instance") or queried_instances):
            recovered = self.recover_app_instances(profile, app)
            if "instances" in recovered:
                hint = state.get("instance") or {}
                instances = [dict(item, how=hint.get("how", "registered") if item["job"] == hint.get("job")
                                  else "registered") for item in recovered["instances"]]
                state["registered_instances"] = instances
                state["instance"] = next((item for item in instances if item["job"] == hint.get("job")),
                                         instances[0] if len(instances) == 1 else None)
            if recovered.get("error"):
                state["instance_error"] = recovered["error"]
            if recovered.get("pending"):
                state["instance_checking"] = True
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
        # The stock GUI's login_token (and the older RCLONE marker) identifies rclone's RC API.
        # Other SMB_GUI programs retain their ordinary Open link.
        prepare_open = which == "RCLONE" or "login_token" in urllib.parse.parse_qs(query or "")
        return dict(url=url, prepare_open=prepare_open,
                    **({"username": user, "password": password} if user else {}))

    def app_open(self, profile, app):
        """Prepare Data transfer's GUI on an explicit Open click, never on a state/health poll."""
        spec = self.app_spec(app)
        if not spec.get("smb"):
            raise ConsoleError(404, f"{spec['title']} has no Open preparation")
        state = self._app_state(profile, app)
        if state["state"] != "up" or not state.get("url"):
            raise ConsoleError(409, f"start {spec['title']} on {profile['name']} before opening it")
        if not state.get("prepare_open"):
            return {"url": state["url"], "cache_cleared": False}
        if not state.get("username") or not state.get("password"):
            raise ConsoleError(409, "Data transfer has not supplied its GUI login yet; see Log")
        auth = base64.b64encode(f"{state['username']}:{state['password']}".encode()).decode()

        def rc(route):
            # A fixed loopback destination, with no proxies or redirects. Credentials stay in memory.
            conn = http.client.HTTPConnection("127.0.0.1", state["port"], timeout=5)
            try:
                conn.request("POST", "/" + route, body=b"{}",
                             headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
                res = conn.getresponse()
                if res.status != 200:
                    raise ConsoleError(502, f"Data transfer cache refresh failed at {route} (HTTP {res.status}); "
                                            "the app was not opened")
                data = json.loads(res.read(1024 * 1024))
                if not isinstance(data, dict):
                    raise ValueError("expected an object")
                return data
            except (OSError, http.client.HTTPException, ValueError):
                raise ConsoleError(502, f"Data transfer cache refresh failed at {route}; "
                                        "the app was not opened") from None
            finally:
                conn.close()

        # Clearing the backend cache may close SMB connections. Check aggregate activity first.
        stats = rc("core/stats")
        if stats.get("transferring") or stats.get("checking"):
            raise ConsoleError(409, "Data transfer is busy transferring or checking files; "
                                    "wait for it to finish, then Open again")
        rc("fscache/clear")
        return {"url": state["url"], "cache_cleared": True}

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
        self.stops[(name, app)] = time.time()
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
        if code != 0:
            raise ConsoleError(502, f"stopping {APPS[app]['title']} did not finish (exit {code}); see its log and retry Stop",
                               stop_exit=code)
        with self.lock:
            proc = self.app_procs.pop((name, app), None)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        return {"stopped": name, "app": app, "stop_exit": code}

    def app_log(self, name, app, lines=200, latest=False):
        self.app_spec(app)
        if latest:
            return {"copy_text": last_log_block(self.app_log_path(name, app))}
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
        self.stops[(name, None)] = time.time()
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
        if code != 0:
            raise ConsoleError(502, f"stopping the agent tunnel did not finish (exit {code}); see its log and retry Stop",
                               agent_stop_exit=code)
        with self.lock:
            proc = self.procs.pop(name, None)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        return {"stopped": name, "agent_stop_exit": code}

    def tail_log(self, name, lines=200, latest=False):
        path = self.log_path(name)
        if latest:
            return {"cluster": name, "copy_text": last_log_block(path)}
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - (256 << 10)))
                text = f.read().decode(errors="replace")
        except FileNotFoundError:
            return {"cluster": name, "lines": [], "path": path}
        return {"cluster": name, "lines": text.splitlines()[-lines:], "path": path}

    # -- this machine: ports, LLM clients, notifications, diagnostics --------

    def _tunnel_running(self, profile, app):
        """Whether the tunnel (agent: app None) holds its port: started here, or answering behind it."""
        name = profile["name"]
        with self.lock:
            proc = self.app_procs.get((name, app)) if app else self.procs.get(name)
        if proc is not None and proc.poll() is None:
            return True
        if app is None:
            return self.tunnel_state(profile)["state"] in ("up", "starting")
        if app not in APPS:
            return False
        try:
            return self._app_state(profile, app)["state"] in ("up", "queued", "starting")
        except Exception:       # noqa: BLE001  (a removed app, an unreadable profile: not running here)
            return False

    def ports_report(self):
        rows, running = [], {}
        for profile, app, which, port in agent_profiles.all_ports():
            key = (profile["name"], app)
            title = ("agent tunnel" if app is None else
                     APPS[app]["title"] if app in APPS else f"{app} (no longer installed)")
            free = agent_profiles.port_free(port)
            if not free and key not in running:
                running[key] = self._tunnel_running(profile, app)
            status = "free" if free else ("tunnel" if running[key] else "taken")
            rows.append({"cluster": profile["name"], "app": app, "title": title, "which": which, "port": port,
                         "status": status})
        lo, hi = agent_profiles.port_range()
        return {"range": [lo, hi], "default_range": list(agent_profiles.PORT_RANGE), "ports": rows}

    def set_port_range(self, body):
        try:
            if body.get("reset"):
                lo, hi = agent_profiles.set_port_range(None, None)
            else:
                lo, hi = agent_profiles.set_port_range(body.get("low"), body.get("high"))
        except ValueError as e:
            raise ConsoleError(422, str(e)) from e
        return {"range": [lo, hi]}

    def _write_mcp_port(self, profile):
        """
        mcp.json after the agent tunnel's port or MCP name changed: the entry under the profile's name, with its
        --url on the port (the rest of the entry stays).
        """
        path = os.path.join(agent_profiles.profile_dir(profile["name"]), "mcp.json")
        try:
            with open(path) as f:
                data = json.load(f)
            servers = data["mcpServers"]
            entry = servers.get(profile["mcp_name"]) or next(iter(servers.values()))
            args = entry["args"]
            i = args.index("--url")
            args[i + 1] = f"http://127.0.0.1:{profile['port']}"
        except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration, AttributeError):
            return False                 # not set up yet: setup_agents writes it with the new port and name
        data["mcpServers"] = {profile["mcp_name"]: entry}
        agent_profiles.write_private(path, json.dumps(data, indent=2) + "\n")
        return True

    def set_local_mcp(self, profile, body):
        """
        PUT /api/clusters/NAME/local: what runs the cluster's MCP server on this machine, with nothing on the
        cluster to change: the launch command ("" finds a Python) and the arguments before rest_mcp.py, and the
        folders push_files/pull_files may use. Saved in the profile; mcp.json is written again, and so are the
        LLM clients kept up to date (an entry with a launcher of its own stays as it is).
        """
        if not isinstance(body, dict) or not body or set(body) - {"command", "args", "local_roots"}:
            raise ConsoleError(400, 'send {"command", "args", "local_roots"} (any of them)')
        name = profile["name"]
        fresh = agent_profiles.load(name) or profile
        if "command" in body:
            command = self._mcp_python(body["command"])
            fresh["mcp_command"] = "" if command == "auto" else command
            fresh.pop("mcp_python", None)
            if "args" not in body:
                fresh["mcp_args"] = []            # a new command doesn't keep another's arguments
        if "args" in body:
            fresh["mcp_args"] = self._mcp_args(body["args"])
        if "local_roots" in body:
            fresh["local_roots"] = self._local_roots(body["local_roots"])
            fresh["local_roots_off"] = "no" if fresh["local_roots"] else "yes"
        path = os.path.join(agent_profiles.profile_dir(name), "mcp.json")
        rest_mcp = os.path.join(HPCLIB_DIR, "servers", "rest_mcp.py")
        try:
            with open(path) as f:
                old = json.load(f)["mcpServers"][profile["mcp_name"]]
            rest_mcp = next(a for a in old["args"] if os.path.basename(a) == "rest_mcp.py")   # setup's clone
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            pass
        python = fresh.get("mcp_command") or agent_profiles.find_mcp_python()
        agent_profiles.save(fresh)
        data = agent_profiles.mcp_entry(fresh, python, rest_mcp)
        agent_profiles.write_private(path, json.dumps(data, indent=2) + "\n")
        return {"entry": data["mcpServers"][fresh["mcp_name"]], "llm_clients": self.sync_llm_clients(),
                "local_roots": fresh.get("local_roots") or []}

    def remove_cluster(self, profile, body):
        """
        DELETE /api/clusters/NAME: forget a cluster here. Optionally revoke its agent token on the cluster first
        (needs the agent tunnel up and the owner token), then stop its tunnels (agent and apps), log out, take its
        MCP entries out of the LLM clients, and move its profile (login, ports, tokens, mcp.json) to
        agents/.removed/, from where it can be moved back. Nothing else on the cluster changes.
        """
        body = body or {}
        options = {"revoke_token": False, "stop_tunnels": True, "log_out": True, "llm_clients": True}
        if set(body) - set(options) or not all(isinstance(v, bool) for v in body.values()):
            raise ConsoleError(400, f"options are true or false: {', '.join(options)}")
        options.update(body)
        name = profile["name"]
        with self.lock:
            busy = [(self.ops.get(name) or {}).get("kind")] if (self.ops.get(name) or {}).get("state") == "running" else []
            if (self.storage_scans.get(name) or {}).get("state") == "running":
                busy.append("storage scan")
        attempt = self.logins.attempts.get(name) or {}
        if attempt.get("state") in ("starting", "password_sent", "push_sent", "code_required"):
            busy.append("login")
        if busy:
            raise ConsoleError(409, f"wait for {name}'s {' and '.join(busy)} to finish first")
        out = {"removed": name, "warnings": []}
        if options["revoke_token"]:
            # first, and stopping if it fails: once the profile is gone, nothing here can reach the server to do it
            try:
                status, _, content = self.call(profile, "POST", "/admin/tokens/revoke",
                                               body=json.dumps({"name": profile.get("token_name")}).encode(),
                                               headers={"Content-Type": "application/json"}, timeout=30)
            except ConsoleError as e:
                raise ConsoleError(502, f"couldn't revoke the agent token ({e}); nothing was removed. Start the "
                                        f"agent tunnel, or uncheck Revoke to remove it anyway") from e
            if status not in (200, 404):
                detail = content.decode(errors="replace")[:300]
                raise ConsoleError(502, f"{name} didn't revoke the agent token (HTTP {status}: {detail}); nothing "
                                        f"was removed. Uncheck Revoke to remove it anyway")
            out["token_revoked"] = status == 200
        if options["stop_tunnels"]:
            fresh = agent_profiles.load(name) or profile
            for app, conf in sorted((fresh.get("apps") or {}).items()):
                with self.lock:
                    proc = self.app_procs.get((name, app))
                running = (proc is not None and proc.poll() is None) or (
                    isinstance(conf, dict) and conf.get("port") and self.port_open(conf["port"]))
                if running and app in APPS:
                    try:
                        self.app_stop(fresh, app)
                    except ConsoleError as e:
                        out["warnings"].append(f"{APPS[app]['title']}: {e}")
            with self.lock:
                proc = self.procs.get(name)
            if (proc is not None and proc.poll() is None) or (profile.get("port") and self.port_open(profile["port"])):
                try:
                    self.stop_tunnel(profile)
                    out["tunnel_stopped"] = True
                except ConsoleError as e:
                    out["warnings"].append(f"agent tunnel: {e}")
        if options["log_out"]:
            try:
                out["logged_out"] = self.logins.disconnect(profile).get("logged_out", False)
            except ConsoleError as e:
                out["warnings"].append(f"log out: {e}")
        if options["llm_clients"]:
            names = [n for n in [profile.get("mcp_name"), *(profile.get("previous_mcp_names") or [])] if n]
            cleaned = {}
            for cid in mcp_clients.CLIENTS:
                try:
                    if not mcp_clients.available(cid):
                        continue
                    servers, _ = mcp_clients._read_servers(mcp_clients.CLIENTS[cid]["path"]())
                    for n in names:
                        if n in servers:
                            mcp_clients.remove(cid, n)
                            cleaned.setdefault(cid, []).append(n)
                except (mcp_clients.ClientError, OSError) as e:
                    out["warnings"].append(f"{mcp_clients.CLIENTS[cid]['title']}: {e}")
            out["llm_clients"] = cleaned
        dest = agent_profiles.remove(name)
        if dest is None:
            raise ConsoleError(404, f"no agent profile {name!r}")
        out["kept_in"] = dest
        with self.lock:
            self.procs.pop(name, None)
            self.ops.pop(name, None)
            self.storage_scans.pop(name, None)
            for key in [k for k in self.app_procs if k[0] == name]:
                self.app_procs.pop(key, None)
        for path in (self.storage_path(name),):
            try:
                os.remove(path)
            except OSError:
                pass
        return out

    def rename_mcp(self, profile, body):
        """A new MCP name for the cluster: its mcp.json, and the LLM clients kept up to date (the old name goes)."""
        name = body.get("name")
        fresh = agent_profiles.load(profile["name"]) or profile
        old = fresh.get("mcp_name")
        try:
            agent_profiles.check_mcp_name(name, fresh["name"])
        except ValueError as e:
            raise ConsoleError(422, str(e)) from e
        if name == old:
            return {"mcp_name": name, "llm_clients": {}}
        mine = self._mcp_wanted().get(old) or {"command": None, "args": None}
        if not self._owned_mcp_name(name):
            for cid in mcp_clients.CLIENTS:
                if mcp_clients.has_other(cid, name, mine):
                    raise ConsoleError(409, f"{mcp_clients.CLIENTS[cid]['title']} already has an MCP server called "
                                            f"{name}; pick another name")
        agent_profiles.rename_mcp(fresh, name)
        agent_profiles.save(fresh)
        self._write_mcp_port(fresh)
        return {"mcp_name": name, "previous": old, "llm_clients": self.sync_llm_clients(drop=[old] if old else [])}

    APP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

    def set_app_view(self, profile, body):
        """{"pinned": [APP...], "hidden": [APP...]}: how the cluster's Apps tab and Overview list its apps (pinned:
        always shown; hidden: not listed; the rest in a collapsed "More apps"). Kept in the profile, this machine."""
        if not isinstance(body, dict) or set(body) - {"pinned", "hidden"}:
            raise ConsoleError(400, 'send {"pinned": [APP...], "hidden": [APP...]}')
        fresh = agent_profiles.load(profile["name"]) or profile
        view = dict(fresh.get("app_view") or {})
        for key in ("pinned", "hidden"):
            if key not in body:
                continue
            ids = body[key]
            if not isinstance(ids, list) or len(ids) > 200 or not all(isinstance(a, str) and self.APP_ID_RE.fullmatch(a)
                                                                      for a in ids):
                raise ConsoleError(400, f"{key}: a list of app ids")
            view[key] = list(dict.fromkeys(ids))
        clash = set(view.get("pinned") or []) & set(view.get("hidden") or [])
        if clash:
            raise ConsoleError(400, f"pinned and hidden at once: {', '.join(sorted(clash))}")
        fresh["app_view"] = {k: v for k, v in view.items() if v}
        agent_profiles.save(fresh)
        return {"app_view": fresh["app_view"]}

    def new_ports(self, profile, body):
        """Pick new local ports for the agent tunnel (app: null) or an app, which must be stopped."""
        app = body.get("app")
        name = profile["name"]
        if app is not None:
            self.app_spec(app)
        if self._tunnel_running(profile, app):
            what = "the agent tunnel" if app is None else APPS[app]["title"]
            raise ConsoleError(409, f"stop {what} on {name} first; its ports change only while it is stopped")
        fresh = agent_profiles.load(name) or profile
        if app is None:
            old = [fresh.get("port"), fresh.get("process_port")]
            fresh["port"] = agent_profiles.random_port(taken=old)
            fresh["process_port"] = agent_profiles.random_port(taken=old + [fresh["port"]])
            agent_profiles.save(fresh)
            out = {"cluster": name, "app": None, "port": fresh["port"], "process_port": fresh["process_port"],
                   "mcp_updated": self._write_mcp_port(fresh)}
            if out["mcp_updated"]:
                out["llm_clients"] = self.sync_llm_clients()
            return out
        conf = fresh.setdefault("apps", {}).setdefault(app, {})
        old = [conf.get("port"), conf.get("process_port")]
        conf["port"] = agent_profiles.random_port(taken=old)
        conf["process_port"] = agent_profiles.random_port(taken=old + [conf["port"]])
        agent_profiles.save(fresh)
        return {"cluster": name, "app": app, "port": conf["port"], "process_port": conf["process_port"]}

    @staticmethod
    def _mcp_wanted():
        return mcp_clients.wanted_entries(agent_profiles.all_profiles(), agent_profiles.profile_dir)

    @staticmethod
    def _owned_mcp_name(name):
        """Names the console may change in an LLM client: hpclib-*, and any a profile has or had."""
        if name.startswith("hpclib-"):
            return True
        return any(name == p.get("mcp_name") or name in (p.get("previous_mcp_names") or [])
                   for p in agent_profiles.all_profiles())

    def llm_clients(self):
        enabled = console_prefs.load(console_dir())["llm_clients"]
        wanted = self._mcp_wanted()
        return [dict(mcp_clients.status(cid, wanted, self._owned_mcp_name), enabled=enabled.get(cid, False))
                for cid in mcp_clients.CLIENTS]

    @staticmethod
    def _client(cid):
        if cid not in mcp_clients.CLIENTS:
            raise ConsoleError(404, f"no LLM client {cid!r}; the console knows {sorted(mcp_clients.CLIENTS)}")
        return cid

    def llm_client_write(self, cid, names=None, force=False):
        """Write the clusters' entries to one client (names: only these MCP names, e.g. the cluster just set up)."""
        self._client(cid)
        if names is not None and (not isinstance(names, list) or not all(isinstance(n, str) for n in names)):
            raise ConsoleError(400, "names: a list of MCP names")
        wanted = self._mcp_wanted()
        if names is not None:
            wanted = {n: e for n, e in wanted.items() if n in names}
            if not wanted:
                raise ConsoleError(404, f"no cluster has the MCP name{'s' if len(names) != 1 else ''} {', '.join(names)}")
        try:
            changed = mcp_clients.write(cid, wanted, force=bool(force))
        except (mcp_clients.ClientError, OSError) as e:
            raise ConsoleError(422, str(e)) from e
        return {"changed": changed, "client": mcp_clients.status(cid, self._mcp_wanted(), self._owned_mcp_name)}

    def cluster_llm_clients(self, profile):
        """One cluster's MCP entry in each LLM client (for the setup panel's Write entry), and whether the Python
        the entry runs has the MCP SDK."""
        name = profile.get("mcp_name")
        wanted = {n: e for n, e in self._mcp_wanted().items() if n == name}
        enabled = console_prefs.load(console_dir())["llm_clients"]
        clients = []
        for cid in mcp_clients.CLIENTS:
            st = mcp_clients.status(cid, wanted, self._owned_mcp_name)
            entry = next((e for e in st["entries"] if e["name"] == name), None)
            clients.append({"id": cid, "title": st["title"], "available": st["available"], "path": st["path"],
                            "error": st["error"], "windows": st.get("windows"), "after": st["after"],
                            "enabled": enabled.get(cid, False), "state": entry["state"] if entry else None,
                            "launcher": entry.get("launcher") if entry else None,
                            "launcher_argv": entry.get("launcher_argv") if entry else None})
        # the launcher (command and the arguments before rest_mcp.py), asked whether it can import the SDK
        launch = mcp_clients.launcher(wanted.get(name) or {})
        has_mcp = None
        if launch:
            try:
                has_mcp = subprocess.run([*launch, "-c", "import mcp"], stdin=subprocess.DEVNULL,
                                         capture_output=True, timeout=20).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                has_mcp = False
        fresh = agent_profiles.load(profile["name"]) or profile
        return {"mcp_name": name, "set_up": bool(wanted), "python": shlex.join(launch) if launch else None,
                "python_has_mcp": has_mcp, "clients": clients,
                "local": {"command": fresh.get("mcp_command") or fresh.get("mcp_python") or "",
                          "args": list(fresh.get("mcp_args") or []) if fresh.get("mcp_command") else [],
                          "local_roots": list(fresh.get("local_roots") or [])}}

    def llm_client_remove(self, cid, body):
        self._client(cid)
        name = body.get("name")
        st = mcp_clients.status(cid, self._mcp_wanted(), self._owned_mcp_name)
        if name not in st["stale"]:
            raise ConsoleError(422, "only an hpclib entry that no cluster has any more can be removed here")
        try:
            mcp_clients.remove(cid, name)
        except (mcp_clients.ClientError, OSError) as e:
            raise ConsoleError(422, str(e)) from e
        return {"removed": name, "client": mcp_clients.status(cid, self._mcp_wanted(), self._owned_mcp_name)}

    def sync_llm_clients(self, drop=()):
        """
        Write the entries to each enabled, installed client (first removing the drop names, a cluster's old MCP
        names): {client: [changed names] or 'error: ...'}.
        """
        enabled = console_prefs.load(console_dir())["llm_clients"]
        wanted = self._mcp_wanted()
        out = {}
        for cid, on in enabled.items():
            if not on or cid not in mcp_clients.CLIENTS or not mcp_clients.available(cid):
                continue
            try:
                st = mcp_clients.status(cid, wanted, self._owned_mcp_name)
                for old in drop:
                    if old in st["stale"]:
                        mcp_clients.remove(cid, old)
                out[cid] = mcp_clients.write(cid, wanted) or "up to date"
            except (mcp_clients.ClientError, OSError) as e:
                out[cid] = f"error: {e}"
        return out

    def watch(self):
        """Each tunnel's local port, open or not (cheap: no health calls), for the page's notifications."""
        out = []
        for p in agent_profiles.all_profiles():
            name = p["name"]
            with self.lock:
                proc = self.procs.get(name)
                procs = {a: self.app_procs.get((name, a)) for a in (p.get("apps") or {})}
            out.append({"cluster": name, "app": None, "title": "agent tunnel", "up": self.port_open(p["port"]),
                        "started_here": proc is not None and proc.poll() is None,
                        "stopped": self.stops.get((name, None))})
            for app, conf in sorted((p.get("apps") or {}).items()):
                if app not in APPS or not isinstance(conf, dict) or not conf.get("port"):
                    continue
                aproc = procs.get(app)
                out.append({"cluster": name, "app": app, "title": APPS[app]["title"],
                            "up": self.port_open(conf["port"]),
                            "started_here": aproc is not None and aproc.poll() is None,
                            "stopped": self.stops.get((name, app))})
        return {"at": time.time(), "tunnels": out}

    def about(self):
        on_disk = local_hpclib_version()
        return {"hpclib_version": STARTED_VERSION, "hpclib_version_on_disk": on_disk,
                "restart_for": on_disk if on_disk and STARTED_VERSION and on_disk != STARTED_VERSION else None,
                "console_version": VERSION, "hpclib_dir": HPCLIB_DIR, "git": git_info(os.path.dirname(HPCLIB_DIR)),
                "python": platform.python_version(), "python_path": sys.executable,
                "platform": platform.platform(), "local_platform": local_platform.describe(),
                "console_dir": console_dir(), "agents_dir": agent_profiles.root(),
                "pid": os.getpid(), "started": STARTED_AT, "clusters": len(self.names()),
                "packages": [{"name": p.get("name"), "version": p.get("version")} for p in self.packages_list()]}

    def diagnostics(self):
        """What a bug report needs, without tokens, passwords or keys: versions, settings, every cluster's state."""
        profiles = agent_profiles.all_profiles()

        def one(p):
            d = self.describe(p)
            t, lg = d.get("tunnel") or {}, d.get("login") or {}
            apps = {}
            for app in sorted(p.get("apps") or {}):
                if app not in APPS:
                    continue
                try:
                    s = self._app_state(p, app)
                    apps[app] = {k: s.get(k) for k in ("state", "error", "port", "started_here")}
                    apps[app]["install"] = (s.get("install") or {}).get("state")
                except Exception as e:   # noqa: BLE001
                    apps[app] = {"error": f"{type(e).__name__}: {e}"}
            return {"name": p["name"], "host": p.get("host"), "set_up": d.get("set_up"),
                    "preset": (p.get("preset") or {}).get("id"), "rest_on": p.get("rest_on") or "job",
                    "connection_hours": d.get("connection_hours"),
                    "tunnel": {k: t.get(k) for k in ("state", "error", "hpclib_version", "server", "started_here",
                                                     "last_exit", "port")},
                    "login": {k: lg.get(k) for k in ("state", "message")},
                    "operation": d.get("operation"), "apps": apps}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, len(profiles)))) as pool:
            clusters = list(pool.map(one, profiles))
        prefs = console_prefs.load(console_dir())
        clients = [{"id": c["id"], "available": c["available"], "enabled": c["enabled"], "error": c["error"],
                    "entries": {e["name"]: e["state"] for e in c["entries"]}, "stale": c["stale"]}
                   for c in self.llm_clients()]
        return redact({"generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "about": self.about(),
                       "preferences": prefs, "port_range": list(agent_profiles.port_range()),
                       "llm_clients": clients, "apps": sorted(APPS), "clusters": clusters})

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
        self.updater = console_update.Updater(os.path.dirname(HPCLIB_DIR))
        self.restart_argv = None     # main() sets it: how to start this console again
        super().__init__(address, ConsoleHandler)

    RESTART_KEY_VAR = "HPCLIB_CONSOLE_RESTART_KEY"   # the session key for the next process (not on its command line)

    def restart(self):
        """Start the console again in this process (exec), with the same port and session key, so the page carries
        on. Tunnels and apps keep running (their ports show them up again); a login or an operation in progress
        would be cut off, so they refuse it."""
        if self.restart_argv is None:
            raise ConsoleError(409, "this console wasn't started by agent_console's main(); restart it yourself")
        c = self.clusters
        busy = []
        if c is not None:
            with c.lock:
                busy += [f"{name}: {op.get('kind')}" for name, op in c.ops.items() if op.get("state") == "running"]
                busy += [f"{name}: storage scan" for name, s in c.storage_scans.items() if s.get("state") == "running"]
            busy += [f"{name}: login" for name, a in list(c.logins.attempts.items())
                     if a.get("state") in ("starting", "password_sent", "push_sent", "code_required")]
        if busy:
            raise ConsoleError(409, "wait for these to finish first: " + "; ".join(busy))
        argv = list(self.restart_argv)

        def again():
            time.sleep(0.5)                      # the answer reaches the page first
            sys.stdout.flush()
            sys.stderr.flush()
            os.environ[self.RESTART_KEY_VAR] = self.key
            os.execv(argv[0], argv)              # this process's sockets aren't inherited: the port is free again
        threading.Thread(target=again, daemon=True).start()
        return {"restarting": True, "after": 0.5}

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
        if parts == ["styles"] and verb == "GET":
            return 200, dict(self.styles(), ok=True)
        if parts == ["styles"] and verb == "PUT":
            return 200, dict(self.styles(self.json_body()), ok=True)
        if parts == ["styles", "open"] and verb == "POST":
            return 200, self.open_stylesheet()
        if parts == ["health"] and verb == "GET":
            return 200, {"ok": True, "console": VERSION, "clusters": len(c.names()), "pid": os.getpid(),
                         "started": STARTED_AT,
                         "hpclib_version": local_hpclib_version()}
        if parts == ["platform"] and verb == "GET":
            return 200, dict(local_platform.describe(), ok=True)
        if parts == ["preferences"] and verb == "GET":
            return 200, dict(console_prefs.load(console_dir()), ok=True)
        if parts == ["preferences"] and verb == "PUT":
            try:
                return 200, dict(console_prefs.update(console_dir(), self.json_body()), ok=True)
            except console_prefs.PrefsError as e:
                raise ConsoleError(422, str(e)) from e
        if parts == ["ports"] and verb == "GET":
            return 200, dict(c.ports_report(), ok=True)
        if parts == ["ports", "range"] and verb == "PUT":
            return 200, dict(c.set_port_range(self.json_body()), ok=True)
        if parts == ["llm-clients"] and verb == "GET":
            return 200, {"ok": True, "clients": c.llm_clients()}
        if len(parts) == 3 and parts[0] == "llm-clients" and parts[2] == "write" and verb == "POST":
            body = self.json_body()
            return 200, dict(c.llm_client_write(parts[1], body.get("names"), body.get("force")), ok=True)
        if len(parts) == 3 and parts[0] == "llm-clients" and parts[2] == "remove" and verb == "POST":
            return 200, dict(c.llm_client_remove(parts[1], self.json_body()), ok=True)
        if parts == ["watch"] and verb == "GET":
            return 200, dict(c.watch(), ok=True)
        if parts == ["about"] and verb == "GET":
            return 200, dict(c.about(), ok=True, url=f"http://127.0.0.1:{self.server.port}",
                             static=bool(self.server.static_dir))
        if parts == ["update"] and verb == "GET":
            return 200, dict(self.server.updater.status(fetch=self.flag("fetch")), ok=True, running=STARTED_VERSION)
        if parts == ["update"] and verb == "POST":
            try:
                return 200, dict(self.server.updater.update(), ok=True, running=STARTED_VERSION)
            except console_update.UpdateError as e:
                raise ConsoleError(e.status, str(e)) from e
        if parts == ["restart"] and verb == "POST":
            return 202, dict(self.server.restart(), ok=True)
        if parts == ["diagnostics"] and verb == "GET":
            return 200, dict(c.diagnostics(), ok=True)
        if parts == ["widgets"] and verb == "GET":
            return 200, {"ok": True, "widgets": c.widgets()}
        if len(parts) == 3 and parts[0] == "widgets" and verb == "GET":
            return 200, dict(c.widget_html(parts[1], parts[2]), ok=True)
        if parts == ["packages"] and verb == "GET":
            return 200, {"ok": True, "packages": c.packages_list()}
        if parts == ["packages"] and verb == "POST":
            data = self.body(console_packages.MAX_ZIP)
            return (200 if self.flag("dry_run") else 201), dict(c.package_add(data, self.flag("dry_run")), ok=True)
        if parts == ["packages", "bundled"] and verb == "GET":
            return 200, {"ok": True, "packages": c.bundled_packages()}
        if len(parts) == 3 and parts[:2] == ["packages", "bundled"] and verb == "POST":
            return (200 if self.flag("dry_run") else 201), dict(c.bundled_add(parts[2], self.flag("dry_run")), ok=True)
        if len(parts) == 2 and parts[0] == "packages" and verb == "DELETE":
            return 200, dict(c.package_remove(parts[1]), ok=True)
        if parts == ["apps"] and verb == "GET":
            return 200, {"ok": True, "apps": [{"id": "agents", "title": "Agents"}] +
                         [{"id": k, "title": v["title"], "kind": v.get("kind", "tunnel"),
                           "description": v.get("description"), "package": v.get("package"),
                           "tab": v.get("tab")}
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
            if not rest and verb == "DELETE":
                return 200, dict(c.remove_cluster(profile, self.json_body()), ok=True)
            if rest == ["install"] and verb == "POST":
                return 202, dict(c.install(profile, self.json_body()), ok=True)
            if rest == ["setup"] and verb == "POST":
                return 202, dict(c.setup(profile, self.json_body()), ok=True)
            if rest == ["jobs"] and verb == "GET":
                return 200, dict(c.cluster_jobs(profile), ok=True)
            if rest == ["jobs", "cancel"] and verb == "POST":
                return 200, dict(c.cancel_job(profile, self.json_body()), ok=True)
            if rest == ["storage"] and verb == "GET":
                return 200, dict(c.storage(profile), ok=True)
            if rest == ["storage", "scan"] and verb == "POST":
                return 202, dict(c.start_storage_scan(profile), ok=True)
            if rest == ["quota"] and verb == "GET":
                return 200, dict(c.quota(profile, self.query.get("kind", ["all"])[-1]), ok=True)
            if rest == ["quota", "settings"] and verb == "PUT":
                return 200, dict(c.set_quota_checks(profile, self.json_body()), ok=True)
            if rest == ["app-view"] and verb == "PUT":
                return 200, dict(c.set_app_view(profile, self.json_body()), ok=True)
            if rest == ["python-tools"] and verb == "GET":
                return 200, dict(c.python_tools(profile), ok=True)
            if rest == ["python-tools"] and verb == "PUT":
                return 200, dict(c.set_python_tools(profile, self.json_body()), ok=True)
            if rest == ["python-tools", "install"] and verb == "POST":
                return 200, dict(c.install_python_tool(profile, self.json_body()), ok=True)
            if rest == ["sandbox-tools"] and verb == "GET":
                return 200, dict(c.sandbox_tools(profile), ok=True)
            if rest == ["sandbox-tools", "install"] and verb == "POST":
                body = self.json_body()
                if not isinstance(body, dict) or not body.get("tool"):
                    raise ConsoleError(400, "Choose bubblewrap or charliecloud to install")
                return 200, dict(c.sandbox_tools(profile, body), ok=True)
            if rest == ["ports"] and verb == "GET":
                return 200, {"ok": True, "ports": c.login_ports(profile)}
            if rest == ["ports", "clear"] and verb == "POST":
                return 202, dict(c.clear_ports(profile, self.json_body()), ok=True)
            if rest == ["mcp-name"] and verb == "PUT":
                return 200, dict(c.rename_mcp(profile, self.json_body()), ok=True)
            if rest == ["new-ports"] and verb == "POST":
                return 200, dict(c.new_ports(profile, self.json_body()), ok=True)
            if rest == ["ssh", "setup"] and verb == "POST":
                return 202, dict(c.setup_ssh(profile, self.json_body()), ok=True)
            if rest == ["operation"] and verb == "GET":
                return 200, dict(c.operation(profile["name"], self.int_arg("lines", 200, 1, 5000), self.flag("latest")), ok=True)
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
            if rest == ["local"] and verb == "PUT":
                return 200, dict(c.set_local_mcp(profile, self.json_body()), ok=True)
            if rest == ["llm-clients"] and verb == "GET":
                return 200, dict(c.cluster_llm_clients(profile), ok=True)
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
                return 200, dict(c.tail_log(profile["name"], self.int_arg("lines", 200, 1, 5000), self.flag("latest")), ok=True)
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
        if action == ["open"] and verb == "POST":
            return 200, dict(c.app_open(profile, app), ok=True)
        if action == ["answer"] and verb == "POST":
            proc = c.app_waiting(profile["name"], app)
            return 200, dict(c.answer_prompt(proc, self.json_body().get("answer")), ok=True)
        if action == ["log"] and verb == "GET":
            return 200, dict(c.app_log(profile["name"], app, self.int_arg("lines", 200, 1, 5000), self.flag("latest")), ok=True)
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
        # empty and dot segments too: "admin//ssh/x" or "admin/./ssh/x" would pass a prefix check below
        if not REST_ROUTE_RE.fullmatch(route) or any(part in ("", ".", "..") for part in route.rstrip("/").split("/")):
            raise ConsoleError(400, f"invalid cluster route {route!r}")
        if (profile.get("preset") or {}).get("ssh_keys") == "site-managed" and route.startswith("admin/ssh/") and verb != "GET":
            raise ConsoleError(409, "this cluster's SSH files are managed by the site; only read operations are available")
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
            _, credential = self.server.clusters.token(profile, as_agent=as_agent)
            recovery = ("use Setup → Recover owner access; if already recovered, restart the agent tunnel"
                        if credential == "owner" else
                        "restore a valid agent token, or use Rebuild (also replaces configuration and templates)")
            raise ConsoleError(502, f"{profile['name']} refused the stored {credential} credential (HTTP 401); "
                                    f"{recovery}", cluster_status=401)
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
                                    "(time, mem, partition, account, qos, cpus-per-task, constraint, nodelist)")
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

    def default_css(self):
        """The bundled default-styles.css (None without --static)."""
        if not self.server.static_dir:
            return None
        try:
            with open(os.path.join(self.server.static_dir, "default-styles.css")) as f:
                return f.read()
        except OSError:
            return None

    def styles(self, values=None):
        """GET /api/styles: what user.css sets each of default-styles.css's variables to (Interface Settings' form);
        PUT (values): write them into user.css (console_styles.py)."""
        defaults = self.default_css()
        if defaults is None:
            raise ConsoleError(503, "Interface Settings needs the bundled default-styles.css: run the console with --static")
        path = os.path.join(console_dir(), "user.css")
        console_styles.migrate(console_dir(), defaults)
        if values is not None:
            try:
                current = console_styles.save(path, defaults, values)
            except console_styles.StyleError as e:
                raise ConsoleError(422, str(e)) from e
        else:
            try:
                with open(path) as f:
                    current = console_styles.read(f.read(), defaults)
            except OSError:
                current = console_styles.read("", defaults)
        return {"path": path, "exists": os.path.isfile(path), "values": current}

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
    key = opts.key or os.environ.pop(ConsoleServer.RESTART_KEY_VAR, None) or secrets.token_urlsafe(32)
    restarted = bool(os.environ.get("HPCLIB_CONSOLE_RESTARTED"))
    session_file = os.path.join(console_dir(), "session")
    try:
        server = ConsoleServer(("127.0.0.1", opts.port), key, Clusters(), allow_origin=opts.allow_origin,
                               static_dir=opts.static)
    except OSError as e:
        sys.exit(f"agent_console: can't listen on 127.0.0.1:{opts.port} ({e}); is it already running? "
                 f"pick another with --port")
    if opts.static:      # Interface Settings' values from an earlier console's preferences.json, into user.css
        try:
            with open(os.path.join(opts.static, "default-styles.css")) as f:
                console_styles.migrate(console_dir(), f.read())
        except OSError:
            pass
    write_private(session_file, json.dumps({"url": f"http://127.0.0.1:{server.port}", "key": key,
                                            "pid": os.getpid()}) + "\n")
    # a restart (Console Settings) runs this again: same port, the key through the environment, no new browser tab
    given = list(sys.argv[1:] if argv is None else argv)
    keep, skip = [], False
    for a in given:
        if skip:
            skip = False
        elif a in ("--key", "--port"):
            skip = True
        elif not (a == "--open" or a.startswith(("--key=", "--port="))):
            keep.append(a)
    server.restart_argv = [sys.executable, os.path.abspath(__file__), *keep, "--port", str(server.port)]
    os.environ["HPCLIB_CONSOLE_RESTARTED"] = "1"
    url = f"http://127.0.0.1:{server.port}"
    print(f"hpclib Agent Console on {url}")
    print(f"  session key: {key}")
    print(f"  (also in {session_file}; curl -H \"Authorization: Bearer $KEY\" {url}/api/clusters)")
    print(f"  clusters: {', '.join(server.clusters.names()) or 'none yet; run setup_agents'}")
    if opts.allow_origin:
        print(f"  browser calls allowed from: {opts.allow_origin}")
    if opts.static:
        print(f"  front end: {url}/#key={key}")
        if opts.open and not restarted:
            if local_platform.platform() == "wsl":       # the browser is Windows'
                try:
                    local_platform.open_target(f"{url}/#key={key}")
                except OSError as e:
                    print(f"  couldn't open your browser ({e}); open the front end link above")
            else:
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
