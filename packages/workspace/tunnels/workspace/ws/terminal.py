"""
Terminals that outlive the browser tab. A session is a login shell in a pseudo-terminal that the server keeps running
on its own; a page attaches to it over a WebSocket and detaches by closing, and the shell stays. What the shell printed
while nobody watched is kept in a ring buffer (the last 256 KiB), so attaching again shows the screen's recent history
before the live output. The page remembers its sessions' ids and attaches again after a reload, a lost connection or a
laptop that slept. A session ends when its shell exits or is closed; all of them end with the Workspace.

  Sessions()
    .create(cwd, cols, rows)     -> Session    (at most 16 alive)
    .get(id)                     -> Session, or Refused(404)
    .listing()                   -> [{id, cwd, created, alive, title, attached, last_output}]
    .close(id)                   hang the shell up
    .close_all()
  Session.attach(wfile)          replay the buffer, then follow live output; returns an Attachment (detach() to leave)

The WebSocket framing (ws_accept, ws_send, ws_read) is here too; frames are binary for the terminal's bytes and JSON text
for {"type": "resize", "cols", "rows"} in and {"type": "session", "id"}, {"type": "exit", "code"} out.

Python 3.6 or newer, standard library only.
"""
import base64
import hashlib
import json
import os
import secrets
import signal
import struct
import subprocess
import threading
import time

from .common import Refused

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
BUFFER_BYTES = 256 * 1024
MAX_SESSIONS = 16
ID_LENGTH = 16


def ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_send(wfile, opcode, payload, lock):
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 1 << 16:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    with lock:
        wfile.write(head + payload)
        wfile.flush()


def _read_exact(rfile, n):
    data = b""
    while len(data) < n:
        chunk = rfile.read(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def ws_read(rfile, limit=1 << 20):
    """(opcode, payload) of the next whole message."""
    opcode, parts = None, []
    while True:
        b0, b1 = _read_exact(rfile, 2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _read_exact(rfile, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _read_exact(rfile, 8))[0]
        if n > limit:
            raise EOFError
        mask = _read_exact(rfile, 4) if b1 & 0x80 else None
        data = _read_exact(rfile, n)
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        op = b0 & 0x0F
        if op >= 0x8:
            return op, data
        if op:
            opcode = op
        parts.append(data)
        if b0 & 0x80:
            return opcode, b"".join(parts)


class Attachment:
    """One page's WebSocket on a session."""

    def __init__(self, session, wfile):
        self.session, self.wfile, self.lock, self.open = session, wfile, threading.Lock(), True

    def send(self, opcode, payload):
        if not self.open:
            return False
        try:
            ws_send(self.wfile, opcode, payload, self.lock)
            return True
        except (OSError, ValueError):
            self.open = False
            return False

    def detach(self):
        self.open = False
        self.session.detach(self)


class Session:
    """A login shell in a pseudo-terminal of its own (a session whose controlling terminal it is)."""

    def __init__(self, cwd, cols, rows):
        import fcntl
        import pty
        import termios
        self.id = secrets.token_hex(ID_LENGTH // 2)
        self.cwd, self.created, self.last_output = cwd, time.time(), None
        self.exit_code = None
        self.buffer = bytearray()
        self.attached = []
        self.lock = threading.Lock()
        master, slave = pty.openpty()
        self._resize(slave, cols, rows)
        shell = os.environ.get("SHELL") or "/bin/bash"

        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
        env.pop("WORKSPACE_TOKEN", None)
        try:
            self.proc = subprocess.Popen([shell, "-l"], stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                                         close_fds=True, preexec_fn=controlling_terminal)
        finally:
            os.close(slave)
        self.master = master
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()

    @staticmethod
    def _resize(fd, cols, rows):
        import fcntl
        import termios
        cols = max(10, min(int(cols), 1000))
        rows = max(2, min(int(rows), 500))
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    @property
    def alive(self):
        return self.exit_code is None

    def resize(self, cols, rows):
        try:
            self._resize(self.master, cols, rows)
        except (OSError, ValueError, TypeError):
            pass

    def write(self, data):
        try:
            os.write(self.master, data)
        except OSError:
            pass

    def _pump(self):
        """Read the shell's output for as long as it lives: into the buffer, and to whoever is attached."""
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            with self.lock:
                self.buffer += data
                if len(self.buffer) > BUFFER_BYTES:
                    del self.buffer[:len(self.buffer) - BUFFER_BYTES]
                self.last_output = time.time()
                listeners = list(self.attached)
            for a in listeners:
                if not a.send(0x2, data):
                    self.detach(a)
        code = self.proc.wait()
        with self.lock:
            self.exit_code = code
            listeners, self.attached = list(self.attached), []        # told, and let go: nothing more will come
        for a in listeners:
            a.send(0x1, json.dumps({"type": "exit", "code": code}).encode())
            a.send(0x8, struct.pack(">H", 1000))
        try:
            os.close(self.master)
        except OSError:
            pass

    def attach(self, wfile):
        """Tell the page the session's id, replay what the buffer holds, then follow the shell's output."""
        a = Attachment(self, wfile)
        a.send(0x1, json.dumps({"type": "session", "id": self.id, "alive": self.alive, "created": self.created}).encode())
        with self.lock:
            backlog = bytes(self.buffer)
            self.attached.append(a)
        if backlog:
            a.send(0x2, backlog)
        if not self.alive:
            a.send(0x1, json.dumps({"type": "exit", "code": self.exit_code}).encode())
        return a

    def detach(self, attachment):
        with self.lock:
            if attachment in self.attached:
                self.attached.remove(attachment)

    def title(self):
        """What the shell is doing now: its working directory (the process's own, read from /proc where there is one)."""
        try:
            return os.readlink("/proc/{}/cwd".format(self.proc.pid))
        except OSError:
            return self.cwd

    def hangup(self):
        if self.proc.poll() is None:
            for sig in (signal.SIGHUP, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sig)
                except OSError:
                    pass
                try:
                    self.proc.wait(3 if sig == signal.SIGHUP else 5)
                    break
                except subprocess.TimeoutExpired:
                    continue
        self.reader.join(2)


class Sessions:
    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def _reap(self):
        """Forget sessions whose shell has exited and that nobody is attached to."""
        for sid in [k for k, s in self._sessions.items() if not s.alive and not s.attached]:
            del self._sessions[sid]

    def create(self, cwd, cols=100, rows=30):
        with self._lock:
            self._reap()
            if sum(1 for s in self._sessions.values() if s.alive) >= MAX_SESSIONS:
                raise Refused(429, "{} terminals are already open: close one first".format(MAX_SESSIONS))
            session = Session(cwd, cols, rows)
            self._sessions[session.id] = session
            return session

    def get(self, sid):
        with self._lock:
            session = self._sessions.get(sid)
        if session is None:
            raise Refused(404, "that terminal has ended (or the Workspace was restarted)")
        return session

    def listing(self):
        with self._lock:
            self._reap()
            sessions = list(self._sessions.values())
        return [{"id": s.id, "cwd": s.cwd, "title": s.title(), "created": s.created, "alive": s.alive,
                 "attached": len(s.attached), "last_output": s.last_output} for s in sessions]

    def close(self, sid):
        session = self.get(sid)
        session.hangup()
        with self._lock:
            self._sessions.pop(sid, None)
        return {"closed": sid}

    def close_all(self):
        with self._lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        for s in sessions:
            s.hangup()


def bridge(session, rfile, wfile):
    """Serve one WebSocket: the page's keystrokes in, the session's output out, until either side leaves. Leaving
    detaches; it doesn't close the session."""
    attachment = session.attach(wfile)
    try:
        while True:
            op, data = ws_read(rfile)
            if op == 0x8:
                break
            if op == 0x9:
                attachment.send(0xA, data)
            elif op == 0x2:
                session.write(data)
            elif op == 0x1:
                try:
                    msg = json.loads(data.decode())
                except ValueError:
                    continue
                if msg.get("type") == "resize":
                    session.resize(msg.get("cols", 100), msg.get("rows", 30))
    except (EOFError, OSError):
        pass
    finally:
        attachment.detach()
