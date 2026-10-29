# Charliecloud and Bubblewrap sandboxes

These backends extend the Pyxis and Podman support. `auto` keeps its existing
preference: Apptainer/Singularity, Pyxis, then Podman; Bubblewrap is an additional
fallback. Installing a runtime does not switch an existing configuration.

## Install and select from agent-console

1. Apply this patch locally, restart agent-console, and update hpclib on the
   cluster using its normal installation/update control. Restart the agent
   tunnel so its REST process loads the new Python modules.
2. Log in to the cluster and open **Settings → Sandbox installation**.
3. Enter compiler/proxy modules if the login node needs them, then select
   **Install** beside Bubblewrap or Charliecloud + Bubblewrap. The Charliecloud
   option installs both. Builds use one worker and no administrator access.
4. Under **Server → Sandbox**, choose the method and **Save server settings**.
   The method changes immediately; installations and switching methods do not
   restart or alter existing jobs. Clear extra runtime flags when switching.
5. Select **Test active sandbox**. Review `self_test.passed`, namespace checks
   and resource-limit notes. This tests the REST node; validate compute nodes
   before relying on their capabilities.

Installation requires Linux, `cc`, `make`, `pkg-config`, Python with pip,
the libseccomp runtime library, network access, and unprivileged user/mount/PID/
network/IPC/UTS/cgroup namespaces. Site policy can prohibit these; hpclib reports
the failure and never changes sysctls, device permissions, groups or site configuration.

Runtimes install under `~/.local/share/hpclib/sandbox-tools/`, with stable paths:

```
~/.local/share/hpclib/sandbox-tools/bin/bwrap
~/.local/share/hpclib/sandbox-tools/bin/ch-run
```

hpclib prefers these managed builds over PATH without changing shell startup
files; an explicit absolute runtime path overrides detection. Their root
directory is protected from scoped-agent file writes and mounted read-only if
it falls under a writable parent. A manually configured runtime in an unprotected
agent-writable directory is refused. Installs use an exclusive lock, verified
source checksums and versioned destinations; no working runtime is overwritten
by a failed build. An interrupted installation may leave a lock or incomplete
version directory: inspect the named path before removing it and retrying.

Pinned sources: Charliecloud 0.45.2, Bubblewrap 0.13.1, libcap 2.78. libcap is
built privately and linked statically into Bubblewrap, so site development
headers are unnecessary. Meson 1.3.2 and Ninja 1.11.1.1 install into a temporary
private build-tools directory. System packages remain untouched.

The Bubblewrap build includes three small source changes for read-only mount
destinations. Vision's Lustre filesystem can return `EROFS` before `EEXIST` when
Bubblewrap attempts to create an already-existing mount point or parent. The
fix lets its existing `O_PATH`/`O_NOFOLLOW` checks open and validate that node;
missing nodes still fail, and read-only permissions remain enforced. See
`sandbox_tools.patch_readonly_destinations` for the complete, guarded source
patch. Its version directory is `bubblewrap/0.13.1-hpclib2`. Stock builds can
still be selected explicitly; use the managed build for this Lustre case.

## Policy and configuration

```json
{
  "sandbox": {
    "method": "bubblewrap",
    "network": "none",
    "binds": ["/sw", "/cm"],
    "writable": [],
    "devices": [],
    "allow_unsandboxed": false
  }
}
```

For Charliecloud, set `method` to `charliecloud`. `runtime` optionally selects
ch-run; `boundary_runtime` optionally selects its Bubblewrap. For Bubblewrap,
`runtime` selects bwrap. Empty paths use detection, including managed installs.

Both backends provide separate PID, IPC, UTS and user namespaces, no new
privileges, private temporary directories, a minimal device tree, and token
directory/protected-path semantics matching the existing backends. With
`network: none`, a fresh network namespace cannot reach host loopback or the
cluster network. `network: default` shares the node's network. Environment syncs
explicitly receive networking; services using `--sandbox-network host` do too.
The plan reports the effective network choice.

A native-architecture libseccomp policy denies debugging/memory access to
other processes, kernel/module control, keyrings, io_uring, userfaultfd,
open-by-handle and terminal injection. Bubblewrap also denies namespace/mount
creation and disables further user namespaces. Charliecloud needs those setup
calls in its nested user namespace; its source mounts remain locked by the
outer boundary. Its `--seccomp` root-emulation feature is unrelated and is not
used as a workload security filter. This is a deny policy, not an application
syscall allowlist; some applications may need changes for denied operations.

Environment control variables, exported shell functions and inherited SSH/
D-Bus authentication socket variables are removed. Modules and template
parameters remain available. This does not make arbitrary exported application
secrets safe: only expose credentials a workload is intended to use.

Extra runtime `flags` are refused for these methods because flags could undo
the filesystem, device or namespace policy. Use the structured settings instead.

## Why Charliecloud needs Bubblewrap

`ch-run -b SRC:DST IMAGE -- COMMAND` is fully unprivileged, but its bind option
has no read-only selector. A writable host directory remains writable through
that bind. Charliecloud also shares host processes and networking by itself.

hpclib constructs the permitted filesystem with Bubblewrap first, including
read-only software, writable token directories and secret/protected-path masks.
Trusted ch-run then runs in that view, binds those same sources into its image,
and starts the body. It cannot make an inherited locked read-only mount
writable. `/dev` and `/proc` come from the outer restricted view. Charliecloud
is refused if Bubblewrap is missing; there is no less-restricted fallback.

## Images and squashed Python environments

By default jobs use host software, without downloading a base OS image. Set
`image` to a directory or a raw SquashFS file to use another filesystem.
Custom images must contain the bind destinations and standard directories
(`/proc`, `/dev`, `/sys`, `/run`, `/tmp`, `/var/tmp`, `/dev/shm`). They also need
the selected body interpreter. For example, Alpine commonly has `/bin/sh`
rather than `/bin/bash`. These backends do not run SIF/ext3 images.

Root SquashFS images and additional squashed environments use the same external
FUSE strategy as Pyxis: `squashfuse_ll` or `squashfuse` mounts each file read-only
at a job-private temporary path, and the boundary binds that view at its intended
destination. Mounts are detached and scratch is removed on exit/signals.
The shared `.venv` directory is never used as a host mount point.

SquashFUSE and accessible `/dev/fuse` must already be available on execution
nodes. They are optional dependencies, not installed by this runtime installer.
Missing support stops the job with an actionable error; use an unsquashed
environment or have the site provide SquashFUSE. Jobs do not pull images.

## HPC access and limits

Job submission/status/cancellation stays in the trusted hpclib service, which
runs Slurm commands outside the payload boundary. Each submitted body receives
its own boundary. Do not expose Slurm/Munge sockets or SSH keys to payloads to
enable raw scheduler commands.

Devices are opt-in individual `/dev` paths. A GPU request without explicit
devices fails rather than exposing the entire device tree. NVIDIA workloads
also need driver libraries in read-only binds and the site-enforced allocation's
device access. Automatic CDI/CUDA injection and MPI/multi-node execution are
not implemented for these backends.

A cgroup namespace does not impose quotas. Slurm/site cgroups must enforce
compute CPU, memory, process and device limits. Login-node execution needs
administrator-supported limits or delegated cgroups; hpclib reports that these
backends do not themselves enforce CPU/memory quotas. Namespaces share the host
kernel. These controls are not a microVM kernel boundary or a security audit.

Primary references: [Charliecloud ch-run](https://charliecloud.io/latest/ch-run.html),
[Bubblewrap](https://github.com/containers/bubblewrap/blob/v0.13.1/README.md),
[Vision containers](https://docs.vision.tamus.edu/using-containers/).
