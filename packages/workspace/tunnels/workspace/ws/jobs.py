"""
Jobs: check and submit a job from the guided form, and the runs the Workspace has made. A *run* is a submission: the
script, the spec that made it, the commit and environment it ran with, and where its output goes, kept as one JSON file
under $HPCTUNNELS_DATA_DIR/workspace/runs. The Runs page lists them with their live state, one `squeue` and one `sacct`
call for all of them, so it costs the scheduler no more with fifty runs than with one.

  Jobs(shell, cluster, envs, info, home)
    .render(body)             {spec} -> {script, spec, errors, warnings, environment}
    .check(body)              {spec | script, dir?} -> sbatch --test-only: {ok, message, start}
    .submit(body)             {spec | script, dir?, script_dir?, name?, overwrite?, as_is?, allow_blocked?} -> {job_id, path, output_path, run}
    .write_and_sbatch(script, name, dir, overwrite)   the classic submit: write the file beside the work, sbatch it
    .runs(directory=None, limit=200)   the runs, newest first, with state; a directory keeps those from it and below
    .cancel(job_id)           scancel one of the runs
    .state(job_id)            one job's state (squeue, else sacct)

Programs run directly with the login shell's environment (ShellEnv.run): no shell parses the job's id or file names.

Python 3.6 or newer, standard library only.
"""
import hashlib
import io
import json
import os
import re
import subprocess
import time

try:
    import clusterinfo
    import envinfo
except ImportError:                       # imported as a package from the repository
    from hpclib.servers import clusterinfo, envinfo

from . import jobspec
from .common import MAX_JSON, Refused, oserror, resolve, state_dir
from .fs import _umask, write_atomic

JOB_RE = re.compile(r"Submitted batch job (\d+)")
SCRIPT_NAME_RE = re.compile(r"[A-Za-z0-9_.+@-][A-Za-z0-9_.+@ -]{0,200}")
JOB_ID_RE = re.compile(r"\d+")
# what sacct says of a job that is over (RUNNING and PENDING are not: squeue is behind, or the job was requeued)
ENDED_STATES = ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL", "DEADLINE",
                "PREEMPTED")
RUN_LIMIT = 200


class Jobs:
    def __init__(self, shell, cluster, envs, home):
        self.shell, self.cluster, self.envs, self.home = shell, cluster, envs, home
        self.on_submit = None             # called with the spec of each job submitted (the profiles keep "last used")

    # -- the form: render, check, submit -----------------------------------------------------------------------------

    def _environment_for(self, spec):
        """(info, status, reason, blocks) for the environment a spec would use: the project at or above the script (or
        the working directory). It blocks Submit unless the spec lets the job sync or leaves the environment alone."""
        mode = spec["environment"]["mode"]
        if mode == "none":
            return None, "not used", "this job doesn't activate an environment", False
        start = spec["command"] if spec["kind"] == "script" and spec["command"].startswith("/") else spec["workdir"]
        project = envinfo.find_project(start, self.home)
        if project is None:
            return None, "none found", "no pyproject.toml or pixi.toml at or above the script", False
        info = envinfo.detect(project)
        word, reason = envinfo.status(info)
        tool = ((self.envs.tools().get("tools") or {}).get(info.get("manager")) or {}).get("path") if info.get("manager") else None
        if info.get("manager") and not tool and word in ("not prepared", "stale"):
            word, reason = "tool missing", "{} isn't installed for you yet".format(info["manager"])
        blocks = word in ("not prepared", "stale", "busy", "failed", "tool missing") and mode == "auto"
        return info, word, reason, blocks

    def render(self, body):
        """The form's spec -> the script, what's wrong with it, and the environment it would use."""
        spec = body.get("spec")
        if not isinstance(spec, dict):
            raise Refused(400, "spec: the job spec")
        cluster = self.cluster.get()
        clean, errors, warnings = jobspec.validate(spec, cluster)
        out = {"spec": clean, "errors": errors, "warnings": warnings, "script": None}
        if errors:
            return out
        info, word, reason, blocks = self._environment_for(clean)
        out.update(script=jobspec.render(clean, cluster, info), environment={
            "status": word, "reason": reason, "blocks": blocks, "project": info and info["project"],
            "manager": info and info.get("manager")})
        return out

    def import_script(self, path):
        """A job script on disk for the form: its spec (the one a generated script carries, else what parse_script reads
        from a hand-written one), what the form would write for that spec, and whether that differs from the file."""
        path = resolve(path)
        try:
            if os.path.getsize(path) > MAX_JSON:
                raise Refused(413, "{} is too long to read as a job script".format(path))
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            raise oserror(e, path)
        spec = jobspec.embedded_spec(text)
        source = "embedded" if spec else "imported"
        warnings = []
        if spec is None:
            spec, warnings = jobspec.parse_script(text, os.path.dirname(path), os.path.splitext(os.path.basename(path))[0] or "job")
        out = self.render({"spec": spec})
        out.update(path=path, text=text, source=source, import_warnings=warnings,
                   differs=out.get("script") is None or out["script"].strip() != text.strip())
        return out

    def adopt(self, body):
        """Write the form's version of a script over the file, keeping the original as NAME.bak."""
        path = resolve(body.get("path") or "")
        rendered = self.render(body)
        if rendered["errors"] or not rendered["script"]:
            raise Refused(400, "; ".join(rendered["errors"]) or "nothing to write")
        backup = path + ".bak"
        try:
            with open(path, "rb") as f:
                original = f.read()
            mode = os.stat(path).st_mode & 0o777
            write_atomic(backup, io.BytesIO(original), len(original), mode)
            data = rendered["script"].encode()
            write_atomic(path, io.BytesIO(data), len(data), mode)
        except OSError as e:
            raise oserror(e, path)
        return {"path": path, "backup": backup}

    def interactive(self, body):
        """The srun line for an interactive session with the form's resources, and the folder to start it in."""
        spec = body.get("spec")
        if not isinstance(spec, dict):
            raise Refused(400, "spec: the job spec")
        try:
            command, warnings = jobspec.interactive_command(spec)
        except jobspec.SpecError as e:
            raise Refused(400, "; ".join(e.errors))
        workdir = spec.get("workdir") if isinstance(spec.get("workdir"), str) and spec["workdir"].startswith("/") else self.home
        for key in ("array", "sweep", "dependency"):
            if spec.get(key):
                warnings.append("An interactive session has no {}: it is one shell".format(key))
        return {"command": command, "workdir": workdir, "warnings": warnings}

    def _script_from(self, body):
        """(script text, spec or None, working directory, environment tuple or None) for a Check or Submit: the form's
        spec rendered, or a script the person edited by hand (its embedded spec, when it has one, is only for the run
        record)."""
        text = body.get("script")
        if isinstance(text, str) and text.strip():
            if len(text) > MAX_JSON:
                raise Refused(400, "script: too long")
            embedded = jobspec.embedded_spec(text)
            clean = None
            if embedded:
                clean, errors, _ = jobspec.validate(embedded)
                clean = None if errors else clean
            workdir = clean["workdir"] if clean else body.get("dir")
            return text, clean, workdir, (self._environment_for(clean) if clean else None)
        rendered = self.render(body)
        if rendered["errors"]:
            raise Refused(400, "; ".join(rendered["errors"]))
        clean = rendered["spec"]
        return rendered["script"], clean, clean["workdir"], self._environment_for(clean)

    def check(self, body):
        """`sbatch --test-only` on the script: would the cluster take it, and when would it start."""
        text, clean, workdir, _ = self._script_from(body)
        workdir = resolve(workdir)
        if not os.path.isdir(workdir):
            raise Refused(404, "{} isn't a directory".format(workdir))
        code, out = self.shell.run(["sbatch", "--test-only"], cwd=workdir, timeout=60, stdin_text=text)
        result = clusterinfo.parse_test_only(out)
        result["raw"] = out.strip()[-1500:]
        return result

    def write_and_sbatch(self, script, name, directory, overwrite=False):
        """Write `script` as `name` in `directory` (mode 0755), sbatch it from there: {path, job_id, output}."""
        if not isinstance(script, str) or not script.strip() or len(script) > MAX_JSON:
            raise Refused(400, "script: the job script")
        if not isinstance(name, str) or not SCRIPT_NAME_RE.fullmatch(name) or name in (".", ".."):
            raise Refused(400, "name: the job script's file name (letters, digits, spaces and _ . + @ -)")
        directory = resolve(directory)
        if not os.path.isdir(directory):
            raise Refused(404, "{} isn't a directory".format(directory))
        path = os.path.join(directory, name)
        if os.path.lexists(path) and not overwrite:
            raise Refused(409, "{} already exists".format(path))
        data = script.encode()
        try:
            write_atomic(path, io.BytesIO(data), len(data), 0o777 & ~_umask() & 0o755)
        except OSError as e:
            raise oserror(e, path)
        code, out = self.shell.run(["sbatch", name], cwd=directory, timeout=120)
        m = JOB_RE.search(out)
        if code != 0 or not m:
            raise Refused(400, "sbatch: " + (out.strip()[-1500:] or "exit {}".format(code)))
        return {"path": path, "job_id": m.group(1), "output": out.strip()[-1500:]}

    def sbatch_existing(self, path, expected_text):
        """sbatch a script file that is already there, if it is still what the page showed."""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                if f.read().strip() != expected_text.strip():
                    raise Refused(409, "{} changed on disk since it was opened: open it again".format(path))
        except OSError as e:
            raise oserror(e, path)
        code, out = self.shell.run(["sbatch", os.path.basename(path)], cwd=os.path.dirname(path), timeout=120)
        m = JOB_RE.search(out)
        if code != 0 or not m:
            raise Refused(400, "sbatch: " + (out.strip()[-1500:] or "exit {}".format(code)))
        return {"path": path, "job_id": m.group(1), "output": out.strip()[-1500:]}

    def submit(self, body):
        """Write the script (next to the work), sbatch it, keep a run record, say where the output will be."""
        text, clean, workdir, env = self._script_from(body)
        workdir = resolve(workdir)
        if env is not None and env[3] and not body.get("allow_blocked"):
            raise Refused(409, "the environment is {} ({}): prepare it first, or let the job sync".format(env[1], env[2]))
        name = body.get("name") or ((clean["name"] if clean else "job") + ".slurm")
        target = resolve(body.get("script_dir") or workdir)          # where the script file lies (an opened script stays put)
        if body.get("as_is"):             # a script that stays as it is on disk: only sbatch it
            done = self.sbatch_existing(os.path.join(target, name), text)
        else:
            done = self.write_and_sbatch(text, name, target, bool(body.get("overwrite")))
        job_id = done["job_id"]
        run = {"job_id": job_id, "submitted": time.time(), "script": done["path"], "workdir": workdir, "spec": clean,
               "script_sha256": hashlib.sha256(text.encode()).hexdigest(), "git": self._git(workdir)}
        if env is not None and env[0]:
            info = env[0]
            run["environment"] = {"status": env[1], "project": info["project"], "manager": info.get("manager"),
                                  "synced": info.get("synced"), "packages": info.get("packages"), "python": info.get("python")}
        pattern = (clean or {}).get("output") or "%x-%j.out"
        fanned = bool(clean and (clean.get("array") or clean.get("sweep")))
        if fanned:                        # the follower shows the first task's file; the run knows how many there are
            run["tasks"] = jobspec.task_count(clean)
        out_path = pattern.replace("%x", (clean or {}).get("name") or os.path.splitext(name)[0]).replace("%j", job_id)
        out_path = out_path.replace("%A", job_id).replace("%a", str(jobspec.first_task(clean)) if fanned else "0")
        out_path = out_path if out_path.startswith("/") else os.path.join(workdir, out_path)
        run["output"] = out_path
        try:
            self._save(run)
        except OSError:
            pass
        if clean and self.on_submit:
            try:
                self.on_submit(clean)
            except Exception:             # remembering is a convenience: it never fails a submission
                pass
        done.update(output_path=out_path, run=run)
        return done

    @staticmethod
    def _git(directory):
        """The commit (and whether the tree has changes) the work was submitted from; None outside a repository."""
        try:
            head = subprocess.run(["git", "-C", directory, "rev-parse", "HEAD"], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, universal_newlines=True, timeout=5)
            if head.returncode != 0:
                return None
            dirty = subprocess.run(["git", "-C", directory, "status", "--porcelain"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, universal_newlines=True, timeout=10)
            return {"head": head.stdout.strip(), "dirty": bool(dirty.stdout.strip())}
        except (OSError, subprocess.SubprocessError):
            return None

    # -- the runs ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _runs_dir():
        return os.path.join(state_dir(), "runs")

    def _save(self, run):
        os.makedirs(self._runs_dir(), mode=0o700, exist_ok=True)
        path = os.path.join(self._runs_dir(), run["job_id"] + ".json")
        with open(path + ".tmp", "w") as f:
            json.dump(run, f, indent=1)
        os.replace(path + ".tmp", path)

    def _load(self, job_id=None):
        """The run records, newest first (or the one for `job_id`)."""
        if job_id is not None:
            try:
                with open(os.path.join(self._runs_dir(), job_id + ".json")) as f:
                    run = json.load(f)
                return run if isinstance(run, dict) else None
            except (OSError, ValueError):
                return None
        runs = []
        try:
            names = os.listdir(self._runs_dir())
        except OSError:
            return runs
        for name in names:
            if not name.endswith(".json") or not JOB_ID_RE.fullmatch(name[:-5]):
                continue
            try:
                with open(os.path.join(self._runs_dir(), name)) as f:
                    run = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(run, dict) and run.get("job_id"):
                runs.append(run)
        runs.sort(key=lambda r: r.get("submitted") or 0, reverse=True)
        return runs

    @staticmethod
    def _task_count(text):
        """How many tasks a job id like 123_7 or 123_[8-20%4] stands for."""
        m = re.search(r"_\[(\d+)(?:-(\d+))?", text)
        return (int(m.group(2) or m.group(1)) - int(m.group(1)) + 1) if m else 1

    @staticmethod
    def _fold(rows):
        """One answer from the rows of an array (or the one row of a plain job): {state, reason, elapsed, tasks,
        exit_code}. Running beats pending beats the ends; the worst end shows when all are over."""
        tasks = {}
        for state, count in rows:
            tasks[state] = tasks.get(state, 0) + count
        for state in ("RUNNING", "COMPLETING", "PENDING", "REQUEUED", "SUSPENDED"):
            if state in tasks:
                return state, tasks
        bad = [s for s in tasks if s != "COMPLETED"]
        return (bad[0] if bad else "COMPLETED"), tasks

    def _states(self, ids):
        """{job id: {state, reason, elapsed, exit_code, ended, tasks?}} for ids: one squeue call, then one sacct for the
        rest. An array is one job id here: its tasks are folded into one state, with the counts in `tasks`."""
        found = {}
        if not ids:
            return found
        live = {}
        code, out = self.shell.run(["squeue", "-r", "-h", "-j", ",".join(ids), "-o", "%i|%T|%r|%M"], timeout=30)
        if code == 0:
            for line in out.splitlines():
                parts = line.strip().split("|")
                base = parts[0].split("_")[0] if len(parts) == 4 else None
                if base in ids:
                    live.setdefault(base, []).append((parts[0], parts[1], parts[2], parts[3]))
        for base, rows in live.items():
            state, tasks = self._fold([(r[1], self._task_count(r[0])) for r in rows])
            pick = next((r for r in rows if r[1] == state), rows[0])
            found[base] = {"state": state, "reason": pick[2], "elapsed": pick[3], "ended": False}
            if len(rows) > 1 or "_" in rows[0][0]:
                found[base]["tasks"] = tasks
        rest = [i for i in ids if i not in found]
        if rest:
            code, out = self.shell.run(["sacct", "-n", "-P", "-X", "-j", ",".join(rest), "-o",
                                        "JobID,State,ExitCode,Elapsed"], timeout=30)
            if code == 0:
                done = {}
                for line in out.splitlines():
                    parts = line.strip().split("|")
                    base = parts[0].split("_")[0] if len(parts) == 4 else None
                    if base in rest:
                        done.setdefault(base, []).append(parts)
                for base, rows in done.items():
                    words = [(p[1].split()[0] if p[1].split() else "UNKNOWN", self._task_count(p[0])) for p in rows]
                    state, tasks = self._fold(words)
                    pick = next((p for p in rows if (p[1].split() or [""])[0] == state), rows[0])
                    found[base] = {"state": state, "exit_code": pick[2], "elapsed": pick[3],
                                   "ended": all(w in ENDED_STATES for w, _ in words)}
                    if len(rows) > 1 or "_" in rows[0][0]:
                        found[base]["tasks"] = tasks
        return found

    def runs(self, directory=None, limit=RUN_LIMIT):
        """The runs, newest first, with state. `directory` keeps the runs whose working directory is it or below it."""
        limit = max(1, min(int(limit), RUN_LIMIT))
        records = self._load()
        if directory:
            here = resolve(directory)
            records = [r for r in records if r.get("workdir") == here or (r.get("workdir") or "").startswith(here.rstrip("/") + "/")]
        records = records[:limit]
        pending = [r["job_id"] for r in records if not r.get("ended")][:100]
        states = self._states(pending)
        out = []
        for r in records:
            if not r.get("ended"):
                live = states.get(r["job_id"]) or {"state": "UNKNOWN", "ended": False}
                if live.get("ended"):             # over: keep how it ended, and never ask about it again
                    r["ended"] = dict({k: live.get(k) for k in ("state", "exit_code", "elapsed", "tasks")}, at=time.time())
                    try:
                        self._save(r)
                    except OSError:
                        pass
            if r.get("ended"):
                live = dict(r["ended"], ended=True)
            spec = r.get("spec") or {}
            env = r.get("environment") or {}
            out.append({"job_id": r["job_id"], "name": spec.get("name") or os.path.basename(r.get("script") or ""),
                        "submitted": r.get("submitted"), "workdir": r.get("workdir"), "script": r.get("script"),
                        "output": r.get("output"), "git": r.get("git"), "resources": spec.get("resources"),
                        "environment": {"manager": env.get("manager"), "project": env.get("project"),
                                        "status": env.get("status")} if env else None,
                        "state": live.get("state"), "reason": live.get("reason"), "exit_code": live.get("exit_code"),
                        "elapsed": live.get("elapsed"), "ended": bool(live.get("ended")),
                        "tasks": live.get("tasks"), "task_total": r.get("tasks")})
        return {"runs": out, "at": time.time()}

    def cancel(self, job_id):
        """scancel one of the runs the Workspace made (a job from elsewhere is the terminal's to cancel)."""
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise Refused(400, "job: a job id")
        if self._load(job_id) is None:
            raise Refused(404, "no run {} here (the Workspace cancels the jobs it submitted)".format(job_id))
        code, out = self.shell.run(["scancel", job_id], timeout=60)
        if code != 0:
            raise Refused(400, "scancel: " + (out.strip()[-500:] or "exit {}".format(code)))
        return {"cancelled": job_id}

    def state(self, job_id):
        """A job's state: squeue while it is queued or running, sacct once it has ended."""
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise Refused(400, "job: a job id")
        got = self._states([job_id]).get(job_id)
        if got is None:
            return {"job": job_id, "state": "UNKNOWN", "ended": False}
        out = {"job": job_id, "state": got["state"], "ended": got["ended"]}
        for key in ("reason", "exit_code"):
            if got.get(key) is not None:
                out[key] = got[key]
        return out
