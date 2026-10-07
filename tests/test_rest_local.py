"""Tests for the local scheduler (rest_local.py): template jobs on a machine without SLURM.

Run with:  python -m unittest tests.test_rest_local -v

Jobs run for real here (as plain processes; the sandbox is off, so limits aren't enforced and the tests turn
enforce_limits off, except where they check that jobs are then refused).
"""
import json
import os
import sys
import tempfile
import shutil
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_rest_jobs import JobServerTestCase  # noqa: E402
import rest_jobs  # noqa: E402
import rest_local  # noqa: E402
import rest_sandbox  # noqa: E402

UNSANDBOXED = rest_sandbox.Sandbox({"method": "none"})
SCRIPT = ('#!/bin/bash\necho "job $SLURM_JOB_ID task ${SLURM_ARRAY_TASK_ID:-none} cpus $SLURM_CPUS_PER_TASK '
          'mem $HPC_JOB_MEMORY scheduler $HPC_JOB_SCHEDULER"\nsleep "${SLEEP:-0}"\n')


def wait_for(fn, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out")


class LocalRunnerTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.runner = self.make()

    def make(self, start=True, **config):
        config = dict({"type": "local", "cpus": 2, "memory": "1G", "enforce_limits": False}, **config)
        runner = rest_local.LocalRunner(config, str(self.tmp / "data"), UNSANDBOXED, poll=0.1, start=start)
        self.addCleanup(runner.stop)
        return runner

    def sbatch(self, *args, script=SCRIPT, runner=None):
        res = (runner or self.runner).run(["sbatch", "--parsable", f"--chdir={self.work}", "--mem=100M", *args],
                                          input=script, cwd=str(self.work))
        return res

    def submit(self, *args, **kwargs):
        res = self.sbatch(*args, **kwargs)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.strip()

    def state(self, job_id, runner=None):
        return (runner or self.runner).query_states([job_id])[job_id]

    def finished(self, job_id, runner=None):
        return wait_for(lambda: self.state(job_id, runner) if self.state(job_id, runner)["terminal"] else None)


class TestLocalRunner(LocalRunnerTestCase):

    def test_refusals(self):
        for args, words in ((["--gres=gpu:1"], "device flags"), (["--nodes=2"], "--nodes must be 1"),
                            (["--cpus-per-task=4"], "2 at most"), (["--mem=2G"], "1024 MB at most"),
                            (["--time=8-00:00:00"], "max_time"), (["--array=1-3"], "only 0-N%T"),
                            (["--exclusive=user"], "doesn't support")):
            res = self.sbatch(*args)
            self.assertEqual(res.returncode, 1, args)
            self.assertIn(words, res.stderr, args)

    def test_job_runs_with_slurm_like_environment(self):
        job = self.submit("--job-name=hpcrest-hello", "--output=hpc-rest-%j.out", "--cpus-per-task=2",
                          "--partition=whatever", "--account=x")
        self.assertGreaterEqual(int(job), rest_local.FIRST_ID)       # beyond SLURM's default job ids
        state = self.finished(job)
        self.assertEqual((state["state"], state["exit_code"], state["partition"]), ("COMPLETED", "0:0", "local"))
        out = (self.work / f"hpc-rest-{job}.out").read_text()
        self.assertEqual(out.strip(), f"job {job} task none cpus 2 mem 100m scheduler local")

    def test_failure_and_timeout(self):
        failed = self.submit(script="#!/bin/bash\nexit 3\n")
        timed = self.submit("--time=0:02", script="#!/bin/bash\nsleep 30\n")
        self.assertEqual(self.finished(failed)["state"], "FAILED")
        self.assertEqual(self.finished(timed)["state"], "TIMEOUT")

    def test_array_throttle_and_fifo(self):
        os.environ["SLEEP"] = "1"
        self.addCleanup(os.environ.pop, "SLEEP", None)
        array = self.submit("--array=0-3%1", "--output=a-%A_%a.out")
        later = self.submit("--cpus-per-task=2")        # needs both CPUs: waits for the array to finish
        time.sleep(0.5)
        states = self.runner.query_states([later], {array: 4})
        tasks = states[array]["tasks"]
        self.assertEqual([tasks[i]["state"] for i in range(4)], ["RUNNING", "PENDING", "PENDING", "PENDING"])
        self.assertEqual(tasks[1]["reason"], "JobArrayTaskLimit")
        self.assertEqual((states[later]["state"], states[later]["reason"]), ("PENDING", "Resources"))
        wait_for(lambda: self.runner.query_states([], {array: 4})[array]["terminal"], timeout=30)
        self.assertEqual(self.runner.query_states([], {array: 4})[array]["state"], "COMPLETED")
        self.assertEqual(self.finished(later)["state"], "COMPLETED")
        self.assertIn("task 3", (self.work / f"a-{array}_3.out").read_text())

    def test_cancel(self):
        script = "#!/bin/bash\ntrap 'echo got TERM; exit 3' TERM\nsleep 60 & wait\n"
        running = self.submit("--output=c-%j.out", script=script)
        wait_for(lambda: self.state(running)["state"] == "RUNNING")
        time.sleep(0.3)                                  # its trap is set
        waiting = self.submit("--cpus-per-task=2")
        self.assertEqual(self.runner.run(["scancel", running]).returncode, 0)
        self.assertEqual(self.runner.run(["scancel", waiting]).returncode, 0)
        self.assertEqual(self.finished(running)["state"], "CANCELLED")
        self.assertEqual(self.state(waiting)["state"], "CANCELLED")
        self.assertEqual((self.work / f"c-{running}.out").read_text().strip(), "got TERM")   # TERM, as under SLURM
        self.assertEqual(self.runner.run(["scancel", "1"]).returncode, 1)

    def test_running_jobs_outlive_the_server(self):
        os.environ["SLEEP"] = "2"
        self.addCleanup(os.environ.pop, "SLEEP", None)
        job = self.submit()
        wait_for(lambda: self.state(job)["state"] == "RUNNING")
        self.runner.stop()
        later = self.make()                               # the server restarts
        self.assertEqual(self.state(job, later)["state"], "RUNNING")
        self.assertEqual(self.finished(job, later)["state"], "COMPLETED")
        self.assertGreater(int(self.submit(runner=later)), int(job))   # ids carry on

    def test_lost_job(self):
        job = self.submit(script="#!/bin/bash\nsleep 60\n")
        wait_for(lambda: self.state(job)["state"] == "RUNNING")
        self.runner.stop()
        with self.runner.lock:
            t = self.runner.jobs[job]["tasks"]["0"]
            os.killpg(t["pid"], 9)                        # the wrapper and job die without a word
        self.runner.procs[(job, "0")].wait()
        later = self.make()
        state = wait_for(lambda: self.state(job, later) if self.state(job, later)["terminal"] else None)
        self.assertEqual(state["state"], "FAILED")
        self.assertIn("without recording", state["reason"])

    def test_limits_refusal(self):
        strict = self.make(start=False, enforce_limits=True)
        res = self.sbatch(runner=strict)
        self.assertEqual(res.returncode, 1)
        self.assertIn("can't hold the job to its CPU and memory limits", res.stderr)
        self.assertIn('"enforce_limits" in the scheduler config to false', res.stderr)

    def test_memory_only_enforcement(self):
        class Limits:
            def __init__(self, memory):
                self.memory = memory
            def limits(self):
                return {"enforced": False, "cpu": False, "memory": self.memory, "reason": "no cpu controller"}
            def resolve(self):
                return "none", "test"
        memory_only = self.make(start=False, enforce_limits="memory")
        memory_only.sandbox = Limits(True)
        self.assertEqual(self.sbatch("--test-only", runner=memory_only).returncode, 0)
        memory_only.sandbox = Limits(False)
        self.assertIn("its memory limit", self.sbatch(runner=memory_only).stderr)
        strict = self.make(start=False, enforce_limits=True)
        strict.sandbox = Limits(True)
        self.assertIn('"memory" enforces only memory', self.sbatch(runner=strict).stderr)
        with self.assertRaises(ValueError):
            rest_local.check_scheduler_section({"type": "local", "enforce_limits": "cpu"})

    def test_podman_storage_reaches_the_job(self):
        # the sandbox moved podman's storage (NFS home): the wrapper's `podman rm` must look in the same place
        class Podman:
            def limits(self):
                return {"enforced": True, "cpu": True, "memory": True}
            def resolve(self):
                return "podman", shutil.which("true")
            def podman_storage_conf(self):
                return "/var/tmp/someone/hpclib-podman/storage.conf"
        self.runner.sandbox = Podman()
        script = '#!/bin/bash\necho "$HPC_JOB_PODMAN $CONTAINERS_STORAGE_CONF"\n'
        job = self.submit("--output=out-%j.txt", script=script)
        self.assertEqual(self.finished(job)["state"], "COMPLETED")
        self.assertEqual((self.work / f"out-{job}.txt").read_text().strip(),
                         f"{shutil.which('true')} /var/tmp/someone/hpclib-podman/storage.conf")

    def test_test_only_and_sinfo(self):
        res = self.sbatch("--test-only", "--partition=gpu")
        self.assertEqual(res.returncode, 0)
        self.assertIn("would run with 1 CPUs, 100 MB", res.stderr)
        self.assertIn("ignored here: --partition=gpu", res.stderr)
        self.assertEqual(self.runner.run(["sinfo", "-h", "-o", "%P"]).stdout.strip(),
                         "local*|up|7-00:00:00|1|2|1024|(null)")
        self.assertEqual(self.runner.run(["squeue"]).returncode, 1)
        info = rest_jobs.ClusterInfo(self.runner).snapshot()
        self.assertEqual([p["name"] for p in info["partitions"]], ["local"])

    def test_config_validation(self):
        self.assertEqual(rest_local.check_scheduler_section(None), {"type": "slurm"})
        for bad in ({"type": "pbs"}, {"type": "local", "cpus": 0}, {"type": "local", "memory": "lots"},
                    {"type": "local", "max_time": "forever"}, {"type": "local", "enforce_limits": "yes"},
                    {"type": "local", "gpus": 1}, []):
            with self.assertRaises(ValueError, msg=bad):
                rest_local.check_scheduler_section(bad)


class TestLocalScheduledJobs(JobServerTestCase):
    """Template jobs through the REST API, run by the local scheduler instead of SLURM."""

    def use_local(self, **config):
        config = dict({"type": "local", "cpus": 2, "memory": "4G", "enforce_limits": False}, **config)
        runner = rest_local.LocalRunner(config, str(self.data / "rest"), UNSANDBOXED, poll=0.1)
        self.addCleanup(runner.stop)
        self.jobs.runner = runner
        self.jobs.cluster = rest_jobs.ClusterInfo(runner)
        return runner

    def test_submit_wait_and_read(self):
        self.use_local()
        dry = self.llm.submit_job("hello", {"message": "hi"}, dry_run=True)
        self.assertTrue(dry["accepted"], dry)
        self.assertIn("local scheduler", dry["slurm_message"])
        out = self.llm.submit_job("hello", {"message": "from the local scheduler"}, workdir=str(self.llm_root))
        job = wait_for(lambda: (lambda j: j if j["terminal"] else None)(self.llm.job_status(out["job_id"])))
        self.assertEqual(job["state"], "COMPLETED")
        self.assertIn("from the local scheduler", Path(job["output"]).read_text())
        info = self.llm.cluster()
        self.assertEqual((info["scheduler"]["type"], info["scheduler"]["cpus"]), ("local", 2))

    def test_refused_when_limits_cant_be_enforced(self):
        self.use_local(enforce_limits=True)
        payload = self.expect_error(502, self.llm.submit_job, "hello", {"message": "x"})
        self.assertIn("local scheduler rejected the job", payload["error"])
        self.assertIn("CPU and memory limits", payload["stderr"])


class TestServerConfig(unittest.TestCase):

    def test_build_jobs_picks_the_runner(self):
        import rest_server
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        os.environ["HPCTUNNELS_DATA_DIR"] = str(tmp)
        self.addCleanup(os.environ.pop, "HPCTUNNELS_DATA_DIR", None)
        base = {"templates_dir": str(tmp / "t"), "jobs_db": str(tmp / "jobs.sqlite"), "proposals_dir": str(tmp / "p")}
        jobs = rest_server.build_jobs(dict(base, scheduler={"type": "local", "enforce_limits": False}), 10)
        self.addCleanup(jobs.runner.stop)
        self.assertIsInstance(jobs.runner, rest_local.LocalRunner)
        self.assertIs(type(rest_server.build_jobs(base, 10).runner), rest_jobs.SlurmRunner)
        path = tmp / "config.json"
        path.write_text(json.dumps({"scheduler": {"type": "local", "cpus": -1}}))
        with self.assertRaises(ValueError):
            rest_server.load_config(str(path))


if __name__ == "__main__":
    unittest.main()
