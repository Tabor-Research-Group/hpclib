"""Notifications from apps and widgets: the manifest's kinds, the switches in Console Settings, the console's poll."""
import http.server
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_agent_console import ConsoleTestCase  # noqa: E402
import agent_console  # noqa: E402
import agent_profiles  # noqa: E402
import app_notifications  # noqa: E402
import console_packages  # noqa: E402
import console_prefs  # noqa: E402

KINDS = [{"id": "done", "label": "A run ended", "hint": "any outcome"}, {"id": "slow", "label": "Something is slow"}]


class TestNotifier(unittest.TestCase):
    def test_poll_numbers_and_the_first_look_takes_nothing(self):
        n = app_notifications.Notifier(keep=3)
        self.assertEqual(n.poll(None), {"latest": 0, "notifications": []})
        for i in range(5):
            n.notify("done", "t%d" % i, "b" * 500, hash="#/x")
        first = n.poll(None)
        self.assertEqual((first["latest"], first["notifications"]), (5, []))          # a first look: where it stands
        got = n.poll(2)
        self.assertEqual([i["id"] for i in got["notifications"]], [3, 4, 5])           # the last 3 are kept
        self.assertEqual(len(got["notifications"][0]["body"]), app_notifications.MAX_BODY)
        self.assertEqual(n.poll(5)["notifications"], [])
        self.assertEqual(n.poll("junk")["notifications"], [])


class TestPreferencesAndManifests(unittest.TestCase):
    def test_app_notifications_are_checked(self):
        good = {"app:fsdiag": {"enabled": True, "kinds": {"run_finished": True}},
                "widget:pkg/clock": {"enabled": False, "kinds": {}}}
        self.assertEqual(console_prefs._app_notifications(good)["app:fsdiag"]["kinds"], {"run_finished": True})
        for bad in ({"fsdiag": {"enabled": True}}, {"app:Bad": {"enabled": True}}, {"app:a": {"enabled": "yes"}},
                    {"app:a": {"enabled": True, "kinds": {"Done": True}}}, {"app:a": {"enabled": True, "kinds": {"x": 1}}},
                    {"app:a": {"enabled": True, "extra": 1}}, ["app:a"]):
            with self.subTest(bad=bad), self.assertRaises(console_prefs.PrefsError):
                console_prefs._app_notifications(bad)
        prefs = {"app_notifications": good}
        self.assertTrue(console_prefs.app_note_wanted(prefs, "app:fsdiag", "run_finished"))
        self.assertFalse(console_prefs.app_note_wanted(prefs, "app:fsdiag", "other"))
        self.assertFalse(console_prefs.app_note_wanted(prefs, "widget:pkg/clock", "x"))
        self.assertFalse(console_prefs.app_note_wanted(prefs, "app:none", "x"))

    def test_kinds_in_a_manifest(self):
        self.assertEqual(console_packages.check_notification_kinds(None, "app a"), [])
        self.assertEqual(console_packages.check_notification_kinds(KINDS, "app a")[1], {"id": "slow", "label": "Something is slow", "hint": ""})
        for bad in ("done", [{"id": "Done", "label": "x"}], [{"id": "a", "label": "x"}, {"id": "a", "label": "y"}],
                    [{"id": "a"}], [{"id": "a", "label": "x", "colour": 1}], [{"id": str(i), "label": "x"} for i in range(13)]):
            with self.subTest(bad=bad), self.assertRaises(console_packages.PackageError):
                console_packages.check_notification_kinds(bad, "app a")


class TestPackagedApps(unittest.TestCase):
    def check(self, **app):
        members = {"hpclib-package.json": b"", "t/sbatch_script.sh": b"x"}
        manifest = {"format": 1, "name": "demo", "apps": {"b": dict({"title": "B", "tunnel": "t"}, **app)},
                    "tunnels": {"t": "t"}}
        return console_packages.check_manifest(manifest, members, set())

    def test_an_app_declares_its_kinds_and_where_the_console_polls(self):
        out = self.check(notifications=KINDS, notifications_path="/api/notifications", notifications_header="X-Demo-Token")
        app = out["apps"]["b"]
        self.assertEqual([k["id"] for k in app["notifications"]], ["done", "slow"])
        self.assertEqual((app["notifications_path"], app["notifications_header"]), ("/api/notifications", "X-Demo-Token"))
        plain = self.check(notifications=KINDS)["apps"]["b"]
        self.assertEqual((plain["notifications_path"], plain["notifications_header"]), ("/notifications", "Authorization"))
        self.assertEqual(self.check()["apps"]["b"]["notifications"], [])
        for bad in ({"notifications": [{"id": "x", "label": "y", "on_state": "sleeping"}]},
                    {"notifications_path": "notifications"}, {"notifications_path": "/a/../b"},
                    {"notifications_header": "Bad Header"}, {"notifications": [{"id": "x"}]}):
            with self.subTest(bad=bad), self.assertRaises(console_packages.PackageError):
                self.check(**bad)

    def test_a_widget_declares_kinds(self):
        members = {"w.html": b"<p>hi</p>"}
        out = console_packages._check_widgets([{"id": "clock", "title": "Clock", "slot": "home", "entry": "w.html",
                                                "notifications": KINDS}], members)
        self.assertEqual(out[0]["notifications"][0]["id"], "done")
        with self.assertRaises(console_packages.PackageError):
            console_packages._check_widgets([{"id": "clock", "title": "Clock", "slot": "home", "entry": "w.html",
                                              "notifications": [{"id": "BAD", "label": "x"}]}], members)


class TestConsole(ConsoleTestCase):
    def serve_app(self):
        """An app's server: GET /notifications, the token in X-Demo-Token."""
        outer = self
        outer.queue, outer.asked = [], []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.asked.append(self.path)
                if self.headers.get("X-Demo-Token") != "tok":
                    self.send_response(401)
                    self.end_headers()
                    return
                after = self.path.partition("after=")[2]
                items = [i for i in outer.queue if after != "" and i["id"] > int(after)]
                body = json.dumps({"latest": len(outer.queue), "notifications": items}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def add(self, kind, title, **extra):
        self.queue.append(dict({"id": len(self.queue) + 1, "kind": kind, "title": title, "body": "b"}, **extra))

    def test_sources_switches_and_polling(self):
        port = self.serve_app()
        spec = {"title": "Demo", "tunnel": "demo", "open_path": "/", "health_path": "/", "notifications": KINDS,
                "notifications_path": "/notifications", "notifications_header": "X-Demo-Token"}
        patched = mock.patch.dict(agent_console.APPS, {"demo": spec})
        patched.start()
        self.addCleanup(patched.stop)
        profile = agent_profiles.load(self.dead)
        profile["apps"] = dict(profile.get("apps") or {}, demo={"port": port})
        agent_profiles.save(profile)
        with mock.patch.object(self.clusters, "_app_token", return_value="tok"):
            sources = self.call("GET", "/api/notification-sources")[1]["sources"]
            self.assertIn({"key": "app:demo", "title": "Demo", "kind": "app", "kinds": KINDS}, sources)
            # off: nothing is asked of the app
            self.assertEqual(self.call("GET", "/api/notifications/apps")[1]["notifications"], [])
            self.assertEqual(self.asked, [])
            self.add("done", "before")                                          # from before it was looked at
            status, prefs = self.call("PUT", "/api/preferences", {"app_notifications": {
                "app:demo": {"enabled": True, "kinds": {"done": True, "slow": False}}}})
            self.assertEqual(status, 200, prefs)
            self.assertEqual(self.call("GET", "/api/notifications/apps")[1]["notifications"], [])    # the first look
            self.assertEqual(self.asked[-1], "/notifications")
            self.add("done", "Run ended", hash="#/c/x/apps/demo")
            self.add("slow", "Slow", body="switched off kind")
            self.add("evil", "Not declared")
            self.add("done", "Bad link", hash="javascript:alert(1)")
            got = self.call("GET", "/api/notifications/apps")[1]["notifications"]
            self.assertEqual([n["title"] for n in got], ["Run ended", "Bad link"])
            self.assertEqual(got[0]["hash"], "#/c/x/apps/demo")
            self.assertTrue(got[1]["hash"].startswith("#/c/") and "javascript" not in got[1]["hash"], got[1])
            self.assertEqual((got[0]["source"], got[0]["app"], got[0]["kind"]), ("app:demo", "demo", "done"))
            self.assertEqual(self.call("GET", "/api/notifications/apps")[1]["notifications"], [])    # taken once
            self.assertTrue(self.asked[-1].endswith("after=5"), self.asked[-1])
            # switched off, then on again: what happened meanwhile is not replayed
            self.call("PUT", "/api/preferences", {"app_notifications": {"app:demo": {"enabled": False, "kinds": {}}}})
            self.call("GET", "/api/notifications/apps")
            self.add("done", "while off")
            self.call("PUT", "/api/preferences", {"app_notifications": {"app:demo": {"enabled": True, "kinds": {"done": True}}}})
            self.assertEqual(self.call("GET", "/api/notifications/apps")[1]["notifications"], [])
            # an app that restarted numbers from 1 again
            self.queue.clear()
            self.assertEqual(self.call("GET", "/api/notifications/apps")[1]["notifications"], [])
            self.add("done", "after restart")
            self.assertEqual([n["title"] for n in self.call("GET", "/api/notifications/apps")[1]["notifications"]], ["after restart"])

    def test_a_state_kind_fires_when_the_app_enters_the_state(self):
        kinds = [{"id": "started", "label": "Its job started", "hint": "Open is ready", "on_state": "up"}]
        spec = {"title": "Demo", "tunnel": "demo", "open_path": "/", "health_path": "/", "notifications": kinds}
        patched = mock.patch.dict(agent_console.APPS, {"demo": spec})
        patched.start()
        self.addCleanup(patched.stop)
        profile = agent_profiles.load(self.dead)
        profile["apps"] = dict(profile.get("apps") or {}, demo={"port": 1})
        agent_profiles.save(profile)
        states = iter(["up", "queued", "queued", "up", "up", "error", "up"])
        self.call("PUT", "/api/preferences", {"app_notifications": {"app:demo": {"enabled": True, "kinds": {"started": True}}}})
        with mock.patch.object(self.clusters, "_app_state", side_effect=lambda p, a: {"state": next(states)}):
            got = [self.call("GET", "/api/notifications/apps")[1]["notifications"] for _ in range(7)]
        self.assertEqual([len(n) for n in got], [0, 0, 0, 1, 0, 0, 1])     # not for the first look; once per entry into "up"
        self.assertEqual((got[3][0]["title"], got[3][0]["body"], got[3][0]["kind"]), ("Its job started", "Open is ready", "started"))

    def test_a_source_left_out_is_switched_off(self):
        self.call("PUT", "/api/preferences", {"app_notifications": {"app:a": {"enabled": True, "kinds": {"x": True}}}})
        out = self.call("PUT", "/api/preferences", {"app_notifications": {}})[1]
        self.assertEqual(out["app_notifications"], {})
        self.assertEqual(self.call("PUT", "/api/preferences", {"app_notifications": {"app:A": {}}})[0], 422)


if __name__ == "__main__":
    unittest.main()
