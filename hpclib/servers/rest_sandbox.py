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
    other projects, not ~/.local/tunnels. The container gets an empty
    home and private /tmp, /dev/shm and process table.

By default the container is a "host image": an almost empty directory
whose /usr, /etc and /opt (and any `binds`, such as a software tree like
/sw) are the host's own, bound read-only. Programs and modules from the
host therefore work unchanged; nothing has to be built to match the
host. Give `image` (a .sif file or a sandbox directory) to run in an
image of your own instead; then only `binds` are added.

Config, the `sandbox` key of the server's config.json:

  {"method": "auto",          # auto | singularity | none
   "runtime": null,           # "apptainer", "singularity", or a path; default: whichever is found
   "image": null,             # default: the host image, built in the server's data directory
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

__all__ = ["SandboxError", "Sandbox", "build_host_image", "probe"]

METHODS = ("auto", "singularity", "none")
RUNTIMES = ("apptainer", "singularity")
CONFIG_KEYS = {"method", "runtime", "image", "binds", "writable", "scratch", "flags", "allow_unsandboxed"}
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
        self.image = _absolute(image, "image") if image else None
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
        self._lock = threading.Lock()

    # -- what will actually run -------------------------------------------

    def find_runtime(self):
        """The runtime's absolute path, or None."""
        names = [self.runtime] if self.runtime else RUNTIMES
        for name in names:
            path = name if os.path.isabs(os.path.expanduser(name)) else self.which(name)
            if path and os.access(os.path.expanduser(path), os.X_OK):
                return os.path.expanduser(path)
        return None

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
        runtime = self.find_runtime()
        if runtime is None:
            wanted = self.runtime or " or ".join(RUNTIMES)
            reason = (f"{wanted} was not found on the server's PATH; set sandbox `runtime` to its full path "
                      f"(GET /sandbox shows what the node has)")
            if self.method == "auto" and self.allow_unsandboxed:
                return "none", reason
            raise SandboxError(f"jobs must run sandboxed but {reason}")
        if self.image and not os.path.exists(self.image):
            raise SandboxError(f"sandbox image {self.image} does not exist")
        return "singularity", runtime

    def supports(self, runtime, flag):
        """Whether `runtime exec --help` lists `flag` (cached)."""
        with self._lock:
            if runtime not in self._help_cache:
                try:
                    out = subprocess.run([runtime, "exec", "--help"], capture_output=True, text=True,
                                         timeout=30, stdin=subprocess.DEVNULL).stdout
                except (OSError, subprocess.TimeoutExpired):
                    out = ""
                self._help_cache[runtime] = out
            return flag in self._help_cache[runtime]

    def plan(self, writable):
        """What a job would see: the method and the read-only and read-write binds."""
        method, detail = self.resolve()
        if method == "none":
            return {"method": "none", "reason": detail}
        ro, rw, missing = [], [], []
        for d in ([] if self.image else list(HOST_IMAGE_BINDS)) + self.binds:
            (ro if os.path.exists(d) else missing).append(d)
        for d in list(writable) + self.writable:
            d = os.path.normpath(d)
            if d in rw:
                continue
            (rw if os.path.exists(d) else missing).append(d)
        plan = {"method": "singularity", "runtime": detail, "image": self.image or self.host_image,
                "host_image": self.image is None, "read_only": ro, "read_write": rw}
        if missing:
            plan["missing"] = missing   # not on this node; left out
        return plan

    # -- the job script -----------------------------------------------------

    @staticmethod
    def interpreter(shebang):
        """The template body's interpreter, as an argument list for `-c`."""
        words = shlex.split(shebang[2:]) if shebang.startswith("#!") else ["/bin/bash"]
        program = words[1] if os.path.basename(words[0]) == "env" and len(words) > 1 else words[0]
        if not INTERPRETERS.match(os.path.basename(program)):
            raise SandboxError(f"a sandboxed template body must be a shell or Python script, not {shebang!r}")
        return words

    def launch(self, shebang, body, writable):
        """
        Lines for the end of the job script: start `body` in the
        sandbox and exit with its status. Returns (lines, plan).
        """
        plan = self.plan(writable)
        if plan["method"] == "none":
            return None, plan
        runtime, image = plan["runtime"], plan["image"]
        if plan["host_image"]:
            build_host_image(image, plan["read_only"] + plan["read_write"])
        args = [runtime, "-q", "exec", "--contain", "--pid", "--ipc"]
        if self.supports(runtime, "--no-mount"):
            args += ["--no-mount", "bind-paths"]   # site-wide binds would add paths behind our back
        for path in plan["read_only"]:
            args += ["--bind", f"{path}:{path}:ro"]
        for path in plan["read_write"]:
            args += ["--bind", f"{path}:{path}"]
        args += self.flags
        quoted = " ".join(shlex.quote(a) for a in args)

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
        interp = " ".join(shlex.quote(w) for w in self.interpreter(shebang))
        lines += [
            f'{quoted} "${{hpc_sandbox_workdir[@]}}" --pwd "$PWD" {shlex.quote(image)} '
            f'{interp} -c "$hpc_sandbox_body" hpc-job',
            "hpc_sandbox_status=$?",
            '[ -n "$hpc_sandbox_tmp" ] && rm -rf "$hpc_sandbox_tmp"',
            'if [ "$hpc_sandbox_status" = 255 ]; then',
            '  echo "hpclib: the job sandbox may have failed to start (exit 255); see the messages above" >&2',
            "fi",
            'exit "$hpc_sandbox_status"',
        ]
        return lines, plan

    def describe(self):
        """The configured sandbox, for /cluster and /sandbox."""
        out = {"configured": self.configured, "method": self.method, "runtime": self.runtime,
               "image": self.image, "binds": self.binds, "writable": self.writable, "scratch": self.scratch,
               "flags": self.flags, "allow_unsandboxed": self.allow_unsandboxed}
        try:
            method, detail = self.resolve()
            out["effective"] = method
            out["runtime_path" if method != "none" else "reason"] = detail
        except SandboxError as e:
            out["effective"] = "unavailable"
            out["error"] = str(e)
        return out


################################################################################
##
##  Probe
##

def _read(path, limit=1 << 16):
    try:
        with open(path) as f:
            return f.read(limit)
    except OSError:
        return None


def _run(args, timeout=30):
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
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
        'echo "pid_one=$(cat /proc/1/comm 2>/dev/null)"',
        'echo "programs=$(command -v bash >/dev/null && echo yes || echo no)"',
    ])
    try:
        lines, plan = sandbox.launch("#!/bin/bash", body, [allowed])
        if lines is None:
            return {"ran": False, "reason": plan["reason"]}
        script = "\n".join(["#!/bin/bash", f"cd {shlex.quote(allowed)}"] + lines) + "\n"
        start = time.time()
        try:
            res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120,
                                 stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {"ran": False, "reason": "the test container did not finish within 120 s"}
        results = dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
        checks = {k: results.get(k) == "yes"
                  for k in ("write_allowed", "outside_hidden", "usr_read_only", "programs")}
        return {"ran": True, "exit_code": res.returncode, "seconds": round(time.time() - start, 2),
                "passed": res.returncode == 0 and all(checks.values()), "checks": checks,
                "pid_one": results.get("pid_one"), "stderr": res.stderr[-2000:]}
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
    info["container_runtimes"] = runtimes
    modulepath, roots = _module_roots()
    info["modules"] = {"modulepath": modulepath, "roots": roots}
    info["automounts"] = _automounts()
    info["sandbox"] = sandbox.describe()

    recommended = {"method": "auto"}
    notes = []
    if runtimes:
        if roots:
            recommended["binds"] = roots
            notes.append(f"`binds` makes the module trees ({', '.join(roots)}) readable in jobs")
        site = [b for r in runtimes for b in r.get("site_bind_paths", [])]
        if site and not all(r["no_mount_flag"] for r in runtimes):
            notes.append(f"this runtime can't turn off the site's bind paths ({', '.join(site)}); jobs will see them")
    else:
        recommended = {"method": "none"}
        notes.append("no Singularity or Apptainer on this node, so jobs can't be sandboxed here; "
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
