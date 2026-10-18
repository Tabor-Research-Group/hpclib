"""Owner-requested tunnel installations on the REST node, with asynchronous progress logs."""
import os
import secrets
import socket
import subprocess
import threading
import time

try:
    from . import rest_jobs, rest_sandbox
except ImportError:
    import rest_jobs
    import rest_sandbox


class TunnelInstalls:
    """Runs only setup_tunnel.sh --install/--check, never caller-supplied commands or paths."""

    def __init__(self):
        self.lock = threading.Lock()
        self.records = {}
        self.logs = {}

    def start(self, tunnel, force=False):
        with self.lock:
            for record in self.records.values():
                if record["tunnel"] == tunnel and record["state"] == "running":
                    if record["force"] != force:
                        raise rest_jobs.RESTError(409, f"an installation of {tunnel} is already running")
                    return dict(record, already_running=True)
            env = rest_sandbox.child_env()
            data = os.path.expanduser(env.get("HPCTUNNELS_DATA_DIR") or "~/.local/tunnels")
            directory = os.path.join(data, "installs")
            os.makedirs(directory, mode=0o700, exist_ok=True)
            ident = secrets.token_hex(16)
            log_path = os.path.join(directory, ident + ".log")
            script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "tunnels", "setup_tunnel.sh")
            command = ["bash", script, tunnel, "--install"] + (["--force"] if force else []) + ["--check"]
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as log:
                try:
                    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                            stderr=subprocess.STDOUT, env=env, start_new_session=True)
                except OSError as e:
                    raise rest_jobs.RESTError(503, f"could not start the tunnel installer: {e}")
            record = {"id": ident, "tunnel": tunnel, "force": force, "state": "running",
                      "started": time.time(), "finished": None, "exit_code": None,
                      "node": socket.gethostname()}
            self.records[ident], self.logs[ident] = record, log_path
            threading.Thread(target=self._wait, args=(record, proc), daemon=True).start()
            return dict(record)

    def _wait(self, record, proc):
        code = proc.wait()
        with self.lock:
            record.update(state="succeeded" if code == 0 else "failed", exit_code=code, finished=time.time())

    def status(self, ident, offset=0):
        with self.lock:
            if ident not in self.records:
                raise rest_jobs.RESTError(404, "no such installation on this REST server; was it restarted?")
            record, log_path = dict(self.records[ident]), self.logs[ident]
        with open(log_path, "rb") as log:
            size = os.fstat(log.fileno()).st_size
            if offset > size:
                raise rest_jobs.RESTError(422, "offset is past the installation log")
            log.seek(offset)
            data = log.read(65536)
        return dict(record, output=data.decode(errors="replace"), offset=offset + len(data),
                    more=offset + len(data) < size)
