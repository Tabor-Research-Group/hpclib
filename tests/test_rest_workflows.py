"""Tests for module discovery, template guides and proposals, job arrays
(including tasks read from a manifest), the bundled ORCA template's
script, registry migration, and local <-> cluster file sync.

Run with:  python -m unittest tests.test_rest_workflows -v
(from the repo root; uses the fake SLURM/module/orca from test_rest_jobs)
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_rest_jobs import (  # noqa: E402
    HAVE_MCP, JobServerTestCase, fake_job, set_task_state, tool_payload, is_error,
)
import rest_jobs  # noqa: E402
import rest_server  # noqa: E402
from rest_client import FileSync, RESTClient, RESTClientError  # noqa: E402


def make_scan(directory, shape=(2, 3), nprocs=4):
    """A directory shaped like Psience's ScanManager.generate output."""
    directory.mkdir(parents=True, exist_ok=True)
    steps = []
    for i in range(shape[0]):
        for j in range(shape[1]):
            fname = f"scan_{i:03d}_{j:03d}.inp"
            (directory / fname).write_text(f"! B97-3c Opt\n%pal nprocs {nprocs} end\n* xyz 0 1\nH 0 0 {i}\n*\n")
            steps.append({"index": [i, j], "values": [i * 0.5, j * 0.5], "file": fname})
    (directory / "scan_info.json").write_text(json.dumps(
        {"scan_id": None, "coord_labels": None, "shape": list(shape), "steps": steps}, indent=2))
    return steps


class WorkflowTestCase(JobServerTestCase):
    limits = {"max_concurrent_jobs": 4, "max_time": "04:00:00", "max_gpus": 0, "partitions": None}

    def setUp(self):
        super().setUp()
        self.builder = RESTClient(self.url, token=self.builder_token)


class TestModules(WorkflowTestCase):

    def test_avail(self):
        out = self.llm.modules("orca")
        self.assertEqual([m["name"] for m in out["modules"]], ["ORCA/5.0.4", "ORCA/6.0.0"])
        self.assertTrue(out["modules"][0]["default"])
        self.assertEqual(out["modules"][0]["location"], "/sw/modules/Core")
        everything = [m["name"] for m in self.llm.modules()["modules"]]
        self.assertIn("python/3.11", everything)
        self.assertNotIn("GCC/", everything)  # family headers are dropped

    def test_spider(self):
        listing = self.llm.modules("orca", spider=True)
        detail = self.llm.modules("ORCA/5.0.4", spider=True)
        self.assertIn("GCC/12.2.0  OpenMPI/4.1.4", detail["text"])
        self.assertEqual([m["name"] for m in listing["modules"]], ["ORCA/5.0.4", "ORCA/6.0.0"])
        calls = (self.slurm / "module-calls").read_text().splitlines()
        self.assertIn("spider ORCA/5.0.4", calls)  # detailed (not terse) for one name/version

    def test_cached(self):
        self.llm.modules("gcc")
        again = self.llm.modules("gcc")
        self.assertTrue(again["cached"])
        self.assertEqual(len((self.slurm / "module-calls").read_text().splitlines()), 1)

    def test_queries_are_names_only(self):
        for bad in ("orca; rm -rf ~", "$(id)", "-h", "a b"):
            self.expect_error(400, self.llm.modules, bad)

    def test_no_module_system(self):
        self.jobs.modules = rest_jobs.ModuleSystem(self.jobs.runner, command=[str(self.tmp / "missing")])
        self.expect_error(503, self.llm.modules, "orca")

    def test_parse_lmod_detailed_spider(self):
        # what Lmod 8 on entropy printed for `module -t spider orca` (one match: detailed layout)
        single = ("\n" + "-" * 152 + "\n  orca: orca/6.1.0\n" + "-" * 152 + "\n\n"
                  "    This module can be loaded directly: module load orca/6.1.0\n\n    Help:\n"
                  "      An ab initio, DFT and semiempirical SCF-MO package Note: Orca is\n")
        self.assertEqual([m["name"] for m in rest_jobs.ModuleSystem.parse_terse(single)], ["orca/6.1.0"])
        several = ("-" * 40 + "\n  gcc:\n" + "-" * 40 + "\n     Versions:\n        gcc/11.2.0\n"
                   "        gcc/12.2.0 (D)\n     Other possible modules matches:\n        gcc-native\n\n"
                   + "-" * 40 + "\n  For detailed information about a specific \"gcc\" package\n")
        self.assertEqual([(m["name"], m["default"]) for m in rest_jobs.ModuleSystem.parse_terse(several)],
                         [("gcc/11.2.0", False), ("gcc/12.2.0", True)])
        listed = "-" * 20 + "\n  python: python/3.10.4, python/3.11.5\n" + "-" * 20 + "\n"
        self.assertEqual([m["name"] for m in rest_jobs.ModuleSystem.parse_terse(listed)],
                         ["python/3.10.4", "python/3.11.5"])

    def test_spider_query_keeps_text(self):
        out = self.llm.modules("orca", spider=True)
        self.assertIn("text", out)

    def test_parse_terse_lmod_markers(self):
        text = "/opt/mods:\nfoo/\nfoo/1.0 (D)\nfoo/2.0 <aL>\ndot\nNo module(s) or extension(s) found!\n"
        names = [(m["name"], m["default"]) for m in rest_jobs.ModuleSystem.parse_terse(text)]
        self.assertEqual(names, [("foo/1.0", True), ("foo/2.0", False), ("dot", False)])


class TestGuides(WorkflowTestCase):

    def test_template_guide_and_examples(self):
        out = self.llm.template_guide("orca")
        self.assertTrue(out["is_template"])
        self.assertIn("ScanManager", out["guide"])
        self.assertEqual(out["examples"]["scan_from_manifest.json"]["template"], "orca")

    def test_guide_only_directory(self):
        out = self.llm.template_guide("writing_templates")
        self.assertFalse(out["is_template"])
        p = self.expect_error(422, self.llm.submit_job, "writing_templates")
        self.assertIn("planning guide", p["error"])

    def test_missing_and_invalid(self):
        self.expect_error(404, self.llm.template_guide, "hello")   # template without a guide
        self.expect_error(404, self.llm.template_guide, "nope")
        self.expect_error(400, self.llm.template_guide, "../rest")


class TestProposals(WorkflowTestCase):

    SPEC = {"description": "Run xtb on an xyz file", "parameters": {"xyz": {"type": "path", "kind": "file"}},
            "resources": {"time": "00:30:00", "mem": "2G"}, "modules": ["xtb/6.6.1"]}
    SCRIPT = "#!/bin/bash\nset -euo pipefail\nxtb \"$HPC_PARAM_XYZ\" --opt\n"

    def test_scope_required(self):
        p = self.expect_error(403, self.llm.propose_template, "xtb", self.SPEC, self.SCRIPT)
        self.assertIn("propose", p["error"])

    def test_propose_review_approve(self):
        out = self.builder.propose_template("xtb", self.SPEC, self.SCRIPT, guide="# xtb\n", rationale="for GFN2")
        self.assertEqual((out["token"], out["replaces_existing"]), ("builder", False))
        self.assertTrue((self.proposals_dir / "xtb" / "script.sh").exists())
        self.assertNotIn("xtb", [t["name"] for t in self.llm.templates()["templates"]])
        self.expect_error(404, self.builder.submit_job, "xtb")   # nothing runs before approval
        self.expect_error(409, self.builder.propose_template, "xtb", self.SPEC, self.SCRIPT)
        self.assertEqual([p["name"] for p in self.llm.proposals()["proposals"]], ["xtb"])
        # proposals are unreachable through the file routes, like the rest of the server's data
        self.expect_error(403, self.owner.read_file, str(self.proposals_dir / "xtb" / "script.sh"))

        with contextlib.redirect_stdout(io.StringIO()) as listed:
            rest_server.main(["--list-proposals"])
        self.assertIn('"name": "xtb"', listed.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            rest_server.main(["--approve-template", "xtb"])
        described = {t["name"]: t for t in self.llm.templates()["templates"]}
        self.assertEqual(described["xtb"]["modules"], ["xtb/6.6.1"])
        self.assertFalse((self.templates / "xtb" / "proposal.json").exists())
        self.assertEqual(self.llm.proposals()["proposals"], [])

    def test_replacing_needs_flag(self):
        self.builder.propose_template("hello", dict(self.SPEC, description="new hello"), self.SCRIPT)
        store = self.jobs.proposals
        with self.assertRaises(ValueError):
            store.approve("hello")
        store.approve("hello", replace=True)
        self.assertEqual(self.llm.templates()["templates"][0]["description"], "new hello")
        self.assertTrue(any(p.name.startswith("hello.replaced-") for p in self.templates.iterdir()))

    def test_invalid_proposals(self):
        self.expect_error(422, self.builder.propose_template, "bad", self.SPEC, "#SBATCH --gres=gpu:8\nrun\n")
        self.expect_error(422, self.builder.propose_template, "bad", dict(self.SPEC, modules=["x; rm -rf ~"]),
                          self.SCRIPT)
        self.expect_error(422, self.builder.propose_template, "../evil", self.SPEC, self.SCRIPT)
        self.expect_error(400, self.builder.request, "POST", "/templates/propose",
                          body={"name": "x", "template": {}, "script": "", "install": True})

    def test_reject(self):
        self.builder.propose_template("xtb", self.SPEC, self.SCRIPT)
        with contextlib.redirect_stdout(io.StringIO()):
            rest_server.main(["--reject-template", "xtb"])
        self.assertFalse((self.proposals_dir / "xtb").exists())


class TestArrays(WorkflowTestCase):

    def setUp(self):
        super().setUp()
        self.scan = self.llm_root / "scans" / "sample"
        self.steps = make_scan(self.scan)
        self.manifest = {"path": "scans/sample/scan_info.json", "key": "steps", "fields": {"input": "file"}}

    def test_explicit_tasks(self):
        tasks = [{"input": f"scans/sample/{s['file']}"} for s in self.steps[:2]]
        out = self.llm.submit_job("orca", {"nprocs": 4}, tasks=tasks, throttle=2, label="sample")
        job = fake_job(self.slurm, out["job_id"])
        self.assertIn("--array=0-1%2", job["args"])
        self.assertIn("--ntasks=4", job["args"])
        self.assertIn("--output=hpc-rest-%A_%a.out", job["args"])
        self.assertEqual(out["array_size"], 2)
        self.assertEqual(out["output"], str(self.llm_root / f"hpc-rest-{out['job_id']}_%a.out"))
        self.assertIn(f"export HPC_TASK_INPUT={self.scan / 'scan_000_001.inp'}", job["script"])

    def test_tasks_from_manifest(self):
        plan = self.llm.submit_job("orca", {"nprocs": 4}, tasks_from=self.manifest, dry_run=True)
        self.assertEqual(plan["array_size"], 6)
        self.assertEqual(plan["throttle"], 4)  # defaults to the concurrency limit
        self.assertEqual(plan["tasks"][4], {"input": str(self.scan / "scan_001_001.inp")})
        self.assertEqual(plan["task_source"]["indices"], list(range(6)))
        out = self.llm.submit_job("orca", {"nprocs": 4}, tasks_from=dict(self.manifest, select=[1, 4]),
                                  throttle=2)
        status = self.llm.job_status(out["job_id"], include_tasks=True)
        self.assertEqual([t["source_index"] for t in status["tasks"]], [1, 4])
        self.assertEqual(status["tasks"][1]["params"]["input"], str(self.scan / "scan_001_001.inp"))
        self.assertEqual(status["tasks"][1]["output"], str(self.llm_root / f"hpc-rest-{out['job_id']}_1.out"))

    def test_manifest_errors(self):
        self.expect_error(422, self.llm.submit_job, "orca", tasks_from=dict(self.manifest, key="nope"))
        self.expect_error(422, self.llm.submit_job, "orca", tasks_from=dict(self.manifest, fields={"input": "path"}))
        self.expect_error(422, self.llm.submit_job, "orca", tasks_from=dict(self.manifest, select=[99]))
        self.expect_error(400, self.llm.submit_job, "orca", tasks_from=dict(self.manifest, glob="*"))
        self.expect_error(403, self.llm.submit_job, "orca", tasks_from=dict(self.manifest, path=str(self.outside)))
        # a manifest can't point tasks outside the token's directories
        (self.outside / "x.inp").write_text("")
        bad = self.scan / "bad.json"
        bad.write_text(json.dumps({"steps": [{"file": str(self.outside / "x.inp")}, {"file": "../../../../x.inp"}]}))
        p = self.expect_error(422, self.llm.submit_job, "orca",
                              tasks_from={"path": str(bad), "key": "steps", "fields": {"input": "file"}})
        self.assertEqual(len(p["violations"]), 2)

    def test_array_rules(self):
        self.expect_error(422, self.llm.submit_job, "orca", {"nprocs": 4})                   # no tasks
        self.expect_error(422, self.llm.submit_job, "orca", tasks=[{"input": "x"}], tasks_from=self.manifest)
        self.expect_error(422, self.llm.submit_job, "hello", tasks=[{"message": "x"}])         # not an array
        self.expect_error(422, self.llm.submit_job, "orca", tasks_from=self.manifest, throttle=5)  # > cap
        self.expect_error(400, self.llm.submit_job, "orca", tasks_from=self.manifest, throttle=0)
        self.expect_error(422, self.llm.submit_job, "orca", tasks=[{"input": "scans/sample/missing.inp"}])
        p = self.expect_error(422, self.llm.submit_job, "orca", tasks=[{"input": "x", "extra": 1}])
        self.assertTrue(any("task 0: unknown parameter 'extra'" in v for v in p["violations"]))
        self.jobs.limits.limits["max_array_tasks"] = 5
        self.expect_error(422, self.llm.submit_job, "orca", tasks_from=self.manifest)

    def test_concurrency_counts_throttle_slots(self):
        self.llm.submit_job("orca", tasks_from=self.manifest, throttle=3)
        self.llm.submit_job("hello")
        p = self.expect_error(429, self.llm.submit_job, "hello")
        self.assertIn("4 of 4", p["error"])

    def test_task_states(self):
        job_id = self.llm.submit_job("orca", tasks_from=self.manifest, throttle=2)["job_id"]
        self.assertEqual(self.llm.job_status(job_id)["state"], "PENDING")
        set_task_state(self.slurm, job_id, 0, "RUNNING")
        status = self.llm.job_status(job_id)
        self.assertEqual((status["state"], status["task_counts"]), ("RUNNING", {"RUNNING": 1, "PENDING": 5}))
        for t in range(6):
            set_task_state(self.slurm, job_id, t, "COMPLETED" if t != 3 else "FAILED")
        status = self.llm.job_status(job_id, include_tasks=True)
        self.assertEqual((status["state"], status["terminal"], status["failed_tasks"]), ("FAILED", True, [3]))
        self.assertEqual(status["tasks"][3]["exit_code"], "1:0")
        self.assertEqual(status["tasks"][3]["source_index"], 3)
        # finished arrays are answered from the registry, without asking SLURM again
        set_task_state(self.slurm, job_id, 3, "COMPLETED")
        self.assertEqual(self.llm.job_status(job_id)["failed_tasks"], [3])
        self.assertEqual([j["job_id"] for j in self.llm.jobs(label=None)["jobs"]], [job_id])

    def test_cancel_array(self):
        job_id = self.llm.submit_job("orca", tasks_from=self.manifest, throttle=2)["job_id"]
        out = self.llm.cancel_job(job_id)
        self.assertEqual((out["state"], out["task_counts"]), ("CANCELLED", {"CANCELLED": 6}))

    def test_labels(self):
        a = self.llm.submit_job("orca", tasks_from=self.manifest, throttle=1, label="scan-a")["job_id"]
        self.llm.submit_job("hello", label="other")
        self.assertEqual([j["job_id"] for j in self.llm.jobs(label="scan-a")["jobs"]], [a])

    def test_array_task_id_parsing(self):
        self.assertEqual(rest_jobs.SlurmRunner._array_tasks("[0-3,7%2]"), [0, 1, 2, 3, 7])
        self.assertEqual(rest_jobs.SlurmRunner._array_tasks("12"), [12])


class TestOrcaScript(WorkflowTestCase):
    """Runs the bundled template's rendered script, with a fake orca."""

    def run_task(self, nprocs=4, task=1, extra_env=None, input_nprocs=4):
        scan = self.llm_root / "scans" / "run"
        make_scan(scan, nprocs=input_nprocs)
        plan = self.llm.submit_job("orca", {"nprocs": nprocs}, dry_run=True,
                                   tasks_from={"path": "scans/run/scan_info.json", "key": "steps",
                                               "fields": {"input": "file"}})
        scratch = self.tmp / "node-scratch"
        scratch.mkdir(exist_ok=True)
        env = dict(os.environ, SLURM_ARRAY_TASK_ID=str(task), SLURM_JOB_ID="77", SLURM_NTASKS=str(nprocs),
                   TMPDIR=str(scratch), **(extra_env or {}))
        res = subprocess.run(["bash", "-c", plan["script"]], cwd=self.tmp, env=env, capture_output=True, text=True)
        return res, scan, scratch

    def test_success(self):
        res, scan, scratch = self.run_task()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("TERMINATED NORMALLY", (scan / "scan_000_001.out").read_text())
        self.assertTrue((scan / "scan_000_001.gbw").exists())
        self.assertTrue((scan / "scan_000_001.xyz").exists())
        self.assertFalse((scan / "scan_000_001.tmp").exists())
        self.assertFalse((scan / "scan_000_000.out").exists())  # only this task's input
        self.assertEqual(list(scratch.iterdir()), [])           # scratch cleaned up

    def test_core_mismatch(self):
        res, _, _ = self.run_task(nprocs=2, input_nprocs=4)
        self.assertEqual(res.returncode, 4)
        self.assertIn("nprocs=4", res.stderr)

    def test_failure(self):
        res, scan, _ = self.run_task(extra_env={"FAKE_ORCA_FAIL": "1"})
        self.assertEqual(res.returncode, 1)
        self.assertIn("SCF NOT CONVERGED", (scan / "scan_000_001.out").read_text())

    def test_no_orca(self):
        res, _, _ = self.run_task(extra_env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(res.returncode, 3)

    def test_module_loads_rendered(self):
        template = rest_jobs.TemplateStore(str(self.templates)).get("orca")
        template.spec["modules"] = ["GCC/12.2.0", "ORCA/5.0.4"]
        script, _ = template.render({"nprocs": 1}, [{"input": "/x.inp"}])
        self.assertTrue(script.startswith("#!/bin/bash -l\n"))  # same environment as search_modules
        self.assertIn("module load GCC/12.2.0 || {", script)
        self.assertLess(script.index("module load GCC"), script.index("module load ORCA"))
        self.assertLess(script.index("module load"), script.index('case "${SLURM_ARRAY_TASK_ID'))
        template.spec["modules"] = []
        self.assertTrue(template.render({"nprocs": 1}, [{"input": "/x.inp"}])[0].startswith("#!/bin/bash\n"))

    def test_failed_module_load_stops_the_task(self):
        template = rest_jobs.TemplateStore(str(self.templates)).get("orca")
        template.spec["modules"] = ["ORCA/9.9.9"]
        script, _ = template.render({"nprocs": 1}, [{"input": str(self.tmp / "x.inp")}])
        fake = self.tmp / "module-fail.sh"
        fake.write_text('module() { echo "Lmod: unknown module $2" >&2; return 1; }\n')
        res = subprocess.run(["bash", "-c", f". {fake}; " + script.split("\n", 1)[1]], capture_output=True,
                             text=True, env=dict(os.environ, SLURM_ARRAY_TASK_ID="0"))
        self.assertEqual(res.returncode, 3)
        self.assertIn("could not load module ORCA/9.9.9", res.stderr)


class TestRegistryMigration(unittest.TestCase):

    def test_old_database_gains_columns(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        db = tmp / "jobs.sqlite"
        with sqlite3.connect(db) as conn:
            conn.executescript(rest_jobs.JobRegistry.SCHEMA)
            conn.execute("INSERT INTO jobs VALUES ('5', 'llm', 'hello', '{}', '{}', '/w', '/w/o', 'n', NULL, 1, "
                         "'COMPLETED', 1)")
        registry = rest_jobs.JobRegistry(str(db))
        job = registry.get("5")
        self.assertIsNone(job["array_size"])
        self.assertEqual(job["state"], "COMPLETED")


class TestFileSync(WorkflowTestCase):

    def setUp(self):
        super().setUp()
        self.local = self.tmp / "local"
        self.local_scan = self.local / "Desktop" / "sample_scan"
        make_scan(self.local_scan)
        (self.local_scan / "__pycache__").mkdir()
        (self.local_scan / "__pycache__" / "x.pyc").write_text("")
        self.sync = FileSync(self.builder, [str(self.local)])

    def test_push_then_pull(self):
        out = self.sync.push(str(self.local_scan), "scans/sample_scan", pattern="*.inp,*.json")
        self.assertEqual(len(out["uploaded"]), 7)
        self.assertTrue((self.llm_root / "scans" / "sample_scan" / "scan_info.json").exists())
        self.assertFalse((self.llm_root / "scans" / "sample_scan" / "__pycache__").exists())
        again = self.sync.push(str(self.local_scan), "scans/sample_scan", pattern="*.inp,*.json")
        self.assertEqual((len(again["uploaded"]), len(again["skipped"])), (0, 7))

        # pretend the jobs ran, then bring the outputs back next to the inputs
        for f in (self.llm_root / "scans" / "sample_scan").glob("*.inp"):
            f.with_suffix(".out").write_text("ORCA TERMINATED NORMALLY")
        out = self.sync.pull("scans/sample_scan", str(self.local_scan), pattern="*.out")
        self.assertEqual(len(out["downloaded"]), 6)
        self.assertTrue((self.local_scan / "scan_001_002.out").exists())
        self.assertEqual(len(self.sync.pull("scans/sample_scan", str(self.local_scan), "*.out")["skipped"]), 6)

    def test_local_roots_enforced(self):
        with self.assertRaises(RESTClientError):
            self.sync.push(str(self.outside), "x")
        with self.assertRaises(RESTClientError):
            self.sync.pull("scans", str(self.tmp / "elsewhere"))
        (self.local / "link").symlink_to(self.outside)
        with self.assertRaises(RESTClientError):
            self.sync.list_local(str(self.local / "link"))

    def test_remote_scope_still_applies(self):
        reader_sync = FileSync(self.llm, [str(self.local)])   # llm token: no files:write
        with self.assertRaises(RESTClientError) as ctx:
            reader_sync.push(str(self.local_scan), "scans/x")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(RESTClientError):
            self.sync.push(str(self.local_scan), str(self.outside))


@unittest.skipUnless(HAVE_MCP, "the MCP Python SDK is not installed (pip install mcp)")
class TestMCPWorkflow(WorkflowTestCase):

    def test_scan_round_trip(self):
        local = self.tmp / "local"
        scan = local / "sample_scan"
        make_scan(scan)

        async def body(session, init):
            out = {"tools": [t.name for t in (await session.list_tools()).tools]}
            out["push"] = await session.call_tool("push_files", {"local_path": str(scan),
                                                                 "remote_dir": "scans/sample_scan"})
            out["modules"] = await session.call_tool("search_modules", {"query": "orca"})
            out["guide"] = await session.call_tool("read_guide", {"name": "orca"})
            out["submit"] = await session.call_tool("submit_job", {
                "template": "orca", "params": {"nprocs": 4}, "label": "sample_scan", "idempotency_key": "s1",
                "tasks_from": {"path": "scans/sample_scan/scan_info.json", "key": "steps",
                               "fields": {"input": "file"}}})
            job_id = tool_payload(out["submit"])["job_id"]
            for t in range(6):
                set_task_state(self.slurm, job_id, t, "COMPLETED")
            for f in (self.llm_root / "scans" / "sample_scan").glob("*.inp"):
                f.with_suffix(".out").write_text("done")
            out["status"] = await session.call_tool("job_status", {"job_id": job_id, "include_tasks": True})
            out["pull"] = await session.call_tool("pull_files", {"remote_path": "scans/sample_scan",
                                                                 "local_dir": str(scan), "pattern": "*.out"})
            out["escape"] = await session.call_tool("pull_files", {"remote_path": "scans/sample_scan",
                                                                   "local_dir": str(self.outside)})
            return out

        from test_rest_jobs import TestMCP
        out = TestMCP.session(self, body, "--local-root", str(local), token=self.builder_token)
        self.assertIn("push_files", out["tools"])
        self.assertEqual(len(tool_payload(out["push"])["uploaded"]), 7)
        self.assertEqual([m["name"] for m in tool_payload(out["modules"])["modules"]], ["ORCA/5.0.4", "ORCA/6.0.0"])
        self.assertIn("tasks_from", tool_payload(out["guide"])["guide"])
        status = tool_payload(out["status"])
        self.assertEqual((status["state"], len(status["tasks"])), ("COMPLETED", 6))
        self.assertEqual(len(tool_payload(out["pull"])["downloaded"]), 6)
        self.assertTrue((scan / "scan_000_000.out").exists())
        self.assertTrue(is_error(out["escape"]))


if __name__ == "__main__":
    unittest.main()
