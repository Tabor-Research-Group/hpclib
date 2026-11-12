"""Git-owned sync snapshots and object exchange. Standard library, Python 3.6+.

Snapshots preserve working files; reviewed application uses only Git's private-index operations.
Inventory reads path/stat metadata before content ingestion. Git alone selects paths,
stages content, makes trees/commits, and exchanges objects.
"""
import base64
import contextlib
import fcntl
import heapq
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time

from sync_spec import (DEFAULT_EXCLUDES, METADATA_FILE, SyncError, digest, effective_git,
                       git_settings, identifier, integer, validate_metadata, setup_metadata)

PROTOCOL = 1
MAX_BUNDLE = 128 << 20
OID = re.compile(r'[0-9a-f]{40}\Z')


def write_json(path, value, immutable=False):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, sort_keys=True, allow_nan=False)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        if immutable:
            try:
                os.link(temp, path)
            except FileExistsError:
                if read_json(path) != value:
                    raise SyncError(409, 'a snapshot review with this ID already has different contents')
        else:
            os.replace(temp, path)
        directory = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_json(path, default=None):
    try:
        with open(path) as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default


class GitStore:
    """One root and one explicitly owned sync branch; persistent state is outside root."""
    def __init__(self, root, state, group, settings=None, binding=None, locks=None):
        self.root = os.path.abspath(root)
        self.state = os.path.realpath(state)
        self.locks = locks or os.path.join(os.path.dirname(self.state), 'root-locks')
        self.group = identifier(group)
        if os.path.realpath(self.root) != self.root or not os.path.isdir(self.root):
            raise SyncError(422, 'sync root must exist without symlinked path components')
        if os.path.commonpath([self.root, self.state]) == self.root:
            raise SyncError(422, 'private Git state must be outside the synced directory')
        self.overrides = git_settings(settings or {})
        self.binding = binding or {}
        self.scope = None
        self.repo = None
        self.index = os.path.join(self.state, 'index')
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_') or
                    k in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM')}
        self.env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0',
                        GIT_AUTHOR_NAME='hpclib sync', GIT_AUTHOR_EMAIL='sync@hpclib.invalid',
                        GIT_COMMITTER_NAME='hpclib sync', GIT_COMMITTER_EMAIL='sync@hpclib.invalid')

    @contextlib.contextmanager
    def locked(self):
        os.makedirs(self.state, mode=0o700, exist_ok=True)
        os.makedirs(self.locks, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(self.locks, digest(self.root) + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SyncError(409, 'this sync endpoint is busy')
            yield
        finally:
            os.close(fd)

    def command(self, *args):
        return ['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false',
                '-c', 'commit.gpgSign=false', '-c', 'core.splitIndex=false', '-c', 'core.sparseCheckout=false',
                '-c', 'core.untrackedCache=false', '-c', 'core.trustctime=true'] + list(args)

    def git(self, *args, **options):
        env = dict(self.env)
        if self.repo:
            env.update(GIT_DIR=self.repo, GIT_WORK_TREE=self.root, GIT_INDEX_FILE=options.get('index', self.index))
        env.update(options.get('env', {}))
        try:
            argv = self.command(*args)
            process = subprocess.Popen(argv, cwd=self.root, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                stdout, stderr = process.communicate(options.get('data'), timeout=options.get('timeout', 120))
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise
            out = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SyncError(503, 'Git could not complete: ' + str(e)) from e
        if len(out.stdout) > options.get('limit', 4 << 20):
            raise SyncError(413, 'Git output exceeded its bounded response limit')
        if out.returncode and options.get('check', True):
            raise SyncError(409, 'Git refused the operation: ' + out.stderr.decode(errors='replace')[-2000:])
        return out

    def text(self, *args, **options):
        return self.git(*args, **options).stdout.decode('utf-8').rstrip('\n')

    def state_file(self, name):
        return os.path.join(self.state, ('upload-' if self.scope is not None and name in ('index-tip.json', 'inventory.json') else '') + name)

    def use_scope(self, paths):
        if not isinstance(paths, (list, tuple, set)) or any(not isinstance(p, str) or not p or '\0' in p or
                p.startswith('/') or any(part in ('', '.', '..') or part.lower() == '.git' for part in p.split('/')) for p in paths):
            raise SyncError(422, 'upload scope must contain safe relative Git file paths')
        paths = sorted(set(paths))
        if len(paths) > 1000000:
            raise SyncError(409, 'upload selection exceeds the one-million-path scan ceiling; use a smaller sync directory', state='needs-volume-review')
        self.scope = paths
        self.index = os.path.join(self.state, 'upload-index')

    def prepare(self, recovering=False):
        from sync_manager import read_setup_file
        raw, _ = read_setup_file(self.root, METADATA_FILE, 1 << 16)
        if raw is None:
            raise SyncError(409, 'review the ignore setup before snapshotting', state='needs-ignore-setup')
        try:
            metadata = validate_metadata(json.loads(raw))
        except (ValueError, TypeError) as e:
            raise SyncError(422, 'repair sync metadata before snapshotting: ' + str(e)) from e
        if 'default_excludes' in self.overrides and self.overrides['default_excludes'] != metadata['git']['default_excludes']:
            raise SyncError(422, 'ignore choices must agree with the reviewed repository setup')
        spec = {'git': self.overrides}
        self.settings, _ = effective_git(spec, metadata, self.overrides)
        self.ref = 'refs/heads/' + self.settings['branch']
        self.owner = 'refs/sync/' + self.group + '/owner/' + digest(self.root)[:24]
        self.git('check-ref-format', self.ref)
        found = self.git('rev-parse', '--absolute-git-dir', check=False)
        if found.returncode == 0:
            top = self.text('rev-parse', '--show-toplevel')
            if os.path.realpath(top) != self.root:
                raise SyncError(422, 'select the repository root, or an independent vendored repository root')
            self.repo = os.path.realpath(os.fsdecode(found.stdout.strip()))
        else:
            if os.path.lexists(os.path.join(self.root, '.git')):
                raise SyncError(422, 'the selected root has unusable Git metadata')
            self.repo = os.path.join(self.state, 'repository.git')
            if not os.path.isdir(self.repo):
                self.repo = None
                self.git('init', '--bare', os.path.join(self.state, 'repository.git'))
                self.repo = os.path.join(self.state, 'repository.git')
        if self.text('rev-parse', '--show-object-format') != 'sha1':
            raise SyncError(501, 'this protocol currently supports SHA-1 Git repositories only')
        worktrees = self.git('worktree', 'list', '--porcelain').stdout
        if ('branch ' + self.ref).encode() in worktrees.splitlines():
            raise SyncError(409, 'the sync branch is checked out in a worktree')
        self.tip = self.text('rev-parse', '--verify', self.ref, check=False) or None
        owner = self.text('rev-parse', '--verify', self.owner, check=False) or None
        for ref in (self.ref, self.owner):
            if self.git('symbolic-ref', '--quiet', ref, check=False).returncode == 0:
                raise SyncError(409, 'sync refs must not be symbolic aliases')
        if owner != self.tip:
            raise SyncError(409, 'the configured sync branch is not owned by this group or was moved externally')
        info = os.stat(self.root)
        identity = [self.root, info.st_dev, info.st_ino, self.repo, self.ref, self.group]
        claim = read_json(os.path.join(self.state, 'identity.json'))
        if claim is not None and claim != identity:
            raise SyncError(409, 'root/repository/branch identity changed; preserve the old state and register anew')
        write_json(os.path.join(self.state, 'identity.json'), identity)
        self.identity = identity
        self.baseline = read_json(self.state_file('inventory.json'), {})
        if self.baseline.get('tip') != self.tip:
            self.baseline = {}
        # A missing or interrupted private index is rebuilt from the owned tree, never the user's index.
        if not os.path.isfile(self.index) or read_json(self.state_file('index-tip.json')) != self.tip:
            self.git('read-tree', self.tip if self.tip else '--empty')
            write_json(self.state_file('index-tip.json'), self.tip)
        global_file = self.text('config', '--path', '--get', 'core.excludesFile', check=False)
        if not global_file:
            global_file = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'git', 'ignore')
        elif not os.path.isabs(global_file):
            global_file = os.path.join(self.root, global_file)
        global_rules = b''
        if global_file:
            try:
                with open(global_file, 'rb') as stream:
                    global_rules = stream.read((1 << 20) + 1)
            except FileNotFoundError:
                pass
            if len(global_rules) > 1 << 20:
                raise SyncError(422, 'global exclude rules exceed the supported size')
        defaults = ('\n'.join(DEFAULT_EXCLUDES) + '\n').encode() if metadata['git']['default_excludes'] == 'sync-only' else b''
        rules = defaults + global_rules
        path = os.path.join(self.state, 'excludes')
        with open(path, 'wb') as stream:
            stream.write(rules)
        self.excludes = ['-c', 'core.excludesFile=' + path]
        self.selection = digest({'scope': self.scope, 'metadata': raw, 'rules': rules.decode('utf-8', errors='surrogateescape'), 'settings': self.settings})
        journal = read_json(os.path.join(self.state, 'apply.json'))
        if journal and journal['phase'] not in ('acknowledged', 'aborted') and not recovering:
            raise SyncError(409, 'an interrupted apply needs reconciliation; retry its reviewed plan',
                            state='recovery-needed', journal=journal)
        shared = read_json(self._root_apply_path())
        if shared and shared['phase'] != 'acknowledged' and (shared['group'] != self.group or not recovering):
            raise SyncError(409, 'this directory has an unfinished reviewed apply; reconcile its owning sync first',
                            state='recovery-needed', journal=shared)

    def _root_apply_path(self):
        return os.path.join(self.locks, digest(self.root) + '.apply.json')

    def _check_root_apply(self, plan, new):
        shared = read_json(self._root_apply_path())
        if shared and shared['phase'] != 'acknowledged' and (
                shared.get('group') != self.group or shared.get('plan') != plan or shared.get('new') != new or
                shared.get('identity') != self.identity):
            raise SyncError(409, 'another reviewed apply owns this directory; preserve it for recovery',
                            state='recovery-needed', journal=shared)

    def _root_apply(self, plan, new, phase):
        self._check_root_apply(plan, new)
        write_json(self._root_apply_path(), {'root': self.root, 'identity': self.identity, 'group': self.group,
                                           'plan': plan, 'new': new, 'phase': phase})

    def paths(self, args, max_files, seconds):
        if self.scope is None:
            yield from self._paths(args, max_files, seconds)
            return
        started, seen = time.monotonic(), set()
        for offset in range(0, len(self.scope), 128):
            batch = self.scope[offset:offset + 128]
            for path in batch:
                full = os.path.join(self.root, path)
                if os.path.isdir(full) and not os.path.islink(full):
                    raise SyncError(409, 'an upload file collides with a directory: ' + path, state='path-collision')
                parts = path.split('/')
                if any(os.path.islink(os.path.join(self.root, *parts[:i])) for i in range(1, len(parts))):
                    raise SyncError(409, 'an upload parent is a symlink: ' + path, state='path-collision')
            for path in self._paths(args + ['--'] + [':(literal)' + p for p in batch], max_files,
                                    max(0.001, seconds - (time.monotonic() - started))):
                if path not in seen:
                    seen.add(path)
                    if len(seen) > max_files or time.monotonic() - started > seconds:
                        raise SyncError(409, 'upload inventory paused at its budget', state='needs-volume-review', incomplete=True)
                    yield path

    def _paths(self, args, max_files, seconds):
        """Stream NUL-delimited Git discovery; stop the process at either budget."""
        env = dict(self.env, GIT_DIR=self.repo, GIT_WORK_TREE=self.root, GIT_INDEX_FILE=self.index)
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(self.command(*self.excludes, 'ls-files', '-z', *args),
                cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=errors, start_new_session=True)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            pending, count, deadline = b'', 0, time.monotonic() + seconds
            try:
                while True:
                    if not selector.select(max(0, deadline - time.monotonic())):
                        raise SyncError(409, 'metadata scan paused at its time budget', state='needs-volume-review', incomplete=True)
                    block = os.read(process.stdout.fileno(), 65536)
                    if not block:
                        break
                    pending += block
                    while b'\0' in pending:
                        raw, pending = pending.split(b'\0', 1)
                        count += 1
                        if time.monotonic() > deadline:
                            raise SyncError(409, 'metadata scan paused at its time budget', state='needs-volume-review', incomplete=True)
                        if count > max_files:
                            raise SyncError(409, 'metadata scan paused at its file budget', state='needs-volume-review', incomplete=True)
                        path = os.fsdecode(raw)
                        if not path or path.startswith('/') or '..' in path.split('/'):
                            raise SyncError(422, 'Git returned an unsafe inventory path')
                        yield path
                    if len(pending) > 65536 or time.monotonic() > deadline:
                        raise SyncError(409, 'metadata scan paused at its time/path budget', state='needs-volume-review', incomplete=True)
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
                if process.returncode or pending:
                    errors.seek(0)
                    raise SyncError(409, 'Git inventory failed: ' + errors.read(2000).decode(errors='replace'))
            finally:
                selector.close()
                if process.poll() is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                process.wait()
                process.stdout.close()

    @staticmethod
    def metadata(path):
        info = os.lstat(path)
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise SyncError(422, 'nested repositories, gitlinks and special files are unsupported: ' + path)
        return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

    def inventory(self, max_files=10000, seconds=5, full_verify=False):
        integer(max_files, 'scan file budget', 1, 1000000)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 60:
            raise SyncError(422, 'scan seconds must be from 0 to 60')
        if not isinstance(full_verify, bool):
            raise SyncError(422, 'full_verify must be true or false')
        records, changed, total, directories, largest, eligible_preview = {}, 0, 0, {}, [], []
        old = {} if full_verify else self.baseline.get('files', {})
        incomplete, reason, ignored = False, None, []
        started = time.monotonic()
        try:
            ignored = list(self.paths(['--cached', '--ignored', '--exclude-standard'], max_files, seconds))
            if ignored:
                raise SyncError(409, 'previously snapshotted paths became ignored; review the selection change',
                                state='needs-selection-review', paths=ignored[:100])
            for path in self.paths(['--cached', '--others', '--exclude-standard'], max_files,
                                   max(0.001, seconds - (time.monotonic() - started))):
                if path in records:
                    continue
                try:
                    meta = self.metadata(os.path.join(self.root, path))
                except FileNotFoundError:
                    continue
                records[path] = meta
                # Keep a deterministic bounded selection even if Git changes its cached/untracked order.
                heapq.heappush(eligible_preview, (path, meta[3], old.get(path) != meta))
                if len(eligible_preview) > 100:
                    heapq.heappop(eligible_preview)
                if old.get(path) != meta:
                    changed += 1
                    total += meta[3]
                    directory = path.split('/')[0] if '/' in path else '.'
                    directories[directory] = directories.get(directory, 0) + meta[3]
                    heapq.heappush(largest, (meta[3], path))
                    if len(largest) > 20:
                        heapq.heappop(largest)
        except SyncError as error:
            if not error.payload.get('incomplete'):
                raise
            incomplete, reason = True, str(error)
        summary = {'eligible_files': len(records), 'new_changed_files': changed, 'new_changed_bytes': total,
                   'eligible_preview': [{'path': p, 'bytes': n, 'metadata_changed': changed}
                                        for p, n, changed in sorted(eligible_preview)],
                   'eligible_preview_truncated': len(records) > len(eligible_preview),
                   'deleted_files': len(set(old) - set(records)) if not incomplete else None,
                   'largest_files': [{'path': p, 'bytes': n} for n, p in sorted(largest, reverse=True)],
                   'directories': [{'path': p, 'bytes': n} for p, n in sorted(directories.items(), key=lambda x: -x[1])[:20]],
                   'complete': not incomplete, 'reason': reason,
                   'scan_limits': {'max_files': max_files, 'seconds': seconds, 'full_verify': full_verify},
                   'thresholds': {k: self.settings[k] for k in ('warn_file_mb', 'warn_snapshot_mb', 'warn_file_count')}}
        warnings = []
        if largest and max(largest)[0] >= self.settings['warn_file_mb'] * 1000000:
            warnings.append('one-file')
        if total >= self.settings['warn_snapshot_mb'] * 1000000:
            warnings.append('aggregate-bytes')
        if changed >= self.settings['warn_file_count']:
            warnings.append('file-count')
        if incomplete:
            warnings.append('incomplete-scan')
        summary['warnings'] = warnings
        key = {'identity': self.identity, 'tip': self.tip, 'binding': self.binding, 'summary_version': 2,
               'selection': self.selection, 'inventory': records, 'limits': summary['scan_limits'],
               'complete': summary['complete'], 'warnings': warnings, 'scan_reason': reason}
        review = digest(key)
        return {'protocol': PROTOCOL, 'type': 'snapshot-review', 'root': self.root, 'review': review,
                'plan': 'p-' + review[:32], 'tip': self.tip, 'summary': summary,
                'state': 'needs-volume-review' if warnings else 'snapshot-ready'}, records, key

    def preview(self, **limits):
        with self.locked():
            self.prepare()
            plan, records, key = self.inventory(**limits)
            write_json(os.path.join(self.state, 'plans', plan['plan'] + '.json'), {'plan': plan, 'key': key}, immutable=True)
            return plan

    def snapshot(self, review=None, approve=False, **limits):
        with self.locked():
            self.prepare()
            plan, records, key = self.inventory(**limits)
            if review is not None and review != plan['review']:
                raise SyncError(409, 'snapshot inputs changed; review the new inventory', state='stale-plan')
            if not plan['summary']['complete'] or (plan['summary']['warnings'] and not (approve and review)):
                write_json(os.path.join(self.state, 'plans', plan['plan'] + '.json'), {'plan': plan, 'key': key}, immutable=True)
                return plan
            # No content ingestion occurs above this line. Immediately recheck all selected metadata.
            self.recheck(records)
            if limits.get('full_verify'):
                # Explicit full verification discards only private stat caches, so Git rehashes all selected content.
                self.git('read-tree', '--empty')
                if self.tip:
                    self.git('read-tree', self.tip)
            if self.scope is None:
                self.git(*self.excludes, 'add', '-A', '--', '.')
            else:
                tracked = set(self.entries(self.tip)) if self.tip else set()
                selected = sorted(set(records) | (tracked & set(self.scope)))
                for offset in range(0, len(selected), 128):
                    self.git(*self.excludes, 'add', '-A', '--', *[':(literal)' + p for p in selected[offset:offset + 128]])
            after, _, _ = self.inventory(**limits)
            if after['review'] != plan['review']:
                raise SyncError(409, 'files changed during staging; no snapshot ref was advanced', state='stale-plan')
            tree = self.text('write-tree')
            entries = self.git('ls-tree', '-r', '-l', '-z', tree, limit=128 << 20).stdout.split(b'\0')
            sizes = {}
            for entry in filter(None, entries):
                head, name = entry.split(b'\t', 1)
                mode, kind, oid, size = head.split()
                if kind != b'blob':
                    raise SyncError(422, 'nested repository/gitlink cannot enter a sync snapshot')
                sizes[os.fsdecode(name)] = int(size)
            if self.scope is not None:
                sizes = {p: n for p, n in sizes.items() if p in self.scope}
            if set(sizes) != set(records):
                raise SyncError(409, 'staged selection changed; no snapshot ref was advanced', state='stale-plan')
            # Filters may expand content. Approval never covers bytes larger than the reviewed stat estimates.
            if any(sizes[p] > records[p][3] for p in records):
                raise SyncError(409, 'Git staged expanded content; review filters before snapshotting', state='needs-volume-review')
            prior_tree = self.text('rev-parse', self.tip + '^{tree}') if self.tip else None
            commit = self.tip
            if tree != prior_tree:
                parents = ['-p', self.tip] if self.tip else []
                commit = self.text('commit-tree', tree, *parents, data=b'hpclib sync snapshot\n')
                zero = '0' * 40
                update = ('start\nupdate %s %s %s\nupdate %s %s %s\nprepare\ncommit\n' %
                          (self.ref, commit, self.tip or zero, self.owner, commit, self.tip or zero)).encode()
                if ('branch ' + self.ref).encode() in self.git('worktree', 'list', '--porcelain').stdout.splitlines():
                    raise SyncError(409, 'the sync branch was checked out during staging')
                self.git('update-ref', '--no-deref', '--stdin', data=update)
            write_json(self.state_file('inventory.json'), {'tip': commit, 'files': records})
            write_json(self.state_file('index-tip.json'), commit)
            return dict(plan, state='snapshotted', commit=commit, tree=tree, changed=commit != self.tip,
                        checkpoint=self._checkpoint(commit, records, plan['summary']['scan_limits']))

    def recheck(self, records):
        info = os.stat(self.root)
        if [self.root, info.st_dev, info.st_ino] != self.identity[:3]:
            raise SyncError(409, 'root identity changed', state='stale-plan')
        for path, expected in records.items():
            try:
                current = self.metadata(os.path.join(self.root, path))
            except OSError as e:
                raise SyncError(409, 'file disappeared after inventory', state='stale-plan') from e
            if current != expected:
                raise SyncError(409, 'file changed after inventory', state='stale-plan')

    def native_offer(self, commit=None):
        with self.locked():
            self.prepare(recovering=True)
            selected = self._oid(commit or self.tip)
            if selected != self.tip:
                raise SyncError(409, 'snapshot changed before native transfer; review again', state='stale-plan')
            return {'native_git': 1, 'commit': selected, 'ref': self.ref}

    def fetch_objects(self, url, offer, name, upload_pack=None, env=None):
        """Native Git negotiates/streams objects; only the owned tracking ref may move."""
        identifier(name)
        commit = self._oid(offer.get('commit'))
        if offer.get('native_git') != 1:
            raise SyncError(501, 'update hpclib on both ends for native Git transfer')
        ref = 'refs/sync/' + self.group + '/remote/' + name
        with self.locked():
            self.prepare(recovering=True)
            if offer.get('ref') != self.ref:
                raise SyncError(422, 'native offer must name the owned sync branch')
            if self.git('symbolic-ref', '--quiet', ref, check=False).returncode == 0:
                raise SyncError(409, 'incoming refs must not be symbolic aliases')
            args = ['-c', 'maintenance.auto=false', '-c', 'gc.auto=0', '-c', 'protocol.allow=never',
                    'fetch', '--no-tags', '--no-write-fetch-head', '--no-recurse-submodules',
                    '--no-auto-maintenance', '--no-write-commit-graph', '--refmap=']
            if upload_pack:
                args.append('--upload-pack=' + upload_pack)
            transport_env = dict({'GIT_ALLOW_PROTOCOL': 'ssh'}, **(env or {}))
            self.git(*(args + ['--', url, offer['ref'] + ':' + ref]), env=transport_env, timeout=300)
            if self.text('rev-parse', '--verify', ref) != commit:
                raise SyncError(409, 'native fetch returned a different snapshot', state='stale-plan')
            return {'native_git': 1, 'commit': commit, 'ref': ref, 'working_files_changed': False}

    def push_objects(self, url, commit, run, receive_pack=None, env=None):
        identifier(run)
        commit = self._oid(commit)
        ref = 'refs/sync/' + self.group + '/incoming/' + run
        with self.locked():
            self.prepare(recovering=True)
            self.git('cat-file', '-e', commit + '^{commit}')
            args = ['-c', 'maintenance.auto=false', '-c', 'gc.auto=0', '-c', 'push.followTags=false',
                    '-c', 'protocol.allow=never', 'push', '--porcelain', '--no-verify']
            if receive_pack:
                args.append('--receive-pack=' + receive_pack)
            transport_env = dict({'GIT_ALLOW_PROTOCOL': 'ssh'}, **(env or {}))
            self.git(*(args + ['--', url, commit + ':' + ref]), env=transport_env, timeout=300)
            return {'native_git': 1, 'commit': commit, 'ref': ref, 'working_files_changed': False}

    def check_incoming(self, commit, run):
        identifier(run)
        commit = self._oid(commit)
        with self.locked():
            self.prepare(recovering=True)
            ref = 'refs/sync/' + self.group + '/incoming/' + run
            if self.text('rev-parse', '--verify', ref, check=False) != commit:
                raise SyncError(409, 'native push was not acknowledged at the expected incoming ref')
            self.git('cat-file', '-e', commit + '^{commit}')
            return {'native_git': 1, 'commit': commit, 'ref': ref, 'working_files_changed': False}

    def export_bundle(self, commit=None):
        with self.locked():
            self.prepare(recovering=True)
            if self.tip is None:
                raise SyncError(409, 'make a snapshot before exporting objects')
            selected = commit or self.tip
            self._oid(selected)
            advertised = self.ref
            if commit is not None:
                advertised = 'refs/sync/' + self.group + '/export/' + selected
                self.retain(advertised, selected)
            fd, path = tempfile.mkstemp(dir=self.state)
            os.close(fd)
            os.unlink(path)
            try:
                self.git('bundle', 'create', path, advertised)
                if self.text('bundle', 'list-heads', path) != selected + ' ' + advertised:
                    raise SyncError(409, 'snapshot ref changed during export', state='stale-plan')
                if os.path.getsize(path) > MAX_BUNDLE:
                    raise SyncError(413, 'snapshot bundle exceeds the 128 MiB transport limit')
                with open(path, 'rb') as stream:
                    encoded = base64.b64encode(stream.read()).decode('ascii')
                return {'protocol': PROTOCOL, 'commit': selected, 'ref': advertised, 'bundle': encoded}
            finally:
                if os.path.exists(path):
                    os.unlink(path)

    def import_bundle(self, packet, kind, name):
        if kind not in ('remote', 'incoming'):
            raise SyncError(422, 'object imports are remote snapshots or incoming runs')
        identifier(name)
        if (not isinstance(packet, dict) or packet.get('protocol') != PROTOCOL or
                not isinstance(packet.get('commit'), str) or not OID.fullmatch(packet['commit']) or
                not isinstance(packet.get('ref'), str)):
            raise SyncError(422, 'invalid snapshot object packet')
        encoded = packet.get('bundle')
        if not isinstance(encoded, str) or len(encoded) > MAX_BUNDLE * 4 // 3 + 4:
            raise SyncError(413, 'snapshot bundle exceeds the transport limit')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError as e:
            raise SyncError(422, 'invalid bundle encoding') from e
        with self.locked():
            self.prepare(recovering=True)
            fd, path = tempfile.mkstemp(dir=self.state)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                heads = self.text('bundle', 'list-heads', path).splitlines()
                allowed = ('refs/heads/', 'refs/sync/' + self.group + '/export/')
                if heads != [packet['commit'] + ' ' + packet.get('ref', '')] or not packet.get('ref', '').startswith(allowed):
                    raise SyncError(422, 'bundle advertised unexpected refs')
                self.git('bundle', 'verify', path)
                self.git('bundle', 'unbundle', path)
                self.git('cat-file', '-e', packet['commit'] + '^{commit}')
                ref = 'refs/sync/' + self.group + '/' + kind + '/' + name
                if self.git('symbolic-ref', '--quiet', ref, check=False).returncode == 0:
                    raise SyncError(409, 'incoming refs must not be symbolic aliases')
                old = self.text('rev-parse', '--verify', ref, check=False) or '0' * 40
                self.git('update-ref', '--no-deref', ref, packet['commit'], old)
                return {'protocol': PROTOCOL, 'commit': packet['commit'], 'ref': ref, 'working_files_changed': False}
            finally:
                os.unlink(path)

    @staticmethod
    def _oid(value):
        if not isinstance(value, str) or not OID.fullmatch(value):
            raise SyncError(422, 'expected a full Git object ID')
        return value

    def tree(self, commit):
        return self.text('rev-parse', self._oid(commit) + '^{tree}')

    def retain(self, ref, oid):
        if not ref.startswith('refs/sync/' + self.group + '/'):
            raise SyncError(422, 'retained objects need an owned sync ref')
        if self.git('symbolic-ref', '--quiet', ref, check=False).returncode == 0:
            raise SyncError(409, 'owned object refs must not be symbolic aliases')
        old = self.text('rev-parse', '--verify', ref, check=False) or '0' * 40
        self.git('update-ref', '--no-deref', ref, self._oid(oid), old)

    def _checkpoint(self, commit, records, limits):
        return {'tip': commit, 'tree': self.tree(commit), 'identity': self.identity,
                'selection': self.selection, 'binding': self.binding, 'files': digest(records), 'limits': limits}

    @contextlib.contextmanager
    def _index_for(self, commit):
        fd, path = tempfile.mkstemp(prefix='verify-index-', dir=self.state)
        os.close(fd)
        os.unlink(path)
        try:
            self.git('read-tree', commit, index=path)
            yield path
        finally:
            for name in (path, path + '.lock'):
                if os.path.exists(name):
                    os.unlink(name)

    def _matches(self, commit):
        # A fresh private index forces Git to check content instead of trusting old stat-cache entries.
        with self._index_for(commit) as index:
            if self.scope is not None:
                self.git('update-index', '--refresh', index=index, check=False)
                for offset in range(0, len(self.scope), 128):
                    paths = [':(literal)' + p for p in self.scope[offset:offset + 128]]
                    changed = self.git('diff-files', '--quiet', '--no-ext-diff', '--', *paths, index=index, check=False)
                    others = self.git(*self.excludes, 'ls-files', '--others', '--exclude-standard', '-z', '--', *paths, index=index).stdout
                    if changed.returncode or others:
                        return False
                return True
            refreshed = self.git('update-index', '--refresh', index=index, check=False)
            changed = self.git('diff-files', '--quiet', '--no-ext-diff', index=index, check=False)
            others = self.git(*self.excludes, 'ls-files', '--others', '--exclude-standard', '-z', index=index).stdout
            return refreshed.returncode == 0 and changed.returncode == 0 and not others

    def _validate_checkpoint(self, expected):
        if not isinstance(expected, dict) or expected.get('tip') != self.tip:
            raise SyncError(409, 'snapshot ref changed since review', state='stale-plan')
        limits = dict(expected['limits'], full_verify=False)
        plan, records, _ = self.inventory(**limits)
        actual = self._checkpoint(self.tip, records, expected['limits'])
        if not plan['summary']['complete'] or actual != expected or not self._matches(self.tip):
            raise SyncError(409, 'working files, selection, or root changed since review', state='stale-plan')
        return records

    def entries(self, commit):
        out = {}
        for raw in self.git('ls-tree', '-r', '-z', self._oid(commit), limit=128 << 20).stdout.split(b'\0'):
            if not raw:
                continue
            header, name = raw.split(b'\t', 1)
            mode, kind, oid = header.split()
            path = os.fsdecode(name)
            if (kind != b'blob' or mode not in (b'100644', b'100755', b'120000') or
                    any(p in ('', '.', '..') or p.lower() == '.git' for p in path.split('/'))):
                raise SyncError(422, 'unsupported path or mode in candidate: ' + path)
            out[path] = (mode.decode(), oid.decode())
        return out

    def changes(self, old, new):
        raw = self.git('diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--name-status', '-z',
                       self._oid(old), self._oid(new), '--', limit=16 << 20).stdout.split(b'\0')
        rows = [{'action': os.fsdecode(raw[i]), 'path': os.fsdecode(raw[i + 1])}
                for i in range(0, len(raw) - 1, 2)]
        return {'counts': {name: sum(r['action'] == name for r in rows) for name in ('A', 'M', 'D', 'T')},
                'files': rows[:500], 'truncated': len(rows) > 500, 'total': len(rows)}

    def _write_commit(self, tree, parents, message):
        stamp = max([int(self.text('show', '-s', '--format=%ct', p)) for p in parents] or [0]) + 1
        dates = {'GIT_AUTHOR_DATE': '@%d +0000' % stamp, 'GIT_COMMITTER_DATE': '@%d +0000' % stamp}
        args = [item for parent in parents for item in ('-p', parent)]
        return self.text('commit-tree', tree, *args, data=message.encode(), env=dates)

    def merge_candidate(self, local, remote, base=None):
        # Caller holds the local root lock. Git is the only content/conflict merge engine.
        for commit in (local, remote):
            self._oid(commit)
        if base is None:
            empty = self.text('mktree', data=b'')
            base = self._write_commit(empty, [], 'hpclib sync initial base\n')
            self.retain('refs/sync/' + self.group + '/initial-base', base)
        else:
            self._oid(base)
            for tip in (local, remote):
                if self.git('merge-base', '--is-ancestor', base, tip, check=False).returncode:
                    raise SyncError(409, 'acknowledged base is not an ancestor of both snapshots', state='recovery-needed')
        if self.git('merge-base', '--is-ancestor', remote, local, check=False).returncode == 0:
            return {'state': 'clean', 'base': base, 'tree': self.tree(local), 'commit': local}
        result = self.git('merge-tree', '--write-tree', '--merge-base=' + base, '--name-only', '-z',
                          local, remote, check=False, limit=8 << 20)
        if result.returncode not in (0, 1):
            diagnostic = result.stderr.decode(errors='replace')[-2000:]
            if result.returncode == 129 or 'unknown option' in diagnostic:
                raise SyncError(501, 'local Git needs merge-tree --write-tree --merge-base support (Git 2.43+); update Git',
                                diagnostics=diagnostic)
            raise SyncError(409, 'Git could not build the merge preview: ' + diagnostic, state='merge-error')
        fields = result.stdout.split(b'\0')
        tree = self._oid(fields.pop(0).decode().strip())
        conflicts = []
        if result.returncode:
            while fields and fields[0]:
                conflicts.append(os.fsdecode(fields.pop(0)))
            if fields:
                fields.pop(0)
            messages = []
            while fields and fields[0]:
                count = int(fields.pop(0))
                paths = [os.fsdecode(fields.pop(0)) for _ in range(count)]
                kind, message = fields.pop(0).decode(errors='replace'), fields.pop(0).decode(errors='replace')
                messages.append({'paths': paths, 'type': kind, 'message': message})
            return {'state': 'conflict', 'base': base, 'tree': tree, 'paths': conflicts, 'messages': messages}
        parents = [local] if local == remote else [local, remote]
        commit = local if local == remote else self._write_commit(tree, parents, 'hpclib sync integration\n')
        return {'state': 'clean', 'base': base, 'tree': tree, 'commit': commit}

    def _guard_writes(self, old, new):
        before, after = self.entries(old), self.entries(new)
        changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
        if self.scope is not None and changed - set(self.scope):
            raise SyncError(409, 'upload candidate changes unmanaged paths', state='stale-plan')
        if any(p == METADATA_FILE or p.split('/')[-1] in ('.gitignore', '.gitattributes') for p in changed):
            raise SyncError(409, 'ignore/metadata changes require a separate selection review before sync apply',
                            state='needs-selection-review')
        for path in after:
            if path not in before and os.path.lexists(os.path.join(self.root, path)):
                raise SyncError(409, 'an ignored/untracked path would be overwritten: ' + path,
                                state='path-collision', paths=[path])
            parts = path.split('/')
            for i in range(1, len(parts)):
                prefix = '/'.join(parts[:i])
                full = os.path.join(self.root, prefix)
                if os.path.lexists(full) and (os.path.islink(full) or not os.path.isdir(full)) and prefix not in before:
                    raise SyncError(409, 'a parent path would be overwritten: ' + prefix,
                                    state='path-collision', paths=[prefix])
        additions = [p for p in after if p not in before]
        if additions:
            ignored = self.git(*self.excludes, 'check-ignore', '--no-index', '-z', '--stdin',
                               data=b''.join(os.fsencode(p) + b'\0' for p in additions), check=False)
            if ignored.returncode not in (0, 1):
                raise SyncError(409, 'Git could not validate candidate eligibility')
            if ignored.stdout:
                raise SyncError(409, 'candidate files are excluded on this endpoint; review ignore settings',
                                state='needs-selection-review', paths=[os.fsdecode(p) for p in ignored.stdout.split(b'\0') if p])

    def validate_apply(self, expected, new, plan=None):
        with self.locked():
            self.prepare(recovering=plan is not None)
            self._check_root_apply(plan, new)
            journal = read_json(os.path.join(self.state, 'apply.json'))
            if journal and journal.get('plan') == plan:
                if (journal.get('checkpoint') != expected or journal.get('new') != new or
                        journal.get('identity') != self.identity or journal.get('selection') != self.selection):
                    raise SyncError(409, 'review inputs changed after an apply attempt', state='recovery-needed', journal=journal)
            if journal and journal['phase'] not in ('acknowledged', 'aborted'):
                if journal.get('plan') != plan or journal.get('new') != new:
                    raise SyncError(409, 'another interrupted apply needs recovery', state='recovery-needed', journal=journal)
            if journal and journal.get('plan') == plan and journal.get('new') == new:
                if self.tip in (expected['tip'], new) and self._matches(new):
                    return {'state': 'already-applied', 'tip': self.tip, 'candidate': new}
                if self.tip != expected['tip'] or not self._matches(expected['tip']):
                    raise SyncError(409, 'interrupted apply contains later edits or mixed content', state='recovery-needed', journal=journal)
            self._validate_checkpoint(expected)
            self._oid(new)
            if self.git('merge-base', '--is-ancestor', self.tip, new, check=False).returncode:
                raise SyncError(409, 'candidate does not descend from this snapshot', state='stale-plan')
            self._guard_writes(self.tip, new)
            with self._index_for(self.tip) as index:
                self.git('update-index', '--refresh', index=index, check=self.scope is None)
                self.git('read-tree', '-n', '-m', '-u', self.tip, new, index=index)
            return {'state': 'apply-ready', 'tip': self.tip, 'candidate': new}

    def _advance(self, old, new):
        for ref in (self.ref, self.owner):
            if self.git('symbolic-ref', '--quiet', ref, check=False).returncode == 0:
                raise SyncError(409, 'sync refs must not be symbolic aliases', state='recovery-needed')
        update = ('start\nupdate %s %s %s\nupdate %s %s %s\nprepare\ncommit\n' %
                  (self.ref, new, old, self.owner, new, old)).encode()
        self.git('update-ref', '--no-deref', '--stdin', data=update)
        self.tip = new

    def apply(self, expected, new, plan):
        identifier(plan)
        self._oid(new)
        with self.locked():
            self.prepare(recovering=True)
            self._check_root_apply(plan, new)
            path = os.path.join(self.state, 'apply.json')
            journal = read_json(path)
            old = self._oid(expected['tip'])
            if journal and journal.get('plan') == plan and journal.get('checkpoint') != expected:
                raise SyncError(409, 'review inputs changed after an apply attempt', state='recovery-needed', journal=journal)
            if journal and journal.get('plan') == plan and (journal.get('identity') != self.identity or
                                                          journal.get('selection') != self.selection):
                raise SyncError(409, 'root or selection changed after an apply attempt', state='recovery-needed', journal=journal)
            if journal and journal['phase'] not in ('acknowledged', 'aborted'):
                if any(journal.get(k) != v for k, v in {'plan': plan, 'old': old, 'new': new,
                                                       'identity': self.identity, 'selection': self.selection}.items()):
                    raise SyncError(409, 'a different interrupted apply needs recovery', state='recovery-needed', journal=journal)
            if journal and journal.get('plan') == plan and journal.get('new') == new:
                if self.tip in (old, new) and self._matches(new):
                    if self.tip == old:
                        self._advance(old, new)
                    self._refresh_baseline(new, expected['limits'])
                    journal.update(phase='acknowledged', acknowledged=time.time())
                    write_json(path, journal)
                    self._root_apply(plan, new, 'acknowledged')
                    return dict(journal, state='applied', recovered=True)
                if journal['phase'] == 'acknowledged' or self.tip != old or not self._matches(old):
                    raise SyncError(409, 'files or refs differ from the interrupted apply; preserve edits and inspect recovery',
                                    state='recovery-needed', journal=journal)
            self._validate_checkpoint(expected)
            if self.git('merge-base', '--is-ancestor', old, new, check=False).returncode:
                raise SyncError(409, 'candidate ancestry changed', state='stale-plan')
            self._guard_writes(old, new)
            journal = {'plan': plan, 'old': old, 'new': new, 'tree': self.tree(new), 'identity': self.identity,
                       'selection': self.selection, 'checkpoint': expected, 'phase': 'prepared', 'started': time.time()}
            with self._index_for(old) as index:
                self.git('update-index', '--refresh', index=index, check=self.scope is None)
                self.git('read-tree', '-n', '-m', '-u', old, new, index=index)
                write_json(path, journal)
                # Close the review-to-write gap again immediately; external editors do not honor this lock.
                try:
                    self._validate_checkpoint(expected)
                except SyncError:
                    journal['phase'] = 'aborted'
                    write_json(path, journal)
                    raise
                journal['phase'] = 'applying'
                # Shared across groups/private repositories, unlike the detailed per-group journal.
                # The root lock covers this admission and Git's complete working-file application.
                self._root_apply(plan, new, 'applying')
                write_json(path, journal)
                self.git('read-tree', '-m', '-u', old, new, index=index)
            journal['phase'] = 'files-applied'
            write_json(path, journal)
            selection = self.selection
            self.prepare(recovering=True)
            if self.selection != selection or self.tip != old:
                raise SyncError(409, 'selection or refs changed during apply', state='recovery-needed', journal=journal)
            if not self._matches(new):
                raise SyncError(409, 'post-apply content differs; no acknowledgement was made', state='recovery-needed', journal=journal)
            if ('branch ' + self.ref).encode() in self.git('worktree', 'list', '--porcelain').stdout.splitlines():
                raise SyncError(409, 'sync branch was checked out during apply', state='recovery-needed')
            self._advance(old, new)
            journal['phase'] = 'refs-updated'
            write_json(path, journal)
            self._refresh_baseline(new, expected['limits'])
            journal.update(phase='acknowledged', acknowledged=time.time())
            write_json(path, journal)
            self._root_apply(plan, new, 'acknowledged')
            return dict(journal, state='applied', recovered=False)

    def _refresh_baseline(self, commit, limits):
        self.git('read-tree', commit)
        write_json(self.state_file('index-tip.json'), commit)
        self.tip, self.baseline = commit, {}
        plan, records, _ = self.inventory(**dict(limits, full_verify=False))
        if not plan['summary']['complete']:
            raise SyncError(409, 'finish the post-apply scan before acknowledging', state='recovery-needed')
        write_json(self.state_file('inventory.json'), {'tip': commit, 'files': records})

    def upload_review(self, source, run, limits=None, review=None, approve=False):
        with self.locked():
            self.prepare()
            previous = read_json(os.path.join(self.state, 'upload.json'), {})
            retained = self.text('rev-parse', '--verify', 'refs/sync/' + self.group + '/upload-source', check=False)
            if previous.get('source') != source or retained != source:
                ref = 'refs/sync/' + self.group + '/incoming/' + identifier(run)
                if self.text('rev-parse', '--verify', ref, check=False) != self._oid(source):
                    raise SyncError(409, 'upload source was not received at the expected owned ref', state='stale-plan')
            paths = set(self.entries(source))
            if previous.get('source'):
                paths.update(self.entries(previous['source']))
            self.use_scope(paths)
        snapshot = self.snapshot(review, approve, **(limits or {}))
        if snapshot['state'] == 'needs-volume-review':
            return snapshot
        with self.locked():
            self.prepare()
            self._validate_checkpoint(snapshot['checkpoint'])
            # A source that rolls back or forks requires owner intervention, never a forced update.
            merged = self.merge_candidate(snapshot['commit'], source, previous.get('source'))
            plan = {'type': 'upload-review', 'state': 'conflict' if merged['state'] == 'conflict' else 'needs-sync-review',
                    'source': source, 'previous': previous, 'scope': self.scope, 'checkpoint': snapshot['checkpoint'],
                    'snapshot': snapshot, 'integration': merged}
            if merged['state'] == 'clean':
                plan['candidate'] = merged['commit']
                plan['changes'] = self.changes(snapshot['commit'], merged['commit'])
                self._guard_writes(snapshot['commit'], merged['commit'])
            before = self.entries(previous['candidate']) if previous.get('candidate') else {}
            after = self.entries(snapshot['commit'])
            plan['remote_changed'] = bool(previous.get('candidate') and any(before.get(p) != after.get(p) for p in self.scope))
            plan['plan'] = 'p-' + digest(plan)[:32]
            self.retain('refs/sync/' + self.group + '/plans/' + plan['plan'], merged.get('commit', merged['tree']))
            write_json(os.path.join(self.state, 'plans', plan['plan'] + '.json'), plan, immutable=True)
            return plan

    def upload_apply(self, plan_id, validate=False):
        identifier(plan_id)
        plan = read_json(os.path.join(self.state, 'plans', plan_id + '.json'))
        if not plan or plan.get('type') != 'upload-review' or plan['state'] != 'needs-sync-review':
            raise SyncError(409, 'only a clean reviewed upload may be applied')
        self.use_scope(plan['scope'])
        previous = read_json(os.path.join(self.state, 'upload.json'), {})
        next_state = {'source': plan['source'], 'candidate': plan['candidate']}
        if previous not in (plan['previous'], next_state):
            raise SyncError(409, 'upload acknowledgement changed since review', state='stale-plan')
        if validate:
            return self.validate_apply(plan['checkpoint'], plan['candidate'], plan_id)
        result = self.apply(plan['checkpoint'], plan['candidate'], plan_id)
        # Retain the source first: an acknowledged JSON receipt must never point at a missing source ref.
        self.retain('refs/sync/' + self.group + '/upload-source', plan['source'])
        write_json(os.path.join(self.state, 'upload.json'), next_state)
        return result

    def base(self, target):
        identifier(target)
        return self.text('rev-parse', '--verify', 'refs/sync/' + self.group + '/base/' + target, check=False) or None

    def acknowledge(self, target, previous, commit):
        with self.locked():
            self.prepare()
            if self.tip != commit or not self._matches(commit):
                raise SyncError(409, 'local content changed before pair acknowledgement', state='recovery-needed')
            ref = 'refs/sync/' + self.group + '/base/' + identifier(target)
            if self.git('symbolic-ref', '--quiet', ref, check=False).returncode == 0:
                raise SyncError(409, 'base ref must not be symbolic', state='recovery-needed')
            old = self.base(target)
            if old == commit:
                return
            if old != previous:
                raise SyncError(409, 'pair acknowledgement moved externally', state='recovery-needed')
            self.git('update-ref', '--no-deref', ref, self._oid(commit), old or '0' * 40)


def root_probe(root, inspect_git=True):
    """Small read-only initial setup probe; no implicit mkdir or Git initialization."""
    from sync_spec import absolute_path
    root = absolute_path(root, 'sync root', remote=True)
    if os.path.realpath(root) != root:
        raise SyncError(409, 'sync paths must not contain symlinks', state='inactive')
    path, missing = root, []
    while not os.path.lexists(path):
        path, name = os.path.split(path)
        missing.insert(0, name)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise SyncError(409, 'a sync path component is not a directory', state='inactive')
    out = {'root': root, 'exists': not missing, 'ancestor': [path, info.st_ino], 'missing': missing,
           'identity': [root, info.st_ino] if not missing else None, 'repository': False}
    if not missing and inspect_git:
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_') or
               k in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM')}
        result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, 'rev-parse', '--show-toplevel'],
                                cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        out['repository'] = result.returncode == 0 and os.path.realpath(os.fsdecode(result.stdout.strip())) == root
        if result.returncode == 0 and not out['repository']:
            raise SyncError(409, 'choose a repository root or independent directory', state='inactive')
        if os.path.lexists(os.path.join(root, '.git')) and not out['repository']:
            raise SyncError(409, 'repair the unusable Git metadata before syncing', state='inactive')
    out['review'] = digest(out)
    return out


def bootstrap(root, action='probe', review=None, mode=None, ignore_review=None):
    from sync_context import atomic_text, preview
    probe = root_probe(root)
    if action == 'probe':
        return probe
    if probe['review'] != review:
        raise SyncError(409, 'directory changed since setup review; check again', state='stale-plan')
    if action == 'mkdir':
        if probe['exists']:
            return probe
        fd = os.open(probe['ancestor'][0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(fd).st_ino != probe['ancestor'][1]:
                raise SyncError(409, 'parent directory changed; review again', state='stale-plan')
            for name in probe['missing']:
                # EEXIST is a stale review, including a concurrently installed symlink.
                os.mkdir(name, mode=0o755, dir_fd=fd)
                new = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.fsync(fd)
                os.close(fd)
                fd = new
        finally:
            os.close(fd)
        return root_probe(root)
    if not probe['exists']:
        raise SyncError(409, 'create the reviewed directory before setting up Git', state='needs-directory')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_ino != probe['identity'][1]:
            raise SyncError(409, 'directory changed; check again', state='stale-plan')
        if action == 'init':
            if not probe['repository']:
                # Use the held directory descriptor even if the original path is renamed meanwhile.
                import sys
                env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_') or
                       k in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM')}
                # fchdir in a fresh child works on macOS and Linux without unsafe preexec_fn in this threaded console.
                launcher = 'import os,sys; os.fchdir(int(sys.argv[1])); os.close(int(sys.argv[1])); os.execvp("git",sys.argv[2:])'
                result = subprocess.run([sys.executable, '-c', launcher, str(fd), 'git', '-c',
                    'core.hooksPath=' + os.devnull, 'init', '--template=', '-b', 'main', '.'],
                    pass_fds=(fd,), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
                if result.returncode:
                    raise SyncError(409, 'Git initialization failed: ' + result.stderr.decode(errors='replace')[-1000:])
            return root_probe(root)
        plan, merged, ignored_mode, metadata_mode = preview(root, fd)
        if action == 'ignore-preview':
            return plan
        if action != 'ignore-apply' or mode not in ('gitignore', 'sync-only', 'off'):
            raise SyncError(422, 'invalid directory setup operation')
        if plan['review'] != ignore_review:
            raise SyncError(409, 'ignore files changed; review again', state='stale-plan')
        # The outer review binds the root; the ignore review separately binds file contents.
        metadata = setup_metadata(plan, mode)
        if mode == 'gitignore' and plan['merge_diff']:
            atomic_text(fd, '.gitignore', merged, ignored_mode)
        atomic_text(fd, METADATA_FILE, json.dumps(metadata, indent=2) + '\n', metadata_mode)
        return preview(root, fd)[0]
    finally:
        os.close(fd)


def endpoint_store(request, state_root=None):
    group, endpoint = identifier(request.get('group')), identifier(request.get('endpoint'))
    state_root = os.path.join(state_root or os.environ.get('HPCTUNNELS_DATA_DIR', os.path.expanduser('~/.local/tunnels')), 'sync')
    return GitStore(request['root'], os.path.join(state_root, group, endpoint), group,
                    request.get('settings'), request.get('binding'), locks=os.path.join(state_root, 'locks'))


def native_pack(service, encoded, repository):
    """SSH owner endpoint; Git owns the bidirectional wire protocol and pack validation."""
    if repository not in ('hpclib-sync', '/hpclib-sync') or len(encoded) > 65536:
        raise SyncError(422, 'invalid native Git endpoint')
    request = json.loads(base64.urlsafe_b64decode(encoded.encode()).decode())
    if request.get('protocol') != PROTOCOL:
        raise SyncError(422, 'unsupported sync protocol')
    store = endpoint_store(request)
    commit = store._oid(request.get('commit'))
    with store.locked():
        store.prepare(recovering=True)
        if service == '--git-upload-pack':
            ref = store.ref
            if store.text('rev-parse', '--verify', ref, check=False) != commit:
                raise SyncError(409, 'snapshot export is no longer available')
            operation = 'upload-pack'
        elif service == '--git-receive-pack':
            ref = 'refs/sync/' + store.group + '/incoming/' + identifier(request.get('run'))
            existing = store.text('rev-parse', '--verify', ref, check=False)
            if existing and existing != commit:
                raise SyncError(409, 'incoming run already identifies a different commit')
            if store.git('symbolic-ref', '--quiet', ref, check=False).returncode == 0:
                raise SyncError(409, 'incoming ref must not be symbolic')
            operation = 'receive-pack'
        else:
            raise SyncError(422, 'unknown native Git service')
        args = ['-c', 'transfer.hideRefs=refs', '-c', 'transfer.hideRefs=HEAD',
                '-c', 'transfer.hideRefs=!' + ref, '-c', 'uploadpack.allowAnySHA1InWant=false',
                '-c', 'uploadpack.allowTipSHA1InWant=false', '-c', 'uploadpack.allowReachableSHA1InWant=false',
                '-c', 'receive.denyDeletes=true', '-c', 'receive.denyNonFastForwards=true',
                '-c', 'receive.autogc=false', '-c', 'gc.auto=0', operation, store.repo]
        return subprocess.call(store.command(*args), env=dict(store.env, GIT_NO_LAZY_FETCH='1'))


def rpc(request, state_root=None):
    if not isinstance(request, dict) or request.get('protocol') != PROTOCOL:
        raise SyncError(422, 'unsupported sync protocol')
    if request.get('operation') in ('agent-poll', 'agent-reply'):
        from sync_context import agent_exchange
        state = state_root or os.environ.get('HPCTUNNELS_DATA_DIR', os.path.expanduser('~/.local/tunnels'))
        return agent_exchange(request, state)
    if request.get('operation') == 'bootstrap':
        directory = state_root or os.environ.get('HPCTUNNELS_DATA_DIR', os.path.expanduser('~/.local/tunnels'))
        claim = None
        if request.get('group') and request.get('endpoint'):
            claim = read_json(os.path.join(directory, 'sync', identifier(request['group']), identifier(request['endpoint']), 'identity.json'))
        if request.get('action') == 'init' and claim and claim[3] != os.path.join(request['root'], '.git'):
            raise SyncError(409, 'This sync already uses private Git history. Keep that storage; initialize a normal repository only for a fresh registration.')
        if request.get('action', 'probe') not in ('probe', 'ignore-preview') and request.get('confirm') is not True:
            raise SyncError(422, 'directory setup requires explicit owner confirmation')
        if request.get('action') == 'ignore-apply':
            current = bootstrap(request['root'], 'ignore-preview', request.get('review'))
            if current['review'] != request.get('ignore_review'):
                raise SyncError(409, 'ignore files changed; review again', state='stale-plan')
        result = bootstrap(request['root'], request.get('action', 'probe'), request.get('review'), request.get('mode'), request.get('ignore_review'))
        if request.get('action', 'probe') == 'probe' and request.get('group') and request.get('endpoint'):
            if claim and claim[0] == request['root']:
                result['registered_identity'] = [claim[0], claim[2]]
        return result
    store = endpoint_store(request, state_root)
    operation = request.get('operation')
    if operation == 'preview':
        return store.preview(**request.get('limits', {}))
    if operation == 'snapshot':
        return store.snapshot(request.get('review'), request.get('approve') is True, **request.get('limits', {}))
    if operation == 'upload-preview':
        with store.locked():
            store.prepare()
            previous = read_json(os.path.join(store.state, 'upload.json'), {})
            selected = request.get('paths')
            store.use_scope(selected)
            if previous.get('source'):
                store.use_scope(set(store.scope) | set(store.entries(previous['source'])))
        return store.preview(**request.get('limits', {}))
    if operation == 'upload-state':
        with store.locked():
            store.prepare()
            return read_json(os.path.join(store.state, 'upload.json'), {})
    if operation == 'upload-review':
        return store.upload_review(request['source'], request['run'], request.get('limits'), request.get('review'), request.get('approve') is True)
    if operation in ('upload-validate', 'upload-apply'):
        if operation == 'upload-apply' and request.get('confirm') is not True:
            raise SyncError(422, 'upload apply requires explicit confirmation')
        return store.upload_apply(request['plan'], validate=operation == 'upload-validate')
    if operation == 'offer':
        return store.native_offer(request.get('commit'))
    if operation == 'check-incoming':
        return store.check_incoming(request['commit'], request['run'])
    if operation == 'export':
        return store.export_bundle()
    if operation == 'import':
        return store.import_bundle(request['packet'], 'incoming', identifier(request['run']))
    if operation == 'validate-apply':
        return store.validate_apply(request['checkpoint'], request['candidate'], request.get('plan'))
    if operation == 'apply':
        if request.get('confirm') is not True:
            raise SyncError(422, 'working-file apply requires explicit confirmation')
        return store.apply(request['checkpoint'], request['candidate'], request['plan'])
    raise SyncError(422, 'unknown sync operation')


if __name__ == '__main__':
    import sys
    try:
        if len(sys.argv) == 4 and sys.argv[1] in ('--git-upload-pack', '--git-receive-pack'):
            sys.exit(native_pack(*sys.argv[1:]))
        line = sys.stdin.buffer.read(MAX_BUNDLE * 2 + 1)
        if len(line) > MAX_BUNDLE * 2:
            raise SyncError(413, 'sync request too large')
        print(json.dumps({'protocol': PROTOCOL, 'result': rpc(json.loads(line))}, allow_nan=False))
    except (SyncError, OSError, ValueError, KeyError) as error:
        if len(sys.argv) > 1:
            print(str(error), file=sys.stderr)
            sys.exit(1)
        print(json.dumps({'protocol': PROTOCOL, 'error': str(error), 'status': getattr(error, 'status', 422),
                          'detail': getattr(error, 'payload', {})}))
        sys.exit(1)
