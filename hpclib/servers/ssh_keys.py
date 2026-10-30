#!/usr/bin/env python3
"""Targeted, owner-managed SSH files in the cluster user's shared ~/.ssh.

No private key material is returned. Native OpenSSH validates public keys and
creates key pairs. --setup and --authorize also run over the login before REST starts.
Generated keys have no passphrase, for unattended cluster services. Existing keys
are never replaced; encrypted keys with their public files can be reused.
"""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

COMPUTE_KEY = "id_ed25519_hpclib_compute"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
HOST_RE = re.compile(r"[!A-Za-z0-9*?.:_\[\]-]{1,253}")
USER_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
RESERVED = {"config", "authorized_keys", "authorized_keys2", "known_hosts", "known_hosts2", "hpclib_keys.conf"}
MAX_FILE = 1 << 20
PRIVATE_HEADER = re.compile(br"-----BEGIN (?:OPENSSH|RSA|EC|DSA|ENCRYPTED)? ?PRIVATE KEY-----")
INCLUDE = '# hpclib SSH key bindings\nInclude "~/.ssh/hpclib_keys.conf"\nHost *\n'


class SSHKeyError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def revision(text):
    return hashlib.sha256(text.encode()).hexdigest()


def comment_value(value):
    if not isinstance(value, str) or len(value) > 256 or any(c in value for c in "\0\r\n"):
        raise SSHKeyError(422, "comment must be one line of at most 256 characters")
    return value


class SSHKeyStore:
    def __init__(self, home=None):
        self.home = Path(home or os.path.expanduser("~")).absolute()
        self.root = self.home / ".ssh"
        self._lock = threading.RLock()
        self._depth = 0

    def _path(self, name):
        return self.root / name

    @staticmethod
    def name(name):
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in RESERVED or name.endswith(".pub"):
            raise SSHKeyError(422, "key name must be a simple SSH filename, not a path or a reserved file")
        return name

    def _check_root(self, create=False):
        if self.root.is_symlink():
            raise SSHKeyError(409, "~/.ssh is a symlink; SSH management requires the shared home directory itself")
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.exists():
            info = self.root.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise SSHKeyError(403, "~/.ssh must be a directory owned by this user")

    def _read(self, path, limit=MAX_FILE):
        self._check_root()
        try:
            fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise SSHKeyError(409, "SSH management does not follow symlinks or special files") from e
        with os.fdopen(fd, "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise SSHKeyError(403, "SSH file must be a regular file owned by this user")
            data = f.read(limit + 1)
        if len(data) > limit:
            raise SSHKeyError(413, "SSH file exceeds the size limit")
        return data

    def _text(self, name):
        raw = self._read(self._path(name))
        try:
            return raw.decode("utf-8") if raw is not None else ""
        except UnicodeDecodeError as e:
            raise SSHKeyError(422, "SSH configuration is not UTF-8 text") from e

    @contextlib.contextmanager
    def locked(self):
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._check_root(create=True)
            try:
                fd = os.open(str(self._path(".hpclib.lock")), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            except OSError as e:
                raise SSHKeyError(409, "SSH management lock is unavailable; symlinks are not followed") from e
            acquired = False
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise SSHKeyError(403, "SSH management lock must be a regular file owned by this user")
                fcntl.flock(fd, fcntl.LOCK_EX)
                acquired = True
                self._depth = 1
                yield
            finally:
                self._depth = 0
                if acquired:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _write(self, path, data, before=None, mode=0o600):
        if self._read(path) != before:
            raise SSHKeyError(409, "SSH file changed; refresh before editing it")
        fd, temporary = tempfile.mkstemp(prefix=".hpclib-write-", dir=str(self.root))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temporary, mode)
            if self._read(path) != before:
                raise SSHKeyError(409, "SSH file changed; refresh before editing it")
            if before is None:
                try:
                    os.link(temporary, str(path))  # never overwrite a concurrently created file
                except FileExistsError as e:
                    raise SSHKeyError(409, "SSH file already exists; refresh before editing it") from e
            else:
                os.replace(temporary, str(path))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _run(args, input=None):
        try:
            result = subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, timeout=20,
                                    env={k: v for k, v in os.environ.items() if not k.startswith("HPC_REST_TOKEN")})
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SSHKeyError(503, "OpenSSH ssh-keygen is unavailable or timed out") from e
        if result.returncode:
            raise SSHKeyError(422, "OpenSSH could not read this key; check its format and public key file")
        return result.stdout

    def public_details(self, line):
        if not isinstance(line, str) or len(line) > 16384 or any(c in line for c in "\0\r\n"):
            raise SSHKeyError(422, "supply exactly one public key line")
        try:
            match = re.search(r'(?:^|\s)((?:ssh-|ecdsa-|sk-)[\w@.+-]+)\s+([A-Za-z0-9+/=]+)(?:\s+(.*))?$', line)
            if not match:
                raise ValueError("missing key fields")
            algorithm, encoded, comment = match.groups()
            blob = base64.b64decode(encoded, validate=True)
            size = struct.unpack(">I", blob[:4])[0]
            if blob[4:4 + size].decode() != algorithm:
                raise ValueError("key type differs from encoded key")
        except (ValueError, UnicodeError, struct.error) as e:
            raise SSHKeyError(422, "invalid SSH public key") from e
        # OpenSSH also checks the complete encoded key, not just its first field.
        info = self._run(["ssh-keygen", "-l", "-E", "sha256", "-f", "/dev/stdin"], input=line + "\n")
        fingerprint = info.split()[1]
        return {"algorithm": algorithm, "fingerprint": fingerprint,
                "comment": comment or "", "options": line[:match.start()].strip(),
                "public_key": algorithm + " " + encoded + (" " + comment if comment else "")}

    def key(self, name):
        name = self.name(name)
        raw = self._read(self._path(name + ".pub"), 16384)
        private = self._read(self._path(name))
        if raw is None and private is None:
            raise SSHKeyError(404, "SSH key not found")
        if private is not None and not PRIVATE_HEADER.match(private):
            raise SSHKeyError(422, "selected file is not a private SSH key; file was preserved")
        out = {"name": name, "private_available": private is not None, "compute": name == COMPUTE_KEY}
        if raw is not None:
            try:
                out.update(self.public_details(raw.decode("utf-8").strip()))
            except (SSHKeyError, UnicodeError):
                out["error"] = "public key file is invalid"
        else:
            out["error"] = "public key file is missing"
        return out

    def keys(self):
        self._check_root()
        names = set()
        if self.root.exists():
            for path in self.root.iterdir():
                name = path.name[:-4] if path.name.endswith(".pub") else path.name
                try:
                    self.name(name)
                except SSHKeyError:
                    continue
                if path.name.endswith(".pub"):
                    names.add(name)
                elif path.is_file() and not path.is_symlink():
                    raw = self._read(path)
                    if raw and PRIVATE_HEADER.match(raw):
                        names.add(name)
        keys, errors = [], []
        for name in sorted(names):
            try:
                keys.append(self.key(name))
            except SSHKeyError as e:
                errors.append({"name": name, "error": str(e)})
        return {"directory": str(self.root), "keys": keys, "errors": errors}

    def create(self, name, algorithm="ed25519", comment=""):
        name = self.name(name)
        comment = comment_value(comment)
        if algorithm not in ("ed25519", "rsa"):
            raise SSHKeyError(422, "key type must be ed25519 or rsa")
        with self.locked():
            private, public = self._path(name), self._path(name + ".pub")
            if os.path.lexists(str(private)) or os.path.lexists(str(public)):
                raise SSHKeyError(409, "SSH key already exists; existing key files are never replaced")
            with tempfile.TemporaryDirectory(prefix=".hpclib-keygen-", dir=str(self.root)) as folder:
                staged = Path(folder) / "key"
                args = ["ssh-keygen", "-q", "-t", algorithm, "-N", "", "-C", comment, "-f", str(staged)]
                if algorithm == "rsa":
                    args += ["-b", "3072"]
                self._run(args)
                self._write(private, staged.read_bytes())
                try:
                    self._write(public, staged.with_suffix(".pub").read_bytes(), mode=0o644)
                except Exception:
                    # Roll back only the private file just created by this operation.
                    if self._read(private) == staged.read_bytes():
                        private.unlink()
                    raise
            return self.key(name)

    def update_comment(self, name, comment):
        comment = comment_value(comment)
        name = self.name(name)
        with self.locked():
            self.key(name)
            path = self._path(name + ".pub")
            old = self._read(path, 16384)
            if old is None:
                raise SSHKeyError(404, "public key file not found")
            details = self.public_details(old.decode().strip())
            if details["options"]:
                raise SSHKeyError(422, "identity public files cannot contain authorized-key options")
            fields = details["public_key"].split(None, 2)
            new = fields[0] + " " + fields[1] + (" " + comment if comment else "") + "\n"
            self._write(path, new.encode(), old, mode=0o644)
            return self.key(name)

    def delete(self, name):
        name = self.name(name)
        with self.locked():
            self.key(name)  # a filename alone cannot select an unrelated private file
            references = [h["host"] for h in self.hosts()["hosts"] if h.get("key") == name]
            if references:
                raise SSHKeyError(409, "remove host bindings for this key first: " + ", ".join(references))
            paths = [self._path(name), self._path(name + ".pub")]
            found = False
            for path in paths:
                if self._read(path) is not None:
                    found = True
            if not found:
                raise SSHKeyError(404, "SSH key not found")
            for path in paths:
                if path.exists():
                    path.unlink()
            return {"deleted": name}

    def authorized(self):
        text = self._text("authorized_keys")
        entries, errors = [], []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                details = self.public_details(line)
                details.update(id=revision(line), line=line, line_number=number)
                entries.append(details)
            except SSHKeyError:
                errors.append({"line_number": number, "error": "unrecognized entry; preserved"})
        return {"entries": entries, "errors": errors, "revision": revision(text)}

    def change_authorized(self, line=None, entry=None, expected=None, delete=False):
        if not delete:
            self.public_details(line)
        with self.locked():
            path = self._path("authorized_keys")
            old = self._read(path)
            text = self._text("authorized_keys")
            if expected is not None and expected != revision(text):
                raise SSHKeyError(409, "authorized_keys changed; refresh before editing it")
            lines = text.splitlines(keepends=True)
            if entry is not None:
                matches = [i for i, value in enumerate(lines) if revision(value.rstrip("\r\n")) == entry]
                if not matches:
                    raise SSHKeyError(404, "authorized-key entry not found")
                if len(matches) != 1:
                    raise SSHKeyError(409, "identical entries exist; edit this duplicate in authorized_keys directly")
                lines[matches[0]] = "" if delete else line + "\n"
            elif delete:
                raise SSHKeyError(422, "entry id is required")
            elif any(value.rstrip("\r\n") == line for value in lines):
                return self.authorized()
            else:
                if lines and not lines[-1].endswith(("\n", "\r")):
                    lines[-1] += "\n"
                lines.append(line + "\n")
            self._write(path, "".join(lines).encode(), old)
            return self.authorized()

    def _host_blocks(self, text, managed):
        lines = text.splitlines(keepends=True)
        starts = [i for i, line in enumerate(lines) if re.match(r"\s*(Host|Match)(?:\s|=)", line, re.I)]
        result = []
        for n, start in enumerate(starts):
            end = starts[n + 1] if n + 1 < len(starts) else len(lines)
            try:
                header = shlex.split(re.sub(r'^\s*(\w+)\s*=\s*', r'\1 ', lines[start]), comments=True)
                if header[0].lower() != "host":
                    continue
                item = {"host": " ".join(header[1:]), "identity_files": [], "managed": managed,
                        "start": start, "end": end}
                for line in lines[start + 1:end]:
                    tokens = shlex.split(re.sub(r'^\s*(\w+)\s*=\s*', r'\1 ', line), comments=True)
                    if len(tokens) < 2:
                        continue
                    field, value = tokens[0].lower(), " ".join(tokens[1:])
                    if field == "identityfile":
                        item["identity_files"].append(value)
                    elif field in ("hostname", "user", "port", "identitiesonly"):
                        item[field] = value
                for identity in item["identity_files"]:
                    for prefix in ("~/.ssh/", str(self.root) + "/", "%d/.ssh/"):
                        if identity.startswith(prefix):
                            item["key"] = identity[len(prefix):]
                result.append(item)
            except ValueError:
                continue
        return result

    def hosts(self):
        managed = self._text("hpclib_keys.conf")
        ordinary = self._text("config")
        blocks = self._host_blocks(managed, True) + self._host_blocks(ordinary, False)
        for block in blocks:
            block.pop("start", None)
            block.pop("end", None)
        return {"hosts": blocks, "revision": revision(managed)}

    def set_host(self, host, key=None, user="", hostname="", port=None, expected=None, delete=False):
        if not isinstance(host, str) or any(c in host for c in "\0\r\n") or not host.split() or not all(HOST_RE.fullmatch(p) for p in host.split()):
            raise SSHKeyError(422, "host must be SSH host names or patterns")
        host = " ".join(host.split())
        if not delete:
            key = self.name(key)
            if not self.key(key)["private_available"]:
                raise SSHKeyError(422, "host identity needs a private key on this cluster")
            if user and (not isinstance(user, str) or not USER_RE.fullmatch(user)):
                raise SSHKeyError(422, "invalid SSH user name")
            if hostname and (not isinstance(hostname, str) or not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", hostname)):
                raise SSHKeyError(422, "invalid SSH host name")
            if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
                raise SSHKeyError(422, "SSH port must be 1 to 65535")
        with self.locked():
            path = self._path("hpclib_keys.conf")
            old = self._read(path)
            text = self._text("hpclib_keys.conf")
            if expected is not None and expected != revision(text):
                raise SSHKeyError(409, "host bindings changed; refresh before editing them")
            blocks = [b for b in self._host_blocks(text, True) if b["host"] == host]
            if len(blocks) > 1:
                raise SSHKeyError(409, "duplicate host blocks; edit hpclib_keys.conf directly")
            lines = text.splitlines(keepends=True)
            if delete and not blocks:
                raise SSHKeyError(404, "managed host binding not found")
            replacement = []
            if not delete:
                replacement = ["Host " + host + "\n", '    IdentityFile "~/.ssh/' + key + '"\n',
                               "    IdentitiesOnly yes\n"]
                for field, value in (("User", user), ("HostName", hostname), ("Port", port)):
                    if value:
                        replacement.append("    " + field + " " + str(value) + "\n")
            if blocks:
                block = blocks[0]
                # Preserve comments and directives outside the targeted identity settings.
                extras = [value for value in lines[block["start"] + 1:block["end"]]
                          if not re.match(r"\s*(IdentityFile|IdentitiesOnly|User|HostName|Port)(?:\s|=)", value, re.I)]
                lines[block["start"]:block["end"]] = replacement + extras
            elif not delete:
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                lines += replacement
            if not delete:
                config = self._path("config")
                previous = self._read(config)
                existing = self._text("config")
                if not existing.startswith(INCLUDE):
                    self._write(config, (INCLUDE + existing).encode(), previous)
            self._write(path, "".join(lines).encode(), old)
            return self.hosts()

    def authorize_login(self, line):
        """Authorize a workstation's public key without relaxing an existing entry's restrictions."""
        details = self.public_details(line)
        if details["options"]:
            raise SSHKeyError(422, "login setup takes a public key without authorization options")
        with self.locked():
            existing = [e for e in self.authorized()["entries"] if e["fingerprint"] == details["fingerprint"]]
            if not existing:
                self.change_authorized(details["public_key"])
            os.chmod(str(self._path("authorized_keys")), 0o600)
            os.chmod(str(self.root), 0o700)
            return {"authorized": True, "authorization_added": not existing,
                    "restrictions_preserved": any(e["options"] for e in existing),
                    "fingerprint": details["fingerprint"]}

    def setup_cluster(self):
        with self.locked():
            private = self._path(COMPUTE_KEY)
            public = self._path(COMPUTE_KEY + ".pub")
            created = False
            if not os.path.lexists(str(private)) and not os.path.lexists(str(public)):
                self.create(COMPUTE_KEY, comment="hpclib compute-node authentication")
                created = True
            elif self._read(private) is None:
                raise SSHKeyError(409, "compute public key exists without its private key; existing files were preserved")
            elif self._read(public) is None:
                value = self._run(["ssh-keygen", "-y", "-P", "", "-f", str(private)]).strip()
                self.public_details(value)
                self._write(public, (value + "\n").encode(), mode=0o644)
            details = self.key(COMPUTE_KEY)
            if details.get("error"):
                raise SSHKeyError(422, details["error"] + "; existing key pair was preserved")
            try:
                derived = self._run(["ssh-keygen", "-y", "-P", "", "-f", str(private)]).strip()
            except SSHKeyError as e:
                raise SSHKeyError(e.status, "compute setup needs a readable, unencrypted private key; existing files were preserved") from e
            if self.public_details(derived)["fingerprint"] != details["fingerprint"]:
                raise SSHKeyError(409, "compute private and public keys differ; existing files were preserved")
            authorized = self.authorized()
            already = any(entry["fingerprint"] == details["fingerprint"] for entry in authorized["entries"])
            if not already:
                self.change_authorized(details["public_key"])
            os.chmod(str(private), 0o600)
            os.chmod(str(self._path("authorized_keys")), 0o600)
            os.chmod(str(self.root), 0o700)
            return {"ok": True, "key": details, "created": created, "authorized": True,
                    "authorization_added": not already, "shared_home": str(self.home)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--setup", action="store_true", help="initialize or reuse the compute key in this shared home")
    mode.add_argument("--authorize", metavar="PUBLIC_KEY", help="authorize one workstation public key; preserve existing restrictions")
    args = parser.parse_args(argv)
    try:
        store = SSHKeyStore()
        print(json.dumps(store.authorize_login(args.authorize) if args.authorize is not None else store.setup_cluster()))
        return 0
    except SSHKeyError as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
