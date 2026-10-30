"""Transport failure and pathname checks, independent of live SMB/Kerberos."""
import argparse
import importlib.util
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('samba_transport', ROOT / 'hpclib/tunnels/data-transfer/smbclient_transfer.py')
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


class TestSambaTransport(unittest.TestCase):
    def test_listing_preserves_spaces_unicode_and_metadata(self):
        line = '  ' + 'a long name α.dat'.ljust(30) + 'A'.rjust(7) + '      123  Thu Oct 29 12:00:00 2026\n'
        row = transport.listing(line)[0]
        self.assertEqual(row['Name'], 'a long name α.dat')
        self.assertEqual(row['Size'], 123)
        self.assertFalse(row['IsDir'])
        self.assertEqual(row['ModTime'], '2026-10-29T12:00:00+00:00')

    def test_unparseable_listings_and_command_injection_fail_closed(self):
        with self.assertRaises(transport.TransferError):
            transport.listing('some unrecognized successful-looking output')
        for name in ('../x', 'a/../x', 'a//b'):
            with self.subTest(name=name), self.assertRaises(transport.TransferError):
                transport.path(name)
        for name in ('a;! touch stolen', 'a"b', 'a\nb', 'wild*', 'a\\b'):
            with self.subTest(name=name), self.assertRaises(transport.TransferError):
                transport.quote(name)

    def test_local_case_collisions_are_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            (directory / 'file').write_text('first')
            with mock.patch.object(transport.os, 'walk', return_value=[(str(directory), [], ['file', 'FILE'])]):
                with self.assertRaisesRegex(transport.TransferError, 'name collision'):
                    transport.local_tree(directory)

    def client(self):
        args = argparse.Namespace(timeout=30, bind=[], runtime='/singularity', image='/image.sif',
                                  cache='/private/ticket', profile='/private/profile')
        return transport.Client(args)

    def test_short_service_identity_retains_destination_and_strips_password_environment(self):
        env = dict(SMB_HOST='files.example.edu', SMB_SPN='cifs/files', SMB_REALM='AUTH.EXAMPLE.EDU',
                   RCLONE_CONFIG_SMB_PASS='must-not-reach-samba', PASSWD='must-not-reach-samba')
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(transport.socket, 'getaddrinfo',
                return_value=[(None, None, None, None, ('10.0.0.1', 445))]):
            client = self.client()
        with mock.patch.object(transport.subprocess, 'run', return_value=argparse.Namespace(returncode=0, stdout='', stderr='')) as run:
            client.run('research', 'quit')
        argv = run.call_args.args[0]
        self.assertIn('//files/research', argv)
        self.assertIn('--ip-address=10.0.0.1', argv)
        self.assertIn('--realm=AUTH.EXAMPLE.EDU', argv)
        self.assertIn('--use-kerberos=required', argv)
        self.assertFalse(any(key.startswith('RCLONE_') for key in run.call_args.kwargs['env']))
        self.assertNotIn('PASSWD', run.call_args.kwargs['env'])

    def test_zero_exit_smb_failure_is_still_failure(self):
        with mock.patch.dict(os.environ, {'SMB_HOST': 'files.example.edu'}, clear=True):
            client = self.client()
        with mock.patch.object(transport.subprocess, 'run', return_value=argparse.Namespace(
                returncode=0, stdout='NT_STATUS_ACCESS_DENIED listing \\*', stderr='')):
            with self.assertRaises(transport.TransferError):
                client.run('research', 'ls "\\*"')

    def test_successful_handshake_diagnostics_are_not_mistaken_for_command_failures(self):
        with mock.patch.dict(os.environ, {'SMB_HOST': 'files.example.edu'}, clear=True):
            client = self.client()
        for diagnostic in ('SPNEGO handshake: NT_STATUS_MORE_PROCESSING_REQUIRED',
                           'cache lookup returned NT_STATUS_OBJECT_NAME_NOT_FOUND'):
            with self.subTest(diagnostic=diagnostic), mock.patch.object(transport.subprocess, 'run',
                    return_value=argparse.Namespace(returncode=0, stdout='', stderr=diagnostic)):
                self.assertEqual(client.run('research', 'ls "\\*"'), '')
        with mock.patch.object(transport.subprocess, 'run', return_value=argparse.Namespace(
                returncode=0, stdout='', stderr='session setup failed: NT_STATUS_ACCESS_DENIED')):
            with self.assertRaises(transport.TransferError): client.run('research', 'ls "\\*"')

    def test_ticket_realm_defaults_and_explicit_realms_take_precedence(self):
        args = argparse.Namespace(timeout=30, bind=[], runtime='/singularity', image='/image.sif',
                                  cache='/private/ticket', profile='/private/profile',
                                  ticket_principal='me@AUTH.EXAMPLE.EDU')
        for settings, expected in (({'SMB_HOST': 'files.example.edu'}, 'AUTH.EXAMPLE.EDU'),
                                   ({'SMB_HOST': 'files.example.edu', 'SMB_REALM': 'OTHER.EXAMPLE.EDU'}, 'OTHER.EXAMPLE.EDU'),
                                   ({'SMB_HOST': 'files.example.edu', 'SMB_SPN': 'cifs/files@TARGET.EXAMPLE.EDU'}, 'TARGET.EXAMPLE.EDU')):
            with self.subTest(settings=settings), mock.patch.dict(os.environ, settings, clear=True), \
                    mock.patch.object(transport.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('10.0.0.1', 445))]):
                client = transport.Client(args)
            self.assertEqual(client.realm, expected)

    def test_unknown_fqdn_retries_one_read_with_short_identity_and_same_destination(self):
        with mock.patch.dict(os.environ, {'SMB_HOST': 'files.example.edu', 'SMB_REALM': 'AUTH.EXAMPLE.EDU'}, clear=True):
            client = self.client()
        failed = argparse.Namespace(returncode=1, stdout='', stderr=(
            'gse_get_client_auth_token: Server principal cifs/files.example.edu@AUTH.EXAMPLE.EDU not found\n'
            'session setup failed: NT_STATUS_INVALID_PARAMETER\n'))
        ok = argparse.Namespace(returncode=0, stdout='listing', stderr='debug output')
        with mock.patch.object(transport.subprocess, 'run', side_effect=[failed, ok, ok]) as run, \
                mock.patch.object(transport.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('10.0.0.1', 445))]):
            self.assertEqual(client.run('research', 'ls "\\*"'), 'listing')
            client.run('research', 'put "local" "\\target"')
        first, second, write = [call.args[0] for call in run.call_args_list]
        self.assertIn('//files.example.edu/research', first)
        self.assertIn('--debuglevel=3', first)
        self.assertIn('--debuglevel=0', write)
        for argv in (second, write):
            self.assertIn('//files/research', argv)
            self.assertIn('--ip-address=10.0.0.1', argv)
            self.assertIn('--realm=AUTH.EXAMPLE.EDU', argv)
            self.assertIn('--use-kerberos=required', argv)

    def test_generic_errors_writes_and_explicit_spns_are_never_retried(self):
        unknown = 'Server principal cifs/files.example.edu@AUTH.EXAMPLE.EDU not found'
        for command, spn, error in (
            ('put "source" "target"', '', unknown),
            ('ls "\\*"; del "target"', '', unknown),
            ('ls "\\*"', 'cifs/files.example.edu', unknown),
            ('ls "\\*"', '', 'session setup failed: NT_STATUS_INVALID_PARAMETER'),
            ('ls "\\*"', '', 'NT_STATUS_ACCESS_DENIED'),
            ('ls "\\*"', '', 'KDC has no support for encryption type'),
            ('ls "\\*"', '', 'No credentials cache found'),
        ):
            with self.subTest(command=command, spn=spn, error=error), mock.patch.dict(os.environ,
                    {'SMB_HOST': 'files.example.edu', 'SMB_REALM': 'AUTH.EXAMPLE.EDU', 'SMB_SPN': spn}, clear=True):
                client = self.client()
            with mock.patch.object(transport.subprocess, 'run', return_value=argparse.Namespace(
                    returncode=1, stdout='', stderr=error)) as run:
                with self.assertRaises(transport.TransferError): client.run('research', command)
                self.assertEqual(run.call_count, 1)

    def test_short_identity_discovery_is_bounded_and_does_not_hide_policy_errors(self):
        with mock.patch.dict(os.environ, {'SMB_HOST': 'files.example.edu', 'SMB_REALM': 'AUTH.EXAMPLE.EDU'}, clear=True):
            client = self.client()
        replies = [argparse.Namespace(returncode=1, stdout='', stderr='Server not found in Kerberos database'),
                   argparse.Namespace(returncode=1, stdout='', stderr='KDC has no support for encryption type\nNT_STATUS_INVALID_PARAMETER')]
        with mock.patch.object(transport.subprocess, 'run', side_effect=replies) as run, \
                mock.patch.object(transport.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('10.0.0.1', 445))]):
            with self.assertRaisesRegex(transport.TransferError, 'cifs/files@AUTH.EXAMPLE.EDU.*encryption type'):
                client.run('research', 'ls "\\*"')
            self.assertEqual(run.call_count, 2)

    def test_failed_download_preserves_existing_destination_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve() / 'result.dat'
            target.write_text('original')
            with mock.patch.dict(os.environ, {'SMB_HOST': 'files.example.edu'}, clear=True):
                client = self.client()
            def incomplete(share, command):
                words = shlex.split(command)
                Path(words[2]).write_text('partial')
                raise transport.TransferError('connection failed')
            client.run = incomplete
            with self.assertRaises(transport.TransferError):
                client.download('research', 'file', target, dict(Size=100, ModTime=None))
            self.assertEqual(target.read_text(), 'original')
            self.assertEqual([p.name for p in target.parent.iterdir()], ['result.dat'])

    def test_download_size_mismatch_never_publishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve() / 'result.dat'
            with mock.patch.dict(os.environ, {'SMB_HOST': 'files.example.edu'}, clear=True):
                client = self.client()
            def incomplete(share, command):
                Path(shlex.split(command)[2]).write_text('short')
            client.run = incomplete
            with self.assertRaisesRegex(transport.TransferError, 'size changed'):
                client.download('research', 'file', target, dict(Size=100, ModTime=None))
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.iterdir()), [])

    def test_sync_dry_run_never_writes_or_deletes(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary).resolve()
            (local / 'extra').write_text('keep')
            client = mock.Mock()
            client.lookup.return_value = dict(IsDir=True)
            client.tree.return_value = {'new': dict(IsDir=False, Size=10)}
            args = argparse.Namespace(command='sync-pull', remote='research/folder', local=str(local), dry_run=True)
            transport.execute(client, args)
            client.download.assert_not_called()
            client.run.assert_not_called()
            self.assertEqual((local / 'extra').read_text(), 'keep')

    def test_failed_sync_copy_never_deletes_extra_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary).resolve()
            (local / 'extra').write_text('keep')
            client = mock.Mock()
            client.lookup.return_value = dict(IsDir=True)
            client.tree.return_value = {'new': dict(IsDir=False, Size=10)}
            client.download.side_effect = transport.TransferError('connection failed')
            args = argparse.Namespace(command='sync-pull', remote='research/folder', local=str(local), dry_run=False)
            with self.assertRaises(transport.TransferError):
                transport.execute(client, args)
            self.assertEqual((local / 'extra').read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
