"""Actual Git snapshot/volume/object contracts; all roots and state are disposable."""
import hashlib
import json
import os
import select
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hpclib/servers'))
from sync_git import GitStore, rpc
from sync_spec import SyncError
from test_sync_manager import native_fixture, native_endpoint


class TestSnapshots(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hpclib-snapshot-', dir=os.environ.get('HPCLIB_SYNC_FIXTURE_ROOT'))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        env = mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
            'XDG_CONFIG_HOME': str(self.base / 'xdg'),
            'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
            'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'})
        env.start()
        self.addCleanup(env.stop)
        self.root = self.fixture('local')
        self.store = GitStore(str(self.root), str(self.base / 'state/local'), 's-group')

    def git(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE).decode().strip()

    def fixture(self, name, repository=True):
        root = self.base / name
        root.mkdir()
        (root / '.hpclib-sync.json').write_text(json.dumps({'v': 1, 'git': {
            'default_excludes': 'sync-only', 'default_excludes_version': 1}}))
        (root / 'code.txt').write_text('base\n')
        if repository:
            self.git(root, 'init', '-q', '-b', 'main')
            self.git(root, 'add', '.')
            self.git(root, 'commit', '-qm', 'ordinary user commit')
        return root

    def state(self, root):
        return (self.git(root, 'rev-parse', 'HEAD'), (root / '.git/index').read_bytes(),
                {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in root.rglob('*') if p.is_file() and '.git' not in p.relative_to(root).parts})

    def objects(self):
        return self.git(self.root, 'cat-file', '--batch-all-objects', '--batch-check')

    def pair_plan(self):
        remote_root = self.fixture('remote')
        (self.root / 'local-only.txt').write_text('local\n')
        (remote_root / 'remote-only.txt').write_text('remote\n')
        remote = GitStore(str(remote_root), str(self.base / 'state/remote'), 's-group')
        left, right = self.store.snapshot(), remote.snapshot()
        self.store.import_bundle(remote.export_bundle(), 'remote', 't-remote')
        with self.store.locked():
            self.store.prepare()
            merged = self.store.merge_candidate(left['commit'], right['commit'])
        self.assertEqual(merged['state'], 'clean', merged)
        remote.import_bundle(self.store.export_bundle(merged['commit']), 'incoming', 'r-plan')
        return remote, left, right, merged

    def native_pair(self, payload_mb=0):
        root = self.fixture("native remote's path")
        self.git(root, 'commit', '--allow-empty', '-qm', 'private ordinary remote history')
        (root / 'remote-only.txt').write_text('native remote\n')
        if payload_mb:
            with (root / 'payload.bin').open('wb') as handle:
                for _ in range(payload_mb):
                    handle.write(os.urandom(1 << 20))
        request = {'protocol': 1, 'root': str(root), 'group': 's-group', 'endpoint': 't-native',
                   'settings': {'warn_file_mb': 500, 'warn_snapshot_mb': 1000}, 'binding': {}}
        data = self.base / 'native-state'
        snapshot = rpc(dict(request, operation='snapshot'), str(data))
        self.assertEqual(snapshot['state'], 'snapshotted')
        offer = rpc(dict(request, operation='offer', commit=snapshot['commit']), str(data))
        local = self.store.snapshot()
        return root, data, request, snapshot, offer, local

    def test_native_fetch_push_preserve_user_refs_indexes_config_and_working_files(self):
        root, data, req, remote, offer, local = self.native_pair()
        before = {str(p): self.state(p) for p in (self.root, root)}
        configs = {str(p): (p / '.git/config').read_bytes() for p in (self.root, root)}
        private = self.git(root, 'rev-parse', 'HEAD')
        with mock.patch.object(GitStore, 'export_bundle', side_effect=AssertionError('no JSON bundle')):
            fetched = native_fixture(data)({}, self.store, dict(req, operation='fetch-objects', offer=offer,
                                                               _repository='/hpclib-sync'))
            self.assertEqual(fetched['commit'], remote['commit'])
            self.assertNotEqual(self.store.git('cat-file', '-e', private, check=False).returncode, 0)
            native_fixture(data)({}, self.store, dict(req, operation='push-objects', commit=local['commit'], run='r-native'))
            incoming = rpc(dict(req, operation='check-incoming', commit=local['commit'], run='r-native'), str(data))
            self.assertEqual(incoming['commit'], local['commit'])
        for p in (self.root, root):
            self.assertEqual(self.state(p), before[str(p)])
            self.assertEqual((p / '.git/config').read_bytes(), configs[str(p)])
            self.assertFalse((p / '.git/FETCH_HEAD').exists())

    def test_native_endpoint_refuses_fetch_or_push_of_ordinary_branches(self):
        root, data, req, remote, offer, local = self.native_pair()
        before = self.state(root)
        url, command = native_endpoint(data, dict(req, operation='fetch-objects', offer=offer))
        result = self.store.git('fetch', '--no-write-fetch-head', '--upload-pack=' + command,
            url, 'refs/heads/main:refs/sync/s-group/forbidden', env={'GIT_ALLOW_PROTOCOL': 'file'}, check=False)
        self.assertNotEqual(result.returncode, 0)
        url, command = native_endpoint(data, dict(req, operation='push-objects', commit=local['commit'], run='r-denied'))
        result = self.store.git('push', '--force', '--receive-pack=' + command, url,
            local['commit'] + ':refs/heads/main', env={'GIT_ALLOW_PROTOCOL': 'file'}, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state(root), before)

    def test_native_stream_is_incremental_and_has_no_json_bundle_limit(self):
        large = os.environ.get('HPCLIB_SYNC_LARGE_TRANSFER_GATE') == '1'
        root, data, req, remote, offer, local = self.native_pair(130 if large else 1)
        first, second = self.base / 'first.pack', self.base / 'second.pack'
        native_fixture(data)({}, self.store, dict(req, operation='fetch-objects', offer=offer, _trace=first))
        self.assertGreater(first.stat().st_size, 128 << 20 if large else 1 << 20)
        (root / 'tiny-change.txt').write_text('new content\n')
        changed = rpc(dict(req, operation='snapshot'), str(data))
        offer = rpc(dict(req, operation='offer', commit=changed['commit']), str(data))
        native_fixture(data)({}, self.store, dict(req, operation='fetch-objects', offer=offer, _trace=second))
        self.assertLess(second.stat().st_size, 1 << 20)
        self.assertFalse((self.root / 'payload.bin').exists())

    def test_native_receive_and_working_directory_operations_share_the_root_lock(self):
        root, data, req, remote, offer, local = self.native_pair()
        url, command = native_endpoint(data, dict(req, operation='push-objects', commit=local['commit'], run='r-held'))
        process = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 10)[0], 'receive-pack sent no advertisement')
            self.assertTrue(os.read(process.stdout.fileno(), 1024))
            with self.assertRaisesRegex(SyncError, 'busy'):
                rpc(dict(req, operation='snapshot'), str(data))
        finally:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.communicate(timeout=10)
        self.assertEqual(rpc(dict(req, operation='preview'), str(data))['state'], 'snapshot-ready')

    def test_unfinished_apply_blocks_other_groups_and_private_repositories(self):
        import sync_git
        remote, left, right, merged = self.pair_plan()
        original = sync_git.write_json
        def crash(path, value, **options):
            original(path, value, **options)
            if str(path).endswith('/apply.json') and value.get('phase') == 'files-applied':
                raise RuntimeError('injected stop after working-file application')
        with mock.patch.object(sync_git, 'write_json', side_effect=crash), self.assertRaises(RuntimeError):
            self.store.apply(left['checkpoint'], merged['commit'], 'p-shared-guard')
        other = GitStore(str(self.root), str(self.base / 'other-state'), 's-other',
                         {'branch': 'sync-other'}, locks=self.store.locks)
        with self.assertRaisesRegex(SyncError, 'owning sync'):
            other.snapshot()
        with self.assertRaises(SyncError):
            self.store.apply(left['checkpoint'], merged['commit'], 'p-different')
        self.store.apply(left['checkpoint'], merged['commit'], 'p-shared-guard')
        self.assertEqual(other.preview()['state'], 'snapshot-ready')

    def test_reviewed_first_sync_applies_git_union_preserving_heads_and_staged_indexes(self):
        (self.root / 'user-staged.txt').write_text('staged version\n')
        self.git(self.root, 'add', 'user-staged.txt')
        (self.root / 'user-staged.txt').write_text('unstaged working version\n')
        (self.root / '.pytest_cache').mkdir()
        (self.root / '.pytest_cache/keep').write_text('ignored data')
        remote, left, right, merged = self.pair_plan()
        before = {s.root: self.state(Path(s.root))[:2] for s in (self.store, remote)}
        self.assertFalse((self.root / 'remote-only.txt').exists())
        self.assertFalse((Path(remote.root) / 'local-only.txt').exists())
        self.store.validate_apply(left['checkpoint'], merged['commit'], 'p-reviewed')
        remote.validate_apply(right['checkpoint'], merged['commit'], 'p-reviewed')
        for store, snapshot in ((self.store, left), (remote, right)):
            ack = store.apply(snapshot['checkpoint'], merged['commit'], 'p-reviewed')
            self.assertEqual(ack['phase'], 'acknowledged')
            self.assertEqual(self.state(Path(store.root))[:2], before[store.root])
            self.assertEqual((Path(store.root) / 'local-only.txt').read_text(), 'local\n')
            self.assertEqual((Path(store.root) / 'remote-only.txt').read_text(), 'remote\n')
            self.assertEqual(store.preview()['summary']['new_changed_files'], 0)
            again = store.apply(snapshot['checkpoint'], merged['commit'], 'p-reviewed')
            self.assertTrue(again['recovered'])

    def crash_and_resume(self, phase, later_edit=False):
        import sync_git
        remote, left, right, merged = self.pair_plan()
        ordinary = self.state(self.root)[:2]
        original = sync_git.write_json
        def fail(path, value, **options):
            original(path, value, **options)
            if str(path).endswith('/apply.json') and value.get('phase') == phase:
                raise RuntimeError('injected crash after ' + phase)
        with mock.patch.object(sync_git, 'write_json', side_effect=fail), self.assertRaises(RuntimeError):
            self.store.apply(left['checkpoint'], merged['commit'], 'p-crash')
        if later_edit:
            (self.root / 'code.txt').write_text('edit after crash\n')
            with self.assertRaises(SyncError) as error:
                self.store.apply(left['checkpoint'], merged['commit'], 'p-crash')
            self.assertEqual(error.exception.payload['state'], 'recovery-needed')
            self.assertEqual((self.root / 'code.txt').read_text(), 'edit after crash\n')
        else:
            self.assertEqual(self.store.apply(left['checkpoint'], merged['commit'], 'p-crash')['phase'], 'acknowledged')
            self.assertEqual((self.root / 'remote-only.txt').read_text(), 'remote\n')
        self.assertEqual(self.state(self.root)[:2], ordinary)

    def test_recover_before_working_file_apply(self):
        self.crash_and_resume('prepared')

    def test_recover_after_marking_apply_started(self):
        self.crash_and_resume('applying')

    def test_recover_after_working_file_apply_before_refs(self):
        self.crash_and_resume('files-applied')

    def test_recover_after_refs_before_acknowledgement(self):
        self.crash_and_resume('refs-updated')

    def test_retry_after_durable_acknowledgement_is_idempotent(self):
        self.crash_and_resume('acknowledged')

    def test_recovery_never_rolls_back_edits_after_an_interruption(self):
        self.crash_and_resume('files-applied', later_edit=True)

    def test_ignored_collision_created_after_review_cannot_be_overwritten(self):
        rules = self.base / 'local-global-ignore'
        rules.write_text('remote-only.txt\n')
        self.git(self.root, 'config', 'core.excludesFile', str(rules))
        remote, left, right, merged = self.pair_plan()
        (self.root / 'remote-only.txt').write_text('precious ignored file\n')
        with self.assertRaises(SyncError) as error:
            self.store.validate_apply(left['checkpoint'], merged['commit'], 'p-collision')
        self.assertEqual(error.exception.payload['state'], 'path-collision')
        self.assertEqual((self.root / 'remote-only.txt').read_text(), 'precious ignored file\n')

    def test_symbolic_plan_export_ref_cannot_move_main(self):
        remote, left, right, merged = self.pair_plan()
        main = self.git(self.root, 'rev-parse', 'main')
        ref = 'refs/sync/s-group/export/' + merged['commit']
        self.git(self.root, 'update-ref', '-d', ref)
        self.git(self.root, 'symbolic-ref', ref, 'refs/heads/main')
        with self.assertRaisesRegex(SyncError, 'symbolic'):
            self.store.export_bundle(merged['commit'])
        self.assertEqual(self.git(self.root, 'rev-parse', 'main'), main)

    def test_git_conflicts_same_line_and_binary_preserve_working_files(self):
        remote_root = self.fixture('remote')
        remote = GitStore(str(remote_root), str(self.base / 'state/remote'), 's-group')
        shared = self.store.snapshot()
        remote.import_bundle(self.store.export_bundle(), 'incoming', 'r-initial')
        # Initial identical content is acknowledged as the shared base without overwriting anything.
        other = remote.snapshot()
        self.store.import_bundle(remote.export_bundle(), 'remote', 't-remote')
        with self.store.locked():
            self.store.prepare()
            base = self.store.merge_candidate(shared['commit'], other['commit'])['commit']
        remote.import_bundle(self.store.export_bundle(base), 'incoming', 'r-base')
        self.store.apply(shared['checkpoint'], base, 'p-base')
        remote.apply(other['checkpoint'], base, 'p-base')
        (self.root / 'code.txt').write_text('local conflict\n')
        (remote_root / 'code.txt').write_text('remote conflict\n')
        (self.root / 'binary.dat').write_bytes(b'\0local')
        (remote_root / 'binary.dat').write_bytes(b'\0remote')
        left, right = self.store.snapshot(), remote.snapshot()
        self.store.import_bundle(remote.export_bundle(), 'remote', 't-remote')
        before = (self.state(self.root), self.state(remote_root))
        with self.store.locked():
            self.store.prepare()
            conflict = self.store.merge_candidate(left['commit'], right['commit'], base)
        self.assertEqual(conflict['state'], 'conflict')
        self.assertIn('code.txt', conflict['paths'])
        self.assertIn('binary.dat', conflict['paths'])
        self.assertTrue(any('binary' in m['type'] for m in conflict['messages']))
        self.assertEqual((self.state(self.root), self.state(remote_root)), before)


    def test_later_local_or_remote_edit_invalidates_review_before_apply(self):
        remote, left, right, merged = self.pair_plan()
        for store, snapshot in ((self.store, left), (remote, right)):
            (Path(store.root) / 'code.txt').write_text('later edit\n')
            with self.assertRaises(SyncError) as error:
                store.apply(snapshot['checkpoint'], merged['commit'], 'p-stale')
            self.assertEqual(error.exception.payload['state'], 'stale-plan')
            self.assertEqual((Path(store.root) / 'code.txt').read_text(), 'later edit\n')
            self.assertFalse((Path(store.state) / 'apply.json').exists())

    def test_unchanged_inventory_lists_eligible_files_without_ingesting_objects(self):
        captured = self.store.snapshot()
        before = self.objects()
        checked = self.store.preview()
        self.assertEqual(checked['tip'], captured['commit'])
        self.assertEqual(checked['summary']['new_changed_files'], 0)
        files = {item['path']: item for item in checked['summary']['eligible_preview']}
        self.assertEqual(set(files), {'code.txt', '.hpclib-sync.json'})
        self.assertEqual(files['code.txt']['bytes'], len(b'base\n'))
        self.assertTrue(all(not item['metadata_changed'] for item in files.values()))
        self.assertFalse(checked['summary']['eligible_preview_truncated'])
        self.assertEqual(self.objects(), before)
        verified = self.store.preview(full_verify=True)
        self.assertEqual(verified['summary']['new_changed_files'], 2)
        self.assertTrue(all(item['metadata_changed'] for item in verified['summary']['eligible_preview']))
        self.assertEqual(self.objects(), before)
        (self.root / 'code.txt').write_text('edited\n')
        changed = self.store.preview()
        self.assertEqual(changed['summary']['new_changed_files'], 1)
        self.assertEqual(changed['summary']['largest_files'][0]['path'], 'code.txt')

    def test_eligible_preview_is_bounded_and_incomplete_counts_are_explicit(self):
        for i in range(120):
            (self.root / ('many-%03d.txt' % i)).write_text('x')
        checked = self.store.preview()
        self.assertEqual(checked['summary']['eligible_files'], 122)
        self.assertEqual(len(checked['summary']['eligible_preview']), 100)
        self.assertTrue(checked['summary']['eligible_preview_truncated'])
        paths = [p.name for p in self.root.iterdir() if p.is_file()]
        with mock.patch.object(self.store, 'paths', side_effect=lambda args, *rest:
                               iter(()) if '--ignored' in args else iter(reversed(paths))):
            reordered, _, _ = self.store.inventory()
        self.assertEqual(checked['review'], reordered['review'])
        self.assertEqual(checked['summary']['eligible_preview'], reordered['summary']['eligible_preview'])
        incomplete = self.store.preview(max_files=3)
        self.assertFalse(incomplete['summary']['complete'])
        self.assertLessEqual(len(incomplete['summary']['eligible_preview']), 3)

    def test_previous_inventory_format_gets_a_new_immutable_review_id(self):
        from sync_git import digest, write_json
        with self.store.locked():
            self.store.prepare()
            plan, records, key = self.store.inventory()
            legacy_key = dict(key)
            legacy_key.pop('summary_version')
            legacy = dict(plan, review=digest(legacy_key), plan='p-' + digest(legacy_key)[:32])
            legacy['summary'] = {k: v for k, v in plan['summary'].items() if not k.startswith('eligible_preview')}
            write_json(str(Path(self.store.state) / 'plans' / (legacy['plan'] + '.json')),
                       {'plan': legacy, 'key': legacy_key}, immutable=True)
        fresh = self.store.preview()
        self.assertNotEqual(fresh['plan'], legacy['plan'])
        with self.assertRaises(SyncError) as error:
            self.store.snapshot(legacy['review'], True)
        self.assertEqual(error.exception.payload['state'], 'stale-plan')

    def test_dirty_staged_untracked_ignored_and_ordinary_branch_are_preserved(self):
        (self.root / 'code.txt').write_text('staged\n')
        self.git(self.root, 'add', 'code.txt')
        (self.root / 'code.txt').write_text('working\n')
        (self.root / 'new.txt').write_text('new\n')
        (self.root / '.venv').mkdir()
        (self.root / '.venv/big').write_bytes(b'x' * 10000)
        before = self.state(self.root)
        result = self.store.snapshot()
        self.assertEqual(before, self.state(self.root))
        self.assertEqual(self.git(self.root, 'show', result['commit'] + ':code.txt'), 'working')
        self.assertIn('new.txt', self.git(self.root, 'ls-tree', '-r', '--name-only', result['commit']))
        self.assertNotIn('.venv', self.git(self.root, 'ls-tree', '-r', '--name-only', result['commit']))
        self.assertFalse((self.root / '.git/FETCH_HEAD').exists())

    def test_unchanged_snapshot_reuses_history_and_missing_index_recovers(self):
        first = self.store.snapshot()
        second = self.store.snapshot()
        self.assertFalse(second['changed'])
        self.assertEqual(first['commit'], second['commit'])
        Path(self.store.index).unlink()
        self.assertEqual(self.store.snapshot()['commit'], first['commit'])
        self.assertEqual(self.git(self.root, 'rev-list', '--count', 'sync'), '1')

    def test_equal_size_mtime_edits_and_deletions_are_captured(self):
        first = self.store.snapshot()
        path = self.root / 'code.txt'
        old = path.stat()
        path.write_text('edit\n')
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        second = self.store.snapshot()
        self.assertNotEqual(first['commit'], second['commit'])
        self.assertEqual(self.git(self.root, 'show', second['commit'] + ':code.txt'), 'edit')
        path.unlink()
        third = self.store.snapshot()
        self.assertNotIn('code.txt', self.git(self.root, 'ls-tree', '-r', '--name-only', third['commit']))

    def test_explicit_full_verification_rebuilds_private_stat_cache_through_git(self):
        self.store.snapshot()
        before = self.state(self.root)[:2]
        self.store.git('update-index', '--assume-unchanged', 'code.txt')
        (self.root / 'code.txt').write_text('full verification captures this\n')
        result = self.store.snapshot(full_verify=True)
        self.assertEqual(self.git(self.root, 'show', result['commit'] + ':code.txt'), 'full verification captures this')
        self.assertEqual(before, self.state(self.root)[:2])
        self.assertTrue(result['summary']['scan_limits']['full_verify'])

    def test_all_warning_kinds_stop_before_any_content_or_ref_writes(self):
        for settings in ({'warn_file_mb': 0.000001}, {'warn_snapshot_mb': 0.000001}, {'warn_file_count': 1}):
            with self.subTest(settings=settings):
                store = GitStore(str(self.root), str(self.base / ('warning-' + next(iter(settings)))), 's-warning', settings)
                before = self.objects()
                with mock.patch.object(store, 'git', wraps=store.git) as calls:
                    plan = store.snapshot()
                self.assertEqual(plan['state'], 'needs-volume-review')
                self.assertEqual(before, self.objects())
                self.assertFalse(any(any(arg in ('add', 'write-tree', 'commit-tree', 'hash-object') for arg in c.args) for c in calls.call_args_list))

    def test_review_approval_staleness_and_unchanged_large_inventory(self):
        store = GitStore(str(self.root), str(self.base / 'review'), 's-review', {'warn_file_count': 1})
        plan = store.preview()
        (self.root / 'new.txt').write_text('added after review')
        before = self.objects()
        with self.assertRaises(SyncError) as error:
            store.snapshot(review=plan['review'], approve=True)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(before, self.objects())
        fresh = store.preview()
        accepted = store.snapshot(review=fresh['review'], approve=True)
        self.assertEqual(accepted['state'], 'snapshotted')
        self.assertEqual(store.snapshot()['state'], 'snapshotted')
        self.assertEqual(store.preview()['summary']['new_changed_files'], 0)

    def test_incomplete_scan_cannot_be_approved_and_larger_budget_can_finish(self):
        for i in range(30):
            (self.root / ('file-%d' % i)).write_text('x')
        plan = self.store.preview(max_files=3)
        self.assertFalse(plan['summary']['complete'])
        before = self.objects()
        self.assertEqual(self.store.snapshot(plan['review'], True, max_files=3)['state'], 'needs-volume-review')
        self.assertEqual(before, self.objects())
        self.assertTrue(self.store.preview(max_files=100)['summary']['complete'])

    def test_growing_file_during_staging_does_not_advance_snapshot_ref(self):
        self.store.snapshot()
        before = self.git(self.root, 'rev-parse', 'sync')
        original = self.store.git
        def race(*args, **options):
            out = original(*args, **options)
            if 'add' in args:
                (self.root / 'code.txt').write_text('grew during staging\n' * 100)
            return out
        with mock.patch.object(self.store, 'git', side_effect=race), self.assertRaises(SyncError):
            self.store.snapshot()
        self.assertEqual(self.git(self.root, 'rev-parse', 'sync'), before)
        self.assertTrue((self.root / 'code.txt').read_text().startswith('grew'))

    def test_new_ignore_of_snapshotted_path_requires_selection_review(self):
        self.store.snapshot()
        (self.root / '.gitignore').write_text('code.txt\n')
        with self.assertRaises(SyncError) as error:
            self.store.snapshot()
        self.assertEqual(error.exception.payload['state'], 'needs-selection-review')

    def test_private_excludes_preserve_global_and_nested_negation_precedence(self):
        global_rules = self.base / 'global-ignore'
        global_rules.write_text('global.tmp\n')
        self.git(self.root, 'config', 'core.excludesFile', str(global_rules))
        (self.root / '.gitignore').write_text('!.DS_Store\n')
        (self.root / '.DS_Store').write_text('explicit inclusion')
        (self.root / 'global.tmp').write_text('ignored globally')
        (self.root / '__pycache__').mkdir()
        (self.root / '__pycache__/cache.pyc').write_bytes(b'ignored')
        result = self.store.snapshot()
        listing = self.git(self.root, 'ls-tree', '-r', '--name-only', result['commit'])
        self.assertIn('.DS_Store', listing)
        self.assertNotIn('global.tmp', listing)
        self.assertNotIn('cache.pyc', listing)

    def test_implicit_xdg_excludes_and_symlink_content_follow_git_semantics(self):
        ignored = self.base / 'xdg/git/ignore'
        ignored.parent.mkdir(parents=True)
        ignored.write_text('implicit.tmp\n')
        (self.root / 'implicit.tmp').write_text('excluded')
        (self.root / 'link').symlink_to('/missing/outside-target')
        result = self.store.snapshot()
        self.assertNotIn('implicit.tmp', self.git(self.root, 'ls-tree', '-r', '--name-only', result['commit']))
        self.assertEqual(self.git(self.root, 'show', result['commit'] + ':link'), '/missing/outside-target')

    def test_relative_global_exclude_file_is_resolved_from_the_work_tree(self):
        (self.root / 'rules.ignore').write_text('relative.tmp\n')
        (self.root / 'relative.tmp').write_text('excluded')
        self.git(self.root, 'config', 'core.excludesFile', 'rules.ignore')
        result = self.store.snapshot()
        self.assertNotIn('relative.tmp', self.git(self.root, 'ls-tree', '-r', '--name-only', result['commit']))

    def test_linked_worktree_root_preserves_both_ordinary_indexes_and_heads(self):
        linked = self.base / 'linked'
        self.git(self.root, 'worktree', 'add', '-q', '-b', 'work', str(linked))
        index = Path(self.git(linked, 'rev-parse', '--git-path', 'index'))
        before = (self.state(self.root)[:2], self.git(linked, 'rev-parse', 'HEAD'), index.read_bytes(), (linked / '.git').read_bytes())
        (linked / 'code.txt').write_text('linked working edit\n')
        store = GitStore(str(linked), str(self.base / 'linked-state'), 's-linked')
        result = store.snapshot()
        self.assertEqual(self.git(linked, 'show', result['commit'] + ':code.txt'), 'linked working edit')
        self.assertEqual(before, (self.state(self.root)[:2], self.git(linked, 'rev-parse', 'HEAD'), index.read_bytes(), (linked / '.git').read_bytes()))
        # Another worktree of the same repository must not reuse this root's branch claim, even in one group.
        other_root = GitStore(str(self.root), str(self.base / 'other-root-state'), 's-linked')
        with self.assertRaisesRegex(SyncError, 'not owned'):
            other_root.snapshot()

    def test_nested_repo_checked_out_branch_unowned_branch_and_alias_are_refused(self):
        nested = self.root / 'vendor'
        nested.mkdir()
        self.git(nested, 'init', '-q')
        (nested / 'file').write_text('vendor')
        self.git(nested, 'add', '.')
        self.git(nested, 'commit', '-qm', 'vendor')
        with self.assertRaises(SyncError):
            self.store.snapshot()
        import shutil
        shutil.rmtree(nested)
        self.git(self.root, 'branch', 'sync')
        with self.assertRaises(SyncError):
            self.store.snapshot()
        self.git(self.root, 'checkout', '-q', 'sync')
        with self.assertRaisesRegex(SyncError, 'checked out'):
            self.store.snapshot()
        self.git(self.root, 'checkout', '-q', 'main')
        self.git(self.root, 'branch', '-D', 'sync')
        self.git(self.root, 'symbolic-ref', 'refs/heads/sync', 'refs/heads/main')
        before = self.state(self.root)
        with self.assertRaises(SyncError):
            self.store.snapshot()
        self.assertEqual(before, self.state(self.root))

    def test_nonrepo_root_uses_private_git_and_unresolved_setup_is_refused(self):
        root = self.fixture('plain', repository=False)
        store = GitStore(str(root), str(self.base / 'plain-state'), 's-plain')
        self.assertEqual(store.snapshot()['state'], 'snapshotted')
        self.assertFalse((root / '.git').exists())
        (root / '.hpclib-sync.json').unlink()
        with self.assertRaises(SyncError) as error:
            store.preview()
        self.assertEqual(error.exception.payload['state'], 'needs-ignore-setup')

    def test_both_directions_exchange_real_objects_without_advancing_heads_or_files(self):
        remote = self.fixture('remote')
        other = GitStore(str(remote), str(self.base / 'state/remote'), 's-group')
        (remote / 'code.txt').write_text('remote edits\n')
        local_commit, remote_commit = self.store.snapshot(), other.snapshot()
        before_local, before_remote = self.state(self.root), self.state(remote)
        self.store.import_bundle(other.export_bundle(), 'remote', 't-remote')
        incoming = other.import_bundle(self.store.export_bundle(), 'incoming', 'r-run')
        self.assertEqual(self.git(self.root, 'show', remote_commit['commit'] + ':code.txt'), 'remote edits')
        self.assertEqual(incoming['commit'], local_commit['commit'])
        self.assertEqual(before_local, self.state(self.root))
        self.assertEqual(before_remote, self.state(remote))
        self.assertEqual(self.git(remote, 'rev-parse', 'sync'), remote_commit['commit'])
        self.assertFalse((remote / '.git/FETCH_HEAD').exists())

    def test_remote_protocol_runs_as_a_real_process(self):
        request = {'protocol': 1, 'operation': 'snapshot', 'group': 's-rpc', 'endpoint': 't-rpc', 'root': str(self.root)}
        result = subprocess.run([sys.executable, str(ROOT / 'hpclib/servers/sync_git.py')],
            input=json.dumps(request).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(os.environ, HPCTUNNELS_DATA_DIR=str(self.base / 'remote-home')))
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(json.loads(result.stdout)['result']['state'], 'snapshotted')


if __name__ == '__main__':
    unittest.main()
