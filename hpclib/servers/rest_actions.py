"""
Tunnel actions: typed operations a tunnel declares, which the REST server runs for agents.

A tunnel ships actions.json beside its scripts:

  {"title": "SMB server",
   "description": "...",
   "available": {"run": ["smbshell.sh", "status", "--json", "--brief"], "line": "HPCLIB_SMB_STATUS",
                 "require": ["host", "image"], "any": ["ticket", "credentials"], "show": ["host", "ticket"],
                 "message": "Sign in to the SMB server from Data transfer first.", "cache": 30},
   "actions": {
     "get": {"title": "Copy from the SMB server", "description": "...",
             "mode": "job",                 # job: it submits a SLURM job and prints its ID; sync: it prints a result
             "default": true,               # on unless the owner turns it off (false: off until turned on)
             "scope": "submit",             # the token scope it needs: read, submit or files:write
             "params": {"remote": {"type": "remote-path", "description": "..."},
                        "local": {"type": "cluster-path", "access": "write", "description": "..."}},
             "run": ["smbshell.sh", "submit", "{job_options}", "action-get", "--scope", "{scope_file}",
                     "--helper", "{helper}", "{remote}", "{local}"],
             "output": "{data_dir}/sessions/data-transfer/transfer-{job_id}.log",
             "timeout": 120}}}

`run` is an argument list run without a shell, from the tunnel's directory (its first item, a script there, runs
with bash when it ends in .sh). Each `{param}` fills a whole argument; the server's own are {job_options} (sbatch
options naming the job's agent and action, which the tunnel passes to sbatch), {scope_file} (a JSON file with the
token's directories, for action_paths.py), {helper} (action_paths.py) and {data_dir} ($HPCTUNNELS_DATA_DIR).

Parameter types: `cluster-path` (resolved like the files API: inside the token's directories, never a protected
path; `access: write` needs file changes allowed, `read` an existing path; the tunnel gets the resolved path),
`remote-path` (a path relative to the tunnel's own root: no leading / or -, no .., no control characters),
`string` (with a `pattern`), `integer` (`minimum`, `maximum`) and `boolean`. Optional parameters have a `default`.

The owner turns a tunnel's actions on in the REST config (PUT /admin/config, section tunnel_actions):
  {"tunnel_actions": {"data-transfer": {"enabled": true, "actions": {"put": true}}}}
A tunnel is off until enabled; an action then follows its manifest's `default` unless the config names it.
Actions run as you, outside the agents' sandbox: that is their point (the SMB login is yours) and why they are
typed, checked, audited and off until you turn them on. Job-mode actions are recorded with the template jobs, so
job_status, wait_for_job and the concurrent-job cap cover them.
Standard library only.
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid

try:
    from . import rest_jobs, rest_sandbox
except ImportError:  # run as a script from the servers directory
    import rest_jobs
    import rest_sandbox
RESTError = rest_jobs.RESTError

ACTIONS_FILE = "actions.json"
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
TUNNEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
PARAM_TYPES = ("cluster-path", "remote-path", "string", "integer", "boolean")
SCOPES = ("read", "submit", "files:write")
MODES = ("sync", "job")
SPECIAL = {"job_options", "scope_file", "helper", "data_dir"}
PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
REMOTE_PATH_RE = re.compile(r"(?![-/])(?!.*(?:^|/)\.\.(?:/|$))[^\0-\x1f\x7f]{0,1024}")
MAX_OUTPUT = 1 << 20
HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "action_paths.py")


def tunnel_dirs():
    """Where tunnels are, first match winning: $HPCLIB_TUNNEL_PATH, else the install location and hpclib's own."""
    given = [p for p in (os.environ.get("HPCLIB_TUNNEL_PATH") or "").split(":") if p]
    if given:
        return given
    home = os.path.expanduser("~")
    install = (os.environ.get("HPCLIB_TUNNEL_INSTALL_LOCATION") or os.environ.get("USER_TUNNEL_DIR") or
               os.path.join(home, ".local", "share", "hpclib", "tunnels"))
    return [install, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tunnels")]


def _text(value, what, limit, required=True):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise ValueError(f"{what} must be text of at most {limit} characters")
    return value.strip()


def _check_run(run, params, what, allow_job=False):
    if not isinstance(run, list) or not run or len(run) > 32 or not all(isinstance(a, str) and len(a) <= 512 for a in run):
        raise ValueError(f"{what}.run must be a list of at most 32 strings")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", run[0]):
        raise ValueError(f"{what}.run[0] must be a script in the tunnel's directory")
    for arg in run[1:]:
        names = PLACEHOLDER_RE.findall(arg)
        if not names:
            if "{" in arg or "}" in arg:
                raise ValueError(f"{what}.run: {arg!r} has a brace that isn't a {{placeholder}}")
            continue
        if arg != "{" + names[0] + "}" or len(names) > 1:
            if names == ["data_dir"]:
                continue       # {data_dir} may be part of an argument (a log path)
            raise ValueError(f"{what}.run: a placeholder fills a whole argument ({arg!r})")
        name = names[0]
        if name not in params and name not in SPECIAL:
            raise ValueError(f"{what}.run: {{{name}}} is not a parameter")
        if name == "job_options" and not allow_job:
            raise ValueError(f"{what}.run: {{job_options}} is for job-mode actions")


def check_manifest(manifest, tunnel):
    """The tunnel's actions.json, normalized; ValueError if it isn't valid."""
    if not isinstance(manifest, dict) or set(manifest) - {"title", "description", "available", "actions"}:
        raise ValueError("actions.json has title, description, available and actions")
    out = {"tunnel": tunnel, "title": _text(manifest.get("title") or tunnel, "title", 80),
           "description": _text(manifest.get("description"), "description", 2000, required=False),
           "available": None, "actions": {}}
    avail = manifest.get("available")
    if avail is not None:
        if not isinstance(avail, dict) or set(avail) - {"run", "line", "require", "any", "show", "message", "cache"}:
            raise ValueError("available has run, line, require, any, show, message and cache")
        _check_run(avail.get("run"), {}, "available")
        for key in ("require", "any", "show"):
            v = avail.get(key) or []
            if not isinstance(v, list) or not all(isinstance(k, str) and re.fullmatch(r"[a-z_]{1,32}", k) for k in v):
                raise ValueError(f"available.{key} is a list of field names")
        line = avail.get("line")
        if line is not None and not (isinstance(line, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", line)):
            raise ValueError("available.line is the marker its JSON line starts with")
        cache = avail.get("cache", 30)
        if isinstance(cache, bool) or not isinstance(cache, int) or not 0 <= cache <= 3600:
            raise ValueError("available.cache is seconds, 0 to 3600")
        out["available"] = {"run": list(avail["run"]), "line": line, "require": list(avail.get("require") or []),
                            "any": list(avail.get("any") or []), "show": list(avail.get("show") or []),
                            "message": _text(avail.get("message"), "available.message", 300, required=False),
                            "cache": cache}
    actions = manifest.get("actions")
    if not isinstance(actions, dict) or not actions or len(actions) > 32:
        raise ValueError("actions maps names to actions (1 to 32)")
    for name, spec in actions.items():
        what = f"action {name}"
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"{what}: names are lower-case letters, digits, _ and -")
        if not isinstance(spec, dict) or set(spec) - {"title", "description", "mode", "default", "scope", "params",
                                                      "run", "output", "timeout", "writes"}:
            raise ValueError(f"{what}: unknown keys")
        mode = spec.get("mode", "sync")
        if mode not in MODES:
            raise ValueError(f"{what}: mode is sync or job")
        scope = spec.get("scope", "read")
        if scope not in SCOPES:
            raise ValueError(f"{what}: scope is one of {', '.join(SCOPES)}")
        default = spec.get("default", True)
        if not isinstance(default, bool):
            raise ValueError(f"{what}: default is true or false")
        writes = spec.get("writes")
        if writes not in (None, "cluster", "remote"):
            raise ValueError(f"{what}: writes is cluster, remote or absent")
        params = spec.get("params") or {}
        if not isinstance(params, dict) or len(params) > 16:
            raise ValueError(f"{what}: params maps names to parameters")
        checked = {}
        for pname, p in params.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", pname) or pname in SPECIAL:
                raise ValueError(f"{what}: parameter name {pname!r}")
            if not isinstance(p, dict) or p.get("type") not in PARAM_TYPES or \
                    set(p) - {"type", "description", "access", "default", "pattern", "minimum", "maximum"}:
                raise ValueError(f"{what}.{pname}: type is one of {', '.join(PARAM_TYPES)}")
            if p["type"] == "cluster-path" and p.get("access", "read") not in ("read", "write"):
                raise ValueError(f"{what}.{pname}: access is read or write")
            if "pattern" in p:
                if p["type"] != "string" or not isinstance(p["pattern"], str) or len(p["pattern"]) > 200:
                    raise ValueError(f"{what}.{pname}: pattern is for strings")
                re.compile(p["pattern"])
            if p["type"] == "string" and "pattern" not in p:
                raise ValueError(f"{what}.{pname}: a string parameter needs a pattern")
            checked[pname] = dict(p, description=_text(p.get("description"), f"{what}.{pname}.description", 300,
                                                        required=False))
            if p["type"] == "cluster-path":
                checked[pname]["access"] = p.get("access", "read")
        _check_run(spec.get("run"), checked, what, allow_job=mode == "job")
        if mode == "job" and "{job_options}" not in spec["run"]:
            raise ValueError(f"{what}: a job-mode action passes {{job_options}} to sbatch")
        timeout = spec.get("timeout", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 900:
            raise ValueError(f"{what}: timeout is seconds, 1 to 900")
        output = spec.get("output")
        if output is not None and (not isinstance(output, str) or len(output) > 512 or
                                   set(PLACEHOLDER_RE.findall(output)) - {"data_dir", "job_id"}):
            raise ValueError(f"{what}: output is a path with {{data_dir}} and {{job_id}}")
        out["actions"][name] = {"name": name, "title": _text(spec.get("title") or name, f"{what}.title", 80),
                                "description": _text(spec.get("description"), f"{what}.description", 1000),
                                "mode": mode, "default": default, "scope": scope, "writes": writes,
                                "params": checked, "run": list(spec["run"]), "output": output, "timeout": timeout}
    return out


def check_config(section):
    """The REST config's tunnel_actions section: {TUNNEL: {"enabled": bool, "actions": {NAME: bool}}}."""
    if section is None:
        return {}
    if not isinstance(section, dict) or len(section) > 64:
        raise ValueError("`tunnel_actions` maps tunnel names to {enabled, actions}")
    for tunnel, entry in section.items():
        if not TUNNEL_RE.fullmatch(tunnel):
            raise ValueError(f"tunnel_actions: {tunnel!r} is not a tunnel name")
        if not isinstance(entry, dict) or set(entry) - {"enabled", "actions"} or \
                not isinstance(entry.get("enabled", False), bool):
            raise ValueError(f"tunnel_actions.{tunnel} is {{\"enabled\": true|false, \"actions\": {{NAME: true|false}}}}")
        acts = entry.get("actions") or {}
        if not isinstance(acts, dict) or not all(NAME_RE.fullmatch(k) and isinstance(v, bool) for k, v in acts.items()):
            raise ValueError(f"tunnel_actions.{tunnel}.actions maps action names to true or false")
    return section


class TunnelActions:
    """The installed tunnels' actions, what the config turns on, and running them."""

    def __init__(self, config=None, dirs=None, data_dir=None, runner=None):
        self.config = check_config(config)
        self.dirs = dirs
        self.data_dir = data_dir or os.path.expanduser(os.environ.get("HPCTUNNELS_DATA_DIR") or "~/.local/tunnels")
        self.runner = runner or self._run
        self._availability = {}          # tunnel -> (time, result)
        self.lock = threading.Lock()

    # -- what there is ---------------------------------------------------------------------------------------

    def manifests(self):
        """{tunnel: (manifest or {"error"}, directory)}, the first directory on the path winning."""
        out = {}
        for root in self.dirs if self.dirs is not None else tunnel_dirs():
            try:
                names = sorted(os.listdir(root))
            except OSError:
                continue
            for tunnel in names:
                path = os.path.join(root, tunnel, ACTIONS_FILE)
                if tunnel in out or not TUNNEL_RE.fullmatch(tunnel) or not os.path.isfile(path):
                    continue
                try:
                    with open(path) as f:
                        out[tunnel] = (check_manifest(json.load(f), tunnel), os.path.join(root, tunnel))
                except (OSError, ValueError) as e:
                    out[tunnel] = ({"tunnel": tunnel, "error": f"{ACTIONS_FILE}: {e}"}, os.path.join(root, tunnel))
        return out

    def tunnel_enabled(self, tunnel):
        return bool((self.config.get(tunnel) or {}).get("enabled", False))

    def action_enabled(self, tunnel, action):
        if not self.tunnel_enabled(tunnel):
            return False
        chosen = ((self.config.get(tunnel) or {}).get("actions") or {}).get(action["name"])
        return action["default"] if chosen is None else chosen

    def availability(self, tunnel, manifest, directory, refresh=False):
        avail = manifest.get("available")
        if avail is None:
            return {"available": True}
        with self.lock:
            cached = self._availability.get(tunnel)
        if cached and not refresh and time.time() - cached[0] < avail["cache"]:
            return cached[1]
        try:
            code, stdout, stderr = self.runner(self._argv(directory, avail["run"]), directory, 60)
        except subprocess.TimeoutExpired:
            code, stdout, stderr = None, "", "timed out"
        info = None
        for line in reversed(stdout.splitlines()):
            text = line[len(avail["line"]) + 1:] if avail["line"] and line.startswith(avail["line"] + " ") else \
                (line if not avail["line"] else None)
            if text is None:
                continue
            try:
                info = json.loads(text)
                break
            except ValueError:
                continue
        if code != 0 or not isinstance(info, dict):
            result = {"available": False, "reason": f"its status check failed: {(stderr or stdout).strip()[-300:]}"}
        else:
            ok = all(info.get(k) for k in avail["require"]) and (not avail["any"] or any(info.get(k) for k in avail["any"]))
            result = {"available": bool(ok), "detail": {k: info.get(k) for k in avail["show"]}}
            if not ok:
                result["reason"] = avail["message"] or "it isn't ready"
        with self.lock:
            self._availability[tunnel] = (time.time(), result)
        return result

    def describe(self, identity, owner, refresh=False):
        """GET /actions: for the owner every tunnel's actions and whether each is on; for an agent the ones it may use."""
        out = []
        for tunnel, (m, directory) in sorted(self.manifests().items()):
            if "error" in m:
                if owner:
                    out.append({"tunnel": tunnel, "error": m["error"], "enabled": self.tunnel_enabled(tunnel)})
                continue
            if not owner and not self.tunnel_enabled(tunnel):
                continue
            actions = []
            for a in m["actions"].values():
                on = self.action_enabled(tunnel, a)
                permitted = identity.has_scope(a["scope"])
                if not owner and not (on and permitted):
                    continue
                actions.append({"name": a["name"], "title": a["title"], "description": a["description"],
                                "mode": a["mode"], "scope": a["scope"], "writes": a["writes"],
                                "default": a["default"], "enabled": on, "permitted": permitted,
                                "params": {k: dict(p) for k, p in a["params"].items()}})
            if not actions and not owner:
                continue
            entry = {"tunnel": tunnel, "title": m["title"], "description": m["description"],
                     "enabled": self.tunnel_enabled(tunnel), "actions": actions}
            if self.tunnel_enabled(tunnel) or refresh:
                entry["availability"] = self.availability(tunnel, m, directory, refresh)
            out.append(entry)
        return {"tunnels": out}

    # -- running one -----------------------------------------------------------------------------------------

    @staticmethod
    def _argv(directory, run):
        script = os.path.join(directory, run[0])
        if os.path.realpath(os.path.dirname(script)) != os.path.realpath(directory) or not os.path.isfile(script):
            raise RESTError(500, f"{run[0]} is not in the tunnel's directory")
        return (["bash", script] if script.endswith(".sh") else [script]) + list(run[1:])

    @staticmethod
    def _run(argv, cwd, timeout):
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=dict(rest_sandbox.child_env(),
                                                                            SMB_NONINTERACTIVE="1"),
                                start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass
            proc.communicate()
            raise
        return proc.returncode, stdout[-MAX_OUTPUT:], stderr[-65536:]

    def check_params(self, action, given, whitelist, file_changes):
        """The arguments' values, checked: {name: value (a resolved path for cluster-path)}."""
        if not isinstance(given, dict):
            raise RESTError(400, "params must be an object")
        unknown = set(given) - set(action["params"])
        if unknown:
            raise RESTError(422, f"unknown parameters {sorted(unknown)}", params=sorted(action["params"]))
        values = {}
        for name, p in action["params"].items():
            if name not in given:
                if "default" not in p:
                    raise RESTError(422, f"{name} is required")
                value = p["default"]
            else:
                value = given[name]
            kind = p["type"]
            if kind == "boolean":
                if not isinstance(value, bool):
                    raise RESTError(422, f"{name} must be true or false")
                values[name] = "true" if value else "false"
            elif kind == "integer":
                if isinstance(value, bool) or not isinstance(value, int) or \
                        ("minimum" in p and value < p["minimum"]) or ("maximum" in p and value > p["maximum"]):
                    raise RESTError(422, f"{name} must be a whole number in its range")
                values[name] = str(value)
            elif kind == "string":
                if not isinstance(value, str) or not re.fullmatch(p["pattern"], value) or value.startswith("-"):
                    raise RESTError(422, f"{name} must match {p['pattern']}")
                values[name] = value
            elif kind == "remote-path":
                if not isinstance(value, str) or not REMOTE_PATH_RE.fullmatch(value) or value.startswith(("smb:",)):
                    raise RESTError(422, f"{name} is a path relative to the server's folder: no leading / or -, "
                                         "no .., no control characters")
                values[name] = value
            else:     # cluster-path
                if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
                    raise RESTError(422, f"{name} must be a path in your directories")
                try:
                    _, real = whitelist.resolve(value)
                except PermissionError as e:
                    raise RESTError(403, f"{name}: {e}") from e
                if p["access"] == "write":
                    if not file_changes:
                        raise RESTError(403, f"{name}: file changes are disabled on this server")
                    if os.path.lexists(real):
                        raise RESTError(409, f"{name}: {value} exists; copies go into a new directory")
                    if not os.path.isdir(os.path.dirname(real)):
                        raise RESTError(422, f"{name}: {os.path.dirname(value) or '.'} is not a directory")
                elif not os.path.exists(real):
                    raise RESTError(404, f"{name}: {value} does not exist")
                values[name] = real
        return values

    def scope_file(self, whitelist):
        """The token's directories, for action_paths.py in the action's job: a private file of its own."""
        directory = os.path.join(self.data_dir, "sessions", "actions")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, f"scope-{uuid.uuid4().hex}.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"roots": list(whitelist.roots) if whitelist.restricted else None, "deny": list(whitelist.deny)}, f)
        return path

    def run(self, identity, tunnel, name, given, whitelist, file_changes, jobs=None, audit=None):
        manifests = self.manifests()
        if tunnel not in manifests or "error" in manifests[tunnel][0] or name not in manifests[tunnel][0]["actions"]:
            raise RESTError(404, f"no action {tunnel}/{name}")
        m, directory = manifests[tunnel]
        action = m["actions"][name]
        if not self.action_enabled(tunnel, action):
            raise RESTError(403, f"{tunnel}/{name} is turned off on this cluster (its owner turns actions on in the "
                                 "console: the cluster's Agents tab)")
        if not identity.has_scope(action["scope"]):
            raise RESTError(403, f"token {identity.name!r} lacks the `{action['scope']}` scope {tunnel}/{name} needs")
        values = self.check_params(action, given, whitelist, file_changes)
        avail = self.availability(tunnel, m, directory)
        if not avail["available"]:
            raise RESTError(409, f"{m['title']} isn't available: {avail.get('reason')}", availability=avail)
        if audit is not None:
            audit.update(tunnel=tunnel, action=name, params={k: v for k, v in values.items()})
        special = {"helper": HELPER, "data_dir": self.data_dir}
        if any(a == "{scope_file}" for a in action["run"]):
            special["scope_file"] = self.scope_file(whitelist)
        job_options = [f"--comment=hpclib-rest:{identity.name}:{tunnel}/{name}"]

        argv = []
        for arg in action["run"][1:]:
            if arg == "{job_options}":
                argv += job_options
                continue
            ph = PLACEHOLDER_RE.fullmatch(arg)
            if ph and ph.group(1) in values:
                argv.append(values[ph.group(1)])
            elif ph:
                argv.append(special[ph.group(1)])
            else:
                argv.append(arg.replace("{data_dir}", self.data_dir))
        command = self._argv(directory, [action["run"][0]]) + argv
        if action["mode"] == "job" and jobs is not None:
            with jobs.submit_lock:        # the cap, the submission and its record together, as for template jobs
                jobs.check_capacity(1)
                return self._finish(identity, tunnel, name, action, values, directory, command, jobs, audit)
        return self._finish(identity, tunnel, name, action, values, directory, command, jobs, audit)

    def _finish(self, identity, tunnel, name, action, values, directory, command, jobs, audit):
        try:
            code, stdout, stderr = self.runner(command, directory, action["timeout"])
        except subprocess.TimeoutExpired:
            raise RESTError(504, f"{tunnel}/{name} took over {action['timeout']} s")
        if code != 0:
            raise RESTError(502, f"{tunnel}/{name} failed (exit {code})", stderr=stderr.strip()[-2000:])
        if action["mode"] == "sync":
            text = stdout.strip()
            try:
                result = json.loads(text) if text else None
            except ValueError:
                result = text
            return 200, {"tunnel": tunnel, "action": name, "result": result, "stderr": stderr.strip()[-2000:]}
        job_id = next((ln.strip().split(";")[0] for ln in reversed(stdout.splitlines()) if
                       re.fullmatch(r"\d+(;\S+)?", ln.strip())), None)
        if job_id is None:
            raise RESTError(502, f"{tunnel}/{name} didn't say which job it submitted", stdout=stdout[-2000:])
        output = (action["output"] or "").replace("{data_dir}", self.data_dir).replace("{job_id}", job_id) or None
        if audit is not None:
            audit["job_id"] = job_id
        view = jobs.record_action_job(identity.name, f"{tunnel}/{name}", {k: v for k, v in values.items()},
                                      job_id, output, workdir=directory) if jobs is not None else {"job_id": job_id}
        return 201, {"tunnel": tunnel, "action": name, "job": view, "stderr": stderr.strip()[-2000:]}
