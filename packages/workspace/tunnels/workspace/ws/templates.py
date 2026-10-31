"""
The classic script templates (script_templates.py parses and renders them): the Workspace's own, yours in
$HPCTUNNELS_DATA_DIR/script_templates, and the values last used with each. The guided Submit page doesn't use these;
the "Classic" page does, and they remain the way to keep a script of your own with fields in it.

  Templates(info, jobs)
    .list()                   {dir, templates: [...], remembered, last}
    .text(name, text=None)    a template's text (or `text` after a size check)
    .render(body)             {template, text?, input, values} -> {script, name, values, dir, fields}
    .submit(body)             {script, name, dir, overwrite?, template?, values?} -> write the script, sbatch it
    .edit(body)               {name, from?} -> your copy to edit

Python 3.6 or newer, standard library only.
"""
import json
import os
import re

import script_templates

from .common import Refused, state_dir, templates_dir

MAX_TEMPLATE = 256 * 1024
TEMPLATE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class Templates:
    def __init__(self, info, jobs):
        """`info()` is the Workspace's info; `jobs` writes and submits a script (Jobs.write_and_sbatch)."""
        self.info, self.jobs = info, jobs

    def _yours(self):
        d, out = templates_dir(), {}
        try:
            names = sorted(os.listdir(d))
        except OSError:
            names = []
        for name in names:
            if not name.endswith(".sh") or not TEMPLATE_NAME_RE.fullmatch(name[:-3]):
                continue
            p = os.path.join(d, name)
            try:
                if os.path.getsize(p) > MAX_TEMPLATE:
                    continue
                with open(p, encoding="utf-8", errors="replace") as f:
                    out[name[:-3]] = (p, f.read())
            except OSError:
                continue
        return out

    def _remembered_path(self):
        return os.path.join(state_dir(), "remembered.json")

    def remembered(self):
        try:
            with open(self._remembered_path()) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def remember(self, template, values, fields):
        data = self.remembered()
        data.setdefault("templates", {})[template] = {k: v for k, v in values.items()
                                                      if k in fields and k not in ("job_name", "args", "project")}
        data["last_template"] = template
        path = self._remembered_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(data, f, indent=2)
        os.replace(path + ".tmp", path)

    def list(self):
        found = {}
        for name, text in script_templates.builtins().items():
            found[name] = {"text": text, "source": "workspace", "path": os.path.join(script_templates.BUILTIN_DIR, name + ".sh")}
        for name, (path, text) in self._yours().items():
            found[name] = {"text": text, "source": "yours", "path": path, "replaces": name in found}
        out = [dict(script_templates.parse(t["text"], name), **t) for name, t in sorted(found.items())]
        remembered = self.remembered()
        return {"dir": templates_dir(), "templates": out, "remembered": remembered.get("templates") or {},
                "last": remembered.get("last_template")}

    def text(self, name, text=None):
        if text is not None:
            if not isinstance(text, str) or len(text) > MAX_TEMPLATE:
                raise Refused(400, "text: a template, at most 256 KiB")
            return text
        for t in self.list()["templates"]:
            if t["name"] == name:
                return t["text"]
        raise Refused(404, "no script template {!r}".format(name))

    def render(self, body):
        name, given = body.get("template"), body.get("values") or {}
        if not isinstance(given, dict) or not all(isinstance(v, str) for v in given.values()):
            raise Refused(400, "values: {field: text}")
        inp = body.get("input")
        if not isinstance(inp, str) or not inp.startswith("/"):
            raise Refused(400, "input: the file's absolute path")
        text = self.text(name, body.get("text"))
        context = script_templates.context_for(inp, self.info())
        try:
            script, values = script_templates.render(text, given, context, name or "")
        except script_templates.TemplateError as e:
            raise Refused(400, "the template: {}".format(e))
        parsed = script_templates.parse(text, name or "")
        return {"script": script, "name": script_templates.script_name(parsed, values), "values": values,
                "dir": context["dir"], "fields": parsed["fields"]}

    def submit(self, body):
        done = self.jobs.write_and_sbatch(body.get("script"), body.get("name"), body.get("dir"), bool(body.get("overwrite")))
        template, values = body.get("template"), body.get("values")
        if isinstance(template, str) and isinstance(values, dict):
            try:
                fields = {f["name"] for f in script_templates.parse(self.text(template))["fields"]}
                self.remember(template, {k: v for k, v in values.items() if isinstance(v, str)}, fields)
            except (Refused, OSError):
                pass
        return done

    def edit(self, body):
        name = body.get("name")
        source = body.get("from") or name
        if not isinstance(name, str) or not TEMPLATE_NAME_RE.fullmatch(name):
            raise Refused(400, "name: letters, digits, _ . -")
        mine = self._yours()
        if name in mine:
            return {"path": mine[name][0], "created": False}
        text = self.text(source)
        os.makedirs(templates_dir(), exist_ok=True)
        path = os.path.join(templates_dir(), name + ".sh")
        with open(path, "x") as f:
            f.write(text)
        return {"path": path, "created": True}
