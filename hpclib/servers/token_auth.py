"""Shared hpclib bearer credentials, grants, sessions and purpose-bound tickets.

Standard library only. Legacy REST token records remain readable; new grant and
session records fail closed in older verifiers. Secrets are returned only when
issued, and only hashes are stored. This module does not manage Kerberos or
credentials issued by third-party programs.
"""
import contextlib
import fcntl
import functools
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time


def tunnels_data_dir():
    return os.path.expanduser(os.environ.get("HPCTUNNELS_DATA_DIR", "~/.local/tunnels"))


def rest_data_dir():
    return os.path.join(tunnels_data_dir(), "rest")


def _tokens_locked(method):
    """Holds an exclusive lock beside the tokens file while `method` reads, changes and writes it (a revoke and a
    concurrent --add-token can't undo each other)."""
    @functools.wraps(method)
    def locked(cls, tokens_file, *args, **kwargs):
        os.makedirs(os.path.dirname(os.path.abspath(tokens_file)), mode=0o700, exist_ok=True)
        fd = os.open(tokens_file + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return method(cls, tokens_file, *args, **kwargs)
        finally:
            os.close(fd)
    return locked


class TokenIdentity:
    def __init__(self, name, scopes, allow=None, **metadata):
        self.name = name
        self.scopes = tuple(scopes)
        self.allow = allow
        self.__dict__.update(metadata)

    def has_scope(self, scope):
        return scope is None or "*" in self.scopes or scope in self.scopes

    def to_json(self):
        return dict(self.__dict__, scopes=list(self.scopes))


class TokenAuth:
    """Bearer-token checking plus loading/creating the tokens themselves."""

    TOKEN_ENV_VAR = 'HPC_REST_TOKEN'
    TOKEN_FILE_ENV_VAR = 'HPC_REST_TOKEN_FILE'
    TOKEN_FILE_NAME = 'rest_token'
    TOKEN_BYTES = 32
    HASH_PREFIX = "sha256:"
    OWNER = "owner"
    SCOPES = ("read", "submit", "propose", "files:write", "envs", "slurm", "*")
    NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

    def __init__(self, token, source=None, tokens_file=None):
        if not token:
            raise ValueError("an empty token would allow anyone in")
        if token.startswith(self.HASH_PREFIX):
            self.owner_hash = token[len(self.HASH_PREFIX):].strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", self.owner_hash):
                raise ValueError("owner token hash must be 64 hex digits")
        else:
            self.owner_hash = self.hash(token)
        self.source = source
        self.tokens_file = tokens_file
        self._tokens_cache = (None, [])
        self._lock = threading.Lock()

    @staticmethod
    def hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def scoped_tokens(self):
        """Entries of the tokens file, re-read whenever it changes."""
        if not self.tokens_file:
            return []
        try:
            mtime = os.stat(self.tokens_file).st_mtime_ns
        except FileNotFoundError:
            return []
        with self._lock:
            if self._tokens_cache[0] != mtime:
                entries = self.read_tokens_file(self.tokens_file)
                self._tokens_cache = (mtime, [e for e in entries if self.valid_entry(e)])
            return self._tokens_cache[1]

    @classmethod
    def valid_entry(cls, entry):
        if not isinstance(entry, dict) or entry.get("kind") is not None:
            return False
        scopes = entry.get("scopes", [])
        if not isinstance(entry.get("sha256"), str) or not cls.NAME_RE.fullmatch(str(entry.get("name", ""))):
            return False
        if not isinstance(scopes, list) or not scopes or any(s not in cls.SCOPES for s in scopes):
            return False
        # anything short of full access has to be pinned to directories
        if "*" not in scopes and not entry.get("allow"):
            print(f"ignoring token {entry['name']}: scoped tokens need an `allow` list", file=sys.stderr)
            return False
        return bool(re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]))

    def identify(self, header):
        if not header:
            return None
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            return None
        digest = self.hash(value.strip())
        if hmac.compare_digest(digest, self.owner_hash):
            return TokenIdentity(self.OWNER, ("*",))
        session = TokenRegistry(self.tokens_file).identify(value.strip()) if self.tokens_file else None
        if session is not None:
            return session
        for entry in self.scoped_tokens():
            if hmac.compare_digest(digest, entry["sha256"]):
                return TokenIdentity(entry["name"], entry["scopes"], entry.get("allow"))
        return None

    def check(self, header) -> bool:
        return self.identify(header) is not None

    @classmethod
    def default_token_file(cls):
        return os.path.join(tunnels_data_dir(), cls.TOKEN_FILE_NAME)

    @classmethod
    def default_tokens_file(cls):
        return os.path.join(rest_data_dir(), "tokens.json")

    @staticmethod
    def check_private(path):
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("credential files must be regular files (no symlinks)")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise PermissionError(f"{path} is not owned by the current user")
        if info.st_mode & 0o077:
            raise PermissionError(f"{path} is accessible by other users; run `chmod 600 {path}`")

    @classmethod
    def read_token_file(cls, token_file):
        cls.check_private(token_file)
        with open(token_file) as f:
            token = f.read().strip()
        if not token:
            raise ValueError(f"token file {token_file} is empty")
        return token

    @staticmethod
    def write_private(path, text):
        os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tokens.")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)  # mkstemp files are already 600

    @classmethod
    def create_token_file(cls, token_file):
        token = secrets.token_urlsafe(cls.TOKEN_BYTES)
        os.makedirs(os.path.dirname(os.path.abspath(token_file)), mode=0o700, exist_ok=True)
        # O_EXCL so two servers starting at once can't clobber each other
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        return token

    @classmethod
    def read_tokens_file(cls, tokens_file):
        cls.check_private(tokens_file)
        with open(tokens_file) as f:
            data = json.load(f)
        entries = data.get("tokens", []) if isinstance(data, dict) else None
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ValueError("invalid token registry")
        return entries

    @classmethod
    @_tokens_locked
    def add_token(cls, tokens_file, name, scopes, allow=None):
        if not cls.NAME_RE.fullmatch(name) or name == cls.OWNER:
            raise ValueError(f"invalid token name {name!r}")
        bad = [s for s in scopes if s not in cls.SCOPES]
        if not scopes or bad:
            raise ValueError(f"unknown scopes {bad}; choose from {cls.SCOPES}")
        if "*" not in scopes and not allow:
            raise ValueError("scoped tokens must be limited to directories with --token-allow")
        entries = cls.read_tokens_file(tokens_file) if os.path.exists(tokens_file) else []
        if any(e.get("name") == name for e in entries):
            raise ValueError(f"a token named {name!r} already exists; revoke it first")
        token = secrets.token_urlsafe(cls.TOKEN_BYTES)
        entry = {"name": name, "sha256": cls.hash(token), "scopes": list(scopes), "created": time.time()}
        if allow:
            entry["allow"] = [os.path.realpath(os.path.expanduser(a)) for a in allow]
        cls.write_private(tokens_file, json.dumps({"tokens": entries + [entry]}, indent=2) + "\n")
        return token

    @classmethod
    @_tokens_locked
    def revoke_token(cls, tokens_file, name):
        entries = cls.read_tokens_file(tokens_file) if os.path.exists(tokens_file) else []
        grant = next((e for e in entries if e.get("name") == name and e.get("kind") == "grant"), None)
        if grant is not None:
            grant["revoked_at"] = time.time()
            cls.write_private(tokens_file, json.dumps({"tokens": entries}, indent=2) + "\n")
            return
        kept = [e for e in entries if e.get("name") != name]
        if len(kept) == len(entries):
            raise ValueError(f"no token named {name!r}")
        cls.write_private(tokens_file, json.dumps({"tokens": kept}, indent=2) + "\n")

    @classmethod
    def lookup_token_hash(cls, tokens_file, digest):
        """The name of the scoped token whose sha256 is `digest`, or None."""
        entries = cls.read_tokens_file(tokens_file) if os.path.exists(tokens_file) else []
        for e in entries:
            if hmac.compare_digest(str(e.get("sha256", "")), digest):
                return e.get("name")
        return None

    @classmethod
    @_tokens_locked
    def revoke_token_hash(cls, tokens_file, digest):
        """Revoke the token whose sha256 is `digest`; returns its name."""
        entries = cls.read_tokens_file(tokens_file) if os.path.exists(tokens_file) else []
        names = [e.get("name") for e in entries if hmac.compare_digest(str(e.get("sha256", "")), digest)]
        if not names:
            raise ValueError("no token has that hash")
        cls.write_private(tokens_file, json.dumps({"tokens": [e for e in entries if e.get("name") not in names]},
                                                  indent=2) + "\n")
        return names[0]

    @classmethod
    @_tokens_locked
    def add_scopes_by_hash(cls, tokens_file, digest, scopes):
        """Add `scopes` to the token whose sha256 is `digest`; returns (name, scopes added)."""
        bad = [s for s in scopes if s not in cls.SCOPES]
        if not scopes or bad or "*" in scopes:
            raise ValueError(f"scopes must come from {[s for s in cls.SCOPES if s != '*']}")
        entries = cls.read_tokens_file(tokens_file) if os.path.exists(tokens_file) else []
        for e in entries:
            if e.get("kind") is None and hmac.compare_digest(str(e.get("sha256", "")), digest):
                added = [s for s in scopes if s not in e.get("scopes", [])]
                if added:
                    e["scopes"] = list(e.get("scopes", [])) + added
                    cls.write_private(tokens_file, json.dumps({"tokens": entries}, indent=2) + "\n")
                return e.get("name"), added
        raise ValueError("no token has that hash")

    @classmethod
    def hash_token_file(cls, token_file):
        token = cls.read_token_file(token_file)
        if token.startswith(cls.HASH_PREFIX):
            return False
        cls.write_private(token_file, cls.HASH_PREFIX + cls.hash(token) + "\n")
        return True

    @classmethod
    def load(cls, token_file=None, tokens_file=None):
        token = os.environ.pop(cls.TOKEN_ENV_VAR, None)  # keep it out of child processes
        if token:
            return cls(token, source=f"${cls.TOKEN_ENV_VAR}", tokens_file=tokens_file)
        if token_file is None:
            token_file = os.environ.get(cls.TOKEN_FILE_ENV_VAR) or cls.default_token_file()
        token_file = os.path.expanduser(token_file)
        try:
            token = cls.create_token_file(token_file)
        except FileExistsError:
            token = cls.read_token_file(token_file)
        return cls(token, source=token_file, tokens_file=tokens_file)


REST_AUDIENCE = "hpclib:rest"


def _duration(value, maximum=86400):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError("duration must be a finite positive number <= {} seconds".format(maximum))
    return value


def _roots(paths):
    if not isinstance(paths, (list, tuple)) or not paths or any(not isinstance(p, str) or not os.path.isabs(p) or "\0" in p for p in paths):
        raise ValueError("allow must contain absolute directory paths")
    return sorted(set(os.path.realpath(p) for p in paths))


def _within(path, root):
    return os.path.commonpath((path, root)) == root


def _intersection(left, right):
    return sorted(set(a if _within(a, b) else b for a in left for b in right
                      if _within(a, b) or _within(b, a)))


def _rights(scopes, allow):
    if not isinstance(scopes, (list, tuple)) or not scopes or any(s not in TokenAuth.SCOPES or s in ("*", "slurm") for s in scopes):
        raise ValueError("session scopes must be nonempty and cannot include * or raw slurm")
    return {"scopes": sorted(set(scopes)), "allow": _roots(allow)}


class TokenRegistry:
    """Durable grants and expiring bearer sessions for ONE audience.

    Issuance is privileged: callers must authorize it before calling create_grant
    or issue. delegate/rotate/revoke_self authenticate the presented credential.
    Every verification reads current records, including the entire parent chain.
    Legacy REST tokens remain a distinct, explicitly non-expiring credential type.
    """
    MAX_LEASE = 7 * 86400
    MAX_DEPTH = 8
    MAX_RECORDS = 10000
    MAX_ACTIVE_PER_GRANT = 128

    def __init__(self, path, audience=REST_AUDIENCE, clock=None):
        if not isinstance(audience, str) or not audience or len(audience) > 128:
            raise ValueError("invalid audience")
        self.path = os.fspath(path)
        self.audience = audience
        self.clock = clock or time.time

    def records(self):
        return TokenAuth.read_tokens_file(self.path) if os.path.exists(self.path) else []

    @contextlib.contextmanager
    def edit(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), mode=0o700, exist_ok=True)
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            records = self.records()
            yield records
            TokenAuth.write_private(self.path, json.dumps({"tokens": records}, indent=2) + "\n")
        finally:
            os.close(fd)

    def create_grant(self, name, scopes, allow):
        if not isinstance(name, str) or not TokenAuth.NAME_RE.fullmatch(name) or name == TokenAuth.OWNER:
            raise ValueError("invalid grant name")
        rights = _rights(scopes, allow)
        with self.edit() as records:
            if len(records) >= self.MAX_RECORDS:
                raise ValueError("token registry capacity reached; owner maintenance required")
            # Never reuse a principal name: jobs retain this stable ownership name.
            if any(r.get("name") == name for r in records):
                raise ValueError("name already used; choose a new grant name")
            grant = dict(kind="grant", id=secrets.token_hex(16), name=name,
                         audience=self.audience, rights=rights, created=self.clock())
            records.append(grant)
        return dict(grant)

    def _grant(self, records, grant_id):
        return next((r for r in records if r.get("kind") == "grant" and r.get("id") == grant_id
                     and r.get("audience") == self.audience and not r.get("revoked_at")), None)

    def _effective(self, records, session, seen=None):
        seen = set() if seen is None else set(seen)
        sid = session["id"]
        if sid in seen or len(seen) >= self.MAX_DEPTH:
            return None
        seen.add(sid)
        now = self.clock()
        expires, lease = session["expires"], session["lease_expires"]
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in (expires, lease)):
            return None
        if session.get("audience") != self.audience or session.get("revoked_at") or not now < expires <= lease:
            return None
        grant = self._grant(records, session["grant_id"])
        if grant is None:
            return None
        own = _rights(**session["rights"])
        current = _rights(**grant["rights"])
        # A granted root replaced by a symlink must not silently become a new root.
        if own["allow"] != session["rights"]["allow"] or current["allow"] != grant["rights"]["allow"]:
            return None
        scopes = set(own["scopes"]) & set(current["scopes"])
        allow = _intersection(own["allow"], current["allow"])
        parent_id = session.get("parent_id")
        if parent_id:
            parent = next((r for r in records if r.get("kind") == "session" and r.get("id") == parent_id), None)
            if parent is None or parent["grant_id"] != grant["id"] or parent.get("device_id") != session.get("device_id"):
                return None
            identity = self._effective(records, parent, seen)
            if identity is None or expires > parent["expires"] or lease > parent["lease_expires"]:
                return None
            scopes &= set(identity.scopes)
            allow = _intersection(allow, identity.allow)
        if not scopes or not allow:
            return None
        return TokenIdentity(grant["name"], sorted(scopes), allow, grant_id=grant["id"],
                             session_id=sid, expires=expires, lease_expires=lease,
                             audience=self.audience, device_id=session.get("device_id"))

    def _find(self, records, token):
        if not isinstance(token, str) or not token or len(token) > 4096:
            return None, None
        digest = TokenAuth.hash(token)
        for record in records:
            if record.get("kind") != "session" or not hmac.compare_digest(digest, str(record.get("sha256", ""))):
                continue
            try:
                return record, self._effective(records, record)
            except (KeyError, TypeError, ValueError, RecursionError):
                return None, None
        return None, None

    def identify(self, token):
        return self._find(self.records(), token)[1]

    def _issue(self, records, grant_id, ttl, lease, scopes, allow, parent=None, device_id=None):
        ttl, lease = _duration(ttl), _duration(lease, self.MAX_LEASE)
        if ttl > lease:
            raise ValueError("session duration exceeds login lease")
        grant = self._grant(records, grant_id)
        if grant is None:
            raise ValueError("grant missing or revoked")
        permitted = self._effective(records, parent) if parent else TokenIdentity(grant["name"], **grant["rights"])
        if permitted is None:
            raise ValueError("parent session is not active")
        rights = _rights(scopes if scopes is not None else permitted.scopes,
                         allow if allow is not None else permitted.allow)
        if not set(rights["scopes"]) <= set(permitted.scopes) or any(not any(_within(p, r) for r in permitted.allow) for p in rights["allow"]):
            raise PermissionError("delegation cannot widen scopes or directory access")
        if device_id is not None and (not isinstance(device_id, str) or not TokenAuth.NAME_RE.fullmatch(device_id)):
            raise ValueError("invalid device identifier")
        now = self.clock()
        active = sum(1 for r in records if r.get("kind") == "session" and r.get("grant_id") == grant_id
                     and not r.get("revoked_at") and isinstance(r.get("expires"), (int, float)) and r["expires"] > now)
        if len(records) >= self.MAX_RECORDS or active >= self.MAX_ACTIVE_PER_GRANT:
            raise ValueError("session capacity reached; revoke unused sessions or ask the owner to maintain the registry")
        expires, until = now + ttl, now + lease
        if parent:
            expires = min(expires, parent["expires"])
            until = min(until, parent["lease_expires"])
            device_id = parent.get("device_id")
            # Count before issuing so a child never starts out unusable.
            depth, ancestor = 1, parent
            while ancestor.get("parent_id"):
                depth += 1
                ancestor = next(r for r in records if r.get("id") == ancestor["parent_id"])
            if depth >= self.MAX_DEPTH:
                raise ValueError("delegation depth limit reached")
        token = secrets.token_urlsafe(TokenAuth.TOKEN_BYTES)
        record = dict(kind="session", id=secrets.token_hex(16), name="session-" + secrets.token_hex(12),
                      grant_id=grant_id, audience=self.audience, rights=rights, sha256=TokenAuth.hash(token),
                      created=now, expires=expires, lease_expires=until, device_id=device_id)
        if parent:
            record["parent_id"] = parent["id"]
        # No top-level scopes/allow: legacy verifiers must reject these records.
        records.append(record)
        return {"token": token, "session": self.public(record)}

    def update_grant(self, grant_id, scopes, allow):
        rights = _rights(scopes, allow)
        with self.edit() as records:
            grant = self._grant(records, grant_id)
            if grant is None:
                raise ValueError("grant missing or revoked")
            grant["rights"] = rights
        return self.public(grant)

    def issue(self, grant_id, ttl=900, lease=43200, scopes=None, allow=None, device_id=None):
        with self.edit() as records:
            return self._issue(records, grant_id, ttl, lease, scopes, allow, device_id=device_id)

    def delegate(self, token, ttl=900, scopes=None, allow=None):
        with self.edit() as records:
            parent, identity = self._find(records, token)
            if identity is None:
                raise PermissionError("active session required")
            return self._issue(records, identity.grant_id, ttl, self.MAX_LEASE, scopes, allow, parent=parent)

    def rotate(self, token, ttl=900):
        ttl = _duration(ttl)
        with self.edit() as records:
            record, identity = self._find(records, token)
            if identity is None:
                raise PermissionError("active session required; log in again if expired")
            until = record["lease_expires"]
            if record.get("parent_id"):
                parent = next(r for r in records if r.get("id") == record["parent_id"])
                until = min(until, parent["expires"])
            value = secrets.token_urlsafe(TokenAuth.TOKEN_BYTES)
            record["sha256"] = TokenAuth.hash(value)
            record["expires"] = min(self.clock() + ttl, until)
            return {"token": value, "session": self.public(record)}

    def revoke(self, record_id):
        with self.edit() as records:
            entry = next((r for r in records if r.get("id") == record_id and r.get("audience") == self.audience), None)
            if entry is None:
                raise ValueError("no matching grant or session")
            entry["revoked_at"] = self.clock() or 1e-9
        return {"revoked": record_id}

    def revoke_self(self, token):
        with self.edit() as records:
            record, identity = self._find(records, token)
            if identity is None:
                raise PermissionError("active session required")
            record["revoked_at"] = self.clock() or 1e-9
            return {"revoked": record["id"]}

    @staticmethod
    def public(record):
        return {k: v for k, v in record.items() if k != "sha256"}

    def list(self):
        return [self.public(r) for r in self.records() if r.get("kind") in ("grant", "session")
                and r.get("audience") == self.audience]


class ServiceToken:
    """One service's startup credential; never consults another service's grants."""
    def __init__(self, secret, audience):
        if not secret or not audience:
            raise ValueError("secret and audience are required")
        self.digest = TokenAuth.hash(secret)
        self.audience = audience

    def check(self, secret, audience):
        return (audience == self.audience and isinstance(secret, str) and bool(secret)
                and hmac.compare_digest(self.digest, TokenAuth.hash(secret)))


class CapabilityTickets:
    """Bounded, single-use, in-memory credentials bound to a purpose and resource."""
    def __init__(self, audience, clock=None, limit=4096):
        self.audience, self.clock, self.limit = audience, clock or time.time, limit
        self._tickets, self._lock = {}, threading.Lock()

    def issue(self, purpose, resource, ttl=10):
        _duration(ttl)
        with self._lock:
            now = self.clock()
            self._tickets = {k: v for k, v in self._tickets.items() if now < v[0]}
            if len(self._tickets) >= self.limit:
                raise ValueError("too many outstanding tickets")
            value = secrets.token_urlsafe(32)
            self._tickets[TokenAuth.hash(value)] = (now + ttl, self.audience, purpose, resource)
            return value

    def redeem(self, value, purpose, resource):
        if not isinstance(value, str) or not value:
            return False
        with self._lock:
            entry = self._tickets.pop(TokenAuth.hash(value), None)
            return bool(entry and self.clock() < entry[0] and entry[1:] == (self.audience, purpose, resource))


class ResourceSigner:
    """Purpose-separated signatures for expiring resource links, never API bearer tokens."""
    def __init__(self, secret, audience):
        self.key = hmac.new(secret.encode(), ("hpclib:links:" + audience).encode(), hashlib.sha256).digest()

    def sign(self, purpose, expires, resource):
        return hmac.new(self.key, json.dumps([purpose, expires, resource], separators=(",", ":")).encode(),
                        hashlib.sha256).hexdigest()

    def check(self, signature, purpose, expires, resource, now=None):
        return (isinstance(signature, str) and isinstance(expires, (int, float)) and math.isfinite(expires)
                and (time.time() if now is None else now) < expires
                and hmac.compare_digest(signature.encode(), self.sign(purpose, expires, resource).encode()))
