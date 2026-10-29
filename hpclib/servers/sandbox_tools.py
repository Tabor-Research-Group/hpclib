"""Owner-side, unprivileged sandbox runtime installation for agent-console.

Run on the cluster login node, without importing the REST service. Installs
versioned, checksum-pinned upstream sources in the user's home, never sudo,
package-manager changes, shell startup edits or live sandbox configuration.
"""
import argparse
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path.home() / ".local/share/hpclib/sandbox-tools"
SOURCES = {
    "bubblewrap": ("0.13.1", "https://github.com/containers/bubblewrap/releases/download/v0.13.1/bubblewrap-0.13.1.tar.xz",
                   "0e85c8702248b8dd64144f594147553650cdd0e4c951690345faace79c217c4d"),
    "charliecloud": ("0.45.2", "https://gitlab.com/charliecloud/charliecloud/-/releases/v0.45.2/downloads/charliecloud-0.45.2.tar.gz",
                     "c3c8c46fc70c6ca1213855b8e735d49978ddfb7bf6132017e1717975e8dea0e8"),
    "libcap": ("2.78", "https://www.kernel.org/pub/linux/libs/security/linux-privs/libcap2/libcap-2.78.tar.xz",
               "0d621e562fd932ccf67b9660fb018e468a683d7b827541df27813228c996bb11"),
}
BINARIES = {"bubblewrap": "bwrap", "charliecloud": "ch-run"}
BWRAP_BUILD = "0.13.1-hpclib2"


def patch_readonly_destinations(source):
    """Apply the bundled upstream-source fix for existing mount points on Lustre.

    Lustre may report EROFS before EEXIST for mkdir/open(O_CREAT|O_EXCL)
    beneath a read-only bind, even when the destination already exists.
    Let the subsequent O_PATH|O_NOFOLLOW open validate that existing node.
    Missing nodes still fail; writable file creation still fails on EROFS.
    This changes setup only, never the final mount's read-only permission.
    """
    path = source / "bubblewrap.c"
    text = path.read_text()
    for old in (
            "if (mkdirat (parent_fd, name, perms >= 0 ? perms : 0755) < 0 && errno != EEXIST)",
            "if (mkdirat (parent_fd, component, mode) < 0 && errno != EEXIST)",
            "if (fd < 0 && errno != EEXIST)"):
        if text.count(old) != 1:
            raise RuntimeError("Bubblewrap source does not match the reviewed read-only destination patch")
        text = text.replace(old, old[:-1] + " && errno != EROFS)", 1)
    path.write_text(text)


def run(args, cwd=None, env=None, timeout=180):
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError(f"{Path(str(args[0])).name} failed ({result.returncode}):\n{result.stdout[-12000:]}")
    return result.stdout


def find(tool, root=ROOT):
    binary = BINARIES[tool]
    managed = Path(root) / "bin" / binary
    return str(managed) if managed.is_file() and os.access(managed, os.X_OK) else shutil.which(binary)


def report(root=ROOT):
    tools = {}
    for tool in BINARIES:
        path = find(tool, root)
        version = None
        if path:
            try:
                version = run([path, "--version"], timeout=10).strip()
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                version = str(error).splitlines()[0]
        tools[tool] = {"path": path, "version": version, "install_version": SOURCES[tool][0]}
    namespace = {"ok": False, "detail": "Linux unshare is required"}
    if platform.system() == "Linux" and shutil.which("unshare"):
        try:
            run(["unshare", "--user", "--map-root-user", "--mount", "--pid", "--net", "--ipc", "--uts",
                 "--cgroup", "--fork", "true"], timeout=10)
            namespace = {"ok": True, "detail": "User, mount, PID, network, IPC, UTS and cgroup namespaces work on this node"}
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            namespace = {"ok": False, "detail": str(error)}
    return {"host": platform.node(), "root": str(root), "tools": tools, "namespaces": namespace,
            "libseccomp": ctypes.util.find_library("seccomp"),
            "prerequisites": {name: shutil.which(name) for name in ("cc", "make", "pkg-config")},
            "note": "Checked on the login node; use sandbox_info on the REST node and validate compute nodes too."}


def unpack(name, build, cache):
    version, url, digest = SOURCES[name]
    archive = cache / url.rsplit("/", 1)[-1]
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        request = urllib.request.Request(url, headers={"User-Agent": "hpclib-sandbox-installer/1"})
        with urllib.request.urlopen(request, timeout=90) as response:
            data = response.read(30 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError(f"Checksum mismatch for {name} {version}; no source was executed")
        archive.write_bytes(data)
    destination = build / name
    destination.mkdir()
    with tarfile.open(archive) as source:
        # Pinned upstream archives can contain relative symlinks, never traversal,
        # devices or links escaping the extraction directory.
        base = destination.resolve()
        for member in source.getmembers():
            target = (base / member.name).resolve()
            if not (target == base or base in target.parents) or member.isdev() or member.isfifo():
                raise RuntimeError("Unsafe source archive member")
            if member.issym() or member.islnk():
                link = ((target.parent if member.issym() else base) / member.linkname).resolve()
                if not (link == base or base in link.parents):
                    raise RuntimeError("Unsafe source archive link")
        source.extractall(destination)
    return destination / f"{name}-{version}"


def _bubblewrap(build, destination, cache):
    dependencies = build / "dependencies"
    cap = unpack("libcap", build, cache)
    options = [f"prefix={dependencies}", "lib=lib", "SHARED=no", "PTHREADS=no", "USE_GPERF=no",
               "GOLANG=no", "PAM_CAP=no", "RAISE_SETFCAP=no"]
    run(["make", "-C", cap / "libcap", "-j1", *options])
    run(["make", "-C", cap / "libcap", "install-static", *options])
    tools = build / "build-tools"
    run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
         "--target", tools, "meson==1.3.2", "ninja==1.11.1.1"], timeout=240)
    environment = dict(os.environ, PYTHONPATH=str(tools),
                       PATH=str(tools / "bin") + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                       PKG_CONFIG_PATH=str(dependencies / "lib/pkgconfig"))
    source = unpack("bubblewrap", build, cache)
    patch_readonly_destinations(source)
    meson = [sys.executable, "-m", "mesonbuild.mesonmain"]
    run([*meson, "setup", build / "bwrap-build", source, f"--prefix={destination}", "--buildtype=release",
         "-Dselinux=disabled", "-Dman=disabled", "-Dbash_completion=disabled", "-Dzsh_completion=disabled",
         "-Dtests=false", "-Dprefer_static=true"], env=environment)
    run([*meson, "compile", "-C", build / "bwrap-build", "-j", "1"], env=environment)
    run([*meson, "install", "-C", build / "bwrap-build"], env=environment)


def _charliecloud(build, destination, cache):
    source = unpack("charliecloud", build, cache)
    run([source / "configure", f"--prefix={destination}", "--disable-ch-image", "--disable-test",
         "--disable-html", "--disable-man", "--disable-seccomp", "--disable-syslog", "--disable-impolite-checks",
         "--without-gc", "--without-json", "--without-squashfuse"], cwd=source)
    run(["make", "-j1"], cwd=source)
    run(["make", "install"], cwd=source)


def install(tool, root=ROOT):
    if tool not in BINARIES:
        raise ValueError("tool must be bubblewrap or charliecloud")
    if platform.system() != "Linux":
        raise RuntimeError("These runtimes must be installed on the Linux cluster")
    root = Path(root)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o022:
        raise RuntimeError("The installation directory must be owned by you, not a symlink and not writable by others")
    lock = root / ".install-lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        raise RuntimeError(f"An installation is already running; inspect {lock} if a previous installer was interrupted")
    try:
        info = report(root)
        if not info["namespaces"]["ok"]:
            raise RuntimeError("Namespace creation is blocked on this node: " + info["namespaces"]["detail"])
        if not info["libseccomp"]:
            raise RuntimeError("The libseccomp runtime library is missing; ask the site to install it")
        missing = [name for name, path in info["prerequisites"].items() if not path]
        if missing:
            raise RuntimeError("Load a compiler/build-tools module first; missing: " + ", ".join(missing))
        # Charliecloud is never installed as an unsafe standalone sandbox.
        wanted = ["bubblewrap", "charliecloud"] if tool == "charliecloud" else ["bubblewrap"]
        cache = root / "downloads"
        cache.mkdir(exist_ok=True)
        (root / "bin").mkdir(exist_ok=True)
        for item in wanted:
            target = root / item / (BWRAP_BUILD if item == "bubblewrap" else SOURCES[item][0])
            binary = target / "bin" / BINARIES[item]
            if not binary.is_file():
                if target.exists():
                    raise RuntimeError(f"Incomplete installation at {target}; inspect/remove it before retrying")
                with tempfile.TemporaryDirectory(prefix="build-", dir=root) as temporary:
                    build = Path(temporary)
                    (_bubblewrap if item == "bubblewrap" else _charliecloud)(build, target, cache)
                    run([binary, "--version"], timeout=10)
            for installed in target.rglob("*"):
                if installed.is_file() and not installed.is_symlink() and installed.stat().st_mode & (stat.S_ISUID | stat.S_ISGID):
                    raise RuntimeError("Refusing an installation with setuid/setgid files")
            link = root / "bin" / BINARIES[item]
            temporary_link = root / "bin" / (BINARIES[item] + ".new")
            temporary_link.unlink(missing_ok=True)
            temporary_link.symlink_to(binary)
            temporary_link.replace(link)
        return report(root)
    finally:
        lock.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", choices=tuple(BINARIES))
    parser.add_argument("--root", type=Path, default=ROOT)
    options = parser.parse_args()
    try:
        result = install(options.install, options.root) if options.install else report(options.root)
        result["ok"] = True
        code = 0
    except Exception as error:
        result = {"ok": False, "error": str(error)}
        code = 1
    print("HPCLIB_SANDBOX_TOOLS " + json.dumps(result))
    return code


if __name__ == "__main__":
    sys.exit(main())
