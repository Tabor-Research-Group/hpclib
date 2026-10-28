"""Owner recovery against the native fake SSH and real loopback REST fixtures."""
import json
import stat
import time
import unittest
from unittest import mock

import test_examples as examples
from test_agent_console import ConsoleTestCase
import agent_profiles


class TestOwnerRecovery(unittest.TestCase):
    setUp = examples.TestSetupAgents.setUp
    bash = examples.TestSetupAgents.bash
    setup_agents = examples.TestSetupAgents.setup_agents
    identify = examples.TestSetupAgents.identify
    config = examples.TestSetupAgents.config
    profile = examples.TestSetupAgents.profile

    def initialize(self):
        self.setup_agents('--work-dir', str(self.work[0]), '--templates', 'hello', 'me@login.example')
        config = self.config()
        config['tunnel_actions'] = {'cmccdb-manage': {'enabled': True}}
        (self.rest / 'config.json').write_text(json.dumps(config))
        (self.rest / 'templates/hello/template.json').write_text('{"custom": true}')
        return {p: p.read_bytes() for p in (
            self.rest / 'config.json', self.rest / 'tokens.json',
            self.rest / 'templates/hello/template.json', self.token_file,
            self.profile_dir / 'profile.json', self.profile_dir / 'mcp.json')}

    def unchanged(self, snapshot):
        for path, content in snapshot.items():
            self.assertEqual(path.read_bytes(), content, path)

    def repair(self, **kwargs):
        return self.setup_agents('--repair-owner-token', 'me@login.example', **kwargs)

    def test_missing_owner_is_reported_without_false_ready(self):
        self.initialize()
        self.owner_file.unlink()
        out = self.setup_agents('--no-install', 'me@login.example')
        self.assertIn('owner access incomplete (missing-or-unusable)', out)
        self.assertIn('--repair-owner-token', out)
        self.assertNotIn('is ready', out)
        self.assertFalse(self.owner_file.exists())

    def test_mismatched_owner_is_reported_and_not_silently_replaced(self):
        self.initialize()
        self.owner_file.write_text('wrong-credential\n')
        out = self.setup_agents('--no-install', 'me@login.example')
        self.assertIn('owner access incomplete (mismatched-or-unverified)', out)
        self.assertEqual(self.owner_file.read_text(), 'wrong-credential\n')

    def test_missing_owner_recovery_preserves_all_other_settings(self):
        snapshot = self.initialize()
        self.owner_file.unlink()
        out = self.repair()
        self.assertIn('Owner credential recovered', out)
        self.assertIn('Restart the agent tunnel', out)
        self.unchanged(snapshot)
        self.assertEqual(stat.S_IMODE(self.owner_file.stat().st_mode), 0o600)
        remote = self.remote_home / '.local/tunnels/rest_token'
        token = self.owner_file.read_text().strip()
        self.assertEqual(examples.TokenAuth.load(str(remote)).identify('Bearer ' + token).name, 'owner')
        self.assertNotIn(token, out)
        self.assertNotIn(token, remote.read_text())

    def test_matching_credential_is_not_rotated(self):
        snapshot = self.initialize()
        owner = self.owner_file.read_bytes()
        remote = (self.remote_home / '.local/tunnels/rest_token').read_bytes()
        out = self.repair()
        self.assertIn('no credential changed', out)
        self.assertEqual(self.owner_file.read_bytes(), owner)
        self.assertEqual((self.remote_home / '.local/tunnels/rest_token').read_bytes(), remote)
        self.unchanged(snapshot)

    def test_empty_unreadable_and_hashed_local_credentials_can_be_repaired(self):
        snapshot = self.initialize()
        for token, mode in [('', 0o600), ('public-token\n', 0o644), ('sha256:' + 'a' * 64, 0o600)]:
            with self.subTest(token=token, mode=mode):
                self.owner_file.write_text(token)
                self.owner_file.chmod(mode)
                out = self.repair()
                self.assertIn('Owner credential recovered', out)
                self.assertEqual(stat.S_IMODE(self.owner_file.stat().st_mode), 0o600)
                self.unchanged(snapshot)

    def test_repair_survives_lost_remote_success_response(self):
        snapshot = self.initialize()
        self.owner_file.unlink()
        out = self.bash('''
            eval "$(declare -f _hpclib_agents_remote | sed '1s/_hpclib_agents_remote/_original_remote/')"
            function _hpclib_agents_remote {
                if [ "$3" = replace ]; then _original_remote "$@" >/dev/null; return 1; fi
                _original_remote "$@"
            }
            setup_agents --repair-owner-token me@login.example
        ''', expect=1)
        self.assertIn('could not be confirmed', out)
        pending = self.owner_file.with_name('owner_token.recovery')
        credential = pending.read_bytes()
        self.assertFalse(self.owner_file.exists())
        remote = (self.remote_home / '.local/tunnels/rest_token').read_bytes()
        self.repair()
        self.assertFalse(pending.exists())
        self.assertEqual(self.owner_file.read_bytes(), credential)
        self.assertEqual((self.remote_home / '.local/tunnels/rest_token').read_bytes(), remote)
        self.unchanged(snapshot)

    def test_repair_refuses_mixed_setup_options(self):
        snapshot = self.initialize()
        self.setup_agents('--repair-owner-token', '--rebuild', 'me@login.example', expect=2)
        self.setup_agents('--repair-owner-token', '--work-dir', '/different', 'me@login.example', expect=2)
        self.unchanged(snapshot)

    def test_recovery_refuses_owner_symlink_and_directory(self):
        snapshot = self.initialize()
        remote = self.remote_home / '.local/tunnels/rest_token'
        original = remote.read_bytes()
        self.owner_file.unlink()
        unrelated = self.tmp / 'unrelated'
        unrelated.write_text('leave-me-alone')
        self.owner_file.symlink_to(unrelated)
        self.repair(expect=1)
        self.assertEqual(unrelated.read_text(), 'leave-me-alone')
        self.assertEqual(remote.read_bytes(), original)
        self.owner_file.unlink()
        self.owner_file.mkdir()
        self.repair(expect=1)
        self.assertEqual(remote.read_bytes(), original)
        self.unchanged(snapshot)

    def test_unknown_profile_is_not_created(self):
        self.setup_agents('--repair-owner-token', 'unknown@host.example', expect=1)
        self.assertFalse(self.profile_dir.exists())


class TestOwnerRecoveryAPI(ConsoleTestCase):
    def wait_op(self, name):
        for _ in range(50):
            out = self.call('GET', f'/api/clusters/{name}/operation')[1]
            if out['state'] != 'running':
                return out
            time.sleep(0.1)
        self.fail('owner recovery operation did not finish')

    def test_owner_only_recovery_ignores_stored_setup_config(self):
        profile = self.clusters.profile(self.live)
        profile['setup_config'] = {'sandbox': {'method': 'auto'}}
        agent_profiles.save(profile)
        with mock.patch.object(self.clusters.logins, 'alive', return_value=True), \
                mock.patch.object(self.clusters, 'sync_llm_clients') as sync:
            status, out = self.call('POST', f'/api/clusters/{self.live}/setup', {'repair_owner_token': True})
            self.assertEqual(status, 202, out)
            self.assertEqual(self.shell.calls[-1], ['setup_agents', '--repair-owner-token', self.live])
            self.wait_op(self.live)
            sync.assert_not_called()

    def test_owner_recovery_request_is_strict(self):
        for body in ({'repair_owner_token': 'yes'}, {'repair_owner_token': True, 'rebuild': True},
                     {'repair_owner_token': True, 'work_dirs': ['/changed']}):
            with self.subTest(body=body):
                self.assertEqual(self.call('POST', f'/api/clusters/{self.live}/setup', body)[0], 400)

    def test_bad_owner_does_not_hide_working_agent_connection(self):
        (self.agents / self.live / 'owner_token').write_text('stale-owner\n')
        status, out = self.call('GET', f'/api/clusters/{self.live}')
        self.assertEqual(status, 200, out)
        self.assertEqual(out['tunnel']['state'], 'up')
        self.assertEqual(out['tunnel']['owner_access'], 'rejected')
        self.assertNotEqual(out['tunnel']['token'], 'owner')
