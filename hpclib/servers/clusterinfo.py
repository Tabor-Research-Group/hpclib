"""
What a cluster says about itself, parsed: the partitions and their limits, the GPU types, the accounts a user may
charge, and what `sbatch --test-only` answers. The Workspace's job form validates against it before sbatch does. The
module runs nothing itself: `collect(run, user)` is given a function that runs a command (the Workspace passes its
login-shell runner, where sites put Slurm on the PATH), so the parsing is testable with canned output.

  parse_time("1d 2h") -> minutes        format_time(minutes) -> "1-02:00:00"        (Slurm's own forms too)
  parse_mem("8G") -> MB                 format_mem(mb) -> "8G"
  parse_sinfo(text), parse_gres(text), parse_assoc(text), parse_test_only(text)
  collect(run, user) -> {slurm_version, slurm_problem, partitions, gpu_types, accounts, ...}

Python 3.6 or newer, standard library only.
"""
import re
import time

SINFO_FORMAT = "%P|%a|%l|%D|%c|%m|%G"
SINFO_FIELDS = ("partition", "available", "time_limit", "nodes", "cpus", "memory_mb", "gres")
INFINITE = ("UNLIMITED", "INFINITE")
_UNITS = {"w": 7 * 1440, "d": 1440, "h": 60, "m": 1, "s": 1.0 / 60}


def parse_time(text):
    """Minutes for a time limit as a person or Slurm writes it: `6h`, `1d 2h`, `90m`, `1h30m`, `6:00:00`, `1-02:00`,
    `1-02:00:00`, or a bare number (Slurm's: minutes). None for UNLIMITED; ValueError for anything else."""
    value = str(text).strip().lower()
    if not value:
        raise ValueError("a time limit is needed")
    if value.upper() in INFINITE:
        return None
    m = re.fullmatch(r"(\d+)-(\d+)(?::(\d+))?(?::(\d+))?", value)
    if m:
        d, h, mi, s = (int(x) if x else 0 for x in m.groups())
        return d * 1440 + h * 60 + mi + s / 60.0
    if re.fullmatch(r"\d+(:\d+){1,2}", value):
        parts = [int(p) for p in value.split(":")]
        return parts[0] + parts[1] / 60.0 if len(parts) == 2 else parts[0] * 60 + parts[1] + parts[2] / 60.0
    if re.fullmatch(r"\d+", value):
        return int(value)
    words = re.findall(r"(\d+(?:\.\d+)?)\s*([wdhms])[a-z]*", value)
    if words and re.sub(r"(\d+(?:\.\d+)?)\s*([wdhms])[a-z]*|[\s,]+|and", "", value) == "":
        return sum(float(n) * _UNITS[u] for n, u in words)
    raise ValueError("a time limit such as 6h, 1d 2h, 90m or 6:00:00, not {!r}".format(text))


def format_time(minutes):
    """Slurm's D-HH:MM:SS (HH:MM:SS under a day) for minutes; UNLIMITED for None."""
    if minutes is None:
        return "UNLIMITED"
    seconds = int(round(minutes * 60))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    mins, secs = divmod(rest, 60)
    return "{}-{:02d}:{:02d}:{:02d}".format(days, hours, mins, secs) if days else "{}:{:02d}:{:02d}".format(hours, mins, secs)


def parse_mem(text):
    """MB for a memory size (`8G`, `500M`, `8 GB`, `2T`, or a bare number: Slurm's MB); ValueError otherwise."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?)b?", str(text).strip().lower())
    if not m:
        raise ValueError("a memory size such as 8G or 500M, not {!r}".format(text))
    scale = {"k": 1.0 / 1024, "": 1.0, "m": 1.0, "g": 1024.0, "t": 1024.0 ** 2}[m.group(2)]
    return float(m.group(1)) * scale


def format_mem(mb):
    """The shortest exact Slurm size for MB: 8G, 500M."""
    mb = int(round(mb))
    if mb and mb % (1024 * 1024) == 0:
        return "{}T".format(mb // (1024 * 1024))
    if mb and mb % 1024 == 0:
        return "{}G".format(mb // 1024)
    return "{}M".format(mb)


def parse_gres(text):
    """[{type, count}] from a gres string: `gpu:a100:2(S:0-1),tmpfs:10G`, `gpu:4`, `gpu:a100:8`. Only GPUs."""
    out = []
    for part in re.split(r",(?![^(]*\))", text or ""):
        part = re.sub(r"\(.*\)", "", part).strip()
        fields = part.split(":")
        if not fields or fields[0] != "gpu":
            continue
        kind, count = "", 0
        if len(fields) == 2:
            count = int(fields[1]) if fields[1].isdigit() else 0
        elif len(fields) >= 3:
            kind = fields[1]
            count = int(fields[2]) if fields[2].isdigit() else 0
        out.append({"type": kind, "count": count})
    return out


def parse_sinfo(text):
    """[{name, default, available, time_limit_minutes, time_limit, nodes, cpus, memory_mb, gpus: [{type, count}]}] per
    partition from `sinfo -h -o SINFO_FORMAT`: node types of one partition are folded into the largest of each."""
    partitions, order = {}, []
    for line in (text or "").splitlines():
        values = line.split("|")
        if len(values) < len(SINFO_FIELDS) or not values[0].strip():
            continue
        row = dict(zip(SINFO_FIELDS, [v.strip() for v in values]))
        name = row["partition"].rstrip("*")
        if name not in partitions:
            try:
                limit = parse_time(row["time_limit"])
            except ValueError:
                limit = None
            partitions[name] = {"name": name, "default": row["partition"].endswith("*"), "available": row["available"],
                                "time_limit": row["time_limit"], "time_limit_minutes": limit, "nodes": 0, "cpus": 0,
                                "memory_mb": 0, "gpus": []}
            order.append(name)
        part = partitions[name]
        for key, field in (("nodes", "nodes"), ("cpus", "cpus"), ("memory_mb", "memory_mb")):
            digits = re.match(r"\d+", row[field])
            if digits:
                n = int(digits.group())
                part[key] = part[key] + n if key == "nodes" else max(part[key], n)
        for gpu in parse_gres(row["gres"]):
            known = [g for g in part["gpus"] if g["type"] == gpu["type"]]
            if known:
                known[0]["count"] = max(known[0]["count"], gpu["count"])
            else:
                part["gpus"].append(gpu)
    return [partitions[n] for n in order]


def parse_assoc(text):
    """[{account, partition, qos}] from `sacctmgr -n -P show assoc user=U format=account,partition,qos`."""
    out = []
    for line in (text or "").splitlines():
        values = line.split("|")
        if values and values[0].strip():
            out.append(dict(zip(("account", "partition", "qos"), [v.strip() for v in values + ["", ""]][:3])))
    return out


def parse_test_only(text):
    """What `sbatch --test-only` printed: {ok, message, start, partition, nodes} or {ok: False, message}. Slurm says
    `sbatch: Job 123 to start at 2026-10-30T09:00:00 using 4 processors on nodes c001 in partition gpu` (a stderr
    line), or `sbatch: error: ...`."""
    text = (text or "").strip()
    m = re.search(r"to start at (\S+)(?: using (\d+) processors?)?(?: on nodes? (\S+))?(?: in partition (\S+))?", text)
    if m and "error" not in text.lower():
        return {"ok": True, "start": m.group(1), "processors": int(m.group(2)) if m.group(2) else None,
                "nodes": m.group(3), "partition": m.group(4),
                "message": "would start {}{}".format(_when(m.group(1)), " on " + m.group(4) if m.group(4) else "")}
    errors = [re.sub(r"^sbatch: (error: )?", "", line).strip() for line in text.splitlines()
              if "error" in line.lower() or "invalid" in line.lower()]
    return {"ok": False, "message": " ".join(errors) or text[-400:] or "sbatch said nothing"}


def _when(stamp):
    """`about now`, `in 3 h` or the date, for Slurm's ISO start estimate."""
    try:
        seconds = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")) - time.time()
    except ValueError:
        return stamp
    if seconds < 90:
        return "about now"
    if seconds < 3600:
        return "in {} min".format(int(seconds // 60))
    if seconds < 86400:
        return "in about {:.0f} h".format(seconds / 3600.0)
    return "on " + stamp.replace("T", " ")[:16]


def collect(run, user):
    """The cluster as the form needs it. `run(argv)` -> (returncode, output) runs a command where Slurm is."""
    info = {"user": user, "at": time.time(), "slurm_version": None, "slurm_problem": None, "partitions": [],
            "gpu_types": [], "accounts": [], "default_account": None}
    code, out = run(["sinfo", "--version"])
    if code != 0 or not out.strip():
        info["slurm_problem"] = (" ".join(out.split())[-200:] or "sinfo exited {}".format(code))
        return info
    info["slurm_version"] = out.strip()
    code, out = run(["sinfo", "-h", "-o", SINFO_FORMAT])
    if code == 0:
        info["partitions"] = parse_sinfo(out)
    types = sorted({g["type"] for p in info["partitions"] for g in p["gpus"] if g["type"]})
    info["gpu_types"] = types
    code, out = run(["sacctmgr", "-n", "-P", "show", "assoc", "user=" + user, "format=account,partition,qos"])
    if code == 0:
        rows = parse_assoc(out)
        info["associations"] = rows
        info["accounts"] = sorted({r["account"] for r in rows if r["account"]})
    code, out = run(["sacctmgr", "-n", "-P", "show", "user", user, "format=defaultaccount"])
    if code == 0 and out.strip():
        info["default_account"] = out.strip().splitlines()[0].strip() or None
    return info
