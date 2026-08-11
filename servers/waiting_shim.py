#!/usr/bin/env python3
"""
Tiny stand-in HTTP server that occupies a tunnel's forwarded port while
its SLURM job is still queued/starting, so the browser at the far end
of the SSH -L forward gets an immediate, friendly "waiting" page
instead of connection-refused. start_tunnel.sh kills this process the
moment the real job is reachable, freeing the port for the real
second-hop forward to bind to.

Usage: waiting_shim.py PORT STATUS_FILE [TUNNEL_NAME]
  - Binds 127.0.0.1:PORT immediately.
  - Every request re-reads STATUS_FILE (start_tunnel.sh keeps this
    updated with whatever squeue reason is current) and serves a page
    that auto-refreshes every 3s.
  - Exits (freeing the port) on SIGTERM.
"""
import html
import http.server
import signal
import sys

PORT = int(sys.argv[1])
STATUS_FILE = sys.argv[2]
TUNNEL_NAME = sys.argv[3] if len(sys.argv) > 3 else "tunnel"

PAGE = """<!doctype html>
<html><head>
<meta http-equiv="refresh" content="3">
<title>Waiting for {tunnel}</title>
<style>
body {{ font-family: system-ui, sans-serif; background:#111; color:#eee;
        display:flex; height:100vh; align-items:center; justify-content:center; }}
.box {{ text-align:center; }}
.spinner {{ width:32px; height:32px; margin:0 auto 16px; border-radius:50%;
            border:4px solid #444; border-top-color:#6cf;
            animation:spin 1s linear infinite; }}
@keyframes spin {{ to {{ transform:rotate(360deg); }} }}
</style>
</head><body><div class="box">
<div class="spinner"></div>
<h2>Waiting for {tunnel}&hellip;</h2>
<p>{status}</p>
<p style="color:#888">This page refreshes automatically.</p>
</div></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # keep session logs clean

    def do_GET(self):
        try:
            with open(STATUS_FILE) as f:
                status = f.read().strip() or "queued"
        except OSError:
            status = "queued"
        body = PAGE.format(
            tunnel=html.escape(TUNNEL_NAME), status=html.escape(status)
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _stop(*_):
    sys.exit(0)


signal.signal(signal.SIGTERM, _stop)

# HTTPServer sets allow_reuse_address=True, so once this process exits
# and frees the port, the real forward can bind the exact same
# 127.0.0.1:PORT right after without hitting a TIME_WAIT EADDRINUSE.
server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
server.serve_forever()