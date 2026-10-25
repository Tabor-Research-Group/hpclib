"""
The console's own preferences (this machine, not a cluster), kept in preferences.json in its directory:

  notifications   which browser notifications the page shows: a tunnel dropping, a password or host-key prompt,
                  an agent's job finishing, a new template proposal
  new_cluster     defaults a cluster added from now on starts with: how long its login is kept, the work
                  directory the Add cluster form suggests ({user} is the login's user), its MCP server's name
                  (hpclib-{cluster} unless changed), and the REST server's GPU mode and per-job GPU limit
  llm_clients     the LLM clients whose MCP configuration the console keeps up to date (see mcp_clients.py)
  widgets         which packaged widgets are hidden, and their order (keys PACKAGE/ID)
  interface       values for the page's CSS variables (default-styles.css): "common" ones, and colours for the
                  light and dark themes; served as /interface.css, before the user's own user.css

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
                    "mcp_name_pattern": "hpclib-{cluster}"},
    "llm_clients": {k: False for k in LLM_CLIENTS},
    "interface": {"common": {}, "light": {}, "dark": {}},
    "widgets": {"hidden": [], "order": []},
}
CSS_VAR_RE = re.compile(r"--[a-z][a-z0-9-]{0,47}")
# a CSS value with nothing that ends a declaration or rule, escapes, comments or fetches anything
CSS_VALUE_RE = re.compile(r"(?!.*(?:/\*|\*/|url\s*\(|@import|expression\s*\())[^;{}<>\\\n\r\0]{1,200}", re.I)
WORK_PATTERN_RE = re.compile(r"(/[A-Za-z0-9_.@{}+-]+)+/?")
MCP_PATTERN_RE = re.compile(r"(?:[A-Za-z0-9_-]|\{(?:cluster|user|host)\}){1,64}")      # agent_profiles' too
MAX_VARS = 200
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
    if "sandbox_gpus" in values and values["sandbox_gpus"] not in GPU_MODES:
        raise PrefsError(f"sandbox_gpus is one of {', '.join(GPU_MODES)}")
    gpus = values.get("max_gpus")
    if gpus is not None and (isinstance(gpus, bool) or not isinstance(gpus, int) or not 0 <= gpus <= 64):
        raise PrefsError("max_gpus is a number of GPUs from 0 to 64, or empty for the server's default")
    return values


def _interface(values):
    if not isinstance(values, dict) or set(values) - {"common", "light", "dark"}:
        raise PrefsError("interface has common, light and dark variable maps")
    count = 0
    for part, given in values.items():
        if not isinstance(given, dict):
            raise PrefsError(f"interface.{part} must map CSS variables to values")
        for name, value in given.items():
            count += 1
            if not CSS_VAR_RE.fullmatch(name):
                raise PrefsError(f"{name!r} is not a CSS variable name like --bg")
            if not isinstance(value, str) or not CSS_VALUE_RE.fullmatch(value.strip()):
                raise PrefsError(f"{name}: a value without ; {{ }} < > \\, comments or url()")
    if count > MAX_VARS:
        raise PrefsError(f"at most {MAX_VARS} interface values")
    return {part: {k: v.strip() for k, v in given.items()} for part, given in values.items()}


def _widgets(values):
    if not isinstance(values, dict) or set(values) - {"hidden", "order"}:
        raise PrefsError("widgets has hidden and order: lists of PACKAGE/ID")
    for k, v in values.items():
        if not isinstance(v, list) or len(v) > 400 or not all(isinstance(x, str) and WIDGET_KEY_RE.fullmatch(x) for x in v):
            raise PrefsError(f"widgets.{k} is a list of PACKAGE/ID")
    return values


CHECKS = {
    "notifications": lambda v: _bool_map(v, NOTIFICATIONS, "notifications"),
    "new_cluster": _new_cluster,
    "llm_clients": lambda v: _bool_map(v, LLM_CLIENTS, "llm_clients"),
    "interface": _interface,
    "widgets": _widgets,
}


def update(console_dir, body):
    """Merge {section: {...}} into the saved preferences (interface replaces its maps whole); returns them all."""
    if not isinstance(body, dict) or not body or set(body) - set(CHECKS):
        raise PrefsError(f"preferences has the sections {', '.join(CHECKS)}")
    checked = {section: CHECKS[section](values) for section, values in body.items()}
    with _lock:
        prefs = load(console_dir)
        for section, values in checked.items():
            if section == "interface":
                prefs[section] = dict(DEFAULTS["interface"], **values)
            else:
                prefs[section].update(values)
        _write(console_dir, prefs)
    return prefs


def interface_css(prefs):
    """/interface.css: the saved variables, laid over default-styles.css with the same theme selectors."""
    ui = prefs.get("interface") or {}

    def block(selector, values):
        if not values:
            return ""
        body = "".join(f"  {k}: {v};\n" for k, v in sorted(values.items())
                       if CSS_VAR_RE.fullmatch(k) and CSS_VALUE_RE.fullmatch(v))
        return f"{selector} {{\n{body}}}\n" if body else ""

    light = dict(ui.get("common") or {}, **(ui.get("light") or {}))
    dark = ui.get("dark") or {}
    return ("/* Saved on the console's Interface Settings page; user.css, loaded after this, wins. */\n" +
            block(":root", light) +
            ("@media (prefers-color-scheme: dark) {\n" + block(':root:not([data-theme="light"])', dark) + "}\n"
             if dark else "") +
            block(':root[data-theme="dark"]', dark))


def cluster_config(prefs, config=None):
    """A new cluster's REST config (setup_config) with the GPU defaults added where it doesn't say."""
    nc = prefs["new_cluster"]
    out = json.loads(json.dumps(config or {}))
    if nc.get("sandbox_gpus") not in (None, "auto"):
        out.setdefault("sandbox", {}).setdefault("gpus", nc["sandbox_gpus"])
    if nc.get("max_gpus") is not None:
        out.setdefault("limits", {}).setdefault("max_gpus", nc["max_gpus"])
    return out or None
