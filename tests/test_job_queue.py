"""Tests for the query_jobs / metadata-blob / graceful-lock-degradation
feature set added to job_queue.queue_db, job_queue.decision, and
job_queue.cli.

Run with:  python -m unittest tests.test_job_queue -v
(requires the job_queue package importable, e.g. run from the repo
root, or `pip install -e .` first)
"""
import contextlib
import io
import json
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from job_queue import queue_db as queue_db_module
from job_queue.cli import main as cli_main
from job_queue.decision import determine_action
from job_queue.metadata import MetadataStore
from job_queue.models import JobStatus
from job_queue.queue_db import QueueDB, QueueUnavailable


@contextlib.contextmanager
def capture_output():
    """Like pytest's capsys, but for plain unittest: yields an object
    with .out/.err populated after the `with` block exits.
    """
    class _Captured:
        out = ""
        err = ""

    captured = _Captured()
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        yield captured
    captured.out = out_buf.getvalue()
    captured.err = err_buf.getvalue()


def _ids(rows):
    return sorted(r["job_id"] for r in rows)


class TempDirTestCase(unittest.TestCase):
    """Common setUp/tearDown for tests that need a scratch directory -
    equivalent to pytest's built-in tmp_path fixture.
    """

    def setUp(self):
        self.tmp_path = Path(tempfile.mkdtemp(prefix="jqtest-"))
        self.addCleanup(shutil.rmtree, self.tmp_path, ignore_errors=True)


class QueueTestCase(TempDirTestCase):
    """Adds a fresh QueueDB backed by a throwaway file, on top of the
    scratch-directory setup above.
    """

    def setUp(self):
        super().setUp()
        self.queue = QueueDB(db_path=self.tmp_path / "queue.db")


class SeededQueueTestCase(QueueTestCase):
    """A queue pre-populated with a handful of jobs spanning multiple
    statuses and directories, used by every filtering test below.
    """

    def setUp(self):
        super().setUp()
        jobs = [
            ("id-1", "jq_t1_success", "/scratch/mark/jqt/metadata/jq_t1_success.json", "COMPLETED", 1),
            ("id-2", "jq_t2_fail_then_restart", "/scratch/mark/jqt/metadata/jq_t2_fail_then_restart.json", "FAILED", 1),
            ("id-3", "jq_t3_exhaust_retries", "/scratch/mark/jqt/metadata/jq_t3_exhaust_retries.json", "FAILED", 5),
            ("id-4", "train_run_42", "/scratch/mark/other_proj/metadata/train_run_42.json", "RUNNING", 1),
            ("id-5", "train_run_43", "/scratch/mark/other_proj/metadata/train_run_43.json", "COMPLETED", 1),
        ]
        for job_id, name, path, status, attempt in jobs:
            self.queue.upsert_job(
                job_id=job_id,
                job_name=name,
                metadata_path=path,
                status=status,
                metadata_json=json.dumps({"job_id": job_id, "job_name": name, "attempt": attempt}),
            )


# --------------------------------------------------------------------------
# Schema / migration
# --------------------------------------------------------------------------

class TestSchemaMigration(QueueTestCase):
    def test_fresh_db_has_metadata_column(self):
        with self.queue._connect() as conn:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        self.assertIn("metadata", cols)

    def test_migrates_preexisting_db_missing_metadata_column(self):
        """Simulates a queue.db created by a version of this package
        before the `metadata` column existed - the migration should
        add it in place, without losing the existing row.
        """
        db_path = self.tmp_path / "legacy.db"
        old_schema = """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id          TEXT PRIMARY KEY,
            job_name        TEXT NOT NULL,
            metadata_path   TEXT NOT NULL UNIQUE,
            slurm_job_id    INTEGER,
            status          TEXT NOT NULL DEFAULT 'NEW',
            submitted_at    TEXT,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
        conn = sqlite3.connect(db_path)
        conn.executescript(old_schema)
        conn.execute(
            "INSERT INTO jobs (job_id, job_name, metadata_path, status) VALUES (?, ?, ?, ?)",
            ("preexisting-1", "legacy_job", "/tmp/legacy/metadata/legacy_job.json", "FAILED"),
        )
        conn.commit()
        conn.close()

        q = QueueDB(db_path=db_path)
        self.assertTrue(q._available)

        rows = q.list_jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], "preexisting-1")
        self.assertIsNone(rows[0]["metadata"])  # migrated column, never populated for this old row

        # and the migrated db is fully usable afterward
        q.upsert_job(
            job_id="new-1", job_name="new_job", metadata_path="/tmp/new/metadata/new_job.json",
            status="NEW", metadata_json=json.dumps({"attempt": 0}),
        )
        row = q.get_job("new-1")
        self.assertEqual(q.get_metadata_dict(row), {"attempt": 0})


# --------------------------------------------------------------------------
# query_jobs filtering semantics
# --------------------------------------------------------------------------

class TestQueryJobsFiltering(SeededQueueTestCase):
    def test_incomplete_excludes_only_completed(self):
        self.assertEqual(_ids(self.queue.query_jobs(incomplete=True)), ["id-2", "id-3", "id-4"])

    def test_status_exact_match(self):
        self.assertEqual(_ids(self.queue.query_jobs(status="FAILED")), ["id-2", "id-3"])

    def test_name_regex_is_unanchored_substring_match(self):
        # "t2" should match jq_t2_fail_then_restart without needing a
        # leading anchor - re.search semantics, not re.fullmatch
        self.assertEqual(_ids(self.queue.query_jobs(name_regex="t2")), ["id-2"])

    def test_name_regex_anchored(self):
        self.assertEqual(
            _ids(self.queue.query_jobs(name_regex=r"^jq_t")), ["id-1", "id-2", "id-3"]
        )

    def test_dir_regex_matches_directory_not_filename(self):
        # "other_proj" only appears in the DIRECTORY portion of the
        # path; this also implicitly checks JOB_DIR() strips the
        # filename, since none of these job_names/paths contain
        # "other_proj" as a substring of the .json filename itself.
        self.assertEqual(_ids(self.queue.query_jobs(dir_regex="other_proj")), ["id-4", "id-5"])

    def test_dir_regex_does_not_match_on_filename_alone(self):
        # "jq_t1_success" (the FILENAME stem) should NOT match via
        # dir_regex, since JOB_DIR() strips the filename before
        # matching - only job_name/name_regex should find this job.
        self.assertEqual(self.queue.query_jobs(dir_regex="jq_t1_success"), [])
        self.assertEqual(_ids(self.queue.query_jobs(name_regex="jq_t1_success")), ["id-1"])

    def test_filters_compose_with_and_semantics(self):
        self.assertEqual(
            _ids(self.queue.query_jobs(incomplete=True, dir_regex="jqt")), ["id-2", "id-3"]
        )
        self.assertEqual(
            _ids(self.queue.query_jobs(incomplete=True, dir_regex="other_proj")), ["id-4"]
        )

    def test_no_filters_returns_everything(self):
        self.assertEqual(
            _ids(self.queue.query_jobs()), ["id-1", "id-2", "id-3", "id-4", "id-5"]
        )

    def test_incomplete_and_status_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            self.queue.query_jobs(incomplete=True, status="FAILED")

    def test_invalid_name_regex_raises_re_error(self):
        with self.assertRaises(re.error):
            self.queue.query_jobs(name_regex="(unclosed")

    def test_invalid_dir_regex_raises_re_error(self):
        with self.assertRaises(re.error):
            self.queue.query_jobs(dir_regex="[unclosed")

    def test_list_jobs_is_backward_compatible_wrapper(self):
        self.assertEqual(
            _ids(self.queue.list_jobs(status="FAILED")),
            _ids(self.queue.query_jobs(status="FAILED")),
        )
        self.assertEqual(_ids(self.queue.list_jobs()), _ids(self.queue.query_jobs()))


# --------------------------------------------------------------------------
# metadata blob storage
# --------------------------------------------------------------------------

class TestMetadataBlobStorage(QueueTestCase):
    def test_metadata_blob_round_trips(self):
        self.queue.upsert_job(
            job_id="id-2", job_name="jq_t2_fail_then_restart",
            metadata_path="/scratch/mark/jqt/metadata/jq_t2_fail_then_restart.json",
            status="FAILED",
            metadata_json=json.dumps({"job_id": "id-2", "job_name": "jq_t2_fail_then_restart", "attempt": 1}),
        )
        row = self.queue.get_job("id-2")
        meta = self.queue.get_metadata_dict(row)
        self.assertEqual(meta["attempt"], 1)
        self.assertEqual(meta["job_name"], "jq_t2_fail_then_restart")

    def test_get_metadata_dict_handles_missing_blob_gracefully(self):
        self.queue.upsert_job(job_id="no-meta", job_name="x", metadata_path="/tmp/x.json", status="NEW")
        row = self.queue.get_job("no-meta")
        self.assertIsNone(self.queue.get_metadata_dict(row))

    def test_get_metadata_dict_handles_malformed_json_gracefully(self):
        self.queue.upsert_job(
            job_id="bad-meta", job_name="x", metadata_path="/tmp/x2.json", status="NEW",
            metadata_json="{not valid json",
        )
        row = self.queue.get_job("bad-meta")
        self.assertIsNone(self.queue.get_metadata_dict(row))

    def test_update_status_without_metadata_json_preserves_existing_blob(self):
        self.queue.upsert_job(
            job_id="keep-meta", job_name="x", metadata_path="/tmp/x3.json", status="NEW",
            metadata_json=json.dumps({"attempt": 1}),
        )
        self.queue.update_status("keep-meta", "RUNNING")  # no metadata_json passed
        row = self.queue.get_job("keep-meta")
        self.assertEqual(row["status"], "RUNNING")
        self.assertEqual(self.queue.get_metadata_dict(row), {"attempt": 1})  # untouched

    def test_upsert_job_metadata_path_collision_folds_into_existing_row(self):
        """Regression test for the two-different-job_ids-same-
        metadata_path race (originally surfaced by the t7 duplicate-
        submission stress test): the second upsert must update the
        FIRST job's row rather than crash with a UNIQUE constraint
        violation on metadata_path.
        """
        self.queue.upsert_job(job_id="id-A", job_name="job_a", metadata_path="/same/path.json", status="NEW")
        self.queue.upsert_job(job_id="id-B", job_name="job_b", metadata_path="/same/path.json", status="FAILED")

        rows = self.queue.list_jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], "id-A")       # original identity preserved
        self.assertEqual(rows[0]["job_name"], "job_b")    # fields still updated
        self.assertEqual(rows[0]["status"], "FAILED")


# --------------------------------------------------------------------------
# Lock contention: retry, and graceful degradation on exhaustion
# --------------------------------------------------------------------------

class TestLockHandling(TempDirTestCase):
    def test_transient_lock_is_retried_and_succeeds(self):
        real_connect = sqlite3.connect
        calls = {"n": 0}

        def flaky_connect(path, timeout=30):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise sqlite3.OperationalError("database is locked")
            return real_connect(path, timeout=timeout)

        with mock.patch.object(queue_db_module.sqlite3, "connect", flaky_connect):
            q = QueueDB(db_path=self.tmp_path / "q.db")

        self.assertTrue(q._available)
        self.assertGreaterEqual(calls["n"], 3)  # actually had to retry, not just get lucky

    def test_permanently_locked_db_degrades_gracefully(self):
        def always_locked(path, timeout=30):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(queue_db_module.sqlite3, "connect", always_locked):
            with capture_output() as captured:
                q = QueueDB(db_path=self.tmp_path / "q.db")
            self.assertFalse(q._available)
            self.assertIn("locked", captured.err.lower())

            # writes: warn, do NOT raise
            with capture_output() as captured:
                q.upsert_job(job_id="x", job_name="x", metadata_path="/tmp/x.json", status="NEW")
                q.update_status("x", "RUNNING")
            self.assertIn("unavailable", captured.err.lower())

            # reads: raise QueueUnavailable, since they genuinely can't answer
            with self.assertRaises(QueueUnavailable):
                q.query_jobs()
            with self.assertRaises(QueueUnavailable):
                q.get_job("x")

    def test_non_lock_operational_error_is_not_swallowed(self):
        """Only 'database is locked' should trigger retry/degrade - any
        other OperationalError is a real bug and must propagate.
        """
        def other_error(path, timeout=30):
            raise sqlite3.OperationalError("no such table: jobs")

        with mock.patch.object(queue_db_module.sqlite3, "connect", other_error):
            with self.assertRaises(sqlite3.OperationalError):
                QueueDB(db_path=self.tmp_path / "q.db")


# --------------------------------------------------------------------------
# decision.py: the metadata blob written to the queue db must match the
# FINAL on-disk metadata state for that decision, not a pre-update
# snapshot (this was a real ordering bug fixed alongside this feature).
# --------------------------------------------------------------------------

class TestDecisionMetadataSync(QueueTestCase):
    def _make_store(self, name="testjob"):
        return MetadataStore(self.tmp_path / "metadata" / f"{name}.json")

    def test_submit_path_metadata_blob_matches_new_record(self):
        store = self._make_store()
        decision = determine_action(metadata_path=str(store.path), job_name="testjob", queue=self.queue)

        self.assertEqual(decision.action, "submit")
        row = self.queue.get_job(decision.job_id)
        blob = self.queue.get_metadata_dict(row)
        self.assertEqual(blob["status"], "NEW")
        self.assertEqual(blob["job_id"], decision.job_id)

    def test_running_path_metadata_blob_matches_on_disk(self):
        store = self._make_store()
        store.load_or_create(job_name="testjob")
        meta = store.record_submission(slurm_job_id=111, script="submit.sh")

        with mock.patch(
            "job_queue.decision.query_sacct",
            lambda slurm_id: (JobStatus.RUNNING, None, "RUNNING"),
        ):
            decision = determine_action(metadata_path=str(store.path), queue=self.queue)

        self.assertEqual(decision.action, "running")
        row = self.queue.get_job(meta.job_id)
        blob = self.queue.get_metadata_dict(row)
        on_disk = store.load()
        self.assertEqual(blob["status"], on_disk.status.value)
        self.assertEqual(blob["status"], "PENDING")  # unchanged by this branch

    def test_complete_path_metadata_blob_reflects_post_update_state(self):
        """The ordering regression test: the queue row's metadata blob
        must show status=COMPLETED and the real exit_code - NOT the
        PENDING/no-exit-code snapshot that existed before
        store.update() ran, which is what an earlier version of this
        code would have stored (it called queue.update_status() before
        store.update()).
        """
        store = self._make_store()
        store.load_or_create(job_name="testjob")
        meta = store.record_submission(slurm_job_id=222, script="submit.sh")

        with mock.patch(
            "job_queue.decision.query_sacct",
            lambda slurm_id: (JobStatus.COMPLETED, 0, "COMPLETED"),
        ):
            decision = determine_action(metadata_path=str(store.path), queue=self.queue)

        self.assertEqual(decision.action, "complete")
        row = self.queue.get_job(meta.job_id)
        blob = self.queue.get_metadata_dict(row)
        on_disk = store.load()

        self.assertEqual(blob["status"], "COMPLETED")
        self.assertEqual(blob["exit_code"], 0)
        self.assertEqual(blob["status"], on_disk.status.value)
        self.assertEqual(blob["exit_code"], on_disk.exit_code)

    def test_restart_path_metadata_blob_reflects_post_update_state(self):
        store = self._make_store()
        store.load_or_create(job_name="testjob")
        meta = store.record_submission(slurm_job_id=333, script="submit.sh")
        self.assertLess(meta.attempt, meta.max_attempts)

        with mock.patch(
            "job_queue.decision.query_sacct",
            lambda slurm_id: (JobStatus.FAILED, 1, "FAILED"),
        ):
            decision = determine_action(metadata_path=str(store.path), queue=self.queue)

        self.assertEqual(decision.action, "restart")
        row = self.queue.get_job(meta.job_id)
        blob = self.queue.get_metadata_dict(row)
        on_disk = store.load()

        self.assertEqual(blob["status"], "FAILED")
        self.assertEqual(blob["exit_code"], 1)
        self.assertEqual(blob["status"], on_disk.status.value)
        self.assertEqual(blob["exit_code"], on_disk.exit_code)

    def test_error_path_metadata_blob_reflects_exhausted_attempts(self):
        store = self._make_store()
        store.load_or_create(job_name="testjob")
        meta = store.record_submission(slurm_job_id=444, script="submit.sh")
        store.update(attempt=meta.max_attempts)  # simulate attempts already exhausted

        with mock.patch(
            "job_queue.decision.query_sacct",
            lambda slurm_id: (JobStatus.FAILED, 1, "FAILED"),
        ):
            decision = determine_action(metadata_path=str(store.path), queue=self.queue)

        self.assertEqual(decision.action, "error")
        row = self.queue.get_job(meta.job_id)
        blob = self.queue.get_metadata_dict(row)
        self.assertEqual(blob["status"], "FAILED")
        self.assertEqual(blob["attempt"], blob["max_attempts"])


# --------------------------------------------------------------------------
# CLI surface (job_queue.cli)
# --------------------------------------------------------------------------

class TestCLI(TempDirTestCase):
    """Redirects job_queue.cli's bare `QueueDB()` calls at a throwaway
    file, and prevents ensure_queue_dir() from touching the real
    ~/.local/share/job-queue directory as a side effect.
    """

    def setUp(self):
        super().setUp()
        self.db_path = self.tmp_path / "queue.db"
        patcher_path = mock.patch.object(queue_db_module, "QUEUE_DB_PATH", self.db_path)
        patcher_ensure = mock.patch.object(queue_db_module, "ensure_queue_dir", lambda: None)
        patcher_path.start()
        patcher_ensure.start()
        self.addCleanup(patcher_path.stop)
        self.addCleanup(patcher_ensure.stop)

    def _run_cli(self, args):
        return cli_main(args)

    def test_cli_list_incomplete(self):
        q = QueueDB(db_path=self.db_path)
        q.upsert_job(job_id="a", job_name="done_job", metadata_path="/x/a.json", status="COMPLETED")
        q.upsert_job(job_id="b", job_name="broken_job", metadata_path="/x/b.json", status="FAILED")

        with capture_output() as captured:
            rc = self._run_cli(["list", "--incomplete"])
        self.assertEqual(rc, 0)
        self.assertIn("broken_job", captured.out)
        self.assertNotIn("done_job", captured.out)

    def test_cli_list_name_regex(self):
        q = QueueDB(db_path=self.db_path)
        q.upsert_job(job_id="a", job_name="jq_t2_fail_then_restart", metadata_path="/x/a.json", status="FAILED")
        q.upsert_job(job_id="b", job_name="jq_t1_success", metadata_path="/x/b.json", status="COMPLETED")

        with capture_output() as captured:
            rc = self._run_cli(["list", "--name-regex", "t2"])
        self.assertEqual(rc, 0)
        self.assertIn("jq_t2_fail_then_restart", captured.out)
        self.assertNotIn("jq_t1_success", captured.out)

    def test_cli_list_dir_regex_prints_metadata_path(self):
        q = QueueDB(db_path=self.db_path)
        q.upsert_job(
            job_id="a", job_name="train_run_42",
            metadata_path="/scratch/mark/other_proj/metadata/train_run_42.json", status="RUNNING",
        )
        with capture_output() as captured:
            rc = self._run_cli(["list", "--dir-regex", "other_proj"])
        self.assertEqual(rc, 0)
        self.assertIn("/scratch/mark/other_proj/metadata/train_run_42.json", captured.out)

    def test_cli_list_no_matches_prints_clean_message(self):
        QueueDB(db_path=self.db_path)  # empty db
        with capture_output() as captured:
            rc = self._run_cli(["list", "--incomplete"])
        self.assertEqual(rc, 0)
        self.assertIn("No jobs found", captured.out)

    def test_cli_list_incomplete_and_status_together_errors_cleanly(self):
        with capture_output() as captured:
            rc = self._run_cli(["list", "--incomplete", "--status", "FAILED"])
        self.assertEqual(rc, 1)
        self.assertIn("mutually exclusive", captured.err)

    def test_cli_list_invalid_regex_errors_cleanly(self):
        with capture_output() as captured:
            rc = self._run_cli(["list", "--name-regex", "(unclosed"])
        self.assertEqual(rc, 1)
        self.assertIn("invalid regex", captured.err.lower())

    def test_cli_list_reports_locked_queue_db_cleanly(self):
        def always_unavailable(self, *a, **kw):
            raise QueueUnavailable("queue db still locked after retrying: simulated")

        with mock.patch.object(QueueDB, "query_jobs", always_unavailable):
            with capture_output() as captured:
                rc = self._run_cli(["list", "--incomplete"])
        self.assertEqual(rc, 1)
        self.assertTrue("locked" in captured.err.lower() or "simulated" in captured.err.lower())

    def test_cli_record_submission_and_record_result_populate_metadata_blob(self):
        metadata_path = self.tmp_path / "metadata" / "cli_job.json"
        metadata_path.parent.mkdir(parents=True)
        MetadataStore(metadata_path).load_or_create(job_name="cli_job")

        with capture_output():
            rc = self._run_cli([
                "record-submission", "--metadata", str(metadata_path),
                "--slurm-job-id", "555", "--script", "submit.sh",
            ])
        self.assertEqual(rc, 0)

        q = QueueDB(db_path=self.db_path)
        rows = q.query_jobs(name_regex="cli_job")
        self.assertEqual(len(rows), 1)
        blob = q.get_metadata_dict(rows[0])
        self.assertEqual(blob["slurm_job_id"], 555)
        self.assertEqual(blob["status"], "PENDING")

        job_id = rows[0]["job_id"]
        with capture_output():
            rc = self._run_cli([
                "record-result", "--metadata", str(metadata_path),
                "--status", "completed", "--exit-code", "0",
            ])
        self.assertEqual(rc, 0)

        row = q.get_job(job_id)
        blob = q.get_metadata_dict(row)
        self.assertEqual(blob["status"], "COMPLETED")
        self.assertEqual(blob["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
