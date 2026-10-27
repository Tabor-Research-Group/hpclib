"""
The LLM clients the console can give each cluster's MCP entry (the agent profile's mcp.json, written by
setup_agents): Claude Desktop and Cursor through their JSON configuration files, Claude Code through its own
`claude mcp` command (it keeps its configuration in a file it rewrites itself), and ChatGPT through Codex's
config.toml (~/.codex/config.toml, or $CODEX_HOME's), which the ChatGPT desktop app, the Codex CLI and its IDE
extension share; ChatGPT on the web can't run local servers.

Under WSL (local_platform.py), Claude Desktop, Cursor and ChatGPT are Windows programs: their configuration is in
your Windows profile folder (as WSL sees it, /mnt/c/Users/NAME), and their entries start the MCP server through
wsl.exe. Claude Code installed in WSL is a Linux client; installed on Windows only (claude.exe), a Windows one.

Only entries the console owns are touched: one per agent profile, under its MCP name (hpclib-NAME by default, or
the name given to it), plus names a profile had before. Before the first change to a configuration file, the file
is copied beside it as FILE.before-hpclib; it is then replaced atomically, keeping its permissions and everything
else in it (in config.toml, the entry's own other keys too). A file that can't be read is left alone, and a
config.toml change is checked by parsing the result before it is written.

Where each client's configuration is, is found (see the *_path functions) unless Console Settings -> LLM clients
gives a path (preferences.json's llm_client_paths: a file per client, and claude_command, the claude program). On
Windows, Claude Desktop's file is looked for where its installers put it: %APPDATA%\\Claude, the Microsoft Store
package's %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude, and %LOCALAPPDATA%\\Claude-Data; the newest
file found wins.

Standard library only.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import local_platform
except ImportError:  # imported without hpclib/lib on the path
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
    import local_platform

BACKUP_SUFFIX = ".before-hpclib"
CLAUDE_CANDIDATES = ("~/.claude/local/claude", "~/.local/bin/claude", "/opt/homebrew/bin/claude",
                     "/usr/local/bin/claude")


class ClientError(Exception):
    pass


NO_WINDOWS_HOME = ("can't find your Windows profile folder from WSL: set it on Console Settings -> This machine, "
                   "or in $HPCLIB_WINDOWS_HOME (e.g. /mnt/c/Users/NAME)")


PATH_KEYS = ("claude_desktop", "claude_code", "cursor", "chatgpt", "claude_command")


def _saved_paths():
    paths = local_platform.console_preferences().get("llm_client_paths")
    return paths if isinstance(paths, dict) else {}


overrides = _saved_paths       # the console points this at its own preferences


def override(key):
    """The path Console Settings gives for a client (None: found automatically). Under WSL a Windows path
    (C:\\Users\\...) is taken as Windows means it."""
    value = (overrides() or {}).get(key) or ""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if re.match(r"[A-Za-z]:[\\/]", value):
        if local_platform.windows_side():
            return local_platform.to_linux(value)
        return value
    return os.path.expanduser(value)


def home():
    return os.path.expanduser(os.environ.get("HPCLIB_MCP_CLIENT_HOME") or "~")


def windows_home():
    return local_platform.windows_home()[0]


def _windows(*parts):
    """A path in your Windows profile folder (None when it can't be found)."""
    h = windows_home()
    return os.path.join(h, *parts) if h else None


def _linux_claude():
    if os.environ.get("HPCLIB_CLAUDE_BIN"):
        return os.environ["HPCLIB_CLAUDE_BIN"]
    if override("claude_command"):
        return override("claude_command")
    found = shutil.which("claude")
    windows = local_platform.windows_side() and found and found.startswith("/mnt/")   # WSL appends Windows' PATH
    if found and not windows:
        return found
    for c in CLAUDE_CANDIDATES:
        p = os.path.expanduser(c)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def windows_client(cid):
    """Whether this client is a Windows program the console reaches from WSL."""
    if not local_platform.windows_side():
        return False
    if cid == "claude_code":
        return _linux_claude() is None and shutil.which("claude.exe") is not None
    return True


def entry_for(cid, entry):
    """The entry as the client runs it: through wsl.exe for a Windows client under WSL."""
    if entry.get("command") is None or not windows_client(cid):
        return entry
    return local_platform.windows_entry(entry)


DESKTOP_FILE = "claude_desktop_config.json"


def find_windows_desktop_config(roaming, local):
    """
    Claude Desktop's configuration on Windows, where its installers keep it: %APPDATA%\\Claude (the classic
    installer), the Microsoft Store package's LocalCache (%LOCALAPPDATA%\\Packages\\Claude_<id>\\LocalCache\\Roaming\\
    Claude, what it sees as %APPDATA%\\Claude), and %LOCALAPPDATA%\\Claude-Data (and its folders, such as claude_<id>).
    The newest file found wins; with none yet, the first of those folders that exists (the package's first: a
    packaged app doesn't see the real %APPDATA%), else %APPDATA%\\Claude.
    """
    standard = os.path.join(roaming, "Claude", DESKTOP_FILE)
    packaged = os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude")
    data = os.path.join(local, "Claude-Data")
    files = [standard] + glob.glob(os.path.join(packaged, DESKTOP_FILE))
    for depth in range(4):
        files += glob.glob(os.path.join(data, *(["*"] * depth), DESKTOP_FILE))
    files = [f for f in files if os.path.isfile(f)]
    if files:
        return max(files, key=lambda f: os.stat(f).st_mtime)
    for folder in sorted(glob.glob(packaged)) + sorted(glob.glob(os.path.join(data, "[Cc]laude*"))) + [data]:
        if os.path.isdir(folder):
            return os.path.join(folder, DESKTOP_FILE)
    return standard


def desktop_config_path():
    if override("claude_desktop"):
        return override("claude_desktop")
    if windows_client("claude_desktop"):
        h = windows_home()
        if not h:
            return None
        return find_windows_desktop_config(os.path.join(h, "AppData", "Roaming"), os.path.join(h, "AppData", "Local"))
    h = home()
    if sys.platform == "darwin":
        return os.path.join(h, "Library", "Application Support", "Claude", DESKTOP_FILE)
    if sys.platform.startswith("win"):
        return find_windows_desktop_config(os.environ.get("APPDATA") or os.path.join(h, "AppData", "Roaming"),
                                           os.environ.get("LOCALAPPDATA") or os.path.join(h, "AppData", "Local"))
    return os.path.join(h, ".config", "Claude", DESKTOP_FILE)


def claude_binary():
    if windows_client("claude_code"):
        return override("claude_command") or shutil.which("claude.exe")
    return _linux_claude()


def claude_code_path():
    if override("claude_code"):
        return override("claude_code")
    return _windows(".claude.json") if windows_client("claude_code") else os.path.join(home(), ".claude.json")


def codex_home():
    if override("chatgpt"):
        return os.path.dirname(override("chatgpt"))
    if os.environ.get("CODEX_HOME"):
        return os.path.expanduser(os.environ["CODEX_HOME"])
    return _windows(".codex") if windows_client("chatgpt") else os.path.join(home(), ".codex")


def cursor_path():
    if override("cursor"):
        return override("cursor")
    return _windows(".cursor", "mcp.json") if windows_client("cursor") else os.path.join(home(), ".cursor", "mcp.json")


def chatgpt_path():
    if override("chatgpt"):
        return override("chatgpt")
    h = codex_home()
    return os.path.join(h, "config.toml") if h else None


CHATGPT_APPS = ("/Applications/ChatGPT.app", "~/Applications/ChatGPT.app")


def chatgpt_installed():
    """Codex's directory exists, or the ChatGPT desktop app or the codex command is installed."""
    h = codex_home()
    if h and os.path.isdir(h):
        return True
    if shutil.which("codex.exe" if windows_client("chatgpt") else "codex"):
        return True
    return sys.platform == "darwin" and any(os.path.isdir(os.path.expanduser(a)) for a in CHATGPT_APPS)


CLIENTS = {
    "claude_desktop": {"title": "Claude Desktop", "kind": "file", "path": desktop_config_path,
                       "after": "Quit and reopen Claude Desktop to load changed entries."},
    "claude_code": {"title": "Claude Code", "kind": "cli", "path": lambda: claude_code_path(),
                    "after": "New Claude Code sessions use them (user scope)."},
    "cursor": {"title": "Cursor", "kind": "file", "path": lambda: cursor_path(),
               "after": "Cursor reloads its MCP servers on its own; otherwise restart it."},
    "chatgpt": {"title": "ChatGPT", "kind": "toml", "path": lambda: chatgpt_path(),
                "detail": "the desktop app, the Codex CLI and its IDE extension (they share config.toml)",
                "after": "Restart the ChatGPT desktop app, or start a new Codex session, to load changed entries."},
}


def wanted_entries(profiles, profile_dir):
    """{mcp_name: {command, args}} from each profile's mcp.json; profiles without one are skipped."""
    out = {}
    for p in profiles:
        name = p.get("mcp_name")
        try:
            with open(os.path.join(profile_dir(p["name"]), "mcp.json")) as f:
                entry = json.load(f)["mcpServers"][name]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if isinstance(entry, dict) and isinstance(entry.get("command"), str) and isinstance(entry.get("args"), list):
            out[name] = {"command": entry["command"], "args": entry["args"]}
    return out


# ---------------------------------------------------------------- config.toml (ChatGPT, Codex)

TOML_HEADER_RE = re.compile(r"^\s*\[(\[?)(.*?)\]\]?\s*(?:#.*)?$")


def _key_parts(text):
    """A TOML dotted key's parts (bare, "basic" or 'literal'), or None if it isn't one."""
    parts, i, n = [], 0, len(text)
    while True:
        while i < n and text[i] in " \t":
            i += 1
        if i < n and text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            try:
                parts.append(json.loads(text[i:j + 1]))
            except ValueError:
                return None
            i = j + 1
        elif i < n and text[i] == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return None
            parts.append(text[i + 1:j])
            i = j + 1
        else:
            m = re.match(r"[A-Za-z0-9_-]+", text[i:])
            if not m:
                return None
            parts.append(m.group(0))
            i += m.end()
        while i < n and text[i] in " \t":
            i += 1
        if i == n:
            return parts
        if text[i] != ".":
            return None
        i += 1


def _toml_blocks(lines):
    """[(first line, end line, key parts)] for each table header, the table running to the next header."""
    heads = []
    for i, line in enumerate(lines):
        m = TOML_HEADER_RE.match(line)
        if m and not line.lstrip().startswith("#"):
            heads.append((i, _key_parts(m.group(2)) or [None]))
    return [(start, heads[k + 1][0] if k + 1 < len(heads) else len(lines), parts)
            for k, (start, parts) in enumerate(heads)]


def _value_end(lines, i):
    """The line after a `key = value` starting at lines[i], following an array across lines."""
    depth, quote = 0, None
    for j in range(i, len(lines)):
        line = lines[j]
        k = line.index("=") + 1 if j == i else 0
        while k < len(line):
            ch = line[k]
            if quote:
                if ch == "\\" and quote == '"':
                    k += 1
                elif ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "#":
                break
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
            k += 1
        if depth <= 0:
            return j + 1
    return len(lines)


def _toml_value(text):
    text = text.strip()
    text = re.sub(r",\s*\]$", "]", re.sub(r"\s+#[^\"']*$", "", text))
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    try:
        return json.loads(re.sub(r"'([^']*)'", lambda m: json.dumps(m.group(1)), text))
    except ValueError:
        return None


def _toml_servers(text):
    """mcp_servers from a config.toml: tomllib where Python has it, else the console's own reading of it."""
    try:
        import tomllib
    except ImportError:
        tomllib = None
    if tomllib is not None:
        try:
            servers = tomllib.loads(text).get("mcp_servers", {})
        except tomllib.TOMLDecodeError as e:
            raise ClientError(f"config.toml isn't valid TOML ({e}); it is left as it is") from e
        if not isinstance(servers, dict):
            raise ClientError("config.toml's mcp_servers isn't a table; it is left as it is")
        return servers
    lines = text.splitlines()
    servers = {}
    for start, end, parts in _toml_blocks(lines):
        if len(parts) == 2 and parts[0] == "mcp_servers":
            entry = servers.setdefault(parts[1], {})
            i = start + 1
            while i < end:
                m = re.match(r"\s*(command|args)\s*=", lines[i])
                if m:
                    stop = _value_end(lines, i)
                    entry[m.group(1)] = _toml_value(" ".join(lines[i:stop]).split("=", 1)[1])
                    i = stop
                else:
                    i += 1
    return servers


def _toml_entry_lines(entry):
    return [f"command = {json.dumps(entry['command'])}",
            "args = [" + ", ".join(json.dumps(a) for a in entry["args"]) + "]"]


def _toml_change(text, put=None, drop=()):
    """config.toml with the put entries' command and args set (other keys kept) and the drop entries removed."""
    lines = text.splitlines()
    put = dict(put or {})
    for name in list(drop):
        for start, end, parts in reversed(_toml_blocks(lines)):
            if len(parts) >= 2 and parts[0] == "mcp_servers" and parts[1] == name:
                del lines[start:end]
    for name, entry in put.items():
        block = next(((s, e) for s, e, parts in _toml_blocks(lines) if parts == ["mcp_servers", name]), None)
        if block is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [f"[mcp_servers.{json.dumps(name) if not re.fullmatch(r'[A-Za-z0-9_-]+', name) else name}]",
                      *_toml_entry_lines(entry)]
            continue
        start, end = block
        body, i = [], start + 1
        while i < end:
            if re.match(r"\s*(command|args)\s*=", lines[i]):
                i = _value_end(lines, i)
            else:
                body.append(lines[i])
                i += 1
        lines[start + 1:end] = _toml_entry_lines(entry) + body
    out = "\n".join(lines) + "\n"
    servers = _toml_servers(out)          # what Codex will read: the change is written only if it reads back
    for name, entry in put.items():
        if not _same(servers.get(name), entry):
            raise ClientError(f"config.toml defines {name} in a way the console can't change; edit it by hand")
    for name in drop:
        if name in servers:
            raise ClientError(f"config.toml defines {name} in a way the console can't remove; edit it by hand")
    return out


def _read_servers(path):
    """The file's mcpServers (empty if the file doesn't exist); ClientError if it can't be read as JSON."""
    if path.endswith(".toml"):
        try:
            with open(path) as f:
                return _toml_servers(f.read()), None
        except FileNotFoundError:
            return {}, None
        except OSError as e:
            raise ClientError(f"{path} isn't readable ({e}); it is left as it is") from e
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        raise ClientError(f"{path} isn't readable JSON ({e}); it is left as it is") from e
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ClientError(f"{path} has no mcpServers object; it is left as it is")
    return data.get("mcpServers", {}), data


def available(cid):
    spec = CLIENTS[cid]
    if spec["path"]() is None:
        return False
    if spec["kind"] == "cli":
        return claude_binary() is not None
    if spec["kind"] == "toml":
        return chatgpt_installed()
    return os.path.isdir(os.path.dirname(spec["path"]()))


def _same(a, b):
    return isinstance(a, dict) and a.get("command") == b["command"] and a.get("args") == b["args"]


def _split(entry):
    """(launcher, server): the argv that starts Python (the command and any arguments before rest_mcp.py, such as
    /usr/bin/arch -arm64 PYTHON, or wsl.exe ... PYTHON) and rest_mcp.py's own arguments (None: no rest_mcp.py)."""
    args = list(entry.get("args") or []) if isinstance(entry, dict) else []
    for i, a in enumerate(args):
        if isinstance(a, str) and os.path.basename(a.replace("\\", "/")) == "rest_mcp.py":
            return [entry.get("command"), *args[:i]], args[i + 1:]
    return [entry.get("command") if isinstance(entry, dict) else None, *args], None


def launcher(entry):
    """The argv before rest_mcp.py ([] for no entry)."""
    argv = _split(entry)[0]
    return [a for a in argv if a] if argv and argv[0] else []


def _serves(have, want):
    """The client's entry starts the same server as ours (the same URL, token and folders), whatever launches it:
    a launcher of your own (e.g. /usr/bin/arch -arm64 PYTHON) counts as having it."""
    mine, theirs = _split(want)[1], _split(have)[1]
    return mine is not None and theirs is not None and mine == theirs


def status(cid, wanted, owned):
    """
    What the client has: each wanted entry current / different (an older port or path) / missing, and `stale`:
    entries named like the console's (owned: a predicate on names) that no profile has any more.
    """
    spec = CLIENTS[cid]
    path = spec["path"]()
    out = {"id": cid, "title": spec["title"], "path": path, "available": available(cid), "after": spec["after"],
           "detail": spec.get("detail"), "entries": [], "stale": [], "error": None, "windows": windows_client(cid),
           "path_setting": (overrides() or {}).get(cid) or ""}
    if spec["kind"] == "cli":
        out["command"] = claude_binary()
        out["command_setting"] = (overrides() or {}).get("claude_command") or ""
    try:
        if path is None:
            raise ClientError(NO_WINDOWS_HOME)
        servers, _ = _read_servers(path)
    except ClientError as e:
        out["error"] = str(e)
        servers = {}
    wanted = {name: entry_for(cid, entry) for name, entry in wanted.items()}
    for name, entry in sorted(wanted.items()):
        have = servers.get(name)
        state = "missing" if have is None else "current" if _same(have, entry) or _serves(have, entry) else "different"
        own = state == "current" and not _same(have, entry)
        out["entries"].append({"name": name, "state": state, "launcher": "own" if own else None,
                               "launcher_argv": launcher(have) if own and not windows_client(cid) else None})
    out["stale"] = sorted(n for n in servers if n not in wanted and owned(n))
    return out


def _replace(path, text):
    """Write a client's configuration file: a backup of the original the first time, then an atomic replace."""
    directory = os.path.dirname(path)
    mode = 0o600
    if os.path.exists(path):
        mode = os.stat(path).st_mode & 0o777
        if not os.path.exists(path + BACKUP_SUFFIX):
            shutil.copy2(path, path + BACKUP_SUFFIX)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".hpclib-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _write_file(path, change):
    servers, data = _read_servers(path)
    data = data if data is not None else {}
    servers = dict(servers)
    change(servers)
    data["mcpServers"] = servers
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        raise ClientError(f"{directory} doesn't exist: is the client installed?")
    _replace(path, json.dumps(data, indent=2) + "\n")


def _write_toml(path, put=None, drop=()):
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    except OSError as e:
        raise ClientError(f"{path} isn't readable ({e}); it is left as it is") from e
    _toml_servers(text)        # unreadable TOML is left alone
    new = _toml_change(text, put, drop)
    if new.strip() == text.strip():
        return
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    _replace(path, new)


def _claude(args, timeout=60):
    binary = claude_binary()
    if binary is None:
        raise ClientError("the claude command isn't installed (or not on the console's PATH)")
    try:
        return subprocess.run([binary, "mcp", *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ClientError(f"claude mcp {args[0]}: {e}") from e


def write(cid, wanted, force=False):
    """Add or update every wanted entry; returns the names changed. An entry that starts the same server with a
    launcher of its own is left as it is, unless force."""
    spec = CLIENTS[cid]
    if spec["path"]() is None:
        raise ClientError(NO_WINDOWS_HOME)
    if not available(cid):
        raise ClientError(f"{spec['title']} isn't installed on this machine")
    wanted = {name: entry_for(cid, entry) for name, entry in wanted.items()}
    if spec["kind"] == "file":
        changed = []

        def change(servers):
            for name, entry in wanted.items():
                if not _same(servers.get(name), entry) and (force or not _serves(servers.get(name), entry)):
                    servers[name] = dict(servers.get(name) or {}, **entry)
                    changed.append(name)
        _write_file(spec["path"](), change)
        return changed
    if spec["kind"] == "toml":
        servers, _ = _read_servers(spec["path"]())
        changed = [n for n, e in wanted.items()
                   if not _same(servers.get(n), e) and (force or not _serves(servers.get(n), e))]
        if changed:
            _write_toml(spec["path"](), put={n: wanted[n] for n in changed})
        return changed
    servers, _ = _read_servers(spec["path"]())
    changed = []
    for name, entry in wanted.items():
        if _same(servers.get(name), entry) or (not force and _serves(servers.get(name), entry)):
            continue
        if name in servers:
            _claude(["remove", "--scope", "user", name])
        done = _claude(["add-json", "--scope", "user", name, json.dumps(dict(type="stdio", **entry))])
        if done.returncode != 0:
            raise ClientError(f"claude mcp add-json {name}: {(done.stderr or done.stdout).strip()[:300]}")
        changed.append(name)
    return changed


def remove(cid, name):
    spec = CLIENTS[cid]
    if spec["path"]() is None:
        raise ClientError(NO_WINDOWS_HOME)
    if spec["kind"] == "file":
        _write_file(spec["path"](), lambda servers: servers.pop(name, None))
        return
    if spec["kind"] == "toml":
        _write_toml(spec["path"](), drop=[name])
        return
    done = _claude(["remove", "--scope", "user", name])
    if done.returncode != 0:
        raise ClientError(f"claude mcp remove {name}: {(done.stderr or done.stdout).strip()[:300]}")


def has_other(cid, name, entry):
    """Whether the client already has a server called name that isn't this entry (someone else's)."""
    if not available(cid):
        return False
    try:
        servers, _ = _read_servers(CLIENTS[cid]["path"]())
    except ClientError:
        return False
    mine = entry_for(cid, entry)
    return name in servers and not _same(servers[name], mine) and not _serves(servers[name], mine)
