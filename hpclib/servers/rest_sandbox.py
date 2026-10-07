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

  {"method": "auto",          # auto | singularity | podman | none
   "runtime": null,           # "apptainer", "singularity", "podman", or a path; default: whichever is found
   "image": null,             # default: the host image, built in the server's data directory
                              #   (podman: also an image name, e.g. "docker.io/library/ubuntu:24.04")
   "network": "none",         # podman: "none" (jobs have no network) or "default"
   "binds": ["/sw"],          # extra read-only binds (software trees, reference data, ...)
   "writable": [],            # extra read-write binds, besides the token's directories
   "scratch": "job",          # /tmp in the container: "job" (a per-job directory under
                              #   $TMPDIR, deleted afterwards), "session" (the runtime's small
                              #   in-memory default), or a directory
   "flags": [],               # extra runtime flags, e.g. ["--nv"] for NVIDIA GPUs
   "allow_unsandboxed": false}  # with "auto": run jobs unsandboxed if no runtime is found

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

__all__ = ["SandboxError", "Sandbox", "build_host_image", "probe", "child_env"]

# Set by the Python launcher setup_agents writes when it loads a Python module:
# the environment from before that, base64 of `env -0`. Commands and jobs the
# server starts get it, so they don't inherit the server's Python modules.
BASE_ENV_VAR = "HPCLIB_BASE_ENV"
SECRET_ENV_PREFIXES = ("HPC_REST_TOKEN",)


def _decode_base_env(text):
    import base64
    try:
        raw = base64.b64decode(text.encode(), validate=False)
    except (ValueError, TypeError):
        return None
    env = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep and key:
            env[key.decode("utf-8", "surrogateescape")] = value.decode("utf-8", "surrogateescape")
    return env or None


def child_env(env=None):
    """
    The environment for processes the server starts: `env`, or the one saved
    from before the Python launcher loaded its modules (else this process's),
    minus anything secret.
    """
    if env is None:
        saved = os.environ.get(BASE_ENV_VAR)
        env = (_decode_base_env(saved) if saved else None) or os.environ
    env = dict(env)
    for key in list(env):
        if key.startswith(SECRET_ENV_PREFIXES) or key == BASE_ENV_VAR:
            del env[key]
    return env

METHODS = ("auto", "singularity", "podman", "none")
RUNTIMES = ("apptainer", "singularity")
PODMAN = "podman"
# the methods that sandbox a job (an effective method of either means jobs are sandboxed)
SANDBOXED = ("singularity", "podman")
NETWORKS = ("none", "default")
CONFIG_KEYS = {"method", "runtime", "image", "binds", "writable", "scratch", "flags", "allow_unsandboxed", "network",
               "storage"}
# file systems where rootless podman's storage can't live: they refuse the chown to your subordinate ids that its
# overlay layers need ("chown ...: operation not permitted")
NETWORK_FILESYSTEMS = ("nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "lustre", "gpfs", "beegfs",
                       "fuse.glusterfs", "ceph", "fuse.ceph", "panfs", "afs")
# podman's --init program (catatonit), where distributions put it
PODMAN_INITS = ("/usr/libexec/podman/catatonit", "/usr/bin/catatonit", "/usr/libexec/catatonit/catatonit")
# a podman image reference: [registry/]name[:tag][@digest]
PODMAN_IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._/:-]{0,254}(@sha256:[0-9a-f]{64})?")
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

    def __init__(self, config=None, data_dir=None, which=shutil.which):
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
                raise ValueError("sandbox `image` must be an absolute path (or, with method podman, an image name)")
            self.image = image
        else:
            self.image = _absolute(image, "image") if image else None
        # podman's storage: "auto" (podman's own, unless that is on a network file system: then a local
        # directory, /var/tmp/USER/hpclib-podman), "default" (podman's own), or a directory
        self.storage = config.get("storage", "auto")
        if not isinstance(self.storage, str) or not (self.storage in ("auto", "default") or
                                                     os.path.isabs(os.path.expanduser(self.storage))):
            raise ValueError('sandbox `storage` must be "auto", "default", or an absolute directory')
        self.network = config.get("network", "none")
        if self.network not in NETWORKS:
            raise ValueError(f"sandbox `network` must be one of {', '.join(NETWORKS)}")
        self.binds = [_absolute(b, "binds") for b in _string_list(config, "binds")]
        self.writable = [_absolute(w, "writable") for w in _string_list(config, "writable")]
        self.flags = _string_list(config, "flags")
        self.scratch = config.get("scratch", "job")
        if not isinstance(self.scratch, str) or not (self.scratch in ("job", "session") or os.path.isabs(
                os.path.expanduser(self.scratch))):
            raise ValueError('sandbox `scratch` must be "job", "session", or an absolute directory')
        self.allow_unsandboxed = config.get("allow_unsandboxed", False)
        if not isinstance(self.allow_unsandboxed, bool):
            raise ValueError("sandbox `allow_unsandboxed` must be true or false")
        self.data_dir = data_dir
        self.which = which
        self._help_cache = {}
        self._podman_cache = {}
        self._lock = threading.Lock()

    # -- what will actually run -------------------------------------------

    def _find(self, names):
        for name in names:
            path = name if os.path.isabs(os.path.expanduser(name)) else self.which(name)
            if path and os.access(os.path.expanduser(path), os.X_OK):
                return os.path.expanduser(path)
        return None

    def find_runtime(self):
        """The Singularity/Apptainer runtime's absolute path, or None."""
        return self._find([self.runtime] if self.runtime and not self._is_podman(self.runtime) else RUNTIMES)

    @staticmethod
    def _is_podman(name):
        return PODMAN in os.path.basename(name or "")

    def _locate(self):
        """(kind, path) of the runtime the method calls for; path is None if it isn't found."""
        if self.method == "podman" or (self.method == "auto" and self._is_podman(self.runtime)):
            return "podman", self._find([self.runtime] if self.runtime else [PODMAN])
        if self.method == "singularity" or self.runtime:
            return "singularity", self.find_runtime()
        found = self.find_runtime()
        if found:
            return "singularity", found
        podman = self._find([PODMAN])
        return ("podman", podman) if podman else ("singularity", None)

    @property
    def host_image(self):
        return os.path.join(self.data_dir or tempfile.gettempdir(), "sandbox", "host")

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
            wanted = self.runtime or (PODMAN if kind == "podman" or self.method == "podman"
                                      else " or ".join(RUNTIMES) + (", or podman" if self.method == "auto" else ""))
            reason = (f"{wanted} was not found on the server's PATH; set sandbox `runtime` to its full path "
                      f"(GET /sandbox shows what the node has)")
            if self.method == "auto" and self.allow_unsandboxed:
                return "none", reason
            raise SandboxError(f"jobs must run sandboxed but {reason}")
        if self.image and self.image.startswith("/") and not os.path.exists(self.image):
            raise SandboxError(f"sandbox image {self.image} does not exist")
        if self.image and not self.image.startswith("/") and kind != "podman":
            raise SandboxError(f"sandbox image {self.image} is a podman image name, and the runtime is {runtime}")
        return kind, runtime

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

    def plan(self, writable, extra_ro=()):
        """
        What a job would see: the method and the read-only and read-write
        binds. `extra_ro` adds read-only binds for this job (e.g. a Python
        environment's interpreter).
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
        plan = {"method": method, "runtime": detail, "image": self.image or self.host_image,
                "host_image": self.image is None, "read_only": ro, "read_write": rw}
        if method == "podman":
            plan["network"] = self.network
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

    def launch(self, shebang, body, writable, prelude=None, extra_ro=(), network=False):
        """
        Lines for the end of the job script: start `body` in the
        sandbox and exit with its status. Returns (lines, plan).
        `prelude` (bash lines) runs inside the sandbox first, in the
        same shell that then execs the body: e.g. activating a Python
        environment, whose activation scripts must not run outside.
        `network`: give it the network whatever the config says (podman;
        Apptainer always shares the host's), for environment syncs.
        """
        plan = self.plan(writable, extra_ro)
        if plan["method"] == "none":
            return None, plan
        runtime, image = plan["runtime"], plan["image"]
        if plan["host_image"]:
            build_host_image(image, plan["read_only"] + plan["read_write"])
        if plan["method"] == "podman":
            if network:
                plan["network"] = "default"
            return self._podman_launch(plan, shebang, body, prelude), plan
        # --no-home: no writable stand-in for $HOME (allowed directories under it are still bound)
        args = [runtime, "-q", "exec", "--contain", "--no-home", "--pid", "--ipc"]
        if self.supports(runtime, "--no-mount"):
            args += ["--no-mount", "bind-paths"]   # site-wide binds would add paths behind our back
        for path in plan["read_only"]:
            args += ["--bind", f"{path}:{path}:ro"]
        for path in plan["read_write"]:
            args += ["--bind", f"{path}:{path}"]
        args += self.flags
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

    def _podman_launch(self, plan, shebang, body, prelude):
        runtime, image = plan["runtime"], plan["image"]
        for path in plan["read_only"] + plan["read_write"]:
            if ":" in path or "," in path:
                raise SandboxError(f"podman can't bind {path!r} (it contains ':' or ',')")
        info = self.podman_info(runtime)
        args = [runtime, "run", "--rm", "--pull=never", "--userns=keep-id", "--cap-drop=all",
                "--security-opt=no-new-privileges", "--security-opt=label=disable", "--read-only",
                "--pid=private", "--ipc=private"] + (["--network=none"] if plan.get("network", "none") == "none" else []) + [
                "--env-host", "--env", "TMPDIR=/tmp", "--label", "hpclib.sandbox=job"]
        if info.get("oci_runtime") == "crun":
            args += ["--group-add", "keep-groups"]   # your groups, for group-owned project directories
        controllers = info.get("cgroup_controllers") or []
        if any(os.path.exists(p) for p in PODMAN_INITS):
            # a small init as PID 1, which passes scancel's TERM on: the job's shell as PID 1 would ignore it
            args.append("--init")
        for path in plan["read_only"]:
            args += ["-v", f"{path}:{path}:ro"]
        for path in plan["read_write"]:
            args += ["-v", f"{path}:{path}"]
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
            'exit "$hpc_sandbox_status"',
        ]
        return lines

    def describe(self):
        """The configured sandbox, for /cluster and /sandbox."""
        out = {"configured": self.configured, "method": self.method, "runtime": self.runtime,
               "image": self.image, "binds": self.binds, "writable": self.writable, "scratch": self.scratch,
               "flags": self.flags, "allow_unsandboxed": self.allow_unsandboxed, "network": self.network,
               "storage": self.storage}
        try:
            method, detail = self.resolve()
            out["effective"] = method
            out["runtime_path" if method != "none" else "reason"] = detail
            if method == "podman":
                info = self.podman_info(detail)
                out["limits"] = self.limits()
                out["supplementary_groups"] = info.get("oci_runtime") == "crun"
                out["podman_storage"] = self.podman_storage(detail) or info.get("graph_root")
        except SandboxError as e:
            out["effective"] = "unavailable"
            out["error"] = str(e)
        return out


################################################################################
##
##  Probe
##

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
        'echo "net_devices=$(ls /sys/class/net 2>/dev/null | tr "\\n" " ")"',
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
        if plan["method"] == "podman" and plan.get("network") == "none":
            checks["network_isolated"] = results.get("net_devices", "").split() in ([], ["lo"])
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
        limits = Sandbox(recommended, data_dir=sandbox.data_dir, which=sandbox.which).limits()
        if not limits["enforced"]:
            notes.append(f"jobs' CPU and memory limits can't be enforced: {limits['reason']}")
    if runtimes:
        if roots:
            recommended["binds"] = roots
            notes.append(f"`binds` makes the module trees ({', '.join(roots)}) readable in jobs")
        site = [b for r in runtimes for b in r.get("site_bind_paths", [])]
        if site and not all(r.get("no_mount_flag", True) for r in runtimes):
            notes.append(f"this runtime can't turn off the site's bind paths ({', '.join(site)}); jobs will see them")
    else:
        recommended = {"method": "none"}
        notes.append("no Singularity, Apptainer or podman on this node, so jobs can't be sandboxed here; "
                     "`module avail` may list one to set as `runtime`")
    if info["automounts"]:
        notes.append("some paths are automounted (autofs); a bind of one fails if it isn't mounted yet")
    info["recommended_config"] = {"sandbox": recommended}
    info["notes"] = notes
    if run_self_test and runtimes:
        current = sandbox if sandbox.method != "none" else Sandbox(dict(recommended), data_dir=sandbox.data_dir)
        info["self_test"] = self_test(current, base_dir)
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
