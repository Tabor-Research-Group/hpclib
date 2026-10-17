"""Browser regressions for Activity; local fixture APIs, no real jobs or cluster."""
import json
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import test_agent_console_ui as console_ui


@unittest.skipUnless(console_ui.sync_playwright, "optional Playwright dependency is not installed")
class TestActivityUI(unittest.TestCase):
    # Share the existing local console/browser fixture, without inheriting its tests.
    @classmethod
    def setUpClass(cls):
        console_ui.TestConsoleUI.setUpClass.__func__(cls)

    def setUp(self):
        console_ui.TestConsoleUI.setUp(self)
        self.calls = []
        now = time.time()
        self.jobs = [
            dict(job_id="103", template="orca", label="Failed scan", token="agent-b", state="FAILED", terminal=True,
                 submitted=now - 30, output="/work/failed.log", exit_code="1:0", elapsed="00:01:23"),
            dict(job_id="102", template="python_project", label="Active analysis", token="agent-a", state="RUNNING", terminal=False,
                 submitted=now - 60, output="/work/active.log"),
            dict(job_id="101", template="hello", label="Finished analysis", token="agent-a", state="COMPLETED", terminal=True,
                 submitted=now - 8 * 86400, output="/work/finished.log", exit_code="0:0"),
            dict(job_id="104", template="orca", label="Array scan", token="agent-a", state="PENDING", terminal=False,
                 submitted=now - 100, output="/work/array-%a.log", array_size=2, throttle=1,
                 task_counts={"FAILED": 1, "PENDING": 1}, tasks=[
                     dict(index=0, state="FAILED", exit_code="2:0", output="/work/array-0.log"),
                     dict(index=1, state="PENDING", output="/work/array-1.log")]),
        ]
        for job in self.jobs:
            job.update(workdir="/work", params={"value": "example"}, resources={"time": "01:00:00"})
        self.entries = [dict(time=now - 10, token="agent-a", verb="POST", path="/jobs", status=201, detail={"job_id": "102"}),
                        dict(time=now - 5, token="agent-b", verb="POST", path="/jobs", status=422, detail={"error": "limits"})]
        self.fail_jobs = self.fail_audit = self.fail_detail = False
        self.clusters = [dict(name=name, host=name, has_owner_token=True, tunnel={"state": "up"}, login={"state": "connected"})
                         for name in ("demo", "other")]
        self.other_jobs = [dict(self.jobs[0], job_id="201", label="Other cluster")]

    def fixture(self, route, path):
        method = route.request.method
        query = parse_qs(urlsplit(route.request.url).query)
        self.calls.append((method, path, query))
        if path == "clusters": result = {"clusters": self.clusters}
        elif path in ("clusters/demo", "clusters/other"): result = self.clusters[0]
        elif "/rest/jobs/status" in path:
            if self.fail_detail:
                route.fulfill(status=503, body=json.dumps({"error": "accounting unavailable"}), content_type="application/json"); return True
            jobs = self.other_jobs if "/other/" in path else self.jobs
            result = next(j for j in jobs if j["job_id"] == query["id"][0])
        elif path.endswith("/rest/jobs"):
            if self.fail_jobs:
                route.fulfill(status=503, body=json.dumps({"error": "scheduler unavailable"}), content_type="application/json"); return True
            result = {"jobs": self.other_jobs if "/other/" in path else self.jobs}
        elif path.endswith("/rest/admin/audit"):
            if self.fail_audit:
                route.fulfill(status=503, body=json.dumps({"error": "audit unavailable"}), content_type="application/json"); return True
            entries = [e for e in self.entries if e["time"] > float(query.get("since", [0])[0])
                       and ("token" not in query or e["token"] == query["token"][0])]
            result = {"entries": entries, "latest": entries[-1]["time"] if entries else float(query.get("since", [0])[0])}
        elif path.endswith("/rest/files/tail"):
            result = {"lines": ["x" * 10000, "Job output"], "binary": False, "truncated": True}
        else: return False
        route.fulfill(body=json.dumps(result), content_type="application/json")
        return True

    def load_activity(self):
        console_ui.TestConsoleUI.load_console(self, self.fixture)
        self.page.get_by_role("link", name="Activity", exact=True).click()
        self.page.get_by_role("button", name="Check now", exact=True).wait_for()

    def rows(self):
        return self.page.locator(".activity-jobs tbody tr[data-job-id]")

    def check(self):
        # HpcActionButton reports completion after the history and detail requests.
        self.page.locator('hpc-action-button[label="Check now"]').evaluate("node => { window.activityCheckDone = false; node.addEventListener('done', () => window.activityCheckDone = true, {once:true}); }")
        self.page.get_by_role("button", name="Check now", exact=True).click()
        self.page.wait_for_function("window.activityCheckDone === true")

    def test_history_filters_finished_details_and_output_stay_bounded(self):
        self.load_activity()
        self.assertEqual(self.page.get_by_role("button", name="Jobs", exact=True).get_attribute("aria-pressed"), "true")
        self.assertEqual(self.rows().evaluate_all("rows => rows.map(r => r.dataset.jobId)"), ["103", "102", "104", "101"])
        self.page.get_by_label("Job state", exact=True).select_option("finished")
        self.assertEqual(self.rows().count(), 2)
        self.page.get_by_label("Submitted within", exact=True).select_option("7")
        self.assertEqual(self.rows().count(), 1)
        self.page.get_by_label("Submitted within", exact=True).select_option("all")
        self.page.get_by_label("Job state", exact=True).select_option("all")
        self.page.get_by_label("Agent token", exact=True).fill("agent-a")
        self.assertEqual(self.rows().count(), 3)
        self.page.get_by_label("Find jobs", exact=True).fill("finished")
        self.assertEqual(self.rows().count(), 1)
        self.rows().get_by_role("button", name="Details", exact=True).click()
        self.page.get_by_text("Exit code", exact=True).wait_for()
        self.assertEqual(self.page.locator(".activity-details").get_by_text("0:0", exact=True).count(), 1)
        self.page.get_by_role("button", name="Preview output", exact=True).click()
        self.page.locator("pre.log").wait_for()
        self.assertEqual(self.page.get_by_role("link", name="Open in Files").get_attribute("href"), "#/files?cluster=demo&path=%2Fwork%2Ffinished.log")
        for width in (1440, 900, 480):
            self.page.set_viewport_size({"width": width, "height": 900})
            sizes = self.page.locator("hpc-panel.log-panel").evaluate("p => ({width:p.getBoundingClientRect().width, parent:p.parentElement.getBoundingClientRect().width, log:p.querySelector('pre').clientWidth, scroll:p.querySelector('pre').scrollWidth})")
            self.assertLessEqual(sizes["width"], min(960, sizes["parent"]) + 1, sizes)
            self.assertGreater(sizes["scroll"], sizes["log"], sizes)
        # Dismissing only the log leaves the job detail open.
        self.page.locator("hpc-panel.log-panel").get_by_role("button", name="Dismiss").click()
        self.assertEqual(self.page.locator(".activity-details").count(), 1)
        self.assertTrue(all(verb == "GET" for verb, _, _ in self.calls))
        self.assertEqual(self.errors, [])

    def test_arrays_request_filters_and_partial_failures(self):
        self.load_activity()
        self.rows().filter(has_text="Array scan").get_by_role("button", name="Details").click()
        task = self.page.get_by_label("Task state", exact=True)
        task.wait_for(); task.select_option("failed")
        self.page.get_by_text("1 of 1 matching tasks shown", exact=True).wait_for()
        self.page.get_by_role("button", name="Preview output").click()
        self.page.locator("pre.log").wait_for()
        self.assertTrue(any(query.get("path") == ["/work/array-0.log"] for _, _, query in self.calls))
        self.jobs[3]["tasks"][1]["state"] = "RUNNING"
        self.check()
        self.assertEqual(self.page.get_by_label("Task state", exact=True).input_value(), "failed")
        self.assertEqual(self.page.locator("pre.log").count(), 1)
        self.page.get_by_role("button", name="Requests", exact=True).click()
        self.assertTrue(self.page.get_by_role("region", name="Request log").is_visible())
        self.page.get_by_label("Request errors only").check()
        self.assertEqual(self.page.get_by_role("region", name="Request log").locator("tbody tr").count(), 1)
        self.page.get_by_label("Agent token", exact=True).fill("agent-b")
        self.page.get_by_label("Agent token", exact=True).press("Tab")
        self.page.wait_for_function("document.querySelector('.activity-jobs tbody tr[data-job-id]')?.dataset.jobId === '103'")
        self.check()
        self.assertTrue(any(query.get("token") == ["agent-b"] for _, path, query in self.calls if path.endswith("/audit")))
        self.fail_jobs = True
        self.check()
        self.page.get_by_text("Jobs: scheduler unavailable", exact=True).wait_for()
        self.page.get_by_role("button", name="Jobs", exact=True).click()
        self.assertEqual(self.rows().count(), 1)
        self.assertIn("stale", self.page.get_by_role("region", name="Submitted jobs").inner_text())
        self.fail_jobs = False; self.fail_audit = True; self.check()
        self.page.get_by_text("Requests: audit unavailable", exact=True).wait_for()
        self.assertEqual(self.rows().count(), 1)
        self.assertEqual(self.errors, [])

    def test_follow_updates_outcomes_and_cluster_switch_clears_details(self):
        self.load_activity()
        self.rows().filter(has_text="Active analysis").get_by_role("button", name="Details").click()
        self.page.locator(".activity-details").wait_for()
        self.page.get_by_label("Follow activity").check()
        self.jobs[1].update(state="COMPLETED", terminal=True, exit_code="0:0")
        self.page.wait_for_function("document.querySelector('tr[data-job-id=\"102\"] hpc-state')?.getAttribute('label') === 'COMPLETED'", timeout=10000)
        self.page.get_by_text("Exit code", exact=True).wait_for()
        self.page.locator("hpc-cluster-picker select").select_option("other")
        self.page.wait_for_function("document.querySelector('tr[data-job-id=\"201\"]') !== null")
        self.assertEqual(self.page.locator(".activity-details").count(), 0)
        self.assertEqual(self.rows().count(), 1)
        self.page.get_by_role("link", name="Clusters", exact=True).click()
        before = len([path for _, path, _ in self.calls if path.endswith("/rest/jobs")])
        self.page.wait_for_timeout(5500)
        self.assertEqual(len([path for _, path, _ in self.calls if path.endswith("/rest/jobs")]), before)
        self.assertEqual(self.errors, [])

    def test_no_owner_connection_has_a_clear_empty_state(self):
        self.clusters = []
        console_ui.TestConsoleUI.load_console(self, self.fixture)
        self.page.get_by_role("link", name="Activity", exact=True).click()
        self.page.get_by_text("Start a cluster tunnel with an owner token to check jobs and requests.", exact=True).wait_for()
        self.assertFalse(any("/rest/jobs" in path for _, path, _ in self.calls))
        self.assertEqual(self.errors, [])
