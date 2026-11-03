"""
fsruns: where the probes run, and the record of each run.

A run is one request to measure: the login node (a local process, so a call stuck in the file system can't wedge this
server), and/or compute nodes (one small SLURM job per node asked for, or one on any node), each running fsprobe.py with
the same places and settings. A compute job carries the probe's source in its own batch script (SLURM keeps a copy of
the script on the node), so starting it doesn't read anything from the file system that may be the slow thing; it
writes its result to the run's folder when it is done.

  $HPCTUNNELS_DATA_DIR/fsdiag/runs/ID/meta.json      the run: what was asked, and each target's state
                                    login.json, node-NAME.json, any.json    a target's result (fsprobe's JSON)
                                    KEY.sbatch, KEY.slurm.out, login.log    the job script and what the jobs printed

  Runs(state_dir, probe_path, shell).start(spec)   -> the run (validates the spec: Refused for what it won't do)
  Runs(...).get(id) / .list() / .cancel(id) / .results(run)
  Slurm(shell).nodes() / .mine()                  what the page offers to target

Python 3.6 or newer, standard library only.
"""
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time

MARKER = "HPCLIB_FSPROBE_SOURCE_END"
MAX_NODES = 16
MAX_PATHS = 12
FINAL = ("done", "failed", "cancelled")


class Refused(Exception):
    """A request that can't be done: `status` is the HTTP status, the message its text."""

    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status


def data_dir():
    return os.path.expanduser(os.environ.get("HPCTUNNELS_DATA_DIR") or "~/.local/tunnels")


def write_json(path, value):
    tmp = path + ".part"
    with open(tmp, "w") as f:
        json.dump(value, f)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------------------------------------ the login shell

class Shell:
    """Programs run with the login shell's environment (read once): sites put sbatch and squeue on the PATH there."""

    def __init__(self, shell="bash"):
        self.shell, self._env, self._lock = shell, None, threading.Lock()

    def env(self, refresh=False):
        with self._lock:
            if refresh or self._env is None:
                self._env = self._capture()
            return dict(self._env)

    def _capture(self):
        try:
            res = subprocess.run([self.shell, "-lc", 'printf "HPCLIB_ENV_BEGIN\\0"; env -0'], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=60)
            at = res.stdout.rfind(b"HPCLIB_ENV_BEGIN\0")
            if at >= 0:
                found = {}
                for item in res.stdout[at + 17:].split(b"\0"):
                    name, eq, value = item.decode("utf-8", "replace").partition("=")
                    if eq and name.isidentifier():
                        found[name] = value
                if found.get("PATH"):
                    found.pop("FSDIAG_TOKEN", None)
                    return found
        except (OSError, subprocess.SubprocessError):
            pass
        return {k: v for k, v in os.environ.items() if k != "FSDIAG_TOKEN"}

    def run(self, argv, cwd=None, timeout=60):
        """(code, output); 127 when the program isn't there, 124 when it took longer than `timeout`."""
        for attempt in (0, 1):
            try:
                res = subprocess.run(argv, cwd=cwd, env=self.env(refresh=attempt == 1), stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, universal_newlines=True, timeout=timeout)
                return res.returncode, res.stdout or ""
            except FileNotFoundError:
                if attempt == 0:
                    continue
                return 127, "{}: command not found".format(argv[0])
            except subprocess.TimeoutExpired:
                return 124, "{} took over {} s".format(argv[0], timeout)
        return 127, "{}: command not found".format(argv[0])


# --------------------------------------------------------------------------------------------------------- SLURM

def expand_hostlist(text):
    """node[01-03,05],other -> [node01, node02, node03, node05, other]."""
    parts, depth, current = [], 0, ""
    for ch in text:
        depth += (ch == "[") - (ch == "]")
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    out = []
    for part in parts:
        m = re.fullmatch(r"(.*)\[([^\]]+)\](.*)", part)
        if not m:
            if part:
                out.append(part)
            continue
        prefix, body, suffix = m.groups()
        for item in body.split(","):
            if "-" in item:
                lo, hi = item.split("-", 1)
                if lo.isdigit() and hi.isdigit() and int(hi) - int(lo) < 5000:
                    out += ["{}{:0{}d}{}".format(prefix, n, len(lo), suffix) for n in range(int(lo), int(hi) + 1)]
            else:
                out.append(prefix + item + suffix)
    return out


class Slurm:
    """What the page offers to target: the cluster's nodes and the nodes the user's own jobs are on."""

    def __init__(self, shell, user):
        self.shell, self.user = shell, user
        self._cache, self._lock = None, threading.Lock()

    def nodes(self, refresh=False):
        """{slurm, partitions: [{name, default}], nodes: [{name, partitions, state, cpus, memory_mb}]}; cached a minute."""
        with self._lock:
            if self._cache and not refresh and time.time() - self._cache[0] < 60:
                return self._cache[1]
        code, out = self.shell.run(["sinfo", "-h", "-N", "-o", "%N|%P|%t|%c|%m"], timeout=30)
        if code != 0:
            found = {"slurm": code != 127, "partitions": [], "nodes": [], "error": out.strip()[:300] if code != 127 else None}
        else:
            nodes, partitions = {}, {}
            for line in out.splitlines():
                f = line.strip().split("|")
                if len(f) < 5:
                    continue
                name, part, state, cpus, mem = f[0], f[1], f[2], f[3], f[4]
                partitions[part.rstrip("*")] = part.endswith("*")
                n = nodes.setdefault(name, {"name": name, "partitions": [], "state": state.rstrip("*~#"), "cpus": cpus, "memory_mb": mem})
                if part.rstrip("*") not in n["partitions"]:
                    n["partitions"].append(part.rstrip("*"))
            found = {"slurm": True, "partitions": [{"name": k, "default": v} for k, v in sorted(partitions.items())],
                     "nodes": sorted(nodes.values(), key=lambda n: n["name"])}
        with self._lock:
            self._cache = (time.time(), found)
        return found

    def mine(self):
        """[{job_id, name, state, nodes}] of this user's running jobs: where a tunnel's server is, for one."""
        code, out = self.shell.run(["squeue", "-h", "-u", self.user, "-t", "RUNNING", "-o", "%i|%j|%T|%N"], timeout=30)
        jobs = []
        if code == 0:
            for line in out.splitlines():
                f = line.strip().split("|")
                if len(f) >= 4 and f[3]:
                    jobs.append({"job_id": f[0], "name": f[1], "state": f[2].lower(), "nodes": expand_hostlist(f[3])})
        return jobs

    def states(self, job_ids):
        """{job_id: (state, node, reason)} for the jobs squeue knows, else what sacct remembers (lower-case words)."""
        found = {}
        if not job_ids:
            return found
        code, out = self.shell.run(["squeue", "-h", "-j", ",".join(job_ids), "-o", "%i|%T|%N|%r"], timeout=30)
        if code == 0:
            for line in out.splitlines():
                f = line.strip().split("|")
                if len(f) >= 4:
                    found[f[0]] = (f[1].lower(), f[2], f[3])
        missing = [j for j in job_ids if j not in found]
        if missing:
            code, out = self.shell.run(["sacct", "-n", "-P", "-X", "-j", ",".join(missing), "-o", "JobID,State,NodeList,Reason"], timeout=30)
            if code == 0:
                for line in out.splitlines():
                    f = line.strip().split("|")
                    if len(f) >= 3 and f[0] in missing:
                        found[f[0]] = (f[1].split()[0].lower() if f[1] else "unknown", f[2], f[3] if len(f) > 3 else "")
        return found


# ---------------------------------------------------------------------------------------------------------- runs

def _text(value, name, pattern, limit=200):
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > limit or not re.fullmatch(pattern, value):
        raise Refused(400, "{}: not an acceptable value".format(name))
    return value


def _number(value, name, low, high, default):
    if value in (None, ""):
        return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise Refused(400, "{}: a number".format(name))
    if not low <= value <= high:
        raise Refused(400, "{}: between {} and {}".format(name, low, high))
    return int(value) if value == int(value) else value


def _paths(values, name, limit):
    if values in (None, ""):
        return []
    if not isinstance(values, list) or len(values) > limit:
        raise Refused(400, "{}: a list of at most {} paths".format(name, limit))
    out = []
    for v in values:
        if not isinstance(v, str) or not v.strip() or len(v) > 4096 or "\0" in v or "\n" in v:
            raise Refused(400, "{}: that isn't a path".format(name))
        v = v.strip()
        if not (v.startswith("/") or v.startswith("~") or re.match(r"^[A-Za-z0-9 _.-]{1,40}=[/~]", v)):
            raise Refused(400, "{}: {} must be an absolute path (or LABEL=/path)".format(name, v))
        out.append(v)
    return out


def validate(spec):
    """The spec made safe and complete, or Refused."""
    if not isinstance(spec, dict):
        raise Refused(400, "a JSON object")
    where = spec.get("where") or "login"
    if where not in ("login", "compute", "both"):
        raise Refused(400, "where: login, compute or both")
    nodes = spec.get("nodes") or []
    if not isinstance(nodes, list) or len(nodes) > MAX_NODES:
        raise Refused(400, "nodes: at most {} names".format(MAX_NODES))
    nodes = [_text(n, "nodes", r"[A-Za-z0-9][A-Za-z0-9._-]*") for n in nodes]
    return {"where": where, "nodes": sorted(set(nodes)),
            "partition": _text(spec.get("partition"), "partition", r"[A-Za-z0-9._,-]+"),
            "account": _text(spec.get("account"), "account", r"[A-Za-z0-9._-]+"),
            "paths": _paths(spec.get("paths"), "paths", MAX_PATHS),
            "survey_roots": _paths(spec.get("survey_roots"), "survey_roots", 6),
            "survey_seconds": _number(spec.get("survey_seconds"), "survey_seconds", 5, 600, 60),
            "repeats": _number(spec.get("repeats"), "repeats", 3, 100, 15),
            "threads": _number(spec.get("threads"), "threads", 1, 16, 4),
            "max_seconds": _number(spec.get("max_seconds"), "max_seconds", 10, 900, 180),
            "quota": bool(spec.get("quota", True)), "latency": bool(spec.get("latency", True)),
            "time_limit": _text(spec.get("time_limit"), "time_limit", r"\d{1,2}:\d{2}(:\d{2})?|\d+-\d{1,2}(:\d{2}){0,2}") or "00:20:00",
            "mem": _text(spec.get("mem"), "mem", r"\d{1,6}[KMGT]?") or "1G"}


def probe_arguments(spec, out, where):
    """The command-line arguments of fsprobe.py for this spec."""
    args = ["--where", where, "--out", out, "--repeats", str(spec["repeats"]), "--threads", str(spec["threads"]),
            "--max-seconds", str(spec["max_seconds"]), "--survey-seconds", str(spec["survey_seconds"])]
    if not spec["latency"]:
        args.append("--no-latency")
    for p in spec["paths"]:
        args += ["--path", p]
    for s in spec["survey_roots"]:
        args += ["--survey", s]
    if not spec["quota"]:
        args.append("--no-quota")
    return args


def batch_script(spec, key, node, run_dir, source, python="python3"):
    """The sbatch script for one compute target: the probe's own source after a here-document."""
    if MARKER in source:
        raise Refused(500, "the probe's source contains the script's end marker")
    lines = ["#!/bin/bash", "#SBATCH --job-name=hpclib-fsdiag", "#SBATCH --output={}".format(os.path.join(run_dir, key + ".slurm.out")),
             "#SBATCH --nodes=1", "#SBATCH --ntasks=1", "#SBATCH --cpus-per-task=1", "#SBATCH --mem={}".format(spec["mem"]),
             "#SBATCH --time={}".format(spec["time_limit"])]
    if node:
        lines.append("#SBATCH --nodelist={}".format(node))
    if spec["partition"]:
        lines.append("#SBATCH --partition={}".format(spec["partition"]))
    if spec["account"]:
        lines.append("#SBATCH --account={}".format(spec["account"]))
    out = os.path.join(run_dir, key + ".json")
    args = " ".join(shlex.quote(a) for a in probe_arguments(spec, out, "compute"))
    return "\n".join(lines) + "\n\n# the probe's source travels in this script, so starting it reads nothing from the file system under test\n" + \
        "{} - {} <<'{}'\n{}\n{}\n".format(python, args, MARKER, source.rstrip("\n"), MARKER)


class Runs:
    def __init__(self, state_dir, probe_path, shell, slurm, python=None):
        self.dir = os.path.join(state_dir, "runs")
        self.probe_path = probe_path
        self.shell, self.slurm = shell, slurm
        self.python = python or sys.executable or "python3"
        self.compute_python = "python3"
        self._procs, self._lock = {}, threading.Lock()

    def _run_dir(self, run_id):
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", run_id or ""):
            raise Refused(404, "no such run")
        return os.path.join(self.dir, run_id)

    def start(self, raw_spec):
        spec = validate(raw_spec)
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        run_id = "{}-{}".format(time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3))
        run_dir = self._run_dir(run_id)
        os.mkdir(run_dir, 0o700)
        targets = []
        if spec["where"] in ("login", "both"):
            targets.append({"key": "login", "where": "login", "node": None})
        if spec["where"] in ("compute", "both"):
            if not self.slurm.nodes()["slurm"]:
                raise Refused(409, "this cluster has no SLURM here (sbatch and sinfo weren't found): only the login node can be probed")
            for n in spec["nodes"] or [None]:
                targets.append({"key": "node-" + n if n else "any", "where": "compute", "node": n})
        for t in targets:
            t.update({"state": "pending", "detail": "", "job_id": None, "result": t["key"] + ".json", "started": None, "finished": None})
        run = {"id": run_id, "created": time.time(), "spec": spec, "targets": targets}
        with open(self.probe_path) as f:
            source = f.read()
        for t in targets:
            try:
                if t["where"] == "login":
                    self._start_local(run_dir, t, spec)
                else:
                    self._start_job(run_dir, t, spec, source)
            except Refused as e:
                t.update(state="failed", detail=str(e), finished=time.time())
        write_json(os.path.join(run_dir, "meta.json"), run)
        return self.get(run_id)

    def _start_local(self, run_dir, target, spec):
        out = os.path.join(run_dir, target["result"])
        log = open(os.path.join(run_dir, "login.log"), "w")
        try:
            proc = subprocess.Popen([self.python, self.probe_path] + probe_arguments(spec, out, "login"), stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, cwd=os.path.expanduser("~"), start_new_session=True)
        except OSError as e:
            raise Refused(500, "couldn't start the probe: {}".format(e))
        finally:
            log.close()
        with self._lock:
            self._procs[(os.path.basename(run_dir), target["key"])] = proc
        target.update(state="running", started=time.time())

    def _start_job(self, run_dir, target, spec, source):
        script = os.path.join(run_dir, target["key"] + ".sbatch")
        with open(script, "w") as f:
            f.write(batch_script(spec, target["key"], target["node"], run_dir, source, self.compute_python))
        code, out = self.shell.run(["sbatch", "--parsable", script], cwd=run_dir, timeout=60)
        m = re.match(r"(\d+)", out.strip())
        if code != 0 or not m:
            raise Refused(502, "sbatch: {}".format(out.strip()[:300] or "failed"))
        target.update(state="pending", job_id=m.group(1), started=time.time())

    # -- state

    def get(self, run_id):
        run_dir = self._run_dir(run_id)
        run = read_json(os.path.join(run_dir, "meta.json"))
        if run is None:
            raise Refused(404, "no such run")
        changed = self._refresh(run_dir, run)
        if changed:
            write_json(os.path.join(run_dir, "meta.json"), run)
        run["state"] = self.summary(run)
        return run

    @staticmethod
    def summary(run):
        states = [t["state"] for t in run["targets"]]
        if any(s not in FINAL for s in states):
            return "running" if any(s == "running" for s in states) else "pending"
        if all(s == "done" for s in states):
            return "done"
        return "partial" if any(s == "done" for s in states) else "failed"

    def _refresh(self, run_dir, run):
        changed = False
        pending_jobs = [t for t in run["targets"] if t["where"] == "compute" and t["state"] not in FINAL and t["job_id"]]
        states = self.slurm.states([t["job_id"] for t in pending_jobs]) if pending_jobs else {}
        for t in run["targets"]:
            if t["state"] in FINAL:
                continue
            result = os.path.join(run_dir, t["result"])
            before = (t["state"], t["detail"])
            if os.path.isfile(result):
                t.update(state="done", detail="", finished=t["finished"] or time.time())
            elif t["where"] == "login":
                proc = self._procs.get((run["id"], t["key"]))
                if proc is None:
                    t.update(state="failed", detail="the server restarted while this ran", finished=time.time())
                elif proc.poll() is not None:
                    t.update(state="failed", detail=self._tail(os.path.join(run_dir, "login.log")) or "exited with {}".format(proc.returncode),
                             finished=time.time())
            elif t["job_id"] in states:
                state, node, reason = states[t["job_id"]]
                if state in ("pending", "configuring"):
                    t.update(state="pending", detail=reason if reason not in ("None", "") else "waiting in the queue")
                elif state in ("running", "completing"):
                    t.update(state="running", detail="on {}".format(node), node_used=node)
                elif state == "completed":
                    t.update(state="failed", detail="the job finished without writing a result: " + (self._tail(os.path.join(run_dir, t["key"] + ".slurm.out")) or "no output"),
                             finished=time.time())
                elif state.startswith("cancelled"):
                    t.update(state="cancelled", detail="cancelled", finished=time.time())
                else:
                    t.update(state="failed", detail="{}: {}".format(state, self._tail(os.path.join(run_dir, t["key"] + ".slurm.out")) or "no output"),
                             finished=time.time())
            if (t["state"], t["detail"]) != before:
                changed = True
        return changed

    @staticmethod
    def _tail(path, lines=4):
        try:
            with open(path, errors="replace") as f:
                return " | ".join(l.strip() for l in f.read().splitlines()[-lines:] if l.strip())[:400]
        except OSError:
            return ""

    def list(self, limit=30):
        try:
            ids = sorted((d for d in os.listdir(self.dir) if re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", d)), reverse=True)[:limit]
        except OSError:
            return []
        runs = []
        for i in ids:
            try:
                r = self.get(i)
            except Refused:
                continue
            runs.append({"id": r["id"], "created": r["created"], "state": r["state"], "where": r["spec"]["where"],
                         "targets": [{k: t.get(k) for k in ("key", "where", "node", "state", "detail", "job_id")} for t in r["targets"]]})
        return runs

    def results(self, run):
        """[fsprobe result] for the run's finished targets, each marked with its target's key."""
        out = []
        for t in run["targets"]:
            if t["state"] == "done":
                data = read_json(os.path.join(self._run_dir(run["id"]), t["result"]))
                if data:
                    data["target"] = t["key"]
                    out.append(data)
        return out

    def cancel(self, run_id):
        run = self.get(run_id)
        run_dir = self._run_dir(run_id)
        for t in run["targets"]:
            if t["state"] in FINAL:
                continue
            proc = self._procs.get((run_id, t["key"]))
            if proc is not None and proc.poll() is None:
                proc.terminate()
            if t.get("job_id"):
                self.shell.run(["scancel", t["job_id"]], timeout=30)
            t.update(state="cancelled", detail="cancelled", finished=time.time())
        write_json(os.path.join(run_dir, "meta.json"), run)
        run["state"] = self.summary(run)
        return run

    def forget(self, run_id):
        """Delete a finished run's folder."""
        run_dir = self._run_dir(run_id)
        run = self.get(run_id)
        if run["state"] in ("running", "pending"):
            raise Refused(409, "cancel it first")
        import shutil
        shutil.rmtree(run_dir, ignore_errors=True)
