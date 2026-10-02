"""Tests for hpclib/examples/orca_scan: run_scan.py against the fake
cluster used by the REST tests, and setup_cluster.sh against a fake ssh
that runs the "remote" side in a temporary home directory.

Run with:  python -m unittest tests.test_examples -v
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_rest_jobs import fake_job, set_task_state  # noqa: E402
from test_rest_workflows import WorkflowTestCase, make_scan  # noqa: E402
from rest_server import TokenAuth  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "hpclib" / "examples" / "orca_scan"


def load_run_scan():
    spec = importlib.util.spec_from_file_location("run_scan", DEMO / "run_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestRunScan(WorkflowTestCase):

    def setUp(self):
        super().setUp()
        self.run_scan = load_run_scan()
        self.scan = self.tmp / "local" / "sample_scan"
        make_scan(self.scan, shape=(2, 2))
        token_file = self.tmp / "token"
        token_file.write_text(self.builder_token)
        token_file.chmod(0o600)
        env = mock.patch.dict(os.environ, {"HPC_REST_URL": self.url, "HPC_REST_TOKEN_FILE": str(token_file)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HPC_REST_TOKEN", None)
        self.remote = self.llm_root / "scans" / "sample_scan"

    def run_demo(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_scan.main([str(self.scan), "--remote-dir", "scans/sample_scan", "--poll", "1", *args])
        return out.getvalue()

    def finish(self, job_id, failed=()):
        for t in range(len(fake_job(self.slurm, job_id)["tasks"])):
            set_task_state(self.slurm, job_id, t, "FAILED" if t in failed else "COMPLETED")
        for inp in self.remote.glob("*.inp"):
            inp.with_suffix(".out").write_text("****ORCA TERMINATED NORMALLY****")
            inp.with_suffix(".xyz").write_text("xyz")

    def test_submit_then_collect(self):
        out = self.run_demo("--no-wait")
        self.assertIn("uploaded 5 files", out)
        self.assertIn("dry run: 4 tasks", out)
        job_id = out.split("submitted job ")[1].split()[0]
        job = fake_job(self.slurm, job_id)
        self.assertIn("--array=0-3%4", job["args"])
        self.assertIn("--ntasks=4", job["args"])
        self.assertIn("--mem=16G", job["args"])

        self.finish(job_id, failed=(2,))
        out = self.run_demo()
        self.assertIn(f"already submitted as job {job_id}", out)   # idempotent rerun
        self.assertIn("uploaded 0 files (5 already there)", out)
        self.assertIn("point 2 (scan_001_000.inp): FAILED", out)
        self.assertIn("downloaded 8 files", out)
        self.assertIn(f"--retry-failed {job_id}", out)
        self.assertTrue((self.scan / "scan_001_001.out").exists())

        out = self.run_demo("--retry-failed", job_id, "--no-wait")
        self.assertIn("resubmitting scan points [2]", out)
        retry_id = out.split("submitted job ")[1].split()[0]
        self.assertIn("--array=0-0%1", fake_job(self.slurm, retry_id)["args"])
        self.assertIn(str(self.remote / "scan_001_000.inp"), fake_job(self.slurm, retry_id)["script"])

    def test_wrong_scope_is_reported(self):
        token_file = self.tmp / "reader"
        token_file.write_text(self.reader_token)
        token_file.chmod(0o600)
        with mock.patch.dict(os.environ, {"HPC_REST_TOKEN_FILE": str(token_file)}):
            with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(io.StringIO()):
                self.run_scan.main([str(self.scan), "--remote-dir", "scans/x"])
        self.assertIn("files:write", str(ctx.exception))


FAKE_SSH = textwrap.dedent("""\
    #!/usr/bin/env bash
    after_host=false
    remote=''
    for ssh_arg in "$@"; do
      if [ "$after_host" = true ]; then
        remote="$remote${remote:+ }$ssh_arg"
      elif [ "$ssh_arg" = me@login.example ]; then
        after_host=true
      fi
    done
    cd "$TEST_REMOTE_HOME"
    exec env -u HPCTUNNELS_DATA_DIR HOME="$TEST_REMOTE_HOME" PATH="$TEST_REMOTE_PATH" bash -c "$remote"
""")


class TestSetupCluster(unittest.TestCase):

    def test_setup_and_rerun(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        remote_home, local_home, bin_dir = tmp / "remote home", tmp / "local", tmp / "bin"
        for d in (remote_home, local_home, bin_dir):
            d.mkdir()
        (bin_dir / "ssh").write_text(FAKE_SSH)
        (bin_dir / "ssh").chmod(0o755)
        work = tmp / "scratch" / "llm"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HPC", "HPCLIB"))}
        env.update(HOME=str(local_home), PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                   TEST_REMOTE_HOME=str(remote_home), TEST_REMOTE_PATH=os.environ["PATH"])
        script = DEMO / "setup_cluster.sh"

        res = subprocess.run(["bash", str(script), "me@login.example", str(work)], env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        rest = remote_home / ".local" / "tunnels" / "rest"
        self.assertTrue((remote_home / "hpclib" / "servers" / "rest_server.py").exists())
        self.assertTrue((rest / "templates" / "orca" / "template.json").exists())
        self.assertEqual(json.loads((rest / "config.json").read_text()),
                         json.loads((DEMO / "cluster_config.json").read_text()))
        self.assertTrue(work.is_dir())
        token_file = local_home / ".config" / "hpclib" / "llm_token"
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
        token = token_file.read_text().strip()
        auth = TokenAuth("unused-owner", tokens_file=str(rest / "tokens.json"))
        identity = auth.identify(f"Bearer {token}")
        self.assertEqual((identity.name, sorted(identity.scopes), identity.allow),
                         ("llm-scan", ["files:write", "propose", "read", "submit"], [str(work)]))
        self.assertTrue((rest / "templates" / "writing_templates" / "guide.md").exists())

        # the owner token lives on this machine; the cluster gets only its hash, which the server accepts
        owner_file = local_home / ".config" / "hpclib" / "rest_token"
        self.assertEqual(stat.S_IMODE(owner_file.stat().st_mode), 0o600)
        owner = owner_file.read_text().strip()
        remote_owner = remote_home / ".local" / "tunnels" / "rest_token"
        self.assertEqual(stat.S_IMODE(remote_owner.stat().st_mode), 0o600)
        self.assertTrue(remote_owner.read_text().startswith("sha256:"))
        self.assertNotIn(owner, remote_owner.read_text())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(TokenAuth.TOKEN_ENV_VAR, None)
            server_auth = TokenAuth.load(str(remote_owner))
        self.assertEqual(server_auth.identify(f"Bearer {owner}").name, "owner")
        self.assertIn("the cluster has the hash of", res.stdout)

        # a rerun keeps the template you edited, the config, and the token
        (rest / "templates" / "orca" / "template.json").write_text("{\"edited\": true}")
        res = subprocess.run(["bash", str(script), "me@login.example", str(work)], env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("kept the existing orca", res.stdout)
        self.assertIn("--revoke-token llm-scan", res.stdout)
        self.assertIn("kept the existing config.json", res.stdout)
        self.assertIn("already exists", res.stdout)
        self.assertIn("already has a hashed owner token", res.stdout)
        self.assertEqual((rest / "templates" / "orca" / "template.json").read_text(), "{\"edited\": true}")
        self.assertEqual(token_file.read_text().strip(), token)
        self.assertEqual(owner_file.read_text().strip(), owner)

    def test_plaintext_owner_token_is_reported(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        remote_home, local_home, bin_dir = tmp / "remote home", tmp / "local", tmp / "bin"
        for d in (remote_home, local_home, bin_dir):
            d.mkdir()
        (bin_dir / "ssh").write_text(FAKE_SSH)
        (bin_dir / "ssh").chmod(0o755)
        plaintext = remote_home / ".local" / "tunnels" / "rest_token"
        plaintext.parent.mkdir(parents=True)
        plaintext.write_text("server-made-token\n")
        plaintext.chmod(0o600)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HPC", "HPCLIB"))}
        env.update(HOME=str(local_home), PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                   TEST_REMOTE_HOME=str(remote_home), TEST_REMOTE_PATH=os.environ["PATH"])
        res = subprocess.run(["bash", str(DEMO / "setup_cluster.sh"), "me@login.example", str(tmp / "work")],
                             env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("plaintext owner token", res.stdout)
        self.assertIn("--hash-token-file", res.stdout)
        self.assertEqual(plaintext.read_text(), "server-made-token\n")      # left for you to copy first
        self.assertFalse((local_home / ".config" / "hpclib" / "rest_token").exists())

    def test_usage(self):
        res = subprocess.run(["bash", str(DEMO / "setup_cluster.sh"), "only-one-arg"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)


if __name__ == "__main__":
    unittest.main()
