"""Scoped REST -> action CLI -> running console manager -> real Git sync, in disposable roots."""
import json
import os
from pathlib import Path
import sys
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hpclib/servers'))
from test_rest_jobs import JobServerTestCase
from test_sync_manager import write_private, native_fixture
from test_sync_spec import spec
import action_paths
import rest_actions
from rest_client import RESTClient
from sync_context import agent_read, agent_write, agent_directory
from sync_git import rpc
from sync_manager import SyncManager
from sync_spec import SyncError, METADATA_FILE

ROOT = Path(__file__).resolve().parents[1]


class TestSyncManagementActions(JobServerTestCase):
    def setUp(self):
        super().setUp()
        isolated = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.tmp / 'xdg'),
            'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'})
        isolated.start()
        self.addCleanup(isolated.stop)
        try:
            parent, name, fd = action_paths.walk_open(str(self.llm_root), {'roots': [str(self.llm_root)], 'deny': []})
        except action_paths.Refused as error:
            if 'Operation not permitted' in str(error): self.skipTest('scoped descriptor gate requires native Linux')
            raise
        os.close(parent); os.close(fd)
        self.server.actions = rest_actions.TunnelActions({'data-transfer': {'enabled': True,
            'actions': {'sync_query': True, 'sync_manage': True}}}, dirs=[str(ROOT / 'hpclib/tunnels')], data_dir=str(self.data))
        self.builder = RESTClient(self.url, token=self.builder_token)
        self.local, self.remote = self.tmp / 'local', self.llm_root / 'remote'
        metadata = json.dumps({'v': 1, 'git': {'default_excludes': 'sync-only', 'default_excludes_version': 1}})
        for root in (self.local, self.remote):
            root.mkdir()
            (root / METADATA_FILE).write_text(metadata)
        (self.local / 'local.txt').write_text('local\n')
        (self.remote / 'remote.txt').write_text('remote\n')
        self.manager = SyncManager(str(self.tmp / 'console'), lambda name: {'name': name}, write_private,
                                  lambda profile, request: rpc(request, str(self.data)), native_fixture(self.data))
        self.addCleanup(self.manager.close)
        body = spec(str(self.local))
        body['targets'] = [{'cluster': 'entropy', 'remote': str(self.remote)}]
        record = self.manager.save(body)
        setup = self.manager.provision_preview(record['spec']['id'])
        self.manager.provision_apply(record['spec']['id'], dict(revision=setup['revision'], review=setup['review'], action='private-git', confirm=True))
        self.sync_id, self.revision = record['spec']['id'], record['spec']['revision']
        self.manager.agent_access(self.sync_id, {'revision': self.revision, 'allow': True, 'confirm': True})
        self.manager.agent_pump()  # Advertise the live, consent-enabled owner connection.

    def enqueue(self, operation, mutates=False, **args):
        params = {'root': str(self.remote), 'operation': operation, 'id': self.sync_id}
        if mutates: params.update(revision=self.revision, confirm=True)
        params.update(args)
        return self.builder.run_action('data-transfer', 'sync_manage' if mutates else 'sync_query', params)['result']

    def ticket(self, item, client=None):
        return (client or self.builder).run_action('data-transfer', 'sync_query', {
            'root': str(self.llm_root), 'operation': 'request-status', 'ticket': item['ticket']})['result']

    def command(self, operation, mutates=False, **args):
        item = self.enqueue(operation, mutates, **args)
        self.manager.agent_pump()
        result = self.ticket(item)
        self.assertEqual(result['state'], 'succeeded', result)
        return result['reply']['result']

    def finished(self):
        deadline = time.monotonic() + 40
        while self.manager.busy() and time.monotonic() < deadline: time.sleep(.02)
        self.assertFalse(self.manager.busy())
        return self.manager.get(self.sync_id)

    def test_actions_list_review_plan_and_apply_real_files(self):
        listed = self.command('list')
        self.assertEqual([r['id'] for r in listed['syncs']], [self.sync_id])
        self.command('review', True)
        reviewed = self.finished()
        self.assertEqual(reviewed['status']['state'], 'needs-sync-review', reviewed['status'])
        plan = reviewed['status']['plan']
        self.assertFalse((self.local / 'remote.txt').exists())
        self.assertEqual(self.command('plan', plan=plan)['candidate'], reviewed['status']['sync_review']['candidate'])
        self.command('apply', True, plan=plan)
        self.assertEqual(self.finished()['status']['state'], 'in-sync')
        self.assertEqual((self.local / 'remote.txt').read_text(), 'remote\n')
        self.assertEqual((self.remote / 'local.txt').read_text(), 'local\n')
        self.assertEqual(self.command('status')['status']['state'], 'in-sync')
        self.assertEqual(list((self.data / 'sessions/actions').glob('scope-*.json')), [])

    def test_disabled_action_read_token_and_no_confirm_cannot_enqueue_mutation(self):
        params = {'root': str(self.remote), 'id': self.sync_id, 'revision': self.revision, 'operation': 'review', 'confirm': True}
        self.expect_error(403, self.llm.run_action, 'data-transfer', 'sync_manage', params)
        self.server.disable_file_changes = True
        self.expect_error(403, self.builder.run_action, 'data-transfer', 'sync_manage', params)
        self.server.disable_file_changes = False
        self.expect_error(502, self.builder.run_action, 'data-transfer', 'sync_manage', dict(params, confirm=False))
        self.server.actions.config['data-transfer']['actions']['sync_manage'] = False
        self.expect_error(403, self.builder.run_action, 'data-transfer', 'sync_manage', params)
        self.assertFalse((self.local / 'remote.txt').exists())
        self.assertEqual(list(Path(agent_directory(str(self.data))).glob('a-*.json')), [])

    def test_ticket_is_actor_scoped_and_expired_requests_do_not_execute(self):
        item = self.enqueue('review', True)
        self.expect_error(502, self.ticket, item, self.llm)
        directory = agent_directory(str(self.data))
        record = agent_read(os.path.join(directory, item['ticket'] + '.json'))
        record['expires'] = time.time() - 1
        agent_write(directory, item['ticket'] + '.json', record)
        self.manager.agent_pump()
        self.assertEqual(self.ticket(item)['state'], 'expired')
        self.assertFalse(self.manager.busy())
        self.assertFalse((self.local / 'remote.txt').exists())

    def test_lost_reply_is_retried_from_receipt_without_repeating_review(self):
        item = self.enqueue('review', True)
        original = self.manager.remote
        def lost(profile, request):
            if request['operation'] == 'agent-reply': raise SyncError(409, 'injected lost reply')
            return original(profile, request)
        self.manager.remote = lost
        self.manager.agent_pump()
        first = self.finished()
        self.assertEqual(self.ticket(item)['state'], 'claimed')
        directory = agent_directory(str(self.data))
        record = agent_read(os.path.join(directory, item['ticket'] + '.json'))
        record['lease_until'] = time.time() - 1
        agent_write(directory, item['ticket'] + '.json', record)
        self.manager.remote = original
        self.manager.agent_retry.clear()
        self.manager.agent_pump()
        self.assertEqual(self.ticket(item)['state'], 'succeeded')
        self.assertEqual(self.manager.get(self.sync_id)['status']['run'], first['status']['run'])

    def test_root_identity_swap_blocks_before_console_dispatch(self):
        item = self.enqueue('review', True)
        self.remote.rename(self.llm_root / 'previous-remote')
        self.remote.mkdir()
        self.manager.agent_pump()
        result = self.ticket(item)
        self.assertEqual(result['state'], 'failed', result)
        self.assertIn('directory changed', result['reply']['error'])
        self.assertFalse(self.manager.busy())

    def test_ticket_expiry_is_reported_even_without_owner_polling(self):
        item = self.enqueue('review', True)
        directory = agent_directory(str(self.data))
        record = agent_read(os.path.join(directory, item['ticket'] + '.json'))
        record['expires'] = time.time() - 1
        agent_write(directory, item['ticket'] + '.json', record)
        self.assertEqual(self.ticket(item)['state'], 'expired')
        record.update(state='claimed', consumer='c-previous', lease_until=time.time() - 1)
        agent_write(directory, item['ticket'] + '.json', record)
        result = self.ticket(item)
        self.assertEqual(result['state'], 'uncertain')
        self.assertIn('inspect sync status', result['reply']['error'])

    def test_directory_metadata_change_requires_fresh_request(self):
        item = self.enqueue('review', True)
        time.sleep(.01)
        (self.remote / 'added-while-queued.txt').write_text('preserve this new file\n')
        self.manager.agent_pump()
        result = self.ticket(item)
        self.assertEqual(result['state'], 'failed', result)
        self.assertIn('directory changed', result['reply']['error'])
        self.assertFalse(self.manager.busy())
        self.assertEqual((self.remote / 'added-while-queued.txt').read_text(), 'preserve this new file\n')

    def test_old_device_bound_ticket_requires_fresh_request(self):
        item = self.enqueue('review', True)
        directory = agent_directory(str(self.data))
        record = agent_read(os.path.join(directory, item['ticket'] + '.json'))
        info = self.remote.stat()
        record['identity'] = [str(self.remote), info.st_dev, info.st_ino]
        agent_write(directory, item['ticket'] + '.json', record)
        self.manager.agent_pump()
        result = self.ticket(item)
        self.assertEqual(result['state'], 'failed', result)
        self.assertIn('older checkpoint', result['reply']['error'])
        self.assertFalse(self.manager.busy())
