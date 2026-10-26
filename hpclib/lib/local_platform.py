#!/usr/bin/env python3
"""
The machine hpclib runs on (yours, not a cluster), for the few things that differ between platforms: opening a
file or a page, and where the LLM clients keep their MCP configuration.

  linux    xdg-open; the clients' Linux configuration
  macos    open; ~/Library/Application Support
  wsl      Linux in WSL on Windows. hpclib, ssh, the tunnels and the console run in WSL, while the browser and
           the LLM clients are Windows programs: files and pages open with wslview, else explorer.exe; Claude
           Desktop, Cursor and ChatGPT (Codex) get their Windows configuration (%APPDATA%, %USERPROFILE%), with
           entries that start the MCP server in WSL (wsl.exe -d DISTRO --exec PYTHON rest_mcp.py ...). Claude
           Code installed in WSL keeps a Linux entry; installed on Windows only, it gets a wsl.exe one.
  windows  native Windows (detected only; hpclib's shell side doesn't run there): os.startfile

Which one, first set wins: $HPCLIB_PLATFORM; the console's Console Settings -> This machine (preferences.json,
system.platform); detected (WSL by $WSL_DISTRO_NAME / $WSL_INTEROP or "microsoft" in the kernel's release).
Under WSL, your Windows profile folder as WSL sees it (/mnt/c/Users/NAME) comes from $HPCLIB_WINDOWS_HOME, then
system.windows_home, then asking Windows (cmd.exe's %USERPROFILE%, through wslpath).

  local_platform.py describe            what hpclib uses, as JSON
  local_platform.py platform            linux, macos, wsl (or windows)
  local_platform.py client-config       mcp.json on stdin ({"mcpServers": {...}}) as Claude Desktop runs it (under
                                        WSL: each entry through wsl.exe)
  local_platform.py desktop-config      Claude Desktop's configuration file (under WSL, its Windows path)

Standard library only.
"""
import json
import os
import re
import shutil
import subprocess
import sys

PLATFORMS = ("linux", "macos", "wsl")          # what can be chosen
CHOICES = ("auto",) + PLATFORMS
TITLES = {"linux": "Linux", "macos": "macOS", "wsl": "WSL (Linux on Windows)", "windows": "Windows"}
OSRELEASE = "/proc/sys/kernel/osrelease"


def console_preferences():
    """The console's preferences.json (Console Settings), read where the console keeps it ({} if there's none)."""
    base = os.path.expanduser(os.environ.get("HPCLIB_CONSOLE_DIR") or "~/.config/hpclib/console")
    try:
        with open(os.path.join(base, "preferences.json")) as f:
            prefs = json.load(f)
    except (OSError, ValueError):
        return {}
    return prefs if isinstance(prefs, dict) else {}


def _console_settings():
    """preferences.json's system section."""
    system = console_preferences().get("system")
    return system if isinstance(system, dict) else {}


settings = _console_settings      # the console may point this at its own reader


def detect():
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return "wsl"
    try:
        with open(OSRELEASE) as f:
            release = f.read().lower()
    except OSError:
        release = ""
    return "wsl" if "microsoft" in release or "wsl" in release else "linux"


def current():
    """(platform, source): source is env, settings or detected."""
    env = (os.environ.get("HPCLIB_PLATFORM") or "").strip().lower()
    if env in PLATFORMS:
        return env, "env"
    chosen = settings().get("platform")
    if chosen in PLATFORMS:
        return chosen, "settings"
    return detect(), "detected"


def platform():
    return current()[0]


def wsl_distro():
    """The WSL distribution hpclib runs in (None: wsl.exe's default)."""
    return os.environ.get("WSL_DISTRO_NAME") or None


def to_linux(path):
    """A Windows path (C:\\Users\\me) as WSL sees it (/mnt/c/Users/me)."""
    try:
        out = subprocess.run(["wslpath", "-u", path], capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    m = re.match(r"([A-Za-z]):[\\/]*(.*)$", path)
    return f"/mnt/{m.group(1).lower()}/{m.group(2).replace(chr(92), '/')}".rstrip("/") if m else None


def to_windows(path):
    """A WSL path as Windows programs see it (C:\\... under /mnt/X, else \\\\wsl.localhost\\DISTRO\\...)."""
    try:
        out = subprocess.run(["wslpath", "-w", path], capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    path = os.path.abspath(path)
    m = re.match(r"/mnt/([a-z])(/.*)?$", path)
    if m:
        return f"{m.group(1).upper()}:" + (m.group(2) or "\\").replace("/", "\\")
    return f"\\\\wsl.localhost\\{wsl_distro() or 'Ubuntu'}" + path.replace("/", "\\")


_asked = {}


def _ask_windows_home():
    if "home" not in _asked:
        home = None
        try:
            out = subprocess.run(["cmd.exe", "/d", "/c", "echo %USERPROFILE%"], capture_output=True, text=True,
                                 timeout=15, stdin=subprocess.DEVNULL,
                                 cwd="/mnt/c" if os.path.isdir("/mnt/c") else None)   # no "UNC paths" warning
            value = out.stdout.strip().splitlines()[-1].strip() if out.stdout.strip() else ""
            if value and "%" not in value:
                home = to_linux(value)
        except (OSError, subprocess.TimeoutExpired, IndexError):
            pass
        _asked["home"] = home
    return _asked["home"]


def windows_home():
    """(path or None, source): your Windows profile folder as WSL sees it."""
    for value, source in ((os.environ.get("HPCLIB_WINDOWS_HOME"), "env"), (settings().get("windows_home"), "settings")):
        if value:
            return os.path.expanduser(value), source
    home = _ask_windows_home()
    return (home, "windows") if home else (None, None)


def windows_side():
    """Whether the LLM clients are Windows programs that reach hpclib through wsl.exe."""
    return platform() == "wsl"


def windows_entry(entry):
    """An MCP entry for a Windows client: wsl.exe starts the same command in this distribution."""
    distro = wsl_distro()
    return {"command": "wsl.exe",
            "args": [*(["-d", distro] if distro else []), "--exec", entry["command"], *entry["args"]]}


def open_command(target):
    """(argv, check_status) that opens a file or URL with this machine's default program; None: os.startfile."""
    which = platform()
    if which == "windows":
        return None
    if which == "macos":
        return ["/usr/bin/open", target], True
    if which == "wsl":
        if shutil.which("wslview"):
            return ["wslview", target], True
        # explorer.exe opens files and URLs with their Windows program; it exits 1 even when it worked
        is_url = re.match(r"[a-z][a-z0-9+.-]*://", target, re.I)
        return ["explorer.exe", target if is_url else to_windows(target)], False
    return ["xdg-open", target], True


def open_target(target):
    """Open a file or URL with the default program, without waiting for it (OSError if that failed at once)."""
    command = open_command(target)
    if command is None:
        os.startfile(target)
        return
    argv, check = command
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        status = proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return      # some openers stay running with the program
    if status and check:
        raise OSError(f"{argv[0]} exited with status {status}")


def describe():
    which, source = current()
    out = {"platform": which, "title": TITLES.get(which, which), "source": source, "detected": detect(),
           "choices": list(CHOICES), "setting": settings().get("platform") or "auto",
           "env": os.environ.get("HPCLIB_PLATFORM") or None}
    if which == "wsl":
        home, home_source = windows_home()
        out.update(wsl_distro=wsl_distro(), windows_home=home, windows_home_source=home_source,
                   windows_home_setting=settings().get("windows_home") or "",
                   opener="wslview" if shutil.which("wslview") else "explorer.exe")
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["describe"]:
        print(json.dumps(describe(), indent=2))
        return 0
    if argv == ["platform"]:
        print(platform())
        return 0
    if argv == ["client-config"]:
        data = json.load(sys.stdin)
        if windows_side():
            data["mcpServers"] = {k: windows_entry(v) for k, v in data["mcpServers"].items()}
        print(json.dumps(data, indent=2))
        return 0
    if argv == ["desktop-config"]:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "servers"))
        import mcp_clients
        path = mcp_clients.desktop_config_path()
        if path and windows_side():
            path = to_windows(path)
        print(path or "%APPDATA%\\Claude\\claude_desktop_config.json")
        return 0
    print("usage: local_platform.py describe | platform | client-config | desktop-config", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
