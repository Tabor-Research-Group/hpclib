"""Tests for sandboxed template jobs (rest_sandbox.py) and GET /sandbox.

Run with:  python -m unittest tests.test_rest_sandbox -v

Most tests use a fake `singularity` that records its arguments and runs
the command directly. TestRealSingularity runs real containers and is
skipped where neither Singularity nor Apptainer is installed.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_rest_jobs import JobServerTestCase, fake_job  # noqa: E402
import rest_sandbox  # noqa: E402

# Plays `singularity`: `exec --help` advertises --no-mount; `-q exec ...`
# logs its arguments, then runs the command after the image directly.
FAKE_RUNTIME = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    if args == ["exec", "--help"]:
        print("      --no-mount strings   disable one or more 'mount xxx' options")
        sys.exit(0)
    if args == ["--version"]:
        print("singularity-ce version 0.0-fake"); sys.exit(0)
    if args == ["buildcfg"]:
        sys.exit(1)
    with open(os.environ["FAKE_RUNTIME_LOG"], "a") as f:
        f.write(json.dumps(args) + "\\n")
    rest = args[args.index("exec") + 1:]
    i = 0
    while rest[i].startswith("-"):
        if rest[i] in ("--bind", "--no-mount", "--workdir", "--pwd"):
            if rest[i] == "--pwd":
                os.chdir(rest[i + 1])
            i += 2
        else:
            i += 1
    os.execvp(rest[i + 1], rest[i + 1:])
""")


class TestSandboxConfig(unittest.TestCase):

    def test_validation(self):
        for bad in ({"method": "chroot"}, {"binds": "/sw"}, {"binds": ["sw"]}, {"scratch": "big"},
                    {"allow_unsandboxed": "yes"}, {"surprise": 1}):
            with self.assertRaises(ValueError, msg=bad):
                rest_sandbox.Sandbox(bad)

    def test_unconfigured_runs_unsandboxed(self):
        method, reason = rest_sandbox.Sandbox(None).resolve()
        self.assertEqual(method, "none")
        self.assertIn("no `sandbox`", reason)

    def test_missing_runtime(self):
        nothing = lambda name: None  # noqa: E731
        with self.assertRaises(rest_sandbox.SandboxError) as ctx:
            rest_sandbox.Sandbox({"method": "auto"}, which=nothing).resolve()
        self.assertIn("not found", str(ctx.exception))
        method, _ = rest_sandbox.Sandbox({"method": "auto", "allow_unsandboxed": True}, which=nothing).resolve()
        self.assertEqual(method, "none")
        with self.assertRaises(rest_sandbox.SandboxError):   # only "auto" may fall back
            rest_sandbox.Sandbox({"method": "singularity", "allow_unsandboxed": True}, which=nothing).resolve()

    def test_interpreters(self):
        self.assertEqual(rest_sandbox.Sandbox.interpreter("#!/bin/bash -l"), ["/bin/bash", "-l"])
        self.assertEqual(rest_sandbox.Sandbox.interpreter("#!/usr/bin/env python3"), ["/usr/bin/env", "python3"])
        with self.assertRaises(rest_sandbox.SandboxError):
            rest_sandbox.Sandbox.interpreter("#!/usr/bin/perl")

    def test_host_image(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        image = tmp / "host"
        rest_sandbox.build_host_image(str(image), ["/usr", "/srv/hpc-test-data", str(tmp / "data"),
                                                   os.path.expanduser("~/x")])
        for d in ("usr", "etc", "tmp", "proc", "dev", "home"):
            self.assertTrue((image / d).is_dir(), d)
        self.assertTrue((image / "etc" / "passwd").exists())
        if os.path.islink("/bin"):
            self.assertEqual(os.readlink(image / "bin"), os.readlink("/bin"))
        self.assertTrue((image / "srv" / "hpc-test-data").exists())
        # nothing under the home directory or /tmp: the container gets fresh ones
        self.assertFalse((image / os.path.expanduser("~/x").lstrip("/")).exists())
        if str(tmp).startswith("/tmp/"):
            self.assertFalse((image / str(tmp / "data").lstrip("/")).exists())


class SandboxServerTestCase(JobServerTestCase):

    def use_sandbox(self, config, runtime=True):
        bin_dir = self.tmp / "runtime-bin"
        bin_dir.mkdir(exist_ok=True)
        fake = bin_dir / "singularity"
        fake.write_text(FAKE_RUNTIME)
        fake.chmod(0o755)
        self.runtime_log = self.tmp / "runtime.log"
        os.environ["FAKE_RUNTIME_LOG"] = str(self.runtime_log)
        self.addCleanup(os.environ.pop, "FAKE_RUNTIME_LOG", None)
        which = (lambda name: str(fake) if name == "singularity" else None) if runtime else (lambda name: None)
        sandbox = rest_sandbox.Sandbox(config, data_dir=str(self.data / "rest"), which=which)
        self.jobs.sandbox = sandbox
        self.server.sandbox_prober = rest_sandbox.Prober(sandbox, str(self.data / "rest" / "sandbox" / "probe"))
        return sandbox

    def run_job_script(self, job_id, cwd):
        script = fake_job(self.slurm, job_id)["script"]
        env = dict(os.environ, TMPDIR=str(self.tmp))
        return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True, env=env, timeout=60)


class TestSandboxedJobs(SandboxServerTestCase):

    def test_job_body_runs_in_the_sandbox(self):
        self.use_sandbox({"method": "auto", "binds": ["/opt"], "writable": [str(self.outside)]})
        out = self.llm.submit_job("hello", params={"message": "hi there"}, workdir=str(self.llm_root))
        plan = out["sandbox"]
        self.assertEqual(plan["method"], "singularity")
        self.assertEqual(plan["read_write"], [str(self.llm_root), str(self.outside)])
        self.assertIn("/usr", plan["read_only"])
        res = self.run_job_script(out["job_id"], self.llm_root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("hi there", res.stdout)                      # parameters reach the body
        args = json.loads(self.runtime_log.read_text().splitlines()[-1])
        self.assertEqual(args[:5], ["-q", "exec", "--contain", "--pid", "--ipc"])
        self.assertIn("bind-paths", args)
        pairs = [args[i + 1] for i, a in enumerate(args) if a == "--bind"]
        self.assertIn(f"{self.llm_root}:{self.llm_root}", pairs)
        self.assertIn("/usr:/usr:ro", pairs)
        self.assertIn("/etc:/etc:ro", pairs)
        self.assertNotIn(str(self.root), [p.split(":")[0] for p in pairs])   # only the token's directory
        self.assertEqual(list(self.tmp.glob("hpc-sandbox.*")), [])         # scratch removed

    def test_exit_status_and_dry_run(self):
        self.use_sandbox({"method": "singularity"})
        out = self.owner.submit_job("hello", params={"message": "x"}, workdir=str(self.llm_root), dry_run=True)
        self.assertEqual(out["sandbox"]["method"], "singularity")
        self.assertIn("hpc_sandbox_body=$(cat <<'HPC_SANDBOX_BODY_", out["script"])
        # the owner token is not limited to a directory: the server's --allow list applies
        self.assertEqual(out["sandbox"]["read_write"], [str(self.root)])

        templates = self.templates / "fails"
        templates.mkdir()
        (templates / "template.json").write_text(json.dumps({"description": "exit 7", "parameters": {},
                                                             "resources": {"time": "00:05:00"}}))
        (templates / "script.sh").write_text("#!/bin/bash\necho before\nexit 7\n")
        job = self.llm.submit_job("fails", workdir=str(self.llm_root))
        res = self.run_job_script(job["job_id"], self.llm_root)
        self.assertEqual(res.returncode, 7)
        self.assertIn("before", res.stdout)

    def test_missing_runtime_refuses_jobs(self):
        self.use_sandbox({"method": "auto"}, runtime=False)
        payload = self.expect_error(503, self.llm.submit_job, "hello", workdir=str(self.llm_root))
        self.assertIn("not found", payload["error"])
        self.assertEqual(self.llm.cluster()["sandbox"]["effective"], "unavailable")

    def test_unsandboxed_fallback_is_reported(self):
        self.use_sandbox({"method": "auto", "allow_unsandboxed": True}, runtime=False)
        out = self.llm.submit_job("hello", workdir=str(self.llm_root))
        self.assertEqual(out["sandbox"]["method"], "none")
        self.assertNotIn("hpc_sandbox_body", fake_job(self.slurm, out["job_id"])["script"])

    def test_probe_route(self):
        self.use_sandbox({"method": "auto"})
        reader = type(self.llm)(self.url, token=self.reader_token)
        info = reader.sandbox()
        for key in ("kernel", "security_modules", "user_namespaces", "landlock", "container_runtimes",
                    "modules", "recommended_config", "sandbox"):
            self.assertIn(key, info)
        self.assertEqual(info["container_runtimes"][0]["no_mount_flag"], True)
        self.assertEqual(info["sandbox"]["effective"], "singularity")
        self.assertTrue(info["self_test"]["ran"])
        # the fake runtime isolates nothing, so the self-test must notice
        self.assertFalse(info["self_test"]["passed"])
        self.assertFalse(info["self_test"]["checks"]["outside_hidden"])
        self.assertEqual(reader.sandbox()["probed_at"], info["probed_at"])   # cached


REAL_RUNTIME = shutil.which("apptainer") or shutil.which("singularity")


@unittest.skipUnless(REAL_RUNTIME, "needs Singularity or Apptainer")
@unittest.skipIf(os.geteuid() == 0, "run as a regular user, as jobs do on a cluster")
class TestRealSingularity(unittest.TestCase):
    """Runs real containers with the host image."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=os.environ.get("HPC_SANDBOX_TEST_DIR"))).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.allowed, self.other = self.tmp / "allowed", self.tmp / "other"
        self.allowed.mkdir()
        self.other.mkdir()
        (self.other / "secret.txt").write_text("not for jobs\n")
        self.sandbox = rest_sandbox.Sandbox({"method": "auto"}, data_dir=str(self.tmp / "data"))

    def run_body(self, body, shebang="#!/bin/bash"):
        lines, plan = self.sandbox.launch(shebang, body, [str(self.allowed)])
        script = "\n".join(["#!/bin/bash"] + lines) + "\n"
        return subprocess.run(["bash", "-c", script], cwd=self.allowed, capture_output=True, text=True,
                              timeout=120, env=dict(os.environ, HPC_PARAM_X="from the host"))

    def test_isolation(self):
        res = self.run_body(textwrap.dedent(f"""\
            set -u
            echo "param=$HPC_PARAM_X"
            echo ok > {self.allowed}/written && echo "write_allowed=yes"
            [ -e {self.other}/secret.txt ] && echo "other_visible=yes" || echo "other_visible=no"
            (: > /usr/.probe) 2>/dev/null && echo "usr_writable=yes" || echo "usr_writable=no"
            (: > /etc/.probe) 2>/dev/null && echo "etc_writable=yes" || echo "etc_writable=no"
            echo "pwd=$PWD"
            echo "tmp=$(touch /tmp/t && echo yes)"
            """))
        self.assertEqual(res.returncode, 0, res.stderr)
        out = dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
        self.assertEqual(out["param"], "from the host")
        self.assertEqual(out["write_allowed"], "yes")
        self.assertEqual(out["other_visible"], "no")
        self.assertEqual(out["usr_writable"], "no")
        self.assertEqual(out["etc_writable"], "no")
        self.assertEqual(out["pwd"], str(self.allowed))
        self.assertEqual(out["tmp"], "yes")
        self.assertEqual((self.allowed / "written").read_text(), "ok\n")

    def test_python_body_and_host_programs(self):
        res = self.run_body("import os, sys\nprint('py', sys.version_info[0], os.getcwd())\n",
                            shebang="#!/usr/bin/env python3")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"py 3 {self.allowed}", res.stdout)

    def test_self_test_passes(self):
        result = rest_sandbox.self_test(self.sandbox, str(self.tmp))
        self.assertTrue(result["passed"], result)


if __name__ == "__main__":
    unittest.main()
