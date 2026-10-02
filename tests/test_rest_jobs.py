"""Tests for template jobs, limits, scoped tokens, cluster info, the audit
log, bounded file reads, the REST client, and the MCP server.

Run with:  python -m unittest tests.test_rest_jobs -v
(from the repo root; uses a fake SLURM, so no cluster needed)
"""
import importlib.util
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import http.server
import unittest
from pathlib import Path
from unittest import mock

SERVERS = Path(__file__).resolve().parents[1] / "hpclib" / "servers"
TEMPLATES = Path(__file__).resolve().parents[1] / "hpclib" / "tunnels" / "rest" / "templates"
sys.path.insert(0, str(SERVERS))
import rest_jobs  # noqa: E402
from rest_client import RESTClient, RESTClientError  # noqa: E402
from rest_server import AuditLog, HPCRESTHandler, PathWhitelist, RESTServer, TokenAuth  # noqa: E402

OWNER = "owner-token"

# One script plays sbatch/squeue/sacct/scancel/sinfo/sacctmgr, keeping
# job state as JSON files in $FAKE_SLURM_DIR.
FAKE_SLURM = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    state_dir = os.environ["FAKE_SLURM_DIR"]
    name = os.path.basename(sys.argv[0])
    args = sys.argv[1:]
    def jobs():
        out = []
        for f in sorted(os.listdir(state_dir)):
            if f.endswith(".json"):
                out.append(json.load(open(os.path.join(state_dir, f))))
        return out
    def save(job):
        json.dump(job, open(os.path.join(state_dir, job["id"] + ".json"), "w"))
    if name == "sbatch":
        if os.environ.get("FAKE_SBATCH_FAIL"):
            print("sbatch: error: Batch job submission failed", file=sys.stderr); sys.exit(1)
        if "--test-only" in args:
            print("sbatch: Job 999 to start at 2026-10-01T12:00:00 using 1 processors on nodes n1 in partition short",
                  file=sys.stderr)
            sys.exit(0)
        counter = os.path.join(state_dir, "counter")
        n = int(open(counter).read()) + 1 if os.path.exists(counter) else 1000
        open(counter, "w").write(str(n))
        job_name = [a.split("=", 1)[1] for a in args if a.startswith("--job-name=")][0]
        array = [a.split("=", 1)[1] for a in args if a.startswith("--array=")]
        job = {"id": str(n), "name": job_name, "args": args, "script": sys.stdin.read(), "state": "PENDING",
               "cwd": os.getcwd(), "token_in_env": any(k.startswith("HPC_REST_TOKEN") for k in os.environ)}
        if array:
            last = int(array[0].split("%")[0].split("-")[1])
            job["tasks"] = ["PENDING"] * (last + 1)
        save(job)
        print(n)
    elif name == "squeue":
        def line(job_id, job_name, state):
            reason = "(Priority)" if state == "PENDING" else "n1"
            print("|".join([job_id, job_name, state, "0:05", "15:00", reason, "short", "n1"]))
        for j in jobs():
            if "tasks" in j:
                for k, state in enumerate(j["tasks"]):
                    if state in ("PENDING", "RUNNING"):
                        line(f"{j['id']}_{k}", j["name"], state)
            elif j["state"] in ("PENDING", "RUNNING"):
                line(j["id"], j["name"], j["state"])
    elif name == "sacct":
        wanted = args[args.index("-j") + 1].split(",")
        def line(job_id, job_name, state):
            code = "0:0" if state == "COMPLETED" else "1:0"
            print("|".join([job_id, job_name, state, code, "00:00:05", "2026-10-01T12:00:00",
                            "2026-10-01T12:00:05", "short", "n1", "None"]))
        for j in jobs():
            if j["id"] not in wanted:
                continue
            if "tasks" in j:
                for k, state in enumerate(j["tasks"]):
                    if state not in ("PENDING", "RUNNING"):
                        line(f"{j['id']}_{k}", j["name"], state)
            elif j["state"] not in ("PENDING", "RUNNING"):
                line(j["id"], j["name"], j["state"])
    elif name == "scancel":
        for j in jobs():
            if j["id"] == args[0]:
                j["state"] = "CANCELLED by 1234"
                if "tasks" in j:
                    j["tasks"] = ["CANCELLED" if t in ("PENDING", "RUNNING") else t for t in j["tasks"]]
                save(j)
    elif name == "sinfo":
        if "--version" in args:
            print("slurm 23.02.7")
        else:
            print("short*|up|4:00:00|10|48|192000|(null)")
            print("gpu|up|2-00:00:00|2|32|256000|gpu:a100:4")
            print("gpu|up|2-00:00:00|1|64|512000|gpu:h100:8")
    elif name == "sacctmgr":
        print("myacct|short|normal")
""")


FAKE_MODULE = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys
    with open(os.path.join(os.environ["FAKE_SLURM_DIR"], "module-calls"), "a") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
    args = [a for a in sys.argv[1:] if a != "-t"]
    terse = "-t" in sys.argv[1:]
    sub, query = args[0], (args[1] if len(args) > 1 else "")
    if sub == "avail":
        print("/sw/modules/Core:")
        for m in ["GCC/", "GCC/12.2.0", "ORCA/", "ORCA/5.0.4(default)", "ORCA/6.0.0", "OpenMPI/4.1.4"]:
            if query.lower() in m.lower():
                print(m)
        print("/sw/modules/tools:")
        if query.lower() in "python/3.11":
            print("python/3.11")
    elif sub == "spider" and terse:
        for m in ["GCC/12.2.0", "ORCA/5.0.4", "ORCA/6.0.0", "OpenMPI/4.1.4", "python/3.11"]:
            if query.lower() in m.lower():
                print(m)
    else:
        print("  ORCA: " + query)
        print("    You will need to load all module(s) on any one of the lines below before the")
        print('    "' + query + '" module is available to load.')
        print("      GCC/12.2.0  OpenMPI/4.1.4")
""")

# Stands in for ORCA when running a rendered orca template script.
FAKE_ORCA = textwrap.dedent("""\
    #!/usr/bin/env bash
    base="${1%.inp}"
    echo "running $1 in $PWD"
    if [ -n "${FAKE_ORCA_FAIL:-}" ]; then echo "SCF NOT CONVERGED"; exit 1; fi
    echo "final geometry" > "$base.xyz"
    echo "orbitals" > "$base.gbw"
    echo "scratch" > "$base.tmp"
    echo "****ORCA TERMINATED NORMALLY****"
""")


def set_task_state(slurm_dir, job_id, task, state):
    path = Path(slurm_dir) / f"{job_id}.json"
    job = json.loads(path.read_text())
    job["tasks"][task] = state
    path.write_text(json.dumps(job))


def set_state(slurm_dir, job_id, state):
    path = Path(slurm_dir) / f"{job_id}.json"
    job = json.loads(path.read_text())
    job["state"] = state
    path.write_text(json.dumps(job))


def fake_job(slurm_dir, job_id):
    return json.loads((Path(slurm_dir) / f"{job_id}.json").read_text())


class JobServerTestCase(unittest.TestCase):

    limits = {"max_concurrent_jobs": 2, "max_time": "02:00:00", "max_gpus": 1, "partitions": None}
    disable_file_changes = False

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "allowed"
        self.llm_root = self.root / "llm"
        self.llm_root.mkdir(parents=True)
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.data = self.root / "tunnel-data"   # deliberately inside the allowed dir
        (self.data / "rest").mkdir(parents=True)
        self.slurm = self.tmp / "slurm"
        self.slurm.mkdir()

        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for cmd in ("sbatch", "squeue", "sacct", "scancel", "scontrol", "sinfo", "sacctmgr"):
            (bin_dir / cmd).write_text(FAKE_SLURM)
            (bin_dir / cmd).chmod(0o755)
        for cmd, text in (("module", FAKE_MODULE), ("orca", FAKE_ORCA)):
            (bin_dir / cmd).write_text(text)
            (bin_dir / cmd).chmod(0o755)
        self.bin_dir = bin_dir
        env = mock.patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_SLURM_DIR": str(self.slurm),
            "HPCTUNNELS_DATA_DIR": str(self.data),
            "HPC_REST_TOKEN_SHOULD_NOT_LEAK": "x",
        })
        env.start()
        self.addCleanup(env.stop)

        self.templates = self.data / "rest" / "templates"
        shutil.copytree(TEMPLATES, self.templates)
        self.tokens_file = self.data / "rest" / "tokens.json"
        self.llm_token = TokenAuth.add_token(str(self.tokens_file), "llm", ["read", "submit"], [str(self.llm_root)])
        self.other_token = TokenAuth.add_token(str(self.tokens_file), "other", ["read", "submit"],
                                               [str(self.llm_root)])
        self.reader_token = TokenAuth.add_token(str(self.tokens_file), "reader", ["read"], [str(self.llm_root)])
        self.builder_token = TokenAuth.add_token(str(self.tokens_file), "builder",
                                                 ["read", "submit", "propose", "files:write"], [str(self.llm_root)])

        runner = rest_jobs.SlurmRunner(timeout=10, user="tester")
        template_store = rest_jobs.TemplateStore(str(self.templates))
        self.proposals_dir = self.data / "rest" / "proposals"
        self.jobs = rest_jobs.JobManager(
            templates=template_store,
            proposals=rest_jobs.ProposalStore(str(self.proposals_dir), template_store),
            modules=rest_jobs.ModuleSystem(runner, command=[str(bin_dir / "module")]),
            registry=rest_jobs.JobRegistry(str(self.data / "rest" / "jobs.sqlite")),
            limits=rest_jobs.ResourceLimits(**self.limits),
            runner=runner, cluster_notes="load modules with `module load`", poll_interval=0.05,
        )
        self.audit_path = self.data / "rest" / "audit.log"
        whitelist = PathWhitelist([str(self.root)], base_dir=self.root, deny=[str(self.data)])
        self.server = RESTServer(("127.0.0.1", 0), HPCRESTHandler,
                                 auth=TokenAuth(OWNER, tokens_file=str(self.tokens_file)),
                                 whitelist=whitelist, command_timeout=10, max_upload=1 << 20,
                                 disable_file_changes=self.disable_file_changes,
                                 jobs=self.jobs, audit=AuditLog(str(self.audit_path)))
        self.server.RequestHandlerClass.log_message = lambda *a: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.owner = RESTClient(self.url, token=OWNER)
        self.llm = RESTClient(self.url, token=self.llm_token)

    def expect_error(self, status, fn, *args, **kwargs):
        with self.assertRaises(RESTClientError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.status, status, ctx.exception.payload)
        return ctx.exception.payload


class TestTemplates(JobServerTestCase):

    def test_list_templates_with_schemas(self):
        out = self.llm.templates()
        by_name = {t["name"]: t for t in out["templates"]}
        self.assertEqual(sorted(by_name), ["hello", "orca", "python_script"])
        self.assertEqual([g["name"] for g in out["guides"]], ["writing_templates"])
        schema = by_name["python_script"]["parameters"]
        self.assertEqual(schema["required"], ["script"])
        self.assertEqual(schema["properties"]["cpus"]["maximum"], 16)
        self.assertFalse(schema["additionalProperties"])

    def test_broken_templates_reported_not_loaded(self):
        bad = self.templates / "bad"
        bad.mkdir()
        (bad / "template.json").write_text(json.dumps({"description": "x"}))
        (bad / "script.sh").write_text("#!/bin/bash\n#SBATCH --gres=gpu:8\necho hi\n")
        out = self.owner.templates()
        self.assertIn("#SBATCH", out["errors"]["bad"])
        self.assertNotIn("bad", [t["name"] for t in out["templates"]])
        self.expect_error(500, self.owner.submit_job, "bad")

    def test_unknown_template(self):
        payload = self.expect_error(404, self.llm.submit_job, "nope")
        self.assertIn("hello", payload["available"])


class TestSubmit(JobServerTestCase):

    def test_dry_run(self):
        out = self.llm.submit_job("hello", {"message": "hi"}, dry_run=True)
        self.assertTrue(out["dry_run"])
        self.assertTrue(out["accepted"])
        self.assertIn("to start at", out["slurm_message"])
        self.assertIn("--time=00:15:00", out["sbatch_args"])
        self.assertIn("export HPC_PARAM_MESSAGE=hi", out["script"])
        self.assertEqual(self.llm.jobs()["jobs"], [])

    def test_submit_records_job_and_passes_resources(self):
        out = self.llm.submit_job("hello", {"message": "hi", "sleep_seconds": 3}, resources={"time": "00:30:00"})
        self.assertFalse(out["duplicate"])
        job = fake_job(self.slurm, out["job_id"])
        self.assertIn("--time=00:30:00", job["args"])
        self.assertIn("--mem=1G", job["args"])
        self.assertIn("--comment=hpclib-rest:llm:hello", job["args"])
        self.assertIn(f"--chdir={self.llm_root}", job["args"])
        self.assertEqual(job["cwd"], str(self.llm_root))
        self.assertFalse(job["token_in_env"])
        self.assertEqual(out["output"], str(self.llm_root / f"hpc-rest-{out['job_id']}.out"))
        self.assertEqual(out["state"], "PENDING")
        self.assertEqual(out["token"], "llm")

    def test_parameters_cannot_inject_shell(self):
        payload = "$(touch pwned); `touch pwned2`; ' \" \n echo nope"
        out = self.llm.submit_job("hello", {"message": payload})
        script = fake_job(self.slurm, out["job_id"])["script"]
        res = subprocess.run(["bash", "-c", script], cwd=self.tmp, capture_output=True, text=True,
                             env=dict(os.environ, SLURM_JOB_ID="1"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(payload, res.stdout)
        self.assertFalse((self.tmp / "pwned").exists() or (self.tmp / "pwned2").exists())

    def test_parameter_validation(self):
        p = self.expect_error(422, self.llm.submit_job, "hello", {"sleep_seconds": 9999, "colour": "red"})
        self.assertTrue(any("<= 600" in v for v in p["violations"]))
        self.assertTrue(any("colour" in v for v in p["violations"]))
        self.expect_error(422, self.llm.submit_job, "hello", {"sleep_seconds": "5"})
        self.expect_error(422, self.llm.submit_job, "hello", {"message": "x" * 201})
        self.expect_error(422, self.llm.submit_job, "python_script", {})

    def test_path_parameters_are_whitelisted(self):
        script = self.llm_root / "run.py"
        script.write_text("print('hi')\n")
        out = self.llm.submit_job("python_script", {"script": "run.py", "cpus": 4}, dry_run=True)
        self.assertEqual(out["params"]["script"], str(script))
        self.assertEqual(out["workdir"], str(self.llm_root))
        self.assertIn("--cpus-per-task=4", out["sbatch_args"])
        (self.outside / "evil.py").write_text("")
        p = self.expect_error(422, self.llm.submit_job, "python_script", {"script": str(self.outside / "evil.py")})
        self.assertTrue(any("outside the allowed" in v for v in p["violations"]))
        # the owner may use the whole server whitelist, but not a missing file
        self.expect_error(422, self.owner.submit_job, "python_script", {"script": "missing.py"})

    def test_limits(self):
        p = self.expect_error(422, self.llm.submit_job, "hello", resources={"time": "05:00:00"})
        self.assertTrue(any("exceeds the limit" in v for v in p["violations"]))
        p = self.expect_error(422, self.llm.submit_job, "hello", resources={"partition": "gpu"})
        self.assertTrue(any("does not allow overriding" in v for v in p["violations"]))
        self.expect_error(422, self.llm.submit_job, "hello", resources={"time": "1;rm -rf /"})
        self.expect_error(400, self.llm.submit_job, "hello", resources="lots")

    def test_workdir(self):
        (self.llm_root / "sub").mkdir()
        out = self.llm.submit_job("hello", workdir="sub", dry_run=True)
        self.assertEqual(out["workdir"], str(self.llm_root / "sub"))
        self.expect_error(403, self.llm.submit_job, "hello", workdir=str(self.outside))
        self.expect_error(403, self.llm.submit_job, "hello", workdir=str(self.root))  # outside llm's dirs
        self.expect_error(422, self.llm.submit_job, "hello", workdir="missing")

    def test_unknown_fields(self):
        self.expect_error(400, self.llm.request, "POST", "/jobs", body={"template": "hello", "sbatch_args": ["-x"]})

    def test_idempotency(self):
        first = self.llm.submit_job("hello", idempotency_key="run-1")
        again = self.llm.submit_job("hello", idempotency_key="run-1")
        self.assertEqual(again["job_id"], first["job_id"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(list(self.slurm.glob("*.json"))), 1)
        # keys are per token
        other = RESTClient(self.url, token=self.other_token).submit_job("hello", idempotency_key="run-1")
        self.assertNotEqual(other["job_id"], first["job_id"])

    def test_concurrency_limit(self):
        a = self.llm.submit_job("hello")
        self.llm.submit_job("hello")
        p = self.expect_error(429, self.llm.submit_job, "hello")
        self.assertEqual(len(p["active_jobs"]), 2)
        set_state(self.slurm, a["job_id"], "COMPLETED")
        self.llm.submit_job("hello")

    def test_sbatch_failure(self):
        with mock.patch.dict(os.environ, {"FAKE_SBATCH_FAIL": "1"}):
            p = self.expect_error(502, self.llm.submit_job, "hello")
        self.assertIn("submission failed", p["stderr"])
        self.assertEqual(self.llm.jobs()["jobs"], [])


class TestJobTracking(JobServerTestCase):

    def test_status_follows_slurm(self):
        job_id = self.llm.submit_job("hello")["job_id"]
        self.assertEqual(self.llm.job_status(job_id)["reason"], "(Priority)")
        set_state(self.slurm, job_id, "RUNNING")
        self.assertEqual(self.llm.job_status(job_id)["state"], "RUNNING")
        set_state(self.slurm, job_id, "FAILED")
        status = self.llm.job_status(job_id)
        self.assertEqual((status["state"], status["terminal"], status["exit_code"]), ("FAILED", True, "1:0"))
        self.assertEqual(self.llm.jobs(active_only=True)["jobs"], [])

    def test_wait(self):
        job_id = self.llm.submit_job("hello")["job_id"]
        out = self.llm.wait_job(job_id, timeout=0)
        self.assertTrue(out["timed_out"])
        threading.Timer(0.2, set_state, (self.slurm, job_id, "COMPLETED")).start()
        out = self.llm.wait_job(job_id, timeout=5)
        self.assertEqual((out["state"], out["timed_out"]), ("COMPLETED", False))

    def test_cancel(self):
        job_id = self.llm.submit_job("hello")["job_id"]
        out = self.llm.cancel_job(job_id)
        self.assertTrue(out["cancel_requested"])
        self.assertEqual(out["state"], "CANCELLED")

    def test_tokens_only_see_their_own_jobs(self):
        job_id = self.llm.submit_job("hello")["job_id"]
        other = RESTClient(self.url, token=self.other_token)
        self.expect_error(404, other.job_status, job_id)
        self.expect_error(404, other.cancel_job, job_id)
        self.assertEqual(other.jobs()["jobs"], [])
        self.assertEqual([j["job_id"] for j in self.owner.jobs()["jobs"]], [job_id])
        self.assertEqual(self.owner.job_status(job_id)["token"], "llm")
        self.expect_error(400, self.owner.job_status, "12; rm")


class TestScopes(JobServerTestCase):

    def test_health_reports_identity(self):
        h = self.llm.health()
        self.assertEqual(h["token"]["name"], "llm")
        self.assertEqual(h["allowed_dirs"], [str(self.llm_root)])
        self.assertEqual(self.owner.health()["token"]["scopes"], ["*"])

    def test_scoped_tokens_cannot_use_raw_slurm_or_write(self):
        p = self.expect_error(403, self.llm.request, "POST", "/slurm/sbatch", body={"input": "#!/bin/bash\n"})
        self.assertIn("slurm", p["error"])
        self.expect_error(403, self.llm.request, "GET", "/slurm/squeue")
        self.expect_error(403, self.llm.upload, "x.txt", "x")
        self.expect_error(403, self.llm.mkdir, "d")
        reader = RESTClient(self.url, token=self.reader_token)
        self.expect_error(403, reader.submit_job, "hello")
        reader.templates()
        self.assertEqual(self.owner.request("GET", "/slurm/squeue")["returncode"], 0)

    def test_token_directories(self):
        (self.root / "private.txt").write_text("secret")
        self.expect_error(403, self.llm.read_file, str(self.root / "private.txt"))
        self.expect_error(403, self.llm.list_files, "..")
        self.assertEqual(self.owner.read_file(str(self.root / "private.txt"))["text"], "secret")

    def test_server_data_is_protected_even_for_the_owner(self):
        for path in (self.tokens_file, self.audit_path, self.templates / "hello" / "script.sh"):
            self.expect_error(403, self.owner.read_file, str(path))
        self.expect_error(403, self.owner.upload, str(self.templates / "evil" / "script.sh"), "x", parents=True)
        self.expect_error(422, self.owner.submit_job, "python_script",
                          {"script": str(self.templates / "hello" / "script.sh")})

    def test_revocation_applies_immediately(self):
        self.llm.health()
        TokenAuth.revoke_token(str(self.tokens_file), "llm")
        os.utime(self.tokens_file, ns=(1, 1))  # force a visible mtime change
        self.expect_error(401, self.llm.health)
        RESTClient(self.url, token=self.other_token).health()

    def test_audit_log(self):
        job_id = self.llm.submit_job("hello")["job_id"]
        self.expect_error(401, RESTClient(self.url, token="wrong").health)
        # the entry is written just after the response goes out, so give it a moment
        for _ in range(100):
            entries = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
            if entries and entries[-1]["status"] == 401:
                break
            time.sleep(0.02)
        submit = [e for e in entries if e["verb"] == "POST" and e["path"] == "/jobs"][-1]
        self.assertEqual((submit["token"], submit["status"]), ("llm", 201))
        self.assertEqual(submit["detail"]["job_id"], job_id)
        self.assertEqual((entries[-1]["token"], entries[-1]["status"]), (None, 401))
        self.assertNotIn(OWNER, self.audit_path.read_text())
        self.assertNotIn(self.llm_token, self.audit_path.read_text())


class TestCluster(JobServerTestCase):

    def test_cluster_info(self):
        info = self.llm.cluster()
        parts = {p["name"]: p for p in info["partitions"]}
        self.assertTrue(parts["short"]["default"])
        self.assertEqual(parts["gpu"]["nodes"], 3)
        self.assertEqual(parts["gpu"]["node_types"][1]["gres"], "gpu:h100:8")
        self.assertIsNone(parts["short"]["node_types"][0]["gres"])
        self.assertEqual(info["slurm_version"], "slurm 23.02.7")
        self.assertEqual(info["associations"], [{"account": "myacct", "partition": "short", "qos": "normal"}])
        self.assertEqual(info["limits"]["max_time"], "02:00:00")
        self.assertEqual(info["allowed_dirs"], [str(self.llm_root)])
        self.assertEqual(sorted(t["name"] for t in info["templates"]), ["hello", "orca", "python_script"])
        self.assertEqual([g["name"] for g in info["guides"]], ["writing_templates"])
        self.assertIn("module load", info["notes"])


class TestBoundedReads(JobServerTestCase):

    def test_read_and_tail(self):
        log = self.llm_root / "out.log"
        log.write_text("".join(f"line {i}\n" for i in range(1000)))
        out = self.llm.read_file("out.log", offset=7, length=6)
        self.assertEqual((out["text"], out["eof"]), ("line 1", False))
        out = self.llm.tail_file("out.log", lines=3)
        self.assertEqual(out["lines"], ["line 997", "line 998", "line 999"])
        self.assertTrue(out["truncated"])
        (self.llm_root / "blob").write_bytes(b"\x00\x01\x02")
        self.assertTrue(self.llm.read_file("blob")["binary"])
        self.expect_error(400, self.llm.read_file, "out.log", length=10 << 20)


class TestUnitParsing(unittest.TestCase):

    def test_times_and_sizes(self):
        self.assertEqual(rest_jobs.parse_slurm_time("90"), 90)
        self.assertEqual(rest_jobs.parse_slurm_time("01:30:00"), 90)
        self.assertEqual(rest_jobs.parse_slurm_time("1-00"), 1440)
        self.assertEqual(rest_jobs.parse_slurm_time("2-01:30"), 2970)
        self.assertEqual(rest_jobs.parse_mem("2G"), 2048)
        self.assertEqual(rest_jobs.parse_mem("512"), 512)
        self.assertEqual(rest_jobs.parse_gres_gpus("gpu:a100:2,tmpfs:10G"), 2)
        self.assertEqual(rest_jobs.parse_gres_gpus("gpu"), 1)
        with self.assertRaises(ValueError):
            rest_jobs.parse_slurm_time("soon")
        with self.assertRaises(ValueError):
            rest_jobs.ResourceLimits(max_walltime="1:00")

    def test_limit_violations(self):
        lim = rest_jobs.ResourceLimits(partitions=["short"], max_cpus=8, max_gpus=1)
        problems = lim.violations({"partition": "gpu", "cpus_per_task": "4", "ntasks": "4", "gres": "gpu:2"})
        self.assertEqual(len(problems), 4, problems)  # partition, missing time, cpus, gpus


class TestTokenManagement(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.tokens = str(self.tmp / "rest" / "tokens.json")

    def test_mint_rules(self):
        with self.assertRaises(ValueError):
            TokenAuth.add_token(self.tokens, "llm", ["read"])          # no directories
        with self.assertRaises(ValueError):
            TokenAuth.add_token(self.tokens, "llm", ["root"], ["/tmp"])
        with self.assertRaises(ValueError):
            TokenAuth.add_token(self.tokens, "owner", ["read"], ["/tmp"])
        token = TokenAuth.add_token(self.tokens, "llm", ["read"], ["/tmp"])
        with self.assertRaises(ValueError):
            TokenAuth.add_token(self.tokens, "llm", ["read"], ["/tmp"])
        self.assertEqual(os.stat(self.tokens).st_mode & 0o777, 0o600)
        self.assertNotIn(token, Path(self.tokens).read_text())
        auth = TokenAuth("owner-secret", tokens_file=self.tokens)
        self.assertEqual(auth.identify(f"Bearer {token}").name, "llm")

    def test_hashed_owner_token(self):
        token_file = self.tmp / "rest_token"
        token_file.write_text("owner-secret\n")
        token_file.chmod(0o600)
        self.assertTrue(TokenAuth.hash_token_file(str(token_file)))
        self.assertFalse(TokenAuth.hash_token_file(str(token_file)))
        self.assertNotIn("owner-secret", token_file.read_text())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(TokenAuth.TOKEN_ENV_VAR, None)
            auth = TokenAuth.load(str(token_file))
        self.assertEqual(auth.identify("Bearer owner-secret").name, "owner")
        self.assertIsNone(auth.identify("Bearer sha256:" + TokenAuth.hash("owner-secret")))


HAVE_MCP = importlib.util.find_spec("mcp") is not None


def field(obj, camel):
    """Read an MCP type's field under its mcp 2.x (snake_case) or 1.x (camelCase) name."""
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()
    return getattr(obj, snake) if snake in type(obj).model_fields else getattr(obj, camel)


def is_error(result):
    return bool(field(result, "isError"))


def tool_payload(result):
    text = result.content[0].text
    return json.loads(text[text.index("{"):]) if is_error(result) else json.loads(text)


@unittest.skipUnless(HAVE_MCP, "the MCP Python SDK is not installed (pip install mcp)")
class TestMCP(JobServerTestCase):
    """Drives rest_mcp.py over real stdio with the SDK's own client."""

    def session(self, body, *extra_args, token=None):
        import anyio
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVERS / "rest_mcp.py"), "--url", self.url, *extra_args],
            env=dict(os.environ, HPC_REST_TOKEN=token or self.llm_token),
        )

        async def go():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    return await body(session, init)
        return anyio.run(go)

    def test_handshake_and_tools(self):
        async def body(session, init):
            return init, (await session.list_tools()).tools
        init, tools = self.session(body)
        self.assertEqual(field(init, "serverInfo").name, "hpclib")
        self.assertIn("dry_run", init.instructions)
        by_name = {t.name: t for t in tools}
        self.assertEqual(sorted(by_name), sorted([
            "cluster_info", "list_templates", "submit_job", "list_jobs", "job_status", "wait_for_job",
            "cancel_job", "list_files", "read_file", "tail_file", "read_guide", "list_modules",
            "search_modules", "list_template_proposals", "propose_template"]))
        self.assertTrue(field(by_name["cluster_info"].annotations, "readOnlyHint"))
        self.assertTrue(field(by_name["cancel_job"].annotations, "destructiveHint"))
        schema = field(by_name["submit_job"], "inputSchema")
        self.assertEqual(schema["required"], ["template"])
        self.assertIn("idempotency_key", schema["properties"])
        self.assertEqual(field(by_name["wait_for_job"], "inputSchema")["properties"]["timeout_seconds"]["maximum"], 300)

    def test_job_flow(self):
        async def body(session, init):
            out = {}
            out["cluster"] = await session.call_tool("cluster_info", {})
            out["plan"] = await session.call_tool("submit_job", {"template": "hello", "params": {"message": "hi"},
                                                                 "dry_run": True})
            out["job"] = await session.call_tool("submit_job", {"template": "hello", "idempotency_key": "k1"})
            job_id = tool_payload(out["job"])["job_id"]
            out["status"] = await session.call_tool("job_status", {"job_id": job_id})
            out["again"] = await session.call_tool("submit_job", {"template": "hello", "idempotency_key": "k1"})
            out["too_long"] = await session.call_tool("submit_job", {"template": "hello",
                                                                     "resources": {"time": "09:00:00"}})
            out["bad_wait"] = await session.call_tool("wait_for_job", {"job_id": job_id, "timeout_seconds": 9999})
            out["wait"] = await session.call_tool("wait_for_job", {"job_id": job_id, "timeout_seconds": 0})
            out["outside"] = await session.call_tool("read_file", {"path": str(self.outside)})
            return out
        out = self.session(body)
        self.assertIn("partitions", tool_payload(out["cluster"]))
        self.assertTrue(tool_payload(out["plan"])["accepted"])
        job = tool_payload(out["job"])
        self.assertEqual(tool_payload(out["status"])["state"], "PENDING")
        self.assertTrue(tool_payload(out["again"])["duplicate"])
        self.assertEqual(len(list(self.slurm.glob("*.json"))), 1)
        self.assertTrue(is_error(out["too_long"]))
        self.assertEqual(tool_payload(out["too_long"])["status"], 422)
        self.assertTrue(is_error(out["bad_wait"]))  # schema bound enforced by the SDK
        self.assertTrue(tool_payload(out["wait"])["timed_out"])
        self.assertEqual(tool_payload(out["outside"])["status"], 403)
        self.assertEqual(job["token"], "llm")

    def test_file_writes_are_opt_in_and_still_scoped(self):
        async def body(session, init):
            names = [t.name for t in (await session.list_tools()).tools]
            return names, await session.call_tool("write_file", {"path": "x.txt", "content": "hi"})
        names, result = self.session(body, "--enable-file-writes")
        self.assertIn("write_file", names)
        self.assertTrue(is_error(result))
        self.assertEqual(tool_payload(result)["status"], 403)  # llm token lacks files:write
        self.assertFalse((self.llm_root / "x.txt").exists())

    def test_missing_token(self):
        env = {k: v for k, v in os.environ.items() if k != "HPC_REST_TOKEN"}
        res = subprocess.run([sys.executable, str(SERVERS / "rest_mcp.py"), "--url", self.url,
                              "--token-file", str(self.tmp / "missing")], env=env, capture_output=True,
                             text=True, stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(res.returncode, 1)
        self.assertIn("no token", res.stderr)


class TestClientErrors(unittest.TestCase):

    def test_unreachable(self):
        with self.assertRaises(RESTClientError) as ctx:
            RESTClient("http://127.0.0.1:9", token="x", timeout=2).health()
        self.assertIn("launch_tunnel", str(ctx.exception))

    def test_waiting_page(self):
        class Waiting(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html>waiting</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Waiting)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(RESTClientError) as ctx:
                RESTClient(f"http://127.0.0.1:{server.server_address[1]}", token="x").health()
            self.assertIn("still queued", str(ctx.exception))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
