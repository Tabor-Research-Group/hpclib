"""
Sandboxing for template jobs, and a probe of what the cluster supports.

A template job's body (its script.sh) runs inside Singularity or
Apptainer, which both run as you, without root, on most clusters. The
job script itself still runs on the host first: it exports the
parameters and loads the template's modules, then starts the body in
the container. Inside the container:

  - the directories the submitting token may use (its allowed
    directories, narrowed by the server's --allow list; just the job's
    workdir if neither restricts it) are bound read-write, along with
    any `writable` directories from the config;
  - everything else that is bound is read-only;
  - nothing else from the host is visible: not your home directory, not
    other projects, not ~/.local/tunnels. The container gets its own
    process table and private, writable /tmp, /var/tmp and /dev/shm (scratch
    that is deleted with the job). Its home directory is not writable, so a
    program that writes there fails with "Read-only file system" instead of
    writing files that would be lost when the job ends.

By default the container is a "host image": an almost empty directory
whose /usr, /etc and /opt (and any `binds`, such as a software tree like
/sw) are the host's own, bound read-only. Programs and modules from the
host therefore work unchanged; nothing has to be built to match the
host. Give `image` (a .sif file or a sandbox directory) to run in an
image of your own instead; then only `binds` are added.

Your account: on clusters it usually comes from a directory service
(LDAP, through SSSD), which the container can't reach, so the host's
/etc/passwd alone doesn't know your uid and programs that look it up
(Postgres, ssh, some MPI and Python libraries) fail. The job therefore
writes, before the container starts, a passwd and a group file: the
host's local entries plus your own account and groups (as `getent` gives
them on the host), and an nsswitch.conf that reads only those files. They
are bound read-only over the host's in the container. They hold nothing
the job couldn't already learn as you, and add no host directory or
socket to the sandbox.

Pyxis (method "pyxis", or "auto" on a SLURM cluster without Singularity or
Apptainer where `srun` has the Pyxis plugin's --container-* options): the job
script starts the body with `srun --container-image=IMAGE
--container-mounts=SRC:DST[:ro],... --no-container-mount-home`, with the
same read-only and read-write mounts, masks and scratch directories. Pyxis
(enroot) needs a real image: hpclib imports a small base image once per user
(`enroot import docker://ubuntu:24.04`, the config's `base_image`) into its
data directory and keeps it; the host's /usr, /etc and /opt and the `binds`
are mounted over it, as for the host image. A `.sqsh` file in `image` is used
instead. Jobs never pull an image. enroot gives the container no network of
its own and no process table of its own, so a Pyxis job has the host's
network (GET /sandbox says so: `network_isolated` is false) and the
`network` setting has no effect. A squashed Python environment is mounted
with squashfuse inside the job, before the container starts (it needs
squashfuse and FUSE on the compute nodes). Pyxis needs SLURM: not with the
local scheduler.

Podman (method "podman", or "auto" where there is no Singularity or
Apptainer): for machines that aren't clusters, rootless podman runs the
same host image (`--rootfs`) with the same read-only and read-write binds,
as you (`--userns=keep-id`), and adds what Apptainer doesn't: no Linux
capabilities, no-new-privileges, the runtime's seccomp filter, and no
network (`network`: "none", the default; environment syncs, which
download, always get the network). Images are never pulled by a job
(`--pull=never`); give `image` as a podman image name to run in one you
pulled. CPU and memory limits (HPC_JOB_CPUS, HPC_JOB_MEMORY, which the
local scheduler sets) only hold where cgroups v2 delegates the cpu and
memory controllers to you; `limits()` says whether they do, since podman
itself only warns and runs the job unlimited. SELinux labels are turned
off for the container (relabelling the host's /usr to bind it would be
worse). Supplementary groups are kept with the crun runtime; with runc a
job has only your primary group.

Config, the `sandbox` key of the server's config.json:

  {"method": "auto",          # auto | singularity | pyxis | podman | bubblewrap | charliecloud | none
   "runtime": null,           # "apptainer", "singularity", "podman", "srun" (pyxis), or a path; default: whichever is found
   "image": null,             # default: the host image, built in the server's data directory
                              #   (podman: also an image name, e.g. "docker.io/library/ubuntu:24.04";
                              #   pyxis: a .sqsh file; default: the imported base_image)
   "base_image": "docker://ubuntu:24.04",   # pyxis: imported once with enroot when `image` isn't given
   "network": "none",         # podman / bubblewrap / charliecloud: "none" (jobs have no network) or "default"
   "binds": ["/sw"],          # extra read-only binds (software trees, reference data, ...)
   "writable": [],            # extra read-write binds, besides the token's directories
   "scratch": "job",          # /tmp in the container: "job" (a per-job directory under
                              #   $TMPDIR, deleted afterwards), "session" (the runtime's small
                              #   in-memory default), or a directory
   "flags": [],               # extra runtime flags, for every job
   "gpus": "auto",            # Singularity/Apptainer's --nv (NVIDIA GPUs and their driver in the container):
                              #   "auto" for jobs that ask SLURM for GPUs (gres gpu), "always", or "never".
                              #   Podman: put its device flag (e.g. --device nvidia.com/gpu=all) in `flags`
   "allow_unsandboxed": false}  # with "auto": run jobs unsandboxed if no runtime is found

Bubblewrap (method "bubblewrap") and Charliecloud ("charliecloud", requiring
Bubblewrap) provide token-scoped binds, separate processes, opt-in networking
and devices, no-new-privileges and a libseccomp policy. Charliecloud's writable
binds are confined by locked source mounts in an outer Bubblewrap. The console
installs pinned private builds; see docs/sandbox-runtimes.md. These backends
share the kernel and require site resource limits. `devices` lists explicit
/dev nodes; `boundary_runtime` optionally selects Charliecloud's Bubblewrap.

With no `sandbox` key, jobs run unsandboxed, as before; the server warns
about it and GET /sandbox recommends a config.

Limits: a sandboxed job runs on one node (srun and other SLURM commands
don't work inside it), and what it can read is what is bound, so a
program installed somewhere not bound fails with "not found". GET
/sandbox lists the module roots to add to `binds`.

Standard library only.
"""
import getpass
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

try:
    from . import rest_bwrap
except ImportError:
    import rest_bwrap

__all__ = ["SandboxError", "Sandbox", "build_host_image", "probe", "child_env", "SANDBOXED", "set_extra_path",
           "extra_path"]

# Set by the Python launcher setup_agents writes when it loads a Python module:
# the environment from before that, `env -0` on an inherited descriptor.
# Commands and jobs the server starts get it, so they don't inherit the server's
# Python modules. Older launchers supplied a base64 environment variable.
BASE_ENV_VAR = "HPCLIB_BASE_ENV"
BASE_ENV_FD_VAR = "HPCLIB_BASE_ENV_FD"
SECRET_ENV_PREFIXES = ("HPC_REST_TOKEN",)


def _decode_base_env(text):
    import base64
    try:
        raw = base64.b64decode(text.encode(), validate=False)
    except (ValueError, TypeError):
        return None
    return _parse_base_env(raw)


def _parse_base_env(raw):
    env = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep and key:
            env[key.decode("utf-8", "surrogateescape")] = value.decode("utf-8", "surrogateescape")
    return env or None


def _read_base_env_fd(text):
    try:
        fd = int(text)
        chunks, offset = [], 0
        while True:
            # pread leaves the descriptor's offset alone, including for concurrent calls.
            chunk = os.pread(fd, 65536, offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
        return _parse_base_env(b"".join(chunks))
    except (AttributeError, OSError, OverflowError, ValueError):
        return None


# Directories the config's `extra_path` adds to the end of PATH (see set_extra_path)
_EXTRA_PATH = ()


def extra_path():
    """The directories set_extra_path added, as a list."""
    return list(_EXTRA_PATH)


def set_extra_path(dirs):
    """
    Add `dirs` to the end of PATH, for this process and for the commands and jobs it starts (child_env), and
    take back the ones an earlier call added. For clusters whose non-interactive shells, which is what a
    server started over SSH gets, lack the directory that SLURM or a container runtime is in, though a login
    shell has it: the config's `extra_path` names those directories.
    """
    global _EXTRA_PATH
    dirs = tuple(dirs or ())
    current = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p not in _EXTRA_PATH]
    os.environ["PATH"] = os.pathsep.join(current + [d for d in dirs if d not in current])
    _EXTRA_PATH = dirs


# The config's `server_modules`: environment modules the server loads for itself, and for the commands and jobs it
# starts, on top of the Python launcher's own (which child_env leaves out). For clusters where SLURM (and its
# configuration and libraries) comes from a module, as on Bright Cluster Manager's `slurm/25.05`.
SERVER_MODULE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+@/-]{0,127}")
MODULE_INIT = (
    'if ! type module > /dev/null 2>&1; then\n'
    '  for f in /etc/profile.d/lmod.sh /etc/profile.d/modules.sh /usr/share/lmod/lmod/init/bash '
    '/usr/share/Modules/init/bash; do\n'
    '    if [ -f "$f" ]; then . "$f" > /dev/null 2>&1; type module > /dev/null 2>&1 && break; fi\n'
    '  done\n'
    'fi\n')
MODULE_ENV_BEFORE, MODULE_ENV_AFTER = b"\0HPCLIB-ENV-BEFORE\0", b"\0HPCLIB-ENV-AFTER\0"
# what a module load changes that isn't the modules' doing: the shell's own bookkeeping, and Lmod's/Modules' state
MODULE_ENV_SKIP = ("_", "SHLVL", "PWD", "OLDPWD", "LOADEDMODULES", "_LMFILES_")
MODULE_ENV_SKIP_PREFIXES = ("BASH_FUNC_", "__MODULES_", "_ModuleTable", "LMOD_", "MODULES_", "_LMOD", "__LMOD")
_SERVER_MODULES = ()
_MODULE_ENV = {}     # VAR -> ("set", value) or ("list", entries put before, entries put after)
_MODULE_SAVED = {}   # VAR -> what this process had before the modules' changes (None: unset)
_MODULE_ERROR = None  # why the last load of the config's modules failed
_MODULE_NOTE = None   # when they changed nothing: what the shell had loaded and said


def module_status():
    """One sentence on the config's `server_modules`: what loaded and what it changed, or why it didn't."""
    if _MODULE_ERROR:
        return f"server_modules did not load: {_MODULE_ERROR}"
    if not _SERVER_MODULES:
        return "no server_modules are set"
    changed = ", ".join(sorted(_MODULE_ENV)) or "nothing (the modules changed no variables" + (
        f"; {_MODULE_NOTE}" if _MODULE_NOTE else "") + ")"
    return f"server_modules {', '.join(_SERVER_MODULES)} loaded, changing {changed}"


def server_modules():
    """The modules load_server_modules loaded, as a list."""
    return list(_SERVER_MODULES)


def _parse_env(data):
    env = {}
    for entry in data.split(b"\0"):
        key, sep, value = entry.decode("utf-8", "replace").partition("=")
        if sep and key:
            env[key] = value
    return env


def _module_changes(before, after):
    """What loading modules changed, between two environments: {VAR: ("set", value) | ("list", before, after)}."""
    changes = {}
    for key, value in after.items():
        if key in MODULE_ENV_SKIP or key.startswith(MODULE_ENV_SKIP_PREFIXES) or before.get(key) == value:
            continue
        old = [p for p in (before.get(key) or "").split(os.pathsep) if p]
        new = [p for p in value.split(os.pathsep) if p]
        if os.pathsep in value and all(p in new for p in old):
            # a PATH-like list that only grew: keep where the new entries went (PATH, LD_LIBRARY_PATH, ...)
            first = new.index(old[0]) if old else 0
            changes[key] = ("list", new[:first], [p for p in new[first:] if p not in old])
        else:
            changes[key] = ("set", value)
    return changes


def _apply_module_env(env, changes):
    for key, change in changes.items():
        if change[0] == "set":
            env[key] = change[1]
            continue
        parts = [p for p in (env.get(key) or "").split(os.pathsep) if p]
        env[key] = os.pathsep.join([p for p in change[1] if p not in parts] + parts +
                                   [p for p in change[2] if p not in parts])


def check_module_names(modules):
    """`modules` as a list of module names (NAME or NAME/VERSION), or raise ValueError."""
    if modules is None:
        return []
    if (not isinstance(modules, list) or len(modules) > 16 or
            not all(isinstance(m, str) and SERVER_MODULE_RE.fullmatch(m.strip()) for m in modules)):
        raise ValueError("`server_modules` must be a list of module names (at most 16), e.g. [\"slurm/25.05\"]")
    out = []
    for module in modules:
        if module.strip() not in out:
            out.append(module.strip())
    return out


def resolve_server_modules(modules, timeout=60):
    """
    What loading `modules` in a login shell (the one this server's commands use) changes in the environment:
    {VAR: ("set", value) | ("list", before, after)}. Raises ValueError, saying why, when they don't load.
    """
    modules = check_module_names(modules)
    if not modules:
        return {}
    script = (MODULE_INIT + "type module > /dev/null 2>&1 || { echo 'there is no module command on this cluster' >&2; exit 4; }\n"
              '# a login shell can load the module itself (a profile that loads DefaultModules, which a plain unload of one\n'
              '# module may not undo), and an environment can say one is loaded while its variables are not set; either way\n'
              '# loading it again changes nothing, so purge first: what the load then changes is the modules\' own doing\n'
              'module --force purge > /dev/null 2>&1 || module purge > /dev/null 2>&1 || true\n'
              "printf '\\0HPCLIB-ENV-BEFORE\\0'; env -0\n"
              'module load "$@" 1>&2 || exit 3\n'
              "printf '\\0HPCLIB-ENV-AFTER\\0'; env -0\n")
    try:
        res = subprocess.run(["bash", "-lc", script, "hpclib-modules"] + modules, capture_output=True, timeout=timeout,
                             stdin=subprocess.DEVNULL, env=child_env(modules=False))
    except subprocess.TimeoutExpired:
        raise ValueError(f"loading {', '.join(modules)} took more than {timeout} seconds")
    except OSError as e:
        raise ValueError(f"could not start a shell to load {', '.join(modules)}: {e}")
    out = res.stdout
    if res.returncode != 0 or MODULE_ENV_AFTER not in out or MODULE_ENV_BEFORE not in out:
        why = " ".join(res.stderr.decode("utf-8", "replace").split())[-300:] or f"exit status {res.returncode}"
        raise ValueError(f"could not load {', '.join(modules)}: {why}")
    before, _, after = out.partition(MODULE_ENV_BEFORE)[2].partition(MODULE_ENV_AFTER)
    before, after = _parse_env(before), _parse_env(after)
    changes = _module_changes(before, after)
    global _MODULE_NOTE
    said = " ".join(res.stderr.decode("utf-8", "replace").split())[-200:]
    _MODULE_NOTE = None if changes else (
        f"after a purge the shell had LOADEDMODULES={before.get('LOADEDMODULES', '')!r}, PATH={before.get('PATH', '')!r}"
        + (f", and `module load` said {said!r}" if said else ""))
    return changes


def set_server_modules(modules, changes):
    """
    Use `changes` (from resolve_server_modules) in this process and in what it starts, in place of an earlier
    call's. `modules` is only remembered, for server_modules().
    """
    global _SERVER_MODULES, _MODULE_ENV, _MODULE_SAVED, _MODULE_ERROR
    _MODULE_ERROR = None
    for key, old in _MODULE_SAVED.items():
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old
    _MODULE_SAVED = {key: os.environ.get(key) for key in changes}
    _apply_module_env(os.environ, changes)
    _SERVER_MODULES, _MODULE_ENV = tuple(modules or ()), dict(changes)
    set_extra_path(_EXTRA_PATH)   # put its directories back at the end of the PATH the modules changed


def load_server_modules(modules):
    """resolve_server_modules and set_server_modules in one; returns the module list."""
    global _MODULE_ERROR
    modules = check_module_names(modules)
    try:
        changes = resolve_server_modules(modules)
    except ValueError as e:
        _MODULE_ERROR = str(e)
        raise
    set_server_modules(modules, changes)
    return modules


def child_env(env=None, modules=True):
    """
    The environment for processes the server starts: `env`, or the one saved
    from before the Python launcher loaded its modules (else this process's),
    minus anything secret, with the config's `server_modules` applied
    (unless `modules` is false) and its `extra_path` at the end of PATH.
    """
    if env is None:
        saved_fd = os.environ.get(BASE_ENV_FD_VAR)
        saved = os.environ.get(BASE_ENV_VAR)
        env = ((_read_base_env_fd(saved_fd) if saved_fd else None) or
               (_decode_base_env(saved) if saved else None) or os.environ)
    env = dict(env)
    for key in list(env):
        if key.startswith(SECRET_ENV_PREFIXES) or key in (BASE_ENV_VAR, BASE_ENV_FD_VAR):
            del env[key]
    if modules and _MODULE_ENV:
        _apply_module_env(env, _MODULE_ENV)
    if _EXTRA_PATH:
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        env["PATH"] = os.pathsep.join(parts + [d for d in _EXTRA_PATH if d not in parts])
    return env

METHODS = ("auto", "singularity", "pyxis", "podman", "charliecloud", "bubblewrap", "none")
RUNTIMES = ("apptainer", "singularity")
PODMAN = "podman"
SRUN = "srun"
ENROOT = "enroot"
# the methods that sandbox a job (an effective method of either means jobs are sandboxed)
SANDBOXED = ("singularity", "pyxis", "podman", "charliecloud", "bubblewrap")
NAMESPACE_METHODS = ("charliecloud", "bubblewrap")
MANAGED_BIN = os.path.expanduser("~/.local/share/hpclib/sandbox-tools/bin")
NETWORKS = ("none", "default")
CONFIG_KEYS = {"method", "runtime", "image", "binds", "writable", "scratch", "flags", "allow_unsandboxed", "network",
               "storage", "gpus", "base_image", "boundary_runtime", "devices"}
GPU_MODES = ("auto", "always", "never")
# file systems where rootless podman's storage can't live: they refuse the chown to your subordinate ids that its
# overlay layers need ("chown ...: operation not permitted")
NETWORK_FILESYSTEMS = ("nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "lustre", "gpfs", "beegfs",
                       "fuse.glusterfs", "ceph", "fuse.ceph", "panfs", "afs")
# podman's --init program (catatonit), where distributions put it
PODMAN_INITS = ("/usr/libexec/podman/catatonit", "/usr/bin/catatonit", "/usr/libexec/catatonit/catatonit")
# a podman image reference: [registry/]name[:tag][@digest]
PODMAN_IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._/:-]{0,254}(@sha256:[0-9a-f]{64})?")
# what srun must offer (the Pyxis plugin) for jobs to be sandboxed with it; --container-readonly is used when there
PYXIS_REQUIRED = ("--container-image", "--container-mounts", "--container-workdir", "--container-env",
                  "--no-container-mount-home")
PYXIS_READONLY = "--container-readonly"
PYXIS_BASE_IMAGE = "docker://ubuntu:24.04"
# an image enroot imports: docker://[USER@][REGISTRY#]IMAGE[:TAG]
PYXIS_IMAGE_URI_RE = re.compile(r"docker://[A-Za-z0-9][A-Za-z0-9._/:@#-]{0,254}")
# host directories every host image binds read-only
HOST_IMAGE_BINDS = ("/usr", "/etc", "/opt")
# top-level directories that are symlinks into /usr on most current distributions
USR_LINKS = ("bin", "sbin", "lib", "lib64")
IMAGE_DIRS = ("usr", "etc", "opt", "tmp", "var/tmp", "home", "proc", "sys", "dev", "run", "root", "srv", "mnt")
# environment variables that would add binds behind the config's back
RUNTIME_BIND_VARS = ("SINGULARITY_BIND", "SINGULARITY_BINDPATH", "APPTAINER_BIND", "APPTAINER_BINDPATH")
# shells and interpreters a template body may use (it is passed with -c)
INTERPRETERS = re.compile(r"^(ba|z|k|da)?sh$|^python[0-9.]*$")


class SandboxError(Exception):
    """The configured sandbox can't run jobs; the message says why."""


def _string_list(config, key):
    value = config.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"sandbox `{key}` must be a list of strings")
    return value


def _absolute(path, key):
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        raise ValueError(f"sandbox `{key}` entries must be absolute paths, not {path!r}")
    return os.path.normpath(path)


def build_host_image(path, targets=()):
    """
    Create (or top up) the host image: a directory with an empty
    mount point for each bind and the host's /bin-style symlinks. It
    holds no programs of its own; everything comes from the binds.
    """
    parent = os.path.dirname(os.path.normpath(path))
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)   # e.g. /var/tmp/USER/hpclib-podman: yours alone
    os.makedirs(path, mode=0o755, exist_ok=True)
    for name in USR_LINKS:
        host, mine = os.path.join("/", name), os.path.join(path, name)
        if os.path.lexists(mine):
            continue
        if os.path.islink(host):
            os.symlink(os.readlink(host), mine)
        elif os.path.isdir(host):
            os.makedirs(mine, exist_ok=True)
    for d in IMAGE_DIRS:
        os.makedirs(os.path.join(path, d), exist_ok=True)
    for f in ("passwd", "group", "hosts", "resolv.conf"):   # the runtime fills these in
        open(os.path.join(path, "etc", f), "a").close()
    add_mount_points(path, targets)
    return path


def add_mount_points(image, targets):
    """
    Mount points for binds the image doesn't have yet (needed where the
    runtime's underlay is off). Not for paths in your home directory or
    /tmp: the container gets fresh empty ones mounted over the image's,
    and the runtime makes mount points inside those itself.
    """
    fresh = [os.path.realpath(os.path.expanduser("~")), "/tmp", "/var/tmp"]
    for target in targets:
        if any(target == f or target.startswith(f + "/") for f in fresh):
            continue
        mine = os.path.join(image, target.lstrip("/"))
        if os.path.lexists(mine):
            continue
        if os.path.isdir(target):
            os.makedirs(mine, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(mine), exist_ok=True)
            open(mine, "a").close()


class Sandbox:
    """Turns a template body into a job script section that runs it in the configured sandbox."""

    def __init__(self, config=None, data_dir=None, which=shutil.which, scheduler="slurm"):
        self.configured = config is not None
        config = dict(config or {"method": "none"})
        unknown = set(config) - CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown sandbox keys {sorted(unknown)}; known: {sorted(CONFIG_KEYS)}")
        self.method = config.get("method", "auto")
        if self.method not in METHODS:
            raise ValueError(f"sandbox `method` must be one of {', '.join(METHODS)}")
        self.runtime = config.get("runtime")
        if self.runtime is not None and not isinstance(self.runtime, str):
            raise ValueError("sandbox `runtime` must be a string")
        image = config.get("image")
        if image is not None and not isinstance(image, str):
            raise ValueError("sandbox `image` must be a path")
        if image and not image.startswith(("/", "~")):
            if self.method != "podman" or not PODMAN_IMAGE_RE.fullmatch(image):
                raise ValueError("sandbox `image` must be an absolute path (or, with method podman, an image name); "
                                 "Pyxis jobs never pull, so give a .sqsh file or leave `image` out for the base image")
            self.image = image
        else:
            self.image = _absolute(image, "image") if image else None
        # podman's storage: "auto" (podman's own, unless that is on a network file system: then a local
        # directory, /var/tmp/USER/hpclib-podman), "default" (podman's own), or a directory
        self.storage = config.get("storage", "auto")
        if not isinstance(self.storage, str) or not (self.storage in ("auto", "default") or
                                                     os.path.isabs(os.path.expanduser(self.storage))):
            raise ValueError('sandbox `storage` must be "auto", "default", or an absolute directory')
        self.base_image = config.get("base_image", PYXIS_BASE_IMAGE)
        if not isinstance(self.base_image, str) or not PYXIS_IMAGE_URI_RE.fullmatch(self.base_image):
            raise ValueError("sandbox `base_image` must be a docker:// image reference (enroot imports it once, "
                             "for method pyxis)")
        self.boundary_runtime = config.get("boundary_runtime")
        if self.boundary_runtime is not None and not isinstance(self.boundary_runtime, str):
            raise ValueError("sandbox boundary_runtime must be a path or null")
        self.devices = [_absolute(d, "devices") for d in _string_list(config, "devices")]
        if any(not d.startswith("/dev/") or os.path.isdir(d) for d in self.devices):
            raise ValueError("sandbox devices must name individual paths under /dev")
        if self.method in NAMESPACE_METHODS and config.get("flags"):
            raise ValueError("Charliecloud/Bubblewrap flags are not accepted; use binds, writable, network and devices")
        self.scheduler = scheduler
        self._import = None
        self.network = config.get("network", "none")
        if self.network not in NETWORKS:
            raise ValueError(f"sandbox `network` must be one of {', '.join(NETWORKS)}")
        self.binds = [_absolute(b, "binds") for b in _string_list(config, "binds")]
        self.writable = [_absolute(w, "writable") for w in _string_list(config, "writable")]
        self.flags = _string_list(config, "flags")
        self.gpus = config.get("gpus", "auto")
        if self.gpus not in GPU_MODES:
            raise ValueError(f"sandbox `gpus` must be one of {', '.join(GPU_MODES)}")
        self.scratch = config.get("scratch", "job")
        if not isinstance(self.scratch, str) or not (self.scratch in ("job", "session") or os.path.isabs(
                os.path.expanduser(self.scratch))):
            raise ValueError('sandbox `scratch` must be "job", "session", or an absolute directory')
        self.allow_unsandboxed = config.get("allow_unsandboxed", False)
        if not isinstance(self.allow_unsandboxed, bool):
            raise ValueError("sandbox `allow_unsandboxed` must be true or false")
        self.data_dir = data_dir
        self.which = which
        self.hidden, self.read_only_paths = (), ()
        self._help_cache = {}
        self._podman_cache = {}
        self._lock = threading.Lock()

    # -- what will actually run -------------------------------------------

    def _find(self, names):
        for name in names:
            managed = os.path.join(MANAGED_BIN, name) if name in ("bwrap", "ch-run") else None
            path = (managed if managed and os.access(managed, os.X_OK) else
                    name if os.path.isabs(os.path.expanduser(name)) else self.which(name))
            if path and os.access(os.path.expanduser(path), os.X_OK):
                return os.path.expanduser(path)
        return None

    def find_runtime(self):
        """The Singularity/Apptainer runtime's absolute path, or None."""
        own = (self.runtime and not self._is_podman(self.runtime) and not self._is_srun(self.runtime)
               and os.path.basename(self.runtime) not in ("bwrap", "ch-run"))
        return self._find([self.runtime] if own else RUNTIMES)

    @staticmethod
    def _is_podman(name):
        return PODMAN in os.path.basename(name or "")

    @staticmethod
    def _is_srun(name):
        return os.path.basename(name or "") == SRUN

    def _auto_pyxis(self):
        """srun's path if `auto` may use Pyxis: SLURM jobs, and srun has the plugin's --container-image."""
        if self.scheduler == "local":
            return None
        srun = self._find([SRUN])
        return srun if srun and self.srun_supports(srun, "--container-image") else None

    def _locate(self):
        """(kind, path) of the runtime the method calls for; path is None if it isn't found."""
        for method, binary in (("bubblewrap", "bwrap"), ("charliecloud", "ch-run")):
            if self.method == method or (self.method == "auto" and os.path.basename(self.runtime or "") == binary):
                return method, self._find([self.runtime] if self.runtime else [binary])
        if self.method == "pyxis" or (self.method == "auto" and self._is_srun(self.runtime)):
            return "pyxis", self._find([self.runtime] if self.runtime else [SRUN])
        if self.method == "podman" or (self.method == "auto" and self._is_podman(self.runtime)):
            return "podman", self._find([self.runtime] if self.runtime else [PODMAN])
        if self.method == "singularity" or self.runtime:
            return "singularity", self.find_runtime()
        found = self.find_runtime()
        if found:
            return "singularity", found
        pyxis = self._auto_pyxis()
        if pyxis:
            return "pyxis", pyxis
        podman = self._find([PODMAN])
        if podman:
            return "podman", podman
        boundary = self._find(["bwrap"])
        return ("bubblewrap", boundary) if boundary else ("singularity", None)

    @property
    def host_image(self):
        return os.path.join(self.data_dir or tempfile.gettempdir(), "sandbox", "host")

    @property
    def pyxis_base_image(self):
        """The .sqsh file `base_image` is imported into, in the server's data directory."""
        return os.path.join(self.data_dir or tempfile.gettempdir(), "sandbox", "pyxis", "base.sqsh")

    def host_image_for(self, method):
        """
        Where the host image is for `method`. Rootless podman with subordinate ids sets the container up as a
        subordinate id, which a network file system judges as a stranger (it ignores capabilities): a host image
        in your NFS home then can't be opened ("crun: readlink ``: No such file or directory"). So for podman it
        is kept on a local disk, beside the jobs' storage, when the server's directory is on one.
        """
        if method == "pyxis":
            return self.pyxis_base_image
        path = self.host_image
        if method != "podman" or _filesystem_type(path) not in NETWORK_FILESYSTEMS:
            return path
        if self.storage not in ("auto", "default"):
            return os.path.join(os.path.normpath(os.path.expanduser(self.storage)), "host")
        return os.path.join("/var/tmp", getpass.getuser(), "hpclib-podman", "host")

    def resolve(self):
        """
        ("singularity", runtime path) or ("none", reason). Raises
        SandboxError when a sandbox is required but unavailable.
        """
        if self.method == "none":
            return "none", ("no `sandbox` in the server config" if not self.configured
                            else "the config sets sandbox method none")
        kind, runtime = self._locate()
        if runtime is None:
            wanted = self.runtime or ("ch-run" if kind == "charliecloud" else "bwrap" if kind == "bubblewrap"
                                      else PODMAN if kind == "podman" or self.method == "podman"
                                      else SRUN if self.method == "pyxis"
                                      else " or ".join(RUNTIMES) + (", srun with Pyxis, podman, or bubblewrap"
                                                                    if self.method == "auto" else ""))
            reason = (f"{wanted} was not found on the server's PATH; set sandbox `runtime` to its full path, or "
                      f"add its directory to `extra_path` (GET /sandbox shows what the node has)")
            if self.method == "auto":
                reason += f". Looked for: {self._auto_details()}. PATH: {os.environ.get('PATH', '')}"
            if self.method == "auto" and self.allow_unsandboxed:
                return "none", reason
            raise SandboxError(f"jobs must run sandboxed but {reason}")
        if kind in NAMESPACE_METHODS:
            boundary = runtime if kind == "bubblewrap" else self._find([self.boundary_runtime or "bwrap"])
            if not boundary:
                raise SandboxError("Charliecloud requires Bubblewrap to enforce read-only binds; install both from cluster Settings")
            required = ("--seccomp", "--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-cgroup", "--remount-ro")
            if kind == "bubblewrap":
                required += ("--disable-userns",)
            missing = [flag for flag in required if not self.srun_supports(boundary, flag)]
            if missing:
                raise SandboxError(f"{boundary} is missing {missing}; install a current Bubblewrap")
        if kind == "pyxis":
            self._check_pyxis(runtime)
        if self.image and self.image.startswith("/") and not os.path.exists(self.image):
            raise SandboxError(f"sandbox image {self.image} does not exist")
        if kind in NAMESPACE_METHODS and self.image and not os.path.isdir(self.image):
            if not os.path.isfile(self.image):
                raise SandboxError("Charliecloud/Bubblewrap image must be a directory or regular SquashFS file")
            try:
                with open(self.image, "rb") as source:
                    if source.read(4) not in (b"hsqs", b"sqsh"):
                        raise SandboxError("Charliecloud/Bubblewrap images must be directories or raw SquashFS files; SIF/ext3 are not supported")
            except OSError as error:
                raise SandboxError(f"cannot read sandbox image {self.image}: {error}") from error
        if self.image and not self.image.startswith("/") and kind != "podman":
            raise SandboxError(f"sandbox image {self.image} is a podman image name, and the runtime is {runtime}")
        return kind, runtime

    def _check_pyxis(self, srun):
        """Raise SandboxError unless Pyxis can run jobs: SLURM, the plugin's options on srun, and an image."""
        if self.scheduler == "local":
            raise SandboxError("the pyxis sandbox runs jobs with srun, and this server's scheduler is the local one "
                               "(no SLURM)")
        missing = [f for f in PYXIS_REQUIRED if not self.srun_supports(srun, f)]
        if missing:
            raise SandboxError(f"{srun} has no {', '.join(missing)}: the Pyxis plugin isn't loaded on this node "
                               f"(`{srun} --help | grep container` should list it)")
        if not self.image:
            state = self.base_image_state()
            if state["state"] != "ready":
                hint = {"importing": "it is being imported (enroot import); jobs can run when it is done",
                        "failed": f"the import failed: {state.get('error')}",
                        "missing": "import it with `rest_server.py --import-sandbox-image`"}[state["state"]]
                raise SandboxError(f"the Pyxis base image {state['path']} isn't there: {hint}")

    def _srun_help(self, srun):
        """(text, problem) of `srun --help`, cached: problem says why it couldn't be run or why it failed, else None."""
        with self._lock:
            key = ("srun", srun)
            if key not in self._help_cache:
                problem = None
                try:
                    out = subprocess.run([srun, "--help"], capture_output=True, text=True, timeout=30,
                                         stdin=subprocess.DEVNULL, env=child_env())
                    text = out.stdout + out.stderr
                    if out.returncode != 0 and "--container-image" not in text:
                        problem = f"exit status {out.returncode}: " + (" ".join(text.split())[-200:] or "no output")
                except subprocess.TimeoutExpired:
                    text, problem = "", "it took more than 30 seconds"
                except OSError as e:
                    text, problem = "", str(e)
                self._help_cache[key] = (text, problem)
            return self._help_cache[key]

    def srun_supports(self, srun, flag):
        """Whether `srun --help` lists `flag` (cached)."""
        return flag in self._srun_help(srun)[0]

    def _auto_details(self):
        """What automatic detection found for each runtime, in a sentence: why none of them was usable."""
        out = []
        for name in RUNTIMES:
            path = self._find([name])
            if path:
                out.append(f"{name} at {path}")
        if self.scheduler == "local":
            out.append("srun: not used (no SLURM here)")
        else:
            srun = self._find([SRUN])
            if srun is None:
                out.append("srun: not found")
            else:
                text, problem = self._srun_help(srun)
                if problem:
                    out.append(f"srun at {srun} did not run ({problem})")
                elif "--container-image" not in text:
                    out.append(f"srun at {srun} has no Pyxis plugin (its --help lacks --container-image)")
                else:
                    out.append(f"srun at {srun} has Pyxis")
        for name in (PODMAN, "bwrap"):
            out.append(f"{name} " + (f"at {self._find([name])}" if self._find([name]) else "not found"))
        out.append(module_status())
        return "; ".join(out)

    # -- the Pyxis base image --------------------------------------------------

    def base_image_state(self):
        """{"path", "source", "state": ready | importing | failed | missing, "error"} of the Pyxis base image."""
        path = self.pyxis_base_image
        state = {"path": path, "source": self.base_image}
        try:
            ready = os.path.getsize(path) > 0
        except OSError:
            ready = False
        with self._lock:
            imp = dict(self._import) if self._import else {}
        if ready:
            state["state"] = "ready"
        elif imp.get("state") == "importing":
            state["state"] = "importing"
        elif imp.get("state") == "failed":
            state.update(state="failed", error=imp.get("error"))
        else:
            state["state"] = "missing"
        return state

    def wants_base_image(self):
        """Whether jobs would use Pyxis with the base image, and it isn't there yet."""
        try:
            return self._locate()[0] == "pyxis" and not self.image and self.base_image_state()["state"] != "ready"
        except Exception:
            return False

    def import_base_image(self, wait=False, timeout=1800):
        """
        Import `base_image` with enroot into pyxis_base_image, once: in a thread, or here with `wait`. The file
        appears only when complete. Returns the state (see base_image_state).
        """
        state = self.base_image_state()
        if state["state"] in ("ready", "importing"):
            if wait and state["state"] == "importing":
                self._import["thread"].join()
                return self.base_image_state()
            return state
        enroot = self._find([ENROOT])
        if enroot is None:
            with self._lock:
                self._import = {"state": "failed", "error": f"enroot was not found on PATH; import it by hand: "
                                f"enroot import -o {self.pyxis_base_image} {self.base_image}"}
            return self.base_image_state()
        thread = threading.Thread(target=self._import_base_image, args=(enroot, timeout), daemon=True)
        with self._lock:
            self._import = {"state": "importing", "thread": thread}
        thread.start()
        if wait:
            thread.join()
        return self.base_image_state()

    def _enroot_env(self):
        """
        child_env() for running enroot as this user. Enroot keeps its working files in ENROOT_RUNTIME_PATH, which
        defaults to $XDG_RUNTIME_DIR/enroot, or /run/enroot where a server started over SSH has no such directory
        (no login session): "mkdir: cannot create directory '/run/enroot': Permission denied". Unless the
        environment sets it, it goes in a private directory in the server's sandbox directory.
        """
        env = child_env()
        if "ENROOT_RUNTIME_PATH" not in env:
            run = os.path.join(os.path.dirname(os.path.dirname(self.pyxis_base_image)), "enroot-run")
            os.makedirs(run, mode=0o700, exist_ok=True)
            env["ENROOT_RUNTIME_PATH"] = run
        return env

    def _import_base_image(self, enroot, timeout):
        path = self.pyxis_base_image
        partial = f"{path}.partial.{os.getpid()}"
        error = None
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            if os.path.lexists(partial):
                os.unlink(partial)
            res = subprocess.run([enroot, "import", "-o", partial, self.base_image], capture_output=True, text=True,
                                 timeout=timeout, stdin=subprocess.DEVNULL, env=self._enroot_env())
            if res.returncode != 0 or not os.path.isfile(partial) or not os.path.getsize(partial):
                error = ((res.stderr or res.stdout).strip()[-600:] or f"enroot import exited {res.returncode}")
            else:
                os.replace(partial, path)
        except subprocess.TimeoutExpired:
            error = f"enroot import did not finish within {timeout} s"
        except OSError as e:
            error = str(e)
        finally:
            if os.path.lexists(partial):
                os.unlink(partial)
        with self._lock:
            self._import = {"state": "failed" if error else "done", "error": error,
                            "thread": self._import["thread"] if self._import else None}

    def supports(self, runtime, flag):
        """Whether `runtime exec --help` lists `flag` (cached)."""
        with self._lock:
            if runtime not in self._help_cache:
                try:
                    out = subprocess.run([runtime, "exec", "--help"], capture_output=True, text=True,
                                         timeout=30, stdin=subprocess.DEVNULL, env=child_env()).stdout
                except (OSError, subprocess.TimeoutExpired):
                    out = ""
                self._help_cache[runtime] = out
            return flag in self._help_cache[runtime]

    def protect(self, hidden=(), read_only=()):
        """
        Paths a job must not see (`hidden`: hpclib's tokens and data, ~/.ssh) or change (`read_only`: what runs
        unsandboxed later, such as hpclib and the installed tunnels) even when a directory that holds them is bound.
        They are covered after the binds: an empty, read-only directory or file over a hidden one, and a read-only
        bind of a protected one over itself.
        """
        self.hidden = tuple(os.path.realpath(os.path.expanduser(p)) for p in hidden if p)
        self.read_only_paths = tuple(os.path.realpath(os.path.expanduser(p)) for p in read_only if p)
        return self

    @staticmethod
    def _within(path, parents):
        return any(path != d and path.startswith(d.rstrip("/") + "/") for d in parents)

    def mask_sources(self):
        """An empty directory and an empty file, both read-only, bound over hidden paths."""
        base = os.path.join(self.data_dir or os.path.join(tempfile.gettempdir(), f"hpclib-{os.getuid()}"),
                            "sandbox", "mask")
        os.makedirs(base, mode=0o700, exist_ok=True)
        empty_dir, empty_file = os.path.join(base, "empty-dir"), os.path.join(base, "empty-file")
        os.makedirs(empty_dir, exist_ok=True)
        if os.listdir(empty_dir):
            raise SandboxError(f"{empty_dir}, bound over hidden paths, must be empty")
        if not os.path.isfile(empty_file) or os.path.getsize(empty_file):
            with open(empty_file, "w"):
                pass
        os.chmod(empty_dir, 0o500)
        os.chmod(empty_file, 0o400)
        return empty_dir, empty_file

    def _protection(self, ro, rw):
        """
        (masks, protected, rebind): hidden paths inside a bind (with what covers each), protected paths inside a
        writable bind, and the binds inside a hidden path (bound again after the masks, e.g. a Python environment's
        under hpclib's data directory).
        """
        binds = ro + rw
        hidden = [h for h in self.hidden if os.path.lexists(h) and self._within(h, binds)]
        masks = []
        if hidden:
            empty_dir, empty_file = self.mask_sources()
            masks = [(empty_dir if os.path.isdir(h) else empty_file, h) for h in hidden]
        protected = [p for p in self.read_only_paths if os.path.exists(p) and self._within(p, rw)
                     and not (p in hidden or self._within(p, hidden))]
        rebind = [(d, "ro" if d in ro else "rw") for d in binds if self._within(d, hidden)]
        return masks, protected, rebind

    def plan(self, writable, extra_ro=(), images=()):
        """
        What a job would see: the method and the read-only and read-write
        binds. `extra_ro` adds read-only binds for this job (e.g. a Python
        environment's interpreter). `images` are (image, mount point)
        pairs: SquashFS (or ext3, SIF) images mounted read-only over a
        directory, e.g. a squashed Python environment on its .venv; they
        come after the binds, so their mount points may be inside them.
        `hidden` and `protected` (see protect) are covered after them, and
        `rebind` binds again over those.
        """
        method, detail = self.resolve()
        if method == "none":
            return {"method": "none", "reason": detail}
        ro, rw, missing = [], [], []
        writable = [os.path.normpath(d) for d in list(writable) + self.writable]
        for d in ([] if self.image else list(HOST_IMAGE_BINDS)) + self.binds + list(extra_ro):
            d = os.path.normpath(d)
            if d in ro or d in writable:
                continue
            (ro if os.path.exists(d) else missing).append(d)
        for d in writable:
            if d in rw:
                continue
            (rw if os.path.exists(d) else missing).append(d)
        mounted = []
        for image, target in images:
            image, target = os.path.normpath(image), os.path.normpath(target)
            if ":" in image + target or "," in image + target:
                raise SandboxError(f"can't mount {image!r} on {target!r} (':' or ',' in a path)")
            if not os.path.isfile(image):
                # without it the job would run on an empty mount point and fail on its first import
                raise SandboxError(f"the environment image {image} isn't there (sync the environment again)")
            if not os.path.isdir(target):
                raise SandboxError(f"{target}, where {image} is mounted, isn't a directory")
            mounted.append([image, target])
        plan = {"method": method, "runtime": detail, "image": self.image or self.host_image_for(method),
                "host_image": self.image is None, "read_only": ro, "read_write": rw}
        if mounted:
            plan["images"] = mounted
        masks, protected, rebind = self._protection(ro, rw)
        if masks or protected:
            plan.update(hidden=[h for _, h in masks], protected=protected, masks=masks, rebind=rebind)
        if method == "podman":
            plan["network"] = self.network
        if method in NAMESPACE_METHODS:
            plan.update(network=self.network, network_isolated=self.network == "none", pid_isolated=True,
                        rootfs_read_only=True, devices=self.devices,
                        boundary_runtime=detail if method == "bubblewrap" else self._find([self.boundary_runtime or "bwrap"]))
        if method == "pyxis":
            plan.update(network="host", network_isolated=False,
                        rootfs_read_only=self.srun_supports(detail, PYXIS_READONLY))
        if missing:
            plan["missing"] = missing   # not on this node; left out
        return plan

    # -- the job script -----------------------------------------------------

    @staticmethod
    def account_lines(flag="--bind"):
        """
        Bash for the job script, on the host: passwd, group and nsswitch.conf files that know your account,
        for the container's /etc (the host image binds the host's /etc, whose passwd usually doesn't: the
        account comes from SSSD, which the container can't reach). Sets hpc_sandbox_account to their binds,
        which come after the /etc bind so they cover the host's files.
        """
        if flag == "--ro-bind":
            lines = Sandbox.account_lines(flag="--bind")
            lines = [line.replace('--bind "$hpc_sandbox_etc/passwd:/etc/passwd:ro"', '--ro-bind "$hpc_sandbox_etc/passwd" /etc/passwd')
                     .replace('--bind "$hpc_sandbox_etc/group:/etc/group:ro"', '--ro-bind "$hpc_sandbox_etc/group" /etc/group')
                     .replace('--bind "$hpc_sandbox_etc/nsswitch.conf:/etc/nsswitch.conf:ro"', '--ro-bind "$hpc_sandbox_etc/nsswitch.conf" /etc/nsswitch.conf')
                     for line in lines]
            return lines
        return [
            'hpc_sandbox_etc=$(mktemp -d "${TMPDIR:-/tmp}/hpc-sandbox-etc.XXXXXX") || '
            '{ echo "hpclib: could not make the sandbox account files" >&2; exit 125; }',
            'hpc_sandbox_uid=$(id -u); hpc_sandbox_user=$(id -un 2>/dev/null || echo "user$hpc_sandbox_uid")',
            '{ grep -v "^[^:]*:[^:]*:$hpc_sandbox_uid:" /etc/passwd 2>/dev/null',
            '  getent passwd "$hpc_sandbox_uid" | head -n 1 | grep . ||',
            "    printf '%s:x:%s:%s::/nonexistent:/bin/bash\\n' \"$hpc_sandbox_user\" \"$hpc_sandbox_uid\" \"$(id -g)\"",
            '} > "$hpc_sandbox_etc/passwd"',
            '{ cat /etc/group 2>/dev/null',
            '  for hpc_sandbox_gid in $(id -G); do',
            '    grep -q "^[^:]*:[^:]*:$hpc_sandbox_gid:" /etc/group 2>/dev/null && continue',
            '    # the group with only you as a member: a directory group can list thousands',
            '    hpc_sandbox_g=$(getent group "$hpc_sandbox_gid" | head -n 1 | cut -d: -f1-3) || hpc_sandbox_g=',
            '    [ -n "$hpc_sandbox_g" ] || hpc_sandbox_g="group$hpc_sandbox_gid:x:$hpc_sandbox_gid"',
            "    printf '%s:%s\\n' \"$hpc_sandbox_g\" \"$hpc_sandbox_user\"",
            '  done',
            '} > "$hpc_sandbox_etc/group"',
            'chmod 644 "$hpc_sandbox_etc/passwd" "$hpc_sandbox_etc/group"',
            f'hpc_sandbox_account=({flag} "$hpc_sandbox_etc/passwd:/etc/passwd:ro" '
            f'{flag} "$hpc_sandbox_etc/group:/etc/group:ro")',
            'if [ -f /etc/nsswitch.conf ]; then',
            '  # look users and groups up in those files only: the directory service is out of reach in there',
            "  sed -E 's/^[[:space:]]*(passwd|group|shadow|gshadow|initgroups)[[:space:]]*:.*/\\1: files/' "
            '/etc/nsswitch.conf > "$hpc_sandbox_etc/nsswitch.conf"',
            '  chmod 644 "$hpc_sandbox_etc/nsswitch.conf"',
            f'  hpc_sandbox_account+=({flag} "$hpc_sandbox_etc/nsswitch.conf:/etc/nsswitch.conf:ro")',
            'fi',
        ]

    @staticmethod
    def interpreter(shebang):
        """The template body's interpreter, as an argument list for `-c`."""
        words = shlex.split(shebang[2:]) if shebang.startswith("#!") else ["/bin/bash"]
        program = words[1] if os.path.basename(words[0]) == "env" and len(words) > 1 else words[0]
        if not INTERPRETERS.match(os.path.basename(program)):
            raise SandboxError(f"a sandboxed template body must be a shell or Python script, not {shebang!r}")
        return words

    def gpu_flags(self, method, gpus):
        """--nv for a Singularity/Apptainer job that asked for `gpus` GPUs (with "auto"), or always/never."""
        if method != "singularity" or "--nv" in self.flags:
            return []
        return ["--nv"] if self.gpus == "always" or (self.gpus == "auto" and gpus > 0) else []

    def launch(self, shebang, body, writable, prelude=None, extra_ro=(), network=False, gpus=0, images=()):
        """
        Lines for the end of the job script: start `body` in the
        sandbox and exit with its status. Returns (lines, plan).
        `prelude` (bash lines) runs inside the sandbox first, in the
        same shell that then execs the body: e.g. activating a Python
        environment, whose activation scripts must not run outside.
        `network`: give it the network whatever the config says (podman;
        Apptainer always shares the host's), for environment syncs; "host"
        shares the host's network as Apptainer does (podman --network=host),
        for a service that talks to others on the node's loopback.
        `gpus`: how many GPUs the job asked SLURM for (see `gpus` in the config).
        `images`: (image, mount point) pairs to mount read-only (see plan).
        """
        plan = self.plan(writable, extra_ro, images)
        if plan["method"] == "none":
            return None, plan
        if images and plan["method"] == "podman":
            raise SandboxError("mounting an image (a squashed Python environment) needs Singularity or Apptainer; "
                               "podman can't. Sync the environment without `squash`")
        runtime, image = plan["runtime"], plan["image"]
        if plan["host_image"] and plan["method"] != "pyxis":   # Pyxis mounts the host's directories over its image
            build_host_image(image, plan["read_only"] + plan["read_write"] + plan.get("hidden", []) +
                             plan.get("protected", []))
        if plan["method"] in NAMESPACE_METHODS:
            if network:
                plan["network"] = "host" if network == "host" else "default"
            plan["network_isolated"] = plan["network"] == "none"
            if (self.gpus == "always" or self.gpus == "auto" and gpus > 0) and not self.devices:
                raise SandboxError("Charliecloud/Bubblewrap GPU jobs need explicit devices and driver-library binds; no host devices are exposed automatically")
            if plan["host_image"] and plan["method"] == "charliecloud":
                # ch-run requires destinations in its read-only image, including project paths under home.
                for target in plan["read_only"] + plan["read_write"] + plan.get("hidden", []) + plan.get("protected", []):
                    mine = os.path.join(image, target.lstrip("/"))
                    if not os.path.lexists(mine):
                        os.makedirs(mine if os.path.isdir(target) else os.path.dirname(mine), exist_ok=True)
                        if not os.path.isdir(target):
                            open(mine, "a").close()
            try:
                return rest_bwrap.launch(self, plan, shebang, body, prelude), plan
            except (rest_bwrap.BoundaryError, OSError) as error:
                raise SandboxError(str(error)) from error
        if plan["method"] == "podman":
            if network:
                plan["network"] = "host" if network == "host" else "default"
            return self._podman_launch(plan, shebang, body, prelude), plan
        if plan["method"] == "pyxis":
            return self._pyxis_launch(plan, shebang, body, prelude), plan
        # --no-home: no writable stand-in for $HOME (allowed directories under it are still bound)
        args = [runtime, "-q", "exec", "--contain", "--no-home", "--pid", "--ipc"]
        if self.supports(runtime, "--no-mount"):
            args += ["--no-mount", "bind-paths"]   # site-wide binds would add paths behind our back
        args += self._bind_args(plan, "--bind")
        nv = self.gpu_flags(plan["method"], gpus)
        if nv:
            plan["gpus"] = "nvidia"
        args += nv + self.flags
        quoted = " ".join(shlex.quote(a) for a in args)
        own_account = plan["host_image"] and "/etc" in plan["read_only"]

        delimiter = "HPC_SANDBOX_BODY_" + secrets.token_hex(8)
        while delimiter in body:
            delimiter = "HPC_SANDBOX_BODY_" + secrets.token_hex(8)
        lines = [
            f"# template body, run in a sandbox: read-write {', '.join(plan['read_write']) or '(none)'}; "
            f"read-only {', '.join(plan['read_only']) or '(none)'}",
            f"hpc_sandbox_body=$(cat <<'{delimiter}'",
            body.rstrip("\n"),
            delimiter,
            ")",
            f"unset {' '.join(RUNTIME_BIND_VARS)}",
            # The runtime replaces PATH and LD_LIBRARY_PATH with the image's defaults; pass the
            # host's (with the template's modules loaded) through. /tmp inside is the job's scratch.
            'export SINGULARITYENV_PATH="$PATH" APPTAINERENV_PATH="$PATH"',
            'if [ -n "${LD_LIBRARY_PATH:-}" ]; then',
            '  export SINGULARITYENV_LD_LIBRARY_PATH="$LD_LIBRARY_PATH" APPTAINERENV_LD_LIBRARY_PATH="$LD_LIBRARY_PATH"',
            'fi',
        ]
        if self.scratch == "job":
            lines += [
                'hpc_sandbox_tmp=$(mktemp -d "${TMPDIR:-/tmp}/hpc-sandbox.XXXXXX") || '
                '{ echo "hpclib: could not make the sandbox scratch directory" >&2; exit 125; }',
                'hpc_sandbox_workdir=(--workdir "$hpc_sandbox_tmp")',
            ]
        elif self.scratch == "session":
            lines += ["hpc_sandbox_tmp=", "hpc_sandbox_workdir=()"]
        else:
            lines += [
                f'hpc_sandbox_tmp=$(mktemp -d {shlex.quote(os.path.expanduser(self.scratch))}/hpc-sandbox.XXXXXX) || '
                '{ echo "hpclib: could not make the sandbox scratch directory" >&2; exit 125; }',
                'hpc_sandbox_workdir=(--workdir "$hpc_sandbox_tmp")',
            ]
        lines.append("export SINGULARITYENV_TMPDIR=/tmp APPTAINERENV_TMPDIR=/tmp")
        if own_account:
            lines += self.account_lines()
        else:
            lines += ["hpc_sandbox_etc=", "hpc_sandbox_account=()"]
        interp = " ".join(shlex.quote(w) for w in self.interpreter(shebang))
        if prelude:
            prelude_text = "\n".join(prelude) + '\nexec "$@"'
            delimiter = "HPC_SANDBOX_PRELUDE_" + secrets.token_hex(8)
            lines += [f"hpc_sandbox_prelude=$(cat <<'{delimiter}'", prelude_text, delimiter, ")"]
            start = f'/bin/bash -c "$hpc_sandbox_prelude" hpc-env {interp}'
        else:
            start = interp
        lines += [
            f'{quoted} "${{hpc_sandbox_account[@]}}" "${{hpc_sandbox_workdir[@]}}" --pwd "$PWD" {shlex.quote(image)} '
            f'{start} -c "$hpc_sandbox_body" hpc-job',
            "hpc_sandbox_status=$?",
            '[ -n "$hpc_sandbox_tmp" ] && rm -rf "$hpc_sandbox_tmp"',
            '[ -n "$hpc_sandbox_etc" ] && rm -rf "$hpc_sandbox_etc"',
            'if [ "$hpc_sandbox_status" = 255 ]; then',
            '  echo "hpclib: the job sandbox may have failed to start (exit 255); see the messages above" >&2',
            "fi",
            'exit "$hpc_sandbox_status"',
        ]
        return lines, plan

    # -- podman ---------------------------------------------------------------

    def podman_info(self, runtime):
        """Facts from `podman info` (cached): cgroups, the controllers you may use, the OCI runtime."""
        with self._lock:
            if runtime in self._podman_cache:
                return self._podman_cache[runtime]
        code, out, err = _run([runtime, "info", "--format", "json"], timeout=60)
        info = {"ok": code == 0, "error": None if code == 0 else (err.strip()[-500:] or "podman info failed")}
        try:
            parsed = json.loads(out) if code == 0 else {}
        except ValueError:
            parsed = {}
        host, store = parsed.get("host") or {}, parsed.get("store") or {}
        info.update(cgroup_version=host.get("cgroupVersion"), cgroup_manager=host.get("cgroupManager"),
                    cgroup_controllers=list(host.get("cgroupControllers") or []),
                    oci_runtime=(host.get("ociRuntime") or {}).get("name"),
                    rootless=(host.get("security") or {}).get("rootless"),
                    id_mappings=bool((host.get("idMappings") or {}).get("uidmap")),
                    graph_root=store.get("graphRoot"))
        with self._lock:
            self._podman_cache[runtime] = info
        return info

    def podman_storage(self, runtime):
        """
        The directory jobs' podman storage goes in (its storage.conf, graph root and run root), or None for
        podman's own. With "auto", podman's own unless that is on a network file system.
        """
        if self.storage == "default":
            return None
        if self.storage != "auto":
            return os.path.normpath(os.path.expanduser(self.storage))
        graph = self.podman_info(runtime).get("graph_root") or os.path.expanduser("~/.local/share/containers/storage")
        if _filesystem_type(graph) in NETWORK_FILESYSTEMS:
            return os.path.join("/var/tmp", getpass.getuser(), "hpclib-podman")
        return None

    def podman_storage_conf(self):
        """CONTAINERS_STORAGE_CONF for jobs' podman, or None (its own); for the local scheduler's clean-up."""
        try:
            method, runtime = self.resolve()
        except SandboxError:
            return None
        directory = self.podman_storage(runtime) if method == "podman" else None
        return os.path.join(directory, "storage.conf") if directory else None

    def limits(self):
        """
        Whether jobs' CPU and memory limits are enforced: {"enforced": bool (both), "cpu": bool, "memory": bool,
        "reason": text}. Only podman with cgroups v2 and the controllers delegated to you enforces them.
        """
        none = {"enforced": False, "cpu": False, "memory": False}
        try:
            method, detail = self.resolve()
        except SandboxError as e:
            return dict(none, reason=str(e))
        if method != "podman":
            return dict(none, reason="only the podman sandbox applies CPU and memory limits outside "
                                     f"SLURM (this sandbox: {method})")
        info = self.podman_info(detail)
        if not info["ok"]:
            return dict(none, reason=f"`podman info` failed: {info['error']}")
        if info["cgroup_version"] != "v2":
            return dict(none, reason=f"cgroups {info['cgroup_version'] or 'unknown'}: rootless podman "
                                     f"can only limit CPU and memory with cgroups v2")
        have = {c: c in info["cgroup_controllers"] for c in ("cpu", "memory")}
        missing = [c for c, ok in have.items() if not ok]
        if missing:
            return dict(have, enforced=False, reason=f"the {' and '.join(missing)} cgroup controller"
                        f"{'s are' if len(missing) > 1 else ' is'} not delegated to you "
                        f"(systemd's Delegate= for user@.service; ask the admin)")
        return dict(have, enforced=True, reason="cgroups v2 with the cpu and memory controllers delegated")

    @staticmethod
    def _bind_args(plan, flag):
        """
        The binds in order: what the job may use, then the masks over hidden paths, read-only binds over protected
        ones, and the binds inside hidden paths again. Later binds cover earlier ones.
        """
        rebind = {d for d, _ in plan.get("rebind", [])}
        args = []
        for path in plan["read_only"]:
            if path not in rebind:
                args += [flag, f"{path}:{path}:ro"]
        for path in plan["read_write"]:
            if path not in rebind:
                args += [flag, f"{path}:{path}"]
        for image, path in plan.get("images", []):
            # mounted from the host (setuid: the kernel's squashfs; unprivileged: squashfuse), read-only
            args += [flag, f"{image}:{path}:image-src=/"]
        for source, path in plan.get("masks", []):
            args += [flag, f"{source}:{path}:ro"]
        for path in plan.get("protected", []):
            args += [flag, f"{path}:{path}:ro"]
        for path, mode in plan.get("rebind", []):
            args += [flag, f"{path}:{path}" + (":ro" if mode == "ro" else "")]
        return args

    def _pyxis_launch(self, plan, shebang, body, prelude):
        srun, image = plan["runtime"], plan["image"]
        sources = [source for source, _ in plan.get("masks", [])] + [path for _, path in plan.get("images", [])]
        for path in (plan["read_only"] + plan["read_write"] + plan.get("hidden", []) + plan.get("protected", []) +
                     sources + [image]):
            if ":" in path or "," in path:
                raise SandboxError(f"Pyxis can't mount {path!r} (it contains ':' or ',')")
        q = shlex.quote
        rebind = {d for d, _ in plan.get("rebind", [])}
        binds = ([f"{p}:{p}:ro" for p in plan["read_only"] if p not in rebind] +
                 [f"{p}:{p}" for p in plan["read_write"] if p not in rebind])
        covers = ([f"{source}:{path}:ro" for source, path in plan.get("masks", [])] +
                  [f"{p}:{p}:ro" for p in plan.get("protected", [])] +
                  [f"{p}:{p}" + (":ro" if mode == "ro" else "") for p, mode in plan.get("rebind", [])])
        own_account = plan["host_image"] and "/etc" in plan["read_only"]

        delimiter = "HPC_SANDBOX_BODY_" + secrets.token_hex(8)
        while delimiter in body:
            delimiter = "HPC_SANDBOX_BODY_" + secrets.token_hex(8)
        lines = [
            f"# template body, run in a Pyxis sandbox: read-write {', '.join(plan['read_write']) or '(none)'}; "
            f"read-only {', '.join(plan['read_only']) or '(none)'}; the host's network",
            f"hpc_sandbox_body=$(cat <<'{delimiter}'",
            body.rstrip("\n"),
            delimiter,
            ")",
        ]
        # /tmp and /var/tmp are the job's own directories ("session" is the same here: the image's own would be
        # read-only), and a place to mount squashed environments
        parent = '"${TMPDIR:-/tmp}"' if self.scratch in ("job", "session") else q(os.path.expanduser(self.scratch))
        lines += [
            f'hpc_sandbox_tmp=$(mktemp -d {parent}/hpc-sandbox.XXXXXX) && mkdir "$hpc_sandbox_tmp/tmp" '
            '"$hpc_sandbox_tmp/var-tmp" "$hpc_sandbox_tmp/img" || { echo "hpclib: could not make the sandbox '
            'scratch directory" >&2; exit 125; }',
            'hpc_sandbox_cleanup() {',
            '  for hpc_sandbox_m in "$hpc_sandbox_tmp"/img/*; do',
            '    [ -d "$hpc_sandbox_m" ] && { fusermount3 -u -z "$hpc_sandbox_m" 2>/dev/null || '
            'fusermount -u -z "$hpc_sandbox_m" 2>/dev/null; }',
            '  done',
            '  rm -rf --one-file-system "$hpc_sandbox_tmp"',
            '  [ -n "${hpc_sandbox_etc:-}" ] && rm -rf "$hpc_sandbox_etc"',
            '}',
        ]
        if own_account:
            lines += self.account_lines(flag="")
        else:
            lines += ["hpc_sandbox_etc=", "hpc_sandbox_account=()"]
        lines += ["hpc_sandbox_mounts=(" + " ".join(q(b) for b in binds) + ")",
                  'hpc_sandbox_mounts+=("$hpc_sandbox_tmp/tmp:/tmp" "$hpc_sandbox_tmp/var-tmp:/var/tmp" '
                  '"${hpc_sandbox_account[@]}")']
        if plan.get("images"):
            # Pyxis has no way to mount an image file: mount it here, on the host, with squashfuse, and mount the
            # result into the container (a directory of the job's own, not the real mount point, so that other
            # jobs on this node don't see it)
            lines += ['hpc_sandbox_fuse=$(command -v squashfuse_ll || command -v squashfuse) || '
                      '{ echo "hpclib: mounting a squashed environment needs squashfuse (squashfuse_ll) on the compute '
                      'node, and there is none; sync the environment without `squash`" >&2; hpc_sandbox_cleanup; '
                      'exit 125; }']
            for n, (img, target) in enumerate(plan["images"]):
                lines += [f'mkdir "$hpc_sandbox_tmp/img/{n}" && "$hpc_sandbox_fuse" {q(img)} "$hpc_sandbox_tmp/img/{n}" '
                          f'|| {{ echo "hpclib: squashfuse could not mount {img} (is FUSE available here?)" >&2; '
                          f'hpc_sandbox_cleanup; exit 125; }}',
                          f'hpc_sandbox_mounts+=("$hpc_sandbox_tmp/img/{n}:{target}:ro")']
        if covers:
            lines.append("hpc_sandbox_mounts+=(" + " ".join(q(c) for c in covers) + ")")
        lines += [
            'hpc_sandbox_mount_arg=$(IFS=,; printf %s "${hpc_sandbox_mounts[*]}")',
            # the image's own PATH would replace the host's (with the template's modules loaded)
            'hpc_sandbox_env=PATH',
            '[ -n "${LD_LIBRARY_PATH:-}" ] && hpc_sandbox_env=PATH,LD_LIBRARY_PATH',
        ]
        # the first thing inside: a marker, so that a job that never started can be told from one that failed
        inner = [': > /tmp/.hpc-sandbox-started 2>/dev/null || true'] + list(prelude or [])
        delimiter = "HPC_SANDBOX_PRELUDE_" + secrets.token_hex(8)
        lines += [f"hpc_sandbox_prelude=$(cat <<'{delimiter}'", "\n".join(inner) + '\nexec "$@"', delimiter, ")"]
        interp = " ".join(q(w) for w in self.interpreter(shebang))
        step = [q(srun), "--ntasks=1", "--nodes=1", '--cpus-per-task="${SLURM_CPUS_PER_TASK:-1}"',
                "--export=ALL,TMPDIR=/tmp", "--container-image=" + q(image),
                '--container-mounts="$hpc_sandbox_mount_arg"', "--no-container-mount-home",
                '--container-workdir="$PWD"', '--container-env="$hpc_sandbox_env"']
        if plan.get("rootfs_read_only"):
            step.append(PYXIS_READONLY)
        step += [q(f) for f in self.flags]
        lines += [
            " ".join(step) + f' /bin/bash -c "$hpc_sandbox_prelude" hpc-env {interp} -c "$hpc_sandbox_body" hpc-job',
            "hpc_sandbox_status=$?",
            'if [ "$hpc_sandbox_status" != 0 ] && [ ! -e "$hpc_sandbox_tmp/tmp/.hpc-sandbox-started" ]; then',
            '  echo "hpclib: the Pyxis sandbox failed to start (srun exit $hpc_sandbox_status); see the pyxis: and '
            'enroot: messages above" >&2',
            "fi",
            "hpc_sandbox_cleanup",
            'exit "$hpc_sandbox_status"',
        ]
        return lines

    def _podman_launch(self, plan, shebang, body, prelude):
        runtime, image = plan["runtime"], plan["image"]
        for path in plan["read_only"] + plan["read_write"] + plan.get("hidden", []) + plan.get("protected", []) + [
                source for source, _ in plan.get("masks", [])]:
            if ":" in path or "," in path:
                raise SandboxError(f"podman can't bind {path!r} (it contains ':' or ',')")
        info = self.podman_info(runtime)
        args = [runtime, "run", "--rm", "--pull=never", "--userns=keep-id", "--cap-drop=all",
                "--security-opt=no-new-privileges", "--security-opt=label=disable", "--read-only",
                "--pid=private", "--ipc=private"] + {"none": ["--network=none"], "host": ["--network=host"]}.get(
                plan.get("network", "none"), []) + [
                "--env-host", "--env", "TMPDIR=/tmp", "--label", "hpclib.sandbox=job"]
        if info.get("oci_runtime") == "crun":
            args += ["--group-add", "keep-groups"]   # your groups, for group-owned project directories
        controllers = info.get("cgroup_controllers") or []
        if any(os.path.exists(p) for p in PODMAN_INITS):
            # a small init as PID 1, which passes scancel's TERM on: the job's shell as PID 1 would ignore it
            args.append("--init")
        args += self._bind_args(plan, "-v")
        args += self.flags
        quoted = " ".join(shlex.quote(a) for a in args)
        own_account = plan["host_image"] and "/etc" in plan["read_only"]
        image_args = ("--rootfs " + shlex.quote(image)) if image.startswith("/") else shlex.quote(image)

        delimiter = "HPC_SANDBOX_BODY_" + secrets.token_hex(8)
        while delimiter in body:
            delimiter = "HPC_SANDBOX_BODY_" + secrets.token_hex(8)
        lines = [
            f"# template body, run in a podman sandbox: read-write {', '.join(plan['read_write']) or '(none)'}; "
            f"read-only {', '.join(plan['read_only']) or '(none)'}; network {plan.get('network', 'none')}",
            f"hpc_sandbox_body=$(cat <<'{delimiter}'",
            body.rstrip("\n"),
            delimiter,
            ")",
        ]
        # /tmp and /var/tmp: the job's own scratch directories, or small in-memory ones
        if self.scratch == "session":
            lines += ["hpc_sandbox_tmp=",
                      "hpc_sandbox_scratch=(--tmpfs /tmp:rw,mode=1777 --tmpfs /var/tmp:rw,mode=1777)"]
        else:
            parent = '"${TMPDIR:-/tmp}"' if self.scratch == "job" else shlex.quote(os.path.expanduser(self.scratch))
            lines += [
                f'hpc_sandbox_tmp=$(mktemp -d {parent}/hpc-sandbox.XXXXXX) && mkdir "$hpc_sandbox_tmp/tmp" '
                '"$hpc_sandbox_tmp/var-tmp" || { echo "hpclib: could not make the sandbox scratch directory" >&2; '
                'exit 125; }',
                'hpc_sandbox_scratch=(-v "$hpc_sandbox_tmp/tmp:/tmp" -v "$hpc_sandbox_tmp/var-tmp:/var/tmp")',
            ]
        storage = self.podman_storage(runtime)
        if storage:
            # podman's storage on a local disk (see podman_storage); its storage.conf is written by each job, the same
            q = shlex.quote
            conf = (f'[storage]\ndriver = "overlay"\ngraphroot = "{storage}/storage"\nrunroot = "{storage}/run"\n')
            lines += [
                f'(umask 077 && mkdir -p {q(storage + "/storage")} {q(storage + "/run")} && '
                f'printf %s {q(conf)} > {q(storage + "/storage.conf.$$")} && '
                f'mv -f {q(storage + "/storage.conf.$$")} {q(storage + "/storage.conf")}) || '
                f'{{ echo "hpclib: could not set up podman storage in {storage}" >&2; exit 125; }}',
                f'export CONTAINERS_STORAGE_CONF={q(storage + "/storage.conf")}',
            ]
        # CPU and memory limits from the local scheduler (SLURM applies its own); no swap beyond the memory
        lines += [
            # crun's default ping_group_range ("0 0") names gid 0, which doesn't exist in the container when you
            # have no subordinate ids (a single id mapping): name your own group instead
            'hpc_sandbox_limits=(--sysctl "net.ipv4.ping_group_range=$(id -g) $(id -g)")',
        ]
        # only the limits whose cgroup controller you have: podman refuses to start a container asking for others
        if "cpu" in controllers:
            lines.append('if [ -n "${HPC_JOB_CPUS:-}" ]; then hpc_sandbox_limits+=(--cpus "$HPC_JOB_CPUS"); fi')
        if "memory" in controllers:
            lines += [
                'if [ -n "${HPC_JOB_MEMORY:-}" ]; then',
                '  hpc_sandbox_limits+=(--memory "$HPC_JOB_MEMORY" --memory-swap "$HPC_JOB_MEMORY")',
                'fi',
            ]
        lines += [
            # a name the local scheduler can remove the container by, if the job is killed
            'hpc_sandbox_name="hpclib-job-${HPC_JOB_TAG:-${SLURM_JOB_ID:-x}-${SLURM_ARRAY_TASK_ID:-0}-$$}"',
        ]
        if own_account:
            lines += self.account_lines(flag="-v")
        else:
            lines += ["hpc_sandbox_etc=", "hpc_sandbox_account=()"]
        interp = " ".join(shlex.quote(w) for w in self.interpreter(shebang))
        if prelude:
            prelude_text = "\n".join(prelude) + '\nexec "$@"'
            delimiter = "HPC_SANDBOX_PRELUDE_" + secrets.token_hex(8)
            lines += [f"hpc_sandbox_prelude=$(cat <<'{delimiter}'", prelude_text, delimiter, ")"]
            start = f'/bin/bash -c "$hpc_sandbox_prelude" hpc-env {interp}'
        else:
            start = interp
        lines += [
            f'{quoted} --name "$hpc_sandbox_name" --hostname "$(hostname)" "${{hpc_sandbox_limits[@]}}" '
            f'"${{hpc_sandbox_scratch[@]}}" '
            f'"${{hpc_sandbox_account[@]}}" --workdir "$PWD" {image_args} {start} -c "$hpc_sandbox_body" hpc-job',
            "hpc_sandbox_status=$?",
            # mount points podman made inside the scratch belong to ids of its user namespace
            f'[ -n "$hpc_sandbox_tmp" ] && {{ rm -rf "$hpc_sandbox_tmp" 2>/dev/null || '
            f'{shlex.quote(runtime)} unshare rm -rf "$hpc_sandbox_tmp"; }}',
            '[ -n "$hpc_sandbox_etc" ] && rm -rf "$hpc_sandbox_etc"',
            'if [ "$hpc_sandbox_status" = 125 ]; then',
            '  echo "hpclib: the podman sandbox failed to start (exit 125); see the messages above" >&2',
            "fi",
        ]
        # with subordinate ids, podman sets the container up as one of them; NFS treats that as another user
        blocked = sorted({b for b in map(_untraversable_on_network, plan["read_only"] + plan["read_write"] + [image])
                          if b}) if info.get("id_mappings") else []
        if blocked:
            hint = (f"hpclib: podman sets jobs up as one of your subordinate ids, which cannot pass through "
                    f"{', '.join(blocked)} (on a network file system), so what is under it cannot be bound into the "
                    f"job; chmod o+x lets it through without letting anyone list it")
            lines += ['if [ "$hpc_sandbox_status" -ge 125 ] && [ "$hpc_sandbox_status" -le 127 ]; then',
                      f"  echo {shlex.quote(hint)} >&2", "fi"]
        lines += ['exit "$hpc_sandbox_status"']
        return lines

    def describe(self):
        """The configured sandbox, for /cluster and /sandbox."""
        out = {"configured": self.configured, "method": self.method, "runtime": self.runtime,
               "image": self.image, "binds": self.binds, "writable": self.writable, "scratch": self.scratch,
               "flags": self.flags, "gpus": self.gpus, "allow_unsandboxed": self.allow_unsandboxed,
               "network": self.network, "storage": self.storage, "boundary_runtime": self.boundary_runtime, "devices": self.devices}
        try:
            method, detail = self.resolve()
            out["effective"] = method
            out["runtime_path" if method != "none" else "reason"] = detail
            out["network_isolated"] = {"podman": self.network == "none"}.get(method, False) if method != "none" else None
            if method in NAMESPACE_METHODS:
                out.update(network_isolated=self.network == "none", pid_isolated=True, rootfs_read_only=True,
                           boundary_runtime_path=detail if method == "bubblewrap" else self._find([self.boundary_runtime or "bwrap"]),
                           nested_userns_disabled=method == "bubblewrap", seccomp="deny-dangerous-syscalls",
                           limits=self.limits(), squashfuse=shutil.which("squashfuse_ll") or shutil.which("squashfuse"),
                           isolation_notes=["Shared host kernel; Slurm cgroups must enforce compute resources. "
                                            "Login CPU/memory/process limits require site delegation."] +
                                           (["Charliecloud setup requires nested user namespaces; Bubblewrap locks mount permissions."]
                                            if method == "charliecloud" else []))
            if method == "pyxis":
                out.update(
                    base_image=dict(self.base_image_state(), source=self.base_image),
                    rootfs_read_only=self.srun_supports(detail, PYXIS_READONLY),
                    pid_isolated=False,
                    squashfuse=shutil.which("squashfuse_ll") or shutil.which("squashfuse"),
                    isolation_notes=["enroot gives jobs the host's network and process table: sandboxed jobs keep "
                                     "network access (the `network` setting has no effect)"])
            if method == "podman":
                info = self.podman_info(detail)
                out["limits"] = self.limits()
                out["supplementary_groups"] = info.get("oci_runtime") == "crun"
                out["podman_storage"] = self.podman_storage(detail) or info.get("graph_root")
        except SandboxError as e:
            out["effective"] = "unavailable"
            out["error"] = str(e)
            try:
                if self._locate()[0] == "pyxis" and not self.image:
                    out["base_image"] = dict(self.base_image_state(), source=self.base_image)
            except Exception:
                pass
        return out


################################################################################
##
##  Probe
##

def _untraversable_on_network(path):
    """
    The first directory on the way to `path` that is on a network file system and that other users (and so your
    subordinate ids, which rootless podman sets containers up as) can't pass through, or None.
    """
    path = os.path.realpath(path)
    parts = path.strip("/").split("/")
    for i in range(1, len(parts) + 1):
        d = "/" + "/".join(parts[:i])
        try:
            mode = os.stat(d).st_mode
        except OSError:
            return None
        if not mode & 0o001 and _filesystem_type(d) in NETWORK_FILESYSTEMS:
            return d
    return None


def _filesystem_type(path):
    """The type of the file system `path` (or its nearest existing parent) is on, from /proc/self/mountinfo."""
    path = os.path.realpath(path)
    while not os.path.exists(path) and path != "/":
        path = os.path.dirname(path)
    best, kind = "", None
    for line in (_read("/proc/self/mountinfo", 1 << 20) or "").splitlines():
        left, _, right = line.partition(" - ")
        fields = left.split()
        if len(fields) < 5 or not right:
            continue
        point = fields[4].replace("\\040", " ")
        if (path == point or path.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
            best, kind = point, right.split()[0]
    return kind


def _read(path, limit=1 << 16):
    try:
        with open(path) as f:
            return f.read(limit)
    except OSError:
        return None


def _run(args, timeout=30):
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                             env=child_env())
        return res.returncode, res.stdout, res.stderr
    except FileNotFoundError:
        return None, "", f"{args[0]} not found"
    except subprocess.TimeoutExpired:
        return None, "", f"{args[0]} timed out"


def _os_release():
    text = _read("/etc/os-release") or ""
    m = re.search(r'^PRETTY_NAME="?([^"\n]*)"?', text, re.M)
    return m.group(1) if m else None


def _runtime_config(path, version_text):
    """Facts from `RUNTIME buildcfg` and the site's runtime config file."""
    out = {}
    code, text, _ = _run([path, "buildcfg"])
    cfg = dict(line.split("=", 1) for line in text.splitlines() if "=" in line) if code == 0 else {}
    name = "apptainer" if "apptainer" in version_text.lower() else "singularity"
    sysconf = cfg.get("SYSCONFDIR")
    candidates = [os.path.join(sysconf, name, f"{name}.conf")] if sysconf else []
    candidates += [f"/etc/{name}/{name}.conf", f"/usr/local/etc/{name}/{name}.conf"]
    for conf in candidates:
        text = _read(conf)
        if text is None:
            continue
        settings = {}
        binds = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (s.strip() for s in line.split("=", 1))
            if key == "bind path":
                binds.append(value)
            else:
                settings[key] = value
        out.update(config_file=conf, site_bind_paths=binds,
                   setuid_allowed=settings.get("allow setuid", "yes") == "yes",
                   user_namespaces_allowed=settings.get("allow user ns", "yes") == "yes",
                   underlay=settings.get("enable underlay"),
                   session_dir_max_mb=settings.get("sessiondir max size"))
        break
    starter_suid = os.path.join(cfg.get("LIBEXECDIR", ""), name, "bin", "starter-suid")
    out["setuid_installed"] = bool(cfg) and os.path.exists(starter_suid)
    return out


def _module_roots():
    """Top-level directories of the module trees, from a login shell's MODULEPATH."""
    code, text, _ = _run(["bash", "-lc", 'printf "%s" "${MODULEPATH:-}"'])
    home = os.path.realpath(os.path.expanduser("~"))
    roots = []
    for entry in (text or "").split(":"):
        if not entry.startswith("/"):
            continue
        real = os.path.realpath(entry)
        parts = real.strip("/").split("/")
        root = "/" + parts[0] if parts and parts[0] else None
        if not root or root in HOST_IMAGE_BINDS or real.startswith(home + "/") or real == home:
            continue
        if root not in roots:
            roots.append(root)
    return text, roots


def _automounts():
    out = []
    for line in (_read("/proc/self/mountinfo", 1 << 20) or "").splitlines():
        left, _, right = line.partition(" - ")
        fields = left.split()
        if right.split()[:1] == ["autofs"] and len(fields) > 4:
            out.append(fields[4].replace("\\040", " "))
    return out


def self_test(sandbox: Sandbox, base_dir):
    """
    Run a tiny script in the sandbox and check that it sees what it
    should: a writable allowed directory, a read-only /usr, and none of
    the server's other files.
    """
    tmp = tempfile.mkdtemp(prefix="hpc-sandbox-probe.", dir=base_dir)
    allowed = os.path.join(tmp, "allowed")
    hidden = os.path.join(tmp, "hidden")
    os.makedirs(allowed)
    os.makedirs(hidden)
    with open(os.path.join(hidden, "marker"), "w") as f:
        f.write("not for the sandbox\n")
    body = "\n".join([
        f'touch {shlex.quote(allowed)}/written && echo "write_allowed=yes" || echo "write_allowed=no"',
        f'[ -e {shlex.quote(hidden)}/marker ] && echo "outside_hidden=no" || echo "outside_hidden=yes"',
        '(: > /usr/.hpc-sandbox-probe) 2>/dev/null && { rm -f /usr/.hpc-sandbox-probe; echo "usr_read_only=no"; } '
        '|| echo "usr_read_only=yes"',
        '(: > "$HOME/.hpc-sandbox-probe") 2>/dev/null && { rm -f "$HOME/.hpc-sandbox-probe"; echo "home_read_only=no"; } '
        '|| echo "home_read_only=yes"',
        '(: > /tmp/.hpc-sandbox-probe) 2>/dev/null && { rm -f /tmp/.hpc-sandbox-probe; echo "tmp_writable=yes"; } '
        '|| echo "tmp_writable=no"',
        'echo "pid_one=$(cat /proc/1/comm 2>/dev/null)"',
        'echo "programs=$(command -v bash >/dev/null && echo yes || echo no)"',
        'id -un >/dev/null 2>&1 && echo "user_known=yes" || echo "user_known=no"',
        'echo "net_devices=$(sed -n \'3,$p\' /proc/net/dev | cut -d: -f1 | xargs)"',
        'echo "pid_namespace=$(readlink /proc/self/ns/pid)"',
        'echo "net_namespace=$(readlink /proc/self/ns/net)"',
        'echo "effective_caps=$(sed -n \'s/^CapEff:[[:space:]]*//p\' /proc/self/status)"',
        'echo "no_new_privs=$(sed -n \'s/^NoNewPrivs:[[:space:]]*//p\' /proc/self/status)"',
        'echo "seccomp_mode=$(sed -n \'s/^Seccomp:[[:space:]]*//p\' /proc/self/status)"',
    ])
    try:
        lines, plan = sandbox.launch("#!/bin/bash", body, [allowed])
        if lines is None:
            return {"ran": False, "reason": plan["reason"]}
        script = "\n".join(["#!/bin/bash", f"cd {shlex.quote(allowed)}"] + lines) + "\n"
        start = time.time()
        try:
            res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120, env=child_env(),
                                 stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {"ran": False, "reason": "the test container did not finish within 120 s"}
        results = dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
        checks = {k: results.get(k) == "yes"
                  for k in ("write_allowed", "outside_hidden", "usr_read_only", "home_read_only", "tmp_writable",
                            "programs", "user_known")}
        if plan["method"] in ("podman", "bubblewrap", "charliecloud") and plan.get("network") == "none":
            checks["network_isolated"] = results.get("net_devices", "").split() in ([], ["lo"])
        if plan["method"] in NAMESPACE_METHODS:
            checks["capabilities_dropped"] = results.get("effective_caps") == "0000000000000000"
            checks["no_new_privileges"] = results.get("no_new_privs") == "1"
            checks["seccomp_enforced"] = results.get("seccomp_mode") == "2"
            checks["pid_isolated"] = bool(results.get("pid_namespace")) and results["pid_namespace"] != os.readlink("/proc/self/ns/pid")
            if plan.get("network") == "none":
                checks["network_namespace"] = bool(results.get("net_namespace")) and results["net_namespace"] != os.readlink("/proc/self/ns/net")
        return {"ran": True, "exit_code": res.returncode, "seconds": round(time.time() - start, 2),
                "passed": res.returncode == 0 and all(checks.values()), "checks": checks,
                "pid_one": results.get("pid_one"), "method": plan["method"], "stderr": res.stderr[-2000:]}
    except SandboxError as e:
        return {"ran": False, "reason": str(e)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def probe(sandbox: Sandbox, base_dir, run_self_test=True):
    """Everything a client needs to write a `sandbox` config for this node."""
    lsm = (_read("/sys/kernel/security/lsm") or "").strip()
    max_userns = (_read("/proc/sys/user/max_user_namespaces") or "").strip()
    code, _, err = _run(["unshare", "-U", "-m", "true"]) if shutil.which("unshare") else (None, "", "no unshare")
    selinux = (_read("/sys/fs/selinux/enforce") or "").strip()
    info = {
        "node": socket.gethostname(),
        "note": "probed on the node the REST server runs on; nodes in other partitions may differ",
        "kernel": platform.release(),
        "os": _os_release(),
        "python": sys.version.split()[0],
        "security_modules": lsm.split(",") if lsm else [],
        "selinux": {"1": "enforcing", "0": "permissive"}.get(selinux, "absent"),
        "user_namespaces": {"max": int(max_userns) if max_userns.isdigit() else None,
                            "unprivileged_ok": code == 0, "detail": err.strip()[-300:] or None},
        "landlock": "landlock" in lsm.split(","),
    }
    runtimes = []
    for name in RUNTIMES:
        path = sandbox.which(name)
        if not path:
            continue
        code, out, err = _run([path, "--version"])
        entry = {"name": name, "path": path, "version": (out or err).strip()}
        entry["no_mount_flag"] = sandbox.supports(path, "--no-mount")
        entry.update(_runtime_config(path, entry["version"]))
        runtimes.append(entry)
    podman = sandbox._find([sandbox.runtime] if sandbox._is_podman(sandbox.runtime) else [PODMAN])
    if podman:
        code, out, err = _run([podman, "--version"])
        user = getpass.getuser()
        entry = {"name": PODMAN, "path": podman, "version": (out or err).strip()}
        entry.update({k: v for k, v in sandbox.podman_info(podman).items() if k != "ok"})
        entry["subuid"] = any(line.split(":")[0] in (user, str(os.getuid()))
                              for line in (_read("/etc/subuid") or "").splitlines())
        entry["linger"] = os.path.exists(f"/var/lib/systemd/linger/{user}")
        entry["graph_root_filesystem"] = _filesystem_type(entry.get("graph_root") or
                                                          os.path.expanduser("~/.local/share/containers/storage"))
        entry["job_storage"] = sandbox.podman_storage(podman)
        runtimes.append(entry)
    srun = None if sandbox.scheduler == "local" else sandbox._find(
        [sandbox.runtime] if sandbox._is_srun(sandbox.runtime) else [SRUN])
    if srun and sandbox.srun_supports(srun, "--container-image"):
        code, out, err = _run([srun, "--version"])
        enroot = sandbox._find([ENROOT])
        entry = {"name": "pyxis", "path": srun, "version": f"{(out or err).strip()} with Pyxis",
                 "flags": {f: sandbox.srun_supports(srun, f) for f in PYXIS_REQUIRED + (PYXIS_READONLY,)},
                 "enroot": enroot, "squashfuse": shutil.which("squashfuse_ll") or shutil.which("squashfuse"),
                 "base_image": dict(sandbox.base_image_state(), source=sandbox.base_image)}
        if enroot:
            entry["enroot_version"] = (_run([enroot, "version"])[1] or "").strip()
        runtimes.append(entry)
    for method, binary in (("bubblewrap", "bwrap"), ("charliecloud", "ch-run")):
        path = sandbox._find([sandbox.runtime] if sandbox.method == method and sandbox.runtime else [binary])
        if path:
            code, out, err = _run([path, "--version"])
            runtimes.append({"name": method, "path": path, "version": (out or err).strip(),
                             "boundary_runtime": sandbox._find([sandbox.boundary_runtime or "bwrap"]),
                             "pid_isolated": True, "network_isolation_available": True})
    info["container_runtimes"] = runtimes
    modulepath, roots = _module_roots()
    info["modules"] = {"modulepath": modulepath, "roots": roots}
    info["automounts"] = _automounts()
    info["sandbox"] = sandbox.describe()

    recommended = {"method": "auto"}
    notes = []
    podman_entry = next((r for r in runtimes if r["name"] == PODMAN), None)
    if podman_entry and len(runtimes) == 1:
        recommended = {"method": "podman"}
        if not podman_entry["subuid"]:
            notes.append("you have no subordinate ids in /etc/subuid; rootless podman needs them (ask the admin)")
        if not podman_entry["linger"]:
            notes.append("lingering is off for you (loginctl enable-linger), so your podman jobs may be stopped "
                         "when you log out")
        if podman_entry["graph_root_filesystem"] in NETWORK_FILESYSTEMS:
            notes.append(f"podman's own storage is on {podman_entry['graph_root_filesystem']}, where rootless "
                         f"containers can't be created; jobs keep theirs in {podman_entry['job_storage'] or '(none)'}"
                         " (the sandbox's \"storage\")")
        if podman_entry.get("id_mappings"):
            # set up as a subordinate id: on NFS, a directory others can't pass through stops the job's binds
            for d in sandbox.binds + sandbox.writable:
                blocked = _untraversable_on_network(d)
                if blocked:
                    notes.append(f"podman sets jobs up as one of your subordinate ids, which {blocked} (on a "
                                 f"network file system) doesn't let through, so {d} can't be bound into jobs; "
                                 f"`chmod o+x {blocked}` lets others pass through it without listing it")
        limits = Sandbox(recommended, data_dir=sandbox.data_dir, which=sandbox.which,
                         scheduler=sandbox.scheduler).limits()
        if not limits["enforced"]:
            notes.append(f"jobs' CPU and memory limits can't be enforced: {limits['reason']}")
    pyxis_entry = next((r for r in runtimes if r["name"] == "pyxis"), None)
    if pyxis_entry and not any(r["name"] in RUNTIMES for r in runtimes):
        notes.append("jobs would run with Pyxis (srun --container-image): enroot gives them the host's network and "
                     "process table, so they are not isolated from the network")
        if not pyxis_entry["enroot"]:
            notes.append("enroot isn't on PATH here, so the Pyxis base image can't be imported by hpclib; import it "
                         f"by hand: enroot import -o {sandbox.pyxis_base_image} {sandbox.base_image}")
        elif pyxis_entry["base_image"]["state"] != "ready":
            notes.append("the Pyxis base image isn't imported yet: rest_server.py --import-sandbox-image (setup_agents "
                         "and the server start it)")
        if not pyxis_entry["squashfuse"]:
            notes.append("squashfuse isn't on PATH here: squashed Python environments can't be mounted in Pyxis jobs")
    if runtimes:
        if roots:
            recommended["binds"] = roots
            notes.append(f"`binds` makes the module trees ({', '.join(roots)}) readable in jobs")
        site = [b for r in runtimes for b in r.get("site_bind_paths", [])]
        if site and not all(r.get("no_mount_flag", True) for r in runtimes):
            notes.append(f"this runtime can't turn off the site's bind paths ({', '.join(site)}); jobs will see them")
    else:
        recommended = {"method": "none"}
        notes.append("no supported runtime on this node; install Bubblewrap from cluster Settings, or "
                     "`module avail` may list one to set as `runtime`")
    if info["automounts"]:
        notes.append("some paths are automounted (autofs); a bind of one fails if it isn't mounted yet")
    info["recommended_config"] = {"sandbox": recommended}
    info["notes"] = notes
    if run_self_test and runtimes:
        current = sandbox if sandbox.method != "none" else Sandbox(dict(recommended), data_dir=sandbox.data_dir,
                                                                    scheduler=sandbox.scheduler)
        test_dir = base_dir
        try:
            if current.resolve()[0] == "podman" and _untraversable_on_network(base_dir):
                # the server's own directory is private on NFS, where podman's subordinate ids can't reach it;
                # test beside the host image instead (jobs' own directories get the note above if they can't)
                test_dir = os.path.join(os.path.dirname(current.host_image_for("podman")), "probe")
                os.makedirs(test_dir, mode=0o700, exist_ok=True)
        except SandboxError:
            pass
        info["self_test"] = self_test(current, test_dir)
        info["self_test"]["config"] = "current" if current is sandbox else "recommended"
    return info


class Prober:
    """Caches probe results; a probe starts a container, so it takes a few seconds."""

    CACHE_SECONDS = 300

    def __init__(self, sandbox: Sandbox, base_dir):
        self.sandbox = sandbox
        self.base_dir = base_dir
        self._lock = threading.Lock()
        self._cache = None

    def get(self, refresh=False):
        with self._lock:
            if refresh or self._cache is None or time.time() - self._cache[0] > self.CACHE_SECONDS:
                os.makedirs(self.base_dir, mode=0o700, exist_ok=True)
                self._cache = (time.time(), probe(self.sandbox, self.base_dir))
            return dict(self._cache[1], probed_at=time.strftime("%Y-%m-%dT%H:%M:%S",
                                                                time.localtime(self._cache[0])))
