"""Native Bubblewrap launch policy, also enforcing Charliecloud's bind permissions.

Charliecloud's -b has no read-only mode. Its trusted ch-run therefore starts
inside a Bubblewrap filesystem whose source mounts already have their final
permissions. A nested user namespace cannot turn those locked read-only mounts
writable. No user script runs before the boundary has been constructed.
"""
import ctypes
import ctypes.util
import errno
import hashlib
import os
import shlex
import tempfile


class BoundaryError(Exception):
    pass


def seccomp_filter(directory, charliecloud=False):
    """Compile a native-architecture deny policy with libseccomp; fail closed.

    This complements namespaces, rather than attempting a syscall allowlist
    for every scientific library. libseccomp rejects foreign architectures.
    Charliecloud still needs mount/user-namespace syscalls during its setup.
    """
    library = ctypes.util.find_library("seccomp")
    if not library:
        raise BoundaryError("Bubblewrap needs the libseccomp runtime library (usually libseccomp.so.2); "
                            "ask the site to install it; jobs will not run without a syscall policy")
    lib = ctypes.CDLL(library, use_errno=True)
    class Comparison(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint),
                    ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]
    lib.seccomp_init.argtypes, lib.seccomp_init.restype = [ctypes.c_uint32], ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint, ctypes.POINTER(Comparison)]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    context = lib.seccomp_init(0x7fff0000)   # SCMP_ACT_ALLOW
    if not context:
        raise BoundaryError("libseccomp could not allocate a syscall policy")
    denied = ("ptrace", "process_vm_readv", "process_vm_writev", "bpf", "perf_event_open",
              "keyctl", "add_key", "request_key", "open_by_handle_at", "kexec_load", "kexec_file_load",
              "init_module", "finit_module", "delete_module", "reboot", "swapon", "swapoff",
              "userfaultfd", "io_uring_setup")
    if not charliecloud:
        denied += ("unshare", "setns", "mount", "umount2", "pivot_root", "chroot", "fsopen", "fsconfig",
                   "fsmount", "move_mount", "open_tree", "mount_setattr")
    try:
        for name in denied:
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and lib.seccomp_rule_add_array(context, 0x00050000 | errno.EPERM, number, 0, None):
                raise BoundaryError(f"libseccomp could not restrict {name}")
        ioctl = lib.seccomp_syscall_resolve_name(b"ioctl")
        for request in (0x5412, 0x541c):  # TIOCSTI, TIOCLINUX: terminal injection/control
            # ioctl's request is an unsigned int in the kernel. Mask high
            # argument bits so a 64-bit caller cannot evade this comparison.
            condition = Comparison(1, 7, 0xffffffff, request)   # SCMP_CMP_MASKED_EQ
            if lib.seccomp_rule_add_array(context, 0x00050000 | errno.EPERM, ioctl, 1, ctypes.byref(condition)):
                raise BoundaryError("libseccomp could not restrict terminal injection")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        with tempfile.TemporaryFile() as output:
            if lib.seccomp_export_bpf(context, output.fileno()):
                raise BoundaryError("libseccomp could not export the syscall policy")
            output.seek(0)
            policy = output.read()
        path = os.path.join(directory, hashlib.sha256(policy).hexdigest() + ".bpf")
        # Publish only a complete policy. Concurrent submissions must never
        # observe an empty/partially written filter or replace an open policy.
        descriptor, temporary = tempfile.mkstemp(prefix="policy-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(policy)
            try:
                os.link(temporary, path)
            except FileExistsError:
                with open(path, "rb") as existing:
                    if existing.read() != policy:
                        raise BoundaryError("the saved syscall policy was modified; remove it and probe again")
        finally:
            os.unlink(temporary)
        return path
    finally:
        lib.seccomp_release(context)


def fuse_lines(images):
    """Mount images only at job-private paths, never on shared environment directories."""
    if not images:
        return []
    lines = ['hpc_sandbox_fuse=$(command -v squashfuse_ll || command -v squashfuse) || '
             '{ echo "hpclib: this image needs squashfuse and /dev/fuse on the execution node" >&2; exit 125; }']
    for index, (source, _) in enumerate(images):
        lines += [f'mkdir "$hpc_sandbox_tmp/img/{index}"',
                  f'"$hpc_sandbox_fuse" -o ro {shlex.quote(source)} "$hpc_sandbox_tmp/img/{index}" || '
                  '{ echo "hpclib: SquashFS mount failed; check FUSE permissions or sync without squash" >&2; exit 125; }']
    return lines


def launch(sandbox, plan, shebang, body, prelude):
    q = shlex.quote
    charlie = plan["method"] == "charliecloud"
    bwrap = plan["boundary_runtime"]
    # An agent must not replace a binary that the host will execute on its
    # next request, including a writable symlink to an otherwise protected ELF.
    for runtime in (plan["runtime"], bwrap):
        for path in (os.path.abspath(runtime), os.path.realpath(runtime)):
            writable = any(path == parent or path.startswith(parent.rstrip("/") + "/") for parent in plan["read_write"])
            protected = any(path == parent or path.startswith(parent.rstrip("/") + "/") for parent in plan.get("protected", []))
            if writable and not protected:
                raise BoundaryError("Sandbox runtime is inside an agent-writable directory; install it from cluster "
                                    "Settings or move it outside the token's writable directories")
    if sandbox.flags:
        # Free-form flags can add writable roots, sockets, devices or host namespaces.
        raise BoundaryError("Charliecloud/Bubblewrap does not accept extra runtime flags; use binds, writable, "
                            "network and devices to express the policy")
    if charlie:
        for path in plan["read_only"] + plan["read_write"] + [plan["image"]] + plan.get("hidden", []):
            if ":" in path:
                raise BoundaryError(f"Charliecloud cannot bind a path containing ':': {path!r}")
    private = sandbox.data_dir or os.path.join(tempfile.gettempdir(), f"hpclib-{os.getuid()}")
    policy = seccomp_filter(os.path.join(private, "sandbox", "seccomp"), charlie)
    plan["seccomp"] = "deny-dangerous-syscalls"
    delimiter = "HPC_BWRAP_BODY_" + os.urandom(12).hex()
    while delimiter in body:
        delimiter += "_"
    parent = '"${TMPDIR:-/tmp}"' if sandbox.scratch in ("job", "session") else q(os.path.expanduser(sandbox.scratch))
    lines = [f"hpc_sandbox_body=$(cat <<'{delimiter}'", body.rstrip("\n"), delimiter, ")",
             f'hpc_sandbox_tmp=$(mktemp -d {parent}/hpc-sandbox.XXXXXX) || exit 125',
             'hpc_sandbox_etc=',
             'hpc_sandbox_cleanup() {',
             '  for hpc_sandbox_m in "$hpc_sandbox_tmp"/img/*; do',
             '    [ ! -d "$hpc_sandbox_m" ] || { fusermount3 -u -z "$hpc_sandbox_m" 2>/dev/null || '
             'fusermount -u -z "$hpc_sandbox_m" 2>/dev/null || true; }',
             '  done',
             '  rm -rf --one-file-system -- "$hpc_sandbox_tmp"',
             '  [ -z "$hpc_sandbox_etc" ] || rm -rf -- "$hpc_sandbox_etc"',
             '}',
             "trap hpc_sandbox_cleanup EXIT", "trap 'exit 143' TERM", "trap 'exit 130' INT",
             'mkdir "$hpc_sandbox_tmp/tmp" "$hpc_sandbox_tmp/var-tmp" "$hpc_sandbox_tmp/shm" "$hpc_sandbox_tmp/img" || exit 125']
    images = list(plan.get("images", []))
    root_image = plan["image"]
    if not os.path.isdir(root_image):
        images.insert(0, (root_image, None))
    lines += fuse_lines(images)
    root_arg = q(root_image) if os.path.isdir(root_image) else '"$hpc_sandbox_tmp/img/0"'
    args = [bwrap, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup",
            "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--hostname", "hpclib-job"]
    if plan["network"] == "none":
        args += ["--unshare-net"]
    if not charlie:
        args += ["--disable-userns"]
    lines.append("hpc_bwrap=(" + " ".join(q(a) for a in args) + ")")
    if charlie or plan["host_image"]:
        # An empty tmpfs root: create only the host's standard /usr aliases.
        for name in ("bin", "sbin", "lib", "lib64"):
            host = "/" + name
            if os.path.islink(host):
                lines.append(f"hpc_bwrap+=(--symlink {q(os.readlink(host))} {q(host)})")
            elif os.path.isdir(host):
                lines.append(f"hpc_bwrap+=(--ro-bind {q(host)} {q(host)})")
        if charlie:
            for path in ("/usr", "/etc", "/opt"):
                if os.path.isdir(path):
                    lines.append(f"hpc_bwrap+=(--ro-bind {q(path)} {q(path)})")
    else:
        # Custom images need their bind destinations present. The managed
        # Bubblewrap build handles existing destinations on read-only Lustre.
        lines.append(f"hpc_bwrap+=(--ro-bind {root_arg} /)")
    rebind = {path for path, _ in plan.get("rebind", [])}
    temporary_binds = []
    def add_bind(line, target):
        lines.append(line)
        if target.startswith(("/tmp/", "/var/tmp/")):
            temporary_binds.append(line)
    for mode in ("read_only", "read_write"):
        for path in plan[mode]:
            if path not in rebind:
                flag = "--ro-bind" if mode == "read_only" else "--bind"
                add_bind(f"hpc_bwrap+=({flag} {q(path)} {q(path)})", path)
    # Images are above ordinary binds, below hidden/protected paths.
    for index, (_, target) in enumerate(images):
        if target:
            add_bind(f'hpc_bwrap+=(--ro-bind "$hpc_sandbox_tmp/img/{index}" {q(target)})', target)
    for source, target in plan.get("masks", []):
        add_bind(f"hpc_bwrap+=(--ro-bind {q(source)} {q(target)})", target)
    for target in plan.get("protected", []):
        add_bind(f"hpc_bwrap+=(--ro-bind {q(target)} {q(target)})", target)
    for target, mode in plan.get("rebind", []):
        add_bind(f"hpc_bwrap+=({'--ro-bind' if mode == 'ro' else '--bind'} {q(target)} {q(target)})", target)
    # Never inherit the host's device tree, IPC/control sockets or proc mount.
    lines += ['hpc_bwrap+=(--proc /proc --dev /dev --dir /sys --tmpfs /run '
              '--bind "$hpc_sandbox_tmp/tmp" /tmp --bind "$hpc_sandbox_tmp/var-tmp" /var/tmp '
              '--bind "$hpc_sandbox_tmp/shm" /dev/shm)']
    # Keep explicitly approved projects/environments under /tmp visible above
    # private scratch, preserving the same image/mask/protected/rebind order.
    lines += temporary_binds
    for device in plan.get("devices", []):
        lines.append(f"[ -c {q(device)} ] || [ -b {q(device)} ] || "
                     f"{{ echo {q('hpclib: explicit device is missing or not a device: ' + device)} >&2; exit 125; }}")
        lines.append(f"hpc_bwrap+=(--dev-bind {q(device)} {q(device)})")
    own_account = plan["host_image"] and "/etc" in plan["read_only"]
    if own_account or charlie:
        # ch-run itself runs in the outer view. It also needs the directory
        # account when SSSD is unavailable, even with a custom inner image.
        lines += sandbox.account_lines(flag="--ro-bind")
        lines += ['hpc_bwrap+=("${hpc_sandbox_account[@]}")']
    # Strip control variables, exported shell functions and inherited auth/control sockets.
    lines += ['while IFS= read -r -d "" hpc_env; do',
              '  hpc_env=${hpc_env%%=*}',
              '  case "$hpc_env" in HPC_REST_TOKEN*|HPCLIB_BASE_ENV*|CH_RUN_*|BASH_FUNC_*|'
              'SINGULARITY*|APPTAINER*|ENROOT*|BWRAP*|LD_PRELOAD|LD_AUDIT|BASH_ENV|ENV|'
              'SSH_AUTH_SOCK|SSH_AGENT_PID|DBUS_SESSION_BUS_ADDRESS|XDG_RUNTIME_DIR)',
              '    hpc_bwrap+=(--unsetenv "$hpc_env");; esac',
              'done < <(env -0)',
              'hpc_bwrap+=(--setenv TMPDIR /tmp --setenv HOME /nonexistent --chdir "$PWD")']
    if charlie:
        # These private paths exist only in the trusted outer setup; ch-run pivots away from them.
        lines += [f"hpc_bwrap+=(--ro-bind {root_arg} /.hpclib/image --ro-bind {q(plan['runtime'])} /.hpclib/ch-run)"]
        inner = ["/.hpclib/ch-run", "--no-passwd"]
        inner_binds = list(dict.fromkeys(plan["read_only"] + plan["read_write"] +
                                       [target for _, target in plan.get("images", [])] + plan.get("hidden", []) +
                                       plan.get("protected", [])))
        for path in inner_binds:
            inner += ["--bind", f"{path}:{path}"]
        inner += ["--bind", "/var/tmp:/var/tmp", "--bind", "/dev/shm:/dev/shm"]
        if own_account:
            for name in ("passwd", "group", "nsswitch.conf"):
                if name != "nsswitch.conf" or os.path.isfile("/etc/nsswitch.conf"):
                    inner += ["--bind", f"/etc/{name}:/etc/{name}"]
        command = " ".join(q(a) for a in inner) + ' --cd "$PWD" /.hpclib/image -- '
    else:
        command = ""
    start = " ".join(q(word) for word in sandbox.interpreter(shebang))
    if prelude:
        text = "\n".join(prelude) + '\nexec "$@"'
        lines += [f"hpc_sandbox_prelude={q(text)}"]
        start = '/bin/bash -c "$hpc_sandbox_prelude" hpc-env ' + start
    lines += [f"exec 9<{q(policy)} || exit 125",
              'hpc_bwrap+=(--remount-ro / --seccomp 9)',
              f'if "${{hpc_bwrap[@]}}" {command}{start} -c "$hpc_sandbox_body" hpc-job; then',
              '  hpc_sandbox_status=0', 'else hpc_sandbox_status=$?; fi',
              'exec 9<&-', 'exit "$hpc_sandbox_status"']
    return lines
