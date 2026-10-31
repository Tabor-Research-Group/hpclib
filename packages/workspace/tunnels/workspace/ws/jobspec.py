"""
The Workspace's job spec and the batch script made from it. A spec is a small JSON object the form edits; `render`
turns it into the sbatch script, so nobody writes #SBATCH lines by hand. The script carries its spec in one comment
line (`#HPCLIB-SPEC {...}`), so the form can open a script it made and change it; a script written by hand has no such
line and opens in the editor only.

  validate(spec, cluster=None) -> (spec, errors, warnings)   the spec cleaned and defaulted, what is wrong with it
  render(spec, cluster=None, env=None) -> script text         raises SpecError when validate finds errors
  embedded_spec(script) -> spec or None                       the spec a generated script carries

  spec = {"name", "workdir", "kind": "script"|"command"|"body", "command", "arguments", "body",
          "resources": {"partition", "time", "nodes", "ntasks", "cpus", "memory", "memory_per": "node"|"cpu", "gpus",
                        "gpu_type", "account", "qos"},
          "modules": [..], "environment": {"mode": "auto"|"sync"|"none", "shell_init": "bashrc"|"login"|"none"},
          "output": "%x-%j.out", "extra_sbatch": ["#SBATCH ..."],
          "array": {"range": "1-10", "limit": 4},                       optional: many tasks of one script
          "sweep": {"LR": ["0.1", "0.01"], "SEED": ["1", "2", "3"]},     optional: every combination is a task
          "dependency": {"type": "afterok", "jobs": ["12345"]}}          optional: start after these jobs

A script of the person's own (kind "body") keeps its commands verbatim in `body` (an imported .slurm file: parse_script
reads the #SBATCH lines into a spec and keeps the rest); it runs as written, without the TERM forwarding that wraps a
program the form launched. interactive_command(spec) makes the srun line for an interactive session from the same
resources.

A sweep is an array whose tasks differ by variables: the combinations are written into the script as a small table, and
each task takes its row by SLURM_ARRAY_TASK_ID and exports the names (so the program reads $LR, and an argument written
{LR} becomes "$LR"). The three optional keys are in the cleaned spec only when they are set.

`cluster` is clusterinfo.collect()'s answer (partitions, for the warnings; "hpclib" the path of hpclib.sh there) and
`env` is envinfo.detect()'s (what to activate). Every value that reaches the shell is quoted with shlex.quote; the
arguments are split with shlex.split and each quoted again, so what the form shows is what runs. The script traps TERM
(Slurm's warning, sent a minute before the time limit with --signal=B:TERM@60, and what scancel sends) and passes it on to
the program, which a bash script waiting on its child would otherwise ignore until the child had finished.

Python 3.6 or newer, standard library only.
"""
import json
import os
import re
import shlex

try:
    import clusterinfo
    import envinfo
except ImportError:                       # imported as a package from the repository
    from hpclib.servers import clusterinfo, envinfo

SPEC_VERSION = 1
SPEC_MARK = "#HPCLIB-SPEC "
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
WORD_RE = re.compile(r"[A-Za-z0-9_.,:+-]{1,128}")
MODULE_RE = re.compile(r"[A-Za-z0-9_./+-]{1,128}")
OUTPUT_RE = re.compile(r"[A-Za-z0-9_.%/+-]{1,200}")
SHELL_INITS = ("bashrc", "login", "none")
ENV_MODES = ("auto", "sync", "none")
DEPENDENCY_TYPES = ("afterok", "afterany", "afternotok", "after", "aftercorr")
SWEEP_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,40}")
SWEEP_VALUE_RE = re.compile(r"[A-Za-z0-9_.,:+/=@-]{1,128}")
ARRAY_RANGE_RE = re.compile(r"\d+(-\d+)?(:\d+)?(,\d+(-\d+)?(:\d+)?)*")
SWEEP_RESERVED = ("PATH", "HOME", "USER", "SHELL", "PWD", "IFS", "LD_LIBRARY_PATH", "LD_PRELOAD", "BASH_ENV", "ENV")
MAX_BODY = 256 * 1024
SWEEP_LIMIT = 10000
SLURM_ARRAY_USUAL_MAX = 1000          # MaxArraySize is 1001 unless the site changed it: a warning, not an error
PARAMS_END = "HPCLIB_PARAMS_END"
INTERPRETERS = {".py": "python -u", ".sh": "bash", ".bash": "bash", ".r": "Rscript", ".jl": "julia"}


class SpecError(ValueError):
    """A spec that can't be made into a script; `.errors` lists why (one sentence each)."""

    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def _int(value, label, errors, low=0, high=100000, default=None):
    if value in (None, ""):
        return default
    try:
        n = int(str(value).strip())
    except ValueError:
        errors.append("{} is a whole number, not {!r}".format(label, value))
        return default
    if not low <= n <= high:
        errors.append("{} is between {} and {}".format(label, low, high))
        return default
    return n


def _word(value, label, errors, pattern=WORD_RE):
    value = ("" if value is None else str(value)).strip()
    if value and not pattern.fullmatch(value):
        errors.append("{} has characters that don't belong in it: {!r}".format(label, value))
        return ""
    return value


def _line(value, label, errors):
    value = ("" if value is None else str(value)).strip()
    if "\n" in value or "\r" in value or "\0" in value:
        errors.append("{} is a single line".format(label))
        return ""
    return value


def validate(spec, cluster=None):
    """The spec cleaned (every field present, in its canonical form), the errors that stop a script being made, and the
    warnings that don't (the cluster, as far as it was read, wouldn't take the request)."""
    errors, warnings = [], []
    spec = spec if isinstance(spec, dict) else {}
    res_in = spec.get("resources") if isinstance(spec.get("resources"), dict) else {}
    env_in = spec.get("environment") if isinstance(spec.get("environment"), dict) else {}

    workdir = _line(spec.get("workdir"), "The working directory", errors)
    if not workdir.startswith("/"):
        errors.append("The working directory is a full path (it starts with /)")
    kind = spec.get("kind") or "script"
    if kind not in ("script", "command", "body"):
        errors.append("What to run is a script, a command or your own script text")
        kind = "script"
    command = _line(spec.get("command"), "The command", errors)
    body = spec.get("body") if isinstance(spec.get("body"), str) else ""
    if kind == "body":
        if not body.strip():
            errors.append("The script has nothing to run")
        if "\0" in body or len(body) > MAX_BODY:
            errors.append("The script's text is too long or has a NUL in it")
            body = ""
    elif not command:
        errors.append("Say what to run: a script's path, or a command")
    arguments = _line(spec.get("arguments"), "The arguments", errors)
    try:
        shlex.split(arguments)
    except ValueError as e:
        errors.append("The arguments don't parse ({})".format(e))
    name = _word(spec.get("name") or os.path.splitext(os.path.basename(command))[0] or "job", "The job name", errors, NAME_RE)

    res = {}
    res["partition"] = _word(res_in.get("partition"), "The partition", errors)
    time_text = str(res_in.get("time") or "").strip() or "1:00:00"
    try:
        minutes = clusterinfo.parse_time(time_text)
        if minutes is None:
            res["time"] = "UNLIMITED"
        elif minutes <= 0:
            errors.append("The time limit is more than zero")
            res["time"] = "1:00:00"
        else:
            res["time"] = clusterinfo.format_time(minutes)
    except ValueError as e:
        errors.append(str(e).capitalize())
        res["time"], minutes = "1:00:00", None
    res["nodes"] = _int(res_in.get("nodes"), "Nodes", errors, 1, 4096, 1)
    res["ntasks"] = _int(res_in.get("ntasks"), "Tasks per node", errors, 1, 4096, 1)
    res["cpus"] = _int(res_in.get("cpus"), "CPUs per task", errors, 1, 4096, None)
    memory_mb = None
    if str(res_in.get("memory") or "").strip():
        try:
            memory_mb = clusterinfo.parse_mem(res_in["memory"])
            res["memory"] = clusterinfo.format_mem(memory_mb)
        except ValueError as e:
            errors.append(str(e).capitalize())
            res["memory"] = ""
    else:
        res["memory"] = ""
    res["memory_per"] = res_in.get("memory_per") if res_in.get("memory_per") in ("node", "cpu") else "node"
    res["gpus"] = _int(res_in.get("gpus"), "GPUs", errors, 0, 64, 0)
    res["gpu_type"] = _word(res_in.get("gpu_type"), "The GPU type", errors)
    res["account"] = _word(res_in.get("account"), "The account", errors)
    res["qos"] = _word(res_in.get("qos"), "The QOS", errors)
    if res["gpu_type"] and not res["gpus"]:
        res["gpus"] = 1

    modules = spec.get("modules") if isinstance(spec.get("modules"), list) else str(spec.get("modules") or "").split()
    modules = [str(m).strip() for m in modules if str(m).strip()]
    for m in modules:
        if not MODULE_RE.fullmatch(m):
            errors.append("The module name {!r} has characters that don't belong in it".format(m))
    mode = env_in.get("mode") if env_in.get("mode") in ENV_MODES else "auto"
    shell_init = env_in.get("shell_init") if env_in.get("shell_init") in SHELL_INITS else "bashrc"
    output = _word(spec.get("output") or "%x-%j.out", "The output file", errors, OUTPUT_RE) or "%x-%j.out"
    extra = spec.get("extra_sbatch") if isinstance(spec.get("extra_sbatch"), list) else []
    extra = [_line(x, "An extra #SBATCH line", errors) for x in extra if str(x).strip()]
    for x in extra:
        if not x.startswith("#SBATCH "):
            errors.append("An extra line starts with #SBATCH, not {!r}".format(x[:30]))

    array, sweep, dependency = _fan_out(spec, errors, warnings)
    if (array or sweep) and output == "%x-%j.out":
        output = "%x-%A_%a.out"           # one file per task: %j would be each task's own job id
    _against_cluster(res, minutes, memory_mb, cluster, warnings)
    clean = {"version": SPEC_VERSION, "name": name, "workdir": workdir, "kind": kind, "command": command,
             "arguments": arguments, "resources": res, "modules": modules,
             "environment": {"mode": mode, "shell_init": shell_init}, "output": output, "extra_sbatch": extra}
    for key, value in (("array", array), ("sweep", sweep), ("dependency", dependency)):
        if value:
            clean[key] = value
    if kind == "body":
        clean["body"] = body
    return clean, errors, warnings


def sweep_rows(sweep):
    """The combinations of a sweep's values, first name slowest: a list of tuples, in the order of the names."""
    rows = [()]
    for name in sweep:
        rows = [row + (value,) for row in rows for value in sweep[name]]
    return rows


def task_count(spec):
    """How many tasks the spec makes: 1 for a plain job, the sweep's combinations, or the array range's size."""
    if spec.get("sweep"):
        return len(sweep_rows(spec["sweep"]))
    rng = (spec.get("array") or {}).get("range")
    if not rng:
        return 1
    total = 0
    for part in rng.split(","):
        step = 1
        if ":" in part:
            part, step = part.split(":")
            step = max(int(step), 1)
        low, _, high = part.partition("-")
        low, high = int(low), int(high or low)
        total += (high - low) // step + 1 if high >= low else 0
    return total


def first_task(spec):
    """The index of the first task (what %a is in the first task's output file name)."""
    if spec.get("sweep") or not (spec.get("array") or {}).get("range"):
        return 0
    return int(re.match(r"\d+", spec["array"]["range"]).group(0))


def _fan_out(spec, errors, warnings):
    """(array, sweep, dependency): the cleaned optional parts, each {} when not asked for."""
    arr_in = spec.get("array") if isinstance(spec.get("array"), dict) else {}
    rng = str(arr_in.get("range") or "").strip().replace(" ", "")
    limit = _int(arr_in.get("limit"), "The array's limit on tasks at once", errors, 1, SWEEP_LIMIT, None)
    if rng and not ARRAY_RANGE_RE.fullmatch(rng):
        errors.append("The array range looks like 1-10, 0-99:2 or 1,5,9, not {!r}".format(rng))
        rng = ""
    sweep = {}
    sw_in = spec.get("sweep") if isinstance(spec.get("sweep"), dict) else {}
    for name, values in sw_in.items():
        name = str(name).strip()
        if not name:
            continue
        if not SWEEP_NAME_RE.fullmatch(name) or name in SWEEP_RESERVED or name.startswith(("SLURM_", "HPCLIB_", "BASH")):
            errors.append("A sweep variable is a plain name (letters, digits, _), not {!r}".format(name))
            continue
        if isinstance(values, str):
            values = re.split(r"[\s,]+", values)
        values = [str(v).strip() for v in values if str(v).strip()] if isinstance(values, list) else []
        bad = [v for v in values if not SWEEP_VALUE_RE.fullmatch(v) or v.startswith("HPCLIB_")]
        if bad:
            errors.append("{} takes plain values (letters, digits and . : + / = @ - _), not {!r}".format(name, bad[0]))
            continue
        if not values:
            errors.append("{} needs at least one value to sweep over".format(name))
            continue
        sweep[name] = values
    if sweep:
        count = len(sweep_rows(sweep))
        if count > SWEEP_LIMIT:
            errors.append("The sweep makes {} tasks; the most is {}".format(count, SWEEP_LIMIT))
        elif count > SLURM_ARRAY_USUAL_MAX:
            warnings.append("The sweep makes {} tasks; Slurm's array limit is usually {}, so it may refuse".format(count, SLURM_ARRAY_USUAL_MAX + 1))
        if rng:
            warnings.append("A sweep sets the array range itself (0-{}); the range typed is ignored".format(count - 1))
    array = {}
    if rng or sweep:
        array = {"range": "" if sweep else rng}
        if limit:
            array["limit"] = limit
    elif limit:
        warnings.append("The limit on tasks at once only matters with an array range or a sweep")
    dep_in = spec.get("dependency") if isinstance(spec.get("dependency"), dict) else {}
    jobs = dep_in.get("jobs") if isinstance(dep_in.get("jobs"), list) else str(dep_in.get("jobs") or "").replace(",", " ").split()
    jobs = [str(j).strip() for j in jobs if str(j).strip()]
    dependency = {}
    if jobs:
        kind = dep_in.get("type") or "afterok"
        if kind not in DEPENDENCY_TYPES:
            errors.append("A dependency is one of {}".format(", ".join(DEPENDENCY_TYPES)))
            kind = "afterok"
        if not all(re.fullmatch(r"\d{1,12}", j) for j in jobs):
            errors.append("A dependency names jobs by their numbers")
        elif len(jobs) > 50:
            errors.append("A dependency names at most 50 jobs")
        else:
            dependency = {"type": kind, "jobs": jobs}
    return array, sweep, dependency


def _against_cluster(res, minutes, memory_mb, cluster, warnings):
    """Warnings for what the partitions as read wouldn't take. The cluster knows better than this reading: warnings
    only, and `sbatch --test-only` (Check) has the last word."""
    parts = (cluster or {}).get("partitions") or []
    if not parts:
        return
    part = next((p for p in parts if p["name"] == res["partition"]), None)
    if res["partition"] and part is None:
        warnings.append("No partition called {} was seen on this cluster ({})".format(
            res["partition"], ", ".join(p["name"] for p in parts)))
    part = part or next((p for p in parts if p["default"]), None)
    if part is None:
        return
    where = "partition {}".format(part["name"])
    limit = part.get("time_limit_minutes")
    if minutes and limit and minutes > limit:
        warnings.append("{} allows {} at most".format(where, clusterinfo.format_time(limit)))
    if res["cpus"] and part["cpus"] and res["cpus"] * res["ntasks"] > part["cpus"]:
        warnings.append("{} has nodes of {} CPUs; {} were asked for on one".format(where, part["cpus"], res["cpus"] * res["ntasks"]))
    if memory_mb and part["memory_mb"]:
        total = memory_mb * (max(res["cpus"] or 1, 1) * res["ntasks"] if res["memory_per"] == "cpu" else 1)
        if total > part["memory_mb"]:
            warnings.append("{} has nodes of {}; {} were asked for on one".format(
                where, clusterinfo.format_mem(part["memory_mb"]), clusterinfo.format_mem(total)))
    if res["gpus"]:
        have = [g for g in part["gpus"] if not res["gpu_type"] or g["type"] == res["gpu_type"]]
        if not have:
            warnings.append("No GPUs{} were seen in {}".format(" of type " + res["gpu_type"] if res["gpu_type"] else "", where))
        elif max(g["count"] for g in have) < res["gpus"]:
            warnings.append("{} has at most {} GPUs per node".format(where, max(g["count"] for g in have)))


def _sbatch_lines(spec):
    r = spec["resources"]
    lines = ["#SBATCH --job-name={}".format(spec["name"]),
             "#SBATCH --time={}".format(r["time"]),
             "#SBATCH --nodes={}".format(r["nodes"]),
             "#SBATCH --ntasks-per-node={}".format(r["ntasks"])]
    if r["cpus"]:
        lines.append("#SBATCH --cpus-per-task={}".format(r["cpus"]))
    if r["memory"]:
        lines.append("#SBATCH --{}={}".format("mem-per-cpu" if r["memory_per"] == "cpu" else "mem", r["memory"]))
    if r["gpus"]:
        lines.append("#SBATCH --gres=gpu:{}{}".format(r["gpu_type"] + ":" if r["gpu_type"] else "", r["gpus"]))
    for flag, key in (("partition", "partition"), ("account", "account"), ("qos", "qos")):
        if r[key]:
            lines.append("#SBATCH --{}={}".format(flag, r[key]))
    if spec.get("sweep") or spec.get("array"):
        arr = spec.get("array") or {}
        rng = "0-{}".format(task_count(spec) - 1) if spec.get("sweep") else arr["range"]
        lines.append("#SBATCH --array={}{}".format(rng, "%{}".format(arr["limit"]) if arr.get("limit") else ""))
    if spec.get("dependency"):
        lines.append("#SBATCH --dependency={}:{}".format(spec["dependency"]["type"], ":".join(spec["dependency"]["jobs"])))
        lines.append("#SBATCH --kill-on-invalid-dep=yes     # a job that can't run any more is cancelled, not left waiting")
    lines.append("#SBATCH --chdir={}".format(shlex.quote(spec["workdir"])))
    lines.append("#SBATCH --output={}".format(spec["output"]))
    if spec["kind"] != "body":
        lines.append("#SBATCH --signal=B:TERM@60           # a minute's warning before the time limit")
    if spec["environment"]["shell_init"] != "none":
        lines.append("#SBATCH --export=NONE               # not the submitting shell's environment: the init below sets it up")
    lines.extend(spec["extra_sbatch"])
    return lines


def _command_line(spec, env):
    """The line that runs the program, quoted; with the environment "sync" it is the manager's run command."""
    q = shlex.quote
    names = list(spec.get("sweep") or {})
    holes = re.compile(r"\{(" + "|".join(map(re.escape, names)) + r")\}") if names else None

    def quote(arg):
        # {NAME} for a sweep variable becomes "${NAME}"; the rest of the argument stays literal
        if holes is None or not holes.search(arg):
            return q(arg)
        out, at = "", 0
        for m in holes.finditer(arg):
            out += re.sub(r'([\\"$`])', r"\\\1", arg[at:m.start()]) + "${" + m.group(1) + "}"
            at = m.end()
        return '"' + out + re.sub(r'([\\"$`])', r"\\\1", arg[at:]) + '"'

    args = " ".join(quote(a) for a in shlex.split(spec["arguments"]))
    if spec["kind"] == "command":
        return (spec["command"] + " " + args).strip()
    path = spec["command"]
    interpreter = INTERPRETERS.get(os.path.splitext(path)[1].lower())
    program = (interpreter + " " + q(path)) if interpreter else q(path)
    if spec["environment"]["mode"] == "sync" and env and env.get("manager") and interpreter == "python -u":
        if env["manager"] == "uv":
            program = "uv run --project {} python -u {}".format(q(env["project"]), q(path))
        else:
            program = "pixi run --manifest-path {} python -u {}".format(q(os.path.join(env["project"], "pixi.toml")), q(path))
    return (program + " " + args).strip()


def render(spec, cluster=None, env=None):
    """The sbatch script for a spec. `env` (envinfo.detect's answer) is activated when the spec's environment mode is
    "auto" and it is ready; "sync" lets the job run the manager's own run command (which syncs first); "none" leaves
    the environment to the shell's init and the modules."""
    spec, errors, _ = validate(spec, cluster)
    if errors:
        raise SpecError(errors)
    q = shlex.quote
    hpclib = (cluster or {}).get("hpclib") or "$HOME/hpclib/hpclib.sh"
    out = ["#!/bin/bash",
           "# Made by the hpclib Workspace from the spec below: change it there, or edit this by hand (then the form no longer applies)."]
    out += _sbatch_lines(spec)
    out.append(SPEC_MARK + json.dumps(spec, sort_keys=True, separators=(",", ":")))
    out.append("")
    init = spec["environment"]["shell_init"]
    if init == "bashrc":
        out += ['hpc_dir="$PWD"', "[ -f ~/.bashrc ] && . ~/.bashrc          # --export=NONE starts without your environment", 'cd "$hpc_dir"']
    elif init == "login":
        out += ["[ -f /etc/profile ] && . /etc/profile", "[ -f ~/.bash_profile ] && . ~/.bash_profile", "[ -f ~/.profile ] && . ~/.profile"]
    if spec["modules"]:
        out.append("module load " + " ".join(q(m) for m in spec["modules"]))
    out.append("hpclib_sh={}".format(q(hpclib) if not hpclib.startswith("$HOME/") else '"$HOME/' + hpclib[6:] + '"'))
    out.append('if [ -f "$hpclib_sh" ]; then . "$hpclib_sh"; else slurm_job_info() { :; }; slurm_job_footer() { :; }; fi')
    if spec["environment"]["mode"] == "auto" and env and env.get("ready"):
        out += envinfo.prelude(env)
    if spec.get("sweep"):
        out += ["# the sweep: one row per task, picked by SLURM_ARRAY_TASK_ID; its columns become variables",
                "hpc_params=$(cat <<'{}'".format(PARAMS_END), "\t".join(spec["sweep"])]
        out += ["\t".join(row) for row in sweep_rows(spec["sweep"])]
        out += [PARAMS_END, ")",
                'eval "$(printf \'%s\\n\' "$hpc_params" | awk -F\'\\t\' -v n="${SLURM_ARRAY_TASK_ID:-0}" '
                '\'NR==1{for(i=1;i<=NF;i++)h[i]=$i;next} NR==n+2{for(i=1;i<=NF;i++)print "export " h[i] "=" $i}\')"', ""]
    if spec["kind"] == "body":
        out += ["SLURM_JOB_START_TIME=$(date +%s)", "slurm_job_info", "", spec["body"].rstrip("\n"),
                "hpc_rc=$?", "slurm_job_footer", 'exit "$hpc_rc"', ""]
        return "\n".join(out)
    out += ["SLURM_JOB_START_TIME=$(date +%s)", "slurm_job_info", "",
            "# Slurm sends TERM a minute before the time limit (and scancel sends it): pass it to the program, then wait for it to finish.",
            "hpc_child=",
            'hpc_term() { [ -n "$hpc_child" ] && kill -TERM "$hpc_child" 2>/dev/null; }',
            "trap hpc_term TERM",
            _command_line(spec, env) + " &",
            "hpc_child=$!",
            "while :; do",
            '  wait "$hpc_child"; hpc_rc=$?',
            '  kill -0 "$hpc_child" 2>/dev/null || break      # a wait cut short by the trap: the program is still running',
            "done",
            "slurm_job_footer",
            'exit "$hpc_rc"',
            ""]
    return "\n".join(out)


def embedded_spec(script):
    """The spec a generated script carries, or None for a script written by hand (or whose spec line was damaged)."""
    for line in (script or "").splitlines():
        if line.startswith(SPEC_MARK):
            try:
                spec = json.loads(line[len(SPEC_MARK):])
            except ValueError:
                return None
            return spec if isinstance(spec, dict) else None
    return None


# -- a script written by hand ------------------------------------------------------------------------------------------

SHORT_OPTIONS = {"J": "job-name", "t": "time", "N": "nodes", "c": "cpus-per-task", "p": "partition", "A": "account",
                 "o": "output", "D": "chdir", "a": "array", "d": "dependency", "q": "qos", "G": "gpus", "n": "ntasks",
                 "e": "error", "w": "nodelist", "x": "exclude", "C": "constraint", "M": "clusters", "L": "licenses",
                 "k": "no-kill", "H": "hold", "s": "oversubscribe", "S": "core-spec", "b": "begin"}


def _directive_options(line):
    """The (option, value) pairs of one `#SBATCH` line, [] when it isn't one; a trailing comment is dropped."""
    m = re.match(r"#SBATCH\s+(.*)$", line.rstrip())
    if not m:
        return []
    try:
        tokens = shlex.split(m.group(1), comments=True)
    except ValueError:
        return None
    pairs, i = [], 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--") and len(tok) > 2:
            key, eq, value = tok[2:].partition("=")
            if not eq and i + 1 < len(tokens) and not tokens[i + 1].startswith("-") and key not in ("exclusive", "requeue", "no-requeue", "hold", "test-only", "wait", "contiguous", "overcommit", "oversubscribe", "no-kill", "parsable", "quiet", "verbose"):
                value, i = tokens[i + 1], i + 1
            pairs.append((key, value if (eq or value) else True))
        elif tok.startswith("-") and len(tok) >= 2 and tok[1] in SHORT_OPTIONS:
            value = tok[2:] if len(tok) > 2 else None
            if value is None and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                value, i = tokens[i + 1], i + 1
            pairs.append((SHORT_OPTIONS[tok[1]], value if value is not None else True))
        else:
            pairs.append((tok, True))
        i += 1
    return pairs


def parse_script(text, default_workdir="/", default_name="job"):
    """A script written by hand -> (spec, warnings). The #SBATCH options the form has a field for become the spec's
    fields; every other #SBATCH option stays as an `extra_sbatch` line; the rest of the script, comments and commands,
    is the spec's `body`, kept as it is. Only the leading comment block counts for #SBATCH, as with sbatch itself."""
    warnings = []
    lines = (text or "").replace("\r\n", "\n").split("\n")
    if lines and lines[0].startswith("#!") and "bash" not in lines[0]:
        warnings.append("The script starts with {!r}; the form's version starts with #!/bin/bash".format(lines[0][:60]))
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    res, spec, extra = {}, {"workdir": default_workdir, "name": default_name}, []
    fan = {"array": {}, "dependency": {}}
    head, i = [], 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        head.append(lines[i])
        i += 1
    body_lines = []
    for line in head:
        if line.startswith(SPEC_MARK):
            continue
        pairs = _directive_options(line) if line.startswith("#SBATCH") else []
        if not pairs:
            if pairs is None:
                extra.append(line.rstrip())           # a line shlex can't read: kept as it is
            else:
                body_lines.append(line)
            continue
        unmapped = []
        for key, value in pairs:
            if not _map_option(key, value, res, spec, fan):
                unmapped.append((key, value))
        if len(pairs) == 1 and unmapped:
            extra.append(line.rstrip())
        else:
            extra.extend("#SBATCH --{}{}".format(k, "" if v is True else "=" + shlex.quote(str(v))) for k, v in unmapped)
    body_lines += lines[i:]
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    spec.update(kind="body", command="", body="\n".join(body_lines).rstrip("\n") + "\n", resources=res, extra_sbatch=extra,
                environment={"mode": "none", "shell_init": "none"})
    spec["output"] = spec.get("output") or "slurm-%j.out"       # what sbatch writes to when the script doesn't say
    if fan["array"]:
        spec["array"] = fan["array"]
    if fan["dependency"]:
        spec["dependency"] = fan["dependency"]
    if "time" not in res:
        warnings.append("The script sets no --time, so the cluster's default applies to it; the form's version says 1:00:00")
    return spec, warnings


def _map_option(key, value, res, spec, fan):
    """Put one #SBATCH option into the spec; False when the form has no field for it (or not for this value)."""
    if value is True:
        return False
    value = str(value)
    if key == "job-name" and NAME_RE.fullmatch(value):
        spec["name"] = value
    elif key == "time" and re.fullmatch(r"[0-9:.dhms-]+|UNLIMITED", value, re.I):
        res["time"] = value
    elif key in ("nodes", "ntasks-per-node", "cpus-per-task") and value.isdigit():
        res[{"nodes": "nodes", "ntasks-per-node": "ntasks", "cpus-per-task": "cpus"}[key]] = value
    elif key in ("mem", "mem-per-cpu") and re.fullmatch(r"\d+(\.\d+)?[KMGTkmgt]?[Bb]?", value):
        res["memory"], res["memory_per"] = value, "cpu" if key == "mem-per-cpu" else "node"
    elif key in ("partition", "account", "qos") and WORD_RE.fullmatch(value):
        res[key] = value
    elif key in ("gres", "gpus", "gpus-per-node") and "gpus" not in res:
        m = (re.fullmatch(r"gpu(?::([A-Za-z0-9_.-]+))?(?::(\d+))?", value) if key == "gres"
             else re.fullmatch(r"(?:([A-Za-z0-9_.-]+):)?(\d+)", value))
        if not m:
            return False
        gpu_type, count = m.groups()
        if key == "gres" and count is None and gpu_type and gpu_type.isdigit():
            gpu_type, count = None, gpu_type          # gpu:2
        res["gpus"], res["gpu_type"] = count or "1", gpu_type or ""
    elif key == "chdir" and value.startswith("/"):
        spec["workdir"] = value
    elif key == "output" and OUTPUT_RE.fullmatch(value):
        spec["output"] = value
    elif key == "array" and re.fullmatch(r"(\d[\d,:-]*)(%\d+)?", value) and ARRAY_RANGE_RE.fullmatch(value.split("%")[0]):
        rng, _, limit = value.partition("%")
        fan["array"] = dict({"range": rng}, **({"limit": int(limit)} if limit else {}))
    elif key == "dependency":
        m = re.fullmatch(r"({})((?::\d+)+)".format("|".join(DEPENDENCY_TYPES)), value)
        if not m:
            return False
        fan["dependency"] = {"type": m.group(1), "jobs": m.group(2)[1:].split(":")}
    else:
        return False
    return True


def interactive_command(spec):
    """The line that asks for an interactive session with the spec's resources and gives a shell on the compute node:
    `srun ... --pty bash -l` (with the spec's modules loaded first). The spec is validated first; a script's own
    options (array, dependency, the program) don't apply and are not used."""
    spec, errors, warnings = validate(dict(spec, kind="command", command="bash", body=None))
    if errors:
        raise SpecError(errors)
    r = spec["resources"]
    argv = ["srun", "--job-name={}".format(spec["name"] if spec["name"] != "bash" else "interactive"),
            "--time={}".format(r["time"]), "--nodes={}".format(r["nodes"]), "--ntasks-per-node={}".format(r["ntasks"])]
    if r["cpus"]:
        argv.append("--cpus-per-task={}".format(r["cpus"]))
    if r["memory"]:
        argv.append("--{}={}".format("mem-per-cpu" if r["memory_per"] == "cpu" else "mem", r["memory"]))
    if r["gpus"]:
        argv.append("--gres=gpu:{}{}".format(r["gpu_type"] + ":" if r["gpu_type"] else "", r["gpus"]))
    for flag in ("partition", "account", "qos"):
        if r[flag]:
            argv.append("--{}={}".format(flag, r[flag]))
    argv += ["--pty"]
    if spec["modules"]:
        argv += ["bash", "-lc", "module load {}; exec bash -il".format(" ".join(shlex.quote(m) for m in spec["modules"]))]
    else:
        argv += ["bash", "-il"]
    return " ".join(shlex.quote(a) if not re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", a) else a for a in argv), warnings
