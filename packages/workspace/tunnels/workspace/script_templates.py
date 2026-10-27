"""
Script templates: the job scripts the Workspace builds for a Python or bash file and submits with sbatch. The
Workspace's own are in script_templates/ beside this file; yours are in $HPCTUNNELS_DATA_DIR/script_templates/
(~/.local/tunnels/script_templates; Submit job -> Edit template copies one there), and one of yours with the same
name as one of the Workspace's replaces it.

A template is the job script with a header:

  #!/bin/bash
  #@ title: Python with uv
  #@ description: one line on what it runs
  #@ for: .py                         the files it is offered for (extensions; * for any)
  #@ script: {{input_stem}}.slurm     the job script's file name (next to the file)
  #@ field time: Time limit | 6:00:00 | e.g. 1-00:00:00
  #@ field modules: Modules | | space-separated
  #SBATCH --time={{time}}
  {{?modules}}module load {{modules}}
  ...

`#@ field NAME: Label | default | hint` adds a field to the form (the default may use the values below). In the
script, {{NAME}} is replaced by a field's value or one of these:

  input        the file's name                 input_path   its absolute path
  input_stem   its name without the extension  dir          its directory
  hpclib       hpclib.sh's path on the cluster  user, home   you, your home directory there

A line that starts with {{?NAME}} is kept only when NAME isn't empty, and {{!NAME}} only when it is (the marker is
dropped). The #@ lines aren't part of the script. Values go in as they are: the template quotes what needs it.
Python 3.6+, standard library only.
"""
import os
import re

BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "script_templates")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
FIELD_RE = re.compile(r"^#@\s*field\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
META_RE = re.compile(r"^#@\s*(title|description|for|script)\s*:\s*(.*)$")
VAR_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
COND_RE = re.compile(r"^(\s*)\{\{([?!])([A-Za-z_][A-Za-z0-9_]*)\}\}")
CONTEXT = ("input", "input_path", "input_stem", "dir", "hpclib", "user", "home")
MAX_VALUE = 4000


class TemplateError(ValueError):
    pass


def parse(text, name=""):
    """{name, title, description, for: [...], script, fields: [{name, label, default, hint}]}."""
    out = {"name": name, "title": name, "description": "", "for": ["*"], "script": "{{input_stem}}.slurm",
           "fields": []}
    seen = set()
    for line in text.splitlines():
        m = FIELD_RE.match(line)
        if m:
            parts = [p.strip() for p in m.group(2).split("|")] + ["", ""]
            if m.group(1) in seen or m.group(1) in CONTEXT:
                continue
            seen.add(m.group(1))
            out["fields"].append({"name": m.group(1), "label": parts[0] or m.group(1), "default": parts[1],
                                  "hint": "|".join(parts[2:]).strip("| ").strip()})
            continue
        m = META_RE.match(line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            out[key] = value.split() if key == "for" else value
    return out


def values_for(template, given, context):
    """Every name a template can use: the context, then the fields (given, else their defaults)."""
    given = given or {}
    values = {k: str(context.get(k) or "") for k in CONTEXT}
    for f in template["fields"]:
        v = given.get(f["name"])
        if v is None:
            v = substitute(f["default"], values, strict=False)
        if not isinstance(v, str) or len(v) > MAX_VALUE or "\0" in v:
            raise TemplateError(f"{f['name']}: text, at most {MAX_VALUE} characters")
        values[f["name"]] = v.strip()
    return values


def substitute(text, values, strict=True):
    def one(m):
        if m.group(1) in values:
            return values[m.group(1)]
        if strict:
            raise TemplateError(f"{{{{{m.group(1)}}}}} isn't a field of this template or one of {', '.join(CONTEXT)}")
        return m.group(0)
    return VAR_RE.sub(one, text)


def render(text, given, context, name=""):
    """(script, values): the job script for `text` with the form's values and the file's context."""
    template = parse(text, name)
    values = values_for(template, given, context)
    lines = []
    for line in text.splitlines():
        if line.startswith("#@"):
            continue
        m = COND_RE.match(line)
        if m:
            empty = not values.get(m.group(3))
            if m.group(3) not in values:
                raise TemplateError(f"{{{{{m.group(2)}{m.group(3)}}}}}: no field {m.group(3)}")
            if (m.group(2) == "?") == empty:
                continue
            line = m.group(1) + line[m.end():]
        lines.append(substitute(line, values))
    return "\n".join(lines).rstrip("\n") + "\n", values


def script_name(template, values):
    name = substitute(template.get("script") or "{{input_stem}}.slurm", values, strict=False).strip()
    return os.path.basename(name) or "job.slurm"


def offered(template, filename):
    """Whether the template is for this file (by its `for` extensions)."""
    exts = template.get("for") or ["*"]
    return "*" in exts or any(filename.endswith(e) for e in exts)


def builtins():
    """The Workspace's own templates: {name: text}."""
    out = {}
    try:
        names = sorted(os.listdir(BUILTIN_DIR))
    except OSError:
        return out
    for n in names:
        if n.endswith(".sh"):
            with open(os.path.join(BUILTIN_DIR, n)) as f:
                out[n[:-3]] = f.read()
    return out


def context_for(input_path, info):
    """The context values for a file on the cluster, with the Workspace's `info` (home, user, hpclib)."""
    base = os.path.basename(input_path)
    stem = base.rsplit(".", 1)[0] if "." in base.strip(".") else base
    return {"input": base, "input_path": input_path, "input_stem": stem, "dir": os.path.dirname(input_path) or "/",
            "hpclib": info.get("hpclib") or os.path.join(info.get("home") or "~", "hpclib", "hpclib.sh"),
            "user": info.get("user") or "", "home": info.get("home") or ""}
