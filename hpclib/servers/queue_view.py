"""
The queue of a Slurm cluster, everyone's jobs: how busy each partition is, which GPUs are free, who is using the
cluster, why jobs wait, and the queued and running jobs. CLUSTER_QUEUE_SCRIPT runs on a login (or compute) node and
prints marked lines; parse_cluster_queue turns them into an answer; read_queue runs and checks it. Shared by the
console (over its login, for the Jobs tab) and the REST server (GET /queue, for agents).
"""
import re
import time


QUEUE_LIST_LIMIT = 5000     # the queued and the running jobs of other users sent to the page, which filters them all (all are counted)


QUEUED_JOB_STATES = frozenset({"PENDING", "REQUEUED", "SUSPENDED", "REQUEUE_HOLD", "RESV_DEL_HOLD", "SPECIAL_EXIT"})


GPU_NODE_UNAVAILABLE = ("down", "drain", "drng", "fail", "maint", "inval", "unk", "resv", "reserved", "future", "futr", "power")


def gpu_counts(text):
    """{type: count} from a node's Gres or GresUsed, such as `gpu:a100:4(S:0-1),shard:8`, `gpu:2(IDX:0-1)` or `(null)`;
    GPUs with no type are filed under "gpu"."""
    counts = {}
    for item in re.sub(r"\([^)]*\)", "", text).split(","):
        parts = item.strip().split(":")
        if parts[0] != "gpu":
            continue
        if len(parts) == 1:
            kind, number = "gpu", "1"
        elif len(parts) == 2:
            kind, number = ("gpu", parts[1]) if parts[1].isdigit() else (parts[1], "1")
        else:
            kind, number = parts[1], parts[2]
        if number.isdigit():
            counts[kind] = counts.get(kind, 0) + int(number)
    return counts


def summarise_gpus(rows):
    """Nodes' `(node, partition, state, gres, gres_used)` as [{partition, types: [{type, total, used, free, unavailable,
    nodes, nodes_free, max_free}]}]: free GPUs are on nodes that take jobs (not down, drained, reserved...),
    `unavailable` those on nodes that don't, `max_free` the most free on one node (what a single job can have)."""
    seen, parts = set(), {}
    for node, partition, state, gres, used in rows:
        if (node, partition) in seen:
            continue
        seen.add((node, partition))
        total, taken = gpu_counts(gres), gpu_counts(used)
        if not total:
            continue
        if len(total) == 1 and taken and set(taken) != set(total):           # used GPUs named differently from the total's
            taken = {next(iter(total)): sum(taken.values())}
        bad = any(word in state.lower() for word in GPU_NODE_UNAVAILABLE)
        for kind, n in total.items():
            t = parts.setdefault(partition, {}).setdefault(kind, {"type": kind, "total": 0, "used": 0, "free": 0, "unavailable": 0,
                                                                  "nodes": 0, "nodes_free": 0, "max_free": 0})
            use = min(taken.get(kind, 0), n)
            t["total"] += n
            t["used"] += use
            t["nodes"] += 1
            t["unavailable" if bad else "free"] += n - use
            if not bad and n > use:
                t["nodes_free"] += 1
                t["max_free"] = max(t["max_free"], n - use)
    return [{"partition": name, "types": sorted(kinds.values(), key=lambda t: t["type"])} for name, kinds in sorted(parts.items())]


def parse_cluster_queue(text, limit=QUEUE_LIST_LIMIT, queued_states=QUEUED_JOB_STATES, source_of=None):
    """CLUSTER_QUEUE_SCRIPT's lines: the queue of everyone but you, as {me, partitions, users, reasons, queued,
    running, counts}: your own jobs are left out (the Jobs page has them), but the partitions' CPUs count everything.
    source_of(name, comment) says what started a job ({kind, label, ...}); jobs hpclib started are counted per user."""
    me, partitions, jobs, gpu_rows = "", {}, [], []
    for line in text.splitlines():
        kind, _, rest = line.partition("|")
        if kind == "G":
            f = rest.split("|")
            if len(f) == 5:
                gpu_rows.append(f)
        elif kind == "U":
            me = rest.strip()
        elif kind == "P":
            name, _, rest = rest.partition("|")
            cpus, _, nodes = rest.partition("|")
            parts = cpus.split("/")
            if len(parts) == 4 and all(p.isdigit() for p in parts):
                partitions[name] = {"name": name, "cpus": dict(zip(("allocated", "idle", "other", "total"), map(int, parts))),
                                    "nodes": int(nodes) if nodes.strip().isdigit() else None}
        elif kind == "Q":
            f = rest.split("|", 15)
            if len(f) == 16:
                jobs.append({"id": f[0], "user": f[1], "state": f[2], "reason": f[3], "partition": f[4], "elapsed": f[5],
                             "time_limit": f[6], "nodes": f[7], "cpus": f[8], "memory": f[9], "node_list": f[10],
                             "start": "" if f[11] in ("N/A", "Unknown") else f[11], "submitted": f[12],
                             "priority": f[13], "name": f[15],
                             "source": source_of(f[15], f[14]) if source_of else {"kind": "other", "label": "other"}})
    cpu_of = lambda job: int(job["cpus"]) if job["cpus"].isdigit() else 0
    users, reasons, queued, running, per_part = {}, {}, [], [], {}
    for job in jobs:
        waiting = job["state"] in queued_states
        p = per_part.setdefault(job["partition"], {"running": 0, "queued": 0, "running_cpus": 0, "queued_cpus": 0})
        p["queued" if waiting else "running"] += 1
        p["queued_cpus" if waiting else "running_cpus"] += cpu_of(job)
        if job["user"] == me:
            continue
        u = users.setdefault(job["user"], {"user": job["user"], "running": 0, "queued": 0, "running_cpus": 0,
                                           "queued_cpus": 0, "hpclib": 0})
        u["hpclib"] += job["source"]["kind"] != "other"
        u["queued" if waiting else "running"] += 1
        u["queued_cpus" if waiting else "running_cpus"] += cpu_of(job)
        if waiting:
            reasons[job["reason"]] = reasons.get(job["reason"], 0) + 1
            queued.append(job)
        else:
            running.append(job)
    for name, p in per_part.items():                  # a partition sinfo didn't list still shows what is in it
        partitions.setdefault(name, {"name": name, "cpus": None, "nodes": None}).update(p)
    for part in partitions.values():
        part.setdefault("running", 0), part.setdefault("queued", 0)
        part.setdefault("running_cpus", 0), part.setdefault("queued_cpus", 0)
    busiest = sorted(users.values(), key=lambda u: (-(u["running_cpus"] + u["queued_cpus"]), u["user"]))
    return {"me": me, "partitions": sorted(partitions.values(), key=lambda p: p["name"]), "users": busiest[:25],
            "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "queued": queued[:limit], "running": running[:limit],
            "read": len(jobs), "gpus": summarise_gpus(gpu_rows),
            "counts": {"queued": len(queued), "running": len(running), "users": len(users),
                       "queued_cpus": sum(cpu_of(j) for j in queued), "running_cpus": sum(cpu_of(j) for j in running),
                       "hpclib": sum(u["hpclib"] for u in users.values())}}


# Runs ON the login node: the whole queue, every user's: `U|ME`, then `P|PARTITION|ALLOCATED/IDLE/OTHER/TOTAL CPUS|NODES`
# per partition (sinfo), `G|NODE|PARTITION|STATE|GRES|GRES USED` per node with GPUs (sinfo; nothing where it can't say), `E|MESSAGE` if squeue failed (a busy or rate-limiting Slurm controller says so there) and `Q|...` per queued or running job (squeue; the comment, which hpclib marks its jobs with, and
# the job's name last, since a name may hold a `|`).
CLUSTER_QUEUE_SCRIPT = r"""
echo "U|$(id -un)"
sinfo -h -o 'P|%R|%C|%D' 2>/dev/null
sinfo -h -N -O 'NodeHost:48,Partition:48,StateLong:24,Gres:100,GresUsed:120' 2>/dev/null |
  awk 'NF >= 5 && $4 ~ /gpu/ { sub(/\*$/, "", $2); print "G|" $1 "|" $2 "|" $3 "|" $4 "|" $5 }'
fmt='Q|%i|%u|%T|%r|%P|%M|%l|%D|%C|%m|%N|%S|%V|%Q'
jobs=$(squeue -h -a -o "$fmt|%k|%j" 2>&1); rc=$?
[ "$rc" -eq 0 ] || { jobs=$(squeue -h -a -o "$fmt||%j" 2>&1); rc=$?; }    # a squeue without the comment field still lists jobs
if [ "$rc" -ne 0 ]; then printf 'E|%s\n' "$(printf '%s\n' "$jobs" | head -n 1)"
else printf '%s\n' "$jobs" | awk '/^Q\|/ { if (line != "") print line; line = $0; next } { line = line " " $0 } END { if (line != "") print line }'    # a comment can run over several lines (a job_submit plugin's): one job, one line
fi
"""


def read_queue(run, source_of=None, retry_wait=3, sleep=time.sleep):
    """run() -> (exit code, stdout, stderr) of CLUSTER_QUEUE_SCRIPT, tried once more after retry_wait seconds when
    squeue failed. Returns (parse_cluster_queue's answer, why it failed or None): a failed read is never an empty queue."""
    for attempt in (1, 2):
        code, out, err = run()
        failed = next((line[2:].strip() for line in out.splitlines() if line.startswith("E|")), None)
        if failed is None and code != 0 and "Q|" not in out and "P|" not in out:
            failed = (err or out).strip().splitlines()[-1] if (err or out).strip() else f"exit {code}"
        if failed is None:
            break
        if attempt == 1:
            sleep(retry_wait)
    text = "\n".join(line for line in out.splitlines() if line[:2] in ("U|", "P|", "Q|", "G|"))
    queue = parse_cluster_queue(text, source_of=source_of)
    if failed is None:                # squeue said nothing was wrong: check that what it printed was read
        said = [line for line in out.splitlines() if line.strip()]
        odd = [line for line in said if line[:2] not in ("U|", "P|", "Q|", "G|")]
        lines = [line for line in said if line.startswith("Q|")]
        if lines and not queue["read"]:
            failed = f"squeue printed {len(lines)} lines the console couldn't read, such as {lines[0][:160]!r}"
        elif odd and not lines:
            failed = f"squeue printed something else: {odd[0][:200]}"
    return queue, failed


def failed_queue_answer(answer, queue, reason):
    """The answer when squeue failed and there is no earlier one: the partitions sinfo gave, and no jobs or counts."""
    no_jobs = [dict(p, running=None, queued=None, running_cpus=None, queued_cpus=None) for p in queue["partitions"]]
    return dict(answer, error=reason, partitions=no_jobs, queued=[], running=[], users=[], reasons={},
                counts=dict(queue["counts"], queued=None, running=None, users=None, queued_cpus=None,
                            running_cpus=None, hpclib=None))


def hpclib_source(name, comment):
    """What started a job, as far as a server that doesn't know the console's apps can tell: {kind, label}."""
    m = re.match(r"hpclib-rest:([^:]+):(.+)$", comment or "")
    if m or (name or "").startswith("hpcrest-"):
        return {"kind": "agent", "label": f"agent: {m.group(2) if m else name[len('hpcrest-'):]}"}
    if (name or "").startswith("rest-"):
        return {"kind": "agent-tunnel", "label": "agent tunnel"}
    return {"kind": "other", "label": "other"}


def queue_report(answer, show="none", user=None, partition=None, name=None, hpclib=False, limit=50):
    """An answer of the queue for an agent: the summary (counts, partitions, GPUs, busiest users, why jobs wait),
    and with show = running, queued or all, those jobs that match user (exact), partition (exact), name (part of
    the job's name, any case) and hpclib (only jobs hpclib started): the first `limit` of each, with how many match."""
    out = {key: answer.get(key) for key in ("cluster", "at", "me", "counts", "partitions", "gpus", "users", "reasons", "error", "stale")
           if key in answer}
    if show == "none":
        return out
    low = (name or "").lower()
    keep = lambda j: ((not user or j["user"].lower() == user.lower()) and (not partition or j["partition"] == partition)
                      and (not low or low in j["name"].lower()) and (not hpclib or j["source"]["kind"] != "other"))
    jobs = {}
    for kind in ("running", "queued"):
        if show in (kind, "all"):
            found = [j for j in answer.get(kind, []) if keep(j)]
            jobs[kind] = found[:limit]
            jobs[kind + "_matching"] = len(found)
    out["jobs"] = jobs
    return out
