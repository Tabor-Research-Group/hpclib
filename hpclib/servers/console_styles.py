"""
user.css, the console's one stylesheet of yours, and Interface Settings, a form over it.

The page loads default-styles.css (every variable and its default), style.css (the console's rules), then
user.css (yours, ~/.config/hpclib/console/user.css), so user.css wins. Interface Settings reads the variables
user.css sets in the theme blocks default-styles.css uses, and saving writes them back into user.css:

  - a variable user.css already sets has its value changed where it is (your layout and comments stay);
  - one set back to its default is taken out of user.css (a line that already holds the default value is left);
  - one user.css doesn't set yet goes in a block at the end, between the "Interface Settings" markers, which each
    save rewrites (move a line out of it and it is yours like any other).

Everything else in user.css is never touched. The first change keeps the file as it was in
user.css.before-hpclib. Earlier consoles kept these values in preferences.json; they are moved into user.css once
(migrate), where user.css didn't set its own.

Variables are read from blocks laid out like default-styles.css: one declaration per line, a block opened at the
end of its selector's line. The theme blocks are :root (light, and the variables without a dark value), and the
two dark ones: :root:not([data-theme="light"]) inside @media (prefers-color-scheme: dark), and
:root[data-theme="dark"]. Standard library only.
"""
import json
import os
import re
import shutil
import tempfile
import threading

LIGHT = ":root"
DARK_MEDIA = "@media (prefers-color-scheme: dark)"
DARK = (':root:not([data-theme="light"])', ':root[data-theme="dark"]')
PARTS = ("common", "light", "dark")
BEGIN = "/* >>> Interface Settings: values set there that the rest of this file doesn't set (each save rewrites this block) >>> */"
END = "/* <<< Interface Settings <<< */"
BACKUP_SUFFIX = ".before-hpclib"
CSS_VAR_RE = re.compile(r"--[a-z][a-z0-9-]{0,47}")
# a CSS value with nothing that ends a declaration or rule, escapes, comments or fetches anything
CSS_VALUE_RE = re.compile(r"(?!.*(?:/\*|\*/|url\s*\(|@import|expression\s*\())[^;{}<>\\\n\r\0]{1,200}", re.I)
DECL_RE = re.compile(r"^(\s*)(--[A-Za-z0-9_-]+)(\s*:\s*)(.*?)(\s*;\s*(?:/\*.*?\*/\s*)?)$")
MAX_VARS = 200
# Defaults that changed: a user.css copied from an older default-styles.css still holds the old value, which you
# never chose. migrate() moves such a line to the new default, once per change (recorded in user-css-upgrades.json).
DEFAULT_CHANGES = {"content-width-1600": ("--content-max-width", "1200px")}
lock = threading.Lock()


class StyleError(ValueError):
    pass


def _norm(value):
    return re.sub(r"\s+", " ", value.strip().lower())


def scan(text):
    """[(line, selector or None, variable or None, value or None)]: each line, with the innermost block it
    declares a variable in (selectors with their whitespace collapsed)."""
    stack, out, in_comment = [], [], False
    for line in text.splitlines(keepends=True):
        code = line
        if in_comment:
            if "*/" not in code:
                out.append((line, None, None, None))
                continue
            code, in_comment = code.split("*/", 1)[1], False
        code = re.sub(r"/\*.*?\*/", "", code)
        if "/*" in code:
            code, in_comment = code.split("/*", 1)[0], True
        m = DECL_RE.match(code.rstrip("\n"))
        out.append((line, stack[-1], m.group(2), m.group(4)) if m and stack else (line, None, None, None))
        for ch in re.findall(r"[{}]", code):
            if ch == "{":
                stack.append(re.sub(r"\s+", " ", code.split("{")[0].strip()))
            elif stack:
                stack.pop()
    return out


def variables(default_css):
    """{name: {"default": light/common value, "dark": dark value or None}} from default-styles.css."""
    out = {}
    for _, sel, name, value in scan(default_css):
        if sel == LIGHT:
            out.setdefault(name, {"default": value, "dark": None})["default"] = value
        elif sel in DARK and name in out:
            out[name]["dark"] = value
    return out


def _part(info):
    return "light" if info["dark"] is not None else "common"


def read(user_css, default_css):
    """{"common", "light", "dark"}: what user.css sets each variable to, where that isn't its default."""
    known = variables(default_css)
    out = {p: {} for p in PARTS}
    for _, sel, name, value in scan(user_css or ""):
        info = known.get(name)
        if not info:
            continue
        if sel == LIGHT:
            target, default = _part(info), info["default"]
        elif sel in DARK and info["dark"] is not None:
            target, default = "dark", info["dark"]
        else:
            continue
        if _norm(value) == _norm(default):
            out[target].pop(name, None)
        else:
            out[target][name] = value
    return out


def check(values, default_css):
    """The form's values, checked: {"common", "light", "dark"} maps of known variables to safe CSS values."""
    if not isinstance(values, dict) or set(values) - set(PARTS):
        raise StyleError("send {\"common\": {...}, \"light\": {...}, \"dark\": {...}}")
    known = variables(default_css)
    out, count = {p: {} for p in PARTS}, 0
    for part, given in values.items():
        if not isinstance(given, dict):
            raise StyleError(f"{part} must map CSS variables to values")
        for name, value in given.items():
            count += 1
            if not CSS_VAR_RE.fullmatch(name) or name not in known:
                raise StyleError(f"{name!r} isn't one of default-styles.css's variables")
            if (part == "common") != (known[name]["dark"] is None):
                raise StyleError(f"{name} goes in {_part(known[name]) if part == 'common' else 'common'}")
            if isinstance(value, str) and not value.strip():
                continue                                   # empty: the default
            if not isinstance(value, str) or not CSS_VALUE_RE.fullmatch(value.strip()):
                raise StyleError(f"{name}: a value without ; {{ }} < > \\, comments or url()")
            out[part][name] = value.strip()
    if count > MAX_VARS:
        raise StyleError(f"at most {MAX_VARS} values")
    return out


def _strip_block(text):
    lines, out, inside = text.splitlines(keepends=True), [], False
    for line in lines:
        if line.strip() == BEGIN:
            inside = True
        elif inside and line.strip() == END:
            inside = False
        elif not inside:
            out.append(line)
    return "".join(out)


def write(user_css, default_css, values):
    """user.css with the form's values: changed in place, defaults taken out, new ones in the block at the end."""
    known = variables(default_css)
    want_light = {**values.get("common", {}), **values.get("light", {})}
    want_dark = dict(values.get("dark", {}))
    done = set()                                   # (selector, name) written in place
    kept = []
    for line, sel, name, value in scan(_strip_block(user_css or "")):
        info = known.get(name) if name else None
        if info and (sel == LIGHT or (sel in DARK and info["dark"] is not None)):
            want = want_light.get(name) if sel == LIGHT else want_dark.get(name)
            default = info["default"] if sel == LIGHT else info["dark"]
            if want is None:
                if _norm(value) != _norm(default):
                    continue                       # back to the default: the line goes
            else:
                m = DECL_RE.match(line.rstrip("\n"))
                if m:                              # (a line with a comment before it: the block sets it instead)
                    line = f"{m.group(1)}{m.group(2)}{m.group(3)}{want}{m.group(5)}" + ("\n" if line.endswith("\n") else "")
                    done.add((sel, name))
        kept.append(line)
    text = "".join(kept)
    light = {n: v for n, v in want_light.items() if (LIGHT, n) not in done}
    dark = {sel: {n: v for n, v in want_dark.items() if (sel, n) not in done} for sel in DARK}
    block = []
    if light:
        block += [f"{LIGHT} {{"] + [f"  {n}: {v};" for n, v in sorted(light.items())] + ["}"]
    if dark[DARK[0]]:
        block += [f"{DARK_MEDIA} {{", f"  {DARK[0]} {{"] + [f"    {n}: {v};" for n, v in sorted(dark[DARK[0]].items())] + ["  }", "}"]
    if dark[DARK[1]]:
        block += [f"{DARK[1]} {{"] + [f"  {n}: {v};" for n, v in sorted(dark[DARK[1]].items())] + ["}"]
    if block:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n".join([BEGIN, *block, END]) + "\n"
    return text


def save(path, default_css, values):
    """Write the form's values into the file at path (created if missing); returns what it now sets."""
    values = check(values, default_css)
    with lock:
        try:
            with open(path) as f:
                old = f.read()
        except FileNotFoundError:
            old = None
        new = write(old or "", default_css, values)
        if new != (old or ""):
            directory = os.path.dirname(path)
            os.makedirs(directory, mode=0o700, exist_ok=True)
            if old is not None and not os.path.exists(path + BACKUP_SUFFIX):
                shutil.copy2(path, path + BACKUP_SUFFIX)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".user.css.")
            with os.fdopen(fd, "w") as f:
                f.write(new)
            if old is not None:
                os.chmod(tmp, os.stat(path).st_mode & 0o777)
            os.replace(tmp, path)
        return read(new, default_css)


def upgrade_defaults(console_dir, default_css):
    """DEFAULT_CHANGES not yet applied to user.css: its :root lines holding the old default get the new one."""
    record = os.path.join(console_dir, "user-css-upgrades.json")
    try:
        with open(record) as f:
            done = set(json.load(f))
    except (OSError, ValueError, TypeError):
        done = set()
    todo = {k: v for k, v in DEFAULT_CHANGES.items() if k not in done}
    if not todo or not default_css:
        return []
    path = os.path.join(console_dir, "user.css")
    known = variables(default_css)
    changed = []
    with lock:
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            text = None
        if text is not None:
            lines = []
            for line, sel, name, value in scan(text):
                for key, (var, old) in todo.items():
                    if sel == LIGHT and name == var and _norm(value) == _norm(old) and var in known:
                        m = DECL_RE.match(line.rstrip("\n"))
                        if m:
                            line = (f"{m.group(1)}{m.group(2)}{m.group(3)}{known[var]['default']}{m.group(5)}"
                                    + ("\n" if line.endswith("\n") else ""))
                            changed.append(key)
                lines.append(line)
            if changed:
                new = "".join(lines)
                fd, tmp = tempfile.mkstemp(dir=console_dir, prefix=".user.css.")
                with os.fdopen(fd, "w") as f:
                    f.write(new)
                os.chmod(tmp, os.stat(path).st_mode & 0o777)
                os.replace(tmp, path)
        os.makedirs(console_dir, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=console_dir, prefix=".upgrades.")
        with os.fdopen(fd, "w") as f:
            json.dump(sorted(done | set(todo)), f)
        os.replace(tmp, record)
    return changed


def migrate(console_dir, default_css):
    """Bring user.css up to date with this console: DEFAULT_CHANGES (upgrade_defaults), and Interface Settings
    values an earlier console kept in preferences.json, moved into user.css (where user.css doesn't set its own)
    and taken out of preferences.json. Returns whether there were any of those."""
    upgrade_defaults(console_dir, default_css)
    prefs_path = os.path.join(console_dir, "preferences.json")
    try:
        with open(prefs_path) as f:
            prefs = json.load(f)
    except (OSError, ValueError):
        return False
    old = prefs.get("interface") if isinstance(prefs, dict) else None
    if not isinstance(old, dict) or not default_css:
        return False
    if any(isinstance(v, dict) and v for v in old.values()):
        path = os.path.join(console_dir, "user.css")
        try:
            with open(path) as f:
                current = read(f.read(), default_css)
        except OSError:
            current = {p: {} for p in PARTS}
        known = variables(default_css)
        merged = {p: dict(current[p]) for p in PARTS}
        for part in PARTS:
            for name, value in (old.get(part) or {}).items():
                info = known.get(name)
                if not info or not isinstance(value, str) or not CSS_VALUE_RE.fullmatch(value.strip()):
                    continue
                target = "dark" if part == "dark" else _part(info)
                if target == "dark" and info["dark"] is None:
                    continue
                merged[target].setdefault(name, value.strip())    # user.css's own value won before, and still does
        save(path, default_css, merged)
    del prefs["interface"]
    fd, tmp = tempfile.mkstemp(dir=console_dir, prefix=".preferences.")
    with os.fdopen(fd, "w") as f:
        json.dump(prefs, f, indent=2)
        f.write("\n")
    os.replace(tmp, prefs_path)
    return True
