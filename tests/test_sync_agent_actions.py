"""Agent consent, scope and command dispatch through the console's actual Git sync manager."""
import copy
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hpclib/servers'))
from sync_git import rpc
from sync_manager import SyncManager
from sync_spec import METADATA_FILE, SyncError
from sync_context import agent_root_checkpoint
from test_sync_manager import write_private, native_fixture
from test_sync_spec import spec


class TestSyncAgentActions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hpclib-agent-sync-', dir=os.environ.get('HPCLIB_SYNC_FIXTURE_ROOT'))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        env = mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
                                          'XDG_CONFIG_HOME': str(self.base / 'xdg')})
        env.start()
        self.addCleanup(env.stop)
        self.local, self.remote = self.base / 'local', self.base / 'remote'
        metadata = json.dumps({'v': 1, 'git': {'default_excludes': 'sync-only', 'default_excludes_version': 1}})
        for root in (self.local, self.remote):
            root.mkdir()
            (root / METADATA_FILE).write_text(metadata)
        (self.local / 'local.txt').write_text('local content\n')
        (self.remote / 'remote.txt').write_text('remote content\n')
        self.manager = SyncManager(str(self.base / 'console'), lambda name: {'name': name}, write_private,
            lambda profile, request: rpc(request, str(self.base / 'remote-state')), native_fixture(self.base / 'remote-state'))
        self.addCleanup(self.manager.close)
        body = spec(str(self.local))
        body['targets'] = [{'cluster': 'entropy', 'remote': str(self.remote)}]
        self.record = self.manager.save(body)
        setup = self.manager.provision_preview(self.record['spec']['id'])
        self.manager.provision_apply(self.record['spec']['id'], dict(revision=setup['revision'], review=setup['review'], action='private-git', confirm=True))
        self.sync_id, self.revision = self.record['spec']['id'], self.record['spec']['revision']
        self.profile = {'name': 'entropy'}

    def allow(self, value=True):
        return self.manager.agent_access(self.sync_id, {'revision': self.revision, 'allow': value, 'confirm': True})

    def request(self, operation, **args):
        values = {'id': self.sync_id}
        if operation not in ('list', 'status', 'plan'):
            values.update(revision=self.revision, confirm=True)
        values.update(args)
        return {'v': 1, 'ticket': 'a-' + secrets.token_hex(16), 'actor': 'fixture-agent', 'operation': operation,
                'root': str(self.remote), 'identity': [str(self.remote), self.remote.stat().st_dev, self.remote.stat().st_ino],
                'scope': {'roots': [str(self.base)], 'deny': [], 'actor': 'fixture-agent', 'file_changes': True},
                'args': values, 'created': time.time(), 'expires': time.time() + 300, '_deadline': time.monotonic() + 300}

    def dispatch(self, operation, **args):
        return self.manager._agent_dispatch(self.profile, self.request(operation, **args))

    def finished(self):
        deadline = time.monotonic() + 40
        while self.manager.busy() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(self.manager.busy())
        return self.manager.get(self.sync_id)

    def test_consent_is_explicit_and_reversible_without_changing_sync_revision(self):
        with self.assertRaises(SyncError):
            self.manager.agent_access(self.sync_id, {'revision': self.revision, 'allow': True})
        with self.assertRaises(SyncError):
            self.dispatch('status')
        self.assertTrue(self.allow()['agent_control'])
        self.assertEqual(self.dispatch('status')['spec']['revision'], self.revision)
        self.assertEqual(self.dispatch('list')['syncs'][0]['id'], self.sync_id)
        self.allow(False)
        self.assertEqual(self.manager.get(self.sync_id)['spec']['revision'], self.revision)
        with self.assertRaises(SyncError):
            self.dispatch('status')

    def test_cross_cluster_out_of_scope_and_missing_confirm_are_refused(self):
        self.allow()
        for change in ('scope', 'actor', 'profile', 'confirm', 'expires'):
            with self.subTest(change=change):
                request = self.request('review')
                profile = self.profile
                if change == 'scope': request['scope']['roots'] = [str(self.local)]
                if change == 'actor': request['actor'] = 'forged-actor'
                if change == 'profile': profile = {'name': 'other-cluster'}
                if change == 'confirm': request['args']['confirm'] = False
                if change == 'expires': request['_deadline'] = time.monotonic() - 1
                with self.assertRaises(SyncError):
                    self.manager._agent_dispatch(profile, request)
        self.assertFalse((self.local / 'remote.txt').exists())

    def test_review_plan_apply_uses_real_manager_and_preserves_consent(self):
        self.allow()
        self.dispatch('review')
        review = self.finished()
        self.assertEqual(review['status']['state'], 'needs-sync-review', review['status'])
        self.assertFalse((self.local / 'remote.txt').exists())
        plan_id = review['status']['plan']
        self.assertEqual(self.dispatch('plan', plan=plan_id)['state'], 'needs-sync-review')
        self.dispatch('apply', plan=plan_id)
        done = self.finished()
        self.assertEqual(done['status']['state'], 'in-sync', done['status'])
        self.assertEqual((self.local / 'remote.txt').read_text(), 'remote content\n')
        self.assertEqual((self.remote / 'local.txt').read_text(), 'local content\n')
        self.assertTrue(done['agent_control'])

    def test_volume_approval_is_separate_and_continues_review(self):
        self.allow()
        configured = self.dispatch('configure', settings={'warn_file_count': 1})
        self.revision = configured['spec']['revision']
        self.dispatch('review')
        record = self.finished()
        for _ in range(3):
            if record['status']['state'] != 'needs-volume-review': break
            self.dispatch('approve-snapshot', plan=record['status']['plan'])
            record = self.finished()
        self.assertEqual(record['status']['state'], 'needs-sync-review', record['status'])
        self.assertFalse((self.local / 'remote.txt').exists())

    def test_receipt_deduplicates_a_mutation_and_rejects_ticket_rebinding(self):
        self.allow()
        request = self.request('pause')
        first = self.manager._agent_once(self.profile, request)
        self.assertTrue(first['ok'], first)
        self.assertEqual(self.manager._agent_once(self.profile, request), first)
        self.assertEqual(self.manager.get(self.sync_id)['spec']['revision'], self.revision + 1)
        changed = copy.deepcopy(request)
        changed['operation'] = 'resume'
        self.assertFalse(self.manager._agent_once(self.profile, changed)['ok'])

    def test_crash_after_dispatch_cannot_replay_the_mutation(self):
        self.allow()
        request = self.request('pause')
        original = self.manager._agent_dispatch
        def crash(profile, item):
            original(profile, item)
            raise SystemExit('injected console stop')
        with mock.patch.object(self.manager, '_agent_dispatch', side_effect=crash), self.assertRaises(SystemExit):
            self.manager._agent_once(self.profile, request)
        reply = self.manager._agent_once(self.profile, request)
        self.assertFalse(reply['ok'])
        self.assertIn('inspect sync status', reply['error'])
        self.assertEqual(self.manager.get(self.sync_id)['spec']['revision'], self.revision + 1)

    def test_revoke_blocks_agent_apply_but_owner_can_apply_same_plan(self):
        self.allow()
        self.dispatch('review')
        record = self.finished()
        self.allow(False)
        with self.assertRaises(SyncError): self.dispatch('apply', plan=record['status']['plan'])
        self.manager.resolve(self.sync_id, {'revision': self.revision, 'plan': record['status']['plan'],
                                         'action': 'apply-sync', 'confirm': True})
        self.assertEqual(self.finished()['status']['state'], 'in-sync')

    def test_local_or_target_path_changes_clear_consent_and_settings_preserve_it(self):
        self.allow()
        record = self.dispatch('configure', settings={'warn_snapshot_mb': 9})
        self.assertTrue(record['agent_control'])
        body = copy.deepcopy(record['spec'])
        new = self.base / 'another-local'
        new.mkdir()
        body['local'] = str(new)
        self.assertFalse(self.manager.save(body, self.sync_id)['agent_control'])

    def test_background_bridge_dispatches_and_stops_with_console(self):
        self.allow()
        request = self.request('pause')
        request['valid_for'] = 300
        sent, replied = [], threading.Event()
        def remote(profile, item):
            if item['operation'] == 'agent-poll':
                if sent: return {'requests': []}
                sent.append(item)
                return {'requests': [request]}
            self.assertTrue(item['reply']['ok'], item)
            replied.set()
            return {'state': 'succeeded'}
        self.manager.remote = remote
        self.manager.start_agent_bridge()
        self.assertTrue(replied.wait(8))
        self.assertTrue(self.manager.get(self.sync_id)['spec']['paused'])
        self.manager.close()
        self.assertFalse(self.manager.agent_thread.is_alive())

    def test_root_checkpoint_survives_node_local_device_numbers(self):
        first = SimpleNamespace(st_dev=49, st_ino=22072331199, st_ctime_ns=1794500000123456789)
        second = SimpleNamespace(st_dev=50, st_ino=first.st_ino, st_ctime_ns=first.st_ctime_ns)
        with mock.patch('sync_context.os.fstat', return_value=first):
            original = agent_root_checkpoint('/shared/root', 1)
        with mock.patch('sync_context.os.fstat', return_value=second):
            self.assertEqual(original, agent_root_checkpoint('/shared/root', 1))
        second.st_ctime_ns += 1
        with mock.patch('sync_context.os.fstat', return_value=second):
            self.assertNotEqual(original, agent_root_checkpoint('/shared/root', 1))
        second.st_ctime_ns = first.st_ctime_ns
        second.st_ino += 1
        with mock.patch('sync_context.os.fstat', return_value=second):
            self.assertNotEqual(original, agent_root_checkpoint('/shared/root', 1))
