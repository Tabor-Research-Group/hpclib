"""
Updating the hpclib this console runs from (Console Settings -> Update hpclib): its git clone, from the
branch's upstream on GitHub (or wherever the clone came from).

  status(fetch)   the clone: branch, upstream, the commit here and there, how many commits behind and ahead,
                  local changes to tracked files, the commits an update brings, and HPCLIB_VERSION here and
                  upstream; fetch=True asks the remote first (git fetch)
  update()        fetch, then fast-forward (git merge --ff-only) when nothing stands in the way: tracked files
                  with local changes, local commits the upstream doesn't have, no upstream, a detached HEAD

Only fast-forwards: your changes and commits are never touched (the answer says what is in the way). Untracked
files are left alone. git never asks for a password here (GIT_TERMINAL_PROMPT=0): a remote that needs one
answers with an error. The new code runs once the console restarts (agent_console's POST /api/restart).
Standard library only; needs git.
"""
import os
import re
import subprocess
import threading
import time

VERSION_RE = re.compile(r'^HPCLIB_VERSION="([^"]+)"', re.M)
URL_CREDENTIALS_RE = re.compile(r"(://)[^/@]+@")


class UpdateError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Updater:
    def __init__(self, repo, git="git", fetch_timeout=90):
        self.repo = repo
        self.git_program = git
        self.fetch_timeout = fetch_timeout
        self.lock = threading.Lock()

    def git(self, *args, timeout=30):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", SSH_ASKPASS="", LC_ALL="C",
                   GIT_SSH_COMMAND=os.environ.get("GIT_SSH_COMMAND") or "ssh -o BatchMode=yes")
        try:
            return subprocess.run([self.git_program, "-C", self.repo, *args], capture_output=True, text=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL, env=env)
        except FileNotFoundError as e:
            raise UpdateError(501, "git isn't installed (or not on the console's PATH)") from e
        except subprocess.TimeoutExpired as e:
            raise UpdateError(504, f"git {args[0]} took over {timeout} s") from e

    def _out(self, *args):
        res = self.git(*args)
        return res.stdout.strip() if res.returncode == 0 else None

    @staticmethod
    def _version(text):
        m = VERSION_RE.search(text or "")
        return m.group(1) if m else None

    def status(self, fetch=False):
        top = self._out("rev-parse", "--show-toplevel")
        if top is None or os.path.realpath(top) != os.path.realpath(self.repo):
            return {"repo": self.repo, "git": False, "can_update": False,
                    "reason": "this hpclib isn't a git clone, so it can't update itself: run the installer again "
                              "(it clones hpclib), or replace this copy"}
        out = {"repo": self.repo, "git": True, "fetched": None, "fetch_error": None}
        if fetch:
            res = self.git("fetch", "--quiet", "--prune", timeout=self.fetch_timeout)
            if res.returncode != 0:
                out["fetch_error"] = (res.stderr or res.stdout).strip()[-600:] or f"git fetch failed ({res.returncode})"
        try:
            out["fetched"] = os.stat(os.path.join(self.repo, ".git", "FETCH_HEAD")).st_mtime
        except OSError:
            pass
        branch = self._out("symbolic-ref", "--quiet", "--short", "HEAD")
        upstream = self._out("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") if branch else None
        remote = self._out("config", f"branch.{branch}.remote") if branch else None
        url = self._out("remote", "get-url", remote) if remote else None
        here = self._out("rev-parse", "--short=12", "HEAD")
        try:
            with open(os.path.join(self.repo, "hpclib", "hpclib.sh")) as f:
                version_here = self._version(f.read())
        except OSError:
            version_here = None
        porcelain = self.git("status", "--porcelain", "--untracked-files=no")
        dirty = [line[3:] for line in porcelain.stdout.splitlines() if line.strip()] if porcelain.returncode == 0 else []
        out.update(branch=branch, upstream=upstream, remote_url=URL_CREDENTIALS_RE.sub(r"\1", url) if url else None,
                   commit=here, version=version_here, changed_files=dirty[:20], changed_count=len(dirty),
                   behind=0, ahead=0, upstream_commit=None, upstream_version=None, incoming=[])
        if upstream:
            counts = self._out("rev-list", "--left-right", "--count", "HEAD...@{u}")
            if counts and re.fullmatch(r"\d+\s+\d+", counts):
                out["ahead"], out["behind"] = map(int, counts.split())
            out["upstream_commit"] = self._out("rev-parse", "--short=12", "@{u}")
            out["upstream_version"] = self._version(self._out("show", "@{u}:hpclib/hpclib.sh"))
            log = self._out("log", "--format=%h%x09%ct%x09%s", "-n", "30", "HEAD..@{u}") or ""
            out["incoming"] = [dict(zip(("commit", "time", "subject"), line.split("\t", 2)))
                               for line in log.splitlines() if line.count("\t") >= 2]
            for c in out["incoming"]:
                c["time"] = int(c["time"]) if c["time"].isdigit() else None
        out["reason"] = self._blocker(out)
        out["can_update"] = out["reason"] is None and out["behind"] > 0
        out["up_to_date"] = out["reason"] is None and out["behind"] == 0
        return out

    @staticmethod
    def _blocker(st):
        if not st.get("branch"):
            return "the clone isn't on a branch (a detached HEAD): check out one (git switch master)"
        if not st.get("upstream"):
            return f"the branch {st['branch']} follows no remote branch (git branch -u origin/{st['branch']})"
        if st.get("changed_count"):
            return (f"{st['changed_count']} file(s) here have changes of your own ({', '.join(st['changed_files'][:5])}"
                    f"{'…' if st['changed_count'] > 5 else ''}): commit or stash them first")
        if st.get("ahead"):
            return (f"this clone has {st['ahead']} commit(s) {st['upstream']} doesn't; an update only fast-forwards, "
                    "so merge or rebase by hand")
        return None

    def update(self):
        if not self.lock.acquire(blocking=False):
            raise UpdateError(409, "an update is already running")
        try:
            before = self.status(fetch=True)
            if before.get("fetch_error"):
                raise UpdateError(502, f"couldn't reach {before.get('remote_url') or 'the remote'}: {before['fetch_error']}")
            if not before.get("git") or before.get("reason"):
                raise UpdateError(409, before["reason"])
            if not before["behind"]:
                return {"updated": False, "status": before}
            res = self.git("merge", "--ff-only", "--quiet", "@{u}", timeout=120)
            if res.returncode != 0:
                raise UpdateError(409, "git merge --ff-only: " + ((res.stderr or res.stdout).strip()[-600:] or "failed"))
            after = self.status()
            return {"updated": True, "from": {"commit": before["commit"], "version": before["version"]},
                    "to": {"commit": after["commit"], "version": after["version"]}, "commits": before["incoming"],
                    "status": after, "at": time.time()}
        finally:
            self.lock.release()
