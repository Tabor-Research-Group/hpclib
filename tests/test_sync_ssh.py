"""Real OpenSSH master + Paramiko loopback server + the actual remote Git helper.

Paramiko is a disposable test dependency, never a product requirement. No user SSH
configuration, keys, credentials, home state, or actual cluster login is accessed.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

try:
    import paramiko
except ImportError:
    paramiko = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hpclib/servers'))
from agent_console import Logins
from sync_git import GitStore
from sync_spec import SyncError


@unittest.skipUnless(paramiko and shutil.which('ssh'), 'optional Paramiko/OpenSSH gate unavailable')
class TestStrictSnapshotSSH(unittest.TestCase):
    def test_real_rpc_object_exchange_and_dead_master_never_reauthenticates(self):
        with tempfile.TemporaryDirectory(prefix='wire-', dir=os.environ.get('HPCLIB_SYNC_FIXTURE_ROOT')) as data_temp, \
                tempfile.TemporaryDirectory(prefix='hs-', dir=os.environ.get('HPCLIB_SYNC_SSH_TMPDIR')) as temp:
            base = Path(temp).resolve()
            data_base = Path(data_temp).resolve()
            key, host_key = paramiko.RSAKey.generate(2048), paramiko.RSAKey.generate(2048)
            identity = base / 'key'
            key.write_private_key_file(str(identity))
            identity.chmod(0o600)
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.listen(8)
            listener.settimeout(0.2)
            port = listener.getsockname()[1]
            known = base / 'known'
            known.write_text('[127.0.0.1]:%d %s %s\n' % (port, host_key.get_name(), host_key.get_base64()))
            stopped, connected, commands = threading.Event(), [], []
            transports, errors = [], []
            env = dict(os.environ, HPCTUNNELS_DATA_DIR=str(data_base / 'remote-state'),
                       XDG_CONFIG_HOME=str(base / 'xdg'),
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                       PYTHONDONTWRITEBYTECODE='1')

            class Server(paramiko.ServerInterface):
                def get_allowed_auths(self, username):
                    return 'publickey'

                def check_auth_publickey(self, username, public):
                    return paramiko.AUTH_SUCCESSFUL if username == 'fixture' and public == key else paramiko.AUTH_FAILED

                def check_channel_request(self, kind, chanid):
                    return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

                def check_channel_exec_request(self, channel, command):
                    commands.append(command.decode())
                    def execute():
                        process = None
                        try:
                            process = subprocess.Popen(command.decode(), shell=True, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(base), env=env)
                            def input_stream():
                                try:
                                    while True:
                                        chunk = channel.recv(65536)
                                        if not chunk: break
                                        process.stdin.write(chunk)
                                        process.stdin.flush()
                                except (OSError, EOFError):
                                    pass
                                finally:
                                    try: process.stdin.close()
                                    except OSError: pass
                            def output_stream(stream, send):
                                try:
                                    while True:
                                        chunk = os.read(stream.fileno(), 65536)
                                        if not chunk: break
                                        send(chunk)
                                except (OSError, EOFError):
                                    pass
                            writers = [threading.Thread(target=output_stream, args=(process.stdout, channel.sendall)),
                                       threading.Thread(target=output_stream, args=(process.stderr, channel.sendall_stderr))]
                            threading.Thread(target=input_stream, daemon=True).start()
                            for writer in writers: writer.start()
                            status = process.wait(timeout=45)
                            for writer in writers: writer.join(timeout=5)
                            channel.send_exit_status(status)
                            channel.close()
                        except Exception as error:
                            errors.append(str(error))
                            if process is not None and process.poll() is None:
                                process.kill()
                                process.wait()
                            channel.close()
                    threading.Thread(target=execute, daemon=True).start()
                    return True

            def serve():
                while not stopped.is_set():
                    try:
                        connection, address = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        return
                    connected.append(address)
                    transport = paramiko.Transport(connection)
                    transports.append(transport)
                    transport.add_server_key(host_key)
                    try:
                        transport.start_server(server=Server())
                    except Exception as error:
                        if not stopped.is_set():
                            errors.append(str(error))

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            control = str(base / 'mux')
            host = 'fixture@127.0.0.1'
            options = ['-F', os.devnull, '-p', str(port), '-i', str(identity), '-o', 'IdentitiesOnly=yes',
                '-o', 'IdentityAgent=none', '-o', 'UserKnownHostsFile=' + str(known), '-o', 'GlobalKnownHostsFile=' + os.devnull,
                '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2']
            master = subprocess.Popen(['ssh'] + options + ['-S', control, '-M', '-N', host],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not os.path.exists(control) and master.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                diagnostic = master.stderr.read().decode() if master.poll() is not None else ''
                if 'Operation not permitted' in diagnostic and 'cannot bind to path' in diagnostic:
                    self.skipTest('host forbids OpenSSH control sockets; native gate runs on Entropy')
                self.assertTrue(os.path.exists(control), diagnostic)
                remote, local = data_base / "remote path's ", data_base / 'local'
                for root in (remote, local):
                    root.mkdir()
                    (root / '.hpclib-sync.json').write_text(json.dumps({'v': 1, 'git': {
                        'default_excludes': 'sync-only', 'default_excludes_version': 1}}))
                (remote / 'remote.txt').write_text('from remote\n')
                (local / 'local.txt').write_text('from local\n')
                login = Logins()
                login.CONTROL_PATH = control
                # No such jump host is contacted; the existing master already owns the route.
                profile = {'name': 'fixture', 'host': host, 'login': options + ['-J', 'never-contact.invalid', host]}
                request = {'protocol': 1, 'root': str(remote), 'group': 's-wire', 'endpoint': 't-remote'}
                with mock.patch.dict(os.environ, {'HPCLIB_REMOTE_INSTALL_LOCATION': str(ROOT / 'hpclib'),
                                                  'XDG_CONFIG_HOME': str(base / 'xdg'),
                                                  'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}):
                    self.assertTrue(login.alive(profile))
                    preview = login.sync_call(profile, dict(request, operation='preview'))
                    self.assertEqual(preview['state'], 'snapshot-ready')
                    snapshot = login.sync_call(profile, dict(request, operation='snapshot'))
                    packet = login.sync_call(profile, dict(request, operation='export'))
                    local_store = GitStore(str(local), str(data_base / 'local-state'), 's-wire')
                    local_snapshot = local_store.snapshot()
                    fetched = local_store.import_bundle(packet, 'remote', 't-remote')
                    self.assertEqual(fetched['commit'], snapshot['commit'])
                    self.assertEqual(local_store.text('show', snapshot['commit'] + ':remote.txt'), 'from remote')
                    incoming = login.sync_call(profile, dict(request, operation='import', run='r-test', packet=local_store.export_bundle()))
                    self.assertEqual(incoming['commit'], local_snapshot['commit'])
                    self.assertEqual((remote / 'remote.txt').read_text(), 'from remote\n')
                    self.assertFalse((remote / 'local.txt').exists())
                    with local_store.locked():
                        local_store.prepare()
                        merged = local_store.merge_candidate(local_snapshot['commit'], snapshot['commit'])
                    self.assertEqual(merged['state'], 'clean')
                    candidate = merged['commit']
                    login.sync_call(profile, dict(request, operation='import', run='r-sync', packet=local_store.export_bundle(candidate)))
                    ready = login.sync_call(profile, dict(request, operation='validate-apply', checkpoint=snapshot['checkpoint'],
                                                        candidate=candidate, plan='p-wire'))
                    self.assertEqual(ready['state'], 'apply-ready')
                    local_store.apply(local_snapshot['checkpoint'], candidate, 'p-wire')
                    ack = login.sync_call(profile, dict(request, operation='apply', checkpoint=snapshot['checkpoint'],
                                                       candidate=candidate, plan='p-wire', confirm=True))
                    self.assertEqual(ack['phase'], 'acknowledged')
                    self.assertEqual((remote / 'local.txt').read_text(), 'from local\n')
                    self.assertEqual((local / 'remote.txt').read_text(), 'from remote\n')
                    # Agent requests traverse the same native SSH master and invoke the real manager.
                    from sync_manager import SyncManager
                    from test_sync_manager import write_private
                    from test_sync_spec import spec
                    a_local, a_remote = data_base / 'agent local', data_base / 'agent remote'
                    for root in (a_local, a_remote):
                        root.mkdir()
                        (root / '.hpclib-sync.json').write_text(json.dumps({'v': 1, 'git': {
                            'default_excludes': 'sync-only', 'default_excludes_version': 1}}))
                    (a_local / 'a-local.txt').write_text('local agent data\n')
                    (a_remote / 'a-remote.txt').write_text('remote agent data\n')
                    manager = SyncManager(str(data_base / 'agent-console'), lambda name: profile, write_private,
                                          lambda p, req: login.sync_call(profile, req), login.sync_transfer)
                    try:
                        body = spec(str(a_local))
                        body['targets'] = [{'cluster': 'fixture', 'remote': str(a_remote)}]
                        saved = manager.save(body)
                        setup = manager.provision_preview(saved['spec']['id'])
                        manager.provision_apply(saved['spec']['id'], dict(revision=setup['revision'], review=setup['review'], action='private-git', confirm=True))
                        sid, revision = saved['spec']['id'], saved['spec']['revision']
                        manager.agent_access(sid, {'revision': revision, 'allow': True, 'confirm': True})
                        manager.agent_pump()
                        scope = base / 'agent-scope.json'
                        scope.write_text(json.dumps({'roots': [str(data_base)], 'deny': [str(data_base / 'remote-state')],
                                                    'actor': 'wire-agent', 'file_changes': True}))
                        def command(operation, plan=None):
                            args = [sys.executable, str(ROOT / 'hpclib/servers/sync_context.py'), 'agent',
                                '--command', operation, '--scope', str(scope), '--root', str(a_remote),
                                '--state', str(data_base / 'remote-state'), '--id', sid, '--revision', str(revision), '--confirm', 'true']
                            if plan: args += ['--plan', plan]
                            item = json.loads(subprocess.check_output(args, env=env))
                            manager.agent_pump()
                            args = [sys.executable, str(ROOT / 'hpclib/servers/sync_context.py'), 'agent',
                                '--command', 'request-status', '--scope', str(scope), '--root', str(a_remote),
                                '--state', str(data_base / 'remote-state'), '--ticket', item['ticket']]
                            reply = json.loads(subprocess.check_output(args, env=env))
                            self.assertEqual(reply['state'], 'succeeded', reply)
                            return reply['reply']['result']
                        self.assertEqual(command('list')['syncs'][0]['id'], sid)
                        command('review')
                        deadline = time.monotonic() + 40
                        while manager.busy() and time.monotonic() < deadline: time.sleep(.02)
                        reviewed = manager.get(sid)
                        self.assertEqual(reviewed['status']['state'], 'needs-sync-review', reviewed['status'])
                        command('apply', reviewed['status']['plan'])
                        deadline = time.monotonic() + 40
                        while manager.busy() and time.monotonic() < deadline: time.sleep(.02)
                        self.assertEqual(manager.get(sid)['status']['state'], 'in-sync')
                        self.assertEqual((a_local / 'a-remote.txt').read_text(), 'remote agent data\n')
                        self.assertEqual((a_remote / 'a-local.txt').read_text(), 'local agent data\n')
                        upload = dict(manager.get(sid)['spec'])
                        upload.update(direction='push', interval=10, policy={'background': True, 'on_change': 'auto-push', 'merge': 'review'})
                        saved = manager.save(upload, sid)
                        revision = saved['spec']['revision']
                        (a_remote / 'cluster-result.dat').write_text('stay remote\n')
                        command('review')
                        deadline = time.monotonic() + 40
                        while manager.busy() and time.monotonic() < deadline: time.sleep(.02)
                        reviewed = manager.get(sid)
                        self.assertEqual(reviewed['status']['state'], 'needs-sync-review', reviewed['status'])
                        command('apply', reviewed['status']['plan'])
                        deadline = time.monotonic() + 40
                        while manager.busy() and time.monotonic() < deadline: time.sleep(.02)
                        self.assertEqual(manager.get(sid)['status']['state'], 'uploaded')
                        self.assertFalse((a_local / 'cluster-result.dat').exists())
                        manager.clock = lambda: 100
                        manager.schedule_pump()
                        (a_local / 'a-local.txt').write_text('automatic local edit\n')
                        before_uploads = sum('--git-upload-pack' in c for c in commands)
                        manager.clock = lambda: 111
                        manager.schedule_pump()
                        deadline = time.monotonic() + 40
                        while manager.busy() and time.monotonic() < deadline: time.sleep(.02)
                        self.assertEqual(manager.get(sid)['status']['state'], 'uploaded', manager.get(sid)['status'])
                        self.assertEqual((a_remote / 'a-local.txt').read_text(), 'automatic local edit\n')
                        self.assertEqual(sum('--git-upload-pack' in c for c in commands), before_uploads)
                        self.assertFalse((a_local / 'cluster-result.dat').exists())
                    finally:
                        manager.close()
                    self.assertEqual(len(connected), 1)
                    self.assertTrue(any('--git-upload-pack' in c for c in commands))
                    self.assertTrue(any('--git-receive-pack' in c for c in commands))
                    native_offer = login.sync_call(profile, dict(request, operation='offer', commit=candidate))
                    # It can die between the liveness check and the command: no new authenticated channel may open.
                    self.assertTrue(login.alive(profile))
                    master.terminate()
                    master.wait(timeout=5)
                    with self.assertRaises(SyncError) as error:
                        login.sync_call(profile, dict(request, operation='preview'))
                    self.assertEqual(error.exception.payload['state'], 'login-needed')
                    for operation in ('fetch-objects', 'push-objects'):
                        with self.assertRaises(SyncError) as error:
                            login.sync_transfer(profile, local_store, dict(request, operation=operation,
                                offer=native_offer, commit=candidate, run='r-dead', settings={}, binding={}))
                        self.assertEqual(error.exception.payload['state'], 'login-needed')
                    with self.assertRaises(SyncError) as error:
                        login.sync_call(profile, {'protocol': 1, 'operation': 'agent-poll', 'consumer': 'c-closed'})
                    self.assertEqual(error.exception.payload['state'], 'login-needed')
                    self.assertEqual(len(connected), 1)
                    self.assertFalse(errors, errors)
            finally:
                if master.poll() is None:
                    master.kill()
                    master.wait(timeout=5)
                master.stdout.close()
                master.stderr.close()
                stopped.set()
                listener.close()
                for transport in transports:
                    transport.close()
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
