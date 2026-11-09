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


# What a job asked sbatch for, by the option's name: Slurm keeps --gpus in TresPerJob, --gres and --gpus-per-node in
# TresPerNode, and so on; the job's constraint is its features. Values are shown the way Slurm prints them, without
# the `gres/` prefix: `gpu:a100:2`.
REQUEST_KEYS = ("gpus", "gres", "gpus-per-socket", "gpus-per-task", "constraint")
# the options a filter may name, and the request each one is kept under
REQUEST_OPTIONS = {"gpus": "gpus", "gres": "gres", "gpus-per-node": "gres", "gpus-per-socket": "gpus-per-socket",
                   "gpus-per-task": "gpus-per-task", "constraint": "constraint", "c": "constraint"}


def normalise_tres(text):
    """A TRES or GRES request as `gpu:a100:2,shard:4`: squeue's `gres/gpu:a100=2`, `gres:gpu:a100:2` and `gpu:2` alike."""
    items = []
    for item in re.split(r"[,;]", text or ""):
        item = item.strip().replace("=", ":")
        item = re.sub(r"^gres[/:]", "", item, flags=re.I)
        if item and item.lower() not in ("n/a", "(null)", "null"):
            items.append(item)
    return ",".join(items)


def _gres_item(item):
    """`gpu:a100:2` -> (name, type or None, count or None)."""
    parts = item.lower().split(":")
    count = int(parts.pop()) if len(parts) > 1 and parts[-1].isdigit() else None
    return parts[0], ":".join(parts[1:]) or None, count


def resource_term(term):
    """A filter on one request, as a function of a job's `requests` ({option: value}): True when it asked for that.
    `gpus=1:a100`, `--gres=gpu:a100:2`, `gpus-per-node=a100`: the option as passed to sbatch, only that one is looked
    at (`--gpus-per-node` is what --gres is kept as). `gpu:a100:2`, `a100`, `gpu` and `2:a100` without an option look at
    every GPU request, and a job's constraint for the type. `constraint=a100` (or `-C a100`) looks at the features.
    A type matches as part of the job's (`a100` matches `a100-80gb`); a count is exact; the type and count may come in
    either order. Anything else (`shard:4`) matches as text in the requests."""
    text = term.strip().lstrip("-").strip()
    option, value = None, text.lower()
    if "=" in text:
        name, _, value = text.partition("=")
        option, value = name.strip().lower(), value.strip().lower()
        if option not in REQUEST_OPTIONS:                       # not an option we know: just text
            return lambda requests: text.lower() in " ".join(f"{k}={v}" for k, v in requests.items()).lower()
    keys = [REQUEST_OPTIONS[option]] if option else list(REQUEST_KEYS)
    if keys == ["constraint"]:
        return lambda requests: bool(value) and value in (requests.get("constraint") or "").lower()
    tokens = [t for t in value.split(":") if t]
    name = "gpu"
    if tokens and (option == "gres" or tokens[0] == "gpu") and not tokens[0].isdigit():
        name = tokens.pop(0)
    counts = [t for t in tokens if t.isdigit()]
    kinds = [t for t in tokens if not t.isdigit()]
    count, kind = (int(counts[-1]) if counts else None), (":".join(kinds) or None)

    def gres_match(requests):
        for key in keys:
            if key == "constraint":
                continue
            for item in (requests.get(key) or "").split(","):
                if not item:
                    continue
                item_name, item_kind, item_count = _gres_item(item)
                if item_name != name or (kind and kind not in (item_kind or "")):
                    continue
                if count is None or count == (item_count if item_count is not None else 1):
                    return True
        return False

    def term_match(requests):
        if gres_match(requests):
            return True
        if option:
            return False
        if kind and count is None and kind in (requests.get("constraint") or "").lower():     # `a100`: a GPU type asked for as a feature
            return True
        return text.lower() in " ".join(f"{k}={v}" for k, v in requests.items()).lower()      # `shard:4`, any other text
    return term_match


def resource_filter(text):
    """Every request in `text` (separated by spaces or ;) must match: resource_term for each. None if there are none."""
    terms = [t for t in re.split(r"[\s;]+", text or "") if t.strip("-")]
    checks = [resource_term(t) for t in terms]
    if not checks:
        return None
    return lambda job: all(check(job.get("requests") or {}) for check in checks)


def parse_cluster_queue(text, limit=QUEUE_LIST_LIMIT, queued_states=QUEUED_JOB_STATES, source_of=None):
    """CLUSTER_QUEUE_SCRIPT's lines: the queue of everyone but you, as {me, partitions, users, reasons, queued,
    running, counts}: your own jobs are left out (the Jobs page has them), but the partitions' CPUs count everything.
    source_of(name, comment) says what started a job ({kind, label, ...}); jobs hpclib started are counted per user."""
    me, partitions, jobs, gpu_rows, asked = "", {}, [], [], {}
    for line in text.splitlines():
        kind, _, rest = line.partition("|")
        if kind == "G":
            f = rest.split("|")
            if len(f) == 5:
                gpu_rows.append(f)
        elif kind == "R":                      # R|ID|TresPerJob|TresPerNode|TresPerSocket|TresPerTask|Features
            f = rest.split("|", 5)                # the features come last: a constraint such as a100|v100 holds a |
            if len(f) == 6:
                request = {key: normalise_tres(value) if key != "constraint" else value.strip()
                           for key, value in zip(REQUEST_KEYS, f[1:]) if value.strip() and value.strip() not in ("N/A", "(null)")}
                if request:
                    asked[f[0]] = {k: v for k, v in request.items() if v}
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
    for job in jobs:
        job["requests"] = asked.get(job["id"]) or {}
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
# per partition (sinfo), `G|NODE|PARTITION|STATE|GRES|GRES USED` per node with GPUs (sinfo; nothing where it can't say), `E|MESSAGE` if squeue failed (a busy or rate-limiting Slurm controller says so there) and `R|ID|PER JOB|PER NODE|PER SOCKET|PER TASK|FEATURES` per job that asked for GRES/TRES or a constraint (a second squeue, whose -O fields older Slurms lack: no R lines then), and `Q|...` per queued or running job (squeue; the comment, which hpclib marks its jobs with, and
# the job's name last, since a name may hold a `|`).
CLUSTER_QUEUE_SCRIPT = r"""
echo "U|$(id -un)"
sinfo -h -o 'P|%R|%C|%D' 2>/dev/null
sinfo -h -N -O 'NodeHost:48,Partition:48,StateLong:24,Gres:100,GresUsed:120' 2>/dev/null |
  awk 'NF >= 5 && $4 ~ /gpu/ { sub(/\*$/, "", $2); print "G|" $1 "|" $2 "|" $3 "|" $4 "|" $5 }'
squeue -h -a -O 'JobID:24,tres-per-job:80,tres-per-node:80,tres-per-socket:50,tres-per-task:50,Features:80' 2>/dev/null |
  awk 'BEGIN { split("24 80 80 50 50 80", w, " ") }
       $1 !~ /^[0-9]/ { next }
       { line = "R"; pos = 1; any = 0
         for (i = 1; i <= 6; i++) { v = substr($0, pos, w[i]); pos += w[i]; gsub(/^ +| +$/, "", v)
                                    if (v == "N/A" || v == "(null)") v = ""
                                    if (i > 1 && v != "") any = 1
                                    line = line "|" v }
         if (any) print line }'
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
    text = "\n".join(line for line in out.splitlines() if line[:2] in ("U|", "P|", "Q|", "G|", "R|"))
    queue = parse_cluster_queue(text, source_of=source_of)
    if failed is None:                # squeue said nothing was wrong: check that what it printed was read
        said = [line for line in out.splitlines() if line.strip()]
        odd = [line for line in said if line[:2] not in ("U|", "P|", "Q|", "G|", "R|")]
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


def queue_report(answer, show="none", user=None, partition=None, name=None, hpclib=False, limit=50, resources=None):
    """An answer of the queue for an agent: the summary (counts, partitions, GPUs, busiest users, why jobs wait),
    and with show = running, queued or all, those jobs that match user (exact), partition (exact), name (part of
    the job's name, any case), hpclib (only jobs hpclib started) and resources (what the job asked sbatch for, such as
    `gpus=1:a100` or `--gres=gpu:a100:2`: see resource_term): the first `limit` of each, with how many match."""
    out = {key: answer.get(key) for key in ("cluster", "at", "me", "counts", "partitions", "gpus", "users", "reasons", "error", "stale")
           if key in answer}
    if show == "none":
        return out
    low = (name or "").lower()
    asked = resource_filter(resources)
    keep = lambda j: ((not user or j["user"].lower() == user.lower()) and (not partition or j["partition"] == partition)
                      and (not low or low in j["name"].lower()) and (not hpclib or j["source"]["kind"] != "other")
                      and (asked is None or asked(j)))
    jobs = {}
    for kind in ("running", "queued"):
        if show in (kind, "all"):
            found = [j for j in answer.get(kind, []) if keep(j)]
            jobs[kind] = found[:limit]
            jobs[kind + "_matching"] = len(found)
    out["jobs"] = jobs
    return out
