#!/usr/bin/env python3
"""
Filesystem diagnostics: why are my tunnels slow? It runs on the login node as you (the fsdiag tunnel's sbatch_script.sh
starts it), listens on 127.0.0.1 only, and answers only with the token it prints at start (the console's Open adds it).

It measures file system calls where you ask (the login node, a SLURM job on any compute node, a job on the nodes you
name) with fsprobe.py, surveys where your files are, reads the REST server's audit log and stack dumps to say what
slow requests were waiting for, and turns it into findings. The token travels only in the X-Fsdiag-Token header,
requests that change something must be JSON, and a request naming another Origin is refused.

  fsdiag_server.py --port PORT

  GET  /health                     {"ok", "version"}
  GET  /api/info                   user, host, the default places, the control, the settings
  GET  /api/nodes[?refresh=1]      {slurm, partitions, nodes, mine}: what can be targeted; mine: where your running jobs are
  GET  /api/runs                   the runs, newest first, with each target's state
  POST /api/runs {spec}            start one: {where: login|compute|both, nodes, partition, account, paths, repeats, threads,
                                   survey_roots, survey_seconds, quota, max_seconds, time_limit, mem} -> the run
  GET  /api/run?id=[&requests=1]   the run, its results and the findings (with the request analysis when requests=1)
  POST /api/run/cancel {id}        POST /api/run/delete {id}
  GET  /api/requests[?hours=&slow_ms=]   the REST requests' analysis (routes, waiting, stacks, timeline) and its findings
  GET  /api/report?id=[&requests=1]      the run as a text report to attach to a message

Python 3.6+, standard library only.
"""
import argparse
import http.server
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

from token_auth import ServiceToken

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fsfindings  # noqa: E402
import fsprobe  # noqa: E402
import fsrequests  # noqa: E402
from fsruns import Refused, Runs, Shell, Slurm, data_dir  # noqa: E402

VERSION = "1.0.0"
UI_DIR = os.path.join(HERE, "ui")
MAX_JSON = 1 << 20
AUDIENCE = "hpclib:fsdiag"


class Diagnostics:
    """What the server serves: the runs, the node list, the request analysis."""

    def __init__(self):
        home = os.path.expanduser("~")
        self.user = os.environ.get("USER") or os.path.basename(home)
        self.shell = Shell()
        self.slurm = Slurm(self.shell, self.user)
        self.state_dir = os.path.join(data_dir(), "fsdiag")
        self.runs = Runs(self.state_dir, os.path.join(HERE, "fsprobe.py"), self.shell, self.slurm)
        self.runs.compute_python = os.environ.get("FSDIAG_COMPUTE_PYTHON") or "python3"
        self._requests, self._lock = {}, threading.Lock()

    def settings(self):
        env = os.environ.get
        return {"paths": [p for p in (env("FSDIAG_PATHS") or "").split(":") if p],
                "survey_roots": [p for p in (env("FSDIAG_SURVEY_ROOTS") or "").split(":") if p],
                "partition": env("FSDIAG_PARTITION") or "", "account": env("FSDIAG_ACCOUNT") or "",
                "time_limit": env("FSDIAG_TIME_LIMIT") or "00:20:00", "mem": env("FSDIAG_MEM") or "1G"}

    def info(self):
        home = os.path.expanduser("~")
        places = [{"label": label, "path": "~" if path == home else path} for label, path in fsprobe.default_places()]
        s = self.settings()
        for p in s["paths"]:
            label, eq, rest = p.partition("=")
            places.append({"label": label, "path": rest} if eq and "/" not in label else {"label": os.path.basename(p.rstrip("/")) or p, "path": p})
        return {"version": VERSION, "fsprobe": fsprobe.VERSION, "user": self.user, "host": socket.gethostname(), "home": home,
                "data_dir": data_dir(), "places": places, "control": fsprobe.control_path(), "settings": s,
                "ops": [{"name": n, "info": fsprobe.OP_INFO.get(n, "")} for n in fsprobe.ORDER + ["sqlite_commit"]]}

    def nodes(self, refresh=False):
        out = dict(self.slurm.nodes(refresh))
        out["mine"] = self.slurm.mine() if out.get("slurm") else []
        return out

    def requests(self, hours, slow_ms):
        key = (hours, slow_ms)
        with self._lock:
            hit = self._requests.get(key)
            if hit and time.time() - hit[0] < 20:
                return hit[1]
        analysis = fsrequests.load(data_dir(), hours, slow_ms)
        with self._lock:
            self._requests[key] = (time.time(), analysis)
        return analysis

    def run_detail(self, run_id, with_requests=False, hours=24.0):
        run = self.runs.get(run_id)
        results = self.runs.results(run)
        analysis = self.requests(hours, fsrequests.SLOW_MS) if with_requests else None
        return run, results, analysis


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, token, diagnostics):
        self.auth = ServiceToken(token, AUDIENCE)
        self.diagnostics = diagnostics
        http.server.HTTPServer.__init__(self, address, Handler)

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            http.server.HTTPServer.handle_error(self, request, client_address)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "hpclib-fsdiag/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("FSDIAG_LOG_REQUESTS"):
            sys.stderr.write("[fsdiag] " + (fmt % args) + "\n")

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_POST(self):
        self.dispatch("POST")

    def send_bytes(self, status, body, ctype, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, payload):
        self.send_bytes(status, (json.dumps(payload) + "\n").encode(), "application/json")

    def host_ok(self):
        return re.fullmatch(r"(127\.0\.0\.1|localhost|\[::1\])(:\d+)?", (self.headers.get("Host") or "").lower()) is not None

    def authorized(self):
        return self.server.auth.check(self.headers.get("X-Fsdiag-Token") or "", AUDIENCE)

    def same_origin(self):
        host = (self.headers.get("Host") or "").lower()
        origin = self.headers.get("Origin")
        if origin is not None and urllib.parse.urlsplit(origin.lower()).netloc != host:
            return False
        return (self.headers.get("Sec-Fetch-Site") or "same-origin") in ("same-origin", "none")

    def json_body(self):
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise Refused(415, "send JSON (Content-Type: application/json)")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_JSON:
            raise Refused(413, "the request is too large")
        try:
            out = json.loads((self.rfile.read(length) if length else b"").decode() or "{}")
        except ValueError:
            raise Refused(400, "not JSON")
        if not isinstance(out, dict):
            raise Refused(400, "a JSON object")
        return out

    def dispatch(self, verb):
        url = urllib.parse.urlsplit(self.path)
        self.query = urllib.parse.parse_qs(url.query)
        path = url.path
        try:
            if not self.host_ok():
                raise Refused(421, "unexpected Host header: open fsdiag through its tunnel")
            if path == "/health":
                return self.send_json(200, {"ok": True, "version": VERSION, "service": "hpclib-fsdiag"})
            if verb in ("GET", "HEAD") and (path == "/" or path.startswith("/ui/")):
                return self.static(path)
            if not self.same_origin():
                raise Refused(403, "fsdiag answers only its own page")
            if not self.authorized():
                raise Refused(401, "fsdiag's token is missing or wrong (Open it from the console again)")
            if verb == "POST" and not path.startswith("/api/"):
                raise Refused(404, "not found")
            status, payload = self.route(verb, path)
            if isinstance(payload, tuple):         # (text, filename): a download
                text, name = payload
                return self.send_bytes(status, text.encode(), "text/plain; charset=utf-8",
                                       [("Content-Disposition", 'attachment; filename="{}"'.format(name))])
            self.send_json(status, payload)
        except Refused as e:
            self.close_connection = True
            self.send_json(e.status, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self.close_connection = True
            self.send_json(500, {"error": "{}: {}".format(type(e).__name__, e)})

    def q(self, name, default=None):
        return self.query.get(name, [default])[-1]

    def number(self, name, default, low, high):
        value = self.q(name)
        if value in (None, ""):
            return default
        try:
            value = float(value)
        except ValueError:
            raise Refused(400, "{}: a number".format(name))
        return min(high, max(low, value))

    def route(self, verb, path):
        d = self.server.diagnostics
        if verb == "GET" and path == "/api/info":
            return 200, d.info()
        if verb == "GET" and path == "/api/nodes":
            return 200, d.nodes(bool(self.q("refresh")))
        if verb == "GET" and path == "/api/runs":
            return 200, {"runs": d.runs.list()}
        if verb == "POST" and path == "/api/runs":
            spec = self.json_body()
            s = d.settings()
            for key in ("partition", "account", "time_limit", "mem"):
                spec.setdefault(key, s[key])
            return 201, d.runs.start(spec)
        if verb == "GET" and path in ("/api/run", "/api/report"):
            want = bool(self.q("requests"))
            hours = self.number("hours", 24.0, 0.1, 24 * 14)
            run, results, analysis = d.run_detail(self.q("id", ""), want, hours)
            found = fsfindings.findings(results, analysis)
            if path == "/api/report":
                title = "File system diagnostics for {}@{} (run {})".format(d.user, socket.gethostname(), run["id"])
                return 200, (fsfindings.report_text(title, results, found, analysis), "fsdiag-{}.txt".format(run["id"]))
            return 200, {"run": run, "results": results, "findings": found, "requests": analysis}
        if verb == "POST" and path == "/api/run/cancel":
            return 200, d.runs.cancel(self.json_body().get("id", ""))
        if verb == "POST" and path == "/api/run/delete":
            d.runs.forget(self.json_body().get("id", ""))
            return 200, {"ok": True}
        if verb == "GET" and path == "/api/requests":
            analysis = d.requests(self.number("hours", 24.0, 0.1, 24 * 14), self.number("slow_ms", fsrequests.SLOW_MS, 10, 600000))
            return 200, {"analysis": analysis, "findings": fsfindings.request_findings(analysis)}
        raise Refused(404, "not found")

    def static(self, path):
        name = "index.html" if path == "/" else path[len("/ui/"):]
        full = os.path.normpath(os.path.join(UI_DIR, name))
        if not full.startswith(UI_DIR + os.sep) or not os.path.isfile(full):
            raise Refused(404, "not found")
        with open(full, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self.send_bytes(200, body, ctype)


def main(argv=None):
    p = argparse.ArgumentParser(description="The hpclib filesystem diagnostics server (run by the fsdiag tunnel).")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--token", default=os.environ.get("FSDIAG_TOKEN") or None, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if sys.version_info < (3, 6):
        print("fsdiag: needs Python 3.6 or newer", file=sys.stderr)
        return 2
    token = args.token or secrets.token_urlsafe(24)
    server = Server(("127.0.0.1", args.port), token, Diagnostics())

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    # the console's Open finds the token here (the app's token_regex)
    print("hpclib fsdiag {} on {}: http://127.0.0.1:{}/?token={}".format(VERSION, socket.gethostname(), args.port, token), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
