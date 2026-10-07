"""
A job runner for machines without SLURM: development servers that run
podman instead of Apptainer and have no scheduler. The REST server runs
on the machine itself (the cluster profile's rest_on: login) and runs
template jobs there, one after another or side by side within a CPU and
memory budget, each in the job sandbox (podman, see rest_sandbox).

It stands in for the part of sbatch, squeue/sacct, scancel and sinfo
that JobManager uses, so templates, resource limits, proposals, tokens
and the audit log work unchanged:

  - a job's resources (--cpus-per-task x --ntasks, --mem, --time) are
    what it reserves from the budget, and what it is held to: the time
    limit always (timeout), CPU and memory by podman, which can only
    enforce them where cgroups v2 delegates the cpu and memory
    controllers to you. Where they aren't, jobs are refused (with
    `enforce_limits`, the default) rather than run unlimited;
  - waiting jobs start in submission order (first in, first out; an array
    keeps to its throttle), while the REST server runs. Running jobs are
    separate processes and carry on if the server stops; it picks them
    up again when it restarts. Ask the admin for `loginctl enable-linger`
    so they also outlive your logins;
  - --partition, --account, --qos and --constraint mean nothing here and
    are ignored; more than one node, or GPUs through --gres, are refused
    (give the sandbox the device flags instead, for every job, e.g.
    "flags": ["--device", "nvidia.com/gpu=all"]);
  - jobs see SLURM_JOB_ID, SLURM_CPUS_PER_TASK, SLURM_ARRAY_TASK_ID and
    the like, as under SLURM, plus HPC_JOB_SCHEDULER=local.

Config, the `scheduler` key of the server's config.json:

  {"type": "local",              # "slurm" (the default) or "local"
   "cpus": null,                 # the CPUs jobs may use at once; default: all of this machine's
   "memory": null,               # the memory jobs may use at once, e.g. "64G"; default: 80% of this machine's
   "default_memory": "2G",       # a job that asks for none
   "default_time": "24:00:00",   # a job that asks for none
   "max_time": "7-00:00:00",
   "enforce_limits": true}       # refuse jobs whose CPU and memory limits can't be enforced;
                                 #   "memory": only memory must be (CPU use then isn't capped)

Standard library only.
"""
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time

try:
    from . import rest_jobs
    from .rest_jobs import RESTError, SlurmRunner
except ImportError:
    import rest_jobs
    from rest_jobs import RESTError, SlurmRunner

__all__ = ["LocalRunner", "SCHEDULER_TYPES", "check_scheduler_section"]

SCHEDULER_TYPES = ("slurm", "local")
CONFIG_KEYS = {"type", "cpus", "memory", "default_memory", "default_time", "max_time", "enforce_limits"}
# Above SLURM's default MaxJobId (67043328), so an id can't be confused with one from a SLURM era of the same data
FIRST_ID = 70_000_001
IGNORED = ("--partition", "--account", "--qos", "--constraint")
ARRAY_RE = re.compile(r"0-(\d+)(?:%(\d+))?")
OUTPUT_RE = re.compile(r"%(%|j|A|a|x|u)")

WRAPPER = r"""#!/bin/bash
# hpclib local scheduler: run one job (or array task) to its end, under its time limit, and record how it ended.
#   run-task.sh JOBDIR TASK SECONDS OUTPUT
jobdir="$1" task="$2" limit="$3" out="$4"
timeout -s TERM -k 30 "$limit" bash "$jobdir/script.sh" > "$out" 2>&1 < /dev/null &
child=$!
# scancel: the job's processes get TERM, as under SLURM (timeout passes it on to them)
trap 'kill -TERM "$child" 2>/dev/null' TERM INT HUP
while :; do
  wait "$child"
  rc=$?
  kill -0 "$child" 2>/dev/null || break
done
# a podman container that outlived its client (killed after the grace period)
if [ -n "${HPC_JOB_PODMAN:-}" ]; then
  "$HPC_JOB_PODMAN" rm -f -t 5 "hpclib-job-$HPC_JOB_TAG" > /dev/null 2>&1
fi
printf '%s %s\n' "$rc" "$(date +%s)" > "$jobdir/exit-$task.tmp" && mv -f "$jobdir/exit-$task.tmp" "$jobdir/exit-$task"
"""


def _meminfo_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _minutes(value, key):
    try:
        minutes = rest_jobs.parse_slurm_time(value)
    except ValueError:
        raise ValueError(f"scheduler `{key}` must be a SLURM time like 24:00:00 or 2-00:00:00, not {value!r}")
    if minutes == float("inf") or minutes <= 0:
        raise ValueError(f"scheduler `{key}` must be a finite time")
    return minutes


def check_scheduler_section(section):
    """The `scheduler` section, validated; None or {"type": "slurm"} mean SLURM."""
    if section is None:
        return {"type": "slurm"}
    if not isinstance(section, dict):
        raise ValueError("`scheduler` must be an object")
    unknown = set(section) - CONFIG_KEYS
    if unknown:
        raise ValueError(f"unknown `scheduler` keys {sorted(unknown)}; known: {sorted(CONFIG_KEYS)}")
    kind = section.get("type", "slurm")
    if kind not in SCHEDULER_TYPES:
        raise ValueError(f"scheduler `type` must be one of {', '.join(SCHEDULER_TYPES)}")
    if kind == "local":
        LocalSettings(section)
    return dict(section, type=kind)


class LocalSettings:
    def __init__(self, section):
        section = section or {}
        cpus = section.get("cpus")
        if cpus is not None and (isinstance(cpus, bool) or not isinstance(cpus, int) or cpus < 1):
            raise ValueError("scheduler `cpus` must be a positive whole number")
        self.cpus = cpus or os.cpu_count() or 1
        total = _meminfo_mb() or 4096
        memory = section.get("memory")
        try:
            self.memory_mb = rest_jobs.parse_mem(memory) if memory is not None else total * 0.8
            self.default_memory_mb = rest_jobs.parse_mem(section.get("default_memory", "2G"))
        except ValueError as e:
            raise ValueError(f"scheduler: {e}")
        self.default_minutes = _minutes(section.get("default_time", "24:00:00"), "default_time")
        self.max_minutes = _minutes(section.get("max_time", "7-00:00:00"), "max_time")
        self.enforce_limits = section.get("enforce_limits", True)
        if self.enforce_limits not in (True, False, "memory"):
            raise ValueError('scheduler `enforce_limits` must be true, false or "memory"')


def _slurm_time(seconds):
    seconds = max(0, int(seconds))
    d, rest = divmod(seconds, 86400)
    h, rest = divmod(rest, 3600)
    m, s = divmod(rest, 60)
    return f"{d}-{h:02d}:{m:02d}:{s:02d}" if d else f"{h}:{m:02d}:{s:02d}"


def _process_start(pid):
    """A process's start time (clock ticks since boot), to tell it from a later one with the same pid."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


class LocalRunner(SlurmRunner):
    """SlurmRunner's interface, for a machine without SLURM: jobs run here, in the order they came."""

    TERMINAL = SlurmRunner.TERMINAL_STATES
    SUBMITTER = "the local scheduler"

    def __init__(self, config, data_dir, sandbox, timeout=120, user=None, poll=2.0, start=True):
        super().__init__(timeout=timeout, user=user)
        self.settings = LocalSettings(config)
        self.sandbox = sandbox
        self.dir = os.path.join(data_dir, "local-jobs")
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        self.wrapper = os.path.join(self.dir, "run-task.sh")
        with open(self.wrapper + ".tmp", "w") as f:
            f.write(WRAPPER)
        os.chmod(self.wrapper + ".tmp", 0o700)
        os.replace(self.wrapper + ".tmp", self.wrapper)
        self.host = socket.gethostname().split(".")[0]
        self.poll = poll
        self.lock = threading.RLock()
        self.jobs = {}
        self.procs = {}          # (job id, task) -> Popen, for the tasks this server started
        self._load()
        self._stop = threading.Event()
        if start:
            threading.Thread(target=self._loop, name="hpclib-local-scheduler", daemon=True).start()

    # -- the job files ----------------------------------------------------------

    def _job_dir(self, job_id):
        return os.path.join(self.dir, str(job_id))

    def _save(self, job):
        path = os.path.join(self._job_dir(job["id"]), "job.json")
        with open(path + ".tmp", "w") as f:
            json.dump(job, f)
        os.replace(path + ".tmp", path)

    def _load(self):
        for name in os.listdir(self.dir):
            path = os.path.join(self.dir, name, "job.json")
            if name.isdigit() and os.path.isfile(path):
                try:
                    with open(path) as f:
                        self.jobs[name] = json.load(f)
                except (OSError, ValueError):
                    continue

    def _next_id(self):
        path = os.path.join(self.dir, "next_id")
        try:
            with open(path) as f:
                job_id = int(f.read().strip())
        except (OSError, ValueError):
            job_id = FIRST_ID
        job_id = max([job_id, FIRST_ID] + [int(j) + 1 for j in self.jobs])
        with open(path + ".tmp", "w") as f:
            f.write(f"{job_id + 1}\n")
        os.replace(path + ".tmp", path)
        return str(job_id)

    # -- what the job asks for ------------------------------------------------

    @staticmethod
    def _reject(message):
        return subprocess.CompletedProcess(["sbatch"], 1, "", f"sbatch: error: {message}\n")

    def limits(self):
        return self.sandbox.limits() if self.sandbox is not None else {
            "enforced": False, "reason": "jobs aren't sandboxed"}

    def _parse_sbatch(self, args):
        opts, flags = {}, set()
        for arg in args:
            if arg in ("--parsable", "--test-only"):
                flags.add(arg)
                continue
            key, sep, value = arg.partition("=")
            if not sep or not key.startswith("--"):
                raise ValueError(f"unsupported sbatch argument {arg!r}")
            opts[key] = value
        known = {"--job-name", "--comment", "--chdir", "--output", "--array", "--time", "--mem", "--cpus-per-task",
                 "--ntasks", "--nodes", "--gres"} | set(IGNORED)
        unknown = sorted(set(opts) - known)
        if unknown:
            raise ValueError(f"the local scheduler doesn't support {', '.join(unknown)}")
        nodes = opts.get("--nodes", "1")
        if nodes not in ("1", "1-1"):
            raise ValueError("jobs here run on this one machine; --nodes must be 1")
        if opts.get("--gres"):
            raise ValueError("GPUs aren't scheduled here; give the sandbox the device flags instead, e.g. "
                             '"flags": ["--device", "nvidia.com/gpu=all"] in the sandbox config')
        try:
            per_task = int(opts.get("--cpus-per-task", "1"))
            ntasks = int(opts.get("--ntasks", "1"))
        except ValueError:
            raise ValueError("--cpus-per-task and --ntasks must be whole numbers")
        if per_task < 1 or ntasks < 1:
            raise ValueError("--cpus-per-task and --ntasks must be at least 1")
        s = self.settings
        mem_mb = rest_jobs.parse_mem(opts["--mem"]) if opts.get("--mem") else s.default_memory_mb
        minutes = rest_jobs.parse_slurm_time(opts["--time"]) if opts.get("--time") else s.default_minutes
        if minutes > s.max_minutes:
            raise ValueError(f"--time {opts['--time']} is longer than this machine's max_time "
                             f"({_slurm_time(s.max_minutes * 60)})")
        cpus = per_task * ntasks
        if cpus > s.cpus:
            raise ValueError(f"the job asks for {cpus} CPUs; jobs here may use {s.cpus} at most")
        if mem_mb > s.memory_mb:
            raise ValueError(f"the job asks for {int(mem_mb)} MB of memory; jobs here may use "
                             f"{int(s.memory_mb)} MB at most")
        array = None
        if opts.get("--array"):
            m = ARRAY_RE.fullmatch(opts["--array"])
            if not m:
                raise ValueError(f"unsupported --array {opts['--array']!r} (only 0-N%T)")
            size = int(m.group(1)) + 1
            array = {"size": size, "throttle": min(int(m.group(2) or size), size)}
        return {"name": opts.get("--job-name", "job"), "comment": opts.get("--comment", ""),
                "chdir": opts.get("--chdir"), "output": opts.get("--output", "slurm-%j.out"), "array": array,
                "cpus": cpus, "cpus_per_task": per_task, "ntasks": ntasks, "mem_mb": int(mem_mb),
                "seconds": int(minutes * 60), "ignored": {k: opts[k] for k in IGNORED if opts.get(k)}}, flags

    def _sbatch(self, args, script, cwd):
        try:
            spec, flags = self._parse_sbatch(args)
        except ValueError as e:
            return self._reject(str(e))
        workdir = spec["chdir"] or cwd or os.getcwd()
        if not os.path.isdir(workdir):
            return self._reject(f"--chdir {workdir} is not a directory")
        limits = self.limits()
        enforce = self.settings.enforce_limits
        if enforce == "memory" and not limits.get("memory"):
            return self._reject(f"this machine can't hold the job to its memory limit: {limits['reason']}. "
                                f"Jobs are refused rather than run unlimited; the server owner may set "
                                f'"enforce_limits": false in the scheduler config to run them anyway')
        if enforce is True and not limits["enforced"]:
            hint = ('; "memory" enforces only memory, which this machine can' if limits.get("memory") else "")
            return self._reject(f"this machine can't hold the job to its CPU and memory limits: {limits['reason']}. "
                                f"Jobs are refused rather than run unlimited; the server owner may set "
                                f'"enforce_limits" in the scheduler config to false (run them anyway){hint}')
        if "--test-only" in flags:
            with self.lock:
                running = sum(1 for j in self.jobs.values() for t in j["tasks"].values() if t["state"] == "RUNNING")
                waiting = sum(1 for j in self.jobs.values() for t in j["tasks"].values() if t["state"] == "PENDING")
            notes = f"; ignored here: {' '.join(f'{k}={v}' for k, v in spec['ignored'].items())}" if spec["ignored"] else ""
            return subprocess.CompletedProcess(["sbatch"], 0, "", (
                f"sbatch: local scheduler on {self.host}: would run with {spec['cpus']} CPUs, {spec['mem_mb']} MB, "
                f"time limit {_slurm_time(spec['seconds'])} ({running} running, {waiting} waiting; limits "
                f"{'enforced' if limits['enforced'] else 'NOT enforced: ' + limits['reason']}){notes}\n"))
        if not script:
            return self._reject("no job script on standard input")
        with self.lock:
            job_id = self._next_id()
            d = self._job_dir(job_id)
            os.makedirs(d, mode=0o700)
            with open(os.path.join(d, "script.sh"), "w") as f:
                f.write(script)
            os.chmod(os.path.join(d, "script.sh"), 0o700)
            size = spec["array"]["size"] if spec["array"] else 1
            job = dict(spec, id=job_id, workdir=workdir, submitted=time.time(),
                       tasks={str(i): {"state": "PENDING", "reason": "Priority"} for i in range(size)})
            self.jobs[job_id] = job
            self._save(job)
            self._tick()
        return subprocess.CompletedProcess(["sbatch"], 0, f"{job_id}\n", "")

    # -- running them ---------------------------------------------------------

    def _output(self, job, task):
        user = self.user
        subs = {"%": "%", "j": job["id"], "A": job["id"], "a": str(task), "x": job["name"], "u": user}
        path = OUTPUT_RE.sub(lambda m: subs[m.group(1)], job["output"])
        return path if os.path.isabs(path) else os.path.join(job["workdir"], path)

    def _env(self, job, task):
        env = rest_jobs.clean_env()
        for key in list(env):
            if key.startswith(("SLURM_", "SBATCH_")):
                del env[key]
        env.update(SLURM_JOB_ID=job["id"], SLURM_JOBID=job["id"], SLURM_JOB_NAME=job["name"],
                   SLURM_SUBMIT_DIR=job["workdir"], SLURM_CPUS_PER_TASK=str(job["cpus_per_task"]),
                   SLURM_NTASKS=str(job["ntasks"]), SLURM_JOB_NODELIST=self.host,
                   HPC_JOB_SCHEDULER="local", HPC_JOB_CPUS=str(job["cpus"]), HPC_JOB_MEMORY=f"{job['mem_mb']}m",
                   HPC_JOB_TAG=f"{job['id']}-{task}")
        if job["array"]:
            env.update(SLURM_ARRAY_JOB_ID=job["id"], SLURM_ARRAY_TASK_ID=str(task),
                       SLURM_ARRAY_TASK_COUNT=str(job["array"]["size"]), SLURM_ARRAY_TASK_MIN="0",
                       SLURM_ARRAY_TASK_MAX=str(job["array"]["size"] - 1))
        try:
            method, runtime = self.sandbox.resolve() if self.sandbox is not None else ("none", None)
        except Exception:
            method, runtime = "none", None
        if method == "podman":
            env["HPC_JOB_PODMAN"] = runtime
            try:
                conf = self.sandbox.podman_storage_conf()
            except Exception:
                conf = None
            if conf:  # so the clean-up's `podman rm` finds the job's container
                env["CONTAINERS_STORAGE_CONF"] = conf
        return env

    def _start(self, job, task):
        t = job["tasks"][str(task)]
        try:
            proc = subprocess.Popen(
                ["bash", self.wrapper, self._job_dir(job["id"]), str(task), str(job["seconds"]), self._output(job, task)],
                cwd=job["workdir"], env=self._env(job, task), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            t.update(state="FAILED", reason=f"could not start: {e}", start=time.time(), end=time.time())
            return
        self.procs[(job["id"], str(task))] = proc
        t.update(state="RUNNING", reason="None", pid=proc.pid, pid_start=_process_start(proc.pid), start=time.time())

    def _finish(self, job, task):
        """Update a running task from its exit file, or notice that it was lost. True if it changed."""
        t = job["tasks"][str(task)]
        proc = self.procs.get((job["id"], str(task)))
        if proc is not None:
            proc.poll()   # reaps it
        path = os.path.join(self._job_dir(job["id"]), f"exit-{task}")
        try:
            with open(path) as f:
                rc, end = f.read().split()
            rc, end = int(rc), float(end)
        except (OSError, ValueError):
            if self._alive(t, proc):
                return False
            # gone without a word (killed, or the machine restarted): give the exit file a moment
            if time.time() - t.get("lost_since", time.time()) < 5:
                t.setdefault("lost_since", time.time())
                return False
            t.update(state="FAILED", reason="the job's process ended without recording how", end=time.time(),
                     exit=None)
            self.procs.pop((job["id"], str(task)), None)
            return True
        self.procs.pop((job["id"], str(task)), None)
        elapsed = end - t.get("start", end)
        if t.get("cancel"):
            state = "CANCELLED"
        elif rc in (124, 137) and elapsed >= job["seconds"] - 2:
            state = "TIMEOUT"
        else:
            state = "COMPLETED" if rc == 0 else "FAILED"
        t.update(state=state, exit=rc, end=end, reason="None" if state == "COMPLETED" else f"exit {rc}")
        return True

    @staticmethod
    def _alive(t, proc=None):
        if proc is not None:   # started by this server: its own child
            return proc.returncode is None
        return bool(t.get("pid")) and t.get("pid_start") is not None and _process_start(t["pid"]) == t["pid_start"]

    def _tick(self):
        with self.lock:
            changed = set()
            used_cpus = used_mem = 0
            for job in self.jobs.values():
                for task, t in job["tasks"].items():
                    if t["state"] == "RUNNING" and self._finish(job, task):
                        changed.add(job["id"])
                    if t["state"] == "RUNNING":
                        used_cpus += job["cpus"]
                        used_mem += job["mem_mb"]
            blocked = False
            for job_id in sorted(self.jobs, key=int):
                job = self.jobs[job_id]
                pending = [k for k, t in job["tasks"].items() if t["state"] == "PENDING"]
                if not pending:
                    continue
                running = sum(1 for t in job["tasks"].values() if t["state"] == "RUNNING")
                throttle = job["array"]["throttle"] if job["array"] else 1
                for task in sorted(pending, key=int):
                    t = job["tasks"][task]
                    if blocked:
                        reason = "Priority"
                    elif running >= throttle:
                        reason = "JobArrayTaskLimit"
                    elif used_cpus + job["cpus"] > self.settings.cpus or used_mem + job["mem_mb"] > self.settings.memory_mb:
                        reason, blocked = "Resources", True   # first in, first out: nothing passes a waiting job
                    else:
                        self._start(job, task)
                        running += 1
                        used_cpus += job["cpus"]
                        used_mem += job["mem_mb"]
                        changed.add(job_id)
                        continue
                    if t.get("reason") != reason:
                        t["reason"] = reason
                        changed.add(job_id)
            for job_id in changed:
                self._save(self.jobs[job_id])

    def _loop(self):
        while not self._stop.wait(self.poll):
            try:
                self._tick()
            except Exception as e:   # keep scheduling whatever one job did
                print(f"hpclib local scheduler: {e!r}", flush=True)

    def stop(self):
        self._stop.set()

    def _scancel(self, args):
        ids = [a for a in args if not a.startswith("-")]
        with self.lock:
            for job_id in ids:
                job = self.jobs.get(job_id.split("_")[0])
                if job is None:
                    return subprocess.CompletedProcess(["scancel"], 1, "", f"scancel: error: Invalid job id {job_id}\n")
                for key, t in job["tasks"].items():
                    if t["state"] == "PENDING":
                        t.update(state="CANCELLED", reason="None", end=time.time())
                    elif t["state"] == "RUNNING" and t.get("pid"):
                        t["cancel"] = True
                        proc = self.procs.get((job["id"], key))
                        if proc is not None:
                            proc.poll()
                        if self._alive(t, proc):
                            try:
                                os.kill(t["pid"], signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                self._save(job)
        return subprocess.CompletedProcess(["scancel"], 0, "", "")

    # -- what squeue, sacct and sinfo would say -----------------------------------

    def _row(self, job, task):
        t = job["tasks"][str(task)]
        start, end = t.get("start"), t.get("end")
        elapsed = ((end or time.time()) - start) if start else 0
        return {"state": t["state"], "exit_code": "" if t.get("exit") is None else f"{t['exit']}:0",
                "elapsed": _slurm_time(elapsed), "reason": t.get("reason", ""),
                "nodes": self.host if start else ""}

    def query_states(self, job_ids, arrays=None):
        self._tick()
        arrays = {str(k): int(v) for k, v in (arrays or {}).items()}
        result = {}
        with self.lock:
            for job_id in [str(j) for j in job_ids if str(j) not in arrays]:
                job = self.jobs.get(job_id)
                if job is None:
                    result[job_id] = {"job_id": job_id, "state": "UNKNOWN", "source": None, "terminal": False}
                    continue
                row = dict(self._row(job, 0), job_id=job_id, name=job["name"], partition="local",
                           time_limit=_slurm_time(job["seconds"]), source="local")
                row["terminal"] = row["state"] in self.TERMINAL
                result[job_id] = row
            for job_id, n in arrays.items():
                job = self.jobs.get(job_id)
                per_task = {i: (self._row(job, i) if job and str(i) in job["tasks"] else {"state": "UNKNOWN"})
                            for i in range(n)}
                result[job_id] = dict(self.summarize(per_task), job_id=job_id, tasks=per_task)
        return result

    def _sinfo(self, args):
        if "--version" in args:
            return subprocess.CompletedProcess(["sinfo"], 0, "hpclib local scheduler (no SLURM)\n", "")
        s = self.settings
        row = f"local*|up|{_slurm_time(s.max_minutes * 60)}|1|{s.cpus}|{int(s.memory_mb)}|(null)\n"
        return subprocess.CompletedProcess(["sinfo"], 0, row, "")

    def run(self, args, input=None, cwd=None):
        command = os.path.basename(args[0]) if args else ""
        if command == "sbatch":
            return self._sbatch(list(args[1:]), input, cwd)
        if command == "scancel":
            return self._scancel(list(args[1:]))
        if command == "sinfo":
            return self._sinfo(list(args[1:]))
        if command in ("squeue", "sacct", "sacctmgr", "scontrol", "srun", "salloc"):
            return subprocess.CompletedProcess(list(args), 1, "", f"{command}: this machine has no SLURM; "
                                                                  f"jobs run on the local scheduler\n")
        return super().run(args, input=input, cwd=cwd)

    def describe(self):
        s = self.settings
        with self.lock:
            counts = {}
            for job in self.jobs.values():
                for t in job["tasks"].values():
                    counts[t["state"]] = counts.get(t["state"], 0) + 1
        return {"type": "local", "host": self.host, "cpus": s.cpus, "memory_mb": int(s.memory_mb),
                "default_memory_mb": int(s.default_memory_mb), "default_time": _slurm_time(s.default_minutes * 60),
                "max_time": _slurm_time(s.max_minutes * 60), "enforce_limits": s.enforce_limits,
                "limits": self.limits(), "tasks": counts}
