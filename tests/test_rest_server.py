"""Tests for hpclib/servers/rest_server.py.

Run with:  python -m unittest tests.test_rest_server -v
(from the repo root; uses fake sbatch/squeue/... scripts, so no SLURM needed)
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hpclib" / "servers"))
import rest_server  # noqa: E402
from rest_server import HPCRESTHandler, PathWhitelist, RESTServer, TokenAuth  # noqa: E402

TOKEN = "test-token"

FAKE_SLURM = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys, time
    name = os.path.basename(sys.argv[0])
    if "--sleep" in sys.argv:
        time.sleep(5)
    stdin = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    print(json.dumps({"cmd": name, "args": sys.argv[1:], "cwd": os.getcwd(), "stdin": stdin}))
    if "--fail" in sys.argv:
        print("boom", file=sys.stderr)
        sys.exit(3)
""")


class RESTServerTestCase(unittest.TestCase):

    allow_dirs = True
    disable_file_changes = False

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.root = self.tmp / "allowed"
        self.outside = self.tmp / "outside"
        self.root.mkdir()
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("nope")

        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for cmd in HPCRESTHandler.SLURM_COMMANDS:
            script = bin_dir / cmd
            script.write_text(FAKE_SLURM)
            script.chmod(0o755)
        path_patch = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
        path_patch.start()
        self.addCleanup(path_patch.stop)

        whitelist = PathWhitelist([str(self.root)] if self.allow_dirs else None, base_dir=self.root)
        self.server = RESTServer(("127.0.0.1", 0), HPCRESTHandler, auth=TokenAuth(TOKEN),
                                 whitelist=whitelist, command_timeout=2, max_upload=1024,
                                 disable_file_changes=self.disable_file_changes)
        self.server.RequestHandlerClass.log_message = lambda *a: None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp)

    def request(self, verb, route, query=None, json_body=None, data=None, token=TOKEN, raw=False):
        url = self.base_url + route
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=verb, headers=headers)
        try:
            with urllib.request.urlopen(req) as res:
                status, body = res.status, res.read()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read()
        return status, (body if raw else json.loads(body))


class TestAuth(RESTServerTestCase):

    def test_missing_token(self):
        status, body = self.request("GET", "/health", token=None)
        self.assertEqual(status, 401)

    def test_wrong_token(self):
        status, _ = self.request("GET", "/health", token="wrong")
        self.assertEqual(status, 401)

    def test_unknown_route_hidden_without_token(self):
        status, _ = self.request("GET", "/nope", token=None)
        self.assertEqual(status, 401)

    def test_health(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["allowed_dirs"], [str(self.root)])

    def test_unknown_route_and_method(self):
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        status, body = self.request("GET", "/slurm/sbatch")
        self.assertEqual(status, 405)
        self.assertEqual(body["allowed"], ["POST"])


class TestSLURM(RESTServerTestCase):

    def test_squeue_get(self):
        status, body = self.request("GET", "/slurm/squeue", query={"arg": ["--me", "-h"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["returncode"], 0)
        out = json.loads(body["stdout"])
        self.assertEqual(out["args"], ["--me", "-h"])
        self.assertEqual(out["cwd"], str(self.root))

    def test_sbatch_with_script_input(self):
        status, body = self.request("POST", "/slurm/sbatch",
                                    json_body={"args": ["--parsable"], "input": "#!/bin/bash\necho hi\n"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body["stdout"])["stdin"], "#!/bin/bash\necho hi\n")

    def test_all_commands_routed(self):
        for cmd in ("sbatch", "squeue", "sacct", "scontrol", "scancel"):
            status, body = self.request("POST", f"/slurm/{cmd}", json_body={"args": ["x"]})
            self.assertEqual(status, 200, cmd)
            self.assertEqual(json.loads(body["stdout"])["cmd"], cmd)

    def test_failure_reported_in_body(self):
        status, body = self.request("POST", "/slurm/scontrol", json_body={"args": ["--fail"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["returncode"], 3)
        self.assertIn("boom", body["stderr"])

    def test_bad_args(self):
        status, _ = self.request("POST", "/slurm/squeue", json_body={"args": "--me"})
        self.assertEqual(status, 400)

    def test_cwd_whitelisted(self):
        (self.root / "sub").mkdir()
        status, body = self.request("POST", "/slurm/sbatch", json_body={"cwd": "sub"})
        self.assertEqual(json.loads(body["stdout"])["cwd"], str(self.root / "sub"))
        status, _ = self.request("POST", "/slurm/sbatch", json_body={"cwd": str(self.outside)})
        self.assertEqual(status, 403)

    def test_timeout(self):
        status, _ = self.request("POST", "/slurm/squeue", json_body={"args": ["--sleep"]})
        self.assertEqual(status, 504)

    def test_shell_metacharacters_not_interpreted(self):
        status, body = self.request("POST", "/slurm/squeue", json_body={"args": ["; touch pwned"]})
        self.assertEqual(json.loads(body["stdout"])["args"], ["; touch pwned"])
        self.assertFalse((self.root / "pwned").exists())


class TestFiles(RESTServerTestCase):

    def test_upload_download_roundtrip(self):
        status, body = self.request("PUT", "/files/content", query={"path": "in/data.bin", "parents": 1},
                                    data=b"\x00\x01hello")
        self.assertEqual(status, 201)
        self.assertEqual(body["size"], 7)
        status, raw = self.request("GET", "/files/content", query={"path": "in/data.bin"}, raw=True)
        self.assertEqual((status, raw), (200, b"\x00\x01hello"))
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "in" / "data.bin").st_mode),
                         self.server.file_mode)

    def test_overwrite_requires_flag(self):
        (self.root / "a.txt").write_text("one")
        status, _ = self.request("PUT", "/files/content", query={"path": "a.txt"}, data=b"two")
        self.assertEqual(status, 409)
        status, _ = self.request("PUT", "/files/content", query={"path": "a.txt", "overwrite": 1}, data=b"two")
        self.assertEqual(status, 200)
        self.assertEqual((self.root / "a.txt").read_text(), "two")

    def test_upload_limit(self):
        status, _ = self.request("PUT", "/files/content", query={"path": "big"}, data=b"x" * 2048)
        self.assertEqual(status, 413)
        self.assertEqual(os.listdir(self.root), [])

    def test_list(self):
        (self.root / "b.txt").write_text("b")
        (self.root / "d").mkdir()
        status, body = self.request("GET", "/files", query={"path": "."})
        self.assertEqual(status, 200)
        self.assertEqual([(e["name"], e["type"]) for e in body["entries"]],
                         [("b.txt", "file"), ("d", "directory")])

    def test_outside_whitelist(self):
        for path in [str(self.outside / "secret.txt"), "../outside/secret.txt", "~"]:
            status, _ = self.request("GET", "/files/content", query={"path": path})
            self.assertEqual(status, 403, path)
        status, _ = self.request("PUT", "/files/content", query={"path": str(self.outside / "x")}, data=b"x")
        self.assertEqual(status, 403)

    def test_symlink_escape(self):
        (self.root / "link").symlink_to(self.outside)
        status, _ = self.request("GET", "/files/content", query={"path": "link/secret.txt"})
        self.assertEqual(status, 403)
        status, _ = self.request("GET", "/files", query={"path": "link"})
        self.assertEqual(status, 403)

    def test_prefix_sibling_not_allowed(self):
        sibling = self.tmp / "allowed-but-not"
        sibling.mkdir()
        status, _ = self.request("GET", "/files", query={"path": str(sibling)})
        self.assertEqual(status, 403)

    def test_mkdir_and_delete(self):
        self.assertEqual(self.request("POST", "/files/mkdir", query={"path": "x/y", "parents": 1})[0], 201)
        self.assertEqual(self.request("POST", "/files/mkdir", query={"path": "x"})[0], 409)
        self.assertEqual(self.request("DELETE", "/files", query={"path": "x"})[0], 409)  # not empty
        self.assertEqual(self.request("DELETE", "/files", query={"path": "x/y"})[0], 200)
        self.assertEqual(self.request("DELETE", "/files", query={"path": "x"})[0], 200)
        self.assertEqual(self.request("DELETE", "/files", query={"path": "x"})[0], 404)

    def test_cannot_delete_root(self):
        status, _ = self.request("DELETE", "/files", query={"path": str(self.root)})
        self.assertEqual(status, 403)
        self.assertTrue(self.root.exists())

    def test_delete_symlink_not_target(self):
        target = self.root / "target.txt"
        target.write_text("keep")
        (self.root / "ln").symlink_to(target)
        self.assertEqual(self.request("DELETE", "/files", query={"path": "ln"})[0], 200)
        self.assertTrue(target.exists())


class TestUnrestricted(RESTServerTestCase):
    allow_dirs = False

    def test_no_whitelist_allows_anything(self):
        status, raw = self.request("GET", "/files/content", query={"path": str(self.outside / "secret.txt")},
                                   raw=True)
        self.assertEqual((status, raw), (200, b"nope"))
        status, body = self.request("GET", "/health")
        self.assertIsNone(body["allowed_dirs"])


class TestFileChangesDisabled(RESTServerTestCase):
    disable_file_changes = True

    def test_mutating_routes_refused(self):
        (self.root / "keep.txt").write_text("keep")
        (self.root / "empty").mkdir()
        before = sorted(os.listdir(self.root))
        status, body = self.request("PUT", "/files/content", query={"path": "new.txt"}, data=b"x")
        self.assertEqual(status, 403)
        self.assertIn("--disable-file-changes", body["error"])
        self.assertEqual(self.request("PUT", "/files/content", query={"path": "keep.txt", "overwrite": 1},
                                      data=b"x")[0], 403)
        self.assertEqual(self.request("POST", "/files/mkdir", query={"path": "d"})[0], 403)
        self.assertEqual(self.request("DELETE", "/files", query={"path": "keep.txt"})[0], 403)
        self.assertEqual(self.request("DELETE", "/files", query={"path": "empty"})[0], 403)
        self.assertEqual(sorted(os.listdir(self.root)), before)
        self.assertEqual((self.root / "keep.txt").read_text(), "keep")

    def test_reads_and_slurm_still_work(self):
        (self.root / "keep.txt").write_text("keep")
        self.assertEqual(self.request("GET", "/files/content", query={"path": "keep.txt"}, raw=True),
                         (200, b"keep"))
        self.assertEqual(self.request("GET", "/files", query={"path": "."})[0], 200)
        self.assertEqual(self.request("GET", "/slurm/squeue")[0], 200)
        self.assertFalse(self.request("GET", "/health")[1]["file_changes"])

    def test_still_requires_token(self):
        self.assertEqual(self.request("PUT", "/files/content", query={"path": "x"}, data=b"x", token=None)[0], 401)


class TestTokenFile(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        env_patch = mock.patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop(TokenAuth.TOKEN_ENV_VAR, None)
        os.environ.pop(TokenAuth.TOKEN_FILE_ENV_VAR, None)

    def test_creates_private_token_and_reuses_it(self):
        token_file = self.tmp / "sub" / "rest_token"
        TokenAuth.load(str(token_file))
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
        token = token_file.read_text().strip()
        self.assertGreaterEqual(len(token), 32)
        self.assertTrue(TokenAuth.load(str(token_file)).check(f"Bearer {token}"))

    def test_rejects_readable_token_file(self):
        token_file = self.tmp / "rest_token"
        token_file.write_text("abc\n")
        token_file.chmod(0o644)
        with self.assertRaises(PermissionError):
            TokenAuth.load(str(token_file))

    def test_rejects_empty_token_file(self):
        token_file = self.tmp / "rest_token"
        token_file.write_text("\n")
        token_file.chmod(0o600)
        with self.assertRaises(ValueError):
            TokenAuth.load(str(token_file))

    def test_env_token_wins(self):
        os.environ[TokenAuth.TOKEN_ENV_VAR] = "from-env"
        self.assertTrue(TokenAuth.load(str(self.tmp / "unused")).check("Bearer from-env"))
        self.assertNotIn(TokenAuth.TOKEN_ENV_VAR, os.environ)  # kept out of child processes
        self.assertFalse((self.tmp / "unused").exists())

    def test_header_check(self):
        auth = TokenAuth("abc")
        self.assertTrue(auth.check("Bearer abc"))
        self.assertTrue(auth.check("bearer abc"))
        self.assertFalse(auth.check("Bearer abcd"))
        self.assertFalse(auth.check("Basic abc"))
        self.assertFalse(auth.check(None))


class TestCLI(unittest.TestCase):

    def test_allow_dirs_from_env_and_flags(self):
        with mock.patch.dict(os.environ, {"HPC_REST_ALLOWED_DIRS": "/a:/b", "PROCESS_PORT": "6123"}):
            opts = rest_server.parse_args(["--allow", "/c", "--max-upload", "10M"])
        self.assertEqual(opts.allow, ["/a", "/b", "/c"])
        self.assertEqual(opts.port, 6123)
        self.assertEqual(opts.max_upload, 10 << 20)
        self.assertFalse(opts.disable_file_changes)

    def test_disable_file_changes_flag_and_env(self):
        self.assertTrue(rest_server.parse_args(["--disable-file-changes"]).disable_file_changes)
        with mock.patch.dict(os.environ, {"HPC_REST_DISABLE_FILE_CHANGES": "1"}):
            self.assertTrue(rest_server.parse_args([]).disable_file_changes)


if __name__ == "__main__":
    unittest.main()
