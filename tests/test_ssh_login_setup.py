"""Workstation key bootstrap over the first login, using only isolated local homes."""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest import mock

from test_agent_console import ConsoleTestCase, agent_console, agent_profiles
from ssh_keys import SSHKeyStore


class TestSSHLoginSetup(ConsoleTestCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / 'workstation'
        self.remote = self.tmp / 'cluster home'
        self.remote.mkdir()
        patch = mock.patch.dict(os.environ, {'HOME': str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(self.clusters.logins, 'alive', return_value=False)
        self.alive = patch.start()
        self.addCleanup(patch.stop)
        self.store = SSHKeyStore(self.home)
        self.route = f'/api/clusters/{self.dead}/ssh/login-key'
        self.keys_route = f'/api/clusters/{self.dead}/ssh/login-keys'
        self.transfers = []

    def remote_script(self, profile, script, args=(), **options):
        self.transfers.append((script, args, options))
        # Run the actual shipped helper with a synthetic cluster HOME and the approved interpreter.
        script = script.replace('launcher=python3', 'launcher=' + shlex.quote(sys.executable))
        result = subprocess.run(['bash', '-s', '--', *args], input=script, text=True, capture_output=True,
                                env=dict(os.environ, HOME=str(self.remote),
                                         HPCTUNNELS_DATA_DIR=str(self.remote / 'not installed')), timeout=20)
        return result.returncode, result.stdout, result.stderr

    def test_requires_console_auth_first_login_and_site_permission_before_creating_keys(self):
        self.assertEqual(self.call('GET', self.keys_route, key=None)[0], 401)
        self.assertEqual(self.call('POST', self.route, {'key': 'laptop', 'create': True}, key=None)[0], 401)
        self.assertEqual(self.call('GET', self.keys_route)[1]['keys'], [])
        self.assertEqual(self.call('POST', self.route, {'key': 'laptop', 'create': True})[0], 409)
        self.assertFalse(self.home.exists())
        self.alive.return_value = True
        for body in ({'key': '../escape'}, {'key': '/private/key'}, {'key': 'laptop', 'private': 'secret'},
                     {'key': 'laptop', 'create': 'yes'}, {'create': True}):
            self.assertEqual(self.call('POST', self.route, body)[0], 422, body)
        profile = agent_profiles.load(self.dead)
        profile['preset'] = {'ssh_keys': 'site-managed'}
        agent_profiles.save(profile)
        self.assertEqual(self.call('POST', self.route, {'key': 'laptop', 'create': True})[0], 409)
        self.assertFalse(self.home.exists())

    def test_default_key_name_can_be_created_and_installed_for_an_address_profile(self):
        self.alive.return_value = True
        self.clusters.script_runner = self.remote_script
        self.assertIn('@', self.dead)
        status, info = self.call('GET', self.keys_route)
        self.assertEqual(status, 200, info)
        suggested = info['suggested_key']
        self.assertNotIn('@', suggested)
        status, result = self.call('POST', self.route, {'key': suggested, 'create': True})
        self.assertEqual(status, 200, result)
        self.assertTrue((self.store.root / suggested).is_file())
        self.assertEqual((self.remote / '.ssh' / 'authorized_keys').read_text(),
                         self.store.key(suggested)['public_key'] + '\n')

    def test_suggested_key_names_satisfy_filename_contract_at_boundaries(self):
        profile = agent_profiles.load(self.dead)
        prefix = 'id_ed25519_hpclib_'
        labels = ['maboyer@entropy.chem.tamu.edu', 'a name/with:punctuation+and☃',
                  'cluster.pub', '', '...', 'config', 'x' * 400,
                  'x' * (128 - len(prefix) - 4) + '.pub' + 'after truncation']
        for label in labels:
            with self.subTest(label=label):
                info = self.clusters.ssh_login_keys(dict(profile, name=label))
                name = info['suggested_key']
                self.assertEqual(SSHKeyStore.name(name), name)
                self.assertEqual(Path(name).name, name)
                self.assertLessEqual(len(name), 128)
                self.assertFalse(name.endswith('.pub'))
                self.assertNotIn('@', name)
        self.assertFalse(self.home.exists())  # suggestions do not create keys or directories

    def test_first_login_installs_only_public_key_without_rest_and_preserves_ssh_options(self):
        self.alive.return_value = True
        self.clusters.script_runner = self.remote_script
        agent_profiles.cmd_set(self.dead, 'login=-p', 'login+=2222', 'login+=-J', 'login+=jump.example',
                               'login+=-i', 'login+=/existing/key with spaces')
        self.store._check_root(create=True)
        (self.store.root / 'config').write_text('# existing SSH configuration\n')
        (self.store.root / 'authorized_keys').write_text('# local authorizations\n')
        with mock.patch.object(self.clusters.logins, 'disconnect') as disconnect:
            status, result = self.call('POST', self.route, {'key': 'laptop', 'create': True})
            self.assertEqual(status, 200, result)
            disconnect.assert_not_called()
        self.assertTrue(result['authorization_added'])
        self.assertIn('has not been tested', result['message'])
        key = self.store.key('laptop')
        public = key['public_key']
        private = (self.store.root / 'laptop').read_text()
        self.assertEqual((self.remote / '.ssh' / 'authorized_keys').read_text(), public + '\n')
        self.assertFalse((self.remote / '.ssh' / 'laptop').exists())
        self.assertEqual((self.store.root / 'config').read_text(), '# existing SSH configuration\n')
        self.assertEqual((self.store.root / 'authorized_keys').read_text(), '# local authorizations\n')
        saved = agent_profiles.load(self.dead)
        self.assertEqual(saved['login'], ['-i', str(self.store.root / 'laptop'), '-p', '2222', '-J',
                                         'jump.example', '-i', '/existing/key with spaces', saved['host']])
        self.assertEqual(saved['ssh_login_key']['fingerprint'], key['fingerprint'])
        self.assertEqual(self.transfers[0][1], [public])
        self.assertEqual(self.transfers[0][2], {'wait': 45, 'batch': True})
        self.assertNotIn(private, repr(self.transfers))
        self.assertNotIn('PRIVATE KEY', json.dumps(result))
        info = self.call('GET', self.keys_route)[1]
        self.assertEqual(info['configured']['name'], 'laptop')
        self.assertEqual(info['keys'][0]['public_key'], public)
        self.assertNotIn('PRIVATE KEY', json.dumps(info))
        # No duplicate authorization or -i on a retry, and restrictions are not relaxed.
        auth = self.remote / '.ssh' / 'authorized_keys'
        auth.write_text('# preserve me\nrestrict ' + public + '\n')
        status, repeat = self.call('POST', self.route, {'key': 'laptop'})
        self.assertEqual(status, 200, repeat)
        self.assertTrue(repeat['restrictions_preserved'])
        self.assertFalse(repeat['authorization_added'])
        self.assertEqual(auth.read_text(), '# preserve me\nrestrict ' + public + '\n')
        self.assertEqual(agent_profiles.load(self.dead)['login'], saved['login'])

    def test_failures_preserve_key_for_retry_and_do_not_save_unconfirmed_identity(self):
        self.alive.return_value = True
        original = agent_profiles.load(self.dead)
        self.clusters.script_runner = mock.Mock(return_value=(1, '{"error":"remote permission denied"}', ''))
        status, result = self.call('POST', self.route, {'key': 'laptop', 'create': True})
        self.assertEqual(status, 502, result)
        private = (self.store.root / 'laptop').read_bytes()
        self.assertEqual(agent_profiles.load(self.dead), original)
        self.assertEqual(self.call('POST', self.route, {'key': 'laptop', 'create': True})[0], 409)
        self.assertEqual((self.store.root / 'laptop').read_bytes(), private)
        self.clusters.script_runner = mock.Mock(return_value=(0, '[true]', ''))
        self.assertEqual(self.call('POST', self.route, {'key': 'laptop'})[0], 502)
        self.clusters.script_runner = self.remote_script
        self.assertEqual(self.call('POST', self.route, {'key': 'laptop'})[0], 200)
        self.assertEqual((self.store.root / 'laptop').read_bytes(), private)

    def test_local_profile_changes_and_concurrent_setups_are_not_overwritten(self):
        self.alive.return_value = True
        self.store.create('laptop')
        def editing_runner(profile, script, args, **kwargs):
            result = self.remote_script(profile, script, args, **kwargs)
            current = agent_profiles.load(self.dead)
            current['mcp_name'] = 'keep-concurrent-edit'
            agent_profiles.save(current)
            return result
        self.clusters.script_runner = editing_runner
        self.assertEqual(self.call('POST', self.route, {'key': 'laptop'})[0], 200)
        self.assertEqual(agent_profiles.load(self.dead)['mcp_name'], 'keep-concurrent-edit')
        self.clusters.ssh_login_lock.acquire()
        try:
            self.assertEqual(self.call('POST', self.route, {'key': 'another', 'create': True})[0], 409)
            self.assertFalse((self.store.root / 'another').exists())
        finally:
            self.clusters.ssh_login_lock.release()
        def changed_target(profile, script, args, **kwargs):
            result = self.remote_script(profile, script, args, **kwargs)
            current = agent_profiles.load(self.dead)
            current['host'] = 'different@example.edu'
            current.pop('ssh_login_key', None)
            agent_profiles.save(current)
            return result
        self.clusters.script_runner = changed_target
        self.assertEqual(self.call('POST', self.route, {'key': 'laptop'})[0], 409)
        saved = agent_profiles.load(self.dead)
        self.assertEqual(saved['host'], 'different@example.edu')
        self.assertNotIn('ssh_login_key', saved)

    def test_bootstrap_batch_mode_prevents_new_password_prompts_if_master_drops(self):
        profile = agent_profiles.load(self.dead)
        with mock.patch.object(agent_console.subprocess, 'run', return_value=subprocess.CompletedProcess([], 255, b'', b'login expired')) as run:
            self.clusters._login_script(profile, 'echo test', batch=True)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('pssh') + 1:argv.index('pssh') + 7],
                         ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-o', 'ConnectionAttempts=1'])
