"""The whole queue for agents: the REST server's GET /queue (queue_view.py), with a fake squeue and sinfo."""
import unittest

from test_rest_jobs import JobServerTestCase  # noqa: E402
import rest_server  # noqa: E402
from rest_client import RESTClient, RESTClientError  # noqa: E402

QUEUE = ("U|mark\n"
         "P|cpu|880/40/0/920|23\nP|gpu|64/0/0/64|2\n"
         "G|g01|gpu|mixed|gpu:a100:4(S:0-1)|gpu:a100:3(IDX:0-2)\nG|g02|gpu|idle|gpu:a100:4|gpu:a100:0(IDX:N/A)\n"
         "R|201||gres/gpu:a100:2|||\nR|202|gres/gpu:1||||a100|v100\nR|203|gres/gpu:h100=4||||\n"
         "Q|201|alice|RUNNING|None|cpu|1:02:03|2-00:00:00|2|96|384G|c[01-02]|2026-11-04T10:00:00|2026-11-04T09:00:00|1000|hpclib-rest:llm:orca|hpcrest-orca-big\n"
         "Q|202|alice|PENDING|Priority|cpu|0:00|1-00:00:00|1|48|100G||N/A|2026-11-04T09:30:00|900||ml_train\n"
         "Q|203|bob|PENDING|AssocGrpGRES|gpu|0:00|04:00:00|1|8|32G||N/A|2026-11-04T11:00:00|800||torch\n"
         "Q|205|mark|RUNNING|None|cpu|0:10:00|04:00:00|1|4|8G|c03|2026-11-04T12:00:00|2026-11-04T11:00:00|800||mine\n")


class QueueServer(JobServerTestCase):
    def setUp(self):
        super().setUp()
        self.reads = []
        self.answer = (0, QUEUE, "")
        self.server.cluster_queue = rest_server.ClusterQueue(runner=lambda: self.reads.append(1) or self.answer)
        self.server.cluster_queue.RETRY_WAIT = 0
        self.reader = RESTClient(self.url, token=self.reader_token)

    def test_summary_of_everyones_queue_with_gpus_and_no_jobs_unless_asked(self):
        out = self.llm.queue()
        self.assertNotIn("jobs", out)
        self.assertEqual((out["counts"]["running"], out["counts"]["queued"], out["counts"]["users"]), (1, 2, 2))   # others' jobs
        cpu = next(p for p in out["partitions"] if p["name"] == "cpu")
        self.assertEqual((cpu["cpus"]["idle"], cpu["running"], cpu["queued"]), (40, 2, 1))                       # CPUs count mine too
        a100 = out["gpus"][0]["types"][0]
        self.assertEqual((a100["total"], a100["used"], a100["free"], a100["max_free"]), (8, 3, 5, 4))
        self.assertEqual(out["reasons"], {"Priority": 1, "AssocGrpGRES": 1})
        self.assertEqual([u["user"] for u in out["users"]], ["alice", "bob"])
        self.assertIsNone(out["error"])
        self.assertEqual(self.reader.queue()["counts"], out["counts"])                         # any reader may ask

    def test_jobs_are_listed_and_filtered_on_request(self):
        out = self.llm.queue(jobs="all")["jobs"]
        self.assertEqual((out["running_matching"], out["queued_matching"]), (1, 2))
        self.assertEqual(out["running"][0]["source"]["kind"], "agent")
        self.assertNotIn("comment", out["running"][0])
        self.assertEqual([j["id"] for j in self.llm.queue(jobs="queued", user="BOB")["jobs"]["queued"]], ["203"])
        self.assertEqual([j["id"] for j in self.llm.queue(jobs="queued", partition="cpu", name="ML_")["jobs"]["queued"]], ["202"])
        self.assertEqual(self.llm.queue(jobs="queued", hpclib=True)["jobs"]["queued_matching"], 0)
        self.assertNotIn("running", self.llm.queue(jobs="queued")["jobs"])
        limited = self.llm.queue(jobs="queued", limit=1)["jobs"]
        self.assertEqual((len(limited["queued"]), limited["queued_matching"]), (1, 2))
        self.expect_error(422, self.llm.queue, "everything")

    def test_jobs_are_filtered_on_what_they_asked_sbatch_for(self):
        ids = lambda **kw: sorted(j["id"] for kind in ("running", "queued") for j in self.llm.queue(jobs="all", **kw)["jobs"].get(kind, []))
        self.assertEqual(self.llm.queue(jobs="all")["jobs"]["running"][0]["requests"], {"gres": "gpu:a100:2"})
        self.assertEqual(ids(resources="--gres=gpu:a100:2"), ["201"])
        self.assertEqual(ids(resources="gpus=1:a100"), [])                  # 201 asked with --gres, not --gpus
        self.assertEqual(ids(resources="a100"), ["201", "202"])             # a type, in any request or the features
        self.assertEqual(ids(resources="gpu:h100:4"), ["203"])
        self.assertEqual(ids(resources="gpu"), ["201", "202", "203"])
        self.assertEqual(ids(resources="constraint=v100"), ["202"])
        self.assertEqual(ids(resources="a100 constraint=v100"), ["202"])    # every term must match
        self.assertEqual(ids(resources="a100", user="alice", partition="gpu"), [])
        self.assertEqual(self.llm.queue(jobs="all", resources="a100")["jobs"]["queued_matching"], 1)

    def test_answers_are_cached_and_refresh_reads_again(self):
        self.llm.queue()
        self.llm.queue(jobs="all")
        self.assertEqual(len(self.reads), 1)
        self.llm.queue(refresh=True)
        self.assertEqual(len(self.reads), 2)

    def test_a_squeue_that_fails_is_an_error_not_an_empty_queue(self):
        self.answer = (0, "U|mark\nP|cpu|10/90/0/100|5\nE|Slurm RPC rate limit exceeded\n", "")
        out = self.llm.queue(jobs="all")
        self.assertEqual(out["error"], "Slurm RPC rate limit exceeded")
        self.assertIsNone(out["counts"]["running"])
        self.assertEqual(out["jobs"]["running"], [])
        self.answer = (1, "", "sbatch: command not found")
        self.server.cluster_queue._cache = None
        self.expect_error(502, self.llm.queue)
        self.answer = (0, QUEUE, "")                                                           # then it works, and is kept
        self.llm.queue(refresh=True)
        self.answer = (0, "U|mark\nE|busy\n", "")
        out = self.llm.queue(refresh=True)
        self.assertEqual((out["error"], out["stale"], out["counts"]["running"]), ("busy", True, 1))


if __name__ == "__main__":
    unittest.main()
