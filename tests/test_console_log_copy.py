"""Copy complete recent console blocks without exposing arbitrary files or truncating to display tails."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_agent_console import ConsoleTestCase
import agent_console

HEADER = "=== 2026-10-18 10:08:54 agent_tunnel fixture@faster.example ==="


class TestLogBlockReader(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "console.log"

    def test_last_header_is_preserved_and_terminal_controls_are_removed(self):
        self.path.write_text("=== 2026-10-18 09:00:00 previous ===\nold output\n\n" + HEADER +
                             "\r\n\x1b]0;terminal title\x07\x1b[31merror\x1b[0m\r\n--- side command ---\n")
        self.assertEqual(agent_console.last_log_block(self.path), HEADER + "\nerror\n--- side command ---\n")

    def test_header_across_read_chunk_boundary_and_long_history(self):
        last = HEADER + "\n" + "output\n" * 10000
        self.path.write_text("old output\n" * 50000 + last)
        self.assertEqual(agent_console.last_log_block(self.path), last)

    def test_empty_missing_unmarked_and_oversized_logs(self):
        self.assertEqual(agent_console.last_log_block(self.path), "")
        self.path.write_text("")
        self.assertEqual(agent_console.last_log_block(self.path), "")
        self.path.write_text("unmarked output\n")
        self.assertEqual(agent_console.last_log_block(self.path), "unmarked output\n")
        self.path.write_text(HEADER + "\n" + "x" * 1000)
        with self.assertRaises(agent_console.ConsoleError) as error:
            agent_console.last_log_block(self.path, max_bytes=256)
        self.assertEqual(error.exception.status, 413)


class TestLogCopyAPI(ConsoleTestCase):
    def test_agent_app_and_operation_copy_include_more_than_the_visible_tail(self):
        last = HEADER + "\n" + "line\n" * 1000
        paths = [(self.clusters.log_path(self.dead), f"/api/clusters/{self.dead}/tunnel/log"),
                 (self.clusters.app_log_path(self.dead, "vscode"), f"/api/apps/vscode/{self.dead}/log"),
                 (self.clusters.ops_log_path(self.dead), f"/api/clusters/{self.dead}/operation")]
        for path, route in paths:
            with self.subTest(route=route):
                file = Path(path)
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("=== 2026-10-18 09:00:00 old ===\n" + "old\n" * 100000 + last)
                status, out = self.call("GET", route + "?lines=3&latest=1")
                self.assertEqual(status, 200, out)
                self.assertEqual(out["copy_text"], last)
                regular = self.call("GET", route + "?lines=3")[1]
                self.assertEqual(len(regular.get("lines", regular.get("log"))), 3)

    def test_copy_routes_require_the_console_key(self):
        for route in (f"/api/clusters/{self.dead}/tunnel/log", f"/api/apps/vscode/{self.dead}/log",
                      f"/api/clusters/{self.dead}/operation"):
            self.assertEqual(self.call("GET", route + "?latest=1", key="wrong")[0], 401)

    def test_failed_shutdown_preserves_the_managed_tunnel(self):
        for app in (None, "vscode"):
            with self.subTest(app=app):
                procs = self.clusters.procs if app is None else self.clusters.app_procs
                key = self.dead if app is None else (self.dead, app)
                proc = mock.Mock()
                proc.poll.return_value = None
                procs[key] = proc
                stopper = mock.Mock()
                stopper.wait.return_value = 1
                route = f"/api/clusters/{self.dead}/tunnel/stop" if app is None else f"/api/apps/{app}/{self.dead}/stop"
                with mock.patch.object(self.clusters, "shell_runner", return_value=stopper), \
                        mock.patch.object(agent_console.os, "killpg") as kill:
                    status, out = self.call("POST", route)
                self.assertEqual(status, 502, out)
                self.assertIn("retry Stop", out["error"])
                self.assertIs(procs.pop(key), proc)
                kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
