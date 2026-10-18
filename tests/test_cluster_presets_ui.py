"""End-to-end package/preset/MFA browser flow; fake SSH, isolated local profiles."""
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock
import test_agent_console_ui as ui
import test_agent_console as console_tests
import test_console_packages as package_tests
from test_cluster_presets import PRESET
from test_cluster_preset_settings import PORTABLE
import agent_console
import agent_profiles


@unittest.skipUnless(ui.sync_playwright, "optional Playwright dependency is not installed")
class TestClusterPresetsUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ui.TestConsoleUI.setUpClass.__func__(cls)

    def setUp(self):
        ui.TestConsoleUI.setUp(self)
        self.home = self.console / 'home'
        self.agents = self.console / 'agents'
        shutil.rmtree(self.agents, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)
        self.fake_dir = self.console / 'fake-ssh'
        self.fake_dir.mkdir(exist_ok=True)
        fake = self.console / 'ssh-fixture'
        fake.write_text(console_tests.FAKE_SSH_LOGIN); fake.chmod(0o755)
        env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'HPCLIB_AGENTS_DIR': str(self.agents),
                                          'FAKE_SSH_DIR': str(self.fake_dir), 'FAKE_SSH_PASSWORD': 'hunter2', 'FAKE_SSH_MODE': 'tacc'})
        env.start(); self.addCleanup(env.stop)
        self.shell = console_tests.FakeShell()
        self.server.clusters = agent_console.Clusters(shell_runner=self.shell, logins=agent_console.Logins(ssh=str(fake)))
        self.addCleanup(setattr, self.server, 'clusters', None)
        self.addCleanup(self.stop_login_prompts)
        self.zip_file = self.console / 'lonestar-cluster-1.0.0.zip'
        self.zip_file.write_bytes(package_tests.make_zip(PRESET))

    def stop_login_prompts(self):
        logins = self.server.clusters.logins
        with logins.lock:
            for attempt in logins.attempts.values():
                if attempt.get('code_event'):
                    logins._fail(attempt, 'test finished')

    def test_portable_settings_and_editable_username_directory(self):
        self.zip_file.write_bytes(package_tests.make_zip(PORTABLE))
        self.page.goto(self.base + '/#key=ui-test-key')
        self.page.get_by_role('button', name='Add App or Settings', exact=True).click()
        self.page.locator('hpc-dialog input[type=file]').set_input_files(str(self.zip_file))
        self.page.get_by_role('button', name='Install', exact=True).click()
        self.page.get_by_role('button', name='Add cluster', exact=True).wait_for()
        self.page.get_by_role('button', name='Add cluster', exact=True).click()
        self.page.get_by_label('Cluster configuration', exact=True).select_option('entropy')
        username = self.page.get_by_label('Cluster username', exact=True)
        work = self.page.get_by_label('Cluster work directory', exact=True)
        username.fill('newuser')
        self.assertEqual(work.input_value(), '/scratch/user/newuser/llm')
        username.fill('another')
        self.assertEqual(work.input_value(), '/scratch/user/another/llm')
        self.assertTrue(self.page.get_by_text('maximum 8 concurrent agent job slots', exact=False).is_visible())
        work.fill('/work/my-agent-files')
        username.fill('finaluser')
        self.assertEqual(work.input_value(), '/work/my-agent-files')
        self.page.locator('section.card').get_by_role('button', name='Add cluster', exact=True).click()
        self.page.locator('hpc-cluster-row').filter(has_text='finaluser@entropy.chem.tamu.edu').wait_for()
        profile = agent_profiles.all_profiles()[0]
        self.assertEqual(profile['work_dirs'], ['/work/my-agent-files'])
        self.assertEqual(profile['setup_config']['limits']['max_concurrent_jobs'], 8)
        self.assertEqual(profile['apps']['transfer']['settings']['SMB_USER'], 'finaluser')
        self.assertFalse(self.shell.calls)
        self.assertEqual(self.errors, [])

    def test_zip_install_create_profile_and_tacc_mfa_through_real_console(self):
        self.page.goto(self.base + '/#key=ui-test-key')
        self.page.get_by_role('button', name='Add App or Settings', exact=True).click()
        self.page.locator('hpc-dialog input[type=file]').set_input_files(str(self.zip_file))
        self.page.get_by_role('button', name='Install', exact=True).click()
        self.page.get_by_role('button', name='Add cluster', exact=True).wait_for()
        self.page.get_by_role('button', name='Add cluster', exact=True).click()
        self.page.get_by_label('Cluster configuration', exact=True).select_option('lonestar')
        self.page.get_by_label('Cluster username', exact=True).fill('testuser')
        self.page.get_by_label('Cluster work directory', exact=True).fill('/work/testuser/agents')
        self.page.get_by_label('Cluster allocation', exact=True).fill('Project-123')
        self.page.locator('section.card').get_by_role('button', name='Add cluster', exact=True).click()
        row = self.page.locator('hpc-cluster-row').filter(has_text='Lonestar6 (TACC)')
        row.wait_for()
        self.assertTrue(row.get_by_role('button', name='Set up SSH', exact=True).is_disabled())
        self.assertFalse(self.shell.calls)
        profile = agent_profiles.all_profiles()[0]
        self.assertEqual(profile['host'], 'testuser@ls6.tacc.utexas.edu')
        self.assertIn('--account=Project-123', profile['tunnel_args'])
        row.get_by_role('button', name='Log in', exact=True).click()
        self.page.locator('hpc-dialog input[autocomplete=current-password]').fill('hunter2')
        self.page.locator('hpc-dialog').get_by_role('button', name='Log in', exact=True).click()
        self.page.get_by_label('MFA code', exact=True).wait_for()
        self.page.locator('hpc-dialog form').get_by_role('button', name='Close', exact=True).click()
        row.get_by_role('button', name='Enter code', exact=True).wait_for()
        row.get_by_role('button', name='Enter code', exact=True).click()
        self.page.get_by_label('MFA code', exact=True).wait_for()
        self.page.get_by_label('MFA code', exact=True).fill('123456')
        self.page.get_by_label('MFA code', exact=True).press('Enter')
        row.get_by_role('button', name='Log out', exact=True).wait_for(timeout=10000)
        self.assertTrue(row.get_by_role('button', name='Set up SSH', exact=True).is_disabled())
        self.assertEqual(self.errors, [])
        for path in self.console.rglob('*.json'):
            self.assertNotIn('hunter2', path.read_text())
            self.assertNotIn('123456', path.read_text())

    def test_site_managed_ssh_page_allows_copying_and_disables_changes(self):
        record = {'name': 'lonestar', 'host': 'ls6.tacc.utexas.edu', 'set_up': True,
                  'has_owner_token': True, 'tunnel': {'state': 'up'}, 'login': {'state': 'connected'},
                  'preset': {'title': 'Lonestar6', 'ssh_keys': 'site-managed'}}
        mutations = []
        def handler(route, path):
            if route.request.method != 'GET': mutations.append(path)
            if path == 'clusters': result = {'clusters': [record]}
            elif path == 'clusters/lonestar': result = record
            elif path.endswith('/ssh/keys'):
                result = {'directory': '~/.ssh', 'errors': [], 'keys': [{'name': 'site_key', 'private_available': True,
                    'algorithm': 'ssh-ed25519', 'fingerprint': 'SHA256:fixture', 'public_key': 'ssh-ed25519 AAAA fixture', 'comment': 'site'}]}
            elif path.endswith('/ssh/authorized'): result = {'entries': [], 'errors': [], 'revision': 'fixture'}
            elif path.endswith('/ssh/hosts'): result = {'hosts': [], 'revision': 'fixture'}
            else: return False
            route.fulfill(body=json.dumps(result), content_type='application/json'); return True
        ui.TestConsoleUI.load_console(self, handler)
        self.page.get_by_role('link', name='SSH keys', exact=True).click()
        self.page.get_by_role('button', name='Copy public key', exact=True).wait_for()
        self.assertFalse(self.page.get_by_role('button', name='Copy public key', exact=True).is_disabled())
        for label in ('Create key', 'Save comment', 'Delete key', 'Add authorized key', 'Save host binding'):
            self.assertTrue(self.page.get_by_role('button', name=label, exact=True).is_disabled(), label)
        self.assertEqual(mutations, [])
        self.assertEqual(self.errors, [])
