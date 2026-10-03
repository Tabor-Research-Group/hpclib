"""
Job templates, resource limits, a registry of submitted jobs, cluster
and module information, and SLURM output parsing for rest_server.py.

Templates let a client (typically an LLM) start jobs without being able
to submit arbitrary scripts or sbatch options. Each template is a
directory in the templates directory (a "config directory"):

  NAME/template.json   description, typed parameters, default resources,
                       optional `modules` to load and optional `array`
                       (per-task parameters, for one-job-per-input batches)
  NAME/script.sh       the job body (no #SBATCH lines; resources come
                       from template.json so limits can be enforced)
  NAME/guide.md        optional: notes a client reads to plan with this
                       template (inputs to prepare, how to check results)
  NAME/examples/*.json optional: example submissions

A directory holding only `guide.md` is a planning guide: a workflow
description with no job of its own (e.g. "generate inputs locally with
X, then run template Y over them").

Parameters reach the script as environment variables
(`HPC_PARAM_<NAME>`, and `HPC_TASK_<NAME>` per array task, all
shell-quoted), never by text substitution, so a value can't inject shell
code into the script. Resources are passed as sbatch command-line
options after validation against the server's limits.

Clients can also propose new templates. Proposals are validated and
stored separately; nothing runs until the owner approves one with
`rest_server.py --approve-template NAME`, unless the server was started
with `--auto-approve-templates`, which approves proposals for new names
(or, with `=all`, replacements too) as soon as they pass validation, but
only while template jobs are sandboxed.

Standard library only.
"""
import copy
import getpass
import json
import math
import os
import re
import shlex
import shutil
import socket
import sqlite3
import string
import subprocess
import threading
import time
from contextlib import closing

try:
    from . import rest_sandbox
except ImportError:  # run as a script from the servers directory
    import rest_sandbox

__all__ = [
    "RESTError",
    "clean_env",
    "parse_slurm_time",
    "parse_mem",
    "parse_gres_gpus",
    "ResourceLimits",
    "JobTemplate",
    "TemplateStore",
    "ProposalStore",
    "JobRegistry",
    "SlurmRunner",
    "ClusterInfo",
    "ModuleSystem",
    "JobManager",
]


class RESTError(Exception):
    """Raised to send a JSON error response with `status`."""
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.payload = dict({"error": message}, **extra)


SECRET_ENV_PREFIXES = rest_sandbox.SECRET_ENV_PREFIXES

def clean_env(env=None):
    """
    The environment for child processes, minus anything secret: without the
    Python launcher's modules (rest_sandbox.child_env), so sbatch'd jobs and
    module commands see the user's own environment.
    """
    return rest_sandbox.child_env(env)


################################################################################
##
##  SLURM value parsing
##

def parse_slurm_time(value):
    """SLURM time string -> minutes. Accepts M, M:S, H:M:S, D-H, D-H:M, D-H:M:S."""
    v = str(value).strip()
    if v.upper() in ("UNLIMITED", "INFINITE"):
        return math.inf
    m = re.fullmatch(r"(\d+)-(\d+)(?::(\d+))?(?::(\d+))?", v)
    if m:
        d, h, mi, s = (int(x) if x else 0 for x in m.groups())
        return d * 1440 + h * 60 + mi + s / 60
    parts = v.split(":")
    if not all(p.isdigit() for p in parts) or not 1 <= len(parts) <= 3:
        raise ValueError(f"invalid SLURM time {value!r}")
    parts = [int(p) for p in parts]
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[0] + parts[1] / 60
    return parts[0] * 60 + parts[1] + parts[2] / 60

def parse_mem(value):
    """SLURM memory string -> MB (SLURM's default unit)."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]?)B?", str(value).strip().upper())
    if not m:
        raise ValueError(f"invalid memory size {value!r}")
    scale = {"K": 1 / 1024, "": 1, "M": 1, "G": 1024, "T": 1024 ** 2}[m.group(2)]
    return float(m.group(1)) * scale

def parse_gres_gpus(gres):
    """Count GPUs in a gres string like `gpu:2`, `gpu:a100:1,tmpfs:10G`."""
    total = 0
    for item in str(gres or "").split(","):
        fields = item.strip().split(":")
        if not fields or fields[0] != "gpu":
            continue
        if len(fields) == 1:
            total += 1
        elif fields[-1].isdigit():
            total += int(fields[-1])
        else:
            total += 1  # gpu:type with no count
    return total


RESOURCE_FLAGS = {
    "partition": "--partition",
    "account": "--account",
    "qos": "--qos",
    "time": "--time",
    "mem": "--mem",
    "cpus_per_task": "--cpus-per-task",
    "ntasks": "--ntasks",
    "nodes": "--nodes",
    "gres": "--gres",
    "constraint": "--constraint",
}
# resource values become sbatch arguments; keep them to plain tokens
SAFE_RESOURCE_VALUE = re.compile(r"[A-Za-z0-9_.:,=+@/-]{1,200}")


class ResourceLimits:
    """
    Caps applied to every template job. `None` means no cap (for the
    allow-lists: any value is accepted).
    """

    DEFAULTS = {
        "partitions": None,
        "accounts": None,
        "qos": None,
        "max_time": "1-00:00:00",
        "max_mem": "128G",
        "max_cpus": 32,
        "max_nodes": 1,
        "max_gpus": 0,
        "max_concurrent_jobs": 4,
        "max_array_tasks": 1000,
    }

    def __init__(self, **limits):
        unknown = set(limits) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(f"unknown limits {sorted(unknown)}; known: {sorted(self.DEFAULTS)}")
        self.limits = dict(self.DEFAULTS, **limits)
        # fail at startup, not on the first request
        if self.limits["max_time"] is not None:
            parse_slurm_time(self.limits["max_time"])
        if self.limits["max_mem"] is not None:
            parse_mem(self.limits["max_mem"])

    def __getitem__(self, key):
        return self.limits[key]

    def to_json(self):
        return dict(self.limits)

    def violations(self, resources):
        out = []
        lim = self.limits
        for key, allowed in (("partition", "partitions"), ("account", "accounts"), ("qos", "qos")):
            if lim[allowed] is not None:
                value = resources.get(key)
                if value is None:
                    out.append(f"`{key}` must be set to one of {lim[allowed]}")
                elif value not in lim[allowed]:
                    out.append(f"{key} {value!r} is not one of {lim[allowed]}")
        if "time" not in resources:
            out.append("`time` must be set")
        else:
            try:
                minutes = parse_slurm_time(resources["time"])
                if lim["max_time"] is not None and minutes > parse_slurm_time(lim["max_time"]):
                    out.append(f"time {resources['time']} exceeds the limit of {lim['max_time']}")
            except ValueError as e:
                out.append(str(e))
        if "mem" in resources and lim["max_mem"] is not None:
            try:
                if parse_mem(resources["mem"]) > parse_mem(lim["max_mem"]):
                    out.append(f"mem {resources['mem']} exceeds the limit of {lim['max_mem']}")
            except ValueError as e:
                out.append(str(e))
        ints = {}
        for key in ("cpus_per_task", "ntasks", "nodes"):
            if key in resources:
                try:
                    ints[key] = int(resources[key])
                    if ints[key] < 1:
                        raise ValueError
                except ValueError:
                    out.append(f"{key} must be a positive integer, got {resources[key]!r}")
        cpus = ints.get("cpus_per_task", 1) * ints.get("ntasks", 1)
        if lim["max_cpus"] is not None and cpus > lim["max_cpus"]:
            out.append(f"{cpus} CPUs (cpus_per_task x ntasks) exceeds the limit of {lim['max_cpus']}")
        if lim["max_nodes"] is not None and ints.get("nodes", 1) > lim["max_nodes"]:
            out.append(f"nodes {ints['nodes']} exceeds the limit of {lim['max_nodes']}")
        gpus = parse_gres_gpus(resources.get("gres"))
        if lim["max_gpus"] is not None and gpus > lim["max_gpus"]:
            out.append(f"{gpus} GPUs exceeds the limit of {lim['max_gpus']}")
        return out

    def check(self, resources):
        problems = self.violations(resources)
        if problems:
            raise RESTError(422, "requested resources are outside the server's limits",
                            violations=problems, limits=self.to_json())


################################################################################
##
##  Templates
##

class JobTemplate:

    NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
    PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
    MODULE_RE = re.compile(r"[A-Za-z0-9_.+-][A-Za-z0-9_.+/-]{0,127}")
    PARAM_TYPES = ("string", "integer", "number", "boolean", "path")
    PARAM_KEYS = {
        "string": {"enum", "pattern", "max_length"},
        "integer": {"minimum", "maximum", "enum"},
        "number": {"minimum", "maximum"},
        "boolean": set(),
        "path": {"must_exist", "kind"},
    }
    SPEC_KEYS = {"description", "parameters", "resources", "overridable", "workdir", "modules", "array"}
    ARRAY_KEYS = {"task_parameters", "max_tasks"}
    DEFAULT_OVERRIDABLE = ("time", "mem")
    ENV_PREFIX = "HPC_PARAM_"
    TASK_ENV_PREFIX = "HPC_TASK_"
    GUIDE_FILE = "guide.md"
    MAX_GUIDE = 256 << 10
    MAX_SCRIPT = 64 << 10

    def __init__(self, name, spec, script, source=None):
        self.name = name
        self.spec = spec
        self.script = script
        self.source = source
        self.validate_spec()

    @classmethod
    def load(cls, directory):
        name = os.path.basename(os.path.normpath(directory))
        with open(os.path.join(directory, "template.json")) as f:
            spec = json.load(f)
        with open(os.path.join(directory, "script.sh")) as f:
            script = f.read()
        return cls(name, spec, script, source=directory)

    @classmethod
    def _validate_param_specs(cls, params, where="parameter"):
        if not isinstance(params, dict):
            raise ValueError(f"{where}s must be an object")
        for pname, pspec in params.items():
            if not cls.PARAM_NAME_RE.fullmatch(pname):
                raise ValueError(f"invalid {where} name {pname!r}")
            if not isinstance(pspec, dict):
                raise ValueError(f"{where} {pname}: must be an object")
            ptype = pspec.get("type")
            if ptype not in cls.PARAM_TYPES:
                raise ValueError(f"{where} {pname}: type must be one of {cls.PARAM_TYPES}")
            allowed = cls.PARAM_KEYS[ptype] | {"type", "description", "required", "default"}
            extra = set(pspec) - allowed
            if extra:
                raise ValueError(f"{where} {pname}: unknown keys {sorted(extra)}")
            if "pattern" in pspec:
                re.compile(pspec["pattern"])
            if pspec.get("kind", "any") not in ("file", "directory", "any"):
                raise ValueError(f"{where} {pname}: kind must be file, directory, or any")

    def validate_spec(self):
        if not self.NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid template name {self.name!r}")
        spec = self.spec
        if not isinstance(spec, dict):
            raise ValueError("template.json must contain an object")
        unknown = set(spec) - self.SPEC_KEYS
        if unknown:
            raise ValueError(f"unknown template keys {sorted(unknown)}")
        if not isinstance(spec.get("description"), str) or not spec["description"].strip():
            raise ValueError("`description` is required; it is what a client reads to choose a template")
        self._validate_param_specs(spec.setdefault("parameters", {}))
        resources = spec.setdefault("resources", {})
        if not isinstance(resources, dict):
            raise ValueError("`resources` must be an object")
        for key in list(resources) + list(spec.get("overridable", [])):
            if key not in RESOURCE_FLAGS:
                raise ValueError(f"unknown resource {key!r}; known: {sorted(RESOURCE_FLAGS)}")
        spec.setdefault("overridable", list(self.DEFAULT_OVERRIDABLE))
        modules = spec.setdefault("modules", [])
        if not isinstance(modules, list) or not all(isinstance(m, str) and self.MODULE_RE.fullmatch(m)
                                                    for m in modules):
            raise ValueError("`modules` must be a list of module names like \"ORCA/5.0.4\"")
        array = spec.get("array")
        if array is not None:
            if not isinstance(array, dict) or set(array) - self.ARRAY_KEYS:
                raise ValueError(f"`array` must be an object with keys from {sorted(self.ARRAY_KEYS)}")
            self._validate_param_specs(array.setdefault("task_parameters", {}), where="task parameter")
            if not array["task_parameters"]:
                raise ValueError("an array template needs at least one task parameter")
            overlap = set(array["task_parameters"]) & set(spec["parameters"])
            if overlap:
                raise ValueError(f"names used as both parameters and task parameters: {sorted(overlap)}")
            max_tasks = array.get("max_tasks")
            if max_tasks is not None and (not isinstance(max_tasks, int) or max_tasks < 1):
                raise ValueError("`array.max_tasks` must be a positive integer")
        if len(self.script) > self.MAX_SCRIPT:
            raise ValueError(f"script.sh is larger than {self.MAX_SCRIPT} bytes")
        if re.search(r"^\s*#SBATCH", self.script, re.M):
            raise ValueError("script.sh must not contain #SBATCH lines; set resources in template.json")

    @property
    def is_array(self):
        return self.spec.get("array") is not None

    @property
    def guide_path(self):
        if self.source is None:
            return None
        path = os.path.join(self.source, self.GUIDE_FILE)
        return path if os.path.isfile(path) else None

    def describe(self):
        out = {
            "name": self.name,
            "description": self.spec["description"],
            "parameters": self.parameter_schema(),
            "resources": self.spec["resources"],
            "overridable_resources": self.spec["overridable"],
            "modules": self.spec["modules"],
            "array": None,
            "has_guide": self.guide_path is not None,
        }
        if self.is_array:
            out["array"] = {
                "task_parameters": self.parameter_schema(self.spec["array"]["task_parameters"]),
                "max_tasks": self.spec["array"].get("max_tasks"),
            }
        return out

    def parameter_schema(self, specs=None):
        """Parameters (or task parameters) as a JSON Schema object."""
        if specs is None:
            specs = self.spec["parameters"]
        props, required = {}, []
        type_map = {"string": "string", "integer": "integer", "number": "number",
                    "boolean": "boolean", "path": "string"}
        for pname, p in specs.items():
            prop = {"type": type_map[p["type"]]}
            desc = p.get("description", "")
            if p["type"] == "path":
                kind = p.get("kind", "any")
                desc = (desc + " " if desc else "") + (
                    f"(a path on the cluster{'' if kind == 'any' else ' to a ' + kind}"
                    f"{', must exist' if p.get('must_exist', True) else ''})"
                )
            if desc:
                prop["description"] = desc
            for key in ("enum", "minimum", "maximum", "default", "pattern"):
                if key in p:
                    prop[key] = p[key]
            if "max_length" in p:
                prop["maxLength"] = p["max_length"]
            props[pname] = prop
            if p.get("required", "default" not in p):
                required.append(pname)
        schema = {"type": "object", "properties": props, "additionalProperties": False}
        if required:
            schema["required"] = required
        return schema

    def _check_values(self, values_in, specs, whitelist, where=""):
        if values_in is None:
            values_in = {}
        if not isinstance(values_in, dict):
            raise RESTError(400, f"{where}`params` must be an object")
        problems = [f"{where}unknown parameter {k!r}" for k in values_in if k not in specs]
        values = {}
        for pname, p in specs.items():
            if pname not in values_in:
                if "default" in p:
                    values[pname] = p["default"]
                elif p.get("required", True):
                    problems.append(f"{where}missing required parameter {pname!r}")
                continue
            try:
                values[pname] = self._check_value(pname, p, values_in[pname], whitelist)
            except ValueError as e:
                problems.append(f"{where}{e}")
        return values, problems

    def validate_params(self, params, whitelist):
        values, problems = self._check_values(params, self.spec["parameters"], whitelist)
        if problems:
            raise RESTError(422, f"invalid parameters for template {self.name}",
                            violations=problems, parameters=self.parameter_schema())
        return values

    def validate_tasks(self, tasks, whitelist, max_tasks):
        """`tasks`: a list of (task_params, task_whitelist) pairs."""
        if self.spec["array"].get("max_tasks"):
            max_tasks = min(max_tasks, self.spec["array"]["max_tasks"]) if max_tasks else \
                self.spec["array"]["max_tasks"]
        if not tasks:
            raise RESTError(422, f"template {self.name} runs one job per task; give `tasks` or `tasks_from`")
        if max_tasks is not None and len(tasks) > max_tasks:
            raise RESTError(422, f"{len(tasks)} tasks exceeds the limit of {max_tasks}")
        specs = self.spec["array"]["task_parameters"]
        values, problems = [], []
        for i, (task, task_whitelist) in enumerate(tasks):
            v, p = self._check_values(task, specs, task_whitelist or whitelist, where=f"task {i}: ")
            values.append(v)
            problems.extend(p)
        if problems:
            shown = problems[:25]
            raise RESTError(422, f"invalid tasks for template {self.name}", violations=shown,
                            more_violations=len(problems) - len(shown),
                            task_parameters=self.parameter_schema(specs))
        return values

    @staticmethod
    def _check_value(pname, p, value, whitelist):
        ptype = p["type"]
        if ptype == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{pname} must be true or false")
            return value
        if ptype in ("integer", "number"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or (
                    ptype == "integer" and not isinstance(value, int)):
                raise ValueError(f"{pname} must be an {ptype}")
            if "minimum" in p and value < p["minimum"]:
                raise ValueError(f"{pname} must be >= {p['minimum']}")
            if "maximum" in p and value > p["maximum"]:
                raise ValueError(f"{pname} must be <= {p['maximum']}")
            if "enum" in p and value not in p["enum"]:
                raise ValueError(f"{pname} must be one of {p['enum']}")
            return value
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"{pname} must be a string")
        if ptype == "string":
            if len(value) > p.get("max_length", 1024):
                raise ValueError(f"{pname} is longer than {p.get('max_length', 1024)} characters")
            if "enum" in p and value not in p["enum"]:
                raise ValueError(f"{pname} must be one of {p['enum']}")
            if "pattern" in p and not re.fullmatch(p["pattern"], value):
                raise ValueError(f"{pname} must match {p['pattern']}")
            return value
        # path
        try:
            _, real = whitelist.resolve(value)
        except PermissionError as e:
            raise ValueError(f"{pname}: {e}")
        kind = p.get("kind", "any")
        if p.get("must_exist", True) and not os.path.exists(real):
            raise ValueError(f"{pname}: {real} does not exist")
        if os.path.exists(real):
            if kind == "file" and not os.path.isfile(real):
                raise ValueError(f"{pname}: {real} is not a file")
            if kind == "directory" and not os.path.isdir(real):
                raise ValueError(f"{pname}: {real} is not a directory")
        return real

    def resources(self, values, overrides=None):
        overrides = overrides or {}
        if not isinstance(overrides, dict):
            raise RESTError(400, "`resources` must be an object")
        problems = []
        for key in overrides:
            if key not in RESOURCE_FLAGS:
                problems.append(f"unknown resource {key!r}")
            elif key not in self.spec["overridable"]:
                problems.append(f"template {self.name} does not allow overriding {key!r}; "
                                f"overridable: {self.spec['overridable']}")
        merged = {}
        str_values = {k: self._env_value(v) for k, v in values.items()}
        for key, value in dict(self.spec["resources"], **overrides).items():
            if key not in RESOURCE_FLAGS:
                continue
            value = str(value)
            if key in self.spec["resources"] and key not in overrides:
                # template defaults may refer to parameters, e.g. "${nprocs}"
                try:
                    value = string.Template(value).substitute(str_values)
                except (KeyError, ValueError) as e:
                    problems.append(f"resource {key}: bad parameter reference {e}")
                    continue
            if not SAFE_RESOURCE_VALUE.fullmatch(value):
                problems.append(f"resource {key} has an invalid value {value!r}")
                continue
            merged[key] = value
        if problems:
            raise RESTError(422, "invalid resources", violations=problems)
        return merged

    def workdir(self, values):
        spec = self.spec.get("workdir")
        if spec is None:
            return None
        str_values = {k: self._env_value(v) for k, v in values.items()}
        try:
            return string.Template(spec).substitute(str_values)
        except (KeyError, ValueError) as e:
            raise RESTError(500, f"template {self.name} has a bad workdir reference {e}")

    @staticmethod
    def _env_value(value):
        if isinstance(value, bool):
            return "1" if value else "0"
        return str(value)

    def _exports(self, prefix, values, indent=""):
        return [f"{indent}export {prefix}{k.upper()}={shlex.quote(self._env_value(values[k]))}"
                for k in sorted(values)]

    MODULE_INIT = (
        "if ! type module >/dev/null 2>&1; then\n"
        "  for f in /etc/profile.d/lmod.sh /etc/profile.d/modules.sh /usr/share/lmod/lmod/init/bash; do\n"
        "    if [ -f \"$f\" ]; then . \"$f\"; break; fi\n"
        "  done\n"
        "fi"
    )

    def render(self, values, tasks=None, sandbox: 'rest_sandbox.Sandbox' = None, writable=()):
        """
        The full job script: shebang, exports, module loads, per-task
        values, then the body - which runs in `sandbox`, with `writable`
        directories, when one is configured. Returns (script, sandbox plan).
        """
        body = self.script
        shebang = "#!/bin/bash"
        if body.startswith("#!"):
            shebang, _, body = body.partition("\n")
        lines = [
            shebang,
            f"# generated by hpclib rest_server from template {self.name}",
            f"export HPC_REST_TEMPLATE={shlex.quote(self.name)}",
            f"export HPC_REST_PARAMS_JSON={shlex.quote(json.dumps(values, sort_keys=True))}",
        ] + self._exports(self.ENV_PREFIX, values)
        if self.spec["modules"]:
            # Module discovery runs in a login shell (module_command), and some
            # clusters only put their module trees on MODULEPATH there, so the job
            # gets a login shell too; otherwise a module found by search_modules
            # can be "unknown" inside the job.
            if lines[0].strip() in ("#!/bin/bash", "#!/usr/bin/env bash"):
                lines[0] = "#!/bin/bash -l"
            lines.append(self.MODULE_INIT)
            lines += [
                f"module load {shlex.quote(m)} || {{ echo {shlex.quote(f'hpclib: could not load module {m}; check the template modules with search_modules')} >&2; exit 3; }}"
                for m in self.spec["modules"]
            ]
        if tasks is not None:
            lines.append('case "${SLURM_ARRAY_TASK_ID:-}" in')
            for i, task in enumerate(tasks):
                lines.append(f"  {i})")
                lines += self._exports(self.TASK_ENV_PREFIX, task, indent="    ")
                lines.append(f"    export HPC_TASK_JSON={shlex.quote(json.dumps(task, sort_keys=True))}")
                lines.append("    ;;")
            lines += [
                '  *) echo "hpclib: no task ${SLURM_ARRAY_TASK_ID:-?} in this batch" >&2; exit 2 ;;',
                "esac",
                'export HPC_TASK_INDEX="$SLURM_ARRAY_TASK_ID"',
            ]
        plan = {"method": "none", "reason": "not sandboxed"}
        if sandbox is not None:
            launch, plan = sandbox.launch(shebang, body, writable)
            if launch is not None:
                if lines[0].strip() == shebang.strip() and not lines[0].startswith("#!/bin/bash"):
                    lines[0] = "#!/bin/bash"   # the launcher is bash, whatever the body is
                return "\n".join(lines) + "\n\n" + "\n".join(launch) + "\n", plan
        return "\n".join(lines) + "\n\n" + body, plan


def _guide_summary(text, limit=300):
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(line.strip() for line in para.splitlines()).strip()
        if para and not para.startswith("#"):
            return para[:limit]
    return ""


class TemplateStore:
    """Templates are re-read on every call so edits apply without a restart."""

    def __init__(self, directory):
        self.directory = os.path.realpath(os.path.expanduser(directory)) if directory else None

    def load(self):
        """`(templates, guides, errors)`; guides are directories with only a guide.md."""
        templates, guides, errors = {}, {}, {}
        if self.directory is None or not os.path.isdir(self.directory):
            return templates, guides, errors
        for name in sorted(os.listdir(self.directory)):
            path = os.path.join(self.directory, name)
            if name.startswith(".") or not os.path.isdir(path):
                continue
            if not os.path.exists(os.path.join(path, "template.json")):
                guide = os.path.join(path, JobTemplate.GUIDE_FILE)
                if os.path.isfile(guide) and JobTemplate.NAME_RE.fullmatch(name):
                    with open(guide, errors="replace") as f:
                        guides[name] = {"name": name, "summary": _guide_summary(f.read(4096)),
                                        "kind": "guide"}
                continue
            try:
                templates[name] = JobTemplate.load(path)
            except (OSError, ValueError) as e:
                errors[name] = str(e)
        return templates, guides, errors

    def get(self, name):
        templates, guides, errors = self.load()
        if name in templates:
            return templates[name]
        if name in errors:
            raise RESTError(500, f"template {name} failed to load: {errors[name]}")
        if name in guides:
            raise RESTError(422, f"{name} is a planning guide, not a job template; read it with the guide route")
        raise RESTError(404, f"unknown template {name!r}", available=sorted(templates))

    def guide(self, name):
        """A template's (or a planning guide's) guide.md, plus any example submissions."""
        if not isinstance(name, str) or not JobTemplate.NAME_RE.fullmatch(name) or self.directory is None:
            raise RESTError(400, f"invalid name {name!r}")
        path = os.path.join(self.directory, name)
        guide_file = os.path.join(path, JobTemplate.GUIDE_FILE)
        if not os.path.isdir(path):
            raise RESTError(404, f"no template or guide named {name!r}")
        out = {"name": name, "is_template": os.path.exists(os.path.join(path, "template.json")),
               "guide": None, "examples": {}}
        if os.path.isfile(guide_file):
            with open(guide_file, errors="replace") as f:
                out["guide"] = f.read(JobTemplate.MAX_GUIDE)
        examples = os.path.join(path, "examples")
        if os.path.isdir(examples):
            for fname in sorted(os.listdir(examples))[:20]:
                if fname.endswith(".json"):
                    try:
                        with open(os.path.join(examples, fname)) as f:
                            out["examples"][fname] = json.load(f)
                    except (OSError, ValueError) as e:
                        out["examples"][fname] = {"error": str(e)}
        if out["guide"] is None and not out["examples"]:
            raise RESTError(404, f"{name!r} has no guide.md or examples")
        return out


class ProposalStore:
    """
    Templates proposed by clients, waiting for the owner to approve them.
    Each proposal is validated exactly as a real template would be, then
    written to `directory/NAME/` with a proposal.json note. Approving
    moves it into the templates directory; nothing in here ever runs.
    """

    META_FILE = "proposal.json"
    MAX_RATIONALE = 4096

    def __init__(self, directory, templates: TemplateStore):
        self.directory = os.path.realpath(os.path.expanduser(directory))
        self.templates = templates

    def propose(self, token_name, request):
        if not isinstance(request, dict):
            raise RESTError(400, "request body must be an object")
        allowed = {"name", "template", "script", "guide", "rationale"}
        unknown = set(request) - allowed
        if unknown:
            raise RESTError(400, f"unknown fields {sorted(unknown)}", allowed=sorted(allowed))
        name, spec, script = request.get("name"), request.get("template"), request.get("script")
        guide, rationale = request.get("guide"), request.get("rationale", "")
        if not isinstance(script, str) or not isinstance(spec, dict) or not isinstance(name, str):
            raise RESTError(400, "`name` (string), `template` (object) and `script` (string) are required")
        if guide is not None and (not isinstance(guide, str) or len(guide) > JobTemplate.MAX_GUIDE):
            raise RESTError(400, f"`guide` must be a string of at most {JobTemplate.MAX_GUIDE} bytes")
        if not isinstance(rationale, str) or len(rationale) > self.MAX_RATIONALE:
            raise RESTError(400, f"`rationale` must be a string of at most {self.MAX_RATIONALE} characters")
        try:
            JobTemplate(name, copy.deepcopy(spec), script)
        except ValueError as e:
            raise RESTError(422, f"invalid template: {e}")
        target = os.path.join(self.directory, name)
        revised = False
        if os.path.exists(target):
            earlier = self._meta(name)
            if earlier is None or earlier.get("token") != token_name:
                raise RESTError(409, f"a proposal named {name!r} from another client is already waiting for review")
            # a client may revise its own pending proposal; the earlier draft is kept for the reviewer
            os.makedirs(os.path.join(self.directory, ".superseded"), mode=0o700, exist_ok=True)
            os.rename(target, os.path.join(self.directory, ".superseded", f"{name}-{int(time.time() * 1000)}"))
            revised = True
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        staging = target + ".tmp"
        os.makedirs(staging)
        with open(os.path.join(staging, "template.json"), "w") as f:
            json.dump(spec, f, indent=2)
        with open(os.path.join(staging, "script.sh"), "w") as f:
            f.write(script)
        if guide:
            with open(os.path.join(staging, JobTemplate.GUIDE_FILE), "w") as f:
                f.write(guide)
        templates, guides, _ = self.templates.load()
        meta = {"name": name, "token": token_name, "proposed": time.time(), "rationale": rationale,
                "replaces_existing": name in templates or name in guides, "revised": revised}
        with open(os.path.join(staging, self.META_FILE), "w") as f:
            json.dump(meta, f, indent=2)
        os.rename(staging, target)
        return 201, dict(meta, status="waiting for the cluster owner to review and approve")

    def _meta(self, name):
        try:
            with open(os.path.join(self.directory, name, self.META_FILE)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def list(self):
        out = []
        if not os.path.isdir(self.directory):
            return out
        for name in sorted(os.listdir(self.directory)):
            if name.startswith("."):
                continue
            meta = os.path.join(self.directory, name, self.META_FILE)
            if os.path.isfile(meta):
                with open(meta) as f:
                    out.append(json.load(f))
        return out

    def approve(self, name, replace=False, approved_by="owner"):
        source = os.path.join(self.directory, name)
        if not JobTemplate.NAME_RE.fullmatch(name) or not os.path.isdir(source):
            raise ValueError(f"no proposal named {name!r}")
        JobTemplate.load(source)  # still valid?
        target = os.path.join(self.templates.directory, name)
        if os.path.exists(target):
            if not replace:
                raise ValueError(f"template {name!r} already exists; pass --replace to swap it in")
            # replaced templates go in a hidden directory, which the server doesn't load
            backups = os.path.join(self.templates.directory, ".replaced")
            os.makedirs(backups, exist_ok=True)
            os.rename(target, os.path.join(backups, f"{name}-{time.strftime('%Y%m%dT%H%M%S')}"))
        os.makedirs(self.templates.directory, exist_ok=True)
        meta = self._meta(name) or {}
        meta.update(approved=time.time(), approved_by=approved_by)
        os.remove(os.path.join(source, self.META_FILE))
        with open(os.path.join(source, ".proposal.json"), "w") as f:   # who proposed and approved it
            json.dump(meta, f, indent=2)
        shutil.move(source, target)
        return target

    def reject(self, name, reason=None, rejected_by="owner"):
        """Set the proposal aside in `.rejected/`, with who rejected it and why."""
        source = os.path.join(self.directory, name)
        if not JobTemplate.NAME_RE.fullmatch(name) or not os.path.isdir(source):
            raise ValueError(f"no proposal named {name!r}")
        if reason is not None and (not isinstance(reason, str) or len(reason) > self.MAX_RATIONALE):
            raise ValueError(f"`reason` must be a string of at most {self.MAX_RATIONALE} characters")
        meta = self._meta(name) or {"name": name}
        meta.update(rejected=time.time(), rejected_by=rejected_by, reason=reason or "")
        with open(os.path.join(source, self.META_FILE), "w") as f:
            json.dump(meta, f, indent=2)
        rejected = os.path.join(self.directory, ".rejected")
        os.makedirs(rejected, mode=0o700, exist_ok=True)
        os.rename(source, os.path.join(rejected, f"{name}-{int(time.time() * 1000)}"))
        return meta

    REVIEW_FILES = ("template.json", "script.sh", JobTemplate.GUIDE_FILE)
    MAX_REVIEW_FILE = 256 << 10

    @classmethod
    def _read_review_files(cls, directory):
        out = {}
        for fname in cls.REVIEW_FILES:
            path = os.path.join(directory, fname)
            if os.path.isfile(path):
                with open(path, errors="replace") as f:
                    out[fname] = f.read(cls.MAX_REVIEW_FILE)
        return out

    def detail(self, name):
        """
        A proposal with its files and, when it would replace a template, the
        current template's files and a unified diff of each file.
        """
        import difflib
        source = os.path.join(self.directory, name)
        meta = self._meta(name) if JobTemplate.NAME_RE.fullmatch(name or "") else None
        if meta is None:
            raise ValueError(f"no proposal named {name!r}")
        proposed = self._read_review_files(source)
        current_dir = os.path.join(self.templates.directory, name) if self.templates.directory else None
        current = self._read_review_files(current_dir) if current_dir and os.path.isdir(current_dir) else None
        diffs = {}
        for fname in self.REVIEW_FILES:
            old, new = (current or {}).get(fname), proposed.get(fname)
            if old == new or (old is None and new is None):
                continue
            diffs[fname] = "".join(difflib.unified_diff(
                (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
                fromfile=f"templates/{name}/{fname}" if old is not None else "/dev/null",
                tofile=f"proposals/{name}/{fname}" if new is not None else "/dev/null"))
        try:
            JobTemplate(name, json.loads(proposed.get("template.json", "null")), proposed.get("script.sh", ""))
            valid, error = True, None
        except (ValueError, TypeError) as e:
            valid, error = False, str(e)
        return {"proposal": meta, "files": proposed, "current": current, "diff": diffs,
                "valid": valid, "error": error}


################################################################################
##
##  Job registry
##

class JobRegistry:
    """sqlite record of every job (and job array) submitted through the API."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        token TEXT NOT NULL,
        template TEXT NOT NULL,
        params TEXT NOT NULL,
        resources TEXT NOT NULL,
        workdir TEXT NOT NULL,
        output TEXT NOT NULL,
        job_name TEXT NOT NULL,
        idempotency_key TEXT,
        submitted REAL NOT NULL,
        state TEXT NOT NULL,
        updated REAL NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency
        ON jobs(token, idempotency_key) WHERE idempotency_key IS NOT NULL;
    """
    # added after the first release; created on open if missing
    EXTRA_COLUMNS = {
        "array_size": "INTEGER",
        "throttle": "INTEGER",
        "tasks": "TEXT",
        "task_states": "TEXT",
        "label": "TEXT",
    }
    JSON_COLUMNS = ("params", "resources", "tasks", "task_states")

    def __init__(self, path):
        self.path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), mode=0o700, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript(self.SCHEMA)
            have = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            for column, kind in self.EXTRA_COLUMNS.items():
                if column not in have:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {kind}")
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _row(self, row):
        if row is None:
            return None
        job = dict(row)
        for key in self.JSON_COLUMNS:
            if job.get(key) is not None:
                job[key] = json.loads(job[key])
        return job

    def record(self, job):
        job = dict(job)
        for key in self.JSON_COLUMNS:
            if job.get(key) is not None:
                job[key] = json.dumps(job[key], sort_keys=True)
        columns = ", ".join(job)
        marks = ", ".join("?" for _ in job)
        with closing(self.connect()) as db, db:
            db.execute(f"INSERT INTO jobs ({columns}) VALUES ({marks})", list(job.values()))

    def get(self, job_id):
        with closing(self.connect()) as db:
            return self._row(db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone())

    def find_idempotent(self, token, key):
        with closing(self.connect()) as db:
            return self._row(db.execute(
                "SELECT * FROM jobs WHERE token = ? AND idempotency_key = ?", (token, key)).fetchone())

    def list(self, token=None, limit=50, active_only=False, label=None):
        query, args = "SELECT * FROM jobs", []
        where = []
        if token is not None:
            where.append("token = ?")
            args.append(token)
        if label is not None:
            where.append("label = ?")
            args.append(label)
        if active_only:
            marks = ", ".join("?" for _ in SlurmRunner.TERMINAL_STATES)
            where.append(f"state NOT IN ({marks})")
            args.extend(sorted(SlurmRunner.TERMINAL_STATES))
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY submitted DESC LIMIT ?"
        args.append(int(limit))
        with closing(self.connect()) as db:
            return [self._row(r) for r in db.execute(query, args).fetchall()]

    def update_states(self, states, task_states=None):
        now = time.time()
        task_states = task_states or {}
        with closing(self.connect()) as db, db:
            db.executemany("UPDATE jobs SET state = ?, updated = ? WHERE job_id = ? AND state != ?",
                           [(s, now, j, s) for j, s in states.items()])
            db.executemany("UPDATE jobs SET task_states = ?, updated = ? WHERE job_id = ?",
                           [(json.dumps(t, sort_keys=True), now, j) for j, t in task_states.items()])


################################################################################
##
##  Talking to SLURM
##

class SlurmRunner:

    TERMINAL_STATES = frozenset({
        "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL",
        "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED", "SPECIAL_EXIT",
    })
    # states of a job that hasn't started (yet, or again)
    WAITING_STATES = frozenset({"PENDING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "RESV_DEL_HOLD"})
    SQUEUE_FIELDS = ("job_id", "name", "state", "elapsed", "time_limit", "reason", "partition", "nodes")
    SQUEUE_FORMAT = "%i|%j|%T|%M|%l|%R|%P|%N"
    SACCT_FIELDS = ("job_id", "name", "state", "exit_code", "elapsed", "start", "end",
                    "partition", "nodes", "reason")
    SACCT_FORMAT = "JobID,JobName,State,ExitCode,Elapsed,Start,End,Partition,NodeList,Reason"
    TASK_FIELDS = ("state", "exit_code", "elapsed", "reason", "nodes")

    def __init__(self, timeout=120, user=None):
        self.timeout = timeout
        self.user = user or getpass.getuser()

    def run(self, args, input=None, cwd=None):
        try:
            return subprocess.run(
                args, input=input, cwd=cwd, capture_output=True, text=True, timeout=self.timeout,
                stdin=subprocess.DEVNULL if input is None else None, env=clean_env(),
            )
        except FileNotFoundError:
            raise RESTError(503, f"`{args[0]}` not found on PATH")
        except subprocess.TimeoutExpired as e:
            raise RESTError(504, f"`{args[0]}` timed out after {e.timeout}s")

    @staticmethod
    def normalize_state(state):
        state = (state or "").strip().split(" ")[0].rstrip("+").upper()
        return state or "UNKNOWN"

    def _parse(self, text, fields):
        rows = []
        for line in text.splitlines():
            if not line.strip():
                continue
            values = line.split("|")
            if len(values) < len(fields):
                values += [""] * (len(fields) - len(values))
            rows.append(dict(zip(fields, values)))
        return rows

    @staticmethod
    def _array_tasks(suffix):
        """`5` -> [5]; `[3-5,9%4]` -> [3, 4, 5, 9]."""
        if suffix.isdigit():
            return [int(suffix)]
        spec = suffix.strip("[]").split("%")[0]
        out = []
        for part in spec.split(","):
            if "-" in part:
                lo, hi = part.split("-", 1)
                if lo.isdigit() and hi.isdigit():
                    out.extend(range(int(lo), int(hi) + 1))
            elif part.isdigit():
                out.append(int(part))
        return out

    def _collect(self, rows, singles, arrays, source, result, tasks):
        for row in rows:
            job_id = row["job_id"]
            base, _, suffix = job_id.partition("_")
            row["state"] = self.normalize_state(row["state"])
            if base in arrays and suffix:
                for t in self._array_tasks(suffix):
                    if t < arrays[base] and t not in tasks[base]:
                        tasks[base][t] = {k: row.get(k, "") for k in self.TASK_FIELDS}
            elif job_id in singles and job_id not in result:
                row["source"] = source
                result[job_id] = row

    def query_states(self, job_ids, arrays=None):
        """
        job_id -> status dict, from squeue for live jobs and sacct for
        finished ones. `arrays` maps array job ids to their task count;
        their status carries per-task states and an overall state.
        """
        arrays = {str(k): int(v) for k, v in (arrays or {}).items()}
        singles = [str(j) for j in job_ids if str(j) not in arrays]
        result, tasks = {}, {a: {} for a in arrays}
        if not singles and not arrays:
            return result
        res = self.run(["squeue", "-h", "-r", "-u", self.user, "-o", self.SQUEUE_FORMAT])
        if res.returncode == 0:
            self._collect(self._parse(res.stdout, self.SQUEUE_FIELDS), singles, arrays, "squeue", result, tasks)
        missing = [j for j in singles if j not in result]
        unfinished = [a for a in arrays if len(tasks[a]) < arrays[a]]
        if missing or unfinished:
            try:
                res = self.run(["sacct", "-X", "-P", "-n", "-j", ",".join(missing + unfinished),
                                "-o", self.SACCT_FORMAT])
            except RESTError:
                res = None  # no accounting on this cluster
            if res is not None and res.returncode == 0:
                self._collect(self._parse(res.stdout, self.SACCT_FIELDS), singles, arrays, "sacct", result, tasks)
        for j in singles:
            result.setdefault(j, {"job_id": j, "state": "UNKNOWN", "source": None})
            result[j]["terminal"] = result[j]["state"] in self.TERMINAL_STATES
        for a, n in arrays.items():
            per_task = {t: tasks[a].get(t, {"state": "UNKNOWN"}) for t in range(n)}
            result[a] = dict(self.summarize(per_task), job_id=a, tasks=per_task)
        return result

    @classmethod
    def summarize(cls, per_task):
        counts = {}
        for t in per_task.values():
            counts[t["state"]] = counts.get(t["state"], 0) + 1
        active = {s for s in counts if s not in cls.TERMINAL_STATES and s != "UNKNOWN"}
        unfinished = set(counts) - {"COMPLETED"}
        if active:
            # COMPLETING, CONFIGURING, SUSPENDED, ... have started; only a job with
            # nothing past waiting is PENDING
            state = "PENDING" if active <= cls.WAITING_STATES else "RUNNING"
        elif "UNKNOWN" in counts:
            state = "UNKNOWN"
        elif not unfinished:
            state = "COMPLETED"
        elif unfinished == {"CANCELLED"}:
            state = "CANCELLED"  # e.g. cancelled part way through; see task_counts
        else:
            state = "FAILED"  # finished, but not every task completed; see task_counts
        failed = sorted(t for t, v in per_task.items()
                        if v["state"] in cls.TERMINAL_STATES and v["state"] != "COMPLETED")
        return {"state": state, "terminal": state in cls.TERMINAL_STATES, "task_counts": counts,
                "failed_tasks": failed}


class ClusterInfo:
    """What a client needs to plan jobs: partitions, accounts, versions. Cached briefly."""

    CACHE_SECONDS = 60
    SINFO_FIELDS = ("partition", "available", "time_limit", "nodes", "cpus", "memory_mb", "gres")
    SINFO_FORMAT = "%P|%a|%l|%D|%c|%m|%G"

    def __init__(self, runner: SlurmRunner):
        self.runner = runner
        self._cache = None
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            if self._cache is not None and time.time() - self._cache[0] < self.CACHE_SECONDS:
                return self._cache[1]
            info = self._collect()
            self._cache = (time.time(), info)
            return info

    def _try(self, args):
        try:
            res = self.runner.run(args)
        except RESTError:
            return None
        return res.stdout if res.returncode == 0 else None

    def _collect(self):
        info = {"user": self.runner.user, "hostname": socket.gethostname()}
        version = self._try(["sinfo", "--version"])
        info["slurm_version"] = version.strip() if version else None
        partitions = {}
        out = self._try(["sinfo", "-h", "-o", self.SINFO_FORMAT]) or ""
        for row in self.runner._parse(out, self.SINFO_FIELDS):
            name = row["partition"]
            default = name.endswith("*")
            name = name.rstrip("*")
            part = partitions.setdefault(name, {
                "name": name, "default": default, "available": row["available"],
                "time_limit": row["time_limit"], "nodes": 0, "node_types": [],
            })
            try:
                count = int(row["nodes"])
            except ValueError:
                count = 0
            part["nodes"] += count
            part["node_types"].append({
                "count": count, "cpus": row["cpus"], "memory_mb": row["memory_mb"],
                "gres": None if row["gres"] in ("", "(null)") else row["gres"],
            })
        info["partitions"] = list(partitions.values())
        out = self._try(["sacctmgr", "-n", "-P", "show", "assoc", f"user={self.runner.user}",
                         "format=account,partition,qos"])
        if out is not None:
            info["associations"] = [
                dict(zip(("account", "partition", "qos"), line.split("|")))
                for line in out.splitlines() if line.strip()
            ]
        return info


class ModuleSystem:
    """
    Read-only queries of the cluster's environment modules (Lmod or Tcl
    modules): `module avail` and `module spider`. `module` is a shell
    function, so it runs through a login shell by default; set
    `module_command` in the config if your cluster needs something else.
    Queries are plain module names (no shell syntax) and results are
    cached, since spider in particular can be slow.
    """

    DEFAULT_COMMAND = ("bash", "-lc", 'module "$@" 2>&1', "hpclib-module")
    QUERY_RE = re.compile(r"[A-Za-z0-9_.+/-]{0,128}")
    CACHE_SECONDS = 600
    MAX_OUTPUT = 200_000
    MARKERS = re.compile(r"\s*(\((default|D|L|S|g|H|E)[^)]*\)|<[^>]*>)\s*")

    def __init__(self, runner: SlurmRunner, command=None):
        self.runner = runner
        self.command = list(command or self.DEFAULT_COMMAND)
        self._cache = {}
        self._lock = threading.Lock()

    def query(self, subcommand, query=""):
        if subcommand not in ("avail", "spider"):
            raise RESTError(400, f"unsupported module subcommand {subcommand!r}")
        query = query or ""
        if not self.QUERY_RE.fullmatch(query) or query.startswith("-"):
            raise RESTError(400, "module queries are module names, e.g. `orca` or `ORCA/5.0.4`")
        key = (subcommand, query)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < self.CACHE_SECONDS:
                return dict(hit[1], cached=True)
        # terse output lists names one per line; a detailed spider of one
        # name/version is the one that explains how to load it
        terse = not (subcommand == "spider" and "/" in query)
        args = self.command + (["-t"] if terse else []) + [subcommand] + ([query] if query else [])
        res = self.runner.run(args)
        text = (res.stdout or "") + (res.stderr or "")
        if res.returncode == 127 or re.search(r"module: (command )?not found", text):
            raise RESTError(503, "no `module` command on this cluster's login shell; set `module_command` in "
                                 "the REST server config if modules are set up some other way")
        out = {"subcommand": subcommand, "query": query, "returncode": res.returncode,
               "truncated": len(text) > self.MAX_OUTPUT, "cached": False}
        if terse:
            out["modules"] = self.parse_terse(text)
            if subcommand == "spider" and query:
                out["text"] = text[:self.MAX_OUTPUT]  # Lmod may have answered in detail
        else:
            out["text"] = text[:self.MAX_OUTPUT]
        with self._lock:
            self._cache[key] = (time.time(), out)
        return out

    NAME_RE = re.compile(r"[A-Za-z0-9_.+-]+/[A-Za-z0-9_.+/-]+")

    @classmethod
    def parse_terse(cls, text):
        """
        Module names from `module -t avail` / `module -t spider` output.
        Lmod falls back to its detailed layout when a spider query matches
        a single module, so this also reads `name: name/1.0, name/2.0`
        lines and `Versions:` blocks, and skips separator rules and prose.
        """
        modules, seen, location, in_versions, detailed = [], set(), None, False, False

        def add(name, default=False):
            if name and name not in seen:
                seen.add(name)
                modules.append({"name": name, "location": location, "default": default})

        for raw in text.splitlines():
            line = raw.strip()
            if not line or set(line) <= set("-=_ "):
                in_versions = False
                detailed = detailed or bool(line)  # separator rules only appear in Lmod's detailed layout
                continue
            if line.endswith(":") and not raw.startswith(" ") and not detailed:
                location = line[:-1]  # a modulepath header in avail output
                continue
            if line.lower() in ("versions:", "other possible modules matches:"):
                in_versions = True
                continue
            listing = re.fullmatch(r"([A-Za-z0-9_.+-]+): (.+)", line)
            if listing:
                for part in listing.group(2).split(","):
                    part = cls.MARKERS.sub("", part).strip()
                    if cls.NAME_RE.fullmatch(part):
                        add(part)
                continue
            default = bool(re.search(r"\((default|D)\)", line))
            name = cls.MARKERS.sub("", line).strip()
            if in_versions and cls.NAME_RE.fullmatch(name):
                add(name, default)
                continue
            in_versions = False
            if detailed or line.endswith("/") or line.endswith(":") or " " in name:
                continue  # prose, a bare family name, or a section heading
            add(name, default)
        return modules


################################################################################
##
##  Putting it together
##

class JobManager:

    SUBMIT_KEYS = {"template", "params", "resources", "workdir", "idempotency_key", "dry_run",
                   "tasks", "tasks_from", "throttle", "label"}
    TASKS_FROM_KEYS = {"path", "key", "fields", "select"}
    OUTPUT_PATTERN = "hpc-rest-%j.out"
    ARRAY_OUTPUT_PATTERN = "hpc-rest-%A_%a.out"
    MAX_WAIT = 300
    MAX_MANIFEST = 16 << 20

    def __init__(self, templates: TemplateStore, registry: JobRegistry, limits: ResourceLimits,
                 runner: SlurmRunner, cluster_notes=None, poll_interval=10, proposals: ProposalStore = None,
                 modules: ModuleSystem = None, sandbox: 'rest_sandbox.Sandbox' = None, auto_approve=None):
        self.templates = templates
        self.registry = registry
        self.limits = limits
        self.runner = runner
        self.cluster = ClusterInfo(runner)
        self.modules = modules or ModuleSystem(runner)
        self.proposals = proposals
        self.cluster_notes = cluster_notes
        self.poll_interval = poll_interval
        self.sandbox = sandbox or rest_sandbox.Sandbox(None)
        if auto_approve not in (None, "new", "all"):
            raise ValueError("auto_approve must be None, 'new' or 'all'")
        self.auto_approve = auto_approve
        self.submit_lock = threading.Lock()

    def describe_templates(self):
        templates, guides, errors = self.templates.load()
        out = {"templates": [t.describe() for t in templates.values()], "guides": list(guides.values())}
        if errors:
            out["errors"] = errors
        return out

    def guide(self, name):
        return self.templates.guide(name)

    def proposal_policy(self):
        """How proposals are approved on this server, for clients."""
        if not self.auto_approve:
            return {"review": "owner", "detail": "proposals wait until the cluster owner approves them"}
        if self.sandbox.describe().get("effective") != "singularity":
            return {"review": "owner", "detail": "the server would approve proposals automatically, but only "
                                                 "while template jobs are sandboxed, and they are not"}
        return {"review": "automatic", "replace_existing": self.auto_approve == "all",
                "detail": "valid proposals become templates immediately" + (
                    "" if self.auto_approve == "all" else "; one that would replace an existing template still "
                                                          "waits for the owner")}

    def propose_template(self, token_name, request):
        if self.proposals is None:
            raise RESTError(503, "template proposals are not configured on this server")
        status, out = self.proposals.propose(token_name, request)
        policy = self.proposal_policy()
        if policy["review"] == "automatic" and (not out["replaces_existing"] or policy["replace_existing"]):
            try:
                self.proposals.approve(out["name"], replace=out["replaces_existing"], approved_by="automatic")
            except (OSError, ValueError) as e:
                return status, dict(out, status=f"waiting for the owner: automatic approval failed ({e})")
            return status, dict(out, approved=True, status=f"approved automatically; template {out['name']!r} "
                                                           f"can be used now (try a dry run first)")
        return status, dict(out, approved=False, status=f"waiting for the cluster owner to review and approve it "
                                                         f"({policy['detail']})")

    def list_proposals(self):
        if self.proposals is None:
            return {"proposals": []}
        return {"proposals": self.proposals.list()}

    # owner review (the /admin routes)
    @property
    def _proposal_store(self):
        if self.proposals is None:
            raise RESTError(503, "template proposals are not configured on this server")
        return self.proposals

    def review_proposals(self):
        return {"proposals": self._proposal_store.list(), "policy": self.proposal_policy()}

    def proposal_detail(self, name):
        try:
            return self._proposal_store.detail(name)
        except ValueError as e:
            raise RESTError(404, str(e))

    def approve_proposal(self, name, replace=False, approved_by="owner"):
        store = self._proposal_store
        if store._meta(name) is None:
            raise RESTError(404, f"no proposal named {name!r}")
        try:
            target = store.approve(name, replace=replace, approved_by=approved_by)
        except ValueError as e:
            raise RESTError(409 if "already exists" in str(e) else 422, str(e))
        return {"approved": name, "template_dir": target, "replaced": replace}

    def reject_proposal(self, name, reason=None, rejected_by="owner"):
        store = self._proposal_store
        if store._meta(name) is None:
            raise RESTError(404, f"no proposal named {name!r}")
        try:
            meta = store.reject(name, reason=reason, rejected_by=rejected_by)
        except ValueError as e:
            raise RESTError(400, str(e))
        return {"rejected": name, "reason": meta.get("reason", "")}

    def cluster_info(self, token_name, whitelist, see_all):
        info = dict(self.cluster.snapshot())
        templates, guides, _ = self.templates.load()
        active = self.registry.list(limit=1000, active_only=True)
        info.update({
            "limits": self.limits.to_json(),
            "active_api_jobs": len(active),
            "templates": [{"name": t.name, "description": t.spec["description"], "array": t.is_array,
                           "has_guide": t.guide_path is not None} for t in templates.values()],
            "guides": list(guides.values()),
            "allowed_dirs": list(whitelist.roots) if whitelist.restricted else None,
            "base_dir": whitelist.base_dir,
            "notes": self.cluster_notes,
            "sandbox": self.sandbox.describe(),
            "template_proposals": self.proposal_policy(),
        })
        return info

    @staticmethod
    def _slots(job):
        return (job.get("throttle") or 1) if job.get("array_size") else 1

    def _output_path(self, job):
        if job.get("array_size"):
            return os.path.join(job["workdir"], self.ARRAY_OUTPUT_PATTERN.replace("%A", str(job["job_id"])))
        return os.path.join(job["workdir"], self.OUTPUT_PATTERN.replace("%j", str(job["job_id"])))

    def _tasks_from(self, spec, whitelist):
        """Read per-task parameters out of a JSON manifest on the cluster (e.g. ScanManager's scan_info.json)."""
        if not isinstance(spec, dict) or set(spec) - self.TASKS_FROM_KEYS or "path" not in spec:
            raise RESTError(400, f"`tasks_from` must be an object with `path` and optionally "
                                 f"{sorted(self.TASKS_FROM_KEYS - {'path'})}")
        fields = spec.get("fields") or {}
        if not isinstance(fields, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in fields.items()):
            raise RESTError(400, "`tasks_from.fields` maps task parameter names to manifest field names")
        _, path = whitelist.resolve(spec["path"])
        if not os.path.isfile(path):
            raise RESTError(422, f"manifest {path} is not a file")
        if os.path.getsize(path) > self.MAX_MANIFEST:
            raise RESTError(413, f"manifest {path} is larger than {self.MAX_MANIFEST} bytes")
        try:
            with open(path) as f:
                data = json.load(f)
        except ValueError as e:
            raise RESTError(422, f"manifest {path} is not valid JSON: {e}")
        for part in (spec.get("key") or "").split("."):
            if part:
                if not isinstance(data, dict) or part not in data:
                    raise RESTError(422, f"manifest {path} has no key {spec['key']!r}")
                data = data[part]
        if not isinstance(data, list):
            raise RESTError(422, f"`{spec.get('key') or '(top level)'}` in {path} is not a list")
        indices = list(range(len(data)))
        select = spec.get("select")
        if select is not None:
            if not isinstance(select, list) or not all(isinstance(i, int) and 0 <= i < len(data) for i in select):
                raise RESTError(422, f"`tasks_from.select` must list indices between 0 and {len(data) - 1}")
            indices = select
        # paths in a manifest are relative to the manifest
        local = whitelist.with_base(os.path.dirname(path))
        tasks, sources = [], []
        for i in indices:
            entry = data[i]
            if not isinstance(entry, dict):
                raise RESTError(422, f"manifest entry {i} is not an object")
            if fields:
                missing = [f for f in fields.values() if f not in entry]
                if missing:
                    raise RESTError(422, f"manifest entry {i} lacks {missing}")
                task = {param: entry[field] for param, field in fields.items()}
            else:
                task = dict(entry)
            tasks.append((task, local))
            sources.append(i)
        return tasks, {"manifest": path, "key": spec.get("key"), "indices": sources}

    def submit(self, token_name, request, whitelist):
        if not isinstance(request, dict):
            raise RESTError(400, "request body must be an object")
        unknown = set(request) - self.SUBMIT_KEYS
        if unknown:
            raise RESTError(400, f"unknown fields {sorted(unknown)}", allowed=sorted(self.SUBMIT_KEYS))
        name = request.get("template")
        if not isinstance(name, str):
            raise RESTError(400, "`template` is required")
        key = request.get("idempotency_key")
        if key is not None and (not isinstance(key, str) or not 0 < len(key) <= 128):
            raise RESTError(400, "`idempotency_key` must be a string of 1-128 characters")
        label = request.get("label")
        if label is not None and (not isinstance(label, str) or not 0 < len(label) <= 128):
            raise RESTError(400, "`label` must be a string of 1-128 characters")
        dry_run = request.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise RESTError(400, "`dry_run` must be true or false")

        template = self.templates.get(name)
        values = template.validate_params(request.get("params"), whitelist)
        resources = template.resources(values, request.get("resources"))
        self.limits.check(resources)

        tasks = task_source = None
        throttle = request.get("throttle")
        if template.is_array:
            if ("tasks" in request) == ("tasks_from" in request):
                raise RESTError(422, f"template {name} is an array template: give exactly one of `tasks` "
                                     f"(a list of task parameter objects) or `tasks_from` (a manifest)")
            if "tasks" in request:
                if not isinstance(request["tasks"], list):
                    raise RESTError(400, "`tasks` must be a list of objects")
                pairs = [(t, None) for t in request["tasks"]]
            else:
                pairs, task_source = self._tasks_from(request["tasks_from"], whitelist)
            tasks = template.validate_tasks(pairs, whitelist, self.limits["max_array_tasks"])
            cap = self.limits["max_concurrent_jobs"]
            if throttle is None:
                throttle = min(len(tasks), cap or len(tasks))
            if not isinstance(throttle, int) or isinstance(throttle, bool) or throttle < 1:
                raise RESTError(400, "`throttle` must be a positive integer")
            if cap is not None and throttle > cap:
                raise RESTError(422, f"throttle {throttle} exceeds max_concurrent_jobs ({cap})")
            throttle = min(throttle, len(tasks))
        elif any(k in request for k in ("tasks", "tasks_from", "throttle")):
            raise RESTError(422, f"template {name} runs a single job; `tasks`, `tasks_from` and `throttle` "
                                 f"are for array templates")

        workdir = request.get("workdir") or template.workdir(values) or whitelist.base_dir
        if not isinstance(workdir, str):
            raise RESTError(400, "`workdir` must be a string")
        _, workdir = whitelist.resolve(workdir)
        if not os.path.isdir(workdir):
            raise RESTError(422, f"workdir {workdir} is not an existing directory")

        job_name = f"hpcrest-{template.name}"
        # A sandboxed job may write where its token may: the token's directories (narrowed
        # by the server's --allow list), or just the workdir if neither restricts it.
        writable = list(whitelist.roots) if whitelist.restricted else [workdir]
        try:
            script, sandbox_plan = template.render(values, tasks, sandbox=self.sandbox, writable=writable)
        except rest_sandbox.SandboxError as e:
            raise RESTError(503, str(e), see="GET /sandbox describes what this node supports")
        args = [
            "sbatch", "--parsable",
            f"--job-name={job_name}",
            f"--comment=hpclib-rest:{token_name}:{template.name}",
            f"--chdir={workdir}",
        ]
        if tasks is not None:
            args += [f"--array=0-{len(tasks) - 1}%{throttle}", f"--output={self.ARRAY_OUTPUT_PATTERN}"]
        else:
            args.append(f"--output={self.OUTPUT_PATTERN}")
        args += [f"{RESOURCE_FLAGS[k]}={v}" for k, v in sorted(resources.items())]

        plan = {"template": template.name, "params": values, "resources": resources, "workdir": workdir,
                "sbatch_args": args[1:], "script": script, "sandbox": sandbox_plan}
        if tasks is not None:
            plan.update(array_size=len(tasks), throttle=throttle, tasks=tasks, task_source=task_source)
        if dry_run:
            res = self.runner.run(args + ["--test-only"], input=script, cwd=workdir)
            if tasks is not None and len(script) > 20000:
                plan["script"] = script[:20000] + f"\n... ({len(script) - 20000} more bytes)"
            return 200, dict(plan, dry_run=True, accepted=res.returncode == 0,
                             slurm_message=(res.stderr or res.stdout).strip())

        with self.submit_lock:
            if key is not None:
                existing = self.registry.find_idempotent(token_name, key)
                if existing is not None:
                    return 200, dict(self.job_view(existing), duplicate=True)
            active = [j for j in self.refresh(self.registry.list(limit=1000, active_only=True)) if not j["terminal"]]
            cap = self.limits["max_concurrent_jobs"]
            used = sum(self._slots(j) for j in active)
            wanted = throttle if tasks is not None else 1
            if cap is not None and used + wanted > cap:
                raise RESTError(429, f"API jobs already use {used} of {cap} concurrent slots and this needs "
                                     f"{wanted}; wait for jobs to finish, cancel one, or lower `throttle`",
                                active_jobs=[j["job_id"] for j in active])
            res = self.runner.run(args, input=script, cwd=workdir)
            job_id = res.stdout.strip().split(";")[0]
            if res.returncode != 0 or not job_id.isdigit():
                raise RESTError(502, "sbatch rejected the job", returncode=res.returncode,
                                stdout=res.stdout, stderr=res.stderr)
            now = time.time()
            job = {
                "job_id": job_id, "token": token_name, "template": template.name,
                "params": values, "resources": resources, "workdir": workdir, "job_name": job_name,
                "idempotency_key": key, "submitted": now, "state": "PENDING", "updated": now, "label": label,
            }
            if tasks is not None:
                job.update(array_size=len(tasks), throttle=throttle,
                           tasks=[{"params": t, "source_index": (task_source["indices"][i] if task_source else None)}
                                  for i, t in enumerate(tasks)])
            job["output"] = self._output_path(job)
            self.registry.record(job)
        return 201, dict(self.job_view(self.registry.get(job_id)), duplicate=False, sandbox=sandbox_plan)

    def job_view(self, job, status=None, include_tasks=False):
        view = {k: job.get(k) for k in ("job_id", "template", "params", "resources", "workdir",
                                        "output", "submitted", "token", "label")}
        view["state"] = job["state"]
        view["terminal"] = job["state"] in SlurmRunner.TERMINAL_STATES
        if job.get("array_size"):
            view.update(array_size=job["array_size"], throttle=job["throttle"])
            per_task = (status or {}).get("tasks") or {int(k): v for k, v in (job.get("task_states") or {}).items()}
            if per_task:
                view.update({k: v for k, v in SlurmRunner.summarize(per_task).items() if k not in ("state", "terminal")})
            if include_tasks:
                view["tasks"] = [
                    dict({"index": i, "params": t["params"], "source_index": t["source_index"],
                          "output": view["output"].replace("%a", str(i))},
                         **{k: v for k, v in per_task.get(i, {"state": "UNKNOWN"}).items() if v != ""})
                    for i, t in enumerate(job.get("tasks") or [])
                ]
            if status:
                view.update({k: status[k] for k in ("state", "terminal")})
        elif status:
            view.update({k: v for k, v in status.items() if k not in ("job_id", "name") and v != ""})
        return view

    def refresh(self, jobs, include_tasks=False):
        live = [j for j in jobs if j["state"] not in SlurmRunner.TERMINAL_STATES]
        arrays = {j["job_id"]: j["array_size"] for j in live if j.get("array_size")}
        states = self.runner.query_states([j["job_id"] for j in live if not j.get("array_size")], arrays)
        self.registry.update_states(
            {j: s["state"] for j, s in states.items() if s["state"] != "UNKNOWN"},
            {j: states[j]["tasks"] for j in arrays if j in states},
        )
        out = []
        for job in jobs:
            status = states.get(job["job_id"])
            if status and status["state"] != "UNKNOWN":
                job = dict(job, state=status["state"])
            out.append(self.job_view(job, status, include_tasks=include_tasks))
        return out

    def _owned(self, job_id, token_name, see_all):
        job_id = str(job_id).strip()
        if not job_id.isdigit():
            raise RESTError(400, f"invalid job id {job_id!r}")
        job = self.registry.get(job_id)
        if job is None or (job["token"] != token_name and not see_all):
            raise RESTError(404, f"job {job_id} was not submitted through this API by this token")
        return job

    def status(self, job_id, token_name, see_all, include_tasks=False):
        return self.refresh([self._owned(job_id, token_name, see_all)], include_tasks=include_tasks)[0]

    def list_jobs(self, token_name, see_all, limit=50, active_only=False, label=None):
        jobs = self.registry.list(None if see_all else token_name, limit=limit, active_only=active_only, label=label)
        return self.refresh(jobs)

    def cancel(self, job_id, token_name, see_all):
        job = self._owned(job_id, token_name, see_all)
        res = self.runner.run(["scancel", job["job_id"]])
        if res.returncode != 0:
            raise RESTError(502, f"scancel failed for job {job['job_id']}", stderr=res.stderr)
        return dict(self.status(job["job_id"], token_name, see_all), cancel_requested=True)

    def wait(self, job_id, token_name, see_all, timeout):
        timeout = max(0.0, min(float(timeout), self.MAX_WAIT))
        deadline = time.time() + timeout
        while True:
            view = self.status(job_id, token_name, see_all)
            if view["terminal"] or time.time() >= deadline:
                return dict(view, timed_out=not view["terminal"])
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.time())))
