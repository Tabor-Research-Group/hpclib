"""SSH management tests use real OpenSSH and isolated shared-home directories."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hpclib" / "servers"))
from ssh_keys import COMPUTE_KEY, INCLUDE, SSHKeyError, SSHKeyStore
from test_rest_jobs import JobServerTestCase


class TestSSHFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SSHKeyStore(Path(self.tmp.name) / "shared home")

    def assert_error(self, status, fn, *args, **kwargs):
        with self.assertRaises(SSHKeyError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.status, status, str(ctx.exception))

    def test_setup_is_idempotent_and_shared_across_nodes(self):
        first = self.store.setup_cluster()
        private = (self.store.root / COMPUTE_KEY).read_bytes()
        authorized = (self.store.root / "authorized_keys").read_bytes()
        second = SSHKeyStore(self.store.home).setup_cluster()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertFalse(second["authorization_added"])
        self.assertEqual(private, (self.store.root / COMPUTE_KEY).read_bytes())
        self.assertEqual(authorized, (self.store.root / "authorized_keys").read_bytes())
        self.assertNotIn("PRIVATE KEY", json.dumps(first))
        for file, mode in ((self.store.root, 0o700), (self.store.root / COMPUTE_KEY, 0o600),
                           (self.store.root / "authorized_keys", 0o600)):
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), mode)

    def test_login_authorization_preserves_restrictions_and_other_lines(self):
        key = self.store.create('workstation')
        auth = self.store.root / 'authorized_keys'
        original = '# existing keys\nrestrict ' + key['public_key'] + '\n'
        auth.write_text(original)
        result = self.store.authorize_login(key['public_key'])
        self.assertTrue(result['authorized'])
        self.assertTrue(result['restrictions_preserved'])
        self.assertFalse(result['authorization_added'])
        self.assertEqual(auth.read_text(), original)
        other = self.store.create('other')
        self.assertTrue(self.store.authorize_login(other['public_key'])['authorization_added'])
        self.assertFalse(self.store.authorize_login(other['public_key'])['authorization_added'])
        self.assertEqual(auth.read_text(), original + other['public_key'] + '\n')
        self.assert_error(422, self.store.authorize_login, 'restrict ' + other['public_key'])

    def test_setup_recovers_public_and_refuses_orphans_or_mismatches(self):
        result = self.store.setup_cluster()
        public = self.store.root / (COMPUTE_KEY + ".pub")
        public.unlink()
        self.assertEqual(self.store.setup_cluster()["key"]["fingerprint"], result["key"]["fingerprint"])
        other = self.store.create("other")
        public.write_text(other["public_key"] + "\n")
        self.assert_error(409, self.store.setup_cluster)
        (self.store.root / COMPUTE_KEY).unlink()
        self.assert_error(409, self.store.setup_cluster)
        self.assertEqual(public.read_text(), other["public_key"] + "\n")

    def test_targeted_authorization_preserves_comments_options_and_stale_edits(self):
        a, b = self.store.create("first"), self.store.create("second")
        path = self.store.root / "authorized_keys"
        prefix = '# custom header\n# keep me\n'
        line = 'restrict,command="echo hello world" ' + a["public_key"]
        path.write_text(prefix + line + '\nnot-an-ssh-key\n')
        state = self.store.authorized()
        entry = state["entries"][0]
        self.assertEqual(entry["options"], 'restrict,command="echo hello world"')
        self.assertEqual(len(state["errors"]), 1)
        updated = self.store.change_authorized(b["public_key"], entry["id"], state["revision"])
        self.assertTrue(path.read_text().startswith(prefix))
        self.assertIn('not-an-ssh-key\n', path.read_text())
        self.assert_error(409, self.store.change_authorized, line, expected=state["revision"])
        self.store.change_authorized(entry=updated["entries"][0]["id"], expected=updated["revision"], delete=True)
        self.assertEqual(path.read_text(), prefix + 'not-an-ssh-key\n')

    def test_compute_setup_preserves_existing_restrictions(self):
        key = self.store.create(COMPUTE_KEY)
        text = '# Restrict this identity\nrestrict ' + key["public_key"] + '\n'
        (self.store.root / "authorized_keys").write_text(text)
        self.assertFalse(self.store.setup_cluster()["authorization_added"])
        self.assertEqual((self.store.root / "authorized_keys").read_text(), text)

    def test_pair_management_and_public_comments(self):
        key = self.store.create("github", comment="Mark's GitHub key")
        private = (self.store.root / "github").read_bytes()
        self.assertEqual(key["comment"], "Mark's GitHub key")
        updated = self.store.update_comment("github", 'shared "research" key')
        self.assertEqual(updated["fingerprint"], key["fingerprint"])
        self.assertEqual(updated["comment"], 'shared "research" key')
        self.assertEqual((self.store.root / "github").read_bytes(), private)
        self.assert_error(409, self.store.create, "github")
        self.store.delete("github")
        self.assertEqual(self.store.keys()["keys"], [])

    def test_rsa_key_generation_uses_native_fingerprints(self):
        key = self.store.create('rsa_key', algorithm='rsa')
        info = subprocess.run(['ssh-keygen', '-l', '-E', 'sha256', '-f', str(self.store.root / 'rsa_key.pub')],
                              check=True, capture_output=True, text=True).stdout
        self.assertTrue(info.startswith('3072 '))
        self.assertEqual(key['fingerprint'], info.split()[1])

    def test_host_bindings_preserve_native_config_and_use_home_references(self):
        self.store.create("github")
        original = '# User comment\nHost private\n  IdentityFile="' + str(self.store.root / 'github') + '"\n  ProxyCommand ssh gateway -W %h:%p\n'
        config = self.store.root / "config"
        config.write_text(original)
        state = self.store.set_host("github.com", "github", user="git")
        self.assertEqual(config.read_text(), INCLUDE + original)
        self.assertEqual(state["hosts"][0]["identity_files"], ['~/.ssh/github'])
        self.assertEqual(state["hosts"][-1]["key"], 'github')
        managed = self.store.root / "hpclib_keys.conf"
        managed.write_text(managed.read_text() + '    # keep this comment\n    ServerAliveInterval 10\n')
        state = self.store.set_host("github.com", "github", user="git", port=2222)
        self.assertIn('ServerAliveInterval 10', managed.read_text())
        self.assertIn('# keep this comment', managed.read_text())
        # Native SSH parses the generated block without connecting anywhere.
        result = subprocess.run(['ssh', '-G', '-F', str(managed), 'github.com'], capture_output=True, text=True, check=True)
        self.assertIn('user git\n', result.stdout)
        self.assertIn('port 2222\n', result.stdout)
        self.assert_error(409, self.store.delete, "github")
        self.assert_error(409, self.store.set_host, "github.com", "github", expected="stale")
        self.store.set_host("github.com", expected=state["revision"], delete=True)
        # The original native config remains untouched and still references it.
        self.assert_error(409, self.store.delete, "github")
        self.assertEqual(config.read_text(), INCLUDE + original)

    def test_refuses_path_injection_symlinks_and_unrelated_files(self):
        self.store.create("safe")
        for name in ("../escape", "config", "/tmp/key", "key.pub", "a\nkey"):
            self.assert_error(422, self.store.create, name)
        target = self.store.home / 'outside'
        target.write_text('preserve me')
        (self.store.root / 'linked.pub').symlink_to(target)
        self.assert_error(409, self.store.delete, 'linked')
        self.assertEqual(target.read_text(), 'preserve me')
        (self.store.root / 'unrelated').write_text('configuration, not a key')
        self.assert_error(422, self.store.delete, 'unrelated')
        self.assert_error(422, self.store.set_host, 'github.com\nProxyCommand evil', 'safe')
        self.assert_error(422, self.store.change_authorized, 'ssh-ed25519 AAAA invalid')
        fifo = self.store.root / 'fifo.pub'
        os.mkfifo(str(fifo))
        self.assert_error(403, self.store.key, 'fifo')

    def test_setup_cli_is_noninteractive_and_public_only(self):
        result = subprocess.run([sys.executable, str(ROOT / 'hpclib/servers/ssh_keys.py'), '--setup'],
                                env=dict(os.environ, HOME=str(self.store.home)), capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(result.stdout)['created'])
        self.assertNotIn('PRIVATE KEY', result.stdout + result.stderr)

    def test_encrypted_identities_can_be_listed_and_bound_but_compute_setup_preserves_them(self):
        self.store._check_root(create=True)
        private = self.store.root / COMPUTE_KEY
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', 'test-only-passphrase', '-f', str(private)], check=True)
        original = private.read_bytes()
        self.assertTrue(self.store.keys()['keys'][0]['private_available'])
        self.store.set_host('private.example', COMPUTE_KEY)
        self.assert_error(422, self.store.setup_cluster)
        self.assertEqual(private.read_bytes(), original)

    def test_setup_rejects_a_symlinked_management_lock(self):
        self.store._check_root(create=True)
        target = self.store.home / 'outside'
        target.write_text('preserve me')
        (self.store.root / '.hpclib.lock').symlink_to(target)
        self.assert_error(409, self.store.setup_cluster)
        self.assertEqual(target.read_text(), 'preserve me')


class TestSSHAPI(JobServerTestCase):
    def setUp(self):
        super().setUp()
        self.server.ssh_keys = SSHKeyStore(self.tmp / 'cluster-home')

    def test_owner_api_lifecycle_and_scoped_tokens_denied(self):
        for route in ('keys', 'authorized', 'hosts'):
            self.expect_error(403, self.llm.request, 'GET', '/admin/ssh/' + route)
        for verb, route, body in (('POST', 'setup', {}), ('POST', 'keys', {'name': 'x'}),
                                 ('PUT', 'keys', {'name': 'x'}), ('DELETE', 'keys', {'name': 'x'}),
                                 ('POST', 'authorized', {}), ('PUT', 'authorized', {}), ('DELETE', 'authorized', {}),
                                 ('PUT', 'hosts', {}), ('DELETE', 'hosts', {})):
            self.expect_error(403, self.llm.request, verb, '/admin/ssh/' + route, body=body)
        first = self.owner.request('POST', '/admin/ssh/setup', body={})
        self.assertTrue(first['created'])
        self.assertFalse(self.owner.request('POST', '/admin/ssh/setup', body={})['created'])
        key = self.owner.request('POST', '/admin/ssh/keys', body={'name': 'github', 'comment': 'GitHub'})['key']
        self.assertEqual(len(self.owner.request('GET', '/admin/ssh/keys')['keys']), 2)
        self.owner.request('PUT', '/admin/ssh/keys', body={'name': 'github', 'comment': 'Updated'})
        auth = self.owner.request('GET', '/admin/ssh/authorized')
        auth = self.owner.request('POST', '/admin/ssh/authorized', body={'line': key['public_key'], 'revision': auth['revision']})
        entry = next(e for e in auth['entries'] if e['fingerprint'] == key['fingerprint'])
        auth = self.owner.request('PUT', '/admin/ssh/authorized', body={'id': entry['id'], 'line': 'restrict ' + key['public_key'], 'revision': auth['revision']})
        entry = next(e for e in auth['entries'] if e['fingerprint'] == key['fingerprint'])
        self.owner.request('DELETE', '/admin/ssh/authorized', body={'id': entry['id'], 'revision': auth['revision']})
        hosts = self.owner.request('PUT', '/admin/ssh/hosts', body={'host': 'github.com', 'key': 'github', 'user': 'git'})
        self.expect_error(409, self.owner.request, 'DELETE', '/admin/ssh/keys', body={'name': 'github'})
        self.owner.request('DELETE', '/admin/ssh/hosts', body={'host': 'github.com', 'revision': hosts['revision']})
        self.owner.request('DELETE', '/admin/ssh/keys', body={'name': 'github'})
        self.expect_error(422, self.owner.request, 'POST', '/admin/ssh/keys', body={'name': 'bad', 'private_key': 'secret'})
        audit = self.audit_path.read_text()
        self.assertNotIn('PRIVATE KEY', audit)
        self.assertNotIn(key['public_key'], audit)


class TestSSHFileChangesDisabled(TestSSHAPI):
    disable_file_changes = True

    def test_owner_api_lifecycle_and_scoped_tokens_denied(self):
        for verb, route in (('POST', 'setup'), ('POST', 'keys'), ('PUT', 'keys'), ('DELETE', 'keys'),
                            ('POST', 'authorized'), ('PUT', 'authorized'), ('DELETE', 'authorized'),
                            ('PUT', 'hosts'), ('DELETE', 'hosts')):
            self.expect_error(403, self.owner.request, verb, '/admin/ssh/' + route, body={})
        self.assertEqual(self.owner.request('GET', '/admin/ssh/keys')['keys'], [])
        self.assertFalse(self.server.ssh_keys.root.exists())
