"""
Notifications an app's server can send to the console (shown as browser notifications), in the console's terms.

An app declares the kinds it may send in its package manifest (`notifications`: [{id, label, hint}]); the user
switches the app, then each kind, on in Console Settings -> Notifications (everything is off until then, and a kind
the manifest doesn't declare is dropped). The server queues them here; the console polls the app over its tunnel
(the manifest's `notifications_path`, default /notifications, with the app's token in `notifications_header`):

    GET /notifications              -> {"latest": N, "notifications": []}      where the queue stands now
    GET /notifications?after=N      -> {"latest": M, "notifications": [{"id", "kind", "title", "body", "hash"}]}

The console starts from "latest", so nothing from before it first looked is shown.

    from app_notifications import Notifier
    notes = Notifier()
    notes.notify("run_finished", "Run 20261105-101500 finished", "3 targets done", hash="#/c/grace/apps/fsdiag")
    ...
    if path == "/notifications": send_json(200, notes.poll(query.get("after", [None])[-1]))

Standard library only; Python 3.6 or newer (embedded in packages with core_helpers: {"DEST": "app_notifications"}).
"""
import threading
import time

MAX_TITLE = 120
MAX_BODY = 300


class Notifier:
    """A short queue of notifications, numbered, for the console to collect."""

    def __init__(self, keep=100):
        self.keep = keep
        self._lock = threading.Lock()
        self._last = 0
        self._items = []

    def notify(self, kind, title, body="", hash=None):
        """Queue one; returns its number. hash is a console address to open when it is clicked (#/c/CLUSTER/...)."""
        with self._lock:
            self._last += 1
            item = {"id": self._last, "kind": str(kind), "title": str(title)[:MAX_TITLE],
                    "body": str(body or "")[:MAX_BODY], "hash": str(hash) if hash else None, "at": time.time()}
            self._items.append(item)
            del self._items[:-self.keep]
            return self._last

    def poll(self, after=None):
        """What the poll endpoint answers: everything after number `after` (none given: just where the queue stands)."""
        with self._lock:
            try:
                after = int(after)
            except (TypeError, ValueError):
                return {"latest": self._last, "notifications": []}
            return {"latest": self._last, "notifications": [dict(i) for i in self._items if i["id"] > after]}
