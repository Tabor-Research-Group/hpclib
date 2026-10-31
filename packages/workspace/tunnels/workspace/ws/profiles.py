"""
Profiles: what a person asks of the cluster over and over, kept as named specs. A profile is the part of a job spec that
is about the cluster and the environment (resources, modules, the environment mode and shell init, the output name and
extra #SBATCH lines), never the script, its arguments or its folder. Three sources, in the order the form offers them:

  last used    the profile of the latest submission, kept here (not in the browser, so another browser has it)
  project      `.hpclib/job.json` in the project (the folder of the script or the nearest one above it, up to home):
               a file the team can commit, offered when a script there is submitted
  yours        named profiles saved with the form, one JSON file each under $HPCTUNNELS_DATA_DIR/workspace/profiles

  Profiles(home)
    .list(directory=None)        {last, project: {path, spec} | {path, problem} | None, profiles: [{name, spec}]}
    .save(name, spec)            keep a profile (the spec is cleaned the way the form's is)
    .delete(name)
    .remember(spec)              keep a submission's profile as "last used"

Python 3.6 or newer, standard library only.
"""
import json
import os
import re

from . import jobspec
from .common import MAX_JSON, Refused, oserror, resolve, state_dir

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.+@-]{0,63}")
KEYS = ("resources", "modules", "environment", "output", "extra_sbatch")
PROJECT_FILE = os.path.join(".hpclib", "job.json")


def profile_of(spec):
    """The profile part of a spec, cleaned, or raises Refused saying what is wrong with it."""
    probe = dict({k: v for k, v in (spec if isinstance(spec, dict) else {}).items() if k in KEYS},
                 workdir="/", kind="command", command="true", name="profile")
    clean, errors, _ = jobspec.validate(probe)
    if errors:
        raise Refused(400, "; ".join(errors))
    out = {k: clean[k] for k in KEYS}
    if out["output"] in ("%x-%j.out", "%x-%A_%a.out"):
        out["output"] = "%x-%j.out"       # the default follows the job: an array doesn't make it the profile's choice
    return out


class Profiles:
    def __init__(self, home):
        self.home = home

    @staticmethod
    def _dir():
        return os.path.join(state_dir(), "profiles")

    def _path(self, name):
        if not isinstance(name, str) or not NAME_RE.fullmatch(name.strip()) or name.strip() in ("last", "."):
            raise Refused(400, "name: a profile's name (letters, digits, spaces and _ . + @ -)")
        return os.path.join(self._dir(), name.strip() + ".json")

    @staticmethod
    def _read(path):
        try:
            with open(path) as f:
                value = json.load(f)
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _write(self, path, spec):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try:
            with open(path + ".tmp", "w") as f:
                json.dump(spec, f, indent=1)
            os.replace(path + ".tmp", path)
        except OSError as e:
            raise oserror(e, path)

    def project_file(self, directory):
        """The `.hpclib/job.json` at or above `directory`, not looking above home; None when there isn't one."""
        here = resolve(directory)
        home = os.path.realpath(self.home)
        while True:
            candidate = os.path.join(here, PROJECT_FILE)
            if os.path.isfile(candidate):
                return candidate
            if here in (home, "/") or not here.startswith(home.rstrip("/") + "/"):
                return None
            here = os.path.dirname(here)

    def list(self, directory=None):
        out = {"last": None, "project": None, "profiles": []}
        last = self._read(os.path.join(self._dir(), "last.json"))
        if last:
            out["last"] = last
        if directory:
            path = self.project_file(directory)
            if path:
                raw = self._read(path) if os.path.getsize(path) <= MAX_JSON else None
                try:
                    if raw is None:
                        raise Refused(400, "it isn't a JSON object")
                    out["project"] = {"path": path, "spec": profile_of(raw)}
                except Refused as e:
                    out["project"] = {"path": path, "problem": str(e)}
        try:
            names = sorted(os.listdir(self._dir()), key=str.lower)
        except OSError:
            names = []
        for name in names:
            if name.endswith(".json") and name != "last.json":
                spec = self._read(os.path.join(self._dir(), name))
                if spec:
                    out["profiles"].append({"name": name[:-5], "spec": spec})
        return out

    def save(self, name, spec):
        clean = profile_of(spec)
        self._write(self._path(name), clean)
        return {"name": name.strip(), "spec": clean}

    def delete(self, name):
        path = self._path(name)
        try:
            os.unlink(path)
        except FileNotFoundError:
            raise Refused(404, "no profile called {}".format(name))
        except OSError as e:
            raise oserror(e, path)
        return {"deleted": name.strip()}

    def remember(self, spec):
        self._write(os.path.join(self._dir(), "last.json"), profile_of(spec))
