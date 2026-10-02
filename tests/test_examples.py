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
        config, demo = json.loads((rest / "config.json").read_text()), json.loads((DEMO / "cluster_config.json").read_text())
        self.assertEqual((config["limits"], config["cluster_notes"]), (demo["limits"], demo["cluster_notes"]))
        self.assertEqual(config["sandbox"]["method"], "auto")     # jobs are sandboxed by default
        self.assertTrue(work.is_dir())
        profile_dir = local_home / ".config" / "hpclib" / "agents" / "me@login.example"
        token_file = profile_dir / "agent_token"
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
        token = token_file.read_text().strip()
        auth = TokenAuth("unused-owner", tokens_file=str(rest / "tokens.json"))
        identity = auth.identify(f"Bearer {token}")
        self.assertEqual((identity.name, sorted(identity.scopes), identity.allow),
                         ("llm-scan", ["files:write", "propose", "read", "submit"], [str(work)]))
        self.assertTrue((rest / "templates" / "writing_templates" / "guide.md").exists())

        # the owner token lives on this machine; the cluster gets only its hash, which the server accepts
        owner_file = profile_dir / "owner_token"
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
        self.assertIn("kept the existing", res.stdout)
        self.assertIn("config.json", res.stdout)
        self.assertIn("already exists; rerun with --rebuild", res.stdout)
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
        self.assertIn("rerun with --rebuild", res.stdout)
        self.assertEqual(plaintext.read_text(), "server-made-token\n")      # left for you to copy first
        self.assertFalse((local_home / ".config" / "hpclib" / "agents" / "me@login.example" / "owner_token").exists())

    def test_usage(self):
        res = subprocess.run(["bash", str(DEMO / "setup_cluster.sh"), "only-one-arg"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)


def machine_token_name():
    """The default agent token name, as agent_profiles.py derives it from this machine's name."""
    import re
    import socket
    return "agent-" + (re.sub(r"[^A-Za-z0-9._@-]+", "-", socket.gethostname().split(".")[0]).strip("-").lower()
                       or "machine")


class TestSetupAgents(unittest.TestCase):
    """setup_agents (lib/tunnels.sh) against a fake ssh, like TestSetupCluster."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.remote_home, self.local_home, bin_dir = self.tmp / "remote home", self.tmp / "local", self.tmp / "bin"
        for d in (self.remote_home, self.local_home, bin_dir):
            d.mkdir()
        (bin_dir / "ssh").write_text(FAKE_SSH)
        (bin_dir / "ssh").chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("HPC", "HPCLIB"))}
        self.env.update(HOME=str(self.local_home), PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                        TEST_REMOTE_HOME=str(self.remote_home), TEST_REMOTE_PATH=os.environ["PATH"])
        self.rest = self.remote_home / ".local" / "tunnels" / "rest"
        self.work = [self.tmp / "scratch" / "llm", self.tmp / "project" / "shared"]
        self.profile_dir = self.local_home / ".config" / "hpclib" / "agents" / "me@login.example"
        self.token_file = self.profile_dir / "agent_token"
        self.owner_file = self.profile_dir / "owner_token"
        self.agent_name = machine_token_name()

    def bash(self, command, *args, expect=0, env=None):
        command = f'source {REPO / "hpclib" / "hpclib.sh"} && {command}'
        res = subprocess.run(["bash", "-c", command, "hpclib-test", *args], env=env or self.env,
                             capture_output=True, text=True, timeout=180)
        self.assertEqual(res.returncode, expect, res.stdout + res.stderr)
        return res.stdout + res.stderr

    def setup_agents(self, *args, expect=0):
        return self.bash('setup_agents "$@"', *args, expect=expect)

    def identify(self, token):
        auth = TokenAuth("unused-owner", tokens_file=str(self.rest / "tokens.json"))
        return auth.identify(f"Bearer {token}")

    def config(self):
        return json.loads((self.rest / "config.json").read_text())

    def profile(self, name="me@login.example"):
        return json.loads((self.local_home / ".config" / "hpclib" / "agents" / name / "profile.json").read_text())

    def test_defaults_then_rebuild(self):
        args = ["--work-dir", str(self.work[0]), "--work-dir", str(self.work[1]), "--bind", "/sw", "me@login.example"]
        out = self.setup_agents(*args)
        for t in ("hello", "orca", "writing_templates"):
            self.assertTrue((self.rest / "templates" / t).is_dir(), t)
        self.assertNotIn("python_script", os.listdir(self.rest / "templates"))
        config = self.config()
        self.assertEqual(config["sandbox"]["method"], "auto")
        self.assertIn("/sw", config["sandbox"]["binds"])
        self.assertIn("max_concurrent_jobs", config["limits"])
        self.assertTrue(all(w.is_dir() for w in self.work))
        token = self.token_file.read_text().strip()
        identity = self.identify(token)
        self.assertEqual((identity.name, sorted(identity.allow)), (self.agent_name, sorted(map(str, self.work))))
        self.assertEqual(sorted(identity.scopes), ["files:write", "propose", "read", "submit"])
        owner = self.owner_file.read_text().strip()
        for f in (self.token_file, self.owner_file, self.profile_dir / "profile.json", self.profile_dir / "mcp.json"):
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600, f)
        self.assertEqual(stat.S_IMODE(self.profile_dir.parent.stat().st_mode), 0o700)
        profile = self.profile()
        self.assertTrue(20000 <= profile["port"] <= 32000 and 20000 <= profile["process_port"] <= 32000)
        self.assertNotEqual(profile["port"], profile["process_port"])
        self.assertEqual(profile["work_dirs"], list(map(str, self.work)))
        self.assertEqual(profile["mcp_name"], "hpclib-login")
        self.assertIn(f"launch_tunnel -A none -P {profile['port']} me@login.example rest "
                      f"--process-port={profile['process_port']} -- --allow {self.work[0]} --allow {self.work[1]}", out)
        mcp = json.loads((self.profile_dir / "mcp.json").read_text())["mcpServers"]["hpclib-login"]
        self.assertEqual(mcp["args"][1:], ["--url", f"http://127.0.0.1:{profile['port']}",
                                           "--token-file", str(self.token_file)])
        self.assertIn('"hpclib-login": {', out)                        # the JSON is in the output
        self.assertIn("claude mcp add-json hpclib-login", out)
        self.assertNotIn("\033[", out[out.index("hpclib agents:"):])     # no colours when not a terminal

        # a rerun with just the address uses the profile: same directories, ports and tokens ...
        (self.rest / "templates" / "orca" / "template.json").write_text('{"edited": true}')
        config["cluster_notes"] = "mine"
        (self.rest / "config.json").write_text(json.dumps(config))
        out = self.setup_agents("--no-install", "me@login.example")
        self.assertIn("kept the existing orca template", out)
        self.assertEqual(self.config()["cluster_notes"], "mine")
        self.assertEqual(self.token_file.read_text().strip(), token)
        self.assertEqual((self.profile()["port"], self.profile()["work_dirs"]), (profile["port"], profile["work_dirs"]))

        # ... and --rebuild replaces everything, keeping old copies on the cluster
        image = self.rest / "sandbox" / "host"
        image.mkdir(parents=True, exist_ok=True)
        (image / "stale").write_text("")
        out = self.setup_agents("--no-install", "--rebuild", "me@login.example")
        self.assertNotIn("edited", (self.rest / "templates" / "orca" / "template.json").read_text())
        self.assertEqual(json.loads(next((self.rest / "templates" / ".replaced").glob("orca-*")).joinpath(
            "template.json").read_text()), {"edited": True})
        self.assertNotEqual(self.config()["cluster_notes"], "mine")
        self.assertIn("/sw", self.config()["sandbox"]["binds"])          # the profile's binds again
        self.assertEqual(len(list(self.rest.glob("config.json.replaced-*"))), 1)
        self.assertFalse((image / "stale").exists())   # rebuilt (the probe may have made a fresh one)
        new_token = self.token_file.read_text().strip()
        self.assertNotEqual(new_token, token)
        self.assertIsNone(self.identify(token))                 # the old token was revoked
        self.assertEqual(self.identify(new_token).name, self.agent_name)
        self.assertIn(f"revoked '{self.agent_name}' (the token in", out)
        # the owner token is kept on this machine, and its hash reinstalled
        self.assertEqual(self.owner_file.read_text().strip(), owner)
        self.assertEqual(TokenAuth.load(str(self.remote_home / ".local" / "tunnels" / "rest_token"))
                         .identify(f"Bearer {owner}").name, "owner")
        # new ports on request
        self.setup_agents("--no-install", "--new-ports", "me@login.example")
        self.assertNotEqual(self.profile()["port"], profile["port"])

    def test_profiles_are_per_cluster(self):
        self.setup_agents("--work-dir", str(self.work[0]), "--templates", "hello", "me@login.example")
        other_home = self.tmp / "other remote"
        other_home.mkdir()
        fake = FAKE_SSH.replace("me@login.example", "me@other.example").replace("TEST_REMOTE_HOME", "TEST_OTHER_HOME")
        (self.tmp / "bin" / "ssh").write_text(
            "#!/usr/bin/env bash\ncase \"$*\" in *me@other.example*) exec \"$(dirname \"$0\")/ssh-other\" \"$@\";; esac\n"
            + FAKE_SSH.split("\n", 1)[1])
        (self.tmp / "bin" / "ssh-other").write_text(fake)
        (self.tmp / "bin" / "ssh-other").chmod(0o755)
        self.env["TEST_OTHER_HOME"] = str(other_home)
        self.setup_agents("--work-dir", str(self.work[1]), "--templates", "hello", "me@other.example")
        first, second = self.profile(), self.profile("me@other.example")
        self.assertNotEqual(first["port"], second["port"])
        self.assertNotEqual(first["token_file"], second["token_file"])
        self.assertEqual(second["mcp_name"], "hpclib-other")
        self.assertTrue((other_home / ".local" / "tunnels" / "rest" / "tokens.json").exists())
        out = self.bash("agent_list")
        self.assertIn("me@login.example", out)
        self.assertIn("me@other.example", out)

    def test_agent_tunnel_uses_the_profile(self):
        self.setup_agents("--work-dir", str(self.work[0]), "--templates", "hello", "me@login.example")
        profile = self.profile()
        out = self.bash('launch_tunnel() { printf "%s\\n" "$*"; }; agent_tunnel "$@"', "hpclib-login", "--time=2:00:00")
        self.assertIn(f"-A none -P {profile['port']} me@login.example rest --process-port={profile['process_port']} "
                      f"--time=2:00:00 -- --allow {self.work[0]}", out)
        out = self.bash('stop_tunnel() { printf "%s\\n" "$*"; }; agent_stop "$@"', "me@login.example")
        self.assertIn(f"-P {profile['port']} me@login.example", out)
        self.bash("agent_tunnel nowhere", expect=1)
        out = self.bash('eval "$(agent_env me@login.example)" && echo "$HPC_REST_URL $HPC_REST_TOKEN_FILE"')
        self.assertIn(f"http://127.0.0.1:{profile['port']} {self.token_file}", out)

    def test_colours_on_request(self):
        env = dict(self.env, HPCLIB_COLOR="always")
        out = self.bash('setup_agents "$@"', "--work-dir", str(self.work[0]), "--templates", "hello",
                        "me@login.example", env=env)
        self.assertIn("\033[1;97;44m", out)
        self.assertIn("\033[33m{", out)                                # the JSON block is coloured
        json_lines = [line[len("\033[33m"):-len("\033[0m")] for line in out.splitlines()
                      if line.startswith("\033[33m")]
        self.assertIn("hpclib-login", json.loads("\n".join(json_lines))["mcpServers"])   # and still valid JSON

    def test_existing_tokens_are_adopted(self):
        """Before profiles, the tokens were ~/.config/hpclib/{llm_token,rest_token}."""
        legacy_agent = self.local_home / ".config" / "hpclib" / "llm_token"
        legacy_owner = self.local_home / ".config" / "hpclib" / "rest_token"
        self.setup_agents("--work-dir", str(self.work[0]), "--templates", "hello", "--token-name", "llm-scan",
                          "--token-file", str(legacy_agent), "--owner-token-file", str(legacy_owner), "--name", "old",
                          "me@login.example")
        shutil.rmtree(self.local_home / ".config" / "hpclib" / "agents")   # as if made by the old setup_agents
        out = self.setup_agents("--no-install", "--work-dir", str(self.work[0]), "--templates", "hello",
                                "me@login.example")
        self.assertIn("copied this cluster's owner token", out)
        self.assertIn("copied this cluster's 'llm-scan' token", out)
        self.assertEqual(self.token_file.read_text(), legacy_agent.read_text())
        self.assertEqual(self.owner_file.read_text(), legacy_owner.read_text())
        self.assertEqual(self.profile()["token_name"], "llm-scan")
        self.assertTrue(legacy_agent.exists() and legacy_owner.exists())   # left in place

    def test_rebuild_revokes_the_token_in_the_file_whatever_its_name(self):
        self.setup_agents("--token-name", "llm-scan", "--work-dir", str(self.work[0]), "--templates", "hello",
                          "me@login.example")
        old = self.token_file.read_text().strip()
        self.assertEqual(self.identify(old).name, "llm-scan")
        out = self.setup_agents("--no-install", "--rebuild", "--token-name", "renamed", "me@login.example")
        self.assertIn("revoked 'llm-scan'", out)
        self.assertIsNone(self.identify(old))
        self.assertEqual(self.identify(self.token_file.read_text().strip()).name, "renamed")

    def test_existing_config_gets_a_sandbox(self):
        self.rest.mkdir(parents=True)
        (self.rest / "config.json").write_text(json.dumps({"cluster_notes": "old"}))
        self.setup_agents("--work-dir", str(self.work[0]), "--templates", "hello", "me@login.example")
        config = self.config()
        self.assertEqual((config["cluster_notes"], config["sandbox"]["method"]), ("old", "auto"))
        self.assertEqual(len(list(self.rest.glob("config.json.replaced-*"))), 1)

    def test_no_sandbox_and_usage(self):
        self.setup_agents("--work-dir", str(self.work[0]), "--no-sandbox", "--templates", "hello", "me@login.example")
        self.assertEqual(self.config()["sandbox"], {"method": "none"})
        self.setup_agents("--no-install", "nobody@new.example", expect=2)     # no profile, no --work-dir
        self.setup_agents("--work-dir", "relative/dir", "me@login.example", expect=2)
        out = self.setup_agents("--no-install", "--templates", "nope", "me@login.example", expect=1)
        self.assertIn("no template 'nope'", out)


if __name__ == "__main__":
    unittest.main()
