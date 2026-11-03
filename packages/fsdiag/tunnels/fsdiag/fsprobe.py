#!/usr/bin/env python3
"""
fsprobe: how long do this node's file system calls really take?

The REST server, the tunnels and the agents' jobs all spend their time in a handful of file system calls: look up a
name, stat it, open it, create a file, rename it, flush it, list a directory. On a network file system each of those
is a round trip to a server that other people are using too, so a call that takes microseconds on a local disk can
take seconds, and everything that makes a lot of them (a web request that resolves five paths, a SQLite commit)
crawls. This measures them, one by one, in the places you name, next to a node-local control (/dev/shm or /tmp), and
says what the mount is, how loaded the node is, and, on NFS, how long the server took to answer and how long the
requests waited in this node's queue.

  fsprobe.py [--path PATH ...] [--repeats N] [--threads N] [--max-seconds S]
             [--survey ROOT ...] [--survey-seconds S] [--no-quota]
             [--where login|compute] [--out FILE] [--text]

Without --path it probes your home directory, hpclib's data directory (~/.local/tunnels) and your scratch directory
when it finds one. It writes JSON (to --out, else to standard output), or with --text a table. Everything it creates
is inside a directory named .hpclib-fsdiag-HOST-PID in each place, which it removes again; a place it can't write to
gets the read-only measurements.

What is measured, per place (milliseconds: min, median, p95, max of N samples, and the samples in order):

  stat_missing        a name that isn't there (every search of PATH does this)
  stat_existing       an existing entry, never looked at before
  open_read_existing  open and read the start of an existing file
  realpath            resolving the path itself (one lstat per component)
  listdir_root        listing the directory itself
  mkdir, create, write, fsync, stat_new, rename, open_read, unlink     the life of a small file
  listdir_small       listing a directory of 100 files and stat-ing each
  create_xN           create, from N threads at once (does the latency grow when requests overlap?)
  sqlite_commit       one SQLite transaction (the job registry is a SQLite file): journal, fsync, locks

plus the mount (type, server, options), the node's load (load average, I/O pressure, I/O wait during the run, how many
processes are stuck in uninterruptible I/O), the file system's inode and block use, for NFS the server's round-trip
and queue times from /proc/self/mountstats taken over the run, and, with --survey, a bounded walk of the roots that
says where the files are (the biggest directories, how long each took to list, the slow ones) and, unless
--no-quota, what the cluster's own quota commands say.

Python 3.6 or newer, standard library only; it also runs from standard input (python3 - ARGS < fsprobe.py), which is how
the compute-node jobs run it.
"""
import argparse
import heapq
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

VERSION = "1.0.0"
FORMAT = 1
SAMPLES_KEPT = 200
SLOW_OP_SECONDS = 10          # one call this slow stops the probe of that place: it would only take longer
NETWORK_TYPES = ("nfs", "nfs4", "lustre", "gpfs", "beegfs", "cifs", "smb3", "ceph", "glusterfs", "panfs", "wekafs",
                 "pvfs2", "orangefs", "afs", "9p", "ocfs2", "gfs2")

OP_INFO = {
    "stat_missing": "look up a name that isn't there (every search of PATH does this)",
    "stat_existing": "stat an existing entry",
    "open_read_existing": "open an existing file and read its start",
    "realpath": "resolve a path (an lstat per component)",
    "listdir_root": "list the directory itself",
    "mkdir": "make a directory",
    "create": "create a new file",
    "write": "write 4 KiB (into the page cache)",
    "fsync": "flush the file to the server (what SQLite and every safe save wait for)",
    "stat_new": "stat the file just made",
    "rename": "rename a file (how a file is replaced safely)",
    "open_read": "open the file again and read it",
    "unlink": "delete a file",
    "listdir_small": "list a directory of 100 files and stat each",
    "sqlite_commit": "one SQLite transaction (the job registry's kind)",
}
ORDER = ["stat_missing", "stat_existing", "open_read_existing", "realpath", "listdir_root", "mkdir", "create", "write",
         "fsync", "stat_new", "rename", "open_read", "unlink", "listdir_small"]
NFS_OPS = ("GETATTR", "LOOKUP", "ACCESS", "OPEN", "CLOSE", "READDIR", "READDIRPLUS", "READ", "WRITE", "COMMIT",
           "CREATE", "REMOVE", "RENAME", "SETATTR", "OPEN_NOATTR", "LOOKUP_ROOT")


# ------------------------------------------------------------------------------------------------------ numbers

def _r(x):
    return round(x, 3) if x < 100 else round(x, 1)


def summarize(seconds):
    """min, median, p95, max, mean (ms) and the samples in the order they were taken; None for no samples."""
    if not seconds:
        return None
    in_order = [s * 1000.0 for s in seconds]
    ms = sorted(in_order)
    n = len(ms)

    def at(p):
        return ms[min(n - 1, int(round(p * (n - 1))))]
    return {"n": n, "min": _r(ms[0]), "median": _r(at(0.5)), "p95": _r(at(0.95)), "max": _r(ms[-1]),
            "mean": _r(sum(ms) / n), "samples_ms": [_r(x) for x in in_order[:SAMPLES_KEPT]]}


class Samples:
    """Timings by operation name; `time(name, fn, *args)` runs fn and keeps how long it took."""

    def __init__(self):
        self.by_op = {}
        self.slowest = 0.0

    def time(self, name, fn, *args):
        started = time.perf_counter()
        try:
            return fn(*args)
        finally:
            took = time.perf_counter() - started
            self.by_op.setdefault(name, []).append(took)
            self.slowest = max(self.slowest, took)


# ------------------------------------------------------------------------------------------------------ the mounts

def _unescape(text):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def read_mounts(source="/proc/self/mountinfo"):
    """[{mountpoint, fstype, source, options}] from mountinfo (empty where there isn't one)."""
    mounts = []
    try:
        with open(source) as f:
            for line in f:
                parts = line.split()
                if "-" not in parts[6:]:
                    continue
                i = parts.index("-", 6)
                if len(parts) < i + 3:
                    continue
                options = parts[5] + ("," + parts[i + 3] if len(parts) > i + 3 else "")
                mounts.append({"mountpoint": _unescape(parts[4]), "fstype": parts[i + 1], "source": _unescape(parts[i + 2]),
                               "options": options})
    except (OSError, IndexError):
        pass
    return mounts


def mount_for(path, mounts):
    """The mount `path` (already resolved) is on: the one with the longest mount point that contains it."""
    best = None
    for m in mounts:
        mp = m["mountpoint"].rstrip("/")
        if path == m["mountpoint"] or path.startswith(mp + "/") or m["mountpoint"] == "/":
            if best is None or len(m["mountpoint"]) > len(best["mountpoint"]):
                best = m
    return best


def describe_mount(mount):
    """The mount with the options that matter for latency pulled out, and whether it is a network file system."""
    if mount is None:
        return None
    out = dict(mount)
    fstype = mount["fstype"]
    out["network"] = fstype in NETWORK_TYPES or fstype.startswith("fuse.")
    wanted = ("vers", "proto", "rsize", "wsize", "hard", "soft", "timeo", "retrans", "actimeo", "acregmin", "acdirmin",
              "lookupcache", "nconnect", "sec", "local_lock", "noac", "sync", "async", "noatime", "relatime")
    found = {}
    for item in mount["options"].split(","):
        key, eq, value = item.partition("=")
        if key in wanted:
            found[key] = value if eq else True
    out["tuning"] = found
    return out


# ------------------------------------------------------------------------------------- NFS server and queue times

def read_mountstats(source="/proc/self/mountstats"):
    """{mountpoint: {fstype, ops: {NAME: [ops, transmissions, timeouts, sent, received, queue_ms, rtt_ms, exec_ms]},
    xprt: {sends, backlog}}} for the NFS mounts; {} where the kernel doesn't say."""
    stats, current, in_ops = {}, None, False
    try:
        with open(source) as f:
            for line in f:
                m = re.match(r"device (\S+) mounted on (.+?) with fstype (\S+)", line)
                if m:
                    current = {"fstype": m.group(3), "source": m.group(1), "ops": {}, "xprt": {}}
                    stats[m.group(2)] = current
                    in_ops = False
                    continue
                if current is None:
                    continue
                if line.strip().startswith("xprt:"):
                    parts = line.split()[1:]
                    try:
                        if parts[0] == "tcp" and len(parts) >= 11:
                            sends = int(parts[6])
                            current["xprt"] = {"protocol": "tcp", "sends": sends, "backlog_util": int(parts[10])}
                    except (ValueError, IndexError):
                        pass
                elif line.strip().startswith("per-op statistics"):
                    in_ops = True
                elif in_ops:
                    m = re.match(r"\s*(\w+):\s+((?:\d+\s*)+)$", line)
                    if m and m.group(1) in NFS_OPS:
                        values = [int(v) for v in m.group(2).split()]
                        if len(values) >= 8:
                            current["ops"][m.group(1)] = values[:8]
    except (OSError, ValueError):
        return {}
    return {mp: s for mp, s in stats.items() if s["fstype"].startswith("nfs")}


def nfs_difference(before, after, mountpoint):
    """How the NFS calls on `mountpoint` fared between two reads of mountstats: per operation how many, the average
    round trip to the server (rtt), the average wait in this node's queue before it was sent (queue), and the whole
    request (exec), in ms; and the same over all operations. None when nothing was sent."""
    a, b = before.get(mountpoint), after.get(mountpoint)
    if not a or not b:
        return None
    ops, total = {}, [0, 0, 0, 0]
    for name, now in b["ops"].items():
        was = a["ops"].get(name, [0] * 8)
        count = now[0] - was[0]
        if count <= 0:
            continue
        queue, rtt, execute = (now[5] - was[5]), (now[6] - was[6]), (now[7] - was[7])
        ops[name] = {"count": count, "avg_rtt_ms": round(rtt / count, 2), "avg_queue_ms": round(queue / count, 2),
                     "avg_exec_ms": round(execute / count, 2), "retransmissions": max(0, (now[1] - was[1]) - count),
                     "timeouts": now[2] - was[2]}
        total[0] += count
        total[1] += queue
        total[2] += rtt
        total[3] += execute
    if not total[0]:
        return None
    sends = b["xprt"].get("sends", 0) - a["xprt"].get("sends", 0)
    backlog = b["xprt"].get("backlog_util", 0) - a["xprt"].get("backlog_util", 0)
    return {"mountpoint": mountpoint, "operations": total[0], "avg_rtt_ms": round(total[2] / total[0], 2),
            "avg_queue_ms": round(total[1] / total[0], 2), "avg_exec_ms": round(total[3] / total[0], 2),
            "backlog_average": round(backlog / sends, 2) if sends > 0 else None, "ops": ops}


# --------------------------------------------------------------------------------------------- the node's own state

def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def cpu_times():
    """(iowait, total) jiffies from /proc/stat, or None."""
    text = _read("/proc/stat")
    if not text:
        return None
    try:
        values = [int(v) for v in text.splitlines()[0].split()[1:]]
        return values[4], sum(values[:8])
    except (ValueError, IndexError):
        return None


def uninterruptible_processes(limit=5000, seconds=0.5):
    """(count, [most common command names]) of the processes in D state: waiting on I/O that can't be interrupted,
    which is what a stalled file system looks like from the node's side."""
    names, counted, deadline = {}, 0, time.monotonic() + seconds
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()][:limit]
    except OSError:
        return None, []
    for pid in pids:
        if time.monotonic() > deadline:
            break
        text = _read("/proc/{}/stat".format(pid))
        if not text or ")" not in text:
            continue
        rest = text.rsplit(")", 1)[1].split()
        if rest and rest[0] == "D":
            counted += 1
            comm = text.split("(", 1)[1].rsplit(")", 1)[0]
            names[comm] = names.get(comm, 0) + 1
    return counted, [n for n, _ in sorted(names.items(), key=lambda kv: -kv[1])[:5]]


def read_context():
    """Load, I/O pressure, dirty memory and stuck processes now."""
    out = {"cpus": os.cpu_count()}
    try:
        out["loadavg"] = [float(x) for x in (_read("/proc/loadavg") or "").split()[:3]]
    except ValueError:
        out["loadavg"] = None
    psi = _read("/proc/pressure/io")
    if psi:
        found = {}
        for line in psi.splitlines():
            kind, _, rest = line.partition(" ")
            fields = dict(item.split("=", 1) for item in rest.split() if "=" in item)
            try:
                found[kind] = {k: float(v) for k, v in fields.items() if k.startswith("avg")}
            except ValueError:
                pass
        out["io_pressure"] = found or None
    meminfo = _read("/proc/meminfo") or ""
    for key, label in (("Dirty", "dirty_kb"), ("Writeback", "writeback_kb"), ("MemAvailable", "mem_available_kb")):
        m = re.search(r"^{}:\s+(\d+)".format(key), meminfo, re.M)
        if m:
            out[label] = int(m.group(1))
    count, names = uninterruptible_processes()
    out["d_state_processes"], out["d_state_names"] = count, names
    return out


# ----------------------------------------------------------------------------------------------- one place's probe

def _small_write(fd, block):
    os.write(fd, block)


def probe_path(path, label, repeats=15, threads=4, deadline=None, mounts=None, include_sqlite=True):
    """Everything fsprobe measures at one place. Never raises: what went wrong goes in "errors"."""
    started = time.monotonic()
    result = {"path": path, "label": label, "ops": {}, "errors": [], "read_only": False, "truncated": False}
    mounts = mounts if mounts is not None else read_mounts()
    samples = Samples()
    expanded = os.path.expanduser(path)

    def over():
        return (deadline is not None and time.monotonic() > deadline) or samples.slowest > SLOW_OP_SECONDS

    try:
        real = samples.time("realpath", os.path.realpath, expanded)
    except OSError as e:
        result["errors"].append("realpath: {}".format(e))
        return result
    result["real"] = real
    result["mount"] = describe_mount(mount_for(real, mounts))
    if not os.path.isdir(real):
        result["errors"].append("{} is not a directory here".format(path))
        return result
    for _ in range(max(0, repeats - 1)):
        samples.time("realpath", os.path.realpath, expanded)
    try:
        st = os.statvfs(real)
        if st.f_files:
            result["statvfs"] = {"inodes_total": st.f_files, "inodes_free": st.f_ffree,
                                 "inodes_used_percent": round(100.0 * (st.f_files - st.f_ffree) / st.f_files, 1),
                                 "bytes_total": st.f_blocks * st.f_frsize, "bytes_free": st.f_bavail * st.f_frsize}
    except OSError:
        pass
    # reads: nothing to create, so they work in a read-only place too
    try:
        names = samples.time("listdir_root", os.listdir, real)
        result["entries"] = len(names)
        names.sort()
        for name in names[:repeats]:
            if over():
                break
            full = os.path.join(real, name)
            try:
                samples.time("stat_existing", os.lstat, full)
            except OSError:
                pass
        reads = 0
        for name in names:
            if reads >= min(5, repeats) or over():
                break
            full = os.path.join(real, name)
            try:
                if os.path.isfile(full) and not os.path.islink(full) and os.path.getsize(full) < (1 << 20):
                    def read_start(p=full):
                        with open(p, "rb") as f:
                            return f.read(4096)
                    samples.time("open_read_existing", read_start)
                    reads += 1
            except OSError:
                pass
    except OSError as e:
        result["errors"].append("listing {}: {}".format(path, e))
    for i in range(repeats):
        if over():
            break
        samples.time("stat_missing", os.path.exists, os.path.join(real, ".hpclib-fsdiag-missing-{}-{}".format(os.getpid(), i)))
    # writes: in a directory of our own, removed at the end
    work = os.path.join(real, ".hpclib-fsdiag-{}-{}".format(socket.gethostname().split(".")[0], os.getpid()))
    made = False
    try:
        try:
            samples.time("mkdir", os.mkdir, work)
            made = True
        except OSError as e:
            result["read_only"] = True
            result["errors"].append("can't write here ({}): only the read measurements were made".format(e.strerror or e))
        if made:
            block = b"x" * 4096
            for i in range(repeats):
                if over():
                    break
                f1, f2 = os.path.join(work, "f{}".format(i)), os.path.join(work, "g{}".format(i))
                fd = samples.time("create", os.open, f1, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    samples.time("write", _small_write, fd, block)
                    samples.time("fsync", os.fsync, fd)
                finally:
                    os.close(fd)
                samples.time("stat_new", os.stat, f1)
                samples.time("rename", os.rename, f1, f2)

                def read_back(p=f2):
                    with open(p, "rb") as f:
                        return f.read()
                samples.time("open_read", read_back)
                samples.time("unlink", os.unlink, f2)
            if not over():
                small = os.path.join(work, "small")
                os.mkdir(small)
                for i in range(100):
                    with open(os.path.join(small, "s{}".format(i)), "wb"):
                        pass

                def list_and_stat():
                    for entry in os.scandir(small):
                        entry.stat()
                for _ in range(max(1, repeats // 3)):
                    samples.time("listdir_small", list_and_stat)
                shutil.rmtree(small, ignore_errors=True)
            if threads > 1 and not over():
                result["concurrent"] = _concurrent_creates(work, threads, repeats, samples, deadline)
            if include_sqlite and not over():
                result["sqlite"] = _sqlite_probe(work, repeats, samples)
    finally:
        if made:
            shutil.rmtree(work, ignore_errors=True)
    result["truncated"] = over() and (deadline is not None and time.monotonic() > deadline or samples.slowest > SLOW_OP_SECONDS)
    if samples.slowest > SLOW_OP_SECONDS:
        result["errors"].append("a call took {:.0f} s, so the probe of this place stopped early".format(samples.slowest))
    result["ops"] = {name: summarize(vals) for name, vals in samples.by_op.items()}
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


def _concurrent_creates(work, threads, repeats, samples, deadline):
    """`threads` threads each creating, writing and deleting files at once: the latency of each create, and the rate."""
    mine = [Samples() for _ in range(threads)]
    errors = []

    def worker(index):
        try:
            for i in range(repeats):
                if deadline is not None and time.monotonic() > deadline:
                    break
                p = os.path.join(work, "c{}-{}".format(index, i))
                fd = mine[index].time("create", os.open, p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                os.unlink(p)
        except OSError as e:
            errors.append(str(e))
    started = time.perf_counter()
    pool = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    wall = time.perf_counter() - started
    every = [s for m in mine for s in m.by_op.get("create", [])]
    name = "create_x{}".format(threads)
    if every:
        samples.by_op[name] = every
    return {"threads": threads, "operations": len(every), "seconds": round(wall, 3),
            "per_second": round(len(every) / wall, 1) if wall > 0 else None, "errors": errors[:3]}


def _sqlite_probe(work, repeats, samples):
    """One SQLite transaction at a time into a file here: what the job registry (jobs.sqlite) does on every submit."""
    try:
        import sqlite3
    except ImportError:
        return {"ok": False, "error": "this Python has no sqlite3"}
    db_path = os.path.join(work, "probe.sqlite")
    out = {"ok": True}
    try:
        connection = sqlite3.connect(db_path, timeout=30)
        try:
            out["journal_mode"] = connection.execute("PRAGMA journal_mode").fetchone()[0]
            connection.execute("CREATE TABLE t (a INTEGER, b TEXT)")
            connection.commit()
            for i in range(max(5, repeats // 2)):
                started = time.perf_counter()
                connection.execute("INSERT INTO t VALUES (?, ?)", (i, "x" * 100))
                connection.commit()
                took = time.perf_counter() - started
                samples.by_op.setdefault("sqlite_commit", []).append(took)
                samples.slowest = max(samples.slowest, took)
        finally:
            connection.close()
    except Exception as e:  # noqa: BLE001  (a locking failure is a result, not a crash)
        out["ok"] = False
        out["error"] = "{}: {}".format(type(e).__name__, e)
    return out


# ------------------------------------------------------------------------------------------------- where the files are

def survey(root, max_entries=500000, max_seconds=60.0, top=15, slow_ms=250.0):
    """A bounded walk of `root` (symlinks are not followed), counting entries per directory without stat-ing them:
    totals, the biggest directories with how long each took to list, the slow listings, a histogram of directory sizes,
    and what each top-level child holds. `truncated` says it stopped at max_entries or max_seconds."""
    root = os.path.realpath(os.path.expanduser(root))
    started = time.perf_counter()
    out = {"root": root, "entries": 0, "directories": 0, "truncated": False, "errors": 0}
    if not os.path.isdir(root):
        out["error"] = "not a directory"
        return out
    stack = [(root, "(directly in the folder)")]
    big, slow, children = [], [], {}
    buckets = [("under 100", 0), ("100 to 1,000", 0), ("1,000 to 10,000", 0), ("10,000 to 100,000", 0), ("100,000 or more", 0)]
    counts = [0] * len(buckets)
    listing_time = 0.0
    while stack:
        if out["entries"] >= max_entries or time.perf_counter() - started > max_seconds:
            out["truncated"] = True
            break
        directory, child = stack.pop()
        t = time.perf_counter()
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            out["errors"] += 1
            continue
        took = time.perf_counter() - t
        listing_time += took
        n = len(entries)
        out["entries"] += n
        out["directories"] += 1
        counts[0 if n < 100 else 1 if n < 1000 else 2 if n < 10000 else 3 if n < 100000 else 4] += 1
        entry = children.setdefault(child, {"name": child, "entries": 0, "directories": 0, "complete": True})
        entry["entries"] += n
        entry["directories"] += 1
        if n >= 1000:
            heapq.heappush(big, (n, directory, took * 1000.0))
            if len(big) > top:
                heapq.heappop(big)
        if took * 1000.0 >= slow_ms:
            slow.append((took * 1000.0, directory, n))
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, child if directory != root else e.name))
            except OSError:
                pass
    if out["truncated"]:
        for _, child in stack:
            if child in children:
                children[child]["complete"] = False
            else:
                children[child] = {"name": child, "entries": 0, "directories": 0, "complete": False}
    wall = time.perf_counter() - started
    out["seconds"] = round(wall, 2)
    out["entries_per_second"] = round(out["entries"] / wall) if wall > 0 else None
    out["listing_seconds"] = round(listing_time, 2)
    out["largest_directories"] = [{"path": p, "entries": n, "list_ms": round(ms, 1)} for n, p, ms in sorted(big, reverse=True)]
    out["slow_listings"] = [{"path": p, "entries": n, "list_ms": round(ms, 1)} for ms, p, n in sorted(slow, reverse=True)[:top]]
    out["directory_sizes"] = [{"label": label, "directories": c} for (label, _), c in zip(buckets, counts)]
    out["children"] = sorted(children.values(), key=lambda c: -c["entries"])[:30]
    return out


# ------------------------------------------------------------------------------------------------------------ quota

def _run(argv, timeout=20):
    started = time.perf_counter()
    try:
        res = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             universal_newlines=True, timeout=timeout)
        code, text = res.returncode, res.stdout or ""
    except subprocess.TimeoutExpired:
        code, text = None, "took longer than {} s".format(timeout)
    except OSError as e:
        code, text = 127, str(e)
    return {"command": " ".join(argv), "exit_code": code, "lines": text.splitlines()[:60],
            "seconds": round(time.perf_counter() - started, 2)}


def quota_checks(places):
    """What the cluster's own quota commands say (hpclib's lib/quota.sh when it is installed, else showquota, myquota
    or quota -s), and `lfs quota` for a Lustre place. The text is the site's: not parsed."""
    found = []
    here = os.path.dirname(os.path.abspath(globals().get("__file__") or "."))   # no __file__ when run from standard input
    script = next((p for p in (os.path.expanduser("~/hpclib/lib/quota.sh"), os.path.join(here, "..", "..", "lib", "quota.sh"))
                   if os.path.isfile(p)), None)
    if script:
        found.append(_run(["bash", script, "storage"], timeout=45))
    else:
        for tool in (["showquota"], ["myquota"], ["quota", "-s"]):
            if shutil.which(tool[0]):
                found.append(_run(tool))
                break
    if shutil.which("lfs"):
        user = os.environ.get("USER") or ""
        for place in places:
            if place.get("mount") and place["mount"]["fstype"] == "lustre" and user:
                found.append(_run(["lfs", "quota", "-h", "-u", user, place.get("real") or place["path"]]))
    return found


# -------------------------------------------------------------------------------------------------- the whole probe

def control_path():
    """A node-local place to compare against: /dev/shm, else the temp directory."""
    for candidate in ("/dev/shm", tempfile.gettempdir()):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return None


def default_places():
    """[(label, path)] of home, hpclib's data directory and a scratch directory, as far as they exist."""
    home = os.path.expanduser("~")
    places = [("home", home)]
    data = os.path.expanduser(os.environ.get("HPCTUNNELS_DATA_DIR") or "~/.local/tunnels")
    if os.path.isdir(data):
        places.append(("hpclib data", data))
    user = os.environ.get("USER") or os.path.basename(home)
    for candidate in (os.environ.get("SCRATCH"), "/scratch/user/" + user, "/scratch/" + user, "/lustre/scratch/" + user):
        if candidate and os.path.isdir(candidate):
            places.append(("scratch", candidate))
            break
    return places


def run_probes(places, repeats=15, threads=4, max_seconds=180.0, survey_roots=(), survey_seconds=60.0, quota=True,
               where=None, progress=None):
    """The whole result (see the module's description). `places` is [(label, path)] or [path]; the control is added.
    No places (and no control) means no latency probe: only the survey, the quota commands and the node's state."""
    say = progress or (lambda text: None)
    begun = time.time()
    mounts = read_mounts()
    chosen = []
    for item in places:
        label, path = item if isinstance(item, (tuple, list)) else (os.path.basename(item.rstrip("/")) or item, item)
        if all(path != p for _, p in chosen):
            chosen.append((label, path))
    control = control_path() if chosen else None
    if control and all(os.path.realpath(control) != os.path.realpath(os.path.expanduser(p)) for _, p in chosen):
        chosen.append(("control", control))
    iowait0, total0 = cpu_times() or (None, None)
    context = read_context()
    deadline_all = time.monotonic() + max_seconds
    out = {"format": FORMAT, "fsprobe": VERSION, "host": socket.gethostname(), "node": os.environ.get("SLURMD_NODENAME") or socket.gethostname(),
           "where": where or ("compute" if os.environ.get("SLURM_JOB_ID") else "login"), "job_id": os.environ.get("SLURM_JOB_ID"),
           "user": os.environ.get("USER"), "python": sys.version.split()[0], "started": begun, "paths": [],
           "context": context, "repeats": repeats, "threads": threads}
    for index, (label, path) in enumerate(chosen):
        say("probing {} ({})".format(label, path))
        left = len(chosen) - index
        budget = max(5.0, (deadline_all - time.monotonic()) / left)
        before = read_mountstats()
        result = probe_path(path, label, repeats, threads, time.monotonic() + budget, mounts)
        after = read_mountstats()
        mount = result.get("mount")
        if mount and mount["fstype"].startswith("nfs"):
            result["nfs"] = nfs_difference(before, after, mount["mountpoint"])
        out["paths"].append(result)
    iowait1, total1 = cpu_times() or (None, None)
    if iowait0 is not None and total1 and total1 > total0:
        context["iowait_percent_during_probe"] = round(100.0 * (iowait1 - iowait0) / (total1 - total0), 1)
    out["surveys"] = []
    for root in survey_roots:
        say("surveying {}".format(root))
        out["surveys"].append(survey(root, max_seconds=survey_seconds))
    if quota:
        say("asking the cluster for quotas")
        out["quota"] = quota_checks(out["paths"])
    out["finished"] = time.time()
    return out


# ------------------------------------------------------------------------------------------------------------ report

def _ms(stats, key="median"):
    return "{:>9.2f}".format(stats[key]) if stats else "{:>9}".format("-")


def render_text(result):
    """A readable table of a result (the CLI's --text)."""
    lines = ["{} ({}), {}, Python {}".format(result.get("node"), result.get("where"),
             time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result.get("started", 0))), result.get("python"))]
    ctx = result.get("context") or {}
    if ctx:
        lines.append("load {}  cpus {}  I/O wait during probe {}%  processes in D state {} {}".format(
            ctx.get("loadavg"), ctx.get("cpus"), ctx.get("iowait_percent_during_probe"), ctx.get("d_state_processes"),
            ctx.get("d_state_names") or ""))
    for p in result["paths"]:
        mount = p.get("mount") or {}
        lines += ["", "{} ({})  {} on {}".format(p["label"], p["path"], mount.get("fstype", "?"), mount.get("source", "?"))]
        for e in p["errors"]:
            lines.append("  ! " + e)
        lines.append("  {:<22}{:>9}{:>9}{:>9}   ms".format("", "median", "p95", "max"))
        names = [n for n in ORDER if n in p["ops"]] + sorted(n for n in p["ops"] if n not in ORDER)
        for name in names:
            s = p["ops"][name]
            lines.append("  {:<22}{}{}{}".format(name, _ms(s), _ms(s, "p95"), _ms(s, "max")))
        nfs = p.get("nfs")
        if nfs:
            lines.append("  NFS: {} calls, server round trip {} ms, wait in this node's queue {} ms".format(
                nfs["operations"], nfs["avg_rtt_ms"], nfs["avg_queue_ms"]))
    for s in result.get("surveys", []):
        lines += ["", "survey of {}: {} entries in {} directories, {} s{}".format(
            s["root"], s["entries"], s["directories"], s.get("seconds"), " (stopped early)" if s["truncated"] else "")]
        for d in s.get("largest_directories", [])[:8]:
            lines.append("  {:>9} entries  {:>8.0f} ms to list  {}".format(d["entries"], d["list_ms"], d["path"]))
    for q in result.get("quota", []):
        lines += ["", "$ {}  (exit {}, {} s)".format(q["command"], q["exit_code"], q["seconds"])] + ["  " + l for l in q["lines"][:20]]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="Time this node's file system calls, per place, with a local control.")
    p.add_argument("--path", action="append", default=[], help="a place to probe, or LABEL=PATH (repeat it); default: home, hpclib's data directory and scratch")
    p.add_argument("--repeats", type=int, default=15)
    p.add_argument("--threads", type=int, default=4, help="threads for the concurrent create test (1 turns it off)")
    p.add_argument("--max-seconds", type=float, default=180.0, help="the whole latency probe's time limit")
    p.add_argument("--survey", action="append", default=[], help="a folder to walk and count (repeat it)")
    p.add_argument("--survey-seconds", type=float, default=60.0)
    p.add_argument("--no-quota", action="store_true")
    p.add_argument("--no-latency", action="store_true", help="skip the latency probe (only the survey, quota and node state)")
    p.add_argument("--where", choices=("login", "compute"), default=None)
    p.add_argument("--out", help="write the JSON here (through a temporary file) instead of to standard output")
    p.add_argument("--text", action="store_true", help="print a table, not JSON")
    args = p.parse_args(argv)
    if sys.version_info < (3, 6):
        print("fsprobe: needs Python 3.6 or newer", file=sys.stderr)
        return 2
    places = []
    for x in args.path:
        label, eq, rest = x.partition("=")
        places.append((label, rest) if eq and "/" not in label and rest else (os.path.basename(x.rstrip("/")) or x, x))
    places = [] if args.no_latency else places or default_places()
    result = run_probes(places, max(3, min(args.repeats, 200)), max(1, min(args.threads, 32)), args.max_seconds,
                        args.survey, args.survey_seconds, not args.no_quota, args.where,
                        progress=lambda text: print("fsprobe: " + text, file=sys.stderr, flush=True))
    if args.out:
        tmp = args.out + ".part"
        with open(tmp, "w") as f:
            json.dump(result, f)
        os.replace(tmp, args.out)
    if args.text or not args.out:
        print(render_text(result) if args.text else json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
