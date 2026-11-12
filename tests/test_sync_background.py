"""Automatic admission and upload-only integration with real Git and disposable endpoints."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hpclib/servers'))
from sync_git import GitStore, root_probe, bootstrap, endpoint_store, rpc
from sync_spec import SyncError, METADATA_FILE, validate
from test_sync_spec import spec
import test_sync_manager as fixtures
from test_sync_manager import write_private, native_fixture


class TestSyncBackground(unittest.TestCase):
    setUp = fixtures.TestSyncManager.setUp
    wait_for_run = fixtures.TestSyncManager.wait_for_run
    sync_group = fixtures.TestSyncManager.sync_group
    apply = fixtures.TestSyncManager.apply
    review_sync = fixtures.TestSyncManager.review_sync
    apply_sync = fixtures.TestSyncManager.apply_sync

    def configure(self, record, on_change='auto-push', interval=10, background=True):
        body = copy.deepcopy(record['spec'])
        body.update(direction='both' if on_change == 'auto-both' else 'push', interval=interval,
                    policy={'background': background, 'on_change': on_change, 'merge': 'review'})
        return self.manager.save(body, body['id'])

    def tick(self, record, now):
        self.manager.clock = lambda: now
        self.manager.schedule_pump()
        return self.wait_for_run()

    def remote_store(self, record, target=0):
        t = record['spec']['targets'][target]
        return endpoint_store(dict(root=t['remote'], group=record['spec']['id'], endpoint=t['id']), str(self.base / 'remote-state'))

    def test_uploads_do_not_fetch_or_snapshot_cluster_outputs_and_repeat_without_history_churn(self):
        record, roots = self.sync_group()
        for root in roots:
            outputs = root / 'outputs'
            outputs.mkdir()
            for i in range(1100):
                (outputs / ('result-%d' % i)).write_bytes(b'cluster only\n')
        record = self.configure(record)
        original = self.manager.transfer
        operations = []
        def upload_only(profile, local, request):
            operations.append(request['operation'])
            self.assertNotEqual(request['operation'], 'fetch-objects')
            return original(profile, local, request)
        self.manager.transfer = upload_only
        reviewed = self.review_sync(record)
        self.assertEqual(reviewed['status']['state'], 'needs-sync-review', reviewed['status'])
        done = self.apply_sync(reviewed)
        self.assertEqual(done['status']['state'], 'uploaded', done['status'])
        self.assertFalse((self.root / 'outputs').exists())
        for root in roots:
            self.assertEqual((root / 'local.txt').read_text(), 'local\n')
            self.assertTrue((root / 'remote-0.txt').exists() or (root / 'remote-1.txt').exists())
        remote = self.remote_store(done)
        with remote.locked():
            remote.prepare()
            tip = remote.tip
            self.assertFalse(any(p.startswith('outputs/') for p in remote.entries(tip)))
        done = self.tick(done, 100)
        done = self.tick(done, 111)
        self.assertEqual(done['status']['state'], 'uploaded', done['status'])
        with remote.locked():
            remote.prepare()
            self.assertEqual(tip, remote.tip)
        (self.root / 'local.txt').write_text('edited locally\n')
        (self.root / 'added.txt').write_text('new\n')
        done = self.tick(done, 122)
        self.assertEqual(done['status']['state'], 'uploaded', done['status'])
        for root in roots:
            self.assertEqual((root / 'local.txt').read_text(), 'edited locally\n')
            self.assertEqual((root / 'added.txt').read_text(), 'new\n')
        (self.root / 'added.txt').unlink()
        done = self.tick(done, 133)
        self.assertEqual(done['status']['state'], 'uploaded', done['status'])
        self.assertTrue(all(not (r / 'added.txt').exists() for r in roots))
        self.assertEqual(len(operations), 6)

    def test_first_automatic_upload_requires_review_and_remote_edits_are_held(self):
        record, roots = self.sync_group()
        record = self.configure(record)
        self.tick(record, 100)
        reviewed = self.tick(record, 111)
        self.assertEqual(reviewed['status']['state'], 'needs-sync-review', reviewed['status'])
        self.assertTrue(all(not (r / 'local.txt').exists() for r in roots))
        done = self.apply_sync(reviewed)
        (roots[0] / 'local.txt').write_text('cluster edit\n')
        held = self.tick(done, 122)
        self.assertEqual(held['status']['state'], 'needs-sync-review', held['status'])
        self.assertEqual((self.root / 'local.txt').read_text(), 'local\n')
        self.assertFalse(held['status']['sync_review']['auto_ready'])
        self.tick(held, 1000)
        self.assertEqual((roots[0] / 'local.txt').read_text(), 'cluster edit\n')

    def test_local_large_snapshot_stops_background_until_review(self):
        record, roots = self.sync_group()
        record = self.configure(record)
        done = self.apply_sync(self.review_sync(record))
        body = copy.deepcopy(done['spec'])
        body['git']['warn_file_mb'] = 0.001
        done = self.manager.save(body, body['id'])
        self.tick(done, 100)
        (self.root / 'large.bin').write_bytes(b'x' * 2000)
        held = self.tick(done, 111)
        self.assertEqual(held['status']['state'], 'needs-volume-review', held['status'])
        run = held['status']['run']
        again = self.tick(held, 1000)
        self.assertEqual(run, again['status']['run'])
        self.assertTrue(all(not (r / 'large.bin').exists() for r in roots))

    def test_two_way_automatic_edits_use_acknowledged_base(self):
        record, roots = self.sync_group()
        record = self.configure(record, 'auto-both')
        done = self.apply_sync(self.review_sync(record))
        self.tick(done, 100)
        (roots[0] / 'remote-0.txt').write_text('updated on cluster\n')
        done = self.tick(done, 111)
        self.assertEqual(done['status']['state'], 'in-sync', done['status'])
        self.assertEqual((self.root / 'remote-0.txt').read_text(), 'updated on cluster\n')

    def test_background_requires_explicit_opt_in_and_interval_and_respects_pause_close(self):
        record, _ = self.sync_group()
        for background, interval in ((False, 10), (True, 0)):
            record = self.configure(record, interval=interval, background=background)
            with mock.patch.object(self.manager, 'start', side_effect=AssertionError('must not run')):
                self.tick(record, 100)
                self.tick(record, 10000)
        record = self.configure(record)
        record = self.manager.pause(record['spec']['id'], True, record['spec']['revision'])
        with mock.patch.object(self.manager, 'start', side_effect=AssertionError('must not run')):
            self.tick(record, 20000)
        self.manager.close()
        self.manager.schedule_pump()

    def provision(self, record, action, endpoint=None, **extra):
        p = self.manager.provision_preview(record['spec']['id'])
        return self.manager.provision_apply(record['spec']['id'], dict(revision=p['revision'], review=p['review'],
            action=action, endpoint=endpoint, confirm=True, **extra))

    def test_initial_missing_directories_need_confirmation_and_can_be_created(self):
        remote = self.base / 'missing-remote' / 'nested'
        self.root.rmdir()
        body = spec(str(self.root))
        body['targets'] = [dict(cluster='entropy', remote=str(remote))]
        record = self.manager.save(body)
        self.manager.profiles = lambda name: {'name': name}
        self.manager.remote = lambda profile, request: rpc(request, str(self.base / 'remote-state'))
        p = self.manager.provision_preview(record['spec']['id'])
        self.assertFalse(self.root.exists())
        self.assertFalse(remote.exists())
        with self.assertRaises(SyncError):
            self.manager.provision_apply(record['spec']['id'], dict(revision=p['revision'], review=p['review'], action='mkdir', endpoint='local'))
        self.provision(record, 'mkdir', 'local')
        self.provision(record, 'mkdir', record['spec']['targets'][0]['id'])
        self.assertTrue(self.root.is_dir())
        self.assertTrue(remote.is_dir())
        self.assertTrue(self.manager.provision_preview(record['spec']['id'])['needs_git_choice'])
        self.provision(record, 'private-git')
        self.assertFalse(self.manager.provision_preview(record['spec']['id'])['needs_git_choice'])
        self.assertFalse((self.root / '.git').exists())

    def test_moved_local_and_remote_are_inactive_and_never_recreated(self):
        record, roots = self.sync_group()
        self.manager.provision_preview(record['spec']['id'])
        self.root.rename(self.base / 'moved')
        self.manager.start(record['spec']['id'], dict(revision=record['spec']['revision']))
        done = self.wait_for_run()
        self.assertEqual(done['status']['state'], 'inactive', done['status'])
        with self.assertRaises(SyncError):
            self.provision(done, 'mkdir', 'local')
        self.assertFalse(self.root.exists())
        self.root.mkdir()
        p = self.manager.provision_preview(record['spec']['id'])
        self.assertIn('local', p['inactive'])
        self.root.rmdir()
        (self.base / 'moved').rename(self.root)
        roots[0].rename(self.base / 'moved-remote')
        p = self.manager.provision_preview(record['spec']['id'])
        self.assertIn(record['spec']['targets'][0]['id'], p['inactive'])
        with self.assertRaises(SyncError):
            self.provision(done, 'mkdir', record['spec']['targets'][0]['id'])
        self.assertFalse(roots[0].exists())

    def test_stale_missing_parent_and_symlink_are_not_followed(self):
        parent = self.base / 'initial'
        parent.mkdir()
        missing = parent / 'missing'
        p = root_probe(str(missing))
        parent.rename(self.base / 'renamed')
        parent.symlink_to(self.base / 'renamed', target_is_directory=True)
        with self.assertRaises(SyncError):
            bootstrap(str(missing), 'mkdir', p['review'])
        self.assertFalse((self.base / 'renamed' / 'missing').exists())

    def test_git_initialization_and_ignore_confirmation_preserve_existing_files(self):
        record, roots = self.sync_group()
        for endpoint in ['local'] + [t['id'] for t in record['spec']['targets']]:
            self.provision(record, 'init', endpoint)
        self.assertTrue((self.root / '.git').is_dir())
        self.assertTrue(all((r / '.git').is_dir() for r in roots))
        self.assertEqual((self.root / 'local.txt').read_text(), 'local\n')
        store = GitStore(str(self.root), str(self.base / 'check'), 's-check')
        self.assertEqual(store.text('symbolic-ref', 'HEAD'), 'refs/heads/main')
        ignored = self.manager.provision_ignore(record['spec']['id'], 'local')
        (self.root / '.gitignore').write_text('human-change\n')
        with self.assertRaises(SyncError):
            self.provision(record, 'ignore-apply', 'local', mode='gitignore', ignore_review=ignored['review'])
        self.assertEqual((self.root / '.gitignore').read_text(), 'human-change\n')

    def test_upload_rejects_file_directory_collision_without_writing_outputs(self):
        record, roots = self.sync_group()
        (roots[0] / 'local.txt').mkdir()
        (roots[0] / 'local.txt' / 'result').write_text('keep\n')
        record = self.configure(record)
        held = self.review_sync(record)
        self.assertEqual(held['status']['state'], 'path-collision', held['status'])
        self.assertEqual((roots[0] / 'local.txt' / 'result').read_text(), 'keep\n')
        self.assertEqual((self.root / 'local.txt').read_text(), 'local\n')

    def test_background_enable_defaults_to_uploads_and_maintenance_stops_admission(self):
        body = spec(str(self.root))
        body['policy'] = {'background': True}
        value = validate(body)
        self.assertEqual(value['direction'], 'push')
        self.assertEqual(value['policy']['on_change'], 'auto-push')
        record, _ = self.sync_group()
        with self.manager.maintenance():
            with self.assertRaises(SyncError):
                self.manager.start(record['spec']['id'], {'revision': record['spec']['revision']})
            self.assertTrue(self.manager.scheduling_held)
        self.assertFalse(self.manager.scheduling_held)

    def test_upload_lost_acknowledgement_retries_idempotently(self):
        record, roots = self.sync_group()
        record = self.configure(record)
        reviewed = self.review_sync(record)
        original = self.manager.remote
        lost = []
        def remote(profile, request):
            result = original(profile, request)
            if request['operation'] == 'upload-apply' and not lost:
                lost.append(True)
                raise SyncError(409, 'lost reply fixture', state='login-needed')
            return result
        self.manager.remote = remote
        partial = self.apply_sync(reviewed)
        self.assertEqual(partial['status']['state'], 'partial', partial['status'])
        self.assertTrue(partial['status']['unfinished_apply'])
        self.manager.remote = original
        recovered = self.apply_sync(partial)
        self.assertEqual(recovered['status']['state'], 'uploaded', recovered['status'])
        self.assertFalse(recovered['status']['unfinished_apply'])
        self.assertTrue(all((r / 'local.txt').read_text() == 'local\n' for r in roots))

    def test_later_local_edit_invalidates_upload_review_before_remote_writes(self):
        record, roots = self.sync_group()
        record = self.configure(record)
        reviewed = self.review_sync(record)
        (self.root / 'local.txt').write_text('later edit\n')
        stale = self.apply_sync(reviewed)
        self.assertEqual(stale['status']['state'], 'stale-plan', stale['status'])
        self.assertTrue(all(not (r / 'local.txt').exists() for r in roots))

    def test_unrelated_edit_preserves_existing_target_identity_and_missing_initial_local_works_offline(self):
        record, roots = self.sync_group()
        self.manager.provision_preview(record['spec']['id'])
        body = copy.deepcopy(record['spec'])
        body['targets'].append({'cluster': 'entropy', 'remote': str(self.base / 'another')})
        record = self.manager.save(body, body['id'])
        roots[0].rename(self.base / 'remote-moved')
        preview = self.manager.provision_preview(record['spec']['id'])
        self.assertIn(record['spec']['targets'][0]['id'], preview['inactive'])

    def test_pinned_legacy_private_history_is_not_replaced_by_git_initialization(self):
        record, _ = self.sync_group()
        done = self.apply_sync(self.review_sync(record))
        with self.assertRaises(SyncError):
            self.provision(done, 'init', 'local')
        self.assertFalse((self.root / '.git').exists())

    def test_upload_retains_source_before_receipt_and_recovers_receipt_write_failure(self):
        import sync_git
        record, roots = self.sync_group()
        record = self.configure(record)
        reviewed = self.review_sync(record)
        original = sync_git.write_json
        failed = []
        def write(path, value, **options):
            if str(path).endswith('/upload.json') and not failed:
                failed.append(True)
                raise OSError('receipt write fault fixture')
            return original(path, value, **options)
        with mock.patch.object(sync_git, 'write_json', side_effect=write):
            interrupted = self.apply_sync(reviewed)
        self.assertTrue(interrupted['status']['unfinished_apply'])
        recovered = self.apply_sync(interrupted)
        self.assertEqual(recovered['status']['state'], 'uploaded', recovered['status'])
        self.assertFalse(recovered['status']['unfinished_apply'])
        self.assertTrue(all((r / 'local.txt').read_text() == 'local\n' for r in roots))

    def test_initial_offline_plain_folder_does_not_silently_start_private_git_history(self):
        body = spec(str(self.root))
        body['targets'] = [{'cluster': 'entropy', 'remote': str(self.base / 'offline-remote')}]
        record = self.manager.save(body)
        record = self.apply(record, 'sync-only')['sync']
        self.manager.profiles = lambda name: {'name': name}
        def offline(profile, request):
            raise SyncError(409, 'offline fixture', state='login-needed')
        self.manager.remote = offline
        self.manager.start(record['spec']['id'], {'revision': record['spec']['revision']}, intent='sync')
        done = self.wait_for_run()
        self.assertEqual(done['status']['state'], 'login-needed', done['status'])
        self.assertFalse((self.state / 'git' / record['spec']['id']).exists())

    def test_slow_groups_do_not_starve_later_registrations(self):
        records = []
        for i in range(3):
            root = self.base / ('fair-%d' % i)
            root.mkdir()
            body = spec(str(root))
            body.update(direction='push', interval=10, policy={'background': True, 'on_change': 'auto-push'})
            body['targets'] = [{'cluster': 'entropy', 'remote': str(self.base / ('remote-fair-%d' % i))}]
            records.append(self.manager.save(body))
        admitted = []
        def start(sync_id, body, **options):
            admitted.append(sync_id)
            self.manager.active[sync_id] = 'fixture-running'
        with mock.patch.object(self.manager, 'start', side_effect=start):
            self.manager.clock = lambda: 100
            self.manager.schedule_pump()
            self.manager.clock = lambda: 111
            self.manager.schedule_pump()
            self.assertEqual(admitted, [r['spec']['id'] for r in records[:2]])
            self.manager.active.clear()
            self.manager.clock = lambda: 200
            self.manager.schedule_pump()
            self.assertEqual(admitted[2], records[2]['spec']['id'])
            self.manager.active.clear()

    def test_upload_checks_and_manual_snapshots_never_fetch_cluster_only_outputs(self):
        record, roots = self.sync_group()
        record = self.configure(record, background=False)
        for root in roots:
            (root / 'large-cluster-output.bin').write_bytes(b'x' * 2000000)
        original = self.manager.transfer
        def uploads_only(profile, local, request):
            self.assertEqual(request['operation'], 'push-objects')
            return original(profile, local, request)
        self.manager.transfer = uploads_only
        self.manager.start(record['spec']['id'], {'revision': record['spec']['revision']}, preview=True)
        checked = self.wait_for_run()
        self.assertEqual(checked['status']['state'], 'preview-ready', checked['status'])
        self.assertTrue(all(t['snapshot']['summary']['eligible_files'] == 1 for t in checked['status']['targets']))
        self.manager.start(record['spec']['id'], {'revision': record['spec']['revision']})
        reviewed = self.wait_for_run()
        self.assertEqual(reviewed['status']['state'], 'needs-sync-review', reviewed['status'])
        self.assertFalse((self.root / 'large-cluster-output.bin').exists())
        self.assertTrue(all(not (r / 'local.txt').exists() for r in roots))

    def test_upload_preview_volume_approval_uses_the_same_managed_scope(self):
        record, roots = self.sync_group()
        record = self.configure(record, background=False)
        done = self.apply_sync(self.review_sync(record))
        body = copy.deepcopy(done['spec'])
        body['git']['warn_file_mb'] = 0.001
        record = self.manager.save(body, body['id'])
        (roots[0] / 'local.txt').write_bytes(b'x' * 2000)
        self.manager.start(record['spec']['id'], {'revision': record['spec']['revision']}, preview=True)
        warning = self.wait_for_run()
        self.assertEqual(warning['status']['state'], 'needs-volume-review', warning['status'])
        self.manager.resolve(record['spec']['id'], {'revision': record['spec']['revision'], 'plan': warning['status']['plan'],
            'action': 'approve-snapshot', 'confirm': True})
        reviewed = self.wait_for_run()
        self.assertEqual(reviewed['status']['state'], 'needs-sync-review', reviewed['status'])
        self.assertEqual((self.root / 'local.txt').read_text(), 'local\n')
