"""Console-owned directory sync store and explicit ignore-setup operations.

list/get/save/unregister/pause are local records only. setup_preview/setup_apply inspect
and, only after a reviewed decision, write the selected local root's metadata/.gitignore.
Transfer workers and the optional rclone bridge are not enabled in this stage.
"""
import contextlib
import copy
import fcntl
import json
import os
import secrets
import stat
import threading
import time

from sync_spec import (EXCLUDE_MODES, GIT_DEFAULTS, METADATA_FILE, SyncError, identifier, integer,
                       object_fields, overlap, validate, ignore_setup_plan, setup_metadata)

CAPABILITIES = {"records": True, "local_ignore_setup": True, "git_snapshot": False,
                "git_transfer": False, "rclone": False, "heartbeat": False}


def read_setup_file(root, name, limit, dir_fd=None):
    path = os.path.join(root, name) if dir_fd is None else name
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None, None
    except OSError as e:
        raise SyncError(422, f"cannot inspect {name}; use a regular file without symlinks: {e}") from e
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise SyncError(422, f"{name} must be a regular file smaller than {limit} bytes")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise SyncError(422, f"{name} grew during inspection; retry")
        return raw.decode("utf-8"), stat.S_IMODE(info.st_mode)
    except UnicodeDecodeError as e:
        raise SyncError(422, f"{name} must be UTF-8 for the setup editor") from e
    finally:
        os.close(fd)


class SyncManager:
    def __init__(self, root, profiles, write_private):
        self.root = os.path.abspath(root)
        self.profiles = profiles
        self.write_private = write_private
        self.lock = threading.RLock()

    @contextlib.contextmanager
    def _write_lock(self):
        with self.lock:
            os.makedirs(self.root, mode=0o700, exist_ok=True)
            fd = os.open(os.path.join(self.root, "store.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _store(self):
        try:
            with open(os.path.join(self.root, "specs.json")) as f:
                value = json.load(f)
        except FileNotFoundError:
            return {"v": 1, "records": {}}
        except (OSError, ValueError) as e:
            raise SyncError(500, f"cannot read sync records; preserve specs.json and repair it: {e}") from e
        if not isinstance(value, dict) or value.get("v") != 1 or not isinstance(value.get("records"), dict):
            raise SyncError(500, "unsupported sync store; preserve specs.json and repair it")
        return value

    def _persist(self, store):
        self.write_private(os.path.join(self.root, "specs.json"), json.dumps(store, indent=2, allow_nan=False) + "\n")

    def _record(self, store, sync_id):
        identifier(sync_id)
        try:
            return store["records"][sync_id]
        except KeyError as e:
            raise SyncError(404, "no sync with that ID") from e

    def _public(self, record):
        result = copy.deepcopy(record)
        result.pop("git_explicit", None)
        result["capabilities"] = dict(CAPABILITIES)
        return result

    def list(self):
        with self.lock:
            records = self._store()["records"].values()
            return {"syncs": [self._public(r) for r in records], "capabilities": dict(CAPABILITIES),
                    "defaults": {"git": dict(GIT_DEFAULTS), "interval": 60}}

    def get(self, sync_id):
        with self.lock:
            return self._public(self._record(self._store(), sync_id))

    def save(self, body, sync_id=None):
        spec = validate(body)
        explicit = sorted(body.get("git", {}))
        if self.profiles is not None:
            for target in spec["targets"]:
                # Use the existing profile resolver without initiating any remote request.
                profile = self.profiles(target["cluster"])
                if not profile or profile.get("name") != target["cluster"]:
                    raise SyncError(422, "targets must name an existing cluster profile")
        with self._write_lock():
            store = self._store()
            old = self._record(store, sync_id) if sync_id else None
            if old:
                revision = integer(body.get("revision"), "revision")
                if revision != old["spec"]["revision"]:
                    raise SyncError(409, "sync changed; reload it before saving")
                if body.get("id", sync_id) != sync_id:
                    raise SyncError(422, "sync ID cannot change")
            elif "id" in body or "revision" in body:
                raise SyncError(422, "IDs and revisions are assigned by the console")
            for other_id, record in store["records"].items():
                if other_id == sync_id:
                    continue
                other = record["spec"]
                if overlap(spec["local"], other["local"]):
                    raise SyncError(409, "this local root is already managed or overlaps another sync; add targets there",
                                    existing=other_id)
                for a in spec["targets"]:
                    for b in other["targets"]:
                        if a["cluster"] == b["cluster"] and overlap(a["remote"], b["remote"]):
                            raise SyncError(409, "remote roots overlap another sync", existing=other_id)
            # Catch overlapping destinations within one group as well.
            for i, a in enumerate(spec["targets"]):
                for b in spec["targets"][i + 1:]:
                    if a["cluster"] == b["cluster"] and overlap(a["remote"], b["remote"]):
                        raise SyncError(422, "remote roots within a cluster cannot overlap")
            sync_id = sync_id or "s-" + secrets.token_hex(12)
            spec.update(id=sync_id, revision=old["spec"]["revision"] + 1 if old else 1)
            status = {"state": "unverified", "message": "Saved locally. Review ignore setup before the first snapshot.",
                      "targets": [{"target": t["id"], "state": "unverified", "last_check": None,
                                   "last_successful_sync": None} for t in spec["targets"]]}
            store["records"][sync_id] = {"spec": spec, "git_explicit": explicit, "status": status,
                                          "updated": time.time()}
            self._persist(store)
            return self._public(store["records"][sync_id])

    def pause(self, sync_id, paused, revision):
        with self._write_lock():
            store = self._store()
            record = self._record(store, sync_id)
            if integer(revision, "revision") != record["spec"]["revision"]:
                raise SyncError(409, "sync changed; reload it")
            record["spec"]["paused"] = paused
            record["spec"]["revision"] += 1
            record["updated"] = time.time()
            self._persist(store)
            return self._public(record)

    def unregister(self, sync_id, body):
        object_fields(body, {"confirm", "revision"}, "unregister")
        if body.get("confirm") is not True:
            raise SyncError(422, "confirm unregistering this sync; working files and history are preserved")
        with self._write_lock():
            store = self._store()
            record = self._record(store, sync_id)
            if integer(body.get("revision"), "revision") != record["spec"]["revision"]:
                raise SyncError(409, "sync changed; reload it")
            # Archive the registration instead of deleting history or recovery material.
            self.write_private(os.path.join(self.root, "removed", sync_id + ".json"), json.dumps(record, indent=2) + "\n")
            del store["records"][sync_id]
            self._persist(store)
            return {"removed": sync_id, "preserved_history": True}


    def _setup(self, record):
        root = record["spec"]["local"]
        try:
            info = os.lstat(root)
        except OSError as e:
            raise SyncError(422, f"local root is not available: {e}") from e
        if not stat.S_ISDIR(info.st_mode):
            raise SyncError(422, "local root must be a directory, not a symlink")
        ignored, ignored_mode = read_setup_file(root, ".gitignore", 1 << 20)
        raw_metadata, _ = read_setup_file(root, METADATA_FILE, 1 << 16)
        result, merged = ignore_setup_plan(ignored, raw_metadata,
            {"id": record["spec"]["id"], "revision": record["spec"]["revision"],
             "root": [root, info.st_dev, info.st_ino]}, record["spec"], record["git_explicit"])
        result["message"] = "Ignore setup only. Git exclusion previews and transfers are not available yet."
        return result, merged, ignored_mode

    def setup_preview(self, sync_id):
        with self.lock:
            record = self._record(self._store(), sync_id)
            if record["spec"]["mode"] != "git":
                raise SyncError(501, "Git ignore setup is only for Git mode")
            return self._setup(record)[0]

    def setup_apply(self, sync_id, body):
        object_fields(body, {"revision", "setup"}, "setup request")
        setup = body.get("setup")
        object_fields(setup, {"mode", "review", "confirm"}, "setup")
        mode = setup.get("mode")
        if mode not in EXCLUDE_MODES or setup.get("confirm") is not True:
            raise SyncError(422, "choose gitignore, sync-only or off and explicitly confirm the setup writes")
        with self._write_lock():
            store = self._store()
            record = self._record(store, sync_id)
            if record["spec"]["mode"] != "git":
                raise SyncError(422, "ignore setup requires Git mode")
            preview, merged, ignored_mode = self._setup(record)
            if integer(body.get("revision"), "revision") != preview["revision"] or setup.get("review") != preview["review"]:
                raise SyncError(409, "setup files or settings changed; review the new preview")
            root = record["spec"]["local"]
            metadata = setup_metadata(preview, mode)
            try:
                if mode == "gitignore" and preview["merge_diff"]:
                    self.write_private(os.path.join(root, ".gitignore"), merged)
                    if ignored_mode is not None:
                        os.chmod(os.path.join(root, ".gitignore"), ignored_mode)
                self.write_private(os.path.join(root, METADATA_FILE), json.dumps(metadata, indent=2) + "\n")
            except OSError as e:
                # An interrupted setup may have written the marked block. A retry previews it idempotently.
                raise SyncError(409, f"setup did not finish; inspect the files and review again: {e}") from e
            record["spec"]["revision"] += 1
            record["updated"] = time.time()
            record["status"].update(state="configured", message="Ignore choice saved. Transfers are not enabled in this stage.")
            self._persist(store)
            return {"sync": self._public(record), "setup": self._setup(record)[0]}

    def unavailable(self, action):
        raise SyncError(501, f"{action} is not enabled in this implementation stage; no transfer or snapshot was started")

    def close(self):
        # No worker is started by constructing or reading the store in the records/setup stage.
        pass
