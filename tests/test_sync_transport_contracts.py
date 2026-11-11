"""Optional real OpenSSH mux proof against a disposable loopback Paramiko server.

Paramiko is a test harness only. No actual cluster, ssh-agent, SSH config, or user key is read.
"""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

try:
    import paramiko
except ImportError:
    paramiko = None


@unittest.skipUnless(paramiko and shutil.which('ssh'), 'optional Paramiko/OpenSSH harness unavailable')
class TestSyncTransportContracts(unittest.TestCase):
    def test_live_master_reused_and_dead_master_cannot_open_new_key_login(self):
        with tempfile.TemporaryDirectory(prefix='hpclib-sync-ssh-') as temp:
            root = Path(temp)
            client_key = paramiko.RSAKey.generate(2048)
            host_key = paramiko.RSAKey.generate(2048)
            identity = root / 'identity'
            client_key.write_private_key_file(str(identity))
            identity.chmod(0o600)
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.listen(5)
            listener.settimeout(0.2)
            port = listener.getsockname()[1]
            known = root / 'known_hosts'
            known.write_text('[127.0.0.1]:%s %s %s\n' % (port, host_key.get_name(), host_key.get_base64()))
            stopped = threading.Event()
            connections = []
            transports = []
            errors = []

            class Server(paramiko.ServerInterface):
                def get_allowed_auths(self, username):
                    return 'publickey'

                def check_auth_publickey(self, username, key):
                    return paramiko.AUTH_SUCCESSFUL if username == 'fixture' and key == client_key else paramiko.AUTH_FAILED

                def check_channel_request(self, kind, chanid):
                    return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

                def check_channel_exec_request(self, channel, command):
                    def reply():
                        channel.send(b'master-only\n')
                        channel.send_exit_status(0)
                        time.sleep(0.05)
                        channel.close()
                    threading.Thread(target=reply, daemon=True).start()
                    return True

            def serve():
                while not stopped.is_set():
                    try:
                        connection, address = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        return
                    connections.append(address)
                    transport = paramiko.Transport(connection)
                    transports.append(transport)
                    transport.add_server_key(host_key)
                    try:
                        transport.start_server(server=Server())
                    except Exception as e:
                        if not stopped.is_set():
                            errors.append(str(e))

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            control = str(root / 'control')
            args = ['ssh', '-F', os.devnull, '-S', control, '-p', str(port), '-i', str(identity),
                    '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes',
                    '-o', 'UserKnownHostsFile=' + str(known), '-o', 'GlobalKnownHostsFile=' + os.devnull,
                    '-o', 'ConnectTimeout=2', '-o', 'ConnectionAttempts=1']
            master = subprocess.Popen(args + ['-M', '-N', 'fixture@127.0.0.1'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                limit = time.monotonic() + 10
                while not os.path.exists(control) and time.monotonic() < limit and master.poll() is None:
                    time.sleep(0.05)
                problem = master.stderr.read().decode() if master.poll() is not None else 'master failed'
                if not os.path.exists(control) and 'cannot bind to path' in problem and 'Operation not permitted' in problem:
                    self.skipTest('execution environment forbids OpenSSH control sockets; mux proof remains incomplete')
                self.assertTrue(os.path.exists(control), problem)
                command = args + ['-o', 'ControlMaster=no', '-o', 'ProxyCommand=false', 'fixture@127.0.0.1', 'fixture-command']
                first = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                self.assertEqual(first.returncode, 0, first.stderr.decode())
                self.assertEqual(first.stdout, b'master-only\n')
                self.assertEqual(len(connections), 1)
                master.terminate()
                master.wait(timeout=5)
                failed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                self.assertNotEqual(failed.returncode, 0)
                time.sleep(0.2)
                self.assertEqual(len(connections), 1, 'dead master opened a new authenticated connection')
                self.assertEqual(errors, [])
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
