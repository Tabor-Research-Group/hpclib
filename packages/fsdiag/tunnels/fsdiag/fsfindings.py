"""
fsfindings: what the probes and the request log say, in plain words.

  findings(results, requests=None) -> [{severity, title, detail, advice}]    problems first

`results` is a list of fsprobe results (one per node probed: the login node, a compute node, another compute
node...), `requests` the analysis from fsrequests (or None). The rules compare each place with the node-local control
in the same result, the same place across nodes (a problem node stands out), and the figures that say *why* a file
system is slow: the NFS server's round trip against the wait in this node's queue, whether latency grows when calls
overlap, SQLite commits, inode use, huge directories, I/O pressure and processes stuck in uninterruptible I/O.

The thresholds are the ones the rules are written around: a metadata call (stat, create, rename) over 20 ms is slow
and over 100 ms is a problem; a local disk or a healthy network file system answers in well under 2 ms.

Python 3.6 or newer, standard library only.
"""
WARN_MS = 20.0
BAD_MS = 100.0
FSYNC_WARN_MS = 50.0
FSYNC_BAD_MS = 250.0
KEY_OPS = ("stat_missing", "stat_existing", "stat_new", "create", "rename", "unlink", "mkdir", "open_read", "open_read_existing",
           "listdir_small")
SEVERITIES = ("problem", "warning", "info", "ok")


def ms(x):
    if x is None:
        return "?"
    return "{:.2f} ms".format(x) if x < 1 else "{:.1f} ms".format(x) if x < 10 else "{:,.0f} ms".format(x)


def _med(path, op):
    s = path.get("ops", {}).get(op)
    return s["median"] if s else None


def _p95(path, op):
    s = path.get("ops", {}).get(op)
    return s["p95"] if s else None


def _name(result):
    return "{} ({})".format(result.get("node") or result.get("host") or "?", "login node" if result.get("where") == "login" else "compute node")


def _finding(severity, title, detail="", advice=""):
    return {"severity": severity, "title": title, "detail": detail, "advice": advice}


def _worst(path):
    """(op, median) of the slowest everyday operation at a place; (None, None) when none was measured."""
    best = (None, None)
    for op in KEY_OPS:
        m = _med(path, op)
        if m is not None and (best[1] is None or m > best[1]):
            best = (op, m)
    return best


def _control(result):
    return next((p for p in result["paths"] if p.get("label") == "control" and p.get("ops")), None)


def latency_findings(results):
    out = []
    for r in results:
        control = _control(r)
        for p in r["paths"]:
            if p.get("label") == "control":
                continue
            for e in p.get("errors", []):
                out.append(_finding("info", "{}: {}".format(p["label"], e), "On {}.".format(_name(r))))
            op, worst = _worst(p)
            if worst is None:
                continue
            ratio = ""
            if control:
                base = _med(control, op) or 0.0
                if worst >= 1 and base is not None:
                    ratio = ", {:,.0f} times the node-local control".format(worst / max(base, 0.01))
            where = "{} on {}".format(p["label"], _name(r))
            if worst >= BAD_MS:
                out.append(_finding("problem", "{}: {} takes {} (median){}".format(where, op.replace("_", " "), ms(worst), ratio),
                                    "Every request that touches {} pays this on each file call; a web request makes dozens.".format(p["path"]),
                                    "Check the other nodes (a specific node may be the problem), the NFS timings below, and ask the cluster's admins "
                                    "whether this file system is under load."))
            elif worst >= WARN_MS:
                out.append(_finding("warning", "{}: {} takes {} (median){}".format(where, op.replace("_", " "), ms(worst), ratio),
                                    "Slow enough to add up over the dozens of file calls one request makes."))
            spike = max([v for v in (_p95(p, o) for o in KEY_OPS) if v is not None] or [0])
            if worst < WARN_MS and spike >= 10 * BAD_MS:
                out.append(_finding("warning", "{}: usually fast, but some calls took {}".format(where, ms(spike)),
                                    "The 95th percentile is far above the median: stalls come and go (another user's load, a server hiccup)."))
            sync = _med(p, "fsync")
            if sync is not None and sync >= FSYNC_WARN_MS and not p.get("read_only"):
                out.append(_finding("problem" if sync >= FSYNC_BAD_MS else "warning",
                                    "{}: flushing a file (fsync) takes {}".format(where, ms(sync)),
                                    "Every safe save, token write and SQLite commit waits for this.",
                                    "Keep files that are written often (jobs.sqlite, logs) on node-local disk or scratch, not here."))
    return out


def node_findings(results):
    """The same place on several nodes: is one node the odd one out? and: are compute nodes slower than the login node?"""
    out, by_place = [], {}
    for r in results:
        for p in r["paths"]:
            if p.get("label") == "control":
                continue
            _, worst = _worst(p)
            if worst is not None:
                by_place.setdefault(p["label"], []).append((r, worst))
    for label, rows in by_place.items():
        if len(rows) < 2:
            continue
        slowest = max(rows, key=lambda x: x[1])
        fastest = min(rows, key=lambda x: x[1])
        if slowest[1] >= 10 and slowest[1] >= 5 * max(fastest[1], 0.5):
            out.append(_finding("problem" if slowest[1] >= BAD_MS else "warning",
                                "{}: {} is {:,.0f} times slower than {}".format(label, _name(slowest[0]), slowest[1] / max(fastest[1], 0.01),
                                                                                  _name(fastest[0])),
                                "{} vs {} (the slowest everyday call at each).".format(ms(slowest[1]), ms(fastest[1])),
                                "The file system isn't uniformly slow: look at this node's mount (below), its load and its network path to the "
                                "file server, and tell the admins the node's name. Running your tunnel on another node avoids it."))
    logins = [r for r in results if r.get("where") == "login"]
    computes = [r for r in results if r.get("where") == "compute"]
    if logins and computes and not any(f["title"].startswith(tuple(by_place)) and "times slower" in f["title"] for f in out):
        for label, rows in by_place.items():
            l = [w for r, w in rows if r.get("where") == "login"]
            c = [w for r, w in rows if r.get("where") == "compute"]
            if l and c and max(c) >= 20 and max(c) >= 3 * max(l):
                out.append(_finding("warning", "{}: compute nodes are slower than the login node".format(label),
                                    "{} on the compute node(s) against {} on the login node.".format(ms(max(c)), ms(max(l))),
                                    "A tunnel's REST server runs on a compute node, so it feels the slower figure."))
    return out


def concurrency_findings(results):
    out = []
    for r in results:
        for p in r["paths"]:
            if p.get("label") == "control":
                continue
            solo = _med(p, "create")
            conc = next((_med(p, op) for op in p.get("ops", {}) if op.startswith("create_x")), None)
            if solo is not None and conc is not None and conc >= 20 and conc >= 3 * max(solo, 0.5):
                threads = (p.get("concurrent") or {}).get("threads")
                out.append(_finding("warning", "{} on {}: latency grows when calls overlap ({} alone, {} with {} at once)".format(
                    p["label"], _name(r), ms(solo), ms(conc), threads or "several"),
                    "The REST server answers several requests at once, so each request waits longer when others are running.",
                    "Fewer simultaneous requests (a page that polls, several agents) helps; so does fewer file calls per request."))
    return out


def nfs_findings(results):
    out = []
    for r in results:
        for p in r["paths"]:
            n = p.get("nfs")
            if not n or n["operations"] < 20:
                continue
            where = "{} on {}".format(p["label"], _name(r))
            if n["avg_rtt_ms"] >= 20:
                out.append(_finding("problem" if n["avg_rtt_ms"] >= 100 else "warning",
                                    "{}: the NFS server takes {} to answer each call".format(where, ms(n["avg_rtt_ms"])),
                                    "{} calls during the probe (from /proc/self/mountstats); the average round trip is the time between "
                                    "sending a request and getting its reply, so this is the server and the network, not this node.".format(n["operations"]),
                                    "The file server is overloaded or the path to it is congested. Nothing in hpclib can fix that: tell the admins "
                                    "the time and the node, and use fewer file calls (scratch or node-local disk) in the meantime."))
            elif n["avg_queue_ms"] >= 20:
                out.append(_finding("warning", "{}: calls wait {} in this node's queue before they are sent".format(where, ms(n["avg_queue_ms"])),
                                    "The server answers quickly ({}), but this node's connection to it has too many requests in flight.".format(ms(n["avg_rtt_ms"])),
                                    "Something on this node is flooding the mount (many processes doing file calls at once). Look at what else runs here."))
            retrans = sum(o.get("retransmissions", 0) for o in n["ops"].values())
            if retrans >= 5:
                out.append(_finding("warning", "{}: {} NFS requests had to be sent again".format(where, retrans),
                                    "Lost or timed-out requests: the network between this node and the file server is dropping packets.",
                                    "Tell the cluster's admins; this is usually a network or server fault."))
    return out


def sqlite_findings(results):
    out = []
    for r in results:
        for p in r["paths"]:
            s = p.get("sqlite")
            if not s:
                continue
            where = "{} on {}".format(p["label"], _name(r))
            if not s.get("ok"):
                out.append(_finding("problem", "{}: SQLite doesn't work here ({})".format(where, s.get("error")),
                                    "The job registry (jobs.sqlite) would fail or hang in this directory.",
                                    "Point `jobs_db` in the REST config at a local or scratch directory."))
                continue
            m = _med(p, "sqlite_commit")
            if m is not None and m >= 50:
                out.append(_finding("problem" if m >= 250 else "warning", "{}: a SQLite commit takes {}".format(where, ms(m)),
                                    "The job registry commits on every submit and state change, and holds a lock while it does.",
                                    "Move `jobs_db` (REST config) to scratch or node-local disk if its history can be rebuilt or lost."))
    return out


def storage_findings(results):
    out, seen = [], set()
    for r in results:
        for p in r["paths"]:
            v = p.get("statvfs")
            key = (p.get("mount") or {}).get("source"), p["label"]
            if not v or key in seen:
                continue
            seen.add(key)
            if v["inodes_used_percent"] >= 90:
                out.append(_finding("problem", "{}: {}% of the file system's inodes are in use".format(p["label"], v["inodes_used_percent"]),
                                    "This is the whole file system, not your quota; a nearly full inode table slows creating files.",
                                    "Ask which of your directories hold the most files (Directories tab) and archive or delete what you can."))
            elif v["inodes_used_percent"] >= 75:
                out.append(_finding("info", "{}: {}% of the file system's inodes are in use".format(p["label"], v["inodes_used_percent"])))
    return out


def survey_findings(results):
    out, seen = [], set()
    for r in results:
        for s in r.get("surveys", []):
            if s["root"] in seen:
                continue
            seen.add(s["root"])
            if s.get("error"):
                out.append(_finding("info", "{}: {}".format(s["root"], s["error"])))
                continue
            big = [d for d in s.get("largest_directories", []) if d["entries"] >= 10000]
            for d in big[:3]:
                out.append(_finding("warning", "{} holds {:,} entries (listing takes {})".format(d["path"], d["entries"], ms(d["list_ms"])),
                                    "A directory this large makes every listing, glob and `ls` slow, and lookups in it on some file systems.",
                                    "Spread the files over subdirectories (by date or job id), or pack finished runs into archives."))
            slow = [d for d in s.get("slow_listings", []) if d["list_ms"] >= 250 and d["entries"] < 10000]
            if slow:
                out.append(_finding("warning", "{} directories took over 250 ms to list although they are small".format(len(slow)),
                                    "e.g. {} ({} entries, {}). Small directories that list slowly point at the file system, not at file counts.".format(
                                        slow[0]["path"], slow[0]["entries"], ms(slow[0]["list_ms"]))))
            if s.get("entries_per_second") is not None and s["entries"] >= 5000 and s["entries_per_second"] < 2000:
                out.append(_finding("warning", "{}: scanning ran at {:,} entries a second".format(s["root"], s["entries_per_second"]),
                                    "A local disk scans hundreds of thousands a second; this is the file system's metadata speed again."))
            if s.get("truncated"):
                out.append(_finding("info", "{}: the survey stopped at {:,} entries or its time limit".format(s["root"], s["entries"]),
                                    "The counts are lower bounds; the children marked partial hold more."))
            huge = [c for c in s.get("children", []) if c["entries"] >= 200000]
            if huge:
                out.append(_finding("info", "{}: {:,} entries under {}".format(s["root"], huge[0]["entries"], huge[0]["name"]),
                                    "Most of the file count is in one place; that is where cleaning up pays most."))
    return out


def context_findings(results):
    out = []
    for r in results:
        c = r.get("context") or {}
        name = _name(r)
        some = ((c.get("io_pressure") or {}).get("some") or {}).get("avg10")
        if some is not None and some >= 10:
            out.append(_finding("warning", "{}: tasks spent {}% of the last 10 s waiting for I/O".format(name, some),
                                "From /proc/pressure/io: the node itself is stalled on I/O while the probe ran."))
        d = c.get("d_state_processes")
        if d is not None and d >= 8:
            out.append(_finding("warning", "{}: {} processes are stuck waiting for I/O".format(name, d),
                                "Processes in D state: {}. A stalled file system looks like this from the node.".format(", ".join(c.get("d_state_names") or []) or "?")))
        wait = c.get("iowait_percent_during_probe")
        if wait is not None and wait >= 20:
            out.append(_finding("info", "{}: the CPUs were idle waiting on I/O {}% of the time during the probe".format(name, wait)))
        load, cpus = (c.get("loadavg") or [None])[0], c.get("cpus")
        if load and cpus and load > 2 * cpus:
            out.append(_finding("info", "{}: load average {} on {} CPUs".format(name, load, cpus),
                                "A busy node (login nodes are shared) makes everything on it slower, file system or not."))
        for p in r["paths"]:
            if p.get("truncated") and p.get("label") != "control":
                out.append(_finding("info", "{}: the probe of {} stopped early".format(name, p["label"]),
                                    "It ran out of time or a call took over 10 s: itself a sign of how slow this place is."))
    return out


def request_findings(requests):
    if not requests:
        return []
    out, r = [], requests["requests"]
    if not r["count"]:
        return [_finding("info", "No request timing was found",
                         "The REST server writes duration_ms and cpu_ms to its audit log from hpclib 1.13.13 on, and stack dumps from 1.13.14.",
                         "Update hpclib on the cluster (Update hpclib), restart the tunnel, use it for a while and look again.")]
    if not r["slow"]:
        return [_finding("ok", "No REST request took over {:g} s in this window ({:,} requests)".format(requests["slow_threshold_ms"] / 1000.0, r["count"]))]
    share = r["slow_share"]
    wait = requests.get("slow_wait_share")
    top = requests["routes"][0] if requests["routes"] else None
    out.append(_finding("problem" if (share or 0) >= 20 else "warning",
                        "{:,} of {:,} REST requests took over {:g} s ({}%)".format(r["slow"], r["count"], requests["slow_threshold_ms"] / 1000.0, share),
                        ("The slow ones spent {}% of their time waiting, not computing. ".format(wait) if wait is not None else "") +
                        ("The route that cost the most total time: {} {} (median {}, 95th percentile {}).".format(
                            top["verb"], top["route"], ms(top["p50_ms"]), ms(top["p95_ms"])) if top else ""),
                        "A request that waits instead of computing is stuck in a file system, lock or subprocess: the categories below say which."))
    st = requests["stacks"]
    if st["count"]:
        c = st["categories"][0]
        out.append(_finding("warning" if c["share"] >= 30 else "info",
                            "{} of {} stack dumps were {}".format(c["count"], st["count"], c["category"]),
                            "Stack dumps are taken once for each request still running after 5 s; the call it is stuck in "
                            "(most common: {}).".format(st["frames"][0]["where"] if st["frames"] else "?"),
                            c["advice"]))
        for other in st["categories"][1:3]:
            if other["share"] >= 20:
                out.append(_finding("info", "{} more were {}".format(other["count"], other["category"]), "", other["advice"]))
    elif r["slow"]:
        out.append(_finding("info", "No stack dumps were found for the slow requests",
                            "They need hpclib 1.13.14 or newer on the cluster, and a request has to run over 5 s to get one."))
    return out


def findings(results, requests=None):
    """Every finding for these probe results and request analysis, problems first (the order within a kind is the order the rules ran in)."""
    out = []
    if results:
        for rule in (latency_findings, node_findings, concurrency_findings, nfs_findings, sqlite_findings, storage_findings,
                     survey_findings, context_findings):
            out += rule(results)
        if not any(f["severity"] in ("problem", "warning") for f in out):
            names = ", ".join(sorted(set(_name(r) for r in results)))
            out.append(_finding("ok", "No slow file system calls were seen in the places probed",
                                "Probed on {}. If your requests are still slow, run it again while they are (the problem may come and go), "
                                "and look at the Slow requests tab.".format(names)))
    out += request_findings(requests)
    return sorted(out, key=lambda f: SEVERITIES.index(f["severity"]))


def report_text(title, results, found, requests=None):
    """A plain-text (Markdown-ish) report of a run: the findings, then each node's table and the request analysis. It is
    what to attach to a message to the cluster's admins: every number is in it, and nothing else of yours."""
    import time
    import fsprobe
    marks = {"problem": "PROBLEM", "warning": "warning", "info": "note", "ok": "ok"}
    lines = ["# {}".format(title), "", "Made {} by hpclib's fsdiag.".format(time.strftime("%Y-%m-%d %H:%M:%S")), "", "## Findings", ""]
    for f in found:
        lines.append("- [{}] {}".format(marks[f["severity"]], f["title"]))
        for key in ("detail", "advice"):
            if f.get(key):
                lines.append("    {}".format(f[key]))
    for r in results:
        lines += ["", "## {}".format(_name(r)), "", "```", fsprobe.render_text(r), "```"]
    if requests and requests["requests"]["count"]:
        q = requests["requests"]
        lines += ["", "## REST requests", "",
                  "{:,} requests, {:,} over {:g} s; wait share of the slow ones: {}%".format(
                      q["count"], q["slow"], requests["slow_threshold_ms"] / 1000.0, requests.get("slow_wait_share")), ""]
        for route in requests["routes"][:10]:
            lines.append("- {} {}: {} requests, median {}, 95th percentile {}, worst {}".format(
                route["verb"], route["route"], route["count"], ms(route["p50_ms"]), ms(route["p95_ms"]), ms(route["max_ms"])))
        for c in requests["stacks"]["categories"]:
            lines.append("- blocked in {}: {} stack dumps ({}%)".format(c["category"], c["count"], c["share"]))
    return "\n".join(lines) + "\n"
