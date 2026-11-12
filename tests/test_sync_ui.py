"""Browser gate for Sync using actual local routes and shared UI elements; no cluster/network."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hpclib' / 'servers'))
import agent_console
from test_sync_routes import Profiles

HTML = '''<!doctype html><html><head><link rel="stylesheet" href="/default-styles.css"><link rel="stylesheet" href="/style.css"></head><body><main></main><script type="module">
import { syncPage } from '/sync.js';
const api = async (path, {method = 'GET', body} = {}) => {
 if (path === 'clusters') return {clusters: [{name: 'grace'}, {name: 'entropy'}]};
 const response = await fetch('/api/' + path, {method, headers: {'Authorization': 'Bearer ui-sync-test', ...(body ? {'Content-Type': 'application/json'} : {})}, body: body ? JSON.stringify(body) : undefined});
 const result = await response.json(); if (!response.ok) throw new Error(result.error); return result;
};
document.querySelector('main').append(await syncPage({api})); window.ready = true;
</script></body></html>'''


@unittest.skipUnless(sync_playwright, 'optional Playwright is not installed')
class TestSyncUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(executable_path=os.environ.get('HPCLIB_TEST_BROWSER'))
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest('local Chromium unavailable: ' + str(e).splitlines()[0])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hpclib-sync-ui-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.code = self.base / 'code'
        self.code.mkdir()
        env = mock.patch.dict(os.environ, {'HPCLIB_CONSOLE_DIR': str(self.base / 'console'),
                                         'HPCLIB_AGENTS_DIR': str(self.base / 'agents'),
                                         'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
                                         'XDG_CONFIG_HOME': str(self.base / 'xdg')})
        env.start()
        self.addCleanup(env.stop)
        quiet = mock.patch.object(agent_console.ConsoleHandler, 'log_message', lambda *args: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.server = agent_console.ConsoleServer(('127.0.0.1', 0), 'ui-sync-test', Profiles(), static_dir=str(ROOT / 'agent-console'))
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.errors = []
        self.last_run = None
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        self.page.route('**/sync-fixture', lambda route: route.fulfill(content_type='text/html', body=HTML))
        self.page.goto('http://127.0.0.1:%s/sync-fixture' % self.server.port)
        self.page.wait_for_function('window.ready === true')

    def create(self):
        p = self.page
        p.get_by_role('button', name='Add sync', exact=True).click()
        p.locator('[name=name]').fill('Code')
        p.locator('[name=local]').first.fill(str(self.code))
        p.get_by_text('Advanced settings', exact=True).click()
        p.locator('[name=warn_file_mb]').fill('2.5')
        p.locator('[name=warn_snapshot_mb]').fill('50')
        p.locator('[name=warn_file_count]').fill('12')
        p.get_by_label('Cluster', exact=True).select_option('grace')
        p.get_by_label('Remote directory', exact=True).fill('/work/me/code')
        p.get_by_role('button', name='Add target', exact=True).click()
        p.get_by_label('Cluster', exact=True).nth(1).select_option('entropy')
        p.get_by_label('Remote directory', exact=True).nth(1).fill('/scratch/me/code')
        p.get_by_role('button', name='Save sync', exact=True).click()
        p.locator('hpc-panel[heading=Code]').wait_for()

    def dialog(self, heading, yes):
        dialog = self.page.locator('hpc-dialog[heading="%s"]' % heading)
        # showModal puts the native shadow-DOM dialog in the top layer; the inline host has no visible box.
        dialog.locator('dialog').wait_for(state='visible')
        dialog.get_by_role('button', name='OK' if yes else 'Cancel', exact=True).click()
        dialog.wait_for(state='detached')

    def wait_for_check(self):
        self.page.wait_for_function('''async previous => {
          const response = await fetch('/api/sync', {headers: {Authorization: 'Bearer ui-sync-test'}});
          const data = await response.json();
          const status = data.syncs[0]?.status;
          return data.syncs.length === 1 && status.run && status.run !== previous && !status.running;
        }''', arg=self.last_run)
        record = self.server.sync.list()['syncs'][0]
        self.last_run = record['status']['run']
        return record

    def test_check_snapshot_repeat_warning_and_stale_approval_through_ui(self):
        from sync_git import rpc
        remote = self.base / 'remote'
        remote.mkdir()
        metadata = {'v': 1, 'git': {'default_excludes': 'sync-only', 'default_excludes_version': 1}}
        for root in (self.code, remote):
            (root / '.hpclib-sync.json').write_text(json.dumps(metadata))
        (self.code / 'local-only.txt').write_text('local fixture\n')
        (remote / 'remote-only.txt').write_text('remote fixture\n')
        (self.code / '.pytest_cache').mkdir()
        (self.code / '.pytest_cache/ignored.txt').write_text('excluded fixture\n')
        # This seam replaces only the network; the production remote protocol and Git operations are real.
        self.server.sync.remote = lambda profile, request: rpc(request, str(self.base / 'remote-state'))
        p = self.page
        p.get_by_role('button', name='Add sync', exact=True).click()
        p.get_by_label('Name', exact=True).fill('Live flow')
        p.get_by_label('Local directory', exact=True).fill(str(self.code))
        p.get_by_label('Cluster', exact=True).select_option('entropy')
        p.get_by_label('Remote directory', exact=True).fill(str(remote))
        p.get_by_role('button', name='Save sync', exact=True).click()
        p.locator('hpc-panel[heading="Live flow"]').wait_for()

        p.get_by_role('button', name='Check files', exact=True).click()
        checked = self.wait_for_check()
        self.assertEqual(checked['status']['state'], 'preview-ready')
        self.assertNotIn('commit', checked['status']['local_snapshot'])
        self.assertEqual(checked['status']['local_snapshot']['summary']['eligible_files'], 2)
        p.get_by_role('button', name='Create snapshots…', exact=True).click()
        self.dialog('Create sync snapshots?', True)
        captured = self.wait_for_check()
        self.assertEqual(captured['status']['state'], 'snapshots-ready')
        commit = captured['status']['local_snapshot']['commit']
        self.assertEqual((self.code / 'local-only.txt').read_text(), 'local fixture\n')
        self.assertFalse((self.code / 'remote-only.txt').exists())
        self.assertFalse((remote / 'local-only.txt').exists())

        p.get_by_role('button', name='Create snapshots…', exact=True).click()
        self.dialog('Create sync snapshots?', True)
        self.assertEqual(self.wait_for_check()['status']['local_snapshot']['commit'], commit)

        p.get_by_role('button', name='Check files', exact=True).click()
        unchanged = self.wait_for_check()
        self.assertEqual(unchanged['status']['local_snapshot']['summary']['new_changed_files'], 0)
        inventory = p.locator('.sync-volume-review')
        inventory.locator('summary').first.click()
        self.assertIn('local-only.txt', inventory.locator('.sync-eligible-files').inner_text())
        self.assertIn('Unchanged metadata', inventory.inner_text())
        self.assertIn('No metadata changes since', inventory.inner_text())
        self.assertIn(commit[:12], p.locator('.sync-pair-table').inner_text())
        self.assertEqual(p.get_by_role('button', name='Verify all files…', exact=True).count(), 0)
        p.get_by_role('button', name='Create verified snapshots…', exact=True).click()
        self.assertIn('create or update commits', p.locator('hpc-dialog dialog').inner_text())
        self.dialog('Create verified sync snapshots?', False)
        p.get_by_role('button', name='Create verified snapshots…', exact=True).click()
        self.dialog('Create verified sync snapshots?', True)
        self.assertEqual(self.wait_for_check()['status']['local_snapshot']['commit'], commit)
        p.get_by_role('button', name='Edit', exact=True).click()
        p.get_by_text('Advanced settings', exact=True).click()
        p.locator('[name=warn_file_count]').fill('1')
        p.get_by_role('button', name='Save sync', exact=True).click()
        p.locator('hpc-panel[heading="Live flow"]').wait_for()
        (self.code / 'warning.txt').write_text('new after snapshot\n')
        p.get_by_role('button', name='Check files', exact=True).click()
        blocked = self.wait_for_check()
        self.assertEqual(blocked['status']['state'], 'needs-volume-review')
        p.get_by_role('button', name='Approve snapshot…', exact=True).wait_for()
        (self.code / 'warning.txt').write_text('changed after preview\n')
        p.get_by_role('button', name='Approve snapshot…', exact=True).click()
        self.dialog('Approve snapshot ingestion?', True)
        self.assertEqual(self.wait_for_check()['status']['state'], 'stale-plan')
        p.get_by_role('button', name='Check files', exact=True).click()
        self.wait_for_check()
        p.get_by_role('button', name='Approve snapshot…', exact=True).click()
        self.dialog('Approve snapshot ingestion?', True)
        final = self.wait_for_check()
        self.assertEqual(final['status']['state'], 'snapshots-ready', final['status'])
        self.assertNotEqual(final['status']['local_snapshot']['commit'], commit)
        self.assertEqual((self.code / 'warning.txt').read_text(), 'changed after preview\n')
        p.get_by_role('button', name='Review sync…', exact=True).click()
        reviewed = self.wait_for_check()
        self.assertEqual(reviewed['status']['state'], 'needs-sync-review', reviewed['status'])
        self.assertFalse((self.code / 'remote-only.txt').exists())
        p.get_by_role('button', name='Apply reviewed sync…', exact=True).click()
        self.dialog('Apply this reviewed Git sync?', False)
        self.assertFalse((self.code / 'remote-only.txt').exists())
        p.get_by_role('button', name='Apply reviewed sync…', exact=True).click()
        self.dialog('Apply this reviewed Git sync?', True)
        applied = self.wait_for_check()
        self.assertEqual(applied['status']['state'], 'in-sync', applied['status'])
        self.assertEqual((self.code / 'remote-only.txt').read_text(), 'remote fixture\n')
        self.assertEqual((remote / 'local-only.txt').read_text(), 'local fixture\n')
        self.assertEqual((remote / 'warning.txt').read_text(), 'changed after preview\n')
        self.assertEqual(self.errors, [])

    def test_pair_editor_thresholds_and_reload(self):
        self.create()
        p = self.page
        self.assertFalse(p.get_by_role('button', name='Review sync…', exact=True).is_disabled())
        p.get_by_role('button', name='Edit', exact=True).click()
        p.get_by_text('Advanced settings', exact=True).click()
        self.assertEqual(p.locator('[name=warn_file_mb]').input_value(), '2.5')
        self.assertEqual(p.locator('[name=warn_snapshot_mb]').input_value(), '50')
        self.assertEqual(p.locator('[name=warn_file_count]').input_value(), '12')
        self.assertEqual(p.get_by_label('Remote directory', exact=True).count(), 2)
        self.assertEqual(self.errors, [])

    def test_agent_access_requires_confirmation_and_can_be_revoked(self):
        self.create()
        p = self.page
        p.get_by_role('button', name='Allow agent access…', exact=True).click()
        self.dialog('Allow agent control of this sync?', False)
        self.assertFalse(self.server.sync.list()['syncs'][0].get('agent_control'))
        p.get_by_role('button', name='Allow agent access…', exact=True).click()
        self.dialog('Allow agent control of this sync?', True)
        p.get_by_role('button', name='Revoke agent access…', exact=True).wait_for()
        self.assertTrue(self.server.sync.list()['syncs'][0]['agent_control'])
        p.get_by_role('button', name='Revoke agent access…', exact=True).click()
        self.dialog('Revoke agent control?', True)
        p.get_by_role('button', name='Allow agent access…', exact=True).wait_for()
        self.assertFalse(self.server.sync.list()['syncs'][0]['agent_control'])

    def test_compact_pair_layout_and_live_direction_arrows(self):
        p = self.page
        p.get_by_role('button', name='Add sync', exact=True).click()
        self.assertFalse(p.locator('.sync-advanced').evaluate('(node) => node.open'))
        self.assertLess(p.locator('.sync-editor-card').bounding_box()['height'], 380)
        local = p.get_by_label('Local directory', exact=True).bounding_box()
        remote = p.get_by_label('Remote directory', exact=True).bounding_box()
        self.assertLess(abs(local['y'] - remote['y']), 5)
        self.assertLess(local['x'], remote['x'])
        self.assertEqual(p.get_by_role('img', name='Bidirectional sync').inner_text(), '↔')
        p.get_by_label('Sync direction', exact=True).select_option('push')
        self.assertEqual(p.get_by_role('img', name='Local to remote').inner_text(), '→')
        p.get_by_label('Sync direction', exact=True).select_option('pull')
        self.assertEqual(p.get_by_role('img', name='Remote to local').inner_text(), '←')
        p.get_by_label('Local directory', exact=True).fill(str(self.code))
        p.get_by_role('button', name='Add target', exact=True).click()
        self.assertEqual(p.get_by_label('Local directory', exact=True).nth(1).input_value(), str(self.code))
        p.get_by_label('Local directory', exact=True).nth(1).fill(str(self.code) + '-changed')
        self.assertEqual(p.get_by_label('Local directory', exact=True).first.input_value(), str(self.code) + '-changed')
        self.assertEqual(self.errors, [])

    def test_merge_declined_private_defaults_accepted(self):
        original = '# human rules\n*.cache\n'
        (self.code / '.gitignore').write_text(original)
        self.create()
        self.page.get_by_role('button', name='Ignore setup…', exact=True).click()
        self.page.get_by_role('button', name='Choose ignore settings…', exact=True).click()
        self.dialog('Merge default ignore rules?', False)
        self.dialog('Use defaults for this sync only?', True)
        self.dialog('Save ignore choice?', True)
        self.page.wait_for_function("!document.querySelector('.sync-setup').textContent.includes('Choose ignore settings')")
        metadata = json.loads((self.code / '.hpclib-sync.json').read_text())
        self.assertEqual(metadata['git']['default_excludes'], 'sync-only')
        self.assertEqual((self.code / '.gitignore').read_text(), original)
        self.assertEqual(self.errors, [])

    def test_declining_both_persists_off_and_merge_yes_adds_one_block(self):
        (self.code / '.gitignore').write_text('!keep.tmp\n')
        self.create()
        p = self.page
        p.get_by_role('button', name='Ignore setup…', exact=True).click()
        p.get_by_role('button', name='Choose ignore settings…', exact=True).click()
        self.dialog('Merge default ignore rules?', False)
        self.dialog('Use defaults for this sync only?', False)
        self.dialog('Save ignore choice?', True)
        p.get_by_role('button', name='Ignore setup…', exact=True).click()
        p.get_by_role('button', name='Change ignore settings…', exact=True).click()
        self.dialog('Merge default ignore rules?', True)
        self.dialog('Save ignore choice?', True)
        p.wait_for_function("!document.querySelector('.sync-setup').textContent.includes('Change ignore settings')")
        text = (self.code / '.gitignore').read_text()
        self.assertEqual(text.count('# BEGIN hpclib sync defaults'), 1)
        self.assertTrue(text.endswith('!keep.tmp\n'))
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
