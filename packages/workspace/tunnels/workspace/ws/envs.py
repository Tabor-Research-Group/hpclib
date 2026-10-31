"""
The Workspace's environments: what uv or pixi would make of a project, whether that has been done, and doing it, as the
user, on the login node. It reads with hpclib's envinfo (the same module the REST server uses, so "ready" means one
thing) and writes the same files beside the environment, so the REST server's sync and this one see each other's work.

  Environments(run, hpclib_path)
    .card(path)                  the environment card for a file or directory: project, manager, status word and why,
                                 the tools (uv, pixi) and their directories, an operation at work
    .prepare(project, update)    start `uv sync` / `pixi install` as a child of the server -> {id}
    .install_tool(tool)          `hpclib_pytools install uv|pixi` the same way
    .operation(id)               {state, kind, log tail, finished, exit_code} of one of those
    .set_dir(var, directory)     `hpclib_pytools set VAR DIR|default`
    .tools() / .dirs()           where uv and pixi are (read directly, fast); their directories (hpclib_pytools, slow)

A Prepare writes the busy marker (`.venv.hpclib-syncing`: pid, kind, id) before it starts and the record
(`.venv.hpclib.json`: when, the interpreter, the packages, or `failed`) when it ends, and a log under the Workspace's
data directory, which the page follows. It runs on the login node, like the terminal does: what uv would have
downloaded, the user's quota and the site's rules for login nodes apply to it just as they apply to a terminal.
If the Workspace itself stops while one runs, the operation finishes without a record: the marker's process is gone, and
the card says "not prepared" (or "stale") again.

Python 3.6 or newer, standard library only.
"""
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time

try:
    import envinfo
except ImportError:                       # imported as a package from the repository
    from hpclib.servers import envinfo

TOOL_VARS = ("UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "PIXI_CACHE_DIR")
ID_RE = re.compile(r"[a-f0-9]{16}")
LOG_TAIL = 6000


class Environments:
    def __init__(self, run, hpclib, home, state_dir, environ=None):
        """`run(command, cwd, timeout)` -> (code, output) in a login shell; `hpclib` is hpclib.sh's path (or None);
        `state_dir()` the directory for logs and run records."""
        self.run, self.hpclib, self.home, self.state_dir = run, hpclib, home, state_dir
        self.operations = {}
        self.lock = threading.Lock()
        self.environ = environ            # () -> the login shell's environment, for finding uv and pixi without a shell
        self._tools = None                # (time, answer): where uv and pixi are, looked at most every TOOLS_SECONDS
        self._dirs = None                 # (time, answer): hpclib_pytools --json (a login shell and an interactive one: slow)

    # -- reading -------------------------------------------------------------------------------------------------

    def _hpclib(self, command, timeout=60):
        """A command with hpclib.sh's functions (hpclib_pytools) in a login shell."""
        if not self.hpclib:
            return 127, "hpclib.sh wasn't found on this cluster"
        return self.run(". {} && {}".format(shlex.quote(self.hpclib), command), None, timeout)

    TOOLS_SECONDS = 30
    DIRS_SECONDS = 60
    KNOWN = {"uv": (".local/bin/uv", ".cargo/bin/uv"), "pixi": (".pixi/bin/pixi",)}

    def _find(self, name):
        """The program's path, found as hpclib_pytools does (the PATH, then the places the installers use) but without a
        shell: a login node whose profile is slow must not make the Submit form wait to learn where uv is."""
        path = ((self.environ() if self.environ else None) or os.environ).get("PATH", "")
        found = shutil.which(name, path=path)
        for candidate in ([found] if found else []) + [os.path.join(self.home, c) for c in self.KNOWN[name]]:
            if candidate and os.access(candidate, os.X_OK) and os.path.isfile(candidate):
                return candidate
        return ""

    def tools(self, fresh=False):
        """{tools: {uv: {path, version}, pixi: ...}}: looked at again after TOOLS_SECONDS (every card and every job form
        asks) or when `fresh` (after an install). Reads the file system and runs `--version`: no login shell."""
        now = time.time()
        if not fresh and self._tools and now - self._tools[0] < self.TOOLS_SECONDS:
            return self._tools[1]
        found = {}
        for name in ("uv", "pixi"):
            path, version = self._find(name), ""
            if path:
                try:
                    res = subprocess.run([path, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                         universal_newlines=True, timeout=15, env=(self.environ() if self.environ else None))
                    version = (res.stdout.splitlines() or [""])[0].strip()
                except (OSError, subprocess.SubprocessError):
                    pass
            found[name] = {"path": path, "version": version}
        answer = {"tools": found}
        self._tools = (now, answer)
        return answer

    def dirs(self, fresh=False):
        """{dirs: {VAR: {value, source, saved}}, settings_file, problem?} from `hpclib_pytools --json`, which also asks an
        interactive shell what ~/.bashrc sets: a slow question, so only the card's directory section asks it, when opened."""
        now = time.time()
        if not fresh and self._dirs and now - self._dirs[0] < self.DIRS_SECONDS:
            return self._dirs[1]
        try:
            code, out = self._hpclib("hpclib_pytools --json", 90)
        except Exception as e:  # noqa: BLE001  (a timeout is Refused: the card says so rather than failing)
            code, out = 124, "hpclib_pytools didn't answer: {}".format(e)
        answer = {"tools": {}, "dirs": {}, "problem": " ".join(out.split())[-300:] or "hpclib_pytools gave no answer"}
        for line in out.splitlines():
            if line.startswith("HPCLIB_PYTOOLS "):
                try:
                    answer = json.loads(line[len("HPCLIB_PYTOOLS "):])
                except ValueError:
                    pass
                break
        if not answer.get("problem"):
            self._dirs = (now, answer)
        return answer

    def card(self, path, manager="auto", name="default"):
        """What the environment card shows for `path` (a file or directory): the project found at or above it."""
        began = time.time()
        project = envinfo.find_project(path, self.home)
        tools = self.tools(fresh=True)               # the card is the person looking: it refreshes what the form's previews reuse
        took = {"tools": round(time.time() - began, 2)}
        dirs = self._dirs[1] if self._dirs else None  # the directories are asked for when that part of the card is opened
        card = {"start": path, "tools": tools.get("tools") or {}, "dirs": (dirs or {}).get("dirs") or {},
                "dirs_loaded": dirs is not None, "settings_file": (dirs or {}).get("settings_file"), "took": took}
        if project is None:
            card.update(found=False, status="none found",
                        reason="no pyproject.toml (uv) or pixi.toml (pixi) at or above here")
            return card
        began = time.time()
        info = envinfo.detect(project, manager, name)
        took["detect"] = round(time.time() - began, 2)
        word, reason = envinfo.status(info)
        wanted = info.get("manager")
        if wanted and not (card["tools"].get(wanted) or {}).get("path") and word in ("not prepared", "stale"):
            word, reason = "tool missing", "{} isn't installed for you yet".format(wanted)
        card.update(found=True, info=info, project=project, manager=wanted, status=word, reason=reason,
                    operation=self._running_for(project))
        return card

    def _running_for(self, project):
        with self.lock:
            for op in self.operations.values():
                if op["project"] == project and op["state"] == "running":
                    return self._public(op)
        return None

    # -- doing ---------------------------------------------------------------------------------------------------

    def prepare(self, project, update=False, manager="auto", name="default"):
        info = envinfo.detect(project, manager, name)
        if not info.get("manager"):
            raise envinfo.EnvironmentError_(422, info.get("problem") or "no environment manifest here")
        if info.get("busy"):
            raise envinfo.EnvironmentError_(409, info["problem"])
        tool = ((self.tools().get("tools") or {}).get(info["manager"]) or {}).get("path")
        if not tool:
            raise envinfo.EnvironmentError_(412, "{} isn't installed for you yet: install it first".format(info["manager"]))
        env = info.get("environment") or "default"
        q = shlex.quote
        if info["manager"] == "uv":
            command = [tool, "sync"] + (["--locked"] if info.get("locked") and not update else [])
        else:
            command = [tool, "install", "--manifest-path", info["manifest"], "-e", env] + (["--locked"] if info.get("locked") and not update else [])
        shown = " ".join(q(c) for c in command)
        script = ["set -e", "cd " + q(project)]
        if self.hpclib:
            script.append("[ -f {0} ] && . {0}      # the uv/pixi directories saved for this cluster".format(q(self.hpclib)))
        script += ["export UV_NO_PROGRESS=true PIXI_NO_PROGRESS=true NO_COLOR=1", "echo " + q("$ " + shown), shown]
        return self._start("sync", project, script, info=info, env=env)

    def install_tool(self, tool):
        if tool not in envinfo.MANAGERS:
            raise envinfo.EnvironmentError_(400, "tool: uv or pixi")
        if not self.hpclib:
            raise envinfo.EnvironmentError_(412, "hpclib.sh wasn't found on this cluster, and it does the installing")
        self._tools = None
        script = ["set -e", ". " + shlex.quote(self.hpclib), "hpclib_pytools install " + tool]
        return self._start("install " + tool, self.home, script)

    def set_dir(self, var, directory):
        if var not in TOOL_VARS:
            raise envinfo.EnvironmentError_(400, "var: one of " + ", ".join(TOOL_VARS))
        if not isinstance(directory, str) or not (directory == "default" or directory.startswith("/")):
            raise envinfo.EnvironmentError_(400, "dir: an absolute path, or default")
        code, out = self._hpclib("hpclib_pytools set {} {}".format(var, shlex.quote(directory)), 60)
        if code != 0:
            raise envinfo.EnvironmentError_(400, out.strip()[-300:] or "hpclib_pytools set failed")
        self._dirs = None
        return {"dirs": self.dirs(fresh=True).get("dirs") or {}}

    def _start(self, kind, project, script, info=None, env=None):
        op_id = secrets.token_hex(8)
        log_dir = os.path.join(self.state_dir(), "operations")
        os.makedirs(log_dir, mode=0o700, exist_ok=True)
        op = {"id": op_id, "kind": kind, "project": project, "state": "running", "started": time.time(),
              "log": os.path.join(log_dir, op_id + ".log"), "exit_code": None, "finished": None}
        marker = None
        if info:
            marker = envinfo.side_path(info["project"], info["manager"], env, envinfo.MARKER_SUFFIX)
        with self.lock:
            self.operations[op_id] = op
        threading.Thread(target=self._run, args=(op, script, info, env, marker), daemon=True).start()
        return self._public(op)

    def _run(self, op, script, info, env, marker):
        code = None
        try:
            with open(op["log"], "wb") as log:
                proc = subprocess.Popen(["bash", "-lc", "\n".join(script)], stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, cwd=op["project"], start_new_session=True)
                if marker:
                    with open(marker, "w") as f:
                        json.dump({"pid": proc.pid, "started": op["started"], "kind": "sync", "id": op["id"]}, f)
                code = proc.wait()
        except OSError as e:
            with open(op["log"], "a") as log:
                log.write("hpclib: could not start: {}\n".format(e))
        ok = code == 0
        if info:
            try:
                self._record(info, env, ok)
            except OSError as e:
                with open(op["log"], "a") as log:
                    log.write("hpclib: could not write the environment's record: {}\n".format(e))
            for suffix in (envinfo.MARKER_SUFFIX,):
                try:
                    os.unlink(envinfo.side_path(info["project"], info["manager"], env, suffix))
                except OSError:
                    pass
        with self.lock:
            op.update(state="succeeded" if ok else "failed", exit_code=code, finished=time.time())
            self._tools = None

    def _record(self, info, env, ok):
        """The sidecar JSON a sync leaves (the REST server's fields: synced, python, packages, failed)."""
        path = envinfo.side_path(info["project"], info["manager"], env, envinfo.RECORD_SUFFIX)
        record = envinfo.read_record(dict(info, environment=env))
        if ok:
            record.update(synced=time.time(), failed=None)
            python = self._python(info)
            if python:
                record["python"] = python
            pins = self._packages(info, env)
            if pins is not None:
                record["packages"] = pins
        else:
            record["failed"] = time.time()
        record.update(manager=info["manager"], project=info["project"], environment=info.get("environment"), path=info["path"])
        record = {k: v for k, v in record.items() if v is not None}
        with open(path + ".tmp", "w") as f:
            json.dump(record, f, indent=1)
        os.replace(path + ".tmp", path)

    @staticmethod
    def _python(info):
        exe = os.path.join(info["path"], "bin", "python")
        if not os.access(exe, os.X_OK):
            return None
        probe = ("import json,sys,sysconfig;print(json.dumps({'version':sys.version.split()[0],"
                 "'base_executable':getattr(sys,'_base_executable',sys.executable),"
                 "'site_packages':sysconfig.get_paths()['purelib']}))")
        try:
            res = subprocess.run([exe, "-c", probe], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL, universal_newlines=True, timeout=30)
            return json.loads(res.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def _packages(self, info, env):
        tool = ((self.tools().get("tools") or {}).get(info["manager"]) or {}).get("path")
        if not tool:
            return None
        if info["manager"] == "uv":
            args = [tool, "pip", "list", "--format=json", "--python", os.path.join(info["path"], "bin", "python")]
        else:
            args = [tool, "list", "--json", "--manifest-path", info["manifest"], "-e", env]
        try:
            res = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                 universal_newlines=True, timeout=120, cwd=info["project"])
            data = json.loads(res.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return [{"name": p.get("name"), "version": p.get("version")} for p in data if isinstance(p, dict) and p.get("name")]

    # -- following -----------------------------------------------------------------------------------------------

    def _public(self, op):
        try:
            with open(op["log"], "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - LOG_TAIL))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            tail = ""
        return {"id": op["id"], "kind": op["kind"], "project": op["project"], "state": op["state"],
                "started": op["started"], "finished": op["finished"], "exit_code": op["exit_code"], "log": tail}

    def operation(self, op_id):
        if not isinstance(op_id, str) or not ID_RE.fullmatch(op_id):
            raise envinfo.EnvironmentError_(400, "id: an operation's id")
        with self.lock:
            op = self.operations.get(op_id)
        if op is None:
            raise envinfo.EnvironmentError_(404, "no such operation (the Workspace was restarted?)")
        return self._public(op)
