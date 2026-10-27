"""
Edits hpclib makes to a project's manifest (pyproject.toml, pixi.toml) when it layers an environment on a core one
(rest_envs.py): text edits that leave the rest of the file as it was written (comments, order, quoting).

  [tool.hpclib]                     core = "../core" (the core project, relative to this one),
                                    core-environment = "default" (pixi)
  [tool.uv] constraint-dependencies  (uv) the core's versions, between two marker comments in the array
  [constraints] / [tool.pixi.constraints]   (pixi) the core's conda packages, version and build, between markers

Everything between the markers is hpclib's and is replaced at each sync; edit outside them. A file this can't edit
safely (the setting written another way, say as a dotted key elsewhere) is refused with what to change, and the
result is checked with tomllib where Python has it (3.11+).

Standard library only.
"""
import os
import re

try:
    import tomllib
except ImportError:      # Python < 3.11: the tools themselves check the file at the sync
    tomllib = None

BEGIN = "# hpclib core"
END = "# hpclib core end"
HEADER_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


class ManifestError(ValueError):
    pass


def _strip_comment(line):
    """The line without a trailing # comment (one outside quotes)."""
    quote = None
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#":
            return line[:i]
        i += 1
    return line


def _tables(lines):
    """[(name, header index, end index)]: each [table]'s lines (end exclusive)."""
    heads = []
    for i, line in enumerate(lines):
        m = HEADER_RE.match(line)
        if m and not line.lstrip().startswith("[\"") and _strip_comment(line).strip().endswith("]"):
            heads.append((m.group(1).replace(" ", ""), i))
    out = []
    for n, (name, i) in enumerate(heads):
        end = heads[n + 1][1] if n + 1 < len(heads) else len(lines)
        out.append((name, i, end))
    return out


def _find_table(lines, name):
    found = [t for t in _tables(lines) if t[0] == name]
    if len(found) > 1:
        raise ManifestError(f"[{name}] appears more than once")
    return found[0] if found else None


def _end_of_table(lines, head, end):
    """Where to add lines to a table: after its last non-blank line."""
    last = head
    for i in range(head + 1, end):
        if lines[i].strip():
            last = i
    return last + 1


def _remove_block(lines, start, end):
    """Lines start..end without hpclib's marker block (and how many were removed)."""
    out, inside, removed = [], False, 0
    for i, line in enumerate(lines):
        if start <= i < end and line.strip().startswith(BEGIN) and not line.strip().startswith(END):
            inside = True
        if inside:
            removed += 1
            if line.strip().startswith(END):
                inside = False
            continue
        out.append(line)
    if inside:
        raise ManifestError(f"hpclib's block has a '{BEGIN}' line without '{END}'")
    return out, removed


def _parsed(text):
    if tomllib is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"not valid TOML: {e}")


def _check(text, path_keys, label):
    """After an edit: the file parses, and the setting is where hpclib put it."""
    data = _parsed(text)
    if data is None:
        return
    for key in path_keys[:-1]:
        data = data.get(key, {}) if isinstance(data, dict) else {}
    if path_keys[-1] not in data:
        raise ManifestError(f"{label}: the edit didn't land where expected")


def _setting_elsewhere(text, keys, table):
    """Whether tomllib sees the setting (keys) in the file though not in [table] as hpclib edits it."""
    data = _parsed(text)
    if data is None:
        return False
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return False
        data = data[key]
    lines = text.splitlines()
    t = _find_table(lines, table)
    return t is None


# -- [tool.hpclib] ----------------------------------------------------------------------------------------------------

def read_core(text):
    """{"core": PATH, "core-environment": NAME} from [tool.hpclib] (keys missing when not set)."""
    data = _parsed(text) if tomllib else None
    if data is not None:
        hp = data.get("tool", {}).get("hpclib", {})
        return {k: v for k, v in hp.items() if k in ("core", "core-environment") and isinstance(v, str)}
    lines = text.splitlines()
    t = _find_table(lines, "tool.hpclib")
    out = {}
    if t:
        for line in lines[t[1] + 1:t[2]]:
            m = re.match(r'\s*(core|core-environment)\s*=\s*"([^"]*)"', line)
            if m:
                out[m.group(1)] = m.group(2)
    return out


def set_core(text, core=None, environment=None):
    """[tool.hpclib] core (and core-environment) set, or removed (core None)."""
    lines = text.splitlines()
    t = _find_table(lines, "tool.hpclib")
    keys = ("core", "core-environment")
    new = []
    if core is not None:
        new.append(f'core = "{_toml_str(core)}"')
        if environment:
            new.append(f'core-environment = "{_toml_str(environment)}"')
    if t is None:
        if not new:
            return text
        body = text.rstrip("\n")
        return (body + "\n\n" if body else "") + "[tool.hpclib]\n" + "\n".join(new) + "\n"
    name, head, end = t
    kept = [line for line in lines[head + 1:end]
            if not re.match(r"\s*(%s)\s*=" % "|".join(re.escape(k) for k in keys), line)]
    if not new and not any(l.strip() and not l.strip().startswith("#") for l in kept):
        # the table held only hpclib's keys: it goes, with the blank lines before it
        before = lines[:head]
        while before and not before[-1].strip():
            before.pop()
        rest = lines[end:]
        out = before + ([""] if before and rest else []) + rest
        return "\n".join(out) + ("\n" if out else "")
    out = lines[:head + 1] + new + kept + lines[end:]
    return "\n".join(out) + "\n"


def _toml_str(s):
    if any(c in s for c in '"\\\n\r'):
        raise ManifestError("a path with quotes, backslashes or line breaks can't go in the manifest")
    return s


# -- uv: [tool.uv] constraint-dependencies ----------------------------------------------------------------------------

def _array_span(text, start):
    """(open index, close index) of the [...] array whose '[' is the first one at or after `start`."""
    i = text.index("[", start)
    open_ = i
    depth, quote = 0, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#":
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return open_, i
        i += 1
    raise ManifestError("an array in [tool.uv] isn't closed")


def set_uv_constraints(text, pins, label):
    """[tool.uv] constraint-dependencies with hpclib's block of `pins` ("name==version"), or without it (no pins)."""
    lines = text.splitlines()
    if _setting_elsewhere(text, ("tool", "uv", "constraint-dependencies"), "tool.uv"):
        raise ManifestError("constraint-dependencies is set outside a [tool.uv] table; move it into [tool.uv] "
                            "so hpclib can add the core's versions")
    block = [f"    {BEGIN}: {label} (hpclib replaces these lines at each sync)"] + \
            [f'    "{_toml_str(p)}",' for p in pins] + [f"    {END}"]
    t = _find_table(lines, "tool.uv")
    if t is None:
        if not pins:
            return text
        body = text.rstrip("\n")
        return body + "\n\n[tool.uv]\nconstraint-dependencies = [\n" + "\n".join(block) + "\n]\n"
    name, head, end = t
    key = None
    for i in range(head + 1, end):
        if re.match(r"\s*constraint-dependencies\s*=", lines[i]):
            key = i
            break
    if key is None:
        if not pins:
            return text
        out = lines[:head + 1] + ["constraint-dependencies = ["] + block + ["]"] + lines[head + 1:]
        result = "\n".join(out) + "\n"
        _check(result, ("tool", "uv", "constraint-dependencies"), "[tool.uv] constraint-dependencies")
        return result
    offset = sum(len(l) + 1 for l in lines[:key])
    open_, close = _array_span(text, offset)
    inner = text[open_ + 1:close]
    inner_lines, _ = _remove_block(inner.split("\n"), 0, len(inner.split("\n")))
    inner = "\n".join(inner_lines)
    # the user's items, each on a line of its own, then hpclib's block
    content = inner.rstrip()
    meaningful = [l for l in content.split("\n") if _strip_comment(l).strip()]
    if meaningful and not _strip_comment(meaningful[-1]).rstrip().endswith((",", "[")):
        # add the comma after the last item (before its comment, if any)
        idx = content.rfind(_strip_comment(meaningful[-1]).rstrip())
        end_item = idx + len(_strip_comment(meaningful[-1]).rstrip())
        content = content[:end_item] + "," + content[end_item:]
    if pins:
        new_inner = (content + "\n" if content.strip() else "\n") + "\n".join(block) + "\n"
    else:
        new_inner = (content + "\n") if content.strip() else ""
    if not content.strip() and not pins:
        new_inner = ""
    result = text[:open_ + 1] + new_inner + text[close:]
    if pins:
        _check(result, ("tool", "uv", "constraint-dependencies"), "[tool.uv] constraint-dependencies")
    else:
        _parsed(result)
    return result


# -- pixi: [constraints] ----------------------------------------------------------------------------------------------

def set_pixi_constraints(text, pins, label, pyproject=False):
    """pixi's constraints table with hpclib's block: pins are (name, version, build)."""
    table = "tool.pixi.constraints" if pyproject else "constraints"
    lines = text.splitlines()
    keys = ("tool", "pixi", "constraints") if pyproject else ("constraints",)
    if _setting_elsewhere(text, keys, table):
        raise ManifestError(f"pixi's constraints are set outside a [{table}] table; move them into [{table}] so "
                            "hpclib can add the core's versions")
    block = [f"{BEGIN}: {label} (hpclib replaces these lines at each sync)"] + \
            [f'{_key(n)} = {{ version = "=={_toml_str(v)}", build = "{_toml_str(b)}" }}' for n, v, b in pins] + [END]
    t = _find_table(lines, table)
    if t is None:
        if not pins:
            return text
        result = text.rstrip("\n") + f"\n\n[{table}]\n" + "\n".join(block) + "\n"
        _check(result, keys, f"[{table}]")
        return result
    name, head, end = t
    table_lines, _ = _remove_block(lines[head + 1:end], 0, end - head - 1)
    names = {n for n, _, _ in pins}
    clash = sorted(n for n in names for l in table_lines
                   if re.match(r'\s*"?%s"?\s*=' % re.escape(n), l))
    if clash:
        raise ManifestError(f"[{table}] already constrains {', '.join(clash)}, which the core pins; remove those "
                            "lines (the core's versions apply)")
    while table_lines and not table_lines[-1].strip():
        table_lines.pop()
    new = table_lines + (block if pins else [])
    trailing = [""] if end < len(lines) else []
    out = lines[:head + 1] + new + trailing + lines[end:]
    if not pins and not any(l.strip() for l in table_lines):
        out = lines[:head] + lines[end:]
        while out and not out[-1].strip():
            out.pop()
    result = "\n".join(out) + "\n"
    if pins:
        _check(result, keys, f"[{table}]")
    else:
        _parsed(result)
    return result


def _key(name):
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else f'"{_toml_str(name)}"'


def relative(path, start):
    """`path` relative to `start` (the form [tool.hpclib] core keeps), else absolute."""
    rel = os.path.relpath(path, start)
    return rel if not rel.startswith(".." + os.sep + ".." + os.sep + ".." + os.sep) else path
