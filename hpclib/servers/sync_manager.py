"""Console-owned sync records, reviewed setup, and reviewed/background Git directory sync.

Cached reads perform no Git/SSH scan. Snapshot runs preserve ordinary indexes/HEAD and
working files. Explicitly reviewed group plans use Git merge/apply; automatic uploads/two-way sync reuse those journals; rclone remains unavailable.
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
from concurrent.futures import ThreadPoolExecutor

from sync_spec import (EXCLUDE_MODES, GIT_DEFAULTS, METADATA_FILE, SyncError, identifier, integer, digest, classify,
                       object_fields, overlap, validate, ignore_setup_plan, setup_metadata)

CAPABILITIES = {"records": True, "local_ignore_setup": True, "git_snapshot": True, 'git_object_exchange': True,
                "git_transfer": True, "git_merge_review": True, "agent_actions": True, "rclone": False, "heartbeat": True}


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
    def __init__(self, root, profiles, write_private, remote=None, transfer=None):
        self.root = os.path.abspath(root)
        self.profiles = profiles
        self.write_private = write_private
        self.lock = threading.RLock()
        self.remote = remote
        self.transfer = transfer
        self.active = {}
        self.executor = None
        self.closing = False
        self.agent_stop = threading.Event()
        self.agent_thread = None
        self.agent_consumer = 'c-' + secrets.token_hex(12)
        self.agent_retry = {}
        self.schedule_due = {}
        self.schedule_failures = {}
        self.scheduling_held = False
        self.clock = time.monotonic

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
        path = os.path.join(self.root, "specs.json")
        self.write_private(path, json.dumps(store, indent=2, allow_nan=False) + "\n")
        # Apply admission/recovery context must survive a crash, not only an atomic rename.
        for item, flags in ((path, os.O_RDWR | os.O_NOFOLLOW), (self.root, os.O_RDONLY | os.O_DIRECTORY)):
            fd = os.open(item, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

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
        expected = record.get('endpoints', {}).get('local')
        if expected and not result['status'].get('running'):
            try:
                info = os.stat(record['spec']['local'], follow_symlinks=False)
                present = (stat.S_ISDIR(info.st_mode) and [record['spec']['local'], info.st_ino] == expected and
                           os.path.realpath(record['spec']['local']) == record['spec']['local'])
            except OSError:
                present = False
            if not present:
                result['status'].update(state='inactive', message='The registered local directory moved, disappeared, or was replaced. Restore it or review a new registration; no replacement will be created.')
        if result['status'].get('running') and not self._is_busy(record['spec']['id']):
            applying = bool(result['status'].get('applying'))
            result['status'].update(running=False, state='recovery-needed' if applying else 'interrupted',
                                    message='Retry the previous reviewed sync to reconcile its journals.' if applying else
                                    'The previous check was interrupted; run a fresh review.')
        return result

    def _is_busy(self, sync_id):
        if sync_id in self.active:
            return True
        try:
            # NFS maps flock(LOCK_EX) to a write lock, requiring a writable descriptor even for this probe.
            fd = os.open(os.path.join(self.root, 'run-locks', sync_id), os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)

    def _not_busy(self, sync_id):
        if sync_id and self._is_busy(sync_id):
            raise SyncError(409, 'a snapshot check is running; wait before changing its registration')

    def busy(self):
        with self.lock:
            return [sync_id + ': snapshot check' for sync_id in self.active]

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
            self._not_busy(sync_id)
            store = self._store()
            old = self._record(store, sync_id) if sync_id else None
            if old:
                if old['status'].get('unfinished_apply'):
                    raise SyncError(409, 'retry/reconcile the interrupted reviewed apply before changing settings', state='recovery-needed')
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
            if old and all(old['spec'][key] == spec[key] for key in ('local', 'mode')) and old['spec']['git']['branch'] == spec['git']['branch']:
                ids = {t['id'] for t in spec['targets']}
                stamps = dict(old['status'].get('last_successes', {}))
                stamps.update({t['target']: t['last_successful_sync'] for t in old['status'].get('targets', []) if t.get('last_successful_sync')})
                status['last_successes'] = {key: value for key, value in stamps.items() if key in ids}
                for target in status['targets']:
                    target['last_successful_sync'] = status['last_successes'].get(target['target'])
            allowed = bool(old and old.get('agent_control') and all(old['spec'][key] == spec[key] for key in ('local', 'targets', 'mode')))
            store["records"][sync_id] = {"spec": spec, "git_explicit": explicit, "status": status, 'agent_control': allowed,
                                          "updated": time.time(),
                                          "endpoints": copy.deepcopy(old.get('endpoints', {})) if old and old['spec']['local'] == spec['local'] and old['spec']['targets'] == spec['targets'] else {},
                                          "git_choice": old.get('git_choice') if old and old['spec']['local'] == spec['local'] and old['spec']['targets'] == spec['targets'] else None}
            endpoints = store['records'][sync_id]['endpoints']
            if old:
                if old['spec']['local'] == spec['local']:
                    endpoints['local'] = old.get('endpoints', {}).get('local')
                for target in spec['targets']:
                    if target['id'] in old.get('endpoints', {}):
                        endpoints[target['id']] = old['endpoints'][target['id']]
            if 'local' not in endpoints:
                from sync_git import root_probe
                probe = root_probe(spec['local'], inspect_git=False)
                endpoints['local'] = probe['identity']
            self._persist(store)
            return self._public(store["records"][sync_id])

    def pause(self, sync_id, paused, revision):
        with self._write_lock():
            self._not_busy(sync_id)
            store = self._store()
            record = self._record(store, sync_id)
            if record['status'].get('unfinished_apply'):
                raise SyncError(409, 'retry/reconcile the interrupted reviewed apply before pausing', state='recovery-needed')
            if integer(revision, "revision") != record["spec"]["revision"]:
                raise SyncError(409, "sync changed; reload it")
            record["spec"]["paused"] = paused
            record["spec"]["revision"] += 1
            record["updated"] = time.time()
            self._persist(store)
            return self._public(record)

    def agent_access(self, sync_id, body):
        object_fields(body, {'revision', 'allow', 'confirm'}, 'agent access')
        if type(body.get('allow')) is not bool or body.get('confirm') is not True:
            raise SyncError(422, 'explicitly confirm allowing or revoking agent access')
        with self._write_lock():
            store = self._store()
            record = self._record(store, sync_id)
            if integer(body.get('revision'), 'revision') != record['spec']['revision']:
                raise SyncError(409, 'sync settings changed; review the directories before granting access')
            record['agent_control'] = body['allow']
            record['updated'] = time.time()
            self._persist(store)
            return self._public(record)

    def unregister(self, sync_id, body):
        object_fields(body, {"confirm", "revision"}, "unregister")
        if body.get("confirm") is not True:
            raise SyncError(422, "confirm unregistering this sync; working files and history are preserved")
        with self._write_lock():
            self._not_busy(sync_id)
            store = self._store()
            record = self._record(store, sync_id)
            if integer(body.get("revision"), "revision") != record["spec"]["revision"]:
                raise SyncError(409, "sync changed; reload it")
            # Archive the registration instead of deleting history or recovery material.
            self.write_private(os.path.join(self.root, "removed", sync_id + ".json"), json.dumps(record, indent=2) + "\n")
            del store["records"][sync_id]
            self._persist(store)
            return {"removed": sync_id, "preserved_history": True}


    def _bootstrap_call(self, record, endpoint, action='probe', **fields):
        from sync_git import bootstrap, PROTOCOL
        spec = record['spec']
        if endpoint == 'local':
            if action == 'init':
                from sync_git import read_json
                claim = read_json(os.path.join(self.root, 'git', spec['id'], 'identity.json'))
                if claim and claim[3] != os.path.join(spec['local'], '.git'):
                    raise SyncError(409, 'This sync already uses private Git history. Keep that storage or use a fresh registration for a normal repository.')
            if action == 'ignore-apply':
                current = bootstrap(spec['local'], 'ignore-preview', fields.get('review'))
                if current['review'] != fields.get('ignore_review'):
                    raise SyncError(409, 'ignore files changed; review again', state='stale-plan')
            return bootstrap(spec['local'], action, fields.get('review'), fields.get('mode'), fields.get('ignore_review'))
        target = next((t for t in spec['targets'] if t['id'] == endpoint), None)
        if not target:
            raise SyncError(422, 'unknown sync endpoint')
        if self.remote is None:
            raise SyncError(409, 'connect the cluster before setup', state='login-needed')
        return self.remote(self.profiles(target['cluster']), dict(protocol=PROTOCOL, operation='bootstrap',
            root=target['remote'], group=spec['id'], endpoint=endpoint, action=action, confirm=action not in ('probe', 'ignore-preview'), **fields))

    def provision_preview(self, sync_id, tolerate_offline=True):
        # The first successful observation pins each directory. Missing initial paths remain unclaimed.
        with self.lock:
            record = copy.deepcopy(self._record(self._store(), sync_id))
        endpoints, probes, inactive = dict(record.get('endpoints', {})), {}, []
        for endpoint in ['local'] + [t['id'] for t in record['spec']['targets']]:
            try:
                probe = self._bootstrap_call(record, endpoint)
            except SyncError as error:
                if not tolerate_offline or endpoint == 'local' or error.payload.get('state') == 'inactive':
                    raise
                probes[endpoint] = {'exists': True, 'unavailable': True, 'repository': True, 'identity': endpoints.get(endpoint),
                                    'root': next(t['remote'] for t in record['spec']['targets'] if t['id'] == endpoint), 'message': str(error)}
                continue
            has_snapshots = bool(probe.get('registered_identity'))
            expected = endpoints.get(endpoint) or probe.get('registered_identity')
            if endpoint == 'local':
                from sync_git import read_json
                claim = read_json(os.path.join(self.root, 'git', record['spec']['id'], 'identity.json'))
                if claim and claim[0] == record['spec']['local']:
                    has_snapshots = True
                    expected = expected or [claim[0], claim[2]]
            if expected:
                endpoints[endpoint] = expected
            if expected and probe['identity'] != expected:
                inactive.append(endpoint)
            elif probe['exists'] and not expected:
                endpoints[endpoint] = probe['identity']
            probe['has_snapshots'] = has_snapshots
            probes[endpoint] = probe
        with self._write_lock():
            store = self._store()
            current = self._record(store, sync_id)
            if current['spec']['revision'] != record['spec']['revision']:
                raise SyncError(409, 'sync settings changed during setup')
            # Compare with any concurrent first observation; never replace a claimed identity.
            for key, value in current.get('endpoints', {}).items():
                if value and probes[key]['identity'] != value and key not in inactive:
                    inactive.append(key)
                if value:
                    endpoints[key] = value
            current['endpoints'] = endpoints
            if inactive:
                current['status'].update(state='inactive', message='A registered directory was moved, removed, or replaced. Restore it or review a new registration for its new location. No replacement will be created.')
            self._persist(store)
        out = {'revision': record['spec']['revision'], 'endpoints': probes, 'inactive': inactive,
               'needs_git_choice': not record.get('git_choice') and not any(p.get('has_snapshots') for p in probes.values()) and all(p['exists'] and not p['repository'] for p in probes.values())}
        out['review'] = digest(out)
        return out

    def provision_apply(self, sync_id, body):
        with self.lock:
            self._not_busy(sync_id)
            identifier(sync_id)
            directory = os.path.join(self.root, 'run-locks')
            os.makedirs(directory, mode=0o700, exist_ok=True)
            fd = os.open(os.path.join(directory, sync_id), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise SyncError(409, 'sync is busy')
            self.active[sync_id] = 'directory-setup'
        try:
            return self._provision_apply(sync_id, body)
        finally:
            os.close(fd)
            with self.lock:
                self.active.pop(sync_id, None)

    def _provision_apply(self, sync_id, body):
        object_fields(body, {'revision', 'review', 'endpoint', 'action', 'mode', 'ignore_review', 'confirm'}, 'directory setup')
        if body.get('confirm') is not True:
            raise SyncError(422, 'explicitly confirm the directory or Git setup decision')
        preview = self.provision_preview(sync_id)
        if body.get('revision') != preview['revision'] or body.get('review') != preview['review']:
            raise SyncError(409, 'directory setup changed; review again', state='stale-plan')
        if preview['inactive']:
            raise SyncError(409, 'this sync is inactive; restore its directory or set up a new registration', state='inactive')
        record = self.get(sync_id)
        action, endpoint = body.get('action'), body.get('endpoint')
        if action == 'private-git':
            if not preview['needs_git_choice']:
                raise SyncError(409, 'Git setup decision no longer matches this review')
        elif action in ('mkdir', 'init', 'ignore-apply'):
            if endpoint not in preview['endpoints']:
                raise SyncError(422, 'choose a reviewed endpoint')
            self._bootstrap_call(record, endpoint, action, review=preview['endpoints'][endpoint]['review'],
                                 mode=body.get('mode'), ignore_review=body.get('ignore_review'))
        else:
            raise SyncError(422, 'unknown setup choice')
        with self._write_lock():
            store = self._store()
            current = self._record(store, sync_id)
            if current['spec']['revision'] != preview['revision']:
                raise SyncError(409, 'settings changed; check setup status before continuing')
            if action in ('private-git', 'init'):
                current['git_choice'] = 'private' if action == 'private-git' else 'repository'
            current['status'].update(state='configured', message='Setup choice saved. Review sync before enabling automatic changes.')
            self._persist(store)
        return self.provision_preview(sync_id)

    def provision_ignore(self, sync_id, endpoint):
        record = self.get(sync_id)
        return self._bootstrap_call(record, endpoint, 'ignore-preview', review=self._bootstrap_call(record, endpoint)['review'])

    def _check_endpoints(self, record):
        # Legacy/offline manual checks still report their usual login-needed result.
        if self.remote is None:
            from sync_git import root_probe
            probe = root_probe(record['spec']['local'])
            expected = record.get('endpoints', {}).get('local')
            if expected and expected != probe['identity']:
                raise SyncError(409, 'registered local directory moved, disappeared, or was replaced', state='inactive')
            if not probe['exists']:
                raise SyncError(409, 'choose Set up directories to create the missing local directory', state='needs-directory')
            return
        setup = self.provision_preview(record['spec']['id'], tolerate_offline=True)
        if setup['inactive']:
            raise SyncError(409, 'registered directory moved, disappeared, or was replaced; restore it or set up a new registration', state='inactive')
        local = setup['endpoints']['local']
        if (any(p.get('unavailable') for p in setup['endpoints'].values()) and not record.get('git_choice') and
                not local['repository'] and not local.get('has_snapshots')):
            raise SyncError(409, 'Connect the cluster to finish the initial Git setup decision before creating snapshots.', state='login-needed')
        if any(not p['exists'] for p in setup['endpoints'].values()):
            raise SyncError(409, 'choose Set up directories to review creating the missing directories', state='needs-directory')
        if setup['needs_git_choice']:
            raise SyncError(409, 'Neither directory is a Git repository. Choose Set up directories to initialize Git or explicitly use private Git storage.', state='needs-git-setup')

    def _setup(self, record):
        root = record["spec"]["local"]
        from sync_git import root_probe
        probe = root_probe(root, inspect_git=False)
        expected = record.get('endpoints', {}).get('local')
        if expected and probe['identity'] != expected:
            raise SyncError(409, 'registered local directory moved or was replaced; restore it or set up a new registration', state='inactive')
        if not probe['exists']:
            raise SyncError(409, 'choose Set up directories to review creating this directory', state='needs-directory')
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
        result["message"] = "Save the ignore choice, then Check files to preview eligible content and snapshot size."
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
            self._not_busy(sync_id)
            store = self._store()
            record = self._record(store, sync_id)
            if record["spec"]["mode"] != "git":
                raise SyncError(422, "ignore setup requires Git mode")
            if record['status'].get('unfinished_apply'):
                raise SyncError(409, 'reconcile the interrupted apply before changing ignore setup', state='recovery-needed')
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
            record["status"].update(state="configured", message="Ignore choice saved. Check files before creating snapshots.")
            self._persist(store)
            return {"sync": self._public(record), "setup": self._setup(record)[0]}

    def unavailable(self, action):
        raise SyncError(501, f"{action} is not enabled in this implementation stage; no transfer or snapshot was started")

    def _status(self, sync_id, status):
        with self._write_lock():
            store = self._store()
            record = self._record(store, sync_id)
            previous = record['status']
            successes = dict(previous.get('last_successes', {}))
            for target in previous.get('targets', []) + status.get('targets', []):
                if target.get('last_successful_sync'):
                    successes[target['target']] = target['last_successful_sync']
            record['status'] = copy.deepcopy(dict(status, last_successes=successes))
            record['updated'] = time.time()
            self._persist(store)

    def plan(self, sync_id, plan_id):
        from sync_git import read_json
        self.get(sync_id)
        identifier(plan_id)
        out = read_json(os.path.join(self.root, 'plans', sync_id, plan_id + '.json'))
        if out is None:
            raise SyncError(404, 'no snapshot review with that ID')
        return out

    def start(self, sync_id, body, preview=False, approvals=None, intent='snapshot', sync_plan=None):
        object_fields(body, {'revision', 'max_files', 'seconds', 'full_verify'}, 'snapshot check')
        limits = {'max_files': integer(body.get('max_files', 10000), 'scan file budget', 1, 1000000),
                  'seconds': body.get('seconds', 5), 'full_verify': body.get('full_verify', False)}
        if isinstance(limits['seconds'], bool) or not isinstance(limits['seconds'], (int, float)) or not 0 < limits['seconds'] <= 60:
            raise SyncError(422, 'scan seconds must be from 0 to 60')
        if not isinstance(limits['full_verify'], bool):
            raise SyncError(422, 'full_verify must be true or false')
        with self._write_lock():
            self._not_busy(sync_id)
            if self.closing or self.scheduling_held:
                raise SyncError(409, 'the console is updating, restarting, or closing; wait before starting a sync')
            store = self._store()
            record = self._record(store, sync_id)
            if intent != 'apply' and record['status'].get('unfinished_apply'):
                raise SyncError(409, 'retry/reconcile the existing apply before starting a new review', state='recovery-needed')
            if integer(body.get('revision'), 'revision') != record['spec']['revision']:
                raise SyncError(409, 'sync settings changed; reload before checking files')
            if record['spec']['mode'] != 'git':
                raise SyncError(501, 'rclone is not enabled; Git-only snapshot checks are available')
            if record['spec']['paused']:
                raise SyncError(409, 'resume this sync before checking files')
            run_id = 'r-' + secrets.token_hex(12)
            directory = os.path.join(self.root, 'run-locks')
            os.makedirs(directory, mode=0o700, exist_ok=True)
            fd = os.open(os.path.join(directory, sync_id), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise SyncError(409, 'this sync is already running in another console')
            self.active[sync_id] = run_id
            record['status'].update(state='applying' if intent == 'apply' else 'checking',
                                    message='Applying reviewed sync…' if intent == 'apply' else 'Checking snapshot inputs…',
                                    running=True, run=run_id, applying=intent == 'apply',
                                    plan=sync_plan['plan'] if sync_plan else None, review=None, details=None)
            try:
                self._persist(store)
            except BaseException:
                self.active.pop(sync_id, None)
                os.close(fd)
                raise
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='hpclib-sync')
            self.executor.submit(self._cycle, copy.deepcopy(record), run_id, preview, limits, approvals or {}, fd, intent, sync_plan)
            return {'run': run_id, 'state': 'checking'}

    def resolve(self, sync_id, body):
        object_fields(body, {'plan', 'action', 'confirm', 'revision'}, 'snapshot resolution')
        if body.get('action') not in ('approve-snapshot', 'apply-sync'):
            raise SyncError(422, 'choose approve-snapshot or apply-sync')
        if body.get('confirm') is not True:
            raise SyncError(422, 'explicitly confirm the reviewed snapshot or working-file operation')
        plan = self.plan(sync_id, body.get('plan'))
        revision = self.get(sync_id)['spec']['revision']
        if body.get('revision') != revision or plan['revision'] != revision:
            raise SyncError(409, 'settings changed after the snapshot review')
        if body['action'] == 'apply-sync':
            if plan.get('type') not in ('sync-review', 'upload-group-review') or plan.get('state') != 'needs-sync-review':
                raise SyncError(409, 'only a clean reviewed Git sync plan can be applied')
            return self.start(sync_id, {'revision': revision}, intent='apply', sync_plan=plan)
        if plan.get('type') != 'snapshot-review':
            raise SyncError(422, 'this plan is not a snapshot volume review')
        if not plan['snapshot']['summary']['complete']:
            raise SyncError(409, 'finish the bounded inventory before approving a snapshot')
        limits = plan['snapshot']['summary']['scan_limits']
        return self.start(sync_id, dict(limits, revision=revision), approvals={plan['endpoint']: plan['snapshot']['review']},
                          intent=self.get(sync_id)['status'].get('review_intent', 'snapshot'))

    def _cycle(self, record, run_id, preview, limits, approvals, fd, intent='snapshot', sync_plan=None):
        from sync_git import GitStore, PROTOCOL
        spec, sync_id = record['spec'], record['spec']['id']
        status = dict(record['status'], targets=[])
        settings = {key: spec['git'][key] for key in record['git_explicit']}
        settings['branch'] = spec['git']['branch']
        binding = {'group': sync_id, 'revision': spec['revision'], 'direction': spec.get('direction', 'both')}

        def review(endpoint, snapshot):
            from sync_git import read_json
            envelope = {'type': 'snapshot-review', 'endpoint': endpoint, 'revision': spec['revision'], 'snapshot': snapshot}
            path = os.path.join(self.root, 'plans', sync_id, snapshot['plan'] + '.json')
            existing = read_json(path)
            if existing is not None and existing != envelope:
                raise SyncError(409, 'snapshot review contents changed; request a new check')
            if existing is None:
                self.write_private(path, json.dumps(envelope) + '\n')
            status.update(state=snapshot['state'], plan=snapshot['plan'], review=envelope, review_intent=intent,
                          message='Review the file inventory before Git ingests this content.')

        def request(target, operation, **fields):
            if self.remote is None:
                raise SyncError(409, 'log in through the console before checking this target', state='login-needed')
            return self.remote(self.profiles(target['cluster']), dict(protocol=PROTOCOL, operation=operation,
                root=target['remote'], group=sync_id, endpoint=target['id'], settings=settings, binding=binding, **fields))

        def transfer(target, local, operation, **fields):
            if self.transfer is None:
                raise SyncError(501, 'update hpclib locally for native Git transport')
            return self.transfer(self.profiles(target['cluster']), local, dict(protocol=PROTOCOL, operation=operation,
                root=target['remote'], group=sync_id, endpoint=target['id'], settings=settings, binding=binding, **fields))

        try:
            self._check_endpoints(record)
            local = GitStore(spec['local'], os.path.join(self.root, 'git', sync_id), sync_id, settings, binding,
                             locks=os.path.join(self.root, 'root-locks'))
            if intent == 'apply' and sync_plan.get('type') == 'upload-group-review':
                self._apply_upload(spec, status, local, request, sync_plan)
                return
            if intent == 'apply':
                self._apply_sync(spec, status, local, request, transfer, sync_plan, run_id, record['status'].get('targets', []))
                return
            if intent in ('sync', 'automatic') and spec.get('direction') == 'pull':
                raise SyncError(501, 'reviewed Git merge/apply currently requires bidirectional mode')
            snapshot = local.preview(**limits) if preview else local.snapshot(approvals.get('local'), 'local' in approvals, **limits)
            status['local_snapshot'] = snapshot
            if snapshot['state'] == 'needs-volume-review':
                review('local', snapshot)
                return
            if not preview and spec.get('direction') == 'push':
                self._plan_upload(spec, status, local, request, transfer, snapshot, run_id, limits, approvals, review)
                if intent == 'automatic' and status.get('sync_review', {}).get('auto_ready'):
                    status['targets'] = []
                    self._apply_upload(spec, status, local, request, status['sync_review'])
                return
            failures = False
            for target in sorted(spec['targets'], key=lambda item: item['id']):
                previous = next((t for t in record['status'].get('targets', []) if t['target'] == target['id']), {})
                info = {'target': target['id'], 'last_check': time.time(), 'last_successful_sync':
                        previous.get('last_successful_sync') or record['status'].get('last_successes', {}).get(target['id'])}
                status['targets'].append(info)
                try:
                    if preview and spec.get('direction') == 'push':
                        from sync_git import read_json
                        selected = read_json(os.path.join(local.state, 'plans', snapshot['plan'] + '.json'))['key']['inventory']
                        remote = request(target, 'upload-preview', paths=sorted(selected), limits=limits)
                    else:
                        remote = request(target, 'preview' if preview else 'snapshot', limits=limits,
                                         review=approvals.get(target['id']), approve=target['id'] in approvals)
                    info.update(state=remote['state'], snapshot=remote)
                    if remote['state'] == 'needs-volume-review':
                        review(target['id'], remote)
                        return
                    if not preview:
                        offer = request(target, 'offer', commit=remote['commit'])
                        info['fetched'] = transfer(target, local, 'fetch-objects', offer=offer)
                        transfer(target, local, 'push-objects', commit=snapshot['commit'], run=run_id)
                        info['incoming'] = request(target, 'check-incoming', commit=snapshot['commit'], run=run_id)
                        info['state'] = 'snapshot-captured'
                except SyncError as error:
                    failures = True
                    info.update(state=error.payload.get('state', 'error'), message=str(error), details=error.payload)
            status.update(state='partial' if failures else ('preview-ready' if preview else 'snapshots-ready'),
                          message='Some targets need attention.' if failures else
                          ('Inventory complete. Changes are relative to each directory\'s own snapshot. Use Review sync to compare directories.' if preview else
                           'Snapshots captured and objects exchanged. Choose Review sync to merge and review working-file changes.'))
            if intent in ('sync', 'automatic'):
                self._plan_sync(spec, status, local)
                plan = status.get('sync_review', {})
                if (intent == 'automatic' and plan.get('state') == 'needs-sync-review' and not plan['excluded'] and
                        all(t['base'] for t in plan['targets'])):
                    status['targets'] = []
                    self._apply_sync(spec, status, local, request, transfer, plan, 'r-' + digest([run_id, 'apply'])[:24])

        except (SyncError, OSError, ValueError) as error:
            status.update(state=getattr(error, 'payload', {}).get('state', 'recovery-needed' if intent == 'apply' else 'error'), message=str(error),
                          details=getattr(error, 'payload', {}))
        except Exception as error:
            status.update(state='recovery-needed' if intent == 'apply' else 'error', message=type(error).__name__ + ': ' + str(error))
        finally:
            status.update(running=False, applying=False, finished=time.time())
            try:
                self.write_private(os.path.join(self.root, 'runs', sync_id, run_id + '.json'), json.dumps(status) + '\n')
                self._status(sync_id, status)
                directory = os.path.join(self.root, 'runs', sync_id)
                runs = sorted((item for item in os.scandir(directory) if item.is_file(follow_symlinks=False) and item.name.endswith('.json')),
                              key=lambda item: item.stat(follow_symlinks=False).st_mtime, reverse=True)
                for item in runs[32:]:
                    os.unlink(item.path)
            finally:
                os.close(fd)
                with self.lock:
                    self.active.pop(sync_id, None)

    def _plan_upload(self, spec, status, local, request, transfer, snapshot, run_id, limits, approvals, review):
        from sync_git import write_json
        plan = {'type': 'upload-group-review', 'revision': spec['revision'], 'spec_digest': digest(spec),
                'state': 'needs-sync-review', 'local': snapshot['checkpoint'], 'candidate': snapshot['commit'],
                'targets': [], 'excluded': [], 'local_changes': {'counts': dict(A=0, M=0, D=0, T=0), 'files': [], 'total': 0},
                'auto_ready': True}
        for target in sorted(spec['targets'], key=lambda t: t['id']):
            uploaded = request(target, 'upload-state')
            if uploaded.get('source') != snapshot['commit']:
                transfer(target, local, 'push-objects', commit=snapshot['commit'], run=run_id)
            remote = request(target, 'upload-review', source=snapshot['commit'], run=run_id, limits=limits,
                             review=approvals.get(target['id']), approve=target['id'] in approvals)
            status['targets'].append(dict(target=target['id'], state=remote['state'], last_check=time.time(),
                                          snapshot=remote.get('snapshot', remote)))
            if remote['state'] == 'needs-volume-review':
                review(target['id'], remote)
                return
            entry = dict(target, upload_plan=remote['plan'], candidate=remote.get('candidate'),
                         classification='local-changed' if remote['previous'] else 'initial-review',
                         changes=remote.get('changes'))
            plan['targets'].append(entry)
            plan['auto_ready'] = plan['auto_ready'] and bool(remote['previous']) and not remote['remote_changed']
            if remote['state'] == 'conflict':
                plan.update(state='conflict', conflict=remote['integration'], auto_ready=False)
                break
        plan['plan'] = 'p-' + digest(plan)[:32]
        write_json(os.path.join(self.root, 'plans', spec['id'], plan['plan'] + '.json'), plan, immutable=True)
        status.update(state=plan['state'], plan=plan['plan'], sync_review=plan,
                      message='Review local uploads. Cluster-only outputs stay on the cluster; local working files are unchanged.'
                      if plan['state'] != 'conflict' else 'Git found conflicting input edits; no working files were applied.')

    def _apply_upload(self, spec, status, local, request, plan):
        if plan['spec_digest'] != digest(spec):
            raise SyncError(409, 'upload settings changed since review', state='stale-plan')
        with local.locked():
            local.prepare(recovering=True)
            local._validate_checkpoint(plan['local'])
        for target in plan['targets']:
            request(target, 'upload-validate', plan=target['upload_plan'])
        status.update(sync_review=plan, plan=plan['plan'], unfinished_apply=True)
        self._status(spec['id'], status)
        failures = False
        for target in plan['targets']:
            info = dict(target=target['id'], state='uploading', last_check=time.time())
            status['targets'].append(info)
            try:
                ack = request(target, 'upload-apply', plan=target['upload_plan'], confirm=True)
                if ack.get('phase') != 'acknowledged' or ack.get('new') != target['candidate']:
                    raise SyncError(409, 'upload was not durably acknowledged', state='recovery-needed')
                info.update(state='uploaded', last_successful_sync=ack['acknowledged'], acknowledgement=ack,
                            snapshot={'tip': target['candidate']})
            except SyncError as error:
                failures = True
                info.update(state=error.payload.get('state', 'recovery-needed'), message=str(error), details=error.payload)
        status.update(state='partial' if failures else 'uploaded', unfinished_apply=failures,
                      message='Some uploads need recovery; retry this reviewed plan.' if failures else
                      'Local changes uploaded through Git. Cluster-only files remain on the cluster.')

    def _plan_sync(self, spec, status, local):
        from sync_git import write_json
        included = [t for t in status['targets'] if t['state'] == 'snapshot-captured']
        if not included:
            raise SyncError(409, 'connect at least one target before reviewing a sync', state='login-needed')
        original = status['local_snapshot']
        if 'checkpoint' not in original or any('checkpoint' not in t['snapshot'] for t in included):
            raise SyncError(501, 'update hpclib on both ends for reviewed Git sync/apply')
        plan = {'type': 'sync-review', 'revision': spec['revision'], 'spec_digest': digest(spec),
                'state': 'needs-sync-review', 'local': original['checkpoint'], 'targets': [],
                'excluded': [dict(t) for t in status['targets'] if t not in included], 'integrations': []}
        candidate = original['commit']
        with local.locked():
            local.prepare()
            local._validate_checkpoint(original['checkpoint'])
            for info in sorted(included, key=lambda t: t['target']):
                target = next(t for t in spec['targets'] if t['id'] == info['target'])
                captured = info['snapshot']
                base = local.base(target['id'])
                entry = dict(target, checkpoint=captured['checkpoint'], base=base,
                             classification=classify(original['tree'], captured['tree'], local.tree(base) if base else None))
                plan['targets'].append(entry)
                merged = local.merge_candidate(candidate, captured['commit'], base)
                plan['integrations'].append(dict(merged, target=target['id']))
                if merged['state'] == 'conflict':
                    plan.update(state='conflict', conflict=merged)
                    break
                candidate = merged['commit']
            plan.update(candidate=candidate, tree=local.tree(candidate))
            if plan['state'] != 'conflict':
                plan['local_changes'] = local.changes(original['commit'], candidate)
                for target in plan['targets']:
                    target['changes'] = local.changes(target['checkpoint']['tip'], candidate)
            plan['plan'] = 'p-' + digest(plan)[:32]
            local.retain('refs/sync/' + spec['id'] + '/plans/' + plan['plan'], candidate)
            if plan.get('conflict'):
                local.retain('refs/sync/' + spec['id'] + '/conflicts/' + plan['plan'], plan['conflict']['tree'])
        write_json(os.path.join(self.root, 'plans', spec['id'], plan['plan'] + '.json'), plan, immutable=True)
        status.update(state=plan['state'], plan=plan['plan'], sync_review=plan,
                      message='Git found conflicts. Resolve the listed files on the source sides and review again; no working files were applied.'
                      if plan['state'] == 'conflict' else
                      'Review the Git merge and per-directory additions, edits and deletions before applying working files.')

    def _apply_sync(self, spec, status, local, request, transfer, plan, run_id, previous_targets=()):
        if plan['spec_digest'] != digest(spec):
            raise SyncError(409, 'sync settings or policy changed since review', state='stale-plan')
        candidate, plan_id = plan['candidate'], plan['plan']
        status.update(sync_review=plan, plan=plan_id)
        with local.locked():
            local.prepare(recovering=True)
            for target in plan['targets']:
                if local.base(target['id']) not in (target['base'], candidate):
                    raise SyncError(409, 'pair acknowledgement changed since review', state='stale-plan')
        # Stage and validate every participant before the first working-file write.
        for target in plan['targets']:
            prior = next((t for t in previous_targets if t['target'] == target['id']), {})
            info = {'target': target['id'], 'state': 'validating', 'last_check': time.time(),
                    'last_successful_sync': prior.get('last_successful_sync') or status.get('last_successes', {}).get(target['id'])}
            status['targets'].append(info)
            transfer(target, local, 'push-objects', commit=candidate, run=run_id)
            request(target, 'check-incoming', commit=candidate, run=run_id)
            info['validation'] = request(target, 'validate-apply', checkpoint=target['checkpoint'], candidate=candidate, plan=plan_id)
        local.validate_apply(plan['local'], candidate, plan_id)
        already_unfinished = bool(status.get('unfinished_apply'))
        status['unfinished_apply'] = True
        self._status(spec['id'], status)  # Durable admission to the first working-file write.
        try:
            local.apply(plan['local'], candidate, plan_id)
        except SyncError:
            from sync_git import read_json
            journal = read_json(os.path.join(local.state, 'apply.json'))
            if not already_unfinished and (journal is None or journal.get('phase') == 'aborted'):
                status['unfinished_apply'] = False
            raise
        status['local_snapshot'] = local.preview()
        failures = False
        for target, info in zip(plan['targets'], status['targets']):
            try:
                ack = request(target, 'apply', checkpoint=target['checkpoint'], candidate=candidate, plan=plan_id, confirm=True)
                if ack.get('state') != 'applied' or ack.get('new') != candidate or ack.get('phase') != 'acknowledged':
                    raise SyncError(409, 'remote did not durably acknowledge this candidate', state='recovery-needed')
                local.acknowledge(target['id'], target['base'], candidate)
                info.update(state='in-sync', last_successful_sync=ack['acknowledged'], acknowledgement=ack,
                            snapshot={'tip': candidate})
            except SyncError as error:
                failures = True
                info.update(state=error.payload.get('state', 'error'), message=str(error), details=error.payload)
        status['targets'].extend(plan['excluded'])
        status.update(state='partial' if failures or plan['excluded'] else 'in-sync', unfinished_apply=failures,
                      message='Some targets need attention; successful targets are acknowledged separately. Retry this reviewed plan to reconcile an interrupted apply.'
                      if failures or plan['excluded'] else 'Working files synchronized through Git. HEAD, main and ordinary indexes were preserved.')

    @contextlib.contextmanager
    def maintenance(self, hold=False):
        with self.lock:
            if self.active or self.scheduling_held:
                raise SyncError(409, 'wait for active directory syncs before updating or restarting')
            self.scheduling_held = True
        succeeded = False
        try:
            yield
            succeeded = True
        finally:
            if not hold or not succeeded:
                with self.lock:
                    self.scheduling_held = False

    def close(self):
        with self.lock:
            self.closing = True
            executor = self.executor
        self.agent_stop.set()
        if self.agent_thread is not None:
            self.agent_thread.join(timeout=12)
        if executor is not None:
            executor.shutdown(wait=True)

    def start_agent_bridge(self):
        if self.remote is None or self.profiles is None or self.agent_thread is not None:
            return
        self.agent_thread = threading.Thread(target=self._agent_loop, name='hpclib-sync-agent', daemon=True)
        self.agent_thread.start()

    def _agent_loop(self):
        while not self.agent_stop.wait(2):
            try:
                self.schedule_pump()
                self.agent_pump()
            except Exception:
                # A missing/stopped console master or older remote helper never initiates a login.
                self.agent_stop.wait(8)

    def schedule_pump(self):
        """Bounded admission: no browser, authentication prompt, or silent setup writes."""
        with self.lock:
            records = copy.deepcopy(list(self._store()['records'].values()))
        now = self.clock()
        # Initialize all deadlines before admitting workers, then serve the oldest due group first.
        # Otherwise two long-running groups could continually take both slots ahead of later registrations.
        valid = {(r['spec']['id'], r['spec']['revision']) for r in records}
        self.schedule_due = {key: value for key, value in self.schedule_due.items() if key in valid}
        for record in records:
            spec = record['spec']
            self.schedule_due.setdefault((spec['id'], spec['revision']), now + spec['interval'])
        records.sort(key=lambda r: self.schedule_due[(r['spec']['id'], r['spec']['revision'])])
        blocked = {'inactive', 'needs-directory', 'needs-git-setup', 'needs-ignore-setup', 'needs-volume-review',
                   'needs-selection-review', 'needs-sync-review', 'conflict', 'stale-plan', 'recovery-needed', 'interrupted'}
        for record in records:
            spec, status = record['spec'], record['status']
            key = spec['id']
            if self.closing or self.scheduling_held or len(self.active) >= 2:
                return
            if (not spec['policy'].get('background') or not spec['interval'] or spec['paused'] or
                    spec['mode'] != 'git' or status.get('unfinished_apply') or self._is_busy(key) or status['state'] in blocked):
                continue
            due_key = (key, spec['revision'])
            if due_key not in self.schedule_due:
                self.schedule_due[due_key] = now + spec['interval']
            if now < self.schedule_due[due_key]:
                continue
            failed = status['state'] in ('error', 'partial', 'login-needed')
            failures = min(6, self.schedule_failures.get(key, 0) + 1) if failed else 0
            self.schedule_failures[key] = failures
            delay = min(3600, spec['interval'] * (2 ** failures))
            self.schedule_due[due_key] = now + delay
            intent = 'background-check' if spec['policy']['on_change'] == 'notify' else 'automatic'
            try:
                self.start(key, {'revision': spec['revision']}, preview=intent == 'background-check', intent=intent)
            except SyncError:
                continue

    def agent_pump(self):
        with self.lock:
            records = copy.deepcopy(list(self._store()['records'].values()))
        clusters = sorted({t['cluster'] for r in records if r.get('agent_control') for t in r['spec']['targets']})
        for cluster in clusters:
            if self.closing:
                return
            if self.agent_retry.get(cluster, 0) > time.time():
                continue
            try:
                profile = self.profiles(cluster)
                batch = self.remote(profile, {'protocol': 1, 'operation': 'agent-poll', 'consumer': self.agent_consumer})
                received = time.monotonic()
                for request in batch.get('requests', [])[:4]:
                    if self.closing:
                        return
                    # Remote wall clocks may differ; use the owner's remaining TTL on our monotonic clock.
                    request['_deadline'] = received + min(300, max(0, request.get('valid_for', 0)))
                    reply = self._agent_once(profile, request)
                    self.remote(profile, {'protocol': 1, 'operation': 'agent-reply', 'consumer': self.agent_consumer,
                                          'ticket': request['ticket'], 'reply': reply})
                self.agent_retry.pop(cluster, None)
            except Exception:
                self.agent_retry[cluster] = time.time() + 10
                continue

    def _agent_once(self, profile, request):
        from sync_git import read_json, write_json
        ticket = identifier(request.get('ticket'))
        stable = {k: request.get(k) for k in ('v', 'ticket', 'actor', 'operation', 'root', 'identity', 'scope', 'args', 'created', 'expires')}
        binding = digest({'profile': profile['name'], 'request': stable})
        path = os.path.join(self.root, 'agent-requests', ticket + '.json')
        with self._write_lock():
            previous = read_json(path)
            if previous:
                if previous['binding'] != binding:
                    return {'ok': False, 'status': 409, 'error': 'request ticket was reused with different inputs'}
                if previous.get('reply') is not None:
                    return previous['reply']
                return {'ok': False, 'status': 409, 'error': 'console stopped during command dispatch; inspect sync status before issuing a fresh command'}
            write_json(path, {'binding': binding, 'state': 'dispatching'}, immutable=True)
        try:
            reply = {'ok': True, 'result': self._agent_dispatch(profile, request)}
        except Exception as error:
            reply = {'ok': False, 'status': getattr(error, 'status', 409), 'error': str(error),
                     'detail': getattr(error, 'payload', {})}
        if len(json.dumps(reply).encode()) > 8 << 20:
            reply = {'ok': False, 'status': 413, 'error': 'sync result exceeds the agent response limit; inspect the plan in the console'}
        write_json(path, {'binding': binding, 'state': 'finished', 'reply': reply})
        return reply

    def _agent_dispatch(self, profile, request):
        from action_paths import check_scope, Refused
        scope = request.get('scope')
        if (request.get('v') != 1 or not isinstance(scope, dict) or not request.get('actor') or
                scope.get('actor') != request['actor'] or request.get('_deadline', 0) <= time.monotonic()):
            raise SyncError(403, 'invalid or expired agent sync request')
        operation, args, root = request['operation'], request['args'], request['root']
        reads = ('list', 'status', 'plan')
        if operation not in reads and (scope.get('file_changes') is not True or args.get('confirm') is not True):
            raise SyncError(403, 'agent sync mutations require file changes and explicit confirmation')
        def permitted(record, listing=False):
            spec = record['spec']
            if not record.get('agent_control') or spec['mode'] != 'git':
                return False
            if not spec['targets'] or any(t['cluster'] != profile['name'] for t in spec['targets']):
                return False  # One cluster token cannot authorize writes to another cluster.
            try:
                check_scope(root, scope)
                for target in spec['targets']:
                    check_scope(target['remote'], scope)
            except (Refused, ValueError):
                return False
            return any((os.path.commonpath([root, t['remote']]) == root if listing else root == t['remote'])
                       for t in spec['targets'])
        if operation == 'list':
            rows = []
            for record in self.list()['syncs']:
                if permitted(record, listing=True):
                    rows.append({'id': record['spec']['id'], 'name': record['spec']['name'], 'revision': record['spec']['revision'],
                                 'local': record['spec']['local'], 'targets': record['spec']['targets'],
                                 'paused': record['spec']['paused'], 'state': record['status']['state']})
            return {'syncs': rows, 'message': 'Only agent-enabled groups fully inside this cluster token scope are shown.'}
        record = self.get(identifier(args.get('id')))
        if not permitted(record):
            raise SyncError(403, 'enable agent control for this sync; all targets must be on this cluster and inside the token scope')
        if operation == 'status':
            out = copy.deepcopy(record)
            state = record['status']
            out['status'] = {k: state[k] for k in ('state', 'message', 'run', 'running', 'plan', 'unfinished_apply',
                                                  'last_successes', 'finished', 'review_intent') if k in state}
            out['status']['targets'] = [{k: t[k] for k in ('target', 'state', 'message', 'last_check', 'last_successful_sync') if k in t}
                                        for t in state.get('targets', [])]
            snapshot = state.get('local_snapshot') or {}
            out['status']['local_commit'] = snapshot.get('commit') or snapshot.get('tip')
            for target, original in zip(out['status']['targets'], state.get('targets', [])):
                snapshot = original.get('snapshot') or {}
                target['commit'] = snapshot.get('commit') or snapshot.get('tip')
            # Detailed inventories and changes are fetched once through the immutable plan query.
            return out
        if operation == 'plan':
            plan = self.plan(record['spec']['id'], args.get('plan'))
            if plan['revision'] != record['spec']['revision']:
                raise SyncError(409, 'only the current settings revision can be inspected by an agent')
            return plan
        revision = integer(args.get('revision'), 'revision')
        if revision != record['spec']['revision']:
            raise SyncError(409, 'sync revision changed; request current status')
        sync_id = record['spec']['id']
        if operation == 'review':
            return self.start(sync_id, {'revision': revision}, intent='sync')
        if operation in ('apply', 'retry', 'approve-snapshot'):
            return self.resolve(sync_id, {'revision': revision, 'plan': args.get('plan'), 'confirm': True,
                                         'action': 'approve-snapshot' if operation == 'approve-snapshot' else 'apply-sync'})
        if operation in ('pause', 'resume'):
            return self.pause(sync_id, operation == 'pause', revision)
        if operation == 'configure':
            updates = args.get('settings')
            object_fields(updates, {'warn_file_mb', 'warn_snapshot_mb', 'warn_file_count'}, 'agent warning settings')
            if not updates:
                raise SyncError(422, 'provide warning settings')
            with self.lock:
                private = self._record(self._store(), sync_id)
                explicit = private['git_explicit']
            body = copy.deepcopy(record['spec'])
            body['git'] = {k: body['git'][k] for k in explicit}
            body['git'].update(updates)
            return self.save(body, sync_id)
        raise SyncError(422, 'unknown agent sync operation')
