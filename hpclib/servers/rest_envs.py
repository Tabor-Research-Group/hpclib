"""
Python environments for template jobs: uv projects and pixi projects kept
in the token's directories.

  - `detect`: which manager a project uses (pixi.toml or [tool.pixi] ->
    pixi; pyproject.toml or uv.lock -> uv) and whether its environment
    (`.pixi/envs/NAME`, `.venv`) exists yet.
  - `sync`: `uv sync` or `pixi install` for a project, in the background,
    run in the job sandbox with only the project and the tool's cache and
    Python directories writable. With a lockfile it installs exactly that
    (`--locked`); `update` re-resolves and rewrites the lock.
  - `prelude` / `job_binds`: what a job needs to run in the environment:
    activation lines run inside its sandbox (so packages' activation
    scripts are sandboxed too) and read-only binds (uv's managed Pythons).

Jobs only activate environments; they never resolve or download, so
compute nodes don't need the network. The sync runs where the REST server
runs (the tunnel's node). Config, the `environments` key of the server's
config.json (all optional):

  {"uv": "auto",          # "auto" (PATH, ~/.local/bin, ~/.cargo/bin), a path, or null to turn it off
   "pixi": "auto",        # "auto" (PATH, ~/.pixi/bin), a path, or null
   "modules": [],         # modules to load before a sync, e.g. ["WebProxy"] for internet access
   "timeout": 1800,       # seconds a sync may take
   "max_running": 2}      # syncs at once

Standard library only.
"""
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time

try:
    from . import rest_sandbox
except ImportError:
    import rest_sandbox

__all__ = ["EnvironmentManager", "EnvironmentError_"]

MANAGERS = ("uv", "pixi")
ENV_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
CONFIG_KEYS = {"uv", "pixi", "modules", "timeout", "max_running"}
MODULE_RE = re.compile(r"[A-Za-z0-9_.+-][A-Za-z0-9_.+/-]{0,127}")
SEARCH = {
    "uv": ("~/.local/bin/uv", "~/.cargo/bin/uv"),
    "pixi": ("~/.pixi/bin/pixi",),
}
LOG_TAIL = 60


class EnvironmentError_(Exception):
    """A request about environments that can't be done; `status` is the HTTP status."""
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _has_pixi_table(pyproject):
    try:
        with open(pyproject, errors="replace") as f:
            return re.search(r"^\[tool\.pixi", f.read(1 << 20), re.M) is not None
    except OSError:
        return False


class EnvironmentManager:

    def __init__(self, config=None, sandbox: 'rest_sandbox.Sandbox' = None, data_dir=None, which=None,
                 environ=None):
        config = dict(config or {})
        unknown = set(config) - CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown `environments` keys {sorted(unknown)}; known: {sorted(CONFIG_KEYS)}")
        self.wanted = {m: config.get(m, "auto") for m in MANAGERS}
        for m, v in self.wanted.items():
            if v is not None and not isinstance(v, str):
                raise ValueError(f"`environments.{m}` must be \"auto\", a path, or null")
        self.modules = config.get("modules", [])
        if not isinstance(self.modules, list) or not all(isinstance(m, str) and MODULE_RE.fullmatch(m)
                                                         for m in self.modules):
            raise ValueError("`environments.modules` must be a list of module names")
        self.timeout = config.get("timeout", 1800)
        self.max_running = config.get("max_running", 2)
        for key in ("timeout", "max_running"):
            value = getattr(self, key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"`environments.{key}` must be a positive integer")
        self.sandbox = sandbox
        self.data_dir = os.path.join(data_dir, "envs") if data_dir else None
        self._which = which
        self._environ = environ
        self._tools = None
        self._lock = threading.Lock()
        self.syncs = {}

    # -- the tools -----------------------------------------------------------

    def env(self):
        return dict(self._environ) if self._environ is not None else rest_sandbox.child_env()

    def _find(self, manager):
        wanted = self.wanted[manager]
        if wanted is None:
            return None
        if wanted != "auto":
            path = os.path.expanduser(wanted)
            return path if os.access(path, os.X_OK) else None
        if self._which is not None:
            found = self._which(manager)
        else:
            found = shutil.which(manager, path=self.env().get("PATH", os.defpath))
        if found:
            return os.path.realpath(found)
        for candidate in SEARCH[manager]:
            path = os.path.expanduser(candidate)
            if os.access(path, os.X_OK):
                return os.path.realpath(path)
        return None

    def _ask(self, args, timeout=30):
        try:
            res = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                 stdin=subprocess.DEVNULL, env=self.env())
        except (OSError, subprocess.TimeoutExpired):
            return None
        return res.stdout.strip() if res.returncode == 0 else None

    def tools(self, refresh=False):
        """{manager: {"path", "version", "dirs": {...}}} for the tools found (cached)."""
        with self._lock:
            if self._tools is not None and not refresh:
                return self._tools
        home = os.path.expanduser("~")
        found = {}
        uv = self._find("uv")
        if uv:
            env = self.env()
            found["uv"] = {
                "path": uv,
                "version": (self._ask([uv, "--version"]) or "").replace("uv ", "") or None,
                "dirs": {
                    "cache": env.get("UV_CACHE_DIR") or self._ask([uv, "cache", "dir"])
                             or os.path.join(home, ".cache", "uv"),
                    "python": env.get("UV_PYTHON_INSTALL_DIR") or self._ask([uv, "python", "dir"])
                              or os.path.join(home, ".local", "share", "uv", "python"),
                },
            }
        pixi = self._find("pixi")
        if pixi:
            env = self.env()
            cache = env.get("PIXI_CACHE_DIR") or env.get("RATTLER_CACHE_DIR")
            if not cache:
                try:
                    cache = json.loads(self._ask([pixi, "info", "--json"], timeout=60) or "{}").get("cache_dir")
                except ValueError:
                    cache = None
            found["pixi"] = {
                "path": pixi,
                "version": (self._ask([pixi, "--version"]) or "").replace("pixi ", "") or None,
                "dirs": {"cache": cache or os.path.join(home, ".cache", "rattler", "cache")},
            }
        with self._lock:
            self._tools = found
        return found

    def describe(self):
        tools = self.tools()
        return {
            "managers": {m: ({"available": True, "version": tools[m]["version"]} if m in tools else
                             {"available": False}) for m in MANAGERS},
            "sync_modules": self.modules,
            "how": "put pyproject.toml (+ uv.lock) or pixi.toml (+ pixi.lock) in a project directory, sync it "
                   "with POST /envs/sync, then submit a template whose `environment` points at the project "
                   "(python_project, for example). Jobs only activate the environment; they never install.",
        }

    # -- projects ------------------------------------------------------------

    @staticmethod
    def detect(project, manager="auto", name="default"):
        """What `project` (a real, allowed directory) holds."""
        if not os.path.isdir(project):
            raise EnvironmentError_(422, f"{project} is not a directory")
        if not ENV_NAME_RE.fullmatch(name or ""):
            raise EnvironmentError_(400, "environment names are letters, digits, _ and -")
        files = [f for f in ("pixi.toml", "pixi.lock", "pyproject.toml", "uv.lock") if
                 os.path.isfile(os.path.join(project, f))]
        pyproject = os.path.join(project, "pyproject.toml")
        if manager == "auto":
            if "pixi.toml" in files or "pixi.lock" in files or ("pyproject.toml" in files and _has_pixi_table(pyproject)):
                manager = "pixi"
            elif "pyproject.toml" in files or "uv.lock" in files:
                manager = "uv"
            else:
                return {"project": project, "manager": None, "files": files, "ready": False,
                        "problem": "no pyproject.toml (uv) or pixi.toml (pixi) in this directory"}
        if manager not in MANAGERS:
            raise EnvironmentError_(400, f"`manager` must be auto, uv or pixi, not {manager!r}")
        out = {"project": project, "manager": manager, "files": files}
        if manager == "uv":
            env_path = os.path.join(project, ".venv")
            out.update(environment=None, path=env_path, locked="uv.lock" in files,
                       ready=os.path.exists(os.path.join(env_path, "bin", "python")))
            if "pyproject.toml" not in files:
                out["problem"] = "uv needs a pyproject.toml"
        else:
            env_path = os.path.join(project, ".pixi", "envs", name)
            manifest = "pixi.toml" if "pixi.toml" in files else "pyproject.toml"
            out.update(environment=name, path=env_path, manifest=os.path.join(project, manifest),
                       locked="pixi.lock" in files, ready=os.path.isdir(os.path.join(env_path, "conda-meta")))
            if manifest not in files:
                out["problem"] = "pixi needs a pixi.toml (or a pyproject.toml with [tool.pixi])"
        return out

    def prelude(self, info):
        """Bash lines that activate `info`'s environment (run inside the job's sandbox)."""
        env, q = info["path"], shlex.quote
        missing = (f"echo {q('hpclib: the ' + info['manager'] + ' environment in ' + info['project'] + ' is missing or broken inside the job; sync it again (sync_environment / POST /envs/sync)')} >&2; exit 4")
        if info["manager"] == "uv":
            return [
                f"export VIRTUAL_ENV={q(env)}",
                'export PATH="$VIRTUAL_ENV/bin:$PATH"',
                "unset PYTHONHOME",
                f'[ -x "$VIRTUAL_ENV/bin/python" ] || {{ {missing}; }}',
            ]
        return [
            f"export CONDA_PREFIX={q(env)} PIXI_PROJECT_ROOT={q(info['project'])} "
            f"PIXI_ENVIRONMENT_NAME={q(info['environment'])} PIXI_IN_SHELL=1",
            'export PATH="$CONDA_PREFIX/bin:$PATH"',
            "unset PYTHONHOME",
            f'[ -d "$CONDA_PREFIX/conda-meta" ] || {{ {missing}; }}',
            'for hpc_activate in "$CONDA_PREFIX"/etc/conda/activate.d/*.sh; do',
            '  [ -f "$hpc_activate" ] && . "$hpc_activate"',
            "done",
            "unset hpc_activate",
        ]

    def job_binds(self, info):
        """Read-only binds a job in this environment needs besides the project."""
        if info["manager"] == "uv":
            uv = self.tools().get("uv")
            if uv and os.path.isdir(uv["dirs"]["python"]):
                return [uv["dirs"]["python"]]
        return []

    # -- syncing -------------------------------------------------------------

    def _record_path(self, sync_id, ext):
        return os.path.join(self.data_dir, f"{sync_id}.{ext}") if self.data_dir else None

    def sync(self, token_name, info, update=False):
        """Start `uv sync` / `pixi install` for a detected project; returns the sync record."""
        if info.get("manager") is None or info.get("problem"):
            raise EnvironmentError_(422, info.get("problem") or "nothing to sync", project=info["project"])
        manager = info["manager"]
        tool = self.tools().get(manager)
        if tool is None:
            raise EnvironmentError_(503, f"{manager} is not installed where the REST server runs; install it "
                                         f"(it is a single binary) or set `environments.{manager}` in config.json",
                                    managers=self.describe()["managers"])
        if self.data_dir is None:
            raise EnvironmentError_(503, "the server has no data directory for sync logs")
        with self._lock:
            running = [s for s in self.syncs.values() if s["state"] == "running"]
            for s in running:
                if s["project"] == info["project"]:
                    return dict(s, already_running=True)
            if len(running) >= self.max_running:
                raise EnvironmentError_(429, f"{len(running)} syncs are already running; try again shortly")
            sync_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
            record = {"id": sync_id, "token": token_name, "project": info["project"], "manager": manager,
                      "environment": info.get("environment"), "update": bool(update), "state": "running",
                      "started": time.time(), "finished": None, "exit_code": None}
            self.syncs[sync_id] = record
        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        script, command = self._script(info, tool, update)
        record["command"] = command
        with open(self._record_path(sync_id, "sh"), "w") as f:
            f.write(script)
        threading.Thread(target=self._run, args=(record,), daemon=True).start()
        return dict(record)

    def _script(self, info, tool, update):
        project, q = info["project"], shlex.quote
        if info["manager"] == "uv":
            command = [tool["path"], "sync"] + ([] if update or not info["locked"] else ["--locked"])
            exports = {"UV_CACHE_DIR": tool["dirs"]["cache"], "UV_PYTHON_INSTALL_DIR": tool["dirs"]["python"],
                       "UV_LINK_MODE": "copy", "UV_NO_PROGRESS": "1"}
            writable = [project, tool["dirs"]["cache"], tool["dirs"]["python"]]
        else:
            command = [tool["path"], "install", "--manifest-path", info["manifest"], "-e", info["environment"]]
            if info["locked"] and not update:
                command.append("--locked")
            exports = {"PIXI_CACHE_DIR": tool["dirs"]["cache"], "PIXI_NO_PROGRESS": "1", "NO_COLOR": "1"}
            writable = [project, tool["dirs"]["cache"]]
        for d in writable[1:]:
            os.makedirs(d, exist_ok=True)
        body = "\n".join([
            "set -e",
            f"cd {q(project)}",
            f"echo '$ {' '.join(command)}'",
            "exec " + " ".join(q(c) for c in command),
        ]) + "\n"
        lines = ["#!/bin/bash", f"# hpclib environment sync for {project}"]
        lines += [f"export {k}={q(v)}" for k, v in sorted(exports.items())]
        if self.modules:
            lines.append(_MODULE_INIT)
            lines += [f"module load {q(m)} || {{ echo 'hpclib: could not load module {m}' >&2; exit 3; }}"
                      for m in self.modules]
        launch = None
        if self.sandbox is not None:
            try:
                launch, _ = self.sandbox.launch("#!/bin/bash", body, writable, extra_ro=[tool["path"]])
            except rest_sandbox.SandboxError as e:
                raise EnvironmentError_(503, f"can't sandbox the sync: {e}")
        script = "\n".join(lines) + "\n\n" + ("\n".join(launch) + "\n" if launch else body)
        return script, command

    def _run(self, record):
        log_path = self._record_path(record["id"], "log")
        try:
            with open(log_path, "wb") as log:
                proc = subprocess.Popen(["bash", self._record_path(record["id"], "sh")], stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT, env=self.env(),
                                        cwd=record["project"], start_new_session=True)
                try:
                    code = proc.wait(self.timeout)
                    state = "succeeded" if code == 0 else "failed"
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, 15)
                    code = proc.wait()
                    state = "timed_out"
        except OSError as e:
            code, state = None, "failed"
            with open(log_path, "a") as log:
                log.write(f"hpclib: could not start the sync: {e}\n")
        with self._lock:
            record.update(state=state, exit_code=code, finished=time.time())
        try:
            with open(self._record_path(record["id"], "json"), "w") as f:
                json.dump(record, f, indent=2)
        except OSError:
            pass

    def status(self, sync_id, token_name, see_all, wait=0, poll=0.5):
        record = self.syncs.get(sync_id)
        if record is None or (record["token"] != token_name and not see_all):
            raise EnvironmentError_(404, f"no sync {sync_id!r}")
        deadline = time.time() + max(0.0, min(float(wait), 300.0))
        while record["state"] == "running" and time.time() < deadline:
            time.sleep(min(poll, max(0.0, deadline - time.time())))
        out = dict(record)
        try:
            with open(self._record_path(sync_id, "log"), errors="replace") as f:
                out["log_tail"] = f.read()[-(64 << 10):].splitlines()[-LOG_TAIL:]
        except OSError:
            out["log_tail"] = []
        if record["state"] != "running":
            try:
                out["environment_info"] = self.detect(record["project"], record["manager"],
                                                      record.get("environment") or "default")
            except EnvironmentError_:
                pass
        return out


_MODULE_INIT = (
    "if ! type module >/dev/null 2>&1; then\n"
    "  for f in /etc/profile.d/lmod.sh /etc/profile.d/modules.sh /usr/share/lmod/lmod/init/bash; do\n"
    "    if [ -f \"$f\" ]; then . \"$f\"; break; fi\n"
    "  done\n"
    "fi"
)
