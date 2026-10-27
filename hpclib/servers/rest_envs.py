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
  - `squash` (uv and pixi): pack the environment into one SquashFS image
    beside it (`.venv.sqfs`; pixi `.pixi-NAME.sqfs`), and empty its
    directory. An environment is tens of thousands of small files, slow to
    start from on a parallel filesystem and heavy on file quotas; the image
    is one file. Jobs mount it back on the directory (Singularity/Apptainer
    --bind IMAGE:DIR:image-src=/), so every path in it (scripts' #! lines,
    pyvenv.cfg, conda's prefixes) is the one the tool wrote, and the job
    sees an ordinary, read-only environment. Bytecode is compiled at the
    sync, since a job can't write it. As a step of a sync (`squash`: true
    packs, false unpacks, left out keeps it as it was) or on its own
    (`squash` / `unsquash`). Changing one that queued or running API jobs
    use is refused (409) without `force` (rest_jobs).
  - a core: an environment built on another (`core`): the core's packages
    aren't installed again, so a large frozen (squashed) core serves many
    small environments. hpclib writes the core's exact versions into this
    project's manifest as constraints, between marker comments
    (rest_manifests.py: [tool.uv] constraint-dependencies; pixi's
    [constraints] with builds), and [tool.hpclib] core = "../core", so
    later syncs keep it. uv: `uv sync --python CORE'S-INTERPRETER
    --no-install-package ...` and a _hpclib_core.pth that puts the core's
    site-packages after the environment's own. pixi: `pixi install --skip
    ...`; jobs run the core's interpreter with the layer as the user site
    (PYTHONUSERBASE), its bin/ and lib/ ahead of the core's. A layer is
    usable while its core is the one it was built on (the core's `synced`
    in its record); after the core is synced again, sync the layer too.
  - the record, `.venv.hpclib.json` (pixi `.pixi-NAME.hpclib.json`): what
    the last sync left there: the interpreter, the packages (for layers'
    constraints), the core, whether it is squashed. `.venv.hpclib-syncing`
    marks an operation at work; jobs aren't submitted then.
  - `prelude` / `job_binds` / `job_images`: what a job needs to run in the
    environment: activation lines run inside its sandbox (so packages'
    activation scripts are sandboxed too), read-only binds (uv's managed
    Pythons) and images to mount (squashed environments, its core's too).

uv's Python and cache directories and pixi's cache come from the
environment (UV_PYTHON_INSTALL_DIR, UV_CACHE_DIR, PIXI_CACHE_DIR), else
what lib/pytools.sh saved for the cluster ($HPCTUNNELS_DATA_DIR/settings/
pytools.sh, the console's Settings -> uv and pixi), else the tools' own.

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
    from . import rest_manifests, rest_sandbox
except ImportError:
    import rest_manifests
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
# hpclib's files beside an environment, in its project (EnvironmentManager.side_path): .venv.sqfs, the image a
# squashed environment is packed into; .venv.hpclib.json, what the last sync recorded (its interpreter, its packages,
# its core); .venv.hpclib-syncing while an operation works on it. For pixi: .pixi-NAME.sqfs and so on.
IMAGE_SUFFIX = ".sqfs"
RECORD_SUFFIX = ".hpclib.json"
MARKER_SUFFIX = ".hpclib-syncing"
SQUASH_IMAGE = ".venv" + IMAGE_SUFFIX


TOOL_DIR_VARS = ("UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "PIXI_CACHE_DIR")


def saved_tool_dirs(environ=None):
    """{VAR: DIR} from $HPCTUNNELS_DATA_DIR/settings/pytools.sh (written by lib/pytools.sh's hpclib_pytools set)."""
    environ = os.environ if environ is None else environ
    path = os.path.join(os.path.expanduser(environ.get("HPCTUNNELS_DATA_DIR") or "~/.local/tunnels"),
                        "settings", "pytools.sh")
    out = {}
    try:
        with open(path) as f:
            lines = f.read(1 << 16).splitlines()
    except OSError:
        return out
    for line in lines:
        name, sep, value = line.partition("=")
        if not sep or name not in TOOL_DIR_VARS:
            continue
        try:
            words = shlex.split(value)
        except ValueError:
            continue
        if len(words) == 1 and words[0].startswith("/"):
            out[name] = words[0]
    return out


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
    RECHECK = 60     # seconds before a missing uv or pixi is looked for again

    def __init__(self, config=None, sandbox: 'rest_sandbox.Sandbox' = None, data_dir=None, which=None,
                 environ=None, sync_env=None):
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
        self.sync_env = dict(sync_env or {})   # the server config's environment.all + environment.syncs
        self.data_dir = os.path.join(data_dir, "envs") if data_dir else None
        self._which = which
        self._environ = environ
        self._tools = None
        self._tools_at = 0
        self._lock = threading.Lock()
        self.syncs = {}

    # -- the tools -----------------------------------------------------------

    def env(self):
        """The syncs' environment: the server's, with the uv and pixi directories saved for this cluster (lib/pytools.sh,
        the console's cluster Settings) where the environment doesn't set them."""
        env = dict(self._environ) if self._environ is not None else rest_sandbox.child_env()
        for k, v in saved_tool_dirs(env).items():
            if not env.get(k):
                env[k] = v
        return env

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
            # cached; looked for again after a minute while one is missing (it may have been installed since)
            fresh = self._tools is not None and (len(self._tools) == len(MANAGERS)
                                                 or time.time() - self._tools_at < self.RECHECK)
            if fresh and not refresh:
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
            self._tools, self._tools_at = found, time.time()
        return found

    def describe(self):
        tools = self.tools()
        return {
            "managers": {m: ({"available": True, "version": tools[m]["version"]} if m in tools else
                             {"available": False}) for m in MANAGERS},
            "sync_modules": self.modules,
            "sync_environment": sorted(self.sync_env),
            "how": "put pyproject.toml (+ uv.lock) or pixi.toml (+ pixi.lock) in a project directory, sync it "
                   "with POST /envs/sync, then submit a template whose `environment` points at the project "
                   "(python_project, for example). Jobs only activate the environment; they never install. "
                   "\"squash\": true packs the environment into one SquashFS file that jobs mount (POST /envs/squash "
                   "and /envs/unsquash on their own); \"core\": DIR builds it on another environment, installing "
                   "only what it adds.",
        }

    # -- projects ------------------------------------------------------------

    @staticmethod
    def side_path(project, manager, name, suffix):
        """hpclib's files beside an environment, in its project: .venv.sqfs, .venv.hpclib.json (uv); for pixi
        .pixi-NAME.sqfs and so on (outside .pixi/, which pixi tidies)."""
        return os.path.join(project, (".venv" if manager == "uv" else f".pixi-{name}") + suffix)

    @classmethod
    def read_record(cls, info):
        """What the last sync recorded about the environment (rest of hpclib's sidecar JSON), or {}."""
        try:
            with open(cls.side_path(info["project"], info["manager"], info.get("environment") or "default",
                                    RECORD_SUFFIX)) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _marker(path):
        """The operation working on an environment (its marker file), unless it is stale."""
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        pid = data.get("pid")
        try:
            os.kill(int(pid), 0)                # the server that started it is gone: stale
        except (OSError, TypeError, ValueError):
            return None
        if time.time() - float(data.get("started") or 0) > 24 * 3600:
            return None
        return data

    @classmethod
    def detect(cls, project, manager="auto", name="default", _depth=0):
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
            manifest = pyproject
            unpacked = os.path.exists(os.path.join(env_path, "bin", "python"))
            out.update(environment=None, locked="uv.lock" in files)
            if "pyproject.toml" not in files:
                out["problem"] = "uv needs a pyproject.toml"
        else:
            env_path = os.path.join(project, ".pixi", "envs", name)
            manifest = os.path.join(project, "pixi.toml" if "pixi.toml" in files else "pyproject.toml")
            unpacked = os.path.isdir(os.path.join(env_path, "conda-meta"))
            out.update(environment=name, manifest=manifest, locked="pixi.lock" in files)
            if not os.path.isfile(manifest):
                out["problem"] = "pixi needs a pixi.toml (or a pyproject.toml with [tool.pixi])"
        out["path"] = env_path
        image = cls.side_path(project, manager, name, IMAGE_SUFFIX)
        record = cls.read_record(dict(out, environment=out.get("environment") or name))
        # an image hpclib made wins over files a tool put back in the mount point (uv run, pixi run outside hpclib)
        squashed = os.path.isfile(image) and (record.get("squashed", True) if record else not unpacked)
        out.update(image=image if squashed else None, squashed=squashed, unpacked=unpacked and not squashed,
                   ready=squashed or unpacked)
        if squashed and unpacked:
            out["note"] = (f"{env_path} has files again (a sync outside hpclib?), which jobs don't see: they mount "
                           f"{image}. Sync through hpclib to change the environment, or unsquash it.")
        if record:
            out.update(synced=record.get("synced"), python=record.get("python"),
                       packages=len(record.get("packages") or []))
            if record.get("failed"):
                out.update(ready=False, problem="its last sync failed; sync it again")
        try:
            with open(manifest, errors="replace") as f:
                declared = rest_manifests.read_core(f.read(1 << 20))
            if declared.get("core"):
                out["core_declared"] = declared
        except (OSError, rest_manifests.ManifestError):
            pass
        marker = cls._marker(cls.side_path(project, manager, name, MARKER_SUFFIX))
        if marker:
            out.update(busy=marker, ready=False,
                       problem=f"a {marker.get('kind', 'sync')} ({marker.get('id')}) is working on it; wait for it")
        core = record.get("core")
        if core and _depth == 0:
            try:
                ci = cls.detect(core["project"], manager, core.get("environment") or "default", _depth=1)
            except EnvironmentError_ as e:
                ci = {"ready": False, "problem": str(e)}
            out["core"] = {"project": core["project"], "environment": core.get("environment"),
                           "path": ci.get("path") or core.get("path"), "image": ci.get("image"),
                           "squashed": bool(ci.get("squashed")), "ready": bool(ci.get("ready")),
                           "synced": ci.get("synced"), "built_on": core.get("synced")}
            if not ci.get("ready"):
                out.update(ready=False, problem=f"its core environment ({core['project']}) isn't usable: "
                                                f"{ci.get('problem') or 'not synced'}")
            elif ci.get("synced") != core.get("synced"):
                out.update(ready=False, problem=f"its core ({core['project']}) was synced again since this "
                                                "environment was built on it; sync this one again")
        return out

    def prelude(self, info):
        """Bash lines that activate `info`'s environment (run inside the job's sandbox)."""
        env, q = info["path"], shlex.quote
        missing = (f"echo {q('hpclib: the ' + info['manager'] + ' environment in ' + info['project'] + ' is missing or broken inside the job; sync it again (sync_environment / POST /envs/sync)')} >&2; exit 4")
        core = info.get("core")
        if info["manager"] == "uv":
            lines = [
                f"export VIRTUAL_ENV={q(env)}",
                'export PATH="$VIRTUAL_ENV/bin:$PATH"' if not core else
                f'export PATH="$VIRTUAL_ENV/bin:{_dq(core["path"])}/bin:$PATH"',
                "unset PYTHONHOME",
                f'[ -x "$VIRTUAL_ENV/bin/python" ] || {{ {missing}; }}',
            ]
            if core:     # the core's packages come through _hpclib_core.pth, so its site-packages must be there
                lines.append(f'[ -d {q(core["path"])}/lib ] || {{ {missing}; }}')
            return lines
        lines = [f"export PIXI_PROJECT_ROOT={q(info['project'])} PIXI_ENVIRONMENT_NAME={q(info['environment'])} "
                 "PIXI_IN_SHELL=1",
                 "unset PYTHONHOME"]
        activate = lambda prefix: [                                     # noqa: E731
            f"export CONDA_PREFIX={q(prefix)}",
            f'[ -d "$CONDA_PREFIX/conda-meta" ] || {{ {missing}; }}',
            'for hpc_activate in "$CONDA_PREFIX"/etc/conda/activate.d/*.sh; do',
            '  [ -f "$hpc_activate" ] && . "$hpc_activate"',
            "done",
            "unset hpc_activate"]
        if not core:
            return lines + ['export PATH="%s/bin:$PATH"' % _dq(env)] + activate(env)
        # a layer on a core: the core's interpreter and libraries, the layer's packages through the user site
        # (PYTHONUSERBASE: site-packages there come before the core's, .pth files included)
        cp = core["path"]
        return lines + [
            f'export PATH="{_dq(env)}/bin:{_dq(cp)}/bin:$PATH"',
            f'export LD_LIBRARY_PATH="{_dq(env)}/lib:{_dq(cp)}/lib${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"',
            f"export PYTHONUSERBASE={q(env)} HPCLIB_ENV_LAYER={q(env)}",
            "unset PYTHONNOUSERSITE",
        ] + activate(cp) + activate(env) + [f"export CONDA_PREFIX={q(cp)}   # the interpreter's prefix"]

    def job_images(self, info):
        """(image, mount point) pairs a job in this environment mounts: squashed environments (and its core's)."""
        out = []
        core = info.get("core")
        if core and core.get("squashed") and core.get("image"):
            out.append((core["image"], core["path"]))
        if info.get("squashed"):
            out.append((info["image"], info["path"]))
        return out

    @staticmethod
    def job_envs(info):
        """The environment directories a job in this one uses (it and its core), for the job registry."""
        return [info["path"]] + ([info["core"]["path"]] if info.get("core") else [])

    def job_binds(self, info):
        """Read-only binds a job in this environment needs besides the project."""
        if info["manager"] == "uv":
            uv = self.tools().get("uv")
            if uv and os.path.isdir(uv["dirs"]["python"]):
                return [uv["dirs"]["python"]]
        return []

    # -- the manifest, for a layer on a core ------------------------------------

    def layer_manifest(self, info, core):
        """Write (or take out) the core's pins and [tool.hpclib] core in `info`'s manifest; True if it changed."""
        manifest = info.get("manifest") or os.path.join(info["project"], "pyproject.toml")
        try:
            with open(manifest) as f:
                text = f.read()
        except OSError as e:
            raise EnvironmentError_(422, f"can't read {manifest}: {e}")
        try:
            if core is None:
                new = rest_manifests.set_core(text, None)
                new = (rest_manifests.set_uv_constraints(new, [], "") if info["manager"] == "uv" else
                       rest_manifests.set_pixi_constraints(new, [], "", manifest.endswith("pyproject.toml")))
            else:
                rel = rest_manifests.relative(core["project"], info["project"])
                packages = self.read_record(core).get("packages") or []
                new = rest_manifests.set_core(text, rel, core.get("environment") if info["manager"] == "pixi" else None)
                if info["manager"] == "uv":
                    pins = [f"{p['name']}=={p['version']}" for p in packages if p.get("version")]
                    new = rest_manifests.set_uv_constraints(new, pins, rel)
                else:
                    pins = [(p["name"], p["version"], p.get("build") or "*") for p in packages
                            if p.get("kind", "conda") == "conda" and p.get("version")]
                    new = rest_manifests.set_pixi_constraints(new, pins, rel, manifest.endswith("pyproject.toml"))
        except rest_manifests.ManifestError as e:
            raise EnvironmentError_(422, f"{manifest}: {e}", project=info["project"])
        if new == text:
            return False
        tmp = manifest + ".hpclib-tmp"
        with open(tmp, "w") as f:
            f.write(new)
        os.chmod(tmp, os.stat(manifest).st_mode & 0o7777)
        os.replace(tmp, manifest)
        return True

    def check_core(self, info, core):
        """Refuse a core this environment can't be built on (EnvironmentError_ 422)."""
        if core["project"] == info["project"]:
            raise EnvironmentError_(422, "an environment can't be its own core")
        if core.get("manager") != info["manager"]:
            raise EnvironmentError_(422, f"the core ({core['project']}) is a {core.get('manager')} project and this "
                                         f"one {info['manager']}: a core and its layers use the same tool")
        if self.read_record(core).get("core"):
            raise EnvironmentError_(422, f"{core['project']} is itself built on a core; build on that core instead")
        if not core.get("ready"):
            raise EnvironmentError_(422, f"the core ({core['project']}) isn't ready: "
                                         f"{core.get('problem') or 'sync it first'}")
        record = self.read_record(core)
        if not record.get("packages") or (info["manager"] == "uv" and not (record.get("python") or {}).get("base_executable")):
            raise EnvironmentError_(422, f"the core ({core['project']}) was synced before hpclib recorded its "
                                         "packages; sync it again first")

    # -- syncing, squashing --------------------------------------------------

    def _record_path(self, sync_id, ext):
        return os.path.join(self.data_dir, f"{sync_id}.{ext}") if self.data_dir else None

    def _start(self, kind, token_name, info, script_fn, **fields):
        """Run an operation (sync, squash, unsquash) on an environment in the background; its record."""
        if info.get("manager") is None:
            raise EnvironmentError_(422, info.get("problem") or "nothing to sync", project=info["project"])
        manager = info["manager"]
        manifest = info.get("manifest") or os.path.join(info["project"], "pyproject.toml")
        if not os.path.isfile(manifest):
            raise EnvironmentError_(422, info.get("problem") or f"no {os.path.basename(manifest)}",
                                    project=info["project"])
        busy = info.get("busy")
        if busy and busy.get("id") not in self.syncs:
            raise EnvironmentError_(409, f"a {busy.get('kind', 'sync')} ({busy.get('id')}) is working on this "
                                         "environment; wait for it", running=busy.get("id"))
        tool = self.tools().get(manager)
        if tool is None:
            raise EnvironmentError_(503, f"{manager} is not installed where the REST server runs; install it "
                                         f"(it is a single binary) or set `environments.{manager}` in config.json",
                                    managers=self.describe()["managers"])
        if self.data_dir is None:
            raise EnvironmentError_(503, "the server has no data directory for sync logs")
        name = info.get("environment") or "default"
        with self._lock:
            running = [s for s in self.syncs.values() if s["state"] == "running"]
            for s in running:
                if s["project"] == info["project"] and (s.get("environment") or "default") == name:
                    if s.get("kind", "sync") == kind:
                        return dict(s, already_running=True)
                    raise EnvironmentError_(409, f"a {s.get('kind', 'sync')} ({s['id']}) is working on this "
                                                 "environment; wait for it", running=s["id"])
            if len(running) >= self.max_running:
                raise EnvironmentError_(429, f"{len(running)} syncs are already running; try again shortly")
            op_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
            record = {"id": op_id, "kind": kind, "token": token_name, "project": info["project"], "manager": manager,
                      "environment": info.get("environment"), "state": "running", "started": time.time(),
                      "finished": None, "exit_code": None}
            record.update(fields)
            self.syncs[op_id] = record
        try:
            os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
            script, command = script_fn(info, tool)
            record["command"] = command
            with open(self._record_path(op_id, "sh"), "w") as f:
                f.write(script)
            with open(self.side_path(info["project"], manager, name, MARKER_SUFFIX), "w") as f:
                json.dump({"id": op_id, "kind": kind, "pid": os.getpid(), "started": record["started"]}, f)
        except BaseException:
            with self._lock:
                self.syncs.pop(op_id, None)
            raise
        threading.Thread(target=self._run, args=(record, info), daemon=True).start()
        return dict(record)

    def sync(self, token_name, info, update=False, squash=None, core=None):
        """Start `uv sync` / `pixi install` for a detected project; returns its record. `squash`: True packs the
        environment into one SquashFS image afterwards, False leaves it unpacked, None keeps it as it is. `core`:
        a detected core environment to build this one on (its packages aren't installed again; the manifest gets
        its versions as constraints and [tool.hpclib] core), or None for a stand-alone environment."""
        if squash is None:
            squash = bool(info.get("squashed"))
        mksquashfs = self.squashfs_tool("mksquashfs") if squash else None
        if core is not None:
            self.check_core(info, core)
        changed = False
        if info.get("manager") in MANAGERS and not info.get("busy") and os.path.isfile(
                info.get("manifest") or os.path.join(info["project"], "pyproject.toml")):
            changed = self.layer_manifest(info, core)     # the lock follows: no --locked this time
        core_record = self.read_record(core) if core is not None else None
        core_ref = None
        if core is not None:
            core_ref = {"project": core["project"], "environment": core.get("environment"), "path": core["path"],
                        "synced": core_record.get("synced")}
        return self._start("sync", token_name, info,
                           lambda i, t: self._script(i, t, bool(update), squash, core, core_record, changed, mksquashfs),
                           update=bool(update), squash=bool(squash), core=core_ref, manifest_changed=changed)

    def squash(self, token_name, info):
        """Pack an unpacked, synced environment into its SquashFS image (no sync)."""
        if info.get("squashed"):
            raise EnvironmentError_(409, f"{info['path']} is already squashed into {info['image']}")
        if not info.get("ready"):
            raise EnvironmentError_(422, f"nothing to squash: {info.get('problem') or 'sync it first'}")
        tool = self.squashfs_tool("mksquashfs")
        return self._start("squash", token_name, info, lambda i, t: self._op_script(i, t, "squash", tool))

    def unsquash(self, token_name, info):
        """Unpack a squashed environment into its directory again, and remove the image."""
        if not info.get("squashed"):
            raise EnvironmentError_(409, f"{info['path']} isn't squashed")
        tool = self.squashfs_tool("unsquashfs")
        return self._start("unsquash", token_name, info, lambda i, t: self._op_script(i, t, "unsquash", tool))

    def squashfs_tool(self, name):
        """mksquashfs or unsquashfs where the server runs (PATH, then /usr/sbin, /sbin), else a 503 at once:
        before a long sync, not after it."""
        path = os.pathsep.join([self.env().get("PATH", os.defpath), "/usr/sbin", "/sbin"])
        found = shutil.which(name, path=path)
        if not found:
            raise EnvironmentError_(503, f"squashing needs {name} (squashfs-tools) where the REST server runs; "
                                         "install it (or load its module in environments.modules)")
        return found

    def _paths(self, info):
        """(environment dir, image, info tmp, pins tmp), relative to the project where the project holds them."""
        project, name = info["project"], info.get("environment") or "default"
        rel = lambda p: os.path.relpath(p, project)        # noqa: E731
        return (rel(info["path"]), rel(self.side_path(project, info["manager"], name, IMAGE_SUFFIX)),
                rel(self.side_path(project, info["manager"], name, ".hpclib-info.tmp")),
                rel(self.side_path(project, info["manager"], name, ".hpclib-pins.tmp")))

    def _describe_lines(self, info, tool):
        """Bash lines (in the project, the environment unpacked) that write its interpreter and packages for the
        record: what layers on it need."""
        q = shlex.quote
        env, _, info_tmp, pins_tmp = self._paths(info)
        lines = [f"rm -f {q(info_tmp)} {q(pins_tmp)}",
                 f'if [ -x {q(env)}/bin/python ]; then {q(env)}/bin/python -c {q(_INFO_PY)} > {q(info_tmp)} || true; fi']
        if info["manager"] == "uv":
            lines.append(f"{q(tool['path'])} export --frozen --no-hashes --no-emit-project --no-header "
                         f"> {q(pins_tmp)} 2> /dev/null || rm -f {q(pins_tmp)}")
        else:
            lines.append(f"{q(tool['path'])} list --json --frozen --no-install --manifest-path {q(info['manifest'])} "
                         f"-e {q(info['environment'])} > {q(pins_tmp)} 2> /dev/null || rm -f {q(pins_tmp)}")
        return lines

    def _script(self, info, tool, update, squash, core=None, core_record=None, manifest_changed=False,
                mksquashfs=None):
        project, q = info["project"], shlex.quote
        env, image, _, _ = self._paths(info)
        skip = sorted({p["name"] for p in (core_record or {}).get("packages") or []})
        if info["manager"] == "uv":
            command = [tool["path"], "sync"]
            if info["locked"] and not update and not manifest_changed:
                command.append("--locked")
            if squash:
                command.append("--compile-bytecode")   # the image is read-only: no .pyc written later
            if core is not None:
                command += ["--python", core_record["python"]["base_executable"]]
                for n in skip:
                    command += ["--no-install-package", n]
            exports = {"UV_CACHE_DIR": tool["dirs"]["cache"], "UV_PYTHON_INSTALL_DIR": tool["dirs"]["python"],
                       "UV_LINK_MODE": "copy", "UV_NO_PROGRESS": "true"}
            writable = [project, tool["dirs"]["cache"], tool["dirs"]["python"]]
        else:
            command = [tool["path"], "install", "--manifest-path", info["manifest"], "-e", info["environment"]]
            if info["locked"] and not update and not manifest_changed:
                command.append("--locked")
            for n in skip:
                command += ["--skip", n]
            exports = {"PIXI_CACHE_DIR": tool["dirs"]["cache"], "PIXI_NO_PROGRESS": "true", "NO_COLOR": "1"}
            writable = [project, tool["dirs"]["cache"]]
        for d in writable[1:]:
            os.makedirs(d, exist_ok=True)
        shown = command if len(command) < 16 else command[:12] + [f"... ({len(skip)} core packages skipped)"]
        body = ["set -e", f"cd {q(project)}",
                # a squashed environment's mount point is empty: the sync builds it again there
                f"echo {q('$ ' + ' '.join(shown))}",
                " ".join(q(c) for c in command)]
        if core is not None and info["manager"] == "uv":
            site = core_record["python"]["site_packages"]
            body += [f"hpc_site=$({q(env)}/bin/python -c 'import sysconfig; print(sysconfig.get_paths()[\"purelib\"])')",
                     f'printf "%s\\n" {q(site)} > "$hpc_site/_hpclib_core.pth"',
                     f"echo {q('built on the core ' + core['project'] + ' (its packages through _hpclib_core.pth)')}"]
        body += self._describe_lines(info, tool)
        if squash:
            body += _squash_lines(env, image, mksquashfs or self.squashfs_tool("mksquashfs"))
        else:
            body.append(f"if [ -e {q(image)} ]; then rm -f {q(image)} && "
                        f"echo {q('removed ' + image + ': the environment is unpacked in ' + env + ' again')}; fi")
        return self._wrap(project, body, exports, writable, tool, [mksquashfs] if squash else []), command

    def _op_script(self, info, tool, kind, squashfs):
        project, q = info["project"], shlex.quote
        env, image, _, _ = self._paths(info)
        if kind == "squash":
            body = ["set -e", f"cd {q(project)}"] + self._describe_lines(info, tool) + _squash_lines(env, image, squashfs)
            command = [squashfs, env, image]
        else:
            body = ["set -e", f"cd {q(project)}",
                    f"echo {q('$ unsquashfs -d ' + env + ' ' + image)}",
                    f"mkdir -p {q(env)}",
                    f"find {q(env)} -mindepth 1 -delete",
                    f"{q(squashfs)} -f -d {q(env)} {q(image)} > /dev/null || {{ find {q(env)} -mindepth 1 -delete; exit 6; }}",
                    f"rm -f {q(image)}",
                    f"echo {q('unpacked ' + image + ' into ' + env + '; the image is gone')}"]
            command = [squashfs, "-f", "-d", env, image]
        writable = [project] + ([tool["dirs"]["cache"]] if info["manager"] == "pixi" else [])
        return self._wrap(project, body, {"NO_COLOR": "1"}, writable, tool, [squashfs]), command

    def _wrap(self, project, body, exports, writable, tool, programs=()):
        q = shlex.quote
        body = "\n".join(body) + "\n"
        lines = ["#!/bin/bash", f"# hpclib environment operation for {project}"]
        lines += [f"export {k}={q(v)}" for k, v in sorted(exports.items())]
        # the server config's variables (e.g. UV_INDEX_URL, HTTPS_PROXY); they may override the defaults above
        lines += [f"export {k}={q(v)}" for k, v in sorted(self.sync_env.items())]
        if self.modules:
            lines.append(_MODULE_INIT)
            lines += [f"module load {q(m)} || {{ echo 'hpclib: could not load module {m}' >&2; exit 3; }}"
                      for m in self.modules]
        launch = None
        if self.sandbox is not None:
            try:
                # a sync downloads packages, so it gets the network even where jobs don't
                launch, _ = self.sandbox.launch("#!/bin/bash", body, writable,
                                                extra_ro=[tool["path"]] + [p for p in programs if p], network=True)
            except rest_sandbox.SandboxError as e:
                raise EnvironmentError_(503, f"can't sandbox the sync: {e}")
        return "\n".join(lines) + "\n\n" + ("\n".join(launch) + "\n" if launch else body)

    def _run(self, record, info):
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
        try:
            self._finish(record, info, state == "succeeded")
        except OSError as e:
            with open(log_path, "a") as log:
                log.write(f"hpclib: could not record the environment: {e}\n")
        with self._lock:
            record.update(state=state, exit_code=code, finished=time.time())
        try:
            with open(self._record_path(record["id"], "json"), "w") as f:
                json.dump(record, f, indent=2)
        except OSError:
            pass

    def _finish(self, record, info, ok):
        """Update the environment's record (hpclib's sidecar JSON) after an operation, and tidy up."""
        project, manager, name = info["project"], info["manager"], info.get("environment") or "default"
        side = lambda suffix: self.side_path(project, manager, name, suffix)     # noqa: E731
        try:
            old = self.read_record(info)
            new = dict(old)
            if ok:
                py = _read_json(side(".hpclib-info.tmp"))
                pins = _read_text(side(".hpclib-pins.tmp"))
                if py:
                    new["python"] = py
                if pins is not None:
                    new["packages"] = _parse_pins(manager, pins)
                if record["kind"] == "sync":
                    new.update(synced=time.time(), core=record.get("core"), failed=None)
                    if record.get("core") and manager == "pixi":
                        # a pixi layer has no interpreter of its own: the core's
                        new["python"] = self.read_record({"project": record["core"]["project"], "manager": manager,
                                                         "environment": record["core"].get("environment")}).get("python")
            elif record["kind"] == "sync":
                new["failed"] = time.time()
            new.update(manager=manager, project=project, environment=info.get("environment"), path=info["path"],
                       image=side(IMAGE_SUFFIX), squashed=os.path.isfile(side(IMAGE_SUFFIX)))
            new = {k: v for k, v in new.items() if v is not None}
            tmp = side(RECORD_SUFFIX) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(new, f, indent=1)
            os.replace(tmp, side(RECORD_SUFFIX))
        finally:
            for suffix in (".hpclib-info.tmp", ".hpclib-pins.tmp", MARKER_SUFFIX):
                try:
                    os.unlink(side(suffix))
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


_INFO_PY = ("import json, sys, sysconfig; print(json.dumps({'executable': sys.executable, "
            "'base_executable': getattr(sys, '_base_executable', sys.executable), "
            "'version': '%d.%d.%d' % sys.version_info[:3], 'site_packages': sysconfig.get_paths()['purelib']}))")
PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:==\s*([^\s;\\]+))?")


def _dq(path):
    """A path for inside double quotes in bash."""
    return re.sub(r'([\\"$`])', r"\\\1", path)


def _read_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _read_text(path):
    try:
        with open(path, errors="replace") as f:
            return f.read(8 << 20)
    except OSError:
        return None


def _parse_pins(manager, text):
    """The packages an environment holds: [{name, version, build?, kind}] from uv export or pixi list --json."""
    if manager == "pixi":
        try:
            items = json.loads(text or "[]")
        except ValueError:
            return []
        return [{"name": p["name"], "version": p.get("version"), "build": p.get("build"),
                 "kind": p.get("kind") or "conda"} for p in items if isinstance(p, dict) and p.get("name")]
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        m = PIN_RE.match(line)
        if m:
            out.append({"name": re.sub(r"[-_.]+", "-", m.group(1)).lower(), "version": m.group(2), "kind": "pypi"})
    return out


def _squash_lines(env, image, mksquashfs):
    """Bash lines, run in the project after the environment is built: pack it into `image` and empty `env`, the
    image's mount point. `env` is emptied just before the new image replaces the old one, so a job submitted
    meanwhile gets the old image or the new one (submissions are refused while this runs anyway)."""
    q = shlex.quote
    tmp = image + ".tmp"
    return [
        f"echo {q('$ mksquashfs ' + env + ' ' + image)}",
        f"rm -f {q(tmp)}",
        # -quiet is newer than squashfs-tools 4.3 (RHEL 8): the summary goes to /dev/null instead
        f"{q(mksquashfs)} {q(env)} {q(tmp)} -noappend -no-xattrs -no-progress > /dev/null || {{ rm -f {q(tmp)}; exit 6; }}",
        f"find {q(env)} -mindepth 1 -delete",
        f"mv -f {q(tmp)} {q(image)}",
        f'echo "packed {env} into {image} ($(du -h {q(image)} | cut -f1)); jobs mount it on {env}"',
    ]


_MODULE_INIT = (
    "if ! type module >/dev/null 2>&1; then\n"
    "  for f in /etc/profile.d/lmod.sh /etc/profile.d/modules.sh /usr/share/lmod/lmod/init/bash; do\n"
    "    if [ -f \"$f\" ]; then . \"$f\"; break; fi\n"
    "  done\n"
    "fi"
)
