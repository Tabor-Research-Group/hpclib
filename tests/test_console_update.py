"""Updating this machine's hpclib from the console (console_update.py, GET/POST /api/update) and restarting the
console (POST /api/restart): with a local bare repository standing in for GitHub."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from test_agent_console import ConsoleTestCase  # noqa: E402
import agent_console  # noqa: E402
import console_update  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "init.defaultBranch=master"]


def git(repo, *args):
    return subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class Repos:
    """upstream.git (GitHub), a clone (this machine's hpclib), and a second clone to push new commits from."""

    def __init__(self, tmp):
        self.upstream, self.clone, self.dev = tmp / "upstream.git", tmp / "hpclib", tmp / "dev"
        subprocess.run([*GIT, "init", "--quiet", "--bare", str(self.upstream)], check=True)
        subprocess.run([*GIT, "clone", "--quiet", str(self.upstream), str(self.dev)], check=True, capture_output=True)
        self.version("1.4.0", "first")
        subprocess.run([*GIT, "clone", "--quiet", str(self.upstream), str(self.clone)], check=True, capture_output=True)

    def version(self, v, message):
        (self.dev / "hpclib").mkdir(exist_ok=True)
        (self.dev / "hpclib" / "hpclib.sh").write_text(f'HPCLIB_VERSION="{v}"\n')
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "--quiet", "-m", message)
        git(self.dev, "push", "--quiet", "origin", "HEAD")


class TestUpdater(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.repos = Repos(self.tmp)
        self.updater = console_update.Updater(str(self.repos.clone))

    def test_check_and_fast_forward(self):
        st = self.updater.status(fetch=True)
        self.assertEqual((st["branch"], st["upstream"], st["behind"], st["up_to_date"]), ("master", "origin/master", 0, True))
        self.repos.version("1.4.1", "fix the thing")
        self.repos.version("1.5.0", "a new feature")
        st = self.updater.status()                       # not fetched yet: doesn't know
        self.assertEqual(st["behind"], 0)
        st = self.updater.status(fetch=True)
        self.assertEqual((st["behind"], st["can_update"], st["upstream_version"], st["version"]), (2, True, "1.5.0", "1.4.0"))
        self.assertEqual([c["subject"] for c in st["incoming"]], ["a new feature", "fix the thing"])
        out = self.updater.update()
        self.assertTrue(out["updated"])
        self.assertEqual((out["from"]["version"], out["to"]["version"]), ("1.4.0", "1.5.0"))
        self.assertEqual((self.repos.clone / "hpclib" / "hpclib.sh").read_text(), 'HPCLIB_VERSION="1.5.0"\n')
        self.assertFalse(self.updater.update()["updated"])                  # nothing new

    def test_your_changes_and_commits_stop_it(self):
        self.repos.version("1.4.1", "upstream change")
        sh = self.repos.clone / "hpclib" / "hpclib.sh"
        sh.write_text('HPCLIB_VERSION="mine"\n')
        with self.assertRaises(console_update.UpdateError) as e:
            self.updater.update()
        self.assertIn("changes of your own (hpclib/hpclib.sh)", str(e.exception))
        self.assertEqual(sh.read_text(), 'HPCLIB_VERSION="mine"\n')                # untouched
        git(self.repos.clone, "commit", "--quiet", "-am", "my own commit")
        with self.assertRaises(console_update.UpdateError) as e:
            self.updater.update()
        self.assertIn("1 commit(s) origin/master doesn't", str(e.exception))
        (self.repos.clone / "notes.txt").write_text("untracked files don't matter")
        git(self.repos.clone, "reset", "--quiet", "--hard", "origin/master~1")
        self.assertTrue(self.updater.update()["updated"])

    def test_not_a_clone_and_an_unreachable_remote(self):
        plain = self.tmp / "copy"
        (plain / "hpclib").mkdir(parents=True)
        st = console_update.Updater(str(plain)).status()
        self.assertFalse(st["git"])
        self.assertIn("isn't a git clone", st["reason"])
        git(self.repos.clone, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        st = self.updater.status(fetch=True)
        self.assertTrue(st["fetch_error"])
        with self.assertRaises(console_update.UpdateError) as e:
            self.updater.update()
        self.assertEqual(e.exception.status, 502)

    def test_credentials_in_the_remote_are_not_shown(self):
        git(self.repos.clone, "remote", "set-url", "origin", "https://me:ghp_secret@github.com/x/hpclib.git")
        self.assertEqual(self.updater.status()["remote_url"], "https://github.com/x/hpclib.git")


class TestUpdateRoutes(ConsoleTestCase):
    def setUp(self):
        super().setUp()
        self.repos = Repos(Path(self.tmp))
        self.console_server.updater = console_update.Updater(str(self.repos.clone))

    def test_check_update_and_restart(self):
        self.repos.version("1.5.0", "new")
        status, out = self.call("GET", "/api/update?fetch=1")
        self.assertEqual(status, 200, out)
        self.assertEqual((out["behind"], out["can_update"], out["running"]), (1, True, agent_console.STARTED_VERSION), out)
        status, out = self.call("POST", "/api/update")
        self.assertEqual((status, out["updated"], out["to"]["version"]), (200, True, "1.5.0"))
        # restart: refused while something runs; otherwise this process is exec'd again with the key in its env
        self.assertEqual(self.call("POST", "/api/restart")[0], 409)               # no restart_argv in tests
        self.console_server.restart_argv = [sys.executable, "agent_console.py", "--static", "/x", "--port", "1"]
        self.clusters.ops["x"] = {"state": "running", "kind": "setup"}
        status, out = self.call("POST", "/api/restart")
        self.assertEqual(status, 409)
        self.assertIn("x: setup", out["error"])
        del self.clusters.ops["x"]
        with mock.patch.object(agent_console.os, "execv") as execv, mock.patch.dict(os.environ):
            self.assertEqual(self.call("POST", "/api/restart")[0], 202)
            for _ in range(40):
                if execv.called:
                    break
                time.sleep(0.05)
            execv.assert_called_once_with(sys.executable, self.console_server.restart_argv)
            self.assertEqual(os.environ[agent_console.ConsoleServer.RESTART_KEY_VAR], self.console_server.key)


class TestRealRestart(unittest.TestCase):
    """The console started, restarted through the API, and answering again on its port with the same key."""

    def test_restart_keeps_port_and_key(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = dict(os.environ, HPCLIB_CONSOLE_DIR=str(tmp / "console"), HPCLIB_AGENTS_DIR=str(tmp / "agents"),
                   HOME=str(tmp))
        env.pop(agent_console.ConsoleServer.RESTART_KEY_VAR, None)
        env.pop("HPCLIB_CONSOLE_RESTARTED", None)
        proc = subprocess.Popen([sys.executable, str(ROOT / "hpclib" / "servers" / "agent_console.py"), "--port", str(port),
                                 "--static", str(ROOT / "agent-console")], env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        key = None
        for _ in range(100):
            try:
                key = json.loads((tmp / "console" / "session").read_text())["key"]
                break
            except (OSError, ValueError):
                time.sleep(0.1)
        self.assertIsNotNone(key)

        def call(method, path):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{path}", method=method, data=b"{}" if method == "POST" else None,
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())

        first = None
        for _ in range(50):
            try:
                first = call("GET", "health")
                break
            except OSError:
                time.sleep(0.1)
        self.assertIsNotNone(first)
        self.assertTrue(call("POST", "restart")["restarting"])
        again = None
        for _ in range(100):
            time.sleep(0.1)
            try:
                again = call("GET", "health")
                if again["started"] != first["started"]:
                    break
            except OSError:
                pass
        self.assertIsNotNone(again)
        self.assertNotEqual(again["started"], first["started"])
        self.assertEqual(again["pid"], first["pid"])                    # the same process, exec'd again
        self.assertIsNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
