"""This machine's platform (lib/local_platform.py) and what depends on it: opening files and pages, and, under WSL,
the LLM clients' Windows configuration with entries that start the MCP server through wsl.exe."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_agent_console import ConsoleTestCase  # noqa: E402
import agent_profiles  # noqa: E402
import local_platform  # noqa: E402
import mcp_clients  # noqa: E402

LIB = Path(__file__).resolve().parents[1] / "hpclib" / "lib"
CLEAR = ("HPCLIB_PLATFORM", "HPCLIB_WINDOWS_HOME", "WSL_DISTRO_NAME", "WSL_INTEROP")


class PlatformCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in CLEAR:
            os.environ.pop(var, None)
        self.saved = {}
        for patch in (mock.patch.object(local_platform, "settings", lambda: self.saved),
                      mock.patch.object(local_platform, "OSRELEASE", str(self.tmp / "osrelease")),
                      mock.patch.dict(local_platform._asked, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)
        (self.tmp / "osrelease").write_text("6.8.0-generic\n")


class TestDetection(PlatformCase):
    def test_detected(self):
        with mock.patch.object(local_platform.sys, "platform", "linux"):
            self.assertEqual(local_platform.detect(), "linux")
            (self.tmp / "osrelease").write_text("5.15.153.1-microsoft-standard-WSL2\n")
            self.assertEqual(local_platform.detect(), "wsl")
            (self.tmp / "osrelease").write_text("6.8.0\n")
            os.environ["WSL_DISTRO_NAME"] = "Ubuntu"
            self.assertEqual(local_platform.detect(), "wsl")
        with mock.patch.object(local_platform.sys, "platform", "darwin"):
            self.assertEqual(local_platform.detect(), "macos")
        with mock.patch.object(local_platform.sys, "platform", "win32"):
            self.assertEqual(local_platform.detect(), "windows")

    def test_environment_then_setting_then_detected(self):
        with mock.patch.object(local_platform.sys, "platform", "linux"):
            self.assertEqual(local_platform.current(), ("linux", "detected"))
            self.saved["platform"] = "wsl"
            self.assertEqual(local_platform.current(), ("wsl", "settings"))
            os.environ["HPCLIB_PLATFORM"] = "macos"
            self.assertEqual(local_platform.current(), ("macos", "env"))
            os.environ["HPCLIB_PLATFORM"] = "beos"            # not one: ignored
            self.assertEqual(local_platform.current(), ("wsl", "settings"))
            self.saved["platform"] = "auto"
            self.assertEqual(local_platform.current(), ("linux", "detected"))

    def test_windows_home(self):
        # asked from Windows: cmd.exe answers C:\Users\me (no wslpath here: converted by hand)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "cmd.exe").write_text("#!/bin/sh\nprintf 'C:\\\\Users\\\\me\\r\\n'\n")
        (bin_dir / "cmd.exe").chmod(0o755)
        os.environ["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        self.assertEqual(local_platform.windows_home(), ("/mnt/c/Users/me", "windows"))
        self.saved["windows_home"] = "/mnt/d/Users/me"
        self.assertEqual(local_platform.windows_home(), ("/mnt/d/Users/me", "settings"))
        os.environ["HPCLIB_WINDOWS_HOME"] = "/mnt/e/me"
        self.assertEqual(local_platform.windows_home(), ("/mnt/e/me", "env"))

    def test_no_windows_to_ask(self):
        os.environ["PATH"] = str(self.tmp)
        self.assertEqual(local_platform.windows_home(), (None, None))

    def test_paths_without_wslpath(self):
        os.environ["PATH"] = str(self.tmp)
        os.environ["WSL_DISTRO_NAME"] = "Ubuntu-24.04"
        self.assertEqual(local_platform.to_windows("/mnt/c/Users/me/a b.css"), "C:\\Users\\me\\a b.css")
        self.assertEqual(local_platform.to_windows("/home/me/x"), "\\\\wsl.localhost\\Ubuntu-24.04\\home\\me\\x")
        self.assertEqual(local_platform.to_linux("D:\\data\\x"), "/mnt/d/data/x")

    def test_windows_entry(self):
        entry = {"command": "/usr/bin/python3", "args": ["/h/rest_mcp.py", "--url", "http://127.0.0.1:2"]}
        os.environ["WSL_DISTRO_NAME"] = "Ubuntu"
        self.assertEqual(local_platform.windows_entry(entry), {"command": "wsl.exe", "args": [
            "-d", "Ubuntu", "--exec", "/usr/bin/python3", "/h/rest_mcp.py", "--url", "http://127.0.0.1:2"]})
        del os.environ["WSL_DISTRO_NAME"]
        self.assertEqual(local_platform.windows_entry(entry)["args"][:2], ["--exec", "/usr/bin/python3"])


class TestOpening(PlatformCase):
    def test_each_platform(self):
        os.environ["WSL_DISTRO_NAME"] = "Ubuntu"
        with mock.patch.object(local_platform.shutil, "which", return_value=None), \
                mock.patch.object(local_platform, "to_windows", lambda p: "C:\\x\\user.css"):
            for which, target, want in (("linux", "/x/user.css", (["xdg-open", "/x/user.css"], True)),
                                        ("macos", "/x/user.css", (["/usr/bin/open", "/x/user.css"], True)),
                                        ("wsl", "/mnt/c/x/user.css", (["explorer.exe", "C:\\x\\user.css"], False)),
                                        ("wsl", "http://127.0.0.1:8/#key=k", (["explorer.exe", "http://127.0.0.1:8/#key=k"], False))):
                os.environ["HPCLIB_PLATFORM"] = which
                self.assertEqual(local_platform.open_command(target), want)
        with mock.patch.object(local_platform.shutil, "which", return_value="/usr/bin/wslview"):
            os.environ["HPCLIB_PLATFORM"] = "wsl"
            self.assertEqual(local_platform.open_command("/x"), (["wslview", "/x"], True))

    def test_explorer_status_is_not_a_failure(self):
        os.environ["HPCLIB_PLATFORM"] = "wsl"
        with mock.patch.object(local_platform.shutil, "which", return_value=None), \
                mock.patch.object(local_platform, "to_windows", lambda p: "C:\\x"), \
                mock.patch.object(local_platform.subprocess, "Popen") as launch:
            launch.return_value.wait.return_value = 1
            local_platform.open_target("/mnt/c/x")                    # explorer.exe exits 1 when it worked
            os.environ["HPCLIB_PLATFORM"] = "linux"
            with self.assertRaises(OSError):
                local_platform.open_target("/x")


class TestCommandLine(PlatformCase):
    def run_cli(self, *args, stdin=None, **env):
        full = {k: v for k, v in os.environ.items() if k not in CLEAR}
        full.update(env, HPCLIB_CONSOLE_DIR=str(self.tmp / "console"))
        return subprocess.run([sys.executable, str(LIB / "local_platform.py"), *args], input=stdin, text=True,
                              capture_output=True, env=full, timeout=30)

    def test_setup_agents_output(self):
        mcp = json.dumps({"mcpServers": {"hpclib-x": {"command": "/py", "args": ["/rest_mcp.py"]}}})
        out = self.run_cli("client-config", stdin=mcp, HPCLIB_PLATFORM="wsl", WSL_DISTRO_NAME="Debian")
        self.assertEqual(json.loads(out.stdout)["mcpServers"]["hpclib-x"],
                         {"command": "wsl.exe", "args": ["-d", "Debian", "--exec", "/py", "/rest_mcp.py"]})
        out = self.run_cli("client-config", stdin=mcp, HPCLIB_PLATFORM="linux")
        self.assertEqual(json.loads(out.stdout), json.loads(mcp))
        out = self.run_cli("desktop-config", HPCLIB_PLATFORM="wsl", HPCLIB_WINDOWS_HOME="/mnt/c/Users/me", PATH="/usr/bin:/bin")
        self.assertEqual(out.stdout.strip(), "C:\\Users\\me\\AppData\\Roaming\\Claude\\claude_desktop_config.json")

    def test_reads_the_consoles_setting(self):
        (self.tmp / "console").mkdir()
        (self.tmp / "console" / "preferences.json").write_text(json.dumps({"system": {"platform": "wsl"}}))
        self.assertEqual(self.run_cli("platform").stdout.strip(), "wsl")
        self.assertEqual(self.run_cli("platform", HPCLIB_PLATFORM="linux").stdout.strip(), "linux")


class TestConsoleUnderWsl(ConsoleTestCase):
    """The console's LLM clients, its This machine setting and GET /api/platform, under WSL."""

    def setUp(self):
        super().setUp()
        self.winhome = self.tmp / "c" / "Users" / "me"
        self.linuxhome = self.tmp / "home"
        env = mock.patch.dict(os.environ, {"HPCLIB_PLATFORM": "wsl", "HPCLIB_WINDOWS_HOME": str(self.winhome),
                                           "WSL_DISTRO_NAME": "Ubuntu", "HPCLIB_MCP_CLIENT_HOME": str(self.linuxhome),
                                           "HPCLIB_CLAUDE_BIN": ""})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CODEX_HOME", None)
        self.which = {}
        for patch in (mock.patch.object(mcp_clients.shutil, "which", side_effect=lambda name: self.which.get(name)),
                      mock.patch.object(mcp_clients, "CLAUDE_CANDIDATES", ())):   # not this machine's claude
            patch.start()
            self.addCleanup(patch.stop)
        p = agent_profiles.load(self.dead)
        self.name = p["mcp_name"]
        self.entry = {"command": "/py", "args": ["/rest_mcp.py", "--url", f"http://127.0.0.1:{p['port']}"]}
        (Path(agent_profiles.profile_dir(self.dead)) / "mcp.json").write_text(
            json.dumps({"mcpServers": {self.name: self.entry}}))
        self.wrapped = {"command": "wsl.exe", "args": ["-d", "Ubuntu", "--exec", "/py", *self.entry["args"]]}

    def client(self, cid):
        return next(c for c in self.call("GET", "/api/llm-clients")[1]["clients"] if c["id"] == cid)

    def test_windows_clients_get_wsl_exe_entries(self):
        desktop = self.winhome / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
        desktop.parent.mkdir(parents=True)
        c = self.client("claude_desktop")
        self.assertEqual((c["path"], c["windows"], c["available"]), (str(desktop), True, True))
        status, out = self.call("POST", "/api/llm-clients/claude_desktop/write")
        self.assertEqual(status, 200, out)
        self.assertEqual(json.loads(desktop.read_text())["mcpServers"][self.name], self.wrapped)
        self.assertEqual({e["state"] for e in self.client("claude_desktop")["entries"]}, {"current"})
        # Cursor and ChatGPT (Codex) in the Windows profile too
        (self.winhome / ".cursor").mkdir()
        self.call("POST", "/api/llm-clients/cursor/write")
        self.assertEqual(json.loads((self.winhome / ".cursor" / "mcp.json").read_text())["mcpServers"][self.name],
                         self.wrapped)
        (self.winhome / ".codex").mkdir()
        self.assertEqual(self.call("POST", "/api/llm-clients/chatgpt/write")[0], 200)
        self.assertIn('command = "wsl.exe"', (self.winhome / ".codex" / "config.toml").read_text())
        # the profile's own mcp.json keeps the plain entry
        self.assertEqual(json.loads((Path(agent_profiles.profile_dir(self.dead)) / "mcp.json").read_text())
                         ["mcpServers"][self.name], self.entry)

    def test_claude_code_in_wsl_stays_linux(self):
        self.which["claude"] = "/usr/local/bin/claude"
        self.which["claude.exe"] = "/mnt/c/Users/me/.local/bin/claude.exe"
        self.assertFalse(mcp_clients.windows_client("claude_code"))
        self.assertEqual(self.client("claude_code")["path"], str(self.linuxhome / ".claude.json"))
        del self.which["claude"]
        self.which["claude"] = "/mnt/c/Users/me/AppData/Roaming/npm/claude"    # Windows' PATH, appended by WSL
        self.assertTrue(mcp_clients.windows_client("claude_code"))
        c = self.client("claude_code")
        self.assertEqual((c["command"], c["path"]), (self.which["claude.exe"], str(self.winhome / ".claude.json")))

    def test_windows_profile_not_found(self):
        os.environ.pop("HPCLIB_WINDOWS_HOME")
        with mock.patch.object(local_platform, "_ask_windows_home", return_value=None):
            c = self.client("cursor")
            self.assertFalse(c["available"])
            self.assertIn("Windows profile folder", c["error"])
            status, out = self.call("POST", "/api/llm-clients/cursor/write")
            self.assertEqual(status, 422)
            self.assertIn("Console Settings", out["error"])

    def test_setting_and_route(self):
        os.environ.pop("HPCLIB_PLATFORM")
        os.environ.pop("HPCLIB_WINDOWS_HOME")
        with mock.patch.object(local_platform, "detect", return_value="linux"), \
                mock.patch.object(local_platform, "_ask_windows_home", return_value=None):
            out = self.call("GET", "/api/platform")[1]
            self.assertEqual((out["platform"], out["source"], out["setting"]), ("linux", "detected", "auto"))
            status, _ = self.call("PUT", "/api/preferences", {"system": {"platform": "wsl", "windows_home": "/mnt/c/Users/me"}})
            self.assertEqual(status, 200)
            out = self.call("GET", "/api/platform")[1]
            self.assertEqual((out["platform"], out["source"], out["windows_home"], out["windows_home_source"]),
                             ("wsl", "settings", "/mnt/c/Users/me", "settings"))
            self.assertEqual(self.call("GET", "/api/about")[1]["local_platform"]["platform"], "wsl")
            for bad in ({"platform": "windows"}, {"windows_home": "C:\\Users\\me"}, {"windows_home": "/a\nb"},
                        {"other": 1}):
                self.assertEqual(self.call("PUT", "/api/preferences", {"system": bad})[0], 422, bad)


class TestWindowsClaudeDesktop(unittest.TestCase):
    """Where Claude Desktop keeps its configuration on Windows: the classic installer's %APPDATA%\\Claude, the
    Microsoft Store package's LocalCache, or %LOCALAPPDATA%\\Claude-Data."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.roaming, self.local = self.tmp / "Roaming", self.tmp / "Local"
        self.roaming.mkdir()
        self.local.mkdir()

    def find(self):
        return mcp_clients.find_windows_desktop_config(str(self.roaming), str(self.local))

    def test_search(self):
        standard = self.roaming / "Claude" / "claude_desktop_config.json"
        self.assertEqual(self.find(), str(standard))                   # nothing installed: the classic place
        packaged = self.local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
        packaged.mkdir(parents=True)
        self.assertEqual(self.find(), str(packaged / "claude_desktop_config.json"))   # a packaged app's folder, no file yet
        data = self.local / "Claude-Data" / "claude_pzs8sxrjxfjjc"
        data.mkdir(parents=True)
        (data / "claude_desktop_config.json").write_text("{}")
        self.assertEqual(self.find(), str(data / "claude_desktop_config.json"))       # the only file
        (packaged / "claude_desktop_config.json").write_text("{}")
        os.utime(data / "claude_desktop_config.json", (1, 1))
        self.assertEqual(self.find(), str(packaged / "claude_desktop_config.json"))   # the newest file

    def test_wsl_uses_it(self):
        home = self.tmp / "me"
        cfg = home / "AppData" / "Local" / "Claude-Data" / "claude_x" / "claude_desktop_config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("{}")
        with mock.patch.dict(os.environ, {"HPCLIB_PLATFORM": "wsl", "HPCLIB_WINDOWS_HOME": str(home)}), \
                mock.patch.object(mcp_clients, "overrides", lambda: {}):
            self.assertEqual(mcp_clients.desktop_config_path(), str(cfg))


class TestClientPathSettings(ConsoleTestCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"HPCLIB_MCP_CLIENT_HOME": str(self.tmp / "home"), "HPCLIB_CLAUDE_BIN": ""})
        env.start()
        self.addCleanup(env.stop)

    def client(self, cid):
        return next(c for c in self.call("GET", "/api/llm-clients")[1]["clients"] if c["id"] == cid)

    def test_paths_from_console_settings(self):
        self.assertEqual(self.client("cursor")["path"], str(self.tmp / "home" / ".cursor" / "mcp.json"))
        mine = self.tmp / "elsewhere" / "cursor.json"
        claude = self.tmp / "bin" / "claude"
        status, out = self.call("PUT", "/api/preferences", {"llm_client_paths": {
            "cursor": str(mine), "chatgpt": str(self.tmp / "codex" / "config.toml"), "claude_command": str(claude)}})
        self.assertEqual(status, 200, out)
        c = self.client("cursor")
        self.assertEqual((c["path"], c["path_setting"]), (str(mine), str(mine)))
        self.assertEqual(self.client("chatgpt")["path"], str(self.tmp / "codex" / "config.toml"))
        self.assertEqual(mcp_clients.codex_home(), str(self.tmp / "codex"))
        self.assertEqual(self.client("claude_code")["command"], str(claude))
        mine.parent.mkdir()
        self.assertEqual(self.call("POST", "/api/llm-clients/cursor/write")[0], 200)
        self.assertTrue(mine.is_file())
        # empty: found again
        self.call("PUT", "/api/preferences", {"llm_client_paths": {"cursor": ""}})
        self.assertEqual(self.client("cursor")["path_setting"], "")
        for bad in ({"cursor": "relative/mcp.json"}, {"cursor": "/a\nb"}, {"vscode": "/x"}, {"cursor": 3}):
            self.assertEqual(self.call("PUT", "/api/preferences", {"llm_client_paths": bad})[0], 422, bad)

    def test_windows_paths_under_wsl(self):
        with mock.patch.dict(os.environ, {"HPCLIB_PLATFORM": "wsl", "PATH": "/usr/bin:/bin"}), \
                mock.patch.object(local_platform, "to_linux", lambda p: "/mnt/c/Users/me/x.json"):
            self.call("PUT", "/api/preferences", {"llm_client_paths": {"claude_desktop": "C:\\Users\\me\\x.json"}})
            self.assertEqual(mcp_clients.desktop_config_path(), "/mnt/c/Users/me/x.json")


if __name__ == "__main__":
    unittest.main()
