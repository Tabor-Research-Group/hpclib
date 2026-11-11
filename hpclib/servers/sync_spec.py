"""Directory sync records: validation, default excludes, policy and change classification.

Pure functions only; paths are checked on disk by SyncManager when explicitly requested.
Console-side Python 3.10+, standard library only.
"""
import copy
import difflib
import hashlib
import json
import math
import os
import posixpath
import re

VERSION = 1
METADATA_FILE = ".hpclib-sync.json"
DEFAULT_EXCLUDES_VERSION = 1
DEFAULT_EXCLUDES = (".venv/", "venv/", "__pycache__/", "*.py[cod]", ".pytest_cache/",
                    ".ipynb_checkpoints/", "node_modules/", ".DS_Store", "Thumbs.db")
GIT_DEFAULTS = {"branch": "sync", "ignored": "exclude", "warn_file_mb": 10,
                "warn_snapshot_mb": 100, "warn_file_count": 1000}
EXCLUDE_MODES = ("gitignore", "sync-only", "off")
ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
TOP_FIELDS = frozenset(("v", "id", "revision", "name", "local", "mode", "interval", "policy",
                        "targets", "git", "rclone", "paused", "direction"))
GIT_FIELDS = frozenset(GIT_DEFAULTS) | {"default_excludes", "default_excludes_version"}


class SyncError(ValueError):
    def __init__(self, status, message, **details):
        super().__init__(message)
        self.status = status
        self.payload = dict(error=message, **details)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def object_fields(value, allowed, label):
    if not isinstance(value, dict):
        raise SyncError(422, f"{label} must be an object")
    extra = set(value) - set(allowed)
    if extra:
        raise SyncError(422, f"unknown {label} field(s): {', '.join(sorted(extra))}")


def integer(value, label, minimum=1, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum is not None and value > maximum):
        raise SyncError(422, f"{label} must be an integer from {minimum}" + (f" to {maximum}" if maximum else ""))
    return value


def identifier(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise SyncError(422, "invalid sync or target ID")
    return value


def absolute_path(value, label, remote=False):
    if not isinstance(value, str) or not value.startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SyncError(422, f"{label} must be an absolute path without control characters")
    if ".." in value.split("/"):
        raise SyncError(422, f"{label} cannot contain '..'")
    result = posixpath.normpath(value) if remote else os.path.realpath(value)
    if result in ("/", "//") or (not remote and result == os.path.realpath(os.path.expanduser("~"))):
        raise SyncError(422, f"{label} cannot be the filesystem root or your whole home directory")
    return result


def git_settings(value, metadata=False):
    object_fields(value, GIT_FIELDS, "git")
    result = copy.deepcopy(value)
    if metadata and set(value) - {"default_excludes", "default_excludes_version", "warn_file_mb", "warn_snapshot_mb", "warn_file_count"}:
        raise SyncError(422, "repo metadata may contain only portable ignore and warning settings")
    branch = value.get("branch", "sync")
    if not isinstance(branch, str) or not branch or branch.startswith(("-", "/")) or branch.endswith(("/", ".")) or any(
            part in branch for part in ("..", "//", "@{", "\\")) or any(
            ord(c) < 33 or ord(c) == 127 or c in "~^:?*[" for c in branch) or any(
            p.startswith(".") or p.endswith(".lock") for p in branch.split("/")) or branch == "@":
        raise SyncError(422, "invalid sync branch name")
    # The later Git boundary also runs check-ref-format; this rejects unsafe values without a subprocess.
    if value.get("ignored", "exclude") != "exclude":
        raise SyncError(501, "the optional rclone bridge is not implemented in this stage; use ignored: exclude")
    for key in ("warn_file_mb", "warn_snapshot_mb"):
        n = value.get(key, GIT_DEFAULTS[key])
        try:
            valid = not isinstance(n, bool) and isinstance(n, (int, float)) and math.isfinite(n) and n > 0
        except OverflowError:
            valid = False
        if not valid:
            raise SyncError(422, f"{key} must be a finite positive number")
    integer(value.get("warn_file_count", GIT_DEFAULTS["warn_file_count"]), "warn_file_count")
    if "default_excludes" in value and value["default_excludes"] not in EXCLUDE_MODES:
        raise SyncError(422, "default_excludes must be gitignore, sync-only or off")
    if "default_excludes_version" in value:
        if integer(value["default_excludes_version"], "default_excludes_version") != DEFAULT_EXCLUDES_VERSION:
            raise SyncError(422, "unsupported default exclude version; review setup before upgrading")
    return result


def validate_metadata(value):
    object_fields(value, {"v", "git"}, "repo metadata")
    if type(value.get("v")) is not int or value["v"] != VERSION:
        raise SyncError(422, "unsupported repo metadata version")
    result = git_settings(value.get("git", {}), metadata=True)
    if "default_excludes" not in result or "default_excludes_version" not in result:
        raise SyncError(422, "repo metadata must record the default exclude choice and version")
    return {"v": VERSION, "git": result}


def validate(value):
    object_fields(value, TOP_FIELDS, "sync")
    if type(value.get("v", VERSION)) is not int or value.get("v", VERSION) != VERSION:
        raise SyncError(422, "unsupported sync version")
    mode = value.get("mode", "git")
    if mode not in ("git", "rclone"):
        raise SyncError(422, "mode must be git or rclone")
    legacy_direction = (value.get("rclone") or {}).get("direction", "push") if isinstance(value.get("rclone"), dict) else "push"
    direction = value.get("direction", legacy_direction if mode == "rclone" else "both")
    if direction not in ("both", "push", "pull") or (mode == "rclone" and direction == "both"):
        raise SyncError(422, "direction must be both, push or pull; standalone rclone currently supports push/pull only")
    if mode == "rclone" and isinstance(value.get("rclone"), dict) and "direction" in value["rclone"] and direction != legacy_direction:
        raise SyncError(422, "direction and rclone.direction must agree")
    name = value.get("name", "")
    if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(ord(c) < 32 for c in name):
        raise SyncError(422, "name must be 1–120 printable characters")
    interval = integer(value.get("interval", 60 if mode == "git" else 300), "interval", 0, 86400)
    if 0 < interval < 10:
        raise SyncError(422, "interval must be 0 (manual) or at least 10 seconds")
    policy = value.get("policy", {})
    object_fields(policy, {"on_change", "merge"}, "policy")
    policy = dict(on_change=policy.get("on_change", "notify"), merge=policy.get("merge", "undecided"))
    if policy["on_change"] not in ("notify", "auto-push", "auto-both") or policy["merge"] not in ("undecided", "review"):
        raise SyncError(422, "unsupported change or merge policy")
    if "paused" in value and not isinstance(value["paused"], bool):
        raise SyncError(422, "paused must be boolean")
    targets = value.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 64:
        raise SyncError(422, "targets must contain 1–64 local/remote pairs")
    normalized, seen = [], set()
    for target in targets:
        object_fields(target, {"id", "cluster", "remote"}, "target")
        cluster = target.get("cluster")
        if not isinstance(cluster, str) or not re.fullmatch(r"[A-Za-z0-9._@-]{1,120}", cluster):
            raise SyncError(422, "invalid cluster profile name")
        remote = absolute_path(target.get("remote"), "remote", remote=True)
        key = (cluster, remote)
        if key in seen:
            raise SyncError(422, "duplicate cluster/remote target")
        seen.add(key)
        normalized.append({"id": "t-" + digest(key)[:24], "cluster": cluster, "remote": remote})
    git = dict(GIT_DEFAULTS, **git_settings(value.get("git", {})))
    rclone = value.get("rclone")
    if rclone is not None:
        object_fields(rclone, {"direction", "delete", "filter", "interval"}, "rclone")
        if rclone.get("direction", "push") not in ("push", "pull") or rclone.get("delete", False) is not False:
            raise SyncError(501, "only non-deleting rclone push/pull is planned for the next stage")
        if not isinstance(rclone.get("filter", []), list) or any(not isinstance(x, str) for x in rclone.get("filter", [])):
            raise SyncError(422, "rclone.filter must be a list of rules")
        if mode == "rclone" and "interval" in rclone:
            raise SyncError(422, "standalone rclone uses the top-level interval")
        if "interval" in rclone:
            integer(rclone["interval"], "rclone.interval", 0, 86400)
    result = {"v": VERSION, "name": name.strip(), "local": absolute_path(value.get("local"), "local"),
              "mode": mode, "interval": interval, "policy": policy, "targets": normalized,
              "git": git, "paused": value.get("paused", False), "direction": direction}
    if rclone is not None:
        result["rclone"] = copy.deepcopy(rclone)
        if mode == "rclone":
            result["rclone"]["direction"] = direction
    return result


def effective_git(spec, metadata=None, explicit=()):
    result, sources = dict(GIT_DEFAULTS), {k: "application default" for k in GIT_DEFAULTS}
    for key, value in (metadata or {}).get("git", {}).items():
        result[key], sources[key] = value, "repository metadata"
    for key in explicit:
        result[key], sources[key] = spec["git"][key], "sync setting"
    return result, sources


def overlap(a, b):
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def classify(local, remote, base):
    if base is None:
        return "initial-review"
    if local == remote:
        return "in-sync"
    if remote == base:
        return "local-changed"
    if local == base:
        return "remote-changed"
    return "both-changed"


def ignore_setup_plan(ignored, raw_metadata, binding, spec=None, explicit=()):
    """Pure ignore preview shared by the console and scoped cluster actions.

    The caller supplies already-read text and a root identity binding; filesystem access
    stays in the existing console/action boundaries.
    """
    metadata = None
    if raw_metadata is not None:
        try:
            metadata = validate_metadata(json.loads(raw_metadata))
        except (ValueError, TypeError) as e:
            raise SyncError(422, f"repair {METADATA_FILE} before continuing: {e}") from e
    newline = "\r\n" if ignored and "\r\n" in ignored else "\n"
    block = newline.join([f"# BEGIN hpclib sync defaults v{DEFAULT_EXCLUDES_VERSION}", *DEFAULT_EXCLUDES,
                          "# END hpclib sync defaults", "", ""])
    merged = ignored if ignored and block in ignored else block + (ignored or "")
    settings, sources = effective_git(spec or {"git": dict(GIT_DEFAULTS)}, metadata, explicit)
    review = digest({"binding": binding, "gitignore": ignored, "metadata": raw_metadata,
                     "default_version": DEFAULT_EXCLUDES_VERSION})
    result = {"review": review, "root": binding["root"][0], "metadata_file": METADATA_FILE,
              "metadata": metadata, "needs_setup": metadata is None, "gitignore_exists": ignored is not None,
              "default_version": DEFAULT_EXCLUDES_VERSION, "defaults": list(DEFAULT_EXCLUDES),
              "effective_git": settings, "setting_sources": sources,
              "merge_diff": "".join(difflib.unified_diff((ignored or "").splitlines(keepends=True),
                                  merged.splitlines(keepends=True), fromfile=".gitignore", tofile=".gitignore")),
              "path_preview_available": False}
    if "revision" in binding:
        result["revision"] = binding["revision"]
    return result, merged


def setup_metadata(preview, mode):
    if mode not in EXCLUDE_MODES:
        raise SyncError(422, "choose gitignore, sync-only or off")
    metadata = copy.deepcopy(preview["metadata"] or {"v": VERSION, "git": {}})
    metadata["git"].update(default_excludes=mode, default_excludes_version=DEFAULT_EXCLUDES_VERSION)
    return validate_metadata(metadata)
