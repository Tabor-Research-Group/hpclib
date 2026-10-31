"""The Workspace's job spec, its script generator and the cluster parsing behind them: golden scripts that bash accepts,
TERM forwarded to the program by a real bash, quoting that holds against hostile values, the spec round trip."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TUNNEL = ROOT / "packages" / "workspace" / "tunnels" / "workspace"
sys.path.insert(0, str(ROOT / "hpclib" / "servers"))
sys.path.insert(0, str(TUNNEL))
import clusterinfo  # noqa: E402
from ws import jobspec  # noqa: E402

SINFO = "gpu*|up|2-00:00:00|4|64|257000|gpu:a100:4(S:0-1),tmpfs:10G\ncpu|up|7-00:00:00|100|48|190000|(null)\n"


class TestClusterInfo(unittest.TestCase):
    def test_times_and_memory(self):
        for text, minutes in (("6h", 360), ("1d 2h", 1560), ("90m", 90), ("1h30m", 90), ("6:00:00", 360),
                              ("1-02:00:00", 1560), ("1-02", 1560), ("45", 45)):
            self.assertEqual(clusterinfo.parse_time(text), minutes, text)
        self.assertIsNone(clusterinfo.parse_time("UNLIMITED"))
        self.assertEqual(clusterinfo.format_time(1560), "1-02:00:00")
        self.assertEqual(clusterinfo.format_time(90), "1:30:00")
        for bad in ("", "soon", "6x", "1h banana"):
            with self.assertRaises(ValueError):
                clusterinfo.parse_time(bad)
        self.assertEqual(clusterinfo.parse_mem("8G"), 8192)
        self.assertEqual(clusterinfo.format_mem(clusterinfo.parse_mem("8 GB")), "8G")
        self.assertEqual(clusterinfo.format_mem(500), "500M")

    def test_sinfo_gres_and_test_only(self):
        parts = clusterinfo.parse_sinfo(SINFO)
        self.assertEqual([p["name"] for p in parts], ["gpu", "cpu"])
        self.assertTrue(parts[0]["default"])
        self.assertEqual(parts[0]["gpus"], [{"type": "a100", "count": 4}])
        self.assertEqual(parts[0]["time_limit_minutes"], 2880)
        self.assertEqual(clusterinfo.parse_gres("gpu:2"), [{"type": "", "count": 2}])
        ok = clusterinfo.parse_test_only("sbatch: Job 12 to start at 2099-01-01T00:00:00 using 4 processors on nodes c1 in partition gpu")
        self.assertTrue(ok["ok"])
        bad = clusterinfo.parse_test_only("sbatch: error: Batch job submission failed: Invalid account or account/partition combination specified")
        self.assertFalse(bad["ok"])
        self.assertIn("Invalid account", bad["message"])

    def test_collect_reports_a_missing_slurm(self):
        info = clusterinfo.collect(lambda argv: (127, "sinfo: command not found"), "me")
        self.assertIn("not found", info["slurm_problem"])


SPEC = {"workdir": "/home/me/proj", "kind": "script", "command": "/home/me/proj/train.py", "arguments": "--epochs 3 'two words'",
        "resources": {"time": "6h", "memory": "8G", "cpus": 4, "gpus": 1, "gpu_type": "a100", "partition": "gpu"},
        "modules": ["GCC/13.3.0"]}
CLUSTER = {"partitions": clusterinfo.parse_sinfo(SINFO), "hpclib": "/home/me/hpclib/hpclib.sh"}


def bash_n(text):
    return subprocess.run(["bash", "-n"], input=text, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class TestRender(unittest.TestCase):
    def test_script_is_valid_bash_and_has_the_directives(self):
        script = jobspec.render(SPEC, CLUSTER, None)
        self.assertEqual(bash_n(script).returncode, 0, bash_n(script).stderr)
        for line in ("#SBATCH --time=6:00:00", "#SBATCH --mem=8G", "#SBATCH --gres=gpu:a100:1", "#SBATCH --partition=gpu",
                     "#SBATCH --cpus-per-task=4", "#SBATCH --signal=B:TERM@60", "trap hpc_term TERM"):
            self.assertIn(line, script)
        self.assertIn("python -u /home/me/proj/train.py --epochs 3 'two words'", script)

    def test_round_trip(self):
        script = jobspec.render(SPEC, CLUSTER, None)
        spec, errors, _ = jobspec.validate(jobspec.embedded_spec(script))
        self.assertEqual(errors, [])
        self.assertEqual(jobspec.render(spec, CLUSTER, None), script)
        self.assertIsNone(jobspec.embedded_spec("#!/bin/bash\necho hi\n"))

    def test_hostile_values_stay_data(self):
        spec = dict(SPEC, command="/tmp/a b; touch /tmp/pwned.py", arguments="'$(touch /tmp/pwned)' `x` ; y",
                    workdir="/tmp/my dir")
        script = jobspec.render(spec, CLUSTER, None)
        self.assertEqual(bash_n(script).returncode, 0)
        self.assertNotIn("\n#SBATCH --chdir=/tmp/my dir", script)
        for bad in ({"name": "a;b"}, {"resources": {"partition": "x\n#SBATCH --wrap=id"}}, {"extra_sbatch": ["rm -rf /"]},
                    {"modules": ["x;y"]}, {"workdir": "relative"}, {"command": ""}):
            with self.assertRaises(jobspec.SpecError, msg=str(bad)):
                jobspec.render(dict(SPEC, **bad), CLUSTER, None)

    def test_warnings_from_the_cluster(self):
        _, errors, warnings = jobspec.validate(dict(SPEC, resources={"time": "5d", "partition": "gpu", "gpus": 8}), CLUSTER)
        self.assertEqual(errors, [])
        self.assertTrue(any("at most" in w for w in warnings))
        self.assertTrue(any("GPUs per node" in w for w in warnings))
        _, _, warnings = jobspec.validate(dict(SPEC, resources={"partition": "nope"}), CLUSTER)
        self.assertTrue(any("nope" in w for w in warnings))

    def test_environment_prelude_and_sync_mode(self):
        env = {"manager": "uv", "project": "/home/me/proj", "path": "/home/me/proj/.venv", "ready": True, "environment": "default"}
        script = jobspec.render(SPEC, CLUSTER, env)
        self.assertIn("export VIRTUAL_ENV=/home/me/proj/.venv", script)
        sync = jobspec.render(dict(SPEC, environment={"mode": "sync"}), CLUSTER, env)
        self.assertIn("uv run --project /home/me/proj python -u /home/me/proj/train.py", sync)
        self.assertNotIn("VIRTUAL_ENV", sync)

    def test_term_reaches_the_program(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "prog.sh").write_text('trap \'echo got-term > "$1"; exit 0\' TERM\necho started > "$1.started"\nwhile :; do sleep 0.1; done\n')
        flag = tmp / "flag"
        spec = {"workdir": str(tmp), "kind": "script", "command": str(tmp / "prog.sh"), "arguments": str(flag),
                "environment": {"shell_init": "none"}}
        script = tmp / "job.sh"
        script.write_text(jobspec.render(spec, {"hpclib": str(tmp / "absent.sh")}, None))
        proc = subprocess.Popen(["bash", str(script)], cwd=str(tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(100):
            if (tmp / "flag.started").exists():
                break
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        self.assertEqual(flag.read_text().strip(), "got-term")


if __name__ == "__main__":
    unittest.main()


class TestFanOut(unittest.TestCase):
    """Arrays, sweeps and dependencies: the directives, and a sweep run by a real bash task by task."""

    BASE = {"workdir": "/tmp", "kind": "script", "command": "/tmp/a.sh", "environment": {"mode": "none", "shell_init": "none"}}

    def test_directives_and_the_defaults_they_change(self):
        script = jobspec.render(dict(self.BASE, array={"range": "1-10:3", "limit": 4}, dependency={"type": "afternotok", "jobs": "5, 6"}))
        for line in ("#SBATCH --array=1-10:3%4", "#SBATCH --dependency=afternotok:5:6", "#SBATCH --kill-on-invalid-dep=yes",
                     "#SBATCH --output=%x-%A_%a.out"):
            self.assertIn(line, script)
        self.assertEqual(bash_n(script).returncode, 0)
        plain = jobspec.validate(self.BASE)[0]
        self.assertTrue(not {"array", "sweep", "dependency"} & set(plain))            # nothing added to a plain spec
        self.assertEqual(plain["output"], "%x-%j.out")
        self.assertEqual(jobspec.task_count({"array": {"range": "1-10:3,20"}}), 5)
        self.assertEqual(jobspec.first_task({"array": {"range": "5-9"}}), 5)

    def test_what_is_refused(self):
        for key, value in (("array", {"range": "1-x"}), ("dependency", {"jobs": ["1;rm"]}), ("dependency", {"type": "never", "jobs": ["1"]}),
                           ("sweep", {"A": ["a b"]}), ("sweep", {"SLURM_X": ["1"]}), ("sweep", {"PATH": ["1"]}), ("sweep", {"B": []}),
                           ("sweep", {"C": ["$(id)"]}), ("sweep", {"D": ["HPCLIB_PARAMS_END"]})):
            self.assertTrue(jobspec.validate(dict(self.BASE, **{key: value}))[1], (key, value))
        many = {"A": [str(i) for i in range(101)], "B": [str(i) for i in range(11)]}
        _, errors, warnings = jobspec.validate(dict(self.BASE, sweep=many))
        self.assertEqual(errors, [])
        self.assertTrue(any("usually" in w for w in warnings))

    def test_each_task_of_a_sweep_gets_its_row(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.sh").write_text('echo "lr=$LR seed=$SEED args: $*"\n')
            spec = dict(self.BASE, workdir=d, command=d + "/a.sh", arguments="--lr {LR} --tag 'x {SEED}.pt' '$HOME {LR}'",
                        sweep={"LR": ["0.1", "0.01"], "SEED": ["1", "2"]})
            script = jobspec.render(spec)
            self.assertEqual(bash_n(script).returncode, 0, bash_n(script).stderr)
            Path(d, "job.sh").write_text(script)
            seen = []
            for n in range(4):
                done = subprocess.run(["bash", d + "/job.sh"], cwd=d, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env={"SLURM_ARRAY_TASK_ID": str(n), "PATH": os.environ["PATH"], "HOME": "/h"})
                seen.append([l for l in done.stdout.splitlines() if l.startswith("lr=")][0])
            self.assertEqual(seen, ["lr=0.1 seed=1 args: --lr 0.1 --tag x 1.pt $HOME 0.1", "lr=0.1 seed=2 args: --lr 0.1 --tag x 2.pt $HOME 0.1",
                                    "lr=0.01 seed=1 args: --lr 0.01 --tag x 1.pt $HOME 0.01", "lr=0.01 seed=2 args: --lr 0.01 --tag x 2.pt $HOME 0.01"])
        spec2, errors, _ = jobspec.validate(jobspec.embedded_spec(script))
        self.assertEqual((errors, jobspec.render(spec2)), ([], script))                 # and the round trip holds


HAND_MADE = """#!/bin/bash
#SBATCH -J myjob
#SBATCH --time=02:00:00 --nodes=2  # two nodes
#SBATCH --ntasks-per-node 4
#SBATCH --mem=16G
#SBATCH --gres=gpu:a100:2
#SBATCH -p gpu
#SBATCH --array=1-20%5
#SBATCH --dependency=afterok:11:12
#SBATCH --mail-type=END
#SBATCH --constraint=intel
# a note
module load GCC
cd $SLURM_SUBMIT_DIR
python run.py --x 1
"""


class TestHandWritten(unittest.TestCase):
    """A script written by hand: read into a spec, kept verbatim, written back; and the interactive line."""

    def test_the_sbatch_lines_become_fields_and_the_rest_is_kept(self):
        spec, warnings = jobspec.parse_script(HAND_MADE, "/home/me/proj", "fallback")
        self.assertEqual(warnings, [])
        res = spec["resources"]
        self.assertEqual((spec["name"], spec["kind"], spec["workdir"], res["time"], res["nodes"], res["ntasks"], res["memory"]),
                         ("myjob", "body", "/home/me/proj", "02:00:00", "2", "4", "16G"))
        self.assertEqual((res["gpus"], res["gpu_type"], res["partition"]), ("2", "a100", "gpu"))
        self.assertEqual((spec["array"], spec["dependency"]), ({"range": "1-20", "limit": 5}, {"type": "afterok", "jobs": ["11", "12"]}))
        self.assertEqual(spec["extra_sbatch"], ["#SBATCH --mail-type=END", "#SBATCH --constraint=intel"])
        self.assertEqual(spec["body"], "# a note\nmodule load GCC\ncd $SLURM_SUBMIT_DIR\npython run.py --x 1\n")
        clean, errors, _ = jobspec.validate(spec)
        self.assertEqual(errors, [])
        script = jobspec.render(spec)
        self.assertEqual(bash_n(script).returncode, 0, bash_n(script).stderr)
        for line in ("#SBATCH --job-name=myjob", "#SBATCH --time=2:00:00", "#SBATCH --gres=gpu:a100:2", "#SBATCH --array=1-20%5",
                     "#SBATCH --mail-type=END", "python run.py --x 1"):
            self.assertIn(line, script)
        self.assertNotIn("--signal", script)                          # nothing to forward TERM to: it is the person's script
        again, errors, _ = jobspec.validate(jobspec.embedded_spec(script))
        self.assertEqual((errors, jobspec.render(again)), ([], script))

    def test_other_ways_of_writing_the_options(self):
        spec, warnings = jobspec.parse_script("#!/bin/sh\n#SBATCH -N 3 -t 30 -J\"a b\"\n#SBATCH --gpus=a100:1\n#SBATCH --gres=gpu:2\n"
                                              "#SBATCH --mem-per-cpu=2G\n#SBATCH --dependency=singleton\n#SBATCH --export=ALL\n\necho hi\n", "/w")
        res = spec["resources"]
        self.assertEqual((res["nodes"], res["time"], res["gpus"], res["gpu_type"], res["memory_per"]), ("3", "30", "1", "a100", "cpu"))
        self.assertEqual(spec["name"], "job")                                           # "a b" isn't a name the form takes
        self.assertEqual(spec["extra_sbatch"], ["#SBATCH --job-name='a b'", "#SBATCH --gres=gpu:2", "#SBATCH --dependency=singleton", "#SBATCH --export=ALL"])
        self.assertTrue(any("#!/bin/sh" in w for w in warnings))
        self.assertNotIn("dependency", spec)
        self.assertEqual(spec["body"], "echo hi\n")
        body_only, warnings = jobspec.parse_script("echo hi\n", "/w", "x")
        self.assertTrue(any("--time" in w for w in warnings))
        self.assertEqual(jobspec.validate(body_only)[1], [])

    def test_a_script_with_no_body_is_refused(self):
        spec, _ = jobspec.parse_script("#!/bin/bash\n#SBATCH -N 1\n", "/w", "x")
        self.assertTrue(jobspec.validate(spec)[1])

    def test_the_interactive_line(self):
        line, warnings = jobspec.interactive_command({"workdir": "/x", "name": "bash", "resources": {"time": "2h", "gpus": 1, "gpu_type": "a100", "memory": "8G", "partition": "gpu"}})
        self.assertEqual(line, "srun --job-name=interactive --time=2:00:00 --nodes=1 --ntasks-per-node=1 --mem=8G --gres=gpu:a100:1 --partition=gpu --pty bash -il")
        line, _ = jobspec.interactive_command({"workdir": "/x", "name": "x", "resources": {}, "modules": ["GCC/13.3.0", "CUDA/12.6.0"]})
        self.assertTrue(line.endswith("--pty bash -lc 'module load GCC/13.3.0 CUDA/12.6.0; exec bash -il'"), line)
        with self.assertRaises(jobspec.SpecError):
            jobspec.interactive_command({"workdir": "/x", "resources": {"time": "soon"}})
