"""Sync API against the real console HTTP handler with isolated local state."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hpclib' / 'servers'))
import agent_console
from test_sync_spec import spec


class Profiles:
    def profile(self, name):
        if name not in ('grace', 'entropy'):
            raise agent_console.ConsoleError(404, 'no cluster')
        return {'name': name, 'host': 'user@example.invalid'}


class TestSyncRoutes(unittest.TestCase):
    def test_agent_control_requires_local_confirmation_and_does_not_change_revision(self):
        _, saved = self.call('POST', body=spec(str(self.code)))
        path = '/' + saved['spec']['id'] + '/agent-control'
        self.assertFalse(saved['agent_control'])
        self.assertEqual(self.call('POST', path, {'revision': 1, 'allow': True})[0], 422)
        status, allowed = self.call('POST', path, {'revision': 1, 'allow': True, 'confirm': True})
        self.assertEqual(status, 200)
        self.assertTrue(allowed['agent_control'])
        self.assertEqual(allowed['spec']['revision'], 1)
        self.assertEqual(self.call('POST', path, {'revision': 99, 'allow': True, 'confirm': True})[0], 409)
        self.assertFalse(self.call('POST', path, {'revision': 1, 'allow': False, 'confirm': True})[1]['agent_control'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hpclib-sync-routes-', dir=os.environ.get('HPCLIB_SYNC_FIXTURE_ROOT'))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.code = self.base / 'code'
        self.code.mkdir()
        env = mock.patch.dict(os.environ, {'HPCLIB_CONSOLE_DIR': str(self.base / 'console'),
                                         'HPCLIB_AGENTS_DIR': str(self.base / 'agents')})
        env.start()
        self.addCleanup(env.stop)
        self.server = agent_console.ConsoleServer(('127.0.0.1', 0), 'sync-test-key', Profiles(),
                                                  allow_origin='http://127.0.0.1:5173')
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:%s' % self.server.port
        quiet = mock.patch.object(agent_console.ConsoleHandler, 'log_message', lambda *args: None)
        quiet.start()
        self.addCleanup(quiet.stop)

    def call(self, method, path='', body=None, key=True, headers=None, raw=None):
        h = {'Authorization': 'Bearer sync-test-key'} if key else {}
        if body is not None:
            raw = json.dumps(body).encode()
            h['Content-Type'] = 'application/json'
        h.update(headers or {})
        req = urllib.request.Request(self.url + '/api/sync' + path, data=raw, method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_auth_required_and_cached_get_has_no_state_writes(self):
        self.assertEqual(self.call('GET', key=False)[0], 401)
        status, out = self.call('GET')
        self.assertEqual(status, 200)
        self.assertEqual(out['syncs'], [])
        self.assertFalse((self.base / 'console').exists())
        self.assertFalse((self.base / 'agents').exists())

    def test_create_get_edit_pause_unregister(self):
        status, out = self.call('POST', body=spec(str(self.code)))
        self.assertEqual(status, 201)
        sync_id = out['spec']['id']
        self.assertEqual(self.call('GET', '/' + sync_id)[0], 200)
        edit = out['spec']
        edit['git']['warn_file_count'] = 21
        self.assertEqual(self.call('POST', '/' + sync_id, body=edit)[0], 200)
        self.assertEqual(self.call('POST', '/' + sync_id, body=edit)[0], 409)
        status, paused = self.call('POST', '/' + sync_id + '/pause', body={'revision': 2})
        self.assertEqual(status, 200)
        self.assertTrue(paused['spec']['paused'])
        self.assertEqual(self.call('DELETE', '/' + sync_id, body={'confirm': True, 'revision': 3})[0], 200)
        self.assertEqual(self.call('GET', '/' + sync_id)[0], 404)
        self.assertEqual(list(self.code.iterdir()), [])

    def test_mutation_content_type_origin_and_host(self):
        payload = spec(str(self.code))
        self.assertEqual(self.call('POST', body=payload, headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.call('POST', body=payload, headers={'Origin': 'http://evil.invalid'})[0], 403)
        self.assertEqual(self.call('POST', body=payload, headers={'Host': 'evil.invalid'})[0], 421)
        self.assertEqual(self.call('POST', body=payload, headers={'Origin': self.url})[0], 201)
        payload['local'] = str(self.base / 'other')
        payload['targets'][0]['remote'] = '/different/a'
        payload['targets'][1]['remote'] = '/different/b'
        self.assertEqual(self.call('POST', body=payload, headers={'Origin': 'http://127.0.0.1:5173'})[0], 201)

    def test_json_type_body_limit_and_unknown_profile(self):
        self.assertEqual(self.call('POST', raw=b'{', headers={'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(self.call('POST', raw=b'[]', headers={'Content-Type': 'application/json'})[0], 400)
        # The handler rejects the declared length before reading the body. Sending a whole MiB races
        # its deliberate close and makes urllib report BrokenPipe instead of the actual HTTP response.
        client = http.client.HTTPConnection('127.0.0.1', self.server.port)
        self.addCleanup(client.close)
        client.request('POST', '/api/sync', headers={'Authorization': 'Bearer sync-test-key',
                       'Content-Type': 'application/json', 'Content-Length': str((1 << 20) + 1)})
        self.assertEqual(client.getresponse().status, 413)
        bad = spec(str(self.code))
        bad['targets'][0]['cluster'] = 'missing'
        self.assertEqual(self.call('POST', body=bad)[0], 404)

    def test_explicit_setup_and_stale_review(self):
        _, saved = self.call('POST', body=spec(str(self.code)))
        path = '/' + saved['spec']['id']
        status, preview = self.call('GET', path + '/setup')
        self.assertEqual(status, 200)
        self.assertEqual(list(self.code.iterdir()), [])
        apply = {'revision': preview['revision'], 'setup': {'mode': 'off', 'review': preview['review'], 'confirm': True}}
        self.assertEqual(self.call('POST', path, body=apply)[0], 200)
        self.assertTrue((self.code / '.hpclib-sync.json').exists())
        self.assertEqual(self.call('POST', path, body=apply)[0], 409)

    def test_snapshot_and_review_routes_require_revision_setup_and_named_confirmation(self):
        _, saved = self.call('POST', body=spec(str(self.code)))
        path = '/' + saved['spec']['id']
        for action in ('run', 'dry-run', 'review'):
            with self.subTest(action=action):
                status, out = self.call('POST', path + '/' + action, body={})
                self.assertEqual(status, 422)
                self.assertEqual(self.call('POST', path + '/' + action, body={'revision': 1})[0], 202)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    result = self.call('GET', path + '/status')[1]
                    if not result['status'].get('running'):
                        break
                    time.sleep(0.02)
                self.assertEqual(result['status']['state'], 'needs-ignore-setup')
        self.assertEqual(self.call('POST', path + '/resolve', body={'action': 'apply'})[0], 422)
        self.assertEqual(self.call('POST', path + '/resolve', body={'action': 'apply-sync', 'revision': 1})[0], 422)
        self.assertEqual(list(self.code.iterdir()), [])


    def test_directory_setup_routes_require_confirmation_and_preserve_missing_roots(self):
        from sync_git import rpc
        remote = self.base.resolve() / 'new-remote'
        self.server.sync.remote = lambda profile, request: rpc(request, str(self.base / 'remote-state'))
        body = spec(str(self.code))
        body['targets'] = [{'cluster': 'entropy', 'remote': str(remote)}]
        status, record = self.call('POST', body=body)
        self.assertEqual(status, 201, record)
        path = '/' + record['spec']['id'] + '/provision'
        status, preview = self.call('GET', path)
        self.assertEqual(status, 200, preview)
        self.assertFalse(remote.exists())
        create = dict(revision=preview['revision'], review=preview['review'], endpoint=record['spec']['targets'][0]['id'], action='mkdir')
        self.assertEqual(self.call('POST', path, body=create)[0], 422)
        self.assertFalse(remote.exists())
        create['confirm'] = True
        status, preview = self.call('POST', path, body=create)
        self.assertEqual(status, 200, preview)
        self.assertTrue(remote.is_dir())
        self.assertTrue(preview['needs_git_choice'])
        choose = dict(revision=preview['revision'], review=preview['review'], action='private-git', confirm=True)
        self.assertEqual(self.call('POST', path, body=choose)[0], 200)
        self.assertFalse((self.code / '.git').exists())
        remote.rename(self.base.resolve() / 'moved-remote')
        status, preview = self.call('GET', path)
        self.assertEqual(status, 200, preview)
        self.assertTrue(preview['inactive'])
        create.update(review=preview['review'])
        self.assertEqual(self.call('POST', path, body=create)[0], 409)
        self.assertFalse(remote.exists())


    def test_restart_admission_cannot_release_an_existing_update_hold(self):
        def restart():
            request = urllib.request.Request(self.url + '/api/restart', data=b'{}', method='POST',
                headers={'Authorization': 'Bearer sync-test-key', 'Content-Type': 'application/json'})
            try:
                urllib.request.urlopen(request)
            except urllib.error.HTTPError as error:
                return error.code
            self.fail('fixture has no restart argv; must not exec')
        with self.server.sync.maintenance():
            self.assertEqual(restart(), 409)
            self.assertTrue(self.server.sync.scheduling_held)
        self.assertFalse(self.server.sync.scheduling_held)
        self.assertEqual(restart(), 409)
        self.assertFalse(self.server.sync.scheduling_held)


if __name__ == '__main__':
    unittest.main()
