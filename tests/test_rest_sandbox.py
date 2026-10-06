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


class TestAccountFiles(unittest.TestCase):
    """The passwd/group/nsswitch.conf the job writes for the container: your account, which SSSD gives on the host."""

    def test_directory_account(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        # an account only the directory service knows, in a big directory group
        (bin_dir / "id").write_text(textwrap.dedent("""\
            #!/bin/sh
            case "$1" in -u|-g) echo 54321 ;; -un) echo maboyer ;; -G) echo "54321 9000 0" ;; esac
            """))
        (bin_dir / "getent").write_text(textwrap.dedent("""\
            #!/bin/sh
            case "$1 $2" in
              "passwd 54321") echo "maboyer:*:54321:54321:Mark Boyer:/home/maboyer:/bin/bash" ;;
              "group 54321") echo "maboyer:*:54321:" ;;
              "group 9000") echo "chem:*:9000:a,b,c,d,e,f,g,h" ;;
              *) exit 2 ;;
            esac
            """))
        for f in bin_dir.iterdir():
            f.chmod(0o755)
        script = "\n".join(rest_sandbox.Sandbox.account_lines() + [
            'cat "$hpc_sandbox_etc/passwd"; echo ---; cat "$hpc_sandbox_etc/group"; echo ---',
            'cat "$hpc_sandbox_etc/nsswitch.conf" 2>/dev/null; echo ---; printf "%s\\n" "${hpc_sandbox_account[@]}"',
            'rm -rf "$hpc_sandbox_etc"'])
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", TMPDIR=str(tmp)))
        self.assertEqual(res.returncode, 0, res.stderr)
        passwd, group, nsswitch, binds = res.stdout.split("---\n")
        self.assertIn("maboyer:*:54321:54321:Mark Boyer:/home/maboyer:/bin/bash", passwd.splitlines())
        self.assertEqual([line.split(":")[2] for line in passwd.splitlines()].count("54321"), 1)
        self.assertIn("chem:*:9000:maboyer", group.splitlines())          # without the other members
        self.assertIn("maboyer:*:54321:maboyer", group.splitlines())
        self.assertEqual(sum(line.split(":")[2] == "0" for line in group.splitlines() if line.count(":") >= 3),
                         sum(line.split(":")[2] == "0" for line in Path("/etc/group").read_text().splitlines()
                             if line.count(":") >= 3))                     # local groups are not repeated
        if Path("/etc/nsswitch.conf").exists():
            self.assertIn("passwd: files", nsswitch)
            self.assertNotIn("sss", " ".join(l for l in nsswitch.splitlines() if l.startswith(("passwd", "group"))))
        binds = binds.split()
        self.assertEqual(binds[:2], ["--bind", binds[1]])
        self.assertTrue(binds[1].endswith("/passwd:/etc/passwd:ro"))
        self.assertTrue(binds[3].endswith("/group:/etc/group:ro"))
        self.assertEqual(list(tmp.glob("hpc-sandbox-etc.*")), [])

    def test_unknown_account(self):
        # no directory answer either: a made-up entry still lets the uid be looked up
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "id").write_text('#!/bin/sh\ncase "$1" in -u|-g) echo 777 ;; -un) exit 1 ;; -G) echo 777 ;; esac\n')
        (bin_dir / "getent").write_text("#!/bin/sh\nexit 2\n")
        for f in bin_dir.iterdir():
            f.chmod(0o755)
        script = "\n".join(rest_sandbox.Sandbox.account_lines() + ['cat "$hpc_sandbox_etc/passwd" "$hpc_sandbox_etc/group"'])
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", TMPDIR=str(tmp)))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("user777:x:777:777::/nonexistent:/bin/bash", res.stdout.splitlines())
        self.assertIn("group777:x:777:user777", res.stdout.splitlines())


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
        self.assertEqual(args[:6], ["-q", "exec", "--contain", "--no-home", "--pid", "--ipc"])
        self.assertIn("bind-paths", args)
        pairs = [args[i + 1] for i, a in enumerate(args) if a == "--bind"]
        self.assertIn(f"{self.llm_root}:{self.llm_root}", pairs)
        self.assertIn("/usr:/usr:ro", pairs)
        self.assertIn("/etc:/etc:ro", pairs)
        # your account, over the host's /etc (so after its bind), from files the job wrote and removed
        etc = pairs.index("/etc:/etc:ro")
        account = [p for p in pairs if p.endswith(("/etc/passwd:ro", "/etc/group:ro", "/etc/nsswitch.conf:ro"))]
        self.assertGreaterEqual(len(account), 2)
        self.assertTrue(all(pairs.index(p) > etc for p in account))
        self.assertEqual(list(self.tmp.glob("hpc-sandbox-etc.*")), [])
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

    def test_host_environment_reaches_the_body(self):
        """Modules put programs and libraries on PATH and LD_LIBRARY_PATH, which the runtime would reset."""
        bin_dir = self.allowed / "bin"
        bin_dir.mkdir()
        prog = bin_dir / "from-a-module"
        prog.write_text("#!/bin/sh\necho module program ran\n")
        prog.chmod(0o755)
        lines, _ = self.sandbox.launch("#!/bin/bash", 'from-a-module; echo "ld=$LD_LIBRARY_PATH"; echo "tmp=$TMPDIR"',
                                       [str(self.allowed)])
        script = "\n".join(["#!/bin/bash"] + lines) + "\n"
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", LD_LIBRARY_PATH="/software/lib",
                   TMPDIR=str(self.tmp))
        res = subprocess.run(["bash", "-c", script], cwd=self.allowed, capture_output=True, text=True,
                             timeout=120, env=env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("module program ran", res.stdout)
        self.assertIn("ld=/software/lib", res.stdout)
        self.assertIn("tmp=/tmp\n", res.stdout)

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
            echo "var_tmp=$(touch /var/tmp/t && echo yes)"
            (: > "$HOME/x") 2>/dev/null && echo "home_writable=yes" || echo "home_writable=no"
            (: > /.x) 2>/dev/null && echo "root_writable=yes" || echo "root_writable=no"
            """))
        self.assertEqual(res.returncode, 0, res.stderr)
        out = dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
        self.assertEqual(out["param"], "from the host")
        self.assertEqual(out["write_allowed"], "yes")
        self.assertEqual(out["other_visible"], "no")
        self.assertEqual(out["usr_writable"], "no")
        self.assertEqual(out["etc_writable"], "no")
        self.assertEqual(out["pwd"], str(self.allowed))
        self.assertEqual(out["tmp"], "yes")                  # scratch stays writable ...
        self.assertEqual(out["var_tmp"], "yes")
        self.assertEqual(out["home_writable"], "no")         # ... nothing else that would vanish is
        self.assertEqual(out["root_writable"], "no")
        self.assertEqual((self.allowed / "written").read_text(), "ok\n")

    def test_your_account_is_known(self):
        """Programs that look up their uid (Postgres refuses to start otherwise) find it, even from SSSD."""
        res = self.run_body("id -un; python3 -c 'import os, pwd; print(pwd.getpwuid(os.getuid()).pw_name)'\n"
                            "(: > /etc/passwd) 2>/dev/null && echo passwd_writable || echo passwd_read_only")
        self.assertEqual(res.returncode, 0, res.stderr)
        name = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(res.stdout.split(), [name, name, "passwd_read_only"])

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


# Plays `podman`: `info` answers from $FAKE_PODMAN_INFO; `run ...` logs its arguments, then runs the
# command after the image directly, in its --workdir and with its --env values.
FAKE_PODMAN = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    args = sys.argv[1:]
    if args[:1] == ["info"]:
        print(os.environ.get("FAKE_PODMAN_INFO", "{}")); sys.exit(0)
    if args == ["--version"]:
        print("podman version 0.0-fake"); sys.exit(0)
    if args[:1] == ["unshare"]:
        os.execvp(args[1], args[1:])
    if args[:1] == ["rm"]:
        sys.exit(0)
    with open(os.environ["FAKE_RUNTIME_LOG"], "a") as f:
        f.write(json.dumps(args) + "\\n")
    rest = args[1:]
    valued = {"-v", "--env", "--label", "--name", "--hostname", "--workdir", "--cpus", "--memory", "--memory-swap",
              "--tmpfs", "--group-add", "--device", "--sysctl"}
    env, cwd, i = dict(os.environ), None, 0
    while rest[i].startswith("-"):
        if rest[i] in valued:
            if rest[i] == "--workdir":
                cwd = rest[i + 1]
            elif rest[i] == "--env":
                k, _, v = rest[i + 1].partition("=")
                env[k] = v
            i += 2
        else:
            i += 1
    sys.exit(subprocess.run(rest[i + 1:], cwd=cwd, env=env).returncode)
""")

PODMAN_V2 = {"host": {"cgroupVersion": "v2", "cgroupControllers": ["cpu", "memory", "pids"],
                      "ociRuntime": {"name": "crun"}, "security": {"rootless": True}}}


class TestPodmanSandbox(SandboxServerTestCase):

    def use_podman(self, config, info=PODMAN_V2, singularity=False):
        bin_dir = self.tmp / "podman-bin"
        bin_dir.mkdir(exist_ok=True)
        fake = bin_dir / "podman"
        fake.write_text(FAKE_PODMAN)
        fake.chmod(0o755)
        self.runtime_log = self.tmp / "runtime.log"
        for key, value in (("FAKE_RUNTIME_LOG", str(self.runtime_log)), ("FAKE_PODMAN_INFO", json.dumps(info))):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        which = lambda name: str(fake) if name == "podman" else ("/usr/bin/singularity" if singularity else None)  # noqa: E731
        sandbox = rest_sandbox.Sandbox(config, data_dir=str(self.data / "rest"), which=which)
        self.jobs.sandbox = sandbox
        return sandbox

    def args(self):
        return json.loads(self.runtime_log.read_text().splitlines()[-1])

    def test_config(self):
        self.assertEqual(rest_sandbox.Sandbox({"method": "podman", "image": "docker.io/library/ubuntu:24.04"}).image,
                         "docker.io/library/ubuntu:24.04")
        for bad in ({"method": "auto", "image": "ubuntu:24.04"}, {"method": "singularity", "image": "ubuntu"},
                    {"method": "podman", "network": "host"}, {"method": "podman", "image": "Bad Name"}):
            with self.assertRaises(ValueError, msg=bad):
                rest_sandbox.Sandbox(bad)
        # auto takes Singularity/Apptainer where there is one, else podman
        self.assertEqual(self.use_podman({"method": "auto"}).resolve()[0], "podman")
        self.assertEqual(self.use_podman({"method": "auto"}, singularity=True).resolve()[0], "singularity")
        self.assertEqual(self.use_podman({"method": "podman"}, singularity=True).resolve()[0], "podman")

    def test_job_runs_in_podman(self):
        self.use_podman({"method": "podman", "binds": ["/opt"]})
        out = self.llm.submit_job("hello", params={"message": "hi there"}, workdir=str(self.llm_root))
        self.assertEqual((out["sandbox"]["method"], out["sandbox"]["network"]), ("podman", "none"))
        res = self.run_job_script(out["job_id"], self.llm_root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("hi there", res.stdout)
        args = self.args()
        self.assertEqual(args[:2], ["run", "--rm"])
        for flag in ("--pull=never", "--userns=keep-id", "--cap-drop=all", "--security-opt=no-new-privileges",
                     "--read-only", "--network=none", "--env-host"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--group-add") + 1], "keep-groups")        # crun keeps your groups
        volumes = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        self.assertIn(f"{self.llm_root}:{self.llm_root}", volumes)
        self.assertIn("/usr:/usr:ro", volumes)
        self.assertTrue(any(v.endswith(":/etc/passwd:ro") for v in volumes))     # your account
        self.assertTrue(any(v.endswith(":/tmp") for v in volumes))               # the job's own scratch
        self.assertEqual(args[args.index("--rootfs") + 1], str(self.data / "rest" / "sandbox" / "host"))
        self.assertEqual(args[args.index("--workdir") + 1], str(self.llm_root))
        self.assertNotIn("--memory", args)                                        # SLURM sets limits, not podman
        self.assertEqual(list(self.tmp.glob("hpc-sandbox.*")), [])                # scratch removed

    def test_local_scheduler_limits_reach_podman(self):
        self.use_podman({"method": "podman"})
        out = self.llm.submit_job("hello", params={"message": "x"}, workdir=str(self.llm_root))
        script = fake_job(self.slurm, out["job_id"])["script"]
        env = dict(os.environ, TMPDIR=str(self.tmp), HPC_JOB_CPUS="2", HPC_JOB_MEMORY="512m", HPC_JOB_TAG="7-0")
        res = subprocess.run(["bash", "-c", script], cwd=self.llm_root, capture_output=True, text=True, env=env,
                             timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        args = self.args()
        self.assertEqual(args[args.index("--cpus") + 1], "2")
        self.assertEqual((args[args.index("--memory") + 1], args[args.index("--memory-swap") + 1]), ("512m", "512m"))
        self.assertEqual(args[args.index("--name") + 1], "hpclib-job-7-0")
        # without subordinate ids only your own group exists in the container; crun's ping sysctl must name it
        self.assertEqual(args[args.index("--sysctl") + 1], f"net.ipv4.ping_group_range={os.getgid()} {os.getgid()}")

    def test_only_delegated_limits_are_asked_for(self):
        # RHEL 9: memory and pids are delegated, cpu isn't; podman would refuse to start a container with --cpus
        rhel = {"host": {"cgroupVersion": "v2", "cgroupControllers": ["memory", "pids"], "ociRuntime": {"name": "crun"}}}
        sandbox = self.use_podman({"method": "podman"}, info=rhel)
        limits = sandbox.limits()
        self.assertEqual((limits["enforced"], limits["cpu"], limits["memory"]), (False, False, True))
        out = self.llm.submit_job("hello", params={"message": "x"}, workdir=str(self.llm_root))
        script = fake_job(self.slurm, out["job_id"])["script"]
        env = dict(os.environ, TMPDIR=str(self.tmp), HPC_JOB_CPUS="2", HPC_JOB_MEMORY="512m")
        res = subprocess.run(["bash", "-c", script], cwd=self.llm_root, capture_output=True, text=True, env=env,
                             timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        args = self.args()
        self.assertNotIn("--cpus", args)
        self.assertEqual(args[args.index("--memory") + 1], "512m")

    def test_syncs_get_the_network(self):
        sandbox = self.use_podman({"method": "podman"})
        lines, plan = sandbox.launch("#!/bin/bash", "true", [str(self.llm_root)], network=True)
        self.assertEqual(plan["network"], "default")
        self.assertNotIn("--network=none", "\n".join(lines))
        lines, plan = sandbox.launch("#!/bin/bash", "true", [str(self.llm_root)])
        self.assertIn("--network=none", "\n".join(lines))

    def test_limits(self):
        self.assertTrue(self.use_podman({"method": "podman"}).limits()["enforced"])
        v1 = {"host": {"cgroupVersion": "v1", "cgroupControllers": [], "ociRuntime": {"name": "runc"}}}
        limits = self.use_podman({"method": "podman"}, info=v1).limits()
        self.assertEqual((limits["enforced"], "v1" in limits["reason"]), (False, True))
        partial = {"host": {"cgroupVersion": "v2", "cgroupControllers": ["cpu", "pids"], "ociRuntime": {"name": "crun"}}}
        limits = self.use_podman({"method": "podman"}, info=partial).limits()
        self.assertEqual((limits["enforced"], "memory" in limits["reason"]), (False, True))
        self.assertFalse(rest_sandbox.Sandbox({"method": "none"}).limits()["enforced"])
        described = self.use_podman({"method": "podman"}, info=v1).describe()
        self.assertEqual((described["effective"], described["supplementary_groups"]), ("podman", False))

    def test_podman_counts_as_sandboxed(self):
        self.use_podman({"method": "podman"})
        self.jobs.auto_approve = "all"
        self.assertEqual(self.jobs.proposal_policy()["review"], "automatic")


class TestRealPodman(unittest.TestCase):
    """Real rootless containers; skipped as root (rootless podman is the point) or without podman."""

    def setUp(self):
        podman = shutil.which("podman")
        if podman is None or os.geteuid() == 0:
            self.skipTest("needs podman, run as an ordinary user")
        if subprocess.run([podman, "info"], capture_output=True, timeout=60).returncode != 0:
            self.skipTest("podman info fails here")
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_self_test_passes(self):
        sandbox = rest_sandbox.Sandbox({"method": "podman"}, data_dir=str(self.tmp / "data"))
        result = rest_sandbox.self_test(sandbox, str(self.tmp))
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["checks"]["network_isolated"], result)
