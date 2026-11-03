"""
fsrequests: why were the REST server's requests slow, and what was each one waiting for?

The REST server (hpclib 1.13.13 and newer) records, for every request, how long it took (duration_ms), how long its
thread computed (cpu_ms), the server's memory and its thread count in its audit log; a request that runs longer than
a second is also named in the server's own log (the tunnel's session log), and one still running after five seconds
gets the stack of the call it is waiting in. This reads those and answers:

  - which routes are slow, and how slow (median, 95th percentile, the worst, the total time they cost);
  - whether they were *working* or *waiting*: a request that took 10 s of which 0.5 ms was CPU was waiting, on a
    file system, a lock, a subprocess or the network (the wait share is 1 - cpu/wall);
  - what they were waiting in, from the stack dumps: resolving a path, a stat, opening a file, a SQLite query, a
    directory listing, a PATH search, a subprocess, a lock, and which route and function it was;
  - when: a per-minute timeline, so a stall that hit every request at once shows up as one bump.

  load(data_dir, hours=24) -> the analysis (a dict)         analyze(audit, slow_lines, stacks) when you have parsed them

Python 3.6 or newer, standard library only.
"""
import glob
import json
import os
import re
import time

SLOW_MS = 1000
TAIL_BYTES = 16 << 20
STACK_HEADER = re.compile(r"^\[(?P<ts>[^\]]+)\] request still running after (?P<seconds>[\d.]+) s: (?P<verb>[A-Z]+) (?P<path>\S+)"
                          r" \(thread (?P<thread>\d+)\); it is waiting here:\s*$")
SLOW_LINE = re.compile(r"^\[(?P<ts>[^\]]+)\] slow request: (?P<verb>[A-Z]+) (?P<path>\S+) -> (?P<status>\d+) in (?P<ms>[\d.]+) ms"
                       r"(?: \(cpu (?P<cpu>[\d.]+) ms)?")
FRAME = re.compile(r'^  File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')

# what a blocked call was doing, from the innermost frame's function and source line; first match wins
CATEGORIES = [
    ("searching PATH (shutil.which)", r"\bwhich\b|_access_check"),
    ("resolving a path (realpath)", r"realpath|_joinrealpath"),
    ("SQLite (the job registry)", r"\bexecute\b|fetchall|fetchone|executemany|sqlite|\bcommit\b"),
    ("listing a directory", r"listdir|scandir|\bwalk\b|iterdir|\bglob\b"),
    ("opening or reading a file", r"\bopen\(|\.read\(|\bread_text\b|json\.load|read_tokens_file|\bload\b"),
    ("stat / exists / access", r"\bexists\b|\bisdir\b|\bisfile\b|\bislink\b|\blexists\b|\baccess\b|\bstat\b|lstat|getsize|getmtime"),
    ("writing (the log, a file or a socket)", r"write|flush|fsync|stderr|sendall|\bsend\b"),
    ("waiting on a subprocess", r"communicate|waitpid|\bwait\b|Popen|check_output|subprocess"),
    ("waiting for a lock", r"flock|acquire|\block\b"),
    ("the network", r"recv|accept|\bselect\b|\bpoll\b"),
]
ADVICE = {
    "searching PATH (shutil.which)": "Each search of PATH stats every directory on it. 1.13.15 remembers a runtime it found for 30 s; a long PATH of "
                                     "network directories (modules, conda) is the cost, so a shorter PATH for the server helps.",
    "resolving a path (realpath)": "Every path the server resolves costs an lstat per component. 1.13.15 stops re-resolving paths it already "
                                   "resolved; what is left is the file system's own latency (see the Probe tab).",
    "SQLite (the job registry)": "The job registry (jobs.sqlite) is on a file system where commits and locks are slow. Put it on a faster disk "
                                 "(`jobs_db` in the REST config; node-local /tmp loses the history when the job ends) or fix the file system.",
    "listing a directory": "A directory the server lists is large or the file system is slow to list it: see the Directories tab for the big ones.",
    "opening or reading a file": "Plain opens are slow: the file system's per-call latency is the problem (Probe tab). Files the server reads on "
                                 "every request (tokens, templates) are the first to cache.",
    "stat / exists / access": "Plain stats are slow: the file system's per-call latency is the problem (Probe tab).",
    "writing (the log, a file or a socket)": "A write to the session log or a file blocked: the file system is slow to accept writes (check fsync in the Probe tab).",
    "waiting on a subprocess": "The server was waiting on a command (squeue, sbatch, a quota check): run it by hand and time it.",
    "waiting for a lock": "Something holds a lock the request needs (a token file, a database): another request or process is stuck holding it.",
    "the network": "The request waited on a socket.",
    "other": "The innermost frame wasn't recognised; the stack is in the examples below.",
}


# ------------------------------------------------------------------------------------------------------- parsing

def _local_epoch(text):
    try:
        return time.mktime(time.strptime(text, "%d/%b/%Y %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def _tail_lines(path, max_bytes=TAIL_BYTES):
    """The last `max_bytes` of a text file as lines (the first, partial one dropped); [] when it can't be read."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if size > max_bytes and lines else lines


def parse_audit(lines, since=0):
    """The audit entries that have timing (a server older than 1.13.13 doesn't write it)."""
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and isinstance(e.get("duration_ms"), (int, float)) and (e.get("time") or 0) >= since:
            out.append(e)
    return out


def parse_session_log(lines):
    """(slow request lines, stack dumps) from a session log. A stack dump is the header line and the indented lines
    after it: frames of {file, line, func, source}, outermost first."""
    slow, stacks, i = [], [], 0
    while i < len(lines):
        line = lines[i]
        m = SLOW_LINE.match(line)
        if m:
            slow.append({"time": _local_epoch(m.group("ts")), "verb": m.group("verb"), "path": m.group("path"),
                         "status": int(m.group("status")), "duration_ms": float(m.group("ms")),
                         "cpu_ms": float(m.group("cpu")) if m.group("cpu") else None})
        m = STACK_HEADER.match(line)
        if m:
            frames = []
            i += 1
            while i < len(lines) and lines[i].startswith(" "):
                f = FRAME.match(lines[i])
                if f:
                    frames.append({"file": f.group("file"), "line": int(f.group("line")), "func": f.group("func"), "source": ""})
                elif frames and lines[i].startswith("    "):
                    frames[-1]["source"] = lines[i].strip()
                i += 1
            stacks.append({"time": _local_epoch(m.group("ts")), "seconds": float(m.group("seconds")), "verb": m.group("verb"),
                           "path": m.group("path"), "thread": m.group("thread"), "frames": frames})
            continue
        i += 1
    return slow, stacks


def route_of(path):
    """The path without its query, ids replaced: /jobs/42/cancel -> /jobs/:id/cancel."""
    return re.sub(r"/\d+(?=/|$)", "/:id", path.split("?", 1)[0]) or "/"


def classify(frames):
    """(category, the frame that decided it) for a stack: the innermost frame's function and source, then outward."""
    for f in reversed(frames[-8:]):
        if f["func"] in ("which", "_access_check") or ".which(" in f["source"]:
            return CATEGORIES[0][0], f
    for f in reversed(frames[-6:]):
        text = "{} {}".format(f["func"], f["source"])
        for name, pattern in CATEGORIES:
            if re.search(pattern, text):
                return name, f
    return "other", (frames[-1] if frames else None)


def _where(frame):
    return "{}:{} {}".format(os.path.basename(frame["file"]), frame["line"], frame["func"]) if frame else "?"


def _percentile(sorted_values, p):
    return sorted_values[min(len(sorted_values) - 1, int(round(p * (len(sorted_values) - 1))))] if sorted_values else None


# ----------------------------------------------------------------------------------------------------- analysis

def analyze(audit, slow_lines=(), stacks=(), slow_ms=SLOW_MS):
    """The analysis (see the module's description) from parsed audit entries, slow-request lines and stack dumps.
    Without audit entries the slow-request lines stand in (they are the slow ones only, so shares are of those)."""
    entries = [{"time": e.get("time"), "verb": e.get("verb"), "path": e.get("path") or "", "status": e.get("status"),
                "duration_ms": float(e["duration_ms"]), "cpu_ms": e.get("cpu_ms")} for e in audit]
    from_audit = bool(entries)
    if not entries:
        entries = [dict(s) for s in slow_lines]
    slow = [e for e in entries if e["duration_ms"] >= slow_ms]
    wall = sum(e["duration_ms"] for e in entries)
    timed = [e for e in entries if isinstance(e.get("cpu_ms"), (int, float))]
    cpu = sum(e["cpu_ms"] for e in timed)
    wall_timed = sum(e["duration_ms"] for e in timed)
    out = {"slow_threshold_ms": slow_ms, "from_audit_log": from_audit,
           "requests": {"count": len(entries), "slow": len(slow),
                        "slow_share": round(100.0 * len(slow) / len(entries), 1) if entries else None,
                        "wall_ms_total": round(wall), "cpu_ms_total": round(cpu, 1),
                        "wait_share": round(100.0 * (1 - cpu / wall_timed), 1) if wall_timed > 0 else None}}
    slow_timed = [e for e in slow if isinstance(e.get("cpu_ms"), (int, float))]
    slow_wall = sum(e["duration_ms"] for e in slow_timed)
    out["slow_wait_share"] = round(100.0 * (1 - sum(e["cpu_ms"] for e in slow_timed) / slow_wall), 1) if slow_wall > 0 else None
    by_route = {}
    for e in entries:
        by_route.setdefault((e["verb"], route_of(e["path"])), []).append(e)
    routes = []
    for (verb, route), group in by_route.items():
        durations = sorted(e["duration_ms"] for e in group)
        g_timed = [e for e in group if isinstance(e.get("cpu_ms"), (int, float))]
        g_wall = sum(e["duration_ms"] for e in g_timed)
        routes.append({"verb": verb, "route": route, "count": len(group), "slow": sum(1 for d in durations if d >= slow_ms),
                       "p50_ms": round(_percentile(durations, 0.5), 1), "p95_ms": round(_percentile(durations, 0.95), 1),
                       "max_ms": round(durations[-1], 1), "total_ms": round(sum(durations)),
                       "wait_share": round(100.0 * (1 - sum(e["cpu_ms"] for e in g_timed) / g_wall), 1) if g_wall > 0 else None})
    out["routes"] = sorted(routes, key=lambda r: -r["total_ms"])[:20]
    out["slowest"] = [{"time": e["time"], "verb": e["verb"], "path": e["path"][:200], "status": e["status"],
                       "duration_ms": round(e["duration_ms"], 1), "cpu_ms": e.get("cpu_ms")}
                      for e in sorted(entries, key=lambda e: -e["duration_ms"])[:15]]
    buckets = {}
    for e in entries:
        if e.get("time"):
            buckets.setdefault(int(e["time"] // 60), []).append(e["duration_ms"])
    out["timeline"] = [{"minute": m * 60, "count": len(v), "slow": sum(1 for d in v if d >= slow_ms),
                        "p95_ms": round(_percentile(sorted(v), 0.95), 1), "max_ms": round(max(v), 1)}
                       for m, v in sorted(buckets.items())][-240:]
    categories, frames, per_route, examples = {}, {}, {}, {}
    for s in stacks:
        category, frame = classify(s["frames"])
        categories[category] = categories.get(category, 0) + 1
        key = _where(frame)
        frames[key] = frames.get(key, 0) + 1
        r = (s["verb"], route_of(s["path"]), category)
        per_route[r] = per_route.get(r, 0) + 1
        if category not in examples or s["seconds"] > examples[category]["seconds"]:
            examples[category] = {"category": category, "seconds": s["seconds"], "request": "{} {}".format(s["verb"], s["path"][:160]),
                                  "decided_by": key, "frames": ["{}:{} in {}   {}".format(os.path.basename(f["file"]), f["line"], f["func"], f["source"]).rstrip()
                                                                for f in s["frames"][-9:]]}
    total = len(stacks)
    out["stacks"] = {"count": total,
                     "categories": [{"category": c, "count": n, "share": round(100.0 * n / total, 1), "advice": ADVICE.get(c, "")}
                                    for c, n in sorted(categories.items(), key=lambda kv: -kv[1])],
                     "frames": [{"where": w, "count": n} for w, n in sorted(frames.items(), key=lambda kv: -kv[1])[:12]],
                     "by_route": [{"verb": v, "route": r, "category": c, "count": n}
                                  for (v, r, c), n in sorted(per_route.items(), key=lambda kv: -kv[1])[:15]],
                     "examples": sorted(examples.values(), key=lambda e: -e["seconds"])[:6]}
    times = [t for t in [e.get("time") for e in entries] + [s.get("time") for s in stacks] if t]
    out["window"] = {"first": min(times), "last": max(times)} if times else None
    return out


def load(data_dir, hours=24.0, slow_ms=SLOW_MS):
    """Analyse what the REST server left in `data_dir` (default ~/.local/tunnels): rest/audit.log and the newest
    session logs under sessions/, for the last `hours`. {"analysis", "sources"}: sources says what was read."""
    data_dir = os.path.expanduser(data_dir or "~/.local/tunnels")
    since = time.time() - hours * 3600
    audit_path = os.path.join(data_dir, "rest", "audit.log")
    audit = parse_audit(_tail_lines(audit_path), since) if os.path.isfile(audit_path) else []
    logs = glob.glob(os.path.join(data_dir, "sessions", "*", "session-*.log")) + glob.glob(os.path.join(data_dir, "sessions", "session-*.log"))
    dated = []
    for p in logs:
        try:
            dated.append((os.path.getmtime(p), p))
        except OSError:
            pass
    recent = [p for m, p in sorted(dated, reverse=True) if m >= since][:6]
    slow, stacks = [], []
    for p in recent:
        s, k = parse_session_log(_tail_lines(p, 8 << 20))
        slow += s
        stacks += k
    slow = [s for s in slow if (s["time"] or since) >= since]
    stacks = [k for k in stacks if (k["time"] or since) >= since]
    analysis = analyze(audit, slow, stacks, slow_ms)
    analysis["sources"] = {"audit_log": audit_path if audit else None, "audit_entries_with_timing": len(audit),
                           "session_logs": recent, "slow_request_lines": len(slow), "stack_dumps": len(stacks), "hours": hours}
    return analysis
