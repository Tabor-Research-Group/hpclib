"""
The login shell's environment, read once. Sites put sbatch, squeue and the rest on the PATH from the login shell's
profile, so the Workspace used to start `bash -lc` for every query, which is slow on a busy login node and lets a
profile's banner into the answers. Now the environment is captured once (`bash -lc 'env -0'`), and programs are run
directly with it:

  ShellEnv().run(["squeue", "-h", "-j", "42"], cwd=None, timeout=60, stdin_text=None) -> (code, output)
  ShellEnv().login("module -t avail")      -> (code, output)    only for what a program can't be: shell functions (module)
  ShellEnv().env(refresh=False)            the captured environment (a dict)

A program that isn't found asks once for a fresh capture (a profile may have changed since) before giving up with
code 127. Output is stdout and stderr together, as sbatch writes its answers to both.

Python 3.6 or newer, standard library only.
"""
import os
import subprocess
import threading
import time

from .common import Refused

MARK = b"HPCLIB_ENV_BEGIN"
STALE_AFTER = 30 * 60           # seconds: a capture older than this is taken again when a program isn't found
DROP = ("WORKSPACE_TOKEN",)


class ShellEnv:
    def __init__(self, shell="bash"):
        self.shell = shell
        self._env = None
        self._at = 0
        self._lock = threading.Lock()

    def env(self, refresh=False):
        with self._lock:
            if refresh or self._env is None:
                self._env, self._at = self._capture(), time.time()
            return dict(self._env)

    def _capture(self):
        """The environment a login shell ends up with; this process's own when the shell can't say."""
        try:
            res = subprocess.run([self.shell, "-lc", 'printf "HPCLIB_ENV_BEGIN\\0"; env -0'], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=60)
            at = res.stdout.rfind(MARK + b"\0")
            if at >= 0:                          # what a profile printed before the marker is not the environment
                found = {}
                for item in res.stdout[at + len(MARK) + 1:].split(b"\0"):
                    name, eq, value = item.decode("utf-8", "replace").partition("=")
                    if eq and name.isidentifier():
                        found[name] = value
                if found.get("PATH"):
                    return {k: v for k, v in found.items() if k not in DROP}
        except (OSError, subprocess.SubprocessError):
            pass
        return {k: v for k, v in os.environ.items() if k not in DROP}

    def run(self, argv, cwd=None, timeout=60, stdin_text=None):
        """(code, output) of a program run with the login environment; 127 when it isn't there."""
        for attempt in (0, 1):
            env = self.env(refresh=attempt == 1)
            try:
                res = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     input=stdin_text, stdin=None if stdin_text is not None else subprocess.DEVNULL,
                                     universal_newlines=True, timeout=timeout)
                return res.returncode, res.stdout or ""
            except FileNotFoundError:
                if attempt == 0 and time.time() - self._at > 1:
                    continue                      # a profile may have changed: look again once
                return 127, "{}: command not found".format(argv[0])
            except subprocess.TimeoutExpired:
                raise Refused(504, "it took over {} s".format(timeout))
        return 127, "{}: command not found".format(argv[0])

    def login(self, command, cwd=None, timeout=60, stdin_text=None):
        """(code, output) of a shell command in a login shell: for `module` and the like, which are shell functions.
        What the profile prints (a banner, a message of the day) comes before a marker and is dropped."""
        try:
            res = subprocess.run([self.shell, "-lc", "printf 'HPCLIB_LOGIN_BEGIN\\n'\n" + command], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 input=stdin_text, stdin=None if stdin_text is not None else subprocess.DEVNULL,
                                 universal_newlines=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise Refused(504, "it took over {} s".format(timeout))
        out = res.stdout or ""
        return res.returncode, out.split("HPCLIB_LOGIN_BEGIN\n", 1)[-1]
