"""
The console's own preferences (this machine, not a cluster), kept in preferences.json in its directory:

  notifications   which browser notifications the page shows: a tunnel dropping, a password or host-key prompt,
                  an agent's job finishing, a new template proposal
  new_cluster     defaults a cluster added from now on starts with: how long its login is kept, the work
                  directory the Add cluster form suggests ({user} is the login's user), the local folder it
                  suggests for the agent's files ({cluster}, {user}; "" suggests none), the Python here it suggests
                  for the MCP server ("" finds one), its MCP server's name
                  (hpclib-{cluster} unless changed), and the REST server's GPU mode and per-job GPU limit
  llm_clients     the LLM clients whose MCP configuration the console keeps up to date (see mcp_clients.py)
  llm_client_paths  where each client's configuration file is, when finding it isn't right ("" finds it), and
                  claude_command, the claude program Claude Code's entries are added with
  widgets         which packaged widgets are hidden, and their order (keys PACKAGE/ID)
(Interface Settings' values live in user.css itself: console_styles.py.)
  system          this machine (local_platform.py): platform, "auto" or one of linux, macos, wsl, for when detection
                  gets it wrong ($HPCLIB_PLATFORM wins); windows_home, under WSL your Windows profile folder as WSL
                  sees it (/mnt/c/Users/NAME), when Windows can't be asked ("" asks)

The local port range tunnels are given ports from is agent_profiles' (it picks them, from bash too).
Standard library only.
"""
import json
import os
import re
import tempfile
import threading

NOTIFICATIONS = ("tunnel", "prompt", "job", "proposal")
GPU_MODES = ("auto", "always", "never")
LLM_CLIENTS = ("claude_desktop", "claude_code", "cursor", "chatgpt")
DEFAULTS = {
    "notifications": {k: False for k in NOTIFICATIONS},
    "new_cluster": {"connection_hours": 12, "work_dir_pattern": "", "sandbox_gpus": "auto", "max_gpus": None,
                    "mcp_name_pattern": "hpclib-{cluster}", "local_root_pattern": "", "mcp_python": ""},
    "llm_clients": {k: False for k in LLM_CLIENTS},
    "widgets": {"hidden": [], "order": []},
    "system": {"platform": "auto", "windows_home": ""},
    "llm_client_paths": {k: "" for k in LLM_CLIENTS + ("claude_command",)},
}
PLATFORM_CHOICES = ("auto", "linux", "macos", "wsl")      # local_platform.CHOICES
WORK_PATTERN_RE = re.compile(r"(/[A-Za-z0-9_.@{}+-]+)+/?")
MCP_PATTERN_RE = re.compile(r"(?:[A-Za-z0-9_-]|\{(?:cluster|user|host)\}){1,64}")      # agent_profiles' too
WIDGET_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/[a-z][a-z0-9_-]{0,31}")


class PrefsError(ValueError):
    pass


_lock = threading.Lock()


def path(console_dir):
    return os.path.join(console_dir, "preferences.json")


def load(console_dir):
    """The preferences, with every section and key present (defaults for what isn't saved)."""
    try:
        with open(path(console_dir)) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    out = json.loads(json.dumps(DEFAULTS))
    for section, values in (saved if isinstance(saved, dict) else {}).items():
        if section in out and isinstance(values, dict):
            out[section].update(values)
    return out


def _write(console_dir, prefs):
    os.makedirs(console_dir, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=console_dir, prefix=".preferences.")
    with os.fdopen(fd, "w") as f:
        json.dump(prefs, f, indent=2)
        f.write("\n")
    os.replace(tmp, path(console_dir))


def _bool_map(values, keys, section):
    if not isinstance(values, dict) or set(values) - set(keys) or not all(isinstance(v, bool) for v in values.values()):
        raise PrefsError(f"{section} takes true or false for {', '.join(keys)}")
    return values


def _new_cluster(values):
    if not isinstance(values, dict):
        raise PrefsError("new_cluster must be an object")
    unknown = set(values) - set(DEFAULTS["new_cluster"])
    if unknown:
        raise PrefsError(f"unknown new_cluster settings {sorted(unknown)}")
    hours = values.get("connection_hours")
    if "connection_hours" in values and (isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 168):
        raise PrefsError("connection_hours must be a whole number of hours from 1 to 168")
    pattern = values.get("work_dir_pattern")
    if "work_dir_pattern" in values and pattern != "" and (
            not isinstance(pattern, str) or len(pattern) > 256 or not WORK_PATTERN_RE.fullmatch(pattern)
            or re.search(r"\{(?!user\})", pattern)):
        raise PrefsError("the work directory is an absolute path; {user} stands for the login's user")
    mcp = values.get("mcp_name_pattern")
    if "mcp_name_pattern" in values and (not isinstance(mcp, str) or not MCP_PATTERN_RE.fullmatch(mcp)
                                         or not re.match(r"[A-Za-z0-9{]", mcp)):
        raise PrefsError("the MCP name is letters, digits, - and _, with {cluster}, {host} or {user} filled in")
    local = values.get("local_root_pattern")
    if "local_root_pattern" in values and local != "" and (
            not isinstance(local, str) or len(local) > 512 or re.search(r"[\0\n\r]", local)
            or not re.match(r"(/|~/|~$|[A-Za-z]:[\\/])", local) or re.search(r"\{(?!(?:cluster|user)\})", local)):
        raise PrefsError("the local folder is a full path on this machine; {cluster} and {user} are filled in")
    py = values.get("mcp_python")
    if "mcp_python" in values and py != "" and (not isinstance(py, str) or len(py) > 1024
                                                 or re.search(r"[\0\n\r]", py)):
        raise PrefsError("mcp_python is the path of a Python on this machine, or empty to find one")
    if "sandbox_gpus" in values and values["sandbox_gpus"] not in GPU_MODES:
        raise PrefsError(f"sandbox_gpus is one of {', '.join(GPU_MODES)}")
    gpus = values.get("max_gpus")
    if gpus is not None and (isinstance(gpus, bool) or not isinstance(gpus, int) or not 0 <= gpus <= 64):
        raise PrefsError("max_gpus is a number of GPUs from 0 to 64, or empty for the server's default")
    return values


def _widgets(values):
    if not isinstance(values, dict) or set(values) - {"hidden", "order"}:
        raise PrefsError("widgets has hidden and order: lists of PACKAGE/ID")
    for k, v in values.items():
        if not isinstance(v, list) or len(v) > 400 or not all(isinstance(x, str) and WIDGET_KEY_RE.fullmatch(x) for x in v):
            raise PrefsError(f"widgets.{k} is a list of PACKAGE/ID")
    return values


def _system(values):
    if not isinstance(values, dict) or set(values) - set(DEFAULTS["system"]):
        raise PrefsError("system has platform and windows_home")
    if "platform" in values and values["platform"] not in PLATFORM_CHOICES:
        raise PrefsError(f"platform is one of {', '.join(PLATFORM_CHOICES)}")
    home = values.get("windows_home")
    if "windows_home" in values and home != "" and (
            not isinstance(home, str) or len(home) > 512 or not home.startswith("/") or re.search(r"[\0\n\r]", home)):
        raise PrefsError("windows_home is your Windows profile folder as WSL sees it, such as /mnt/c/Users/NAME "
                         "(empty: ask Windows)")
    return values


def _client_paths(values):
    keys = DEFAULTS["llm_client_paths"]
    if not isinstance(values, dict) or set(values) - set(keys):
        raise PrefsError(f"llm_client_paths has {', '.join(keys)}")
    for k, v in values.items():
        if v == "":
            continue
        if (not isinstance(v, str) or len(v) > 1024 or re.search(r"[\0\n\r]", v)
                or not re.match(r"(/|~/|[A-Za-z]:[\\/])", v.strip())):
            raise PrefsError(f"llm_client_paths.{k}: a full path (/..., ~/... or C:\\...), or empty to find it")
    return {k: v.strip() for k, v in values.items()}


CHECKS = {
    "notifications": lambda v: _bool_map(v, NOTIFICATIONS, "notifications"),
    "new_cluster": _new_cluster,
    "llm_clients": lambda v: _bool_map(v, LLM_CLIENTS, "llm_clients"),
    "widgets": _widgets,
    "system": _system,
    "llm_client_paths": _client_paths,
}


def update(console_dir, body):
    """Merge {section: {...}} into the saved preferences returns them all."""
    if not isinstance(body, dict) or not body or set(body) - set(CHECKS):
        raise PrefsError(f"preferences has the sections {', '.join(CHECKS)}")
    checked = {section: CHECKS[section](values) for section, values in body.items()}
    with _lock:
        prefs = load(console_dir)
        for section, values in checked.items():
            prefs[section].update(values)
        _write(console_dir, prefs)
    return prefs


def cluster_config(prefs, config=None):
    """A new cluster's REST config (setup_config) with the GPU defaults added where it doesn't say."""
    nc = prefs["new_cluster"]
    out = json.loads(json.dumps(config or {}))
    if nc.get("sandbox_gpus") not in (None, "auto"):
        out.setdefault("sandbox", {}).setdefault("gpus", nc["sandbox_gpus"])
    if nc.get("max_gpus") is not None:
        out.setdefault("limits", {}).setdefault("max_gpus", nc["max_gpus"])
    return out or None
