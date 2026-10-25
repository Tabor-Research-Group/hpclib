"""Console Settings, Interface Settings and About: preferences, ports, LLM client entries, diagnostics."""
import json
import os
import socket
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

from test_agent_console import ConsoleTestCase, KEY  # noqa: E402
import agent_console  # noqa: E402
import agent_profiles  # noqa: E402
import console_prefs  # noqa: E402
import mcp_clients  # noqa: E402


class TestPreferences(ConsoleTestCase):
    def test_defaults_update_and_validation(self):
        status, out = self.call("GET", "/api/preferences")
        self.assertEqual(status, 200)
        self.assertEqual(out["notifications"], {"tunnel": False, "prompt": False, "job": False, "proposal": False})
        self.assertEqual(out["new_cluster"]["connection_hours"], 12)
        status, out = self.call("PUT", "/api/preferences", {"notifications": {"job": True}})
        self.assertEqual(status, 200, out)
        self.assertTrue(out["notifications"]["job"])
        self.assertFalse(out["notifications"]["tunnel"])
        mode = stat.S_IMODE(os.stat(self.console / "preferences.json").st_mode)
        self.assertEqual(mode & 0o077, 0)
        for bad in ({"notifications": {"job": "yes"}}, {"nope": {}}, {"new_cluster": {"connection_hours": 0}},
                    {"new_cluster": {"work_dir_pattern": "relative/{user}"}},
                    {"new_cluster": {"work_dir_pattern": "/scratch/{home}"}},
                    {"new_cluster": {"sandbox_gpus": "sometimes"}}, {"llm_clients": {"vim": True}},
                    {"interface": {"common": {"bg": "red"}}},
                    {"interface": {"common": {"--bg": "red; } body { display: none"}}},
                    {"interface": {"light": {"--bg": "url(http://example.com/x.png)"}}},
                    {"interface": {"dark": {"--bg": "red /* x */"}}}):
            with self.subTest(bad=bad):
                self.assertEqual(self.call("PUT", "/api/preferences", bad)[0], 422)

    def test_interface_css_lays_values_over_both_themes(self):
        ui = {"common": {"--radius-control": "2px", "--font-ui": '"Segoe UI", sans-serif'},
              "light": {"--accent": "#7b3fbf"}, "dark": {"--accent": "#c79bff"}}
        self.assertEqual(self.call("PUT", "/api/preferences", {"interface": ui})[0], 200)
        status, body, headers = self.call("GET", "/interface.css", key=None, raw=True)
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/css"))
        css = body.decode()
        root = css.split(":root {", 1)[1].split("}", 1)[0]
        self.assertIn("--accent: #7b3fbf;", root)
        self.assertIn('--font-ui: "Segoe UI", sans-serif;', root)
        self.assertIn(':root:not([data-theme="light"]) {\n  --accent: #c79bff;', css)
        self.assertIn(':root[data-theme="dark"] {\n  --accent: #c79bff;', css)
        # the interface map is replaced whole: what's left out returns to the default
        self.call("PUT", "/api/preferences", {"interface": {"common": {}, "light": {}, "dark": {}}})
        self.assertNotIn("--", self.call("GET", "/interface.css", key=None, raw=True)[1].decode().split("*/", 1)[1])

    def test_new_cluster_defaults_apply_to_added_clusters(self):
        self.call("PUT", "/api/preferences", {"new_cluster": {"connection_hours": 48, "sandbox_gpus": "always",
                                                              "max_gpus": 2, "work_dir_pattern": "/scratch/{user}/llm"}})
        status, out = self.call("POST", "/api/clusters", {"host": "me@new.example.edu"})
        self.assertEqual(status, 201, out)
        profile = agent_profiles.load(out["name"])
        self.assertEqual(profile["connection_hours"], 48)
        self.assertEqual(profile["setup_config"], {"sandbox": {"gpus": "always"}, "limits": {"max_gpus": 2}})
        # a package's config keeps what it says, and gains what it doesn't
        prefs = console_prefs.load(str(self.console))
        self.assertEqual(console_prefs.cluster_config(prefs, {"limits": {"max_gpus": 4}}),
                         {"limits": {"max_gpus": 4}, "sandbox": {"gpus": "always"}})


class TestPorts(ConsoleTestCase):
    def test_range_is_saved_and_used(self):
        status, out = self.call("GET", "/api/ports")
        self.assertEqual(status, 200)
        self.assertEqual(out["range"], [20000, 32000])
        names = {(p["cluster"], p["which"]) for p in out["ports"]}
        self.assertIn((self.live, "port"), names)
        live = next(p for p in out["ports"] if p["cluster"] == self.live and p["which"] == "port")
        self.assertEqual(live["status"], "tunnel")      # a REST server answers behind it: its tunnel
        dead = agent_profiles.load(self.dead)["port"]
        holder = socket.socket()
        holder.bind(("127.0.0.1", dead))
        holder.listen()
        self.addCleanup(holder.close)
        out = self.call("GET", "/api/ports")[1]
        self.assertEqual(next(p for p in out["ports"] if p["port"] == dead)["status"], "taken")
        for bad in ({"low": 80, "high": 9000}, {"low": 30000, "high": 30010}, {"low": "a", "high": 2}):
            self.assertEqual(self.call("PUT", "/api/ports/range", bad)[0], 422)
        self.assertEqual(self.call("PUT", "/api/ports/range", {"low": 40000, "high": 40100})[1]["range"], [40000, 40100])
        self.assertTrue(all(40000 <= agent_profiles.random_port() <= 40100 for _ in range(5)))
        self.assertEqual(len(agent_profiles.all_profiles()), 2)       # .settings.json is not a profile
        self.assertEqual(self.call("PUT", "/api/ports/range", {"reset": True})[1]["range"], [20000, 32000])

    def test_new_ports_rewrite_mcp_and_refuse_while_running(self):
        profile = agent_profiles.load(self.dead)
        mcp = Path(agent_profiles.profile_dir(self.dead)) / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {profile["mcp_name"]: {
            "command": "/py", "args": ["/rest_mcp.py", "--url", f"http://127.0.0.1:{profile['port']}", "--token-file", "/t"]}}}))
        status, out = self.call("POST", f"/api/clusters/{self.dead}/new-ports", {"app": None})
        self.assertEqual(status, 200, out)
        self.assertNotEqual(out["port"], profile["port"])
        self.assertTrue(out["mcp_updated"])
        args = json.loads(mcp.read_text())["mcpServers"][profile["mcp_name"]]["args"]
        self.assertEqual(args[2], f"http://127.0.0.1:{out['port']}")
        self.assertEqual(agent_profiles.load(self.dead)["port"], out["port"])
        # the live cluster's tunnel answers on its port: its ports stay
        self.assertEqual(self.call("POST", f"/api/clusters/{self.live}/new-ports", {"app": None})[0], 409)


class TestLlmClients(ConsoleTestCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        env = mock.patch.dict(os.environ, {"HPCLIB_MCP_CLIENT_HOME": str(self.home), "HPCLIB_CLAUDE_BIN": ""})
        env.start()
        self.addCleanup(env.stop)
        self.which = mock.patch.object(mcp_clients.shutil, "which", return_value=None)
        self.which.start()
        self.addCleanup(self.which.stop)
        self.cursor = self.home / ".cursor" / "mcp.json"
        self.entries = {}
        for name in (self.live, self.dead):
            p = agent_profiles.load(name)
            entry = {"command": "/py", "args": ["/rest_mcp.py", "--url", f"http://127.0.0.1:{p['port']}"]}
            (Path(agent_profiles.profile_dir(name)) / "mcp.json").write_text(json.dumps({"mcpServers": {p["mcp_name"]: entry}}))
            self.entries[p["mcp_name"]] = entry

    def client(self, cid):
        return next(c for c in self.call("GET", "/api/llm-clients")[1]["clients"] if c["id"] == cid)

    def test_cursor_entries_written_beside_the_users_own(self):
        self.assertFalse(self.client("cursor")["available"])
        self.assertEqual(self.call("POST", "/api/llm-clients/cursor/write")[0], 422)      # not installed
        self.cursor.parent.mkdir(parents=True)
        mine = {"mcpServers": {"github": {"command": "gh-mcp"}, "hpclib-gone": {"command": "/old"}}, "theme": "x"}
        self.cursor.write_text(json.dumps(mine))
        self.cursor.chmod(0o640)
        c = self.client("cursor")
        self.assertEqual({e["state"] for e in c["entries"]}, {"missing"})
        self.assertEqual(c["stale"], ["hpclib-gone"])
        status, out = self.call("POST", "/api/llm-clients/cursor/write")
        self.assertEqual(status, 200, out)
        self.assertEqual(sorted(out["changed"]), sorted(self.entries))
        data = json.loads(self.cursor.read_text())
        self.assertEqual(data["theme"], "x")
        self.assertEqual(data["mcpServers"]["github"], {"command": "gh-mcp"})
        for name, entry in self.entries.items():
            self.assertEqual(data["mcpServers"][name], entry)
        self.assertEqual(stat.S_IMODE(self.cursor.stat().st_mode), 0o640)
        self.assertEqual(json.loads(Path(str(self.cursor) + ".before-hpclib").read_text()), mine)
        self.assertEqual({e["state"] for e in self.client("cursor")["entries"]}, {"current"})
        # only a stale hpclib entry can be removed
        self.assertEqual(self.call("POST", "/api/llm-clients/cursor/remove", {"name": "github"})[0], 422)
        self.assertEqual(self.call("POST", "/api/llm-clients/cursor/remove", {"name": "hpclib-gone"})[0], 200)
        self.assertNotIn("hpclib-gone", json.loads(self.cursor.read_text())["mcpServers"])
        self.assertIn("github", json.loads(self.cursor.read_text())["mcpServers"])

    def test_unreadable_configuration_is_left_alone(self):
        self.cursor.parent.mkdir(parents=True)
        self.cursor.write_text("{ not json")
        self.assertIn("isn't readable JSON", self.client("cursor")["error"])
        self.assertEqual(self.call("POST", "/api/llm-clients/cursor/write")[0], 422)
        self.assertEqual(self.cursor.read_text(), "{ not json")

    def test_kept_up_to_date_after_new_ports(self):
        self.cursor.parent.mkdir(parents=True)
        self.call("PUT", "/api/preferences", {"llm_clients": {"cursor": True}})
        status, out = self.call("POST", f"/api/clusters/{self.dead}/new-ports", {"app": None})
        self.assertEqual(status, 200, out)
        name = agent_profiles.load(self.dead)["mcp_name"]
        self.assertEqual(json.loads(self.cursor.read_text())["mcpServers"][name]["args"][2],
                         f"http://127.0.0.1:{out['port']}")
        self.assertIn(name, out["llm_clients"]["cursor"])

    def test_claude_code_through_its_command(self):
        log = self.tmp / "claude.log"
        fake = self.tmp / "claude"
        fake.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"HPCLIB_CLAUDE_BIN": str(fake)}):
            (self.home).mkdir(exist_ok=True)
            (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {next(iter(self.entries)): {"command": "/old"}}}))
            c = self.client("claude_code")
            self.assertTrue(c["available"])
            self.assertEqual(sorted(e["state"] for e in c["entries"]), ["different", "missing"])
            status, out = self.call("POST", "/api/llm-clients/claude_code/write")
        self.assertEqual(status, 200, out)
        calls = log.read_text().splitlines()
        old = next(iter(self.entries))
        removed = calls.index(f"mcp remove --scope user {old}")
        self.assertTrue(calls[removed + 1].startswith(f"mcp add-json --scope user {old} "))
        self.assertEqual(sum(c.startswith("mcp add-json --scope user hpclib-") for c in calls), 2)
        added = [c for c in calls if c.startswith("mcp add-json")][0]
        self.assertEqual(json.loads(added.split(" ", 5)[5])["type"], "stdio")


class TestChatGptAndNames(TestLlmClients):
    """ChatGPT through Codex's config.toml, and clusters' own MCP names."""

    def setUp(self):
        super().setUp()
        self.codex = self.home / ".codex" / "config.toml"

    def toml(self):
        import tomllib
        return tomllib.loads(self.codex.read_text())

    def test_chatgpt_entries_in_config_toml_keep_the_rest(self):
        self.assertFalse(self.client("chatgpt")["available"])
        self.codex.parent.mkdir(parents=True)
        name = next(iter(self.entries))
        mine = ('model = "gpt-5"\n\n[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y", "@upstash/context7-mcp"]\n\n'
                f'[mcp_servers.{name}]\ncommand = "/old"\nargs = [\n  "/old.py",\n]\nstartup_timeout_sec = 30\n\n'
                f'[mcp_servers.{name}.env]\nA = "b"\n')
        self.codex.write_text(mine)
        c = self.client("chatgpt")
        self.assertTrue(c["available"])
        self.assertEqual(sorted(e["state"] for e in c["entries"]), ["different", "missing"])
        status, out = self.call("POST", "/api/llm-clients/chatgpt/write")
        self.assertEqual(status, 200, out)
        data = self.toml()
        self.assertEqual(data["model"], "gpt-5")
        self.assertEqual(data["mcp_servers"]["context7"]["command"], "npx")
        for n, entry in self.entries.items():
            self.assertEqual({k: data["mcp_servers"][n][k] for k in ("command", "args")}, entry)
        self.assertEqual(data["mcp_servers"][name]["startup_timeout_sec"], 30)      # its other keys stay
        self.assertEqual(data["mcp_servers"][name]["env"], {"A": "b"})
        self.assertEqual(Path(str(self.codex) + ".before-hpclib").read_text(), mine)
        self.assertEqual({e["state"] for e in self.client("chatgpt")["entries"]}, {"current"})
        self.codex.write_text("not = [valid")
        self.assertEqual(self.call("POST", "/api/llm-clients/chatgpt/write")[0], 422)
        self.assertEqual(self.codex.read_text(), "not = [valid")

    def test_rename_updates_mcp_json_and_clients_and_removes_the_old_name(self):
        self.cursor.parent.mkdir(parents=True)
        self.codex.parent.mkdir(parents=True)
        self.call("PUT", "/api/preferences", {"llm_clients": {"cursor": True, "chatgpt": True}})
        for cid in ("cursor", "chatgpt"):
            self.assertEqual(self.call("POST", f"/api/llm-clients/{cid}/write")[0], 200)
        old = agent_profiles.load(self.dead)["mcp_name"]
        for bad in ("", "-x", "has space", "x" * 65, agent_profiles.load(self.live)["mcp_name"]):
            self.assertEqual(self.call("PUT", f"/api/clusters/{self.dead}/mcp-name", {"name": bad})[0], 422, bad)
        # a name a client uses for something else is refused
        data = json.loads(self.cursor.read_text())
        data["mcpServers"]["github"] = {"command": "gh-mcp"}
        self.cursor.write_text(json.dumps(data))
        self.assertEqual(self.call("PUT", f"/api/clusters/{self.dead}/mcp-name", {"name": "github"})[0], 409)
        status, out = self.call("PUT", f"/api/clusters/{self.dead}/mcp-name", {"name": "tamu_dead"})
        self.assertEqual(status, 200, out)
        profile = agent_profiles.load(self.dead)
        self.assertEqual(profile["mcp_name"], "tamu_dead")
        self.assertEqual(profile["previous_mcp_names"], [old])
        mcp = json.loads((Path(agent_profiles.profile_dir(self.dead)) / "mcp.json").read_text())
        self.assertEqual(list(mcp["mcpServers"]), ["tamu_dead"])
        cursor = json.loads(self.cursor.read_text())["mcpServers"]
        self.assertIn("tamu_dead", cursor)
        self.assertNotIn(old, cursor)
        self.assertIn("github", cursor)
        self.assertIn("tamu_dead", self.toml()["mcp_servers"])
        self.assertNotIn(old, self.toml()["mcp_servers"])
        # renaming again: the earlier custom name is the console's to clean up too
        self.call("PUT", f"/api/clusters/{self.dead}/mcp-name", {"name": "dead2"})
        self.assertNotIn("tamu_dead", json.loads(self.cursor.read_text())["mcpServers"])

    def test_new_clusters_named_by_the_pattern(self):
        self.assertEqual(self.call("PUT", "/api/preferences", {"new_cluster": {"mcp_name_pattern": "{user} {x}"}})[0], 422)
        self.call("PUT", "/api/preferences", {"new_cluster": {"mcp_name_pattern": "tamu-{cluster}-{user}"}})
        status, out = self.call("POST", "/api/clusters", {"host": "ana@grace.hprc.tamu.edu"})
        self.assertEqual(status, 201, out)
        self.assertEqual(agent_profiles.load(out["name"])["mcp_name"], "tamu-grace-ana")
        status, out = self.call("POST", "/api/clusters", {"host": "ana@grace.example.org"})
        self.assertEqual(agent_profiles.load(out["name"])["mcp_name"], "tamu-grace-ana-2")
        self.assertEqual(agent_profiles.mcp_name_for("me@faster.hprc.tamu.edu", set()), "hpclib-faster")

    def test_setup_agents_mcp_name_keeps_the_previous_one(self):
        old = agent_profiles.load(self.dead)["mcp_name"]
        self.assertEqual(agent_profiles.cmd_set(self.dead, "mcp_name=bad name"), 2)
        self.assertEqual(agent_profiles.cmd_set(self.dead, "mcp_name=my-cluster"), 0)
        self.assertEqual(agent_profiles.load(self.dead)["previous_mcp_names"], [old])


class TestAboutAndDiagnostics(ConsoleTestCase):
    def test_about_and_redacted_diagnostics(self):
        status, out = self.call("GET", "/api/about")
        self.assertEqual(status, 200)
        self.assertEqual(out["hpclib_version"], agent_console.local_hpclib_version())
        self.assertIsNone(out["restart_for"])
        self.assertEqual(out["url"], self.base)
        with mock.patch.object(agent_console, "STARTED_VERSION", "0.0.1"):
            self.assertEqual(self.call("GET", "/api/about")[1]["restart_for"], agent_console.local_hpclib_version())
        status, out = self.call("GET", "/api/diagnostics")
        self.assertEqual(status, 200, out)
        text = json.dumps(out)
        self.assertEqual({c["name"] for c in out["clusters"]}, {self.live, self.dead})
        self.assertNotIn(KEY, text)
        for token_file in ("owner_token", "agent_token"):
            p = Path(agent_profiles.profile_dir(self.live)) / token_file
            self.assertNotIn(p.read_text().strip(), text)
        self.assertEqual(agent_console.redact("/Users/me/Documents/Postdoc/Development/hpclib/hpclib"),
                         "/Users/me/Documents/Postdoc/Development/hpclib/hpclib")      # paths stay readable
        self.assertEqual(agent_console.redact("error: abcdefghijklmnopqrstuvwxyzABCDEF0123 failed"),
                         "error: [redacted] failed")

    def test_watch_reports_ports_and_stops(self):
        status, out = self.call("GET", "/api/watch")
        self.assertEqual(status, 200)
        by = {t["cluster"]: t for t in out["tunnels"] if t["app"] is None}
        self.assertTrue(by[self.live]["up"])
        self.assertFalse(by[self.dead]["up"])
        self.assertIsNone(by[self.dead]["stopped"])


if __name__ == "__main__":
    unittest.main()
