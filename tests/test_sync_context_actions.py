"""Permanent sync actions through the actual REST/CLI boundary in scoped fixtures."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from test_rest_jobs import JobServerTestCase
import action_paths
import rest_actions
from rest_client import RESTClient
from sync_spec import METADATA_FILE

ROOT = Path(__file__).resolve().parents[1]


class TestSyncContextActions(JobServerTestCase):
    def setUp(self):
        super().setUp()
        scope = {'roots': [str(self.llm_root)], 'deny': [str(self.data)]}
        try:
            parent, name, fd = action_paths.walk_open(str(self.llm_root), scope)
        except action_paths.Refused as e:
            if 'Operation not permitted' in str(e):
                self.skipTest('host disallows scoped ancestor descriptors; this gate runs on Entropy')
            raise
        else:
            os.close(parent)
            os.close(fd)
        self.server.actions = rest_actions.TunnelActions({'data-transfer': {'enabled': True,
            'actions': {'sync_setup': True, 'sync_settings': True}}}, dirs=[str(ROOT / 'hpclib/tunnels')], data_dir=str(self.data))
        self.builder = RESTClient(self.url, token=self.builder_token)
        self.code = self.llm_root / 'sync_code'
        self.code.mkdir()
        (self.code / '.gitignore').write_text('# human rule\n*.tmp\n')

    def call(self, action, **params):
        return self.builder.run_action('data-transfer', action, {'root': str(self.code), **params})['result']

    def test_inspect_and_preview_work_without_smb_login_and_do_not_write_root(self):
        before = {p.name: p.read_bytes() for p in self.code.iterdir()}
        out = self.call('sync_inspect')
        self.assertTrue(out['needs_setup'])
        self.assertFalse(out['transfers_enabled'])
        self.assertEqual(self.call('sync_setup_preview')['root'], str(self.code))
        self.assertEqual({p.name: p.read_bytes() for p in self.code.iterdir()}, before)
        # The REST boundary removes synchronous scope files even when a helper has no queued job.
        scope_dir = self.data / 'sessions/actions'
        self.assertEqual(list(scope_dir.glob('scope-*.json')), [])

    def test_reviewed_setup_and_threshold_update_preserve_user_rules(self):
        preview = self.call('sync_setup_preview')
        self.expect_error(502, self.builder.run_action, 'data-transfer', 'sync_setup',
                          {'root': str(self.code), 'review': preview['review'], 'mode': 'sync-only'})
        configured = self.call('sync_setup', review=preview['review'], mode='sync-only', confirm=True)
        self.assertEqual(configured['metadata']['git']['default_excludes'], 'sync-only')
        self.assertEqual((self.code / '.gitignore').read_text(), '# human rule\n*.tmp\n')
        updated = self.call('sync_settings', review=configured['review'], confirm=True,
                            warn_file_mb=0.5, warn_snapshot_mb=77.25, warn_file_count=9)
        self.assertEqual(updated['metadata']['git']['warn_file_mb'], 0.5)
        self.assertEqual(updated['metadata']['git']['warn_snapshot_mb'], 77.25)
        self.assertEqual(updated['metadata']['git']['warn_file_count'], 9)
        self.assertEqual(updated['metadata']['git']['default_excludes'], 'sync-only')
        self.expect_error(502, self.builder.run_action, 'data-transfer', 'sync_settings',
                          {'root': str(self.code), 'review': configured['review'], 'confirm': True, 'warn_file_count': 12})
        self.assertEqual(json.loads((self.code / METADATA_FILE).read_text())['git']['warn_file_count'], 9)

    def test_reader_mutations_disabled_server_and_scope_escape_are_refused(self):
        preview = self.call('sync_setup_preview')
        args = {'root': str(self.code), 'review': preview['review'], 'mode': 'off', 'confirm': True}
        self.expect_error(403, self.llm.run_action, 'data-transfer', 'sync_setup', args)
        self.server.disable_file_changes = True
        self.expect_error(403, self.builder.run_action, 'data-transfer', 'sync_setup', args)
        self.server.disable_file_changes = False
        self.expect_error(403, self.builder.run_action, 'data-transfer', 'sync_inspect', {'root': str(self.outside)})
        self.assertFalse((self.code / METADATA_FILE).exists())

    def test_symlinked_control_files_are_never_followed(self):
        outside = self.outside / 'metadata.json'
        outside.write_text('protected text')
        (self.code / METADATA_FILE).symlink_to(outside)
        self.expect_error(502, self.builder.run_action, 'data-transfer', 'sync_setup_preview', {'root': str(self.code)})
        self.assertEqual(outside.read_text(), 'protected text')

    def test_availability_is_per_action_and_mutation_options_are_typed(self):
        def runner(argv, cwd, timeout):
            if 'capabilities' in argv:
                self.assertIn(str(ROOT / 'hpclib/servers/action_paths.py'), argv)
                return 0, '{"ready":true}', ''
            return 0, 'HPCLIB_SMB_STATUS {"host":"server","image":true,"ticket":null}', ''
        self.server.actions.runner = runner
        tunnel = next(x for x in self.builder.actions(refresh=True)['tunnels'] if x['tunnel'] == 'data-transfer')
        states = {a['name']: a['availability']['available'] for a in tunnel['actions']}
        self.assertTrue(states['sync_inspect'])
        self.assertTrue(tunnel['availability']['available'])
        self.assertFalse(states['list'])
        for value in (True, '3.5', -1, float('inf')):
            self.expect_error(422, self.builder.run_action, 'data-transfer', 'sync_settings',
                              {'root': str(self.code), 'review': 'a' * 64, 'confirm': True, 'warn_file_mb': value})


if __name__ == '__main__':
    unittest.main()
