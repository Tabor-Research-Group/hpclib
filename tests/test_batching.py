"""Tests for hpclib/job_queue/batch.py - the JSON-batch-file -> one
isolated directory per job layer job_process_queue relies on. Pure
Python/filesystem - no SLURM involved.

    pip install -e hpclib/job_queue
    python -m unittest tests.job_queue.test_batch -v
"""
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hpclib.hpclib.job_queue.batch import load_job_specs, write_job_dir, JobSpec


def _write_json(path, obj):
    path.write_text(json.dumps(obj))
    return path


class TestLoadJobSpecs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_bare_array(self):
        batch = _write_json(self.tmp_path / "batch.json", [
            {"job_name": "a", "lr": 0.1},
            {"job_name": "b", "lr": 0.2, "epochs": 10},
        ])
        specs = load_job_specs(batch)
        self.assertEqual([s.job_name for s in specs], ["a", "b"])
        self.assertEqual(specs[0].config, {"lr": 0.1})
        self.assertEqual(specs[1].config, {"lr": 0.2, "epochs": 10})

    def test_jobs_wrapper(self):
        batch = _write_json(self.tmp_path / "batch.json", {"jobs": [{"job_name": "only", "x": 1}]})
        specs = load_job_specs(batch)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].config, {"x": 1})

    def test_missing_job_name_rejected(self):
        batch = _write_json(self.tmp_path / "batch.json", [{"lr": 0.1}])
        with self.assertRaisesRegex(ValueError, "job_name"):
            load_job_specs(batch)

    def test_duplicate_job_name_rejected(self):
        batch = _write_json(self.tmp_path / "batch.json", [
            {"job_name": "dup", "lr": 0.1}, {"job_name": "dup", "lr": 0.2},
        ])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            load_job_specs(batch)

    def test_non_list_rejected(self):
        batch = _write_json(self.tmp_path / "batch.json", {"lr": 0.1})
        with self.assertRaises(ValueError):
            load_job_specs(batch)


class TestWriteJobDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.sbatch_script = self.tmp_path / "process_sbatch.sh"
        self.sbatch_script.write_text("#!/bin/bash\necho ran with $1\n")
        self.sbatch_script.chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_isolated_directory(self):
        spec = JobSpec(job_name="job-a", config={"lr": 0.01, "epochs": 5})
        jd = write_job_dir(spec, self.tmp_path / "configs", self.sbatch_script)

        self.assertEqual(jd.job_dir, (self.tmp_path / "configs" / "job-a").resolve())
        self.assertEqual(jd.config_path.parent, jd.job_dir)
        self.assertEqual(jd.sbatch_script.parent, jd.job_dir)
        self.assertEqual(json.loads(jd.config_path.read_text()), {"lr": 0.01, "epochs": 5})

    def test_script_copy_is_executable(self):
        jd = write_job_dir(JobSpec(job_name="job-a", config={}), self.tmp_path / "configs", self.sbatch_script)
        self.assertTrue(jd.sbatch_script.stat().st_mode & stat.S_IXUSR)

    def test_script_copy_is_a_snapshot_not_a_link(self):
        """Editing the shared source AFTER copying must not affect a
        job's already-written copy - each job's directory is a
        snapshot, so concurrent jobs can't see each other's edits."""
        jd = write_job_dir(JobSpec(job_name="job-a", config={}), self.tmp_path / "configs", self.sbatch_script)
        original = jd.sbatch_script.read_text()
        self.sbatch_script.write_text("#!/bin/bash\necho DIFFERENT\n")
        self.assertEqual(jd.sbatch_script.read_text(), original)

    def test_segmentation_across_many_jobs(self):
        """The core correctness property this layer exists for: each
        job's config contains ONLY that job's fields, never a
        neighbor's."""
        specs = [JobSpec(job_name=f"job-{i}", config={"index": i, "tag": f"t{i}"}) for i in range(20)]
        config_dir = self.tmp_path / "configs"
        job_dirs = [write_job_dir(s, config_dir, self.sbatch_script) for s in specs]

        for i, jd in enumerate(job_dirs):
            written = json.loads(jd.config_path.read_text())
            self.assertEqual(written, {"index": i, "tag": f"t{i}"},
                              f"job-{i} config was contaminated: {written}")
        self.assertEqual(len({jd.job_dir for jd in job_dirs}), 20)

    def test_rerun_overwrites_atomically_not_merges(self):
        write_job_dir(JobSpec(job_name="job-a", config={"lr": 0.1, "stale": True}),
                      self.tmp_path / "configs", self.sbatch_script)
        jd = write_job_dir(JobSpec(job_name="job-a", config={"lr": 0.2}),
                            self.tmp_path / "configs", self.sbatch_script)
        written = json.loads(jd.config_path.read_text())
        self.assertEqual(written, {"lr": 0.2})
        self.assertNotIn("stale", written)

    def test_missing_sbatch_script_raises(self):
        with self.assertRaises(FileNotFoundError):
            write_job_dir(JobSpec(job_name="job-a", config={}), self.tmp_path / "configs", self.tmp_path / "nope.sh")

    def test_cli_write_configs_end_to_end(self):
        """Exercises the actual `job-queue write-configs` subcommand
        job_process_queue shells out to, not just the library function."""
        batch = _write_json(self.tmp_path / "batch.json",
                             [{"job_name": "a", "lr": 0.1}, {"job_name": "b", "lr": 0.2}])
        config_dir = self.tmp_path / "configs"

        result = subprocess.run(
            [sys.executable, "-m", "job_queue", "write-configs",
             "--batch-file", str(batch), "--sbatch-script", str(self.sbatch_script), "--config-dir", str(config_dir)],
            capture_output=True, text=True, check=True,
        )
        seen = {}
        for line in (l for l in result.stdout.splitlines() if l.strip()):
            job_name, config_path, script_copy = line.split("\t")
            seen[job_name] = (Path(config_path), Path(script_copy))

        self.assertEqual(set(seen), {"a", "b"})
        for job_name, (config_path, script_copy) in seen.items():
            self.assertEqual(config_path.parent, config_dir / job_name)
            self.assertEqual(script_copy.parent, config_dir / job_name)
            expected_lr = 0.1 if job_name == "a" else 0.2
            self.assertEqual(json.loads(config_path.read_text())["lr"], expected_lr)


if __name__ == "__main__":
    unittest.main()