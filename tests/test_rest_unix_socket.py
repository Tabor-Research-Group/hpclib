"""The REST server and waiting_shim on a Unix socket file (--unix-socket / unix:PATH)."""
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hpclib" / "servers"))

import rest_server  # noqa: E402


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(self.path)


def get(path, url):
    conn = UnixConnection(path)
    try:
        conn.request("GET", url)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


class TestUnixSocket(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hpcus")
        self.sock = os.path.join(self.dir, "rest.sock")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.dir])

    def make_server(self):
        return rest_server.UnixRESTServer(self.sock, rest_server.RESTHandler,
                                          auth=rest_server.TokenAuth("t" * 32),
                                          whitelist=rest_server.PathWhitelist([self.dir]))

    def wait_for(self, path):
        for _ in range(100):
            if os.path.exists(path):
                return
            time.sleep(0.1)
        self.fail("socket never appeared")

    def test_server_class_binds_private_socket_and_replaces_stale_one(self):
        self.assertEqual(rest_server.UnixRESTServer.address_family, socket.AF_UNIX)
        stale = socket.socket(socket.AF_UNIX)                                      # left behind by a dead server
        stale.bind(self.sock)
        stale.close()
        self.assertTrue(os.path.exists(self.sock))
        server = self.make_server()
        try:
            self.assertEqual(os.stat(self.sock).st_mode & 0o777, 0o600)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            status, _ = get(self.sock, "/nonexistent")
            self.assertIn(status, (400, 401, 403, 404))
            with self.assertRaises(OSError):                                       # something answers: refuse
                self.make_server()
            plain = os.path.join(self.dir, "plain")
            open(plain, "w").close()
            with self.assertRaises(OSError):                                       # never delete a regular file
                rest_server.UnixRESTServer(plain, rest_server.RESTHandler, auth=rest_server.TokenAuth("t" * 32),
                                           whitelist=rest_server.PathWhitelist([self.dir]))
            self.assertTrue(os.path.exists(plain))
        finally:
            server.shutdown()
            server.server_close()

    def test_waiting_shim_on_a_socket(self):
        proc = subprocess.Popen([sys.executable, str(REPO / "hpclib" / "servers" / "waiting_shim.py"),
                                 f"unix:{self.sock}",
                                 os.path.join(self.dir, "status")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.wait_for(self.sock)
            status, _ = get(self.sock, "/")
            self.assertTrue(100 <= status < 600)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
