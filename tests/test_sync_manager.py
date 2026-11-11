"""Sync storage/setup: real files, explicit writes, stale previews, and preserved user work."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
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


class TestSyncManager(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hpclib-sync-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'code'
        self.root.mkdir()
        self.state = self.base / 'state'
        self.manager = SyncManager(str(self.state), None, write_private)

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
