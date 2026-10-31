"""
What a Python environment is, as the files beside it say: the part of hpclib's environment handling that only reads.
rest_envs.py (the REST server, which syncs inside the agents' sandbox) and the Workspace (which syncs as the user)
both import it, so the two agree on what "ready" means, on the record a sync leaves, and on the lines a job runs to
use an environment.

  detect(project, manager="auto", name="default", read_core=None)
      uv (pyproject.toml, uv.lock) or pixi (pixi.toml, [tool.pixi]); whether `.venv` or `.pixi/envs/NAME` exists;
      the record, the busy marker, a squashed image, a core it was built on -> {project, manager, path, ready, ...}
  find_project(start)       the nearest directory at or above a file that holds a manifest or an environment
  status(info)              one word and a reason for a person: ready, not prepared, stale, busy, failed, none found
  prelude(info)             the bash lines that activate it in a job (never resolving or downloading anything)
  read_record(info), marker(path), side_path(...)    hpclib's files beside an environment (below)

hpclib's files beside an environment, in its project: `.venv.hpclib.json` (pixi: `.pixi-NAME.hpclib.json`) is what
the last sync left (`synced`, its interpreter, its packages, its core, `failed`); `.venv.hpclib-syncing` marks an
operation at work; `.venv.sqfs` is the image of a squashed environment.

Python 3.6 or newer, standard library only (the Workspace runs wherever the login node's python3 is).
"""
import json
import os
import re
import shlex
import time

MANAGERS = ("uv", "pixi")
ENV_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
IMAGE_SUFFIX = ".sqfs"
RECORD_SUFFIX = ".hpclib.json"
MARKER_SUFFIX = ".hpclib-syncing"
MANIFESTS = ("pixi.toml", "pixi.lock", "pyproject.toml", "uv.lock")
MARKER_STALE_SECONDS = 24 * 3600


class EnvironmentError_(Exception):
    """A request about environments that can't be done; `status` is the HTTP status."""

    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def dq(path):
    """A path for inside double quotes in bash."""
    return re.sub(r'([\\"$`])', r"\\\1", path)


def has_pixi_table(pyproject):
    try:
        with open(pyproject, errors="replace") as f:
            return re.search(r"^\s*\[tool\.pixi[\].]", f.read(1 << 20), re.M) is not None
    except OSError:
        return False


# -- the files beside an environment ----------------------------------------------------------------------------------

def side_path(project, manager, name, suffix):
    """hpclib's files beside an environment, in its project: .venv.sqfs, .venv.hpclib.json (uv); for pixi
    .pixi-NAME.sqfs and so on (outside .pixi/, which pixi tidies)."""
    return os.path.join(project, (".venv" if manager == "uv" else ".pixi-{}".format(name)) + suffix)


def read_record(info):
    """What the last sync recorded about the environment (hpclib's sidecar JSON), or {}."""
    try:
        with open(side_path(info["project"], info["manager"], info.get("environment") or "default", RECORD_SUFFIX)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def marker(path):
    """The operation working on an environment (its marker file), unless it is stale: its process gone, or a day old."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        os.kill(int(data.get("pid")), 0)                # the process that started it is gone: stale
    except (OSError, TypeError, ValueError):
        return None
    if time.time() - float(data.get("started") or 0) > MARKER_STALE_SECONDS:
        return None
    return data


# -- detection --------------------------------------------------------------------------------------------------------

def detect(project, manager="auto", name="default", read_core=None, _depth=0):
    """What `project` (a real directory) holds. `read_core(text)` (rest_manifests.read_core) reads the core a
    manifest declares; the Workspace doesn't pass it."""
    if not os.path.isdir(project):
        raise EnvironmentError_(422, "{} is not a directory".format(project))
    if not ENV_NAME_RE.fullmatch(name or ""):
        raise EnvironmentError_(400, "environment names are letters, digits, _ and -")
    files = [f for f in MANIFESTS if os.path.isfile(os.path.join(project, f))]
    pyproject = os.path.join(project, "pyproject.toml")
    if manager == "auto":
        if "pixi.toml" in files or "pixi.lock" in files or ("pyproject.toml" in files and has_pixi_table(pyproject)):
            manager = "pixi"
        elif "pyproject.toml" in files or "uv.lock" in files:
            manager = "uv"
        else:
            return {"project": project, "manager": None, "files": files, "ready": False,
                    "problem": "no pyproject.toml (uv) or pixi.toml (pixi) in this directory"}
    if manager not in MANAGERS:
        raise EnvironmentError_(400, "`manager` must be auto, uv or pixi, not {!r}".format(manager))
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
    image = side_path(project, manager, name, IMAGE_SUFFIX)
    record = read_record(dict(out, environment=out.get("environment") or name))
    # an image hpclib made wins over files a tool put back in the mount point (uv run, pixi run outside hpclib)
    squashed = os.path.isfile(image) and (record.get("squashed", True) if record else not unpacked)
    out.update(image=image if squashed else None, squashed=squashed, unpacked=unpacked and not squashed,
               ready=squashed or unpacked)
    if squashed and unpacked:
        out["note"] = ("{} has files again (a sync outside hpclib?), which jobs don't see: they mount {}. Sync through "
                       "hpclib to change the environment, or unsquash it.".format(env_path, image))
    if record:
        out.update(synced=record.get("synced"), python=record.get("python"), packages=len(record.get("packages") or []))
        if record.get("failed"):
            out.update(ready=False, problem="its last sync failed; sync it again")
    if read_core is not None and manifest and os.path.isfile(manifest):
        try:
            with open(manifest, errors="replace") as f:
                declared = read_core(f.read(1 << 20))
            if declared.get("core"):
                out["core_declared"] = declared
        except (OSError, ValueError):
            pass
    busy = marker(side_path(project, manager, name, MARKER_SUFFIX))
    if busy:
        out.update(busy=busy, ready=False,
                   problem="a {} ({}) is working on it; wait for it".format(busy.get("kind", "sync"), busy.get("id")))
    core = record.get("core")
    if core and _depth == 0:
        try:
            ci = detect(core["project"], manager, core.get("environment") or "default", read_core, _depth=1)
        except EnvironmentError_ as e:
            ci = {"ready": False, "problem": str(e)}
        out["core"] = {"project": core["project"], "environment": core.get("environment"),
                       "path": ci.get("path") or core.get("path"), "image": ci.get("image"),
                       "squashed": bool(ci.get("squashed")), "ready": bool(ci.get("ready")),
                       "synced": ci.get("synced"), "built_on": core.get("synced")}
        if not ci.get("ready"):
            out.update(ready=False, problem="its core environment ({}) isn't usable: {}".format(
                core["project"], ci.get("problem") or "not synced"))
        elif ci.get("synced") != core.get("synced"):
            out.update(ready=False, problem="its core ({}) was synced again since this environment was built on it; "
                                            "sync this one again".format(core["project"]))
    return out


def find_project(start, home=None):
    """The nearest directory at or above `start` (a file or a directory) with a manifest or an environment, or None.
    Stops at the home directory's parent: a pyproject.toml in / or /home isn't somebody's project."""
    path = os.path.abspath(start)
    if not os.path.isdir(path):
        path = os.path.dirname(path)
    stop = os.path.dirname(os.path.abspath(home)) if home else None
    for _ in range(40):
        if path == stop:
            return None
        if any(os.path.isfile(os.path.join(path, f)) for f in MANIFESTS) or \
                os.path.isdir(os.path.join(path, ".venv")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return None


def newer_manifest(info):
    """The manifest or lock file changed after the last sync: (name, mtime) of the newest, or None. Compares with
    the record's `synced`; an environment with no record (made by hand) has nothing to compare."""
    synced = info.get("synced")
    if not synced:
        return None
    newest = None
    for name in ("pyproject.toml", "uv.lock", "pixi.toml", "pixi.lock"):
        path = os.path.join(info["project"], name)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        if mtime > float(synced) + 1 and (newest is None or mtime > newest[1]):
            newest = (name, mtime)
    return newest


def status(info):
    """(word, reason) for a person: ready, not prepared, stale, busy, failed, none found. `tool missing` is the
    caller's to say, since it depends on where the tool is looked for."""
    if not info.get("manager"):
        return "none found", info.get("problem") or "no manifest found"
    if info.get("busy"):
        return "busy", info.get("problem") or "an operation is working on it"
    record_failed = bool(read_record(info).get("failed"))
    if record_failed:
        return "failed", info.get("problem") or "its last sync failed"
    if not info.get("ready"):
        if info.get("core") and info.get("problem"):         # built on a core that is gone or was synced again
            return "stale", info["problem"]
        return "not prepared", "no environment in {} yet".format(info.get("path") or info["project"])
    changed = newer_manifest(info)
    if changed:
        return "stale", "{} changed after the last sync".format(changed[0])
    return "ready", "prepared {}".format(time.strftime("%Y-%m-%d", time.localtime(info["synced"]))) \
        if info.get("synced") else "an environment exists (made outside hpclib)"


# -- activation -------------------------------------------------------------------------------------------------------

def prelude(info, hint="prepare it again in the Workspace"):
    """Bash lines that activate `info`'s environment (a job runs them, sandboxed or not); `hint` ends the message a
    job prints when the environment isn't there."""
    env, q = info["path"], shlex.quote
    missing = "echo {} >&2; exit 4".format(q(
        "hpclib: the {} environment in {} is missing or broken inside the job; {}".format(
            info["manager"], info["project"], hint)))
    core = info.get("core")
    if info["manager"] == "uv":
        lines = [
            "export VIRTUAL_ENV={}".format(q(env)),
            'export PATH="$VIRTUAL_ENV/bin:$PATH"' if not core else
            'export PATH="$VIRTUAL_ENV/bin:{}/bin:$PATH"'.format(dq(core["path"])),
            "unset PYTHONHOME",
            '[ -x "$VIRTUAL_ENV/bin/python" ] || {{ {}; }}'.format(missing),
        ]
        if core:     # the core's packages come through _hpclib_core.pth, so its site-packages must be there
            lines.append("[ -d {}/lib ] || {{ {}; }}".format(q(core["path"]), missing))
        return lines
    lines = ["export PIXI_PROJECT_ROOT={} PIXI_ENVIRONMENT_NAME={} PIXI_IN_SHELL=1".format(
        q(info["project"]), q(info["environment"])), "unset PYTHONHOME"]

    def activate(prefix):
        return ["export CONDA_PREFIX={}".format(q(prefix)),
                '[ -d "$CONDA_PREFIX/conda-meta" ] || {{ {}; }}'.format(missing),
                'for hpc_activate in "$CONDA_PREFIX"/etc/conda/activate.d/*.sh; do',
                '  [ -f "$hpc_activate" ] && . "$hpc_activate"',
                "done",
                "unset hpc_activate"]

    if not core:
        return lines + ['export PATH="{}/bin:$PATH"'.format(dq(env))] + activate(env)
    # a layer on a core: the core's interpreter and libraries, the layer's packages through the user site
    # (PYTHONUSERBASE: site-packages there come before the core's, .pth files included)
    cp = core["path"]
    return lines + [
        'export PATH="{}/bin:{}/bin:$PATH"'.format(dq(env), dq(cp)),
        'export LD_LIBRARY_PATH="{}/lib:{}/lib${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"'.format(dq(env), dq(cp)),
        "export PYTHONUSERBASE={} HPCLIB_ENV_LAYER={}".format(q(env), q(env)),
        "unset PYTHONNOUSERSITE",
    ] + activate(cp) + activate(env) + ["export CONDA_PREFIX={}   # the interpreter's prefix".format(q(cp))]
