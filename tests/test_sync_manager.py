"""Sync storage/setup: real files, explicit writes, stale previews, and preserved user work."""
import json
import base64
import os
from pathlib import Path
import stat
import shlex
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hpclib' / 'servers'))
from sync_manager import SyncManager
from sync_spec import METADATA_FILE, SyncError
from test_sync_spec import spec


def write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def native_endpoint(data_dir, request):
    from sync_git import endpoint_store
    remote = endpoint_store(request, str(data_dir))
    with remote.locked():
        remote.prepare(recovering=True)
    fetch = request['operation'] == 'fetch-objects'
    wire = {k: request[k] for k in ('protocol', 'root', 'group', 'endpoint', 'settings', 'binding')}
    wire['commit'] = request['offer']['commit'] if fetch else request['commit']
    if not fetch:
        wire['run'] = request['run']
    encoded = base64.urlsafe_b64encode(json.dumps(wire).encode()).decode()
    args = ['env', 'HPCTUNNELS_DATA_DIR=' + str(data_dir), sys.executable,
            str(Path(__file__).resolve().parents[1] / 'hpclib/servers/sync_git.py'),
            '--git-upload-pack' if fetch else '--git-receive-pack', encoded, request.get('_repository', 'hpclib-sync')]
    program = 'sh -c ' + shlex.quote('exec ' + ' '.join(shlex.quote(x) for x in args))
    return remote.repo, program


def native_fixture(data_dir):
    """Run real fetch/push and the production pack endpoint over Git's local transport."""
    def transfer(profile, local, request):
        url, program = native_endpoint(data_dir, request)
        env = {'GIT_ALLOW_PROTOCOL': 'file'}
        if request.get('_trace'):
            env['GIT_TRACE_PACKFILE'] = str(request['_trace'])
        if request['operation'] == 'fetch-objects':
            return local.fetch_objects(url, request['offer'], request['endpoint'], program, env)
        return local.push_objects(url, request['commit'], request['run'], program, env)
    return transfer


class TestSyncManager(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hpclib-sync-test-', dir=os.environ.get('HPCLIB_SYNC_FIXTURE_ROOT'))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        env = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.base / 'xdg')})
        env.start()
        self.addCleanup(env.stop)
        self.root = self.base / 'code'
        self.root.mkdir()
        self.state = self.base / 'state'
        self.manager = SyncManager(str(self.state), None, write_private)
        self.manager.transfer = native_fixture(self.base / 'remote-state')
        self.addCleanup(self.manager.close)

    def wait_for_run(self):
        deadline = time.monotonic() + 30
        while self.manager.busy() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.manager.busy(), 'background snapshot run did not finish')
        return self.manager.list()['syncs'][0]

    def sync_group(self):
        from sync_git import rpc
        body = spec(str(self.root))
        roots = []
        for i, target in enumerate(body['targets']):
            root = self.base / ('cluster-%d' % i)
            root.mkdir()
            target['remote'] = str(root)
            (root / ('remote-%d.txt' % i)).write_text('remote %d\n' % i)
            roots.append(root)
        (self.root / 'local.txt').write_text('local\n')
        record = self.apply(self.manager.save(body), 'sync-only')['sync']
        for root in roots:
            (root / METADATA_FILE).write_bytes((self.root / METADATA_FILE).read_bytes())
        self.manager.profiles = lambda name: {'name': name}
        self.manager.remote = lambda profile, request: rpc(request, str(self.base / 'remote-state'))
        setup = self.manager.provision_preview(record['spec']['id'])
        self.manager.provision_apply(record['spec']['id'], dict(revision=setup['revision'], review=setup['review'], action='private-git', confirm=True))
        return record, roots

    def review_sync(self, record):
        self.manager.start(record['spec']['id'], {'revision': record['spec']['revision']}, intent='sync')
        return self.wait_for_run()

    def apply_sync(self, record):
        self.manager.resolve(record['spec']['id'], {'revision': record['spec']['revision'],
            'plan': record['status']['plan'], 'action': 'apply-sync', 'confirm': True})
        return self.wait_for_run()

    def test_reviewed_group_sync_converges_local_and_two_remotes_and_acknowledges(self):
        from sync_git import GitStore
        record, roots = self.sync_group()
        reviewed = self.review_sync(record)
        self.assertEqual(reviewed['status']['state'], 'needs-sync-review', reviewed['status'])
        self.assertFalse((self.root / 'remote-0.txt').exists())
        plan = self.manager.plan(record['spec']['id'], reviewed['status']['plan'])
        self.assertEqual(plan['local_changes']['counts']['A'], 2)
        done = self.apply_sync(reviewed)
        self.assertEqual(done['status']['state'], 'in-sync', done['status'])
        for root in [self.root] + roots:
            self.assertEqual((root / 'local.txt').read_text(), 'local\n')
            self.assertEqual((root / 'remote-0.txt').read_text(), 'remote 0\n')
            self.assertEqual((root / 'remote-1.txt').read_text(), 'remote 1\n')
        self.assertTrue(all(t['last_successful_sync'] for t in done['status']['targets']))
        candidate = done['status']['sync_review']['candidate']
        local = GitStore(str(self.root), str(self.state / 'git' / record['spec']['id']), record['spec']['id'])
        with local.locked():
            local.prepare()
            self.assertTrue(all(local.base(t['id']) == candidate for t in record['spec']['targets']))
        repeated = self.apply_sync(self.review_sync(done))
        self.assertEqual(repeated['status']['state'], 'in-sync', repeated['status'])
        self.assertEqual(repeated['status']['sync_review']['candidate'], candidate)

    def test_remote_edit_reaches_local_and_other_remote_with_git_deletion_rename_and_mode(self):
        record, roots = self.sync_group()
        synced = self.apply_sync(self.review_sync(record))
        (roots[0] / 'local.txt').unlink()
        (roots[0] / 'remote-0.txt').rename(roots[0] / 'renamed remote.txt')
        (roots[0] / 'new link').symlink_to('renamed remote.txt')
        (roots[0] / 'run.sh').write_text('#!/bin/sh\nexit 0\n')
        (roots[0] / 'run.sh').chmod(0o755)
        result = self.apply_sync(self.review_sync(synced))
        self.assertEqual(result['status']['state'], 'in-sync', result['status'])
        for root in [self.root] + roots:
            self.assertFalse((root / 'local.txt').exists())
            self.assertFalse((root / 'remote-0.txt').exists())
            self.assertEqual((root / 'renamed remote.txt').read_text(), 'remote 0\n')
            self.assertEqual(os.readlink(root / 'new link'), 'renamed remote.txt')
            self.assertTrue((root / 'run.sh').stat().st_mode & 0o111)

    def test_remote_a_remote_b_conflict_blocks_all_working_files(self):
        record, roots = self.sync_group()
        synced = self.apply_sync(self.review_sync(record))
        (roots[0] / 'local.txt').write_text('A conflict\n')
        (roots[1] / 'local.txt').write_text('B conflict\n')
        conflict = self.review_sync(synced)
        self.assertEqual(conflict['status']['state'], 'conflict', conflict['status'])
        self.assertIn('local.txt', conflict['status']['sync_review']['conflict']['paths'])
        self.assertEqual((self.root / 'local.txt').read_text(), 'local\n')
        self.assertEqual((roots[0] / 'local.txt').read_text(), 'A conflict\n')
        self.assertEqual((roots[1] / 'local.txt').read_text(), 'B conflict\n')
        with self.assertRaises(SyncError):
            self.apply_sync(conflict)

    def test_later_remote_edit_invalidates_group_review_before_local_writes(self):
        record, roots = self.sync_group()
        reviewed = self.review_sync(record)
        (roots[1] / 'remote-1.txt').write_text('later edit\n')
        result = self.apply_sync(reviewed)
        self.assertEqual(result['status']['state'], 'stale-plan', result['status'])
        self.assertFalse((self.root / 'remote-0.txt').exists())
        self.assertFalse((roots[0] / 'local.txt').exists())
        self.assertEqual((roots[1] / 'remote-1.txt').read_text(), 'later edit\n')

    def test_partial_distribution_preserves_failed_base_and_retries_idempotently(self):
        from sync_git import GitStore
        record, roots = self.sync_group()
        reviewed = self.review_sync(record)
        original = self.manager.remote
        failed = record['spec']['targets'][0]['id']
        def drop(profile, request):
            if request['endpoint'] == failed and request['operation'] == 'apply':
                raise SyncError(409, 'injected lost master', state='login-needed')
            return original(profile, request)
        self.manager.remote = drop
        partial = self.apply_sync(reviewed)
        self.assertEqual(partial['status']['state'], 'partial', partial['status'])
        self.assertTrue(partial['status']['unfinished_apply'])
        with self.assertRaisesRegex(SyncError, 'reconcile'):
            self.manager.save(partial['spec'], partial['spec']['id'])
        with self.assertRaisesRegex(SyncError, 'reconcile'):
            self.manager.pause(partial['spec']['id'], True, partial['spec']['revision'])
        local = GitStore(str(self.root), str(self.state / 'git' / record['spec']['id']), record['spec']['id'])
        with local.locked():
            local.prepare()
            self.assertIsNone(local.base(failed))
        self.manager.remote = original
        retried = self.apply_sync(partial)
        self.assertEqual(retried['status']['state'], 'in-sync', retried['status'])
        self.assertFalse(retried['status']['unfinished_apply'])

    def test_git_merges_disjoint_edits_to_same_file_and_detects_delete_edit(self):
        record, roots = self.sync_group()
        original = ''.join('line %d\n' % i for i in range(20))
        for root in [self.root] + roots:
            (root / 'shared.txt').write_text(original)
        synced = self.apply_sync(self.review_sync(record))
        (self.root / 'shared.txt').write_text(original.replace('line 1\n', 'local edit\n'))
        (roots[1] / 'shared.txt').write_text(original.replace('line 18\n', 'remote edit\n'))
        result = self.apply_sync(self.review_sync(synced))
        self.assertEqual(result['status']['state'], 'in-sync', result['status'])
        combined = original.replace('line 1\n', 'local edit\n').replace('line 18\n', 'remote edit\n')
        for root in [self.root] + roots:
            self.assertEqual((root / 'shared.txt').read_text(), combined)
        (self.root / 'shared.txt').unlink()
        (roots[0] / 'shared.txt').write_text('edit against deletion\n')
        conflict = self.review_sync(result)
        self.assertEqual(conflict['status']['state'], 'conflict', conflict['status'])
        self.assertFalse((self.root / 'shared.txt').exists())
        self.assertEqual((roots[1] / 'shared.txt').read_text(), combined)

    def test_volume_approval_continues_to_sync_review_without_applying(self):
        record, roots = self.sync_group()
        body = dict(record['spec'])
        body['git'] = dict(body['git'], warn_file_count=1)
        record = self.manager.save(body, body['id'])
        current = self.review_sync(record)
        for _ in range(4):
            if current['status']['state'] != 'needs-volume-review':
                break
            self.manager.resolve(current['spec']['id'], {'revision': current['spec']['revision'],
                'plan': current['status']['plan'], 'action': 'approve-snapshot', 'confirm': True})
            current = self.wait_for_run()
        self.assertEqual(current['status']['state'], 'needs-sync-review', current['status'])
        self.assertFalse((self.root / 'remote-0.txt').exists())
        self.assertFalse((roots[0] / 'local.txt').exists())

    def test_size_review_preserves_last_successful_sync_history(self):
        record, roots = self.sync_group()
        synced = self.apply_sync(self.review_sync(record))
        stamps = {t['target']: t['last_successful_sync'] for t in synced['status']['targets']}
        body = dict(synced['spec'])
        body['git'] = dict(body['git'], warn_file_count=1)
        changed = self.manager.save(body, body['id'])
        (self.root / 'new-after-sync.txt').write_text('new file\n')
        paused = self.review_sync(changed)
        self.assertEqual(paused['status']['state'], 'needs-volume-review', paused['status'])
        self.assertEqual(paused['status']['last_successes'], stamps)

    def test_offline_target_is_excluded_and_keeps_its_previous_acknowledged_base(self):
        from sync_git import GitStore
        record, roots = self.sync_group()
        synced = self.apply_sync(self.review_sync(record))
        original = self.manager.remote
        excluded = record['spec']['targets'][0]['id']
        previous = synced['status']['sync_review']['candidate']
        def offline(profile, request):
            if request['endpoint'] == excluded:
                raise SyncError(409, 'offline fixture', state='login-needed')
            return original(profile, request)
        self.manager.remote = offline
        (self.root / 'local.txt').write_text('next local edit\n')
        review = self.review_sync(synced)
        self.assertEqual(review['status']['state'], 'needs-sync-review', review['status'])
        self.assertEqual(len(review['status']['sync_review']['excluded']), 1)
        done = self.apply_sync(review)
        self.assertEqual(done['status']['state'], 'partial', done['status'])
        local = GitStore(str(self.root), str(self.state / 'git' / record['spec']['id']), record['spec']['id'])
        with local.locked():
            local.prepare()
            self.assertEqual(local.base(excluded), previous)
        self.manager.remote = original
        self.assertEqual(self.apply_sync(self.review_sync(done))['status']['state'], 'in-sync')

    def test_snapshot_cycle_captures_local_once_and_exchanges_both_remote_histories(self):
        from sync_git import GitStore, rpc
        local_file = self.root / 'local.txt'
        local_file.write_text('local content')
        body = spec(str(self.root))
        for i, target in enumerate(body['targets']):
            remote = self.base / ('remote-%d' % i)
            remote.mkdir()
            (remote / 'remote.txt').write_text('remote-%d edit' % i)
            (remote / METADATA_FILE).write_text(json.dumps({'v': 1, 'git': {'default_excludes': 'off', 'default_excludes_version': 1}}))
            target['remote'] = str(remote)
        record = self.manager.save(body)
        record = self.apply(record, 'off')['sync']
        self.manager.profiles = lambda name: {'name': name}
        self.manager.remote = lambda profile, request: rpc(request, str(self.base / 'remote-state'))
        setup = self.manager.provision_preview(record['spec']['id'])
        self.manager.provision_apply(record['spec']['id'], dict(revision=setup['revision'], review=setup['review'], action='private-git', confirm=True))
        original = GitStore.git
        local_adds = []
        def counted(store, *args, **options):
            if store.root == str(self.root) and 'add' in args:
                local_adds.append(args)
            return original(store, *args, **options)
        with mock.patch.object(GitStore, 'git', counted):
            self.manager.start(record['spec']['id'], {'revision': record['spec']['revision']})
            result = self.wait_for_run()
        self.assertEqual(result['status']['state'], 'snapshots-ready', result['status'])
        self.assertEqual(len(local_adds), 1)
        self.assertEqual(len(result['status']['targets']), 2)
        self.assertTrue(all(t['state'] == 'snapshot-captured' and t['last_successful_sync'] is None for t in result['status']['targets']))
        local = GitStore(str(self.root), str(self.state / 'git' / record['spec']['id']), record['spec']['id'])
        local.prepare()
        for i, target in enumerate(result['status']['targets']):
            self.assertTrue(local.text('show', target['fetched']['commit'] + ':remote.txt').startswith('remote-'))
        self.assertEqual(local_file.read_text(), 'local content')
        self.assertFalse((self.root / '.git').exists())
        # A clean check means unchanged relative to each endpoint's own commit, not equal working folders.
        commits = [result['status']['local_snapshot']['commit']] + [t['snapshot']['commit'] for t in result['status']['targets']]
        self.manager.start(record['spec']['id'], {'revision': record['spec']['revision']}, preview=True)
        checked = self.wait_for_run()['status']
        self.assertEqual(checked['state'], 'preview-ready')
        self.assertIn('Use Review sync to compare directories', checked['message'])
        snapshots = [checked['local_snapshot']] + [t['snapshot'] for t in checked['targets']]
        self.assertEqual([s['tip'] for s in snapshots], commits)
        self.assertTrue(all(s['summary']['new_changed_files'] == 0 for s in snapshots))
        self.assertIn('local.txt', [f['path'] for f in snapshots[0]['summary']['eligible_preview']])
        self.assertFalse((self.root / 'remote.txt').exists())

    def test_background_volume_plan_requires_complete_fresh_confirmation(self):
        body = spec(str(self.root))
        body['git'] = {'warn_file_count': 1}
        record = self.manager.save(body)
        record = self.apply(record, 'off')['sync']
        sync_id, revision = record['spec']['id'], record['spec']['revision']
        self.manager.start(sync_id, {'revision': revision})
        blocked = self.wait_for_run()
        self.assertEqual(blocked['status']['state'], 'needs-volume-review')
        plan = self.manager.plan(sync_id, blocked['status']['plan'])
        with self.assertRaises(SyncError):
            self.manager.resolve(sync_id, {'revision': revision, 'plan': plan['snapshot']['plan'], 'action': 'approve-snapshot'})
        self.manager.resolve(sync_id, {'revision': revision, 'plan': plan['snapshot']['plan'], 'action': 'approve-snapshot', 'confirm': True})
        result = self.wait_for_run()
        self.assertEqual(result['status']['local_snapshot']['state'], 'snapshotted')
        self.assertEqual(result['status']['state'], 'partial')  # No SSH callback in this fixture.

    def test_stale_snapshot_approval_leaves_no_snapshot_commit(self):
        body = spec(str(self.root))
        body['git'] = {'warn_file_count': 1}
        record = self.apply(self.manager.save(body), 'off')['sync']
        sync_id, revision = record['spec']['id'], record['spec']['revision']
        self.manager.start(sync_id, {'revision': revision})
        plan = self.wait_for_run()['status']['plan']
        (self.root / 'later.txt').write_text('after review')
        self.manager.resolve(sync_id, {'revision': revision, 'plan': plan, 'action': 'approve-snapshot', 'confirm': True})
        result = self.wait_for_run()
        self.assertEqual(result['status']['state'], 'stale-plan')
        from sync_git import GitStore
        local = GitStore(str(self.root), str(self.state / 'git' / sync_id), sync_id)
        local.prepare()
        self.assertIsNone(local.tip)

    def test_preview_does_not_create_commits_and_busy_run_rejects_registration_changes(self):
        from sync_git import rpc
        entered, release = threading.Event(), threading.Event()
        record = self.apply(self.save(), 'off')['sync']
        sync_id, revision = record['spec']['id'], record['spec']['revision']
        self.manager.profiles = lambda name: {'name': name}
        def remote(profile, request):
            entered.set()
            release.wait(5)
            raise SyncError(409, 'fixture offline', state='login-needed')
        self.manager.remote = remote
        try:
            self.manager.start(sync_id, {'revision': revision}, preview=True)
            self.assertTrue(entered.wait(10))
            for operation in (lambda: self.manager.save(record['spec'], sync_id),
                              lambda: self.manager.pause(sync_id, True, revision),
                              lambda: self.manager.unregister(sync_id, {'confirm': True, 'revision': revision}),
                              lambda: self.manager.start(sync_id, {'revision': revision})):
                with self.assertRaises(SyncError) as error:
                    operation()
                self.assertEqual(error.exception.status, 409)
        finally:
            release.set()
        result = self.wait_for_run()
        # An initial offline plain folder must wait for the Git setup decision before even creating private history.
        self.assertNotIn('local_snapshot', result['status'])
        self.assertFalse((self.state / 'git' / sync_id).exists())
        self.assertEqual(result['status']['state'], 'login-needed')

    def test_missing_root_run_finishes_instead_of_leaking_busy_lock(self):
        record = self.save()
        self.root.rmdir()
        self.manager.start(record['spec']['id'], {'revision': record['spec']['revision']})
        self.assertEqual(self.wait_for_run()['status']['state'], 'inactive')

    def save(self):
        return self.manager.save(spec(str(self.root)))

    def apply(self, record, mode, preview=None):
        sync_id = record['spec']['id']
        p = preview or self.manager.setup_preview(sync_id)
        return self.manager.setup_apply(sync_id, {'revision': p['revision'], 'setup': {
            'mode': mode, 'review': p['review'], 'confirm': True}})

    def test_constructor_and_cached_reads_have_no_writes_or_subprocesses(self):
        with mock.patch('subprocess.run', side_effect=AssertionError('no commands')):
            self.assertEqual(self.manager.list()['syncs'], [])
        self.assertFalse(self.state.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_two_targets_persist_without_touching_root(self):
        with mock.patch('subprocess.run', side_effect=AssertionError('no commands')):
            saved = self.save()
            restored = SyncManager(str(self.state), None, write_private).get(saved['spec']['id'])
        self.assertEqual(saved, restored)
        self.assertEqual(len(restored['spec']['targets']), 2)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(stat.S_IMODE((self.state / 'specs.json').stat().st_mode), 0o600)
        self.assertFalse(saved['capabilities']['rclone'])

    def test_duplicate_nested_and_aliased_local_roots_refused(self):
        saved = self.save()
        for path in (self.root, self.root / 'nested'):
            with self.subTest(path=path), self.assertRaises(SyncError) as caught:
                self.manager.save(spec(str(path)))
            self.assertEqual(caught.exception.payload['existing'], saved['spec']['id'])
        alias = self.base / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(SyncError):
            self.manager.save(spec(str(alias)))

    def test_revision_and_warning_changes_stale_setup_review(self):
        saved = self.save()
        sync_id = saved['spec']['id']
        preview = self.manager.setup_preview(sync_id)
        changed = saved['spec']
        changed['git']['warn_file_count'] = 4
        updated = self.manager.save(changed, sync_id)
        self.assertEqual(updated['spec']['git']['warn_file_count'], 4)
        with self.assertRaises(SyncError) as caught:
            self.apply(saved, 'off', preview)
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse((self.root / METADATA_FILE).exists())
        with self.assertRaises(SyncError):
            self.manager.save(changed, sync_id)

    def test_declining_both_records_off_without_changing_ignore(self):
        original = b'custom/\n!custom/keep.txt\n# human note\n'
        (self.root / '.gitignore').write_bytes(original)
        saved = self.save()
        result = self.apply(saved, 'off')
        self.assertEqual((self.root / '.gitignore').read_bytes(), original)
        self.assertEqual(result['setup']['metadata']['git']['default_excludes'], 'off')
        self.assertFalse(self.manager.setup_preview(saved['spec']['id'])['needs_setup'])

    def test_sync_only_metadata_and_original_file_preserved(self):
        original = b'# custom\r\n*.tmp\r\n!keep.tmp\r\n'
        (self.root / '.gitignore').write_bytes(original)
        saved = self.save()
        self.apply(saved, 'sync-only')
        self.assertEqual((self.root / '.gitignore').read_bytes(), original)
        self.assertEqual(json.loads((self.root / METADATA_FILE).read_text())['git']['default_excludes'], 'sync-only')
        self.assertFalse((self.root / '.git' / 'info' / 'exclude').exists())

    def test_merge_preserves_original_order_mode_and_is_idempotent(self):
        original = b'# user rules\r\n!.DS_Store\r\noutput/\r\n'
        f = self.root / '.gitignore'
        f.write_bytes(original)
        f.chmod(0o644)
        saved = self.save()
        result = self.apply(saved, 'gitignore')
        merged = f.read_bytes()
        self.assertTrue(merged.endswith(original))
        self.assertIn(b'# BEGIN hpclib sync defaults v1\r\n', merged)
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o644)
        self.apply(result['sync'], 'gitignore')
        self.assertEqual(f.read_bytes(), merged)

    def test_existing_metadata_uses_recorded_choice_and_portable_thresholds(self):
        (self.root / METADATA_FILE).write_text(json.dumps({'v': 1, 'git': {
            'default_excludes': 'sync-only', 'default_excludes_version': 1, 'warn_file_count': 8}}))
        saved = self.save()
        preview = self.manager.setup_preview(saved['spec']['id'])
        self.assertFalse(preview['needs_setup'])
        self.assertEqual(preview['effective_git']['warn_file_count'], 8)
        self.assertEqual(preview['setting_sources']['warn_file_count'], 'repository metadata')

    def test_no_ignore_file_private_default_does_not_create_it(self):
        saved = self.save()
        self.apply(saved, 'sync-only')
        self.assertFalse((self.root / '.gitignore').exists())

    def test_concurrent_ignore_edit_invalidates_review(self):
        saved = self.save()
        p = self.manager.setup_preview(saved['spec']['id'])
        (self.root / '.gitignore').write_text('new-human-edit\n')
        with self.assertRaises(SyncError) as caught:
            self.apply(saved, 'gitignore', p)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual((self.root / '.gitignore').read_text(), 'new-human-edit\n')

    def test_malformed_or_symlink_metadata_is_never_overwritten(self):
        saved = self.save()
        f = self.root / METADATA_FILE
        f.write_text('{broken')
        with self.assertRaises(SyncError):
            self.manager.setup_preview(saved['spec']['id'])
        self.assertEqual(f.read_text(), '{broken')
        f.unlink()
        outside = self.base / 'other.json'
        outside.write_text('{}')
        f.symlink_to(outside)
        with self.assertRaises(SyncError):
            self.manager.setup_preview(saved['spec']['id'])
        self.assertEqual(outside.read_text(), '{}')

    def test_pause_revision_and_unregister_preserve_metadata_and_history(self):
        saved = self.save()
        updated = self.apply(saved, 'off')['sync']
        sync_id = saved['spec']['id']
        paused = self.manager.pause(sync_id, True, updated['spec']['revision'])
        self.assertTrue(paused['spec']['paused'])
        self.manager.unregister(sync_id, {'confirm': True, 'revision': paused['spec']['revision']})
        self.assertTrue((self.root / METADATA_FILE).exists())
        self.assertTrue((self.state / 'removed' / (sync_id + '.json')).exists())
        self.assertEqual(self.manager.list()['syncs'], [])

    def test_corrupt_store_not_silently_reset(self):
        self.state.mkdir()
        (self.state / 'specs.json').write_text('bad')
        with self.assertRaises(SyncError):
            self.manager.list()
        self.assertEqual((self.state / 'specs.json').read_text(), 'bad')

    def test_independent_managers_serialize_writes_without_lost_records(self):
        barrier = threading.Barrier(4)
        errors = []
        def save_one(i):
            manager = SyncManager(str(self.state), None, write_private)
            body = spec(str(self.base / ('root-' + str(i))))
            for t in body['targets']:
                t['remote'] += '-' + str(i)
            try:
                barrier.wait(timeout=5)
                manager.save(body)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=save_one, args=(i,)) for i in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.manager.list()['syncs']), 4)

    def test_write_failure_is_reported_without_recording_completed_setup(self):
        saved = self.save()
        p = self.manager.setup_preview(saved['spec']['id'])
        with mock.patch.object(self.manager, 'write_private', side_effect=PermissionError('read-only fixture')):
            with self.assertRaises(SyncError) as caught:
                self.apply(saved, 'sync-only', p)
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse((self.root / METADATA_FILE).exists())
        self.assertTrue(self.manager.setup_preview(saved['spec']['id'])['needs_setup'])


if __name__ == '__main__':
    unittest.main()
