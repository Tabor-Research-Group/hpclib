"""
A standard-library client for rest_server.py, for use on your own
machine at the near end of the tunnel.

    client = RESTClient.from_env()          # $HPC_REST_URL, $HPC_REST_TOKEN[_FILE]
    client.cluster()
    client.submit_job("orca", {"input": "jobs/a/input.inp"}, dry_run=True)
"""
import fnmatch
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["RESTClient", "RESTClientError", "FileSync"]


class RESTClientError(Exception):
    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload if payload is not None else {"error": message}


class RESTClient:

    DEFAULT_URL = "http://127.0.0.1:5000"
    DEFAULT_TOKEN_FILE = "~/.config/hpclib/rest_token"
    TUNNEL_HINT = ("start the tunnel first, e.g. `launch_tunnel -A none -P {port} user@login.example rest`")

    def __init__(self, url=None, token=None, token_file=None, timeout=60):
        self.url = (url or self.DEFAULT_URL).rstrip("/")
        if token is None:
            token = self.read_token_file(token_file or self.DEFAULT_TOKEN_FILE)
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_env(cls, **overrides):
        opts = {
            "url": os.environ.get("HPC_REST_URL"),
            "token": os.environ.get("HPC_REST_TOKEN"),
            "token_file": os.environ.get("HPC_REST_TOKEN_FILE"),
        }
        opts.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**opts)

    @staticmethod
    def read_token_file(path):
        path = os.path.expanduser(path)
        try:
            info = os.stat(path)
        except FileNotFoundError:
            raise RESTClientError(f"no token: set $HPC_REST_TOKEN or put the token in {path} (mode 600)")
        if info.st_mode & 0o077:
            print(f"warning: {path} is readable by other users; run `chmod 600 {path}`", file=sys.stderr)
        with open(path) as f:
            return f.read().strip()

    def request(self, verb, route, query=None, body=None, data=None, raw=False, timeout=None):
        url = self.url + route
        if query:
            query = {k: v for k, v in query.items() if v is not None}
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=verb, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as res:
                status, ctype, content = res.status, res.headers.get("Content-Type", ""), res.read()
        except urllib.error.HTTPError as e:
            status, ctype, content = e.code, e.headers.get("Content-Type", ""), e.read()
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            port = urllib.parse.urlsplit(self.url).port or 80
            raise RESTClientError(f"hpclib REST server not reachable at {self.url} ({getattr(e, 'reason', e)}); "
                                  + self.TUNNEL_HINT.format(port=port))
        if "text/html" in ctype:
            # the tunnel's waiting page: the forward is up, the job isn't yet
            raise RESTClientError("the tunnel is up but the REST server's job is still queued or starting; "
                                  "retry in a minute", status=status)
        if raw and status < 400:
            return content
        try:
            payload = json.loads(content) if content else {}
        except ValueError:
            raise RESTClientError(f"unexpected non-JSON response ({status})", status=status)
        if status >= 400:
            raise RESTClientError(payload.get("error", f"HTTP {status}"), status=status, payload=payload)
        return payload

    # convenience wrappers, one per route
    def health(self):
        return self.request("GET", "/health")

    def cluster(self):
        return self.request("GET", "/cluster")

    def templates(self):
        return self.request("GET", "/templates")

    def template_guide(self, name):
        return self.request("GET", "/templates/guide", query={"name": name})

    def proposals(self):
        return self.request("GET", "/templates/proposals")

    def propose_template(self, name, template, script, guide=None, rationale=""):
        body = {"name": name, "template": template, "script": script, "rationale": rationale}
        if guide is not None:
            body["guide"] = guide
        return self.request("POST", "/templates/propose", body=body)

    def modules(self, query="", spider=False):
        return self.request("GET", "/modules/spider" if spider else "/modules/avail", query={"query": query},
                            timeout=max(self.timeout, 150))

    def submit_job(self, template, params=None, resources=None, workdir=None, idempotency_key=None,
                   dry_run=False, tasks=None, tasks_from=None, throttle=None, label=None):
        body = {"template": template, "params": params or {}, "dry_run": dry_run}
        for key, value in (("resources", resources), ("workdir", workdir), ("idempotency_key", idempotency_key),
                           ("tasks", tasks), ("tasks_from", tasks_from), ("throttle", throttle),
                           ("label", label)):
            if value is not None:
                body[key] = value
        return self.request("POST", "/jobs", body=body)

    def jobs(self, active_only=False, limit=50, label=None):
        return self.request("GET", "/jobs", query={"active": int(active_only), "limit": limit, "label": label})

    def job_status(self, job_id, include_tasks=False):
        return self.request("GET", "/jobs/status", query={"id": job_id, "tasks": int(include_tasks)})

    def wait_job(self, job_id, timeout=60):
        return self.request("GET", "/jobs/wait", query={"id": job_id, "timeout": int(timeout)},
                            timeout=timeout + 30)

    def cancel_job(self, job_id):
        return self.request("POST", "/jobs/cancel", query={"id": job_id})

    def list_files(self, path="."):
        return self.request("GET", "/files", query={"path": path})

    def read_file(self, path, offset=0, length=None):
        return self.request("GET", "/files/read", query={"path": path, "offset": offset, "length": length})

    def tail_file(self, path, lines=100):
        return self.request("GET", "/files/tail", query={"path": path, "lines": lines})

    def download(self, path):
        return self.request("GET", "/files/content", query={"path": path}, raw=True)

    def upload(self, path, data, overwrite=False, parents=False):
        if isinstance(data, str):
            data = data.encode()
        return self.request("PUT", "/files/content", data=data,
                            query={"path": path, "overwrite": int(overwrite), "parents": int(parents)})

    def mkdir(self, path, parents=False):
        return self.request("POST", "/files/mkdir", query={"path": path, "parents": int(parents)})


class FileSync:
    """
    Copies files between directories on this machine and the cluster
    through the REST API, so the token's scopes and directories apply
    (pushing needs `files:write`). Local paths must lie inside
    `local_roots`. Meant for job inputs and results; use rsync over SSH
    for bulk data.
    """

    MAX_FILES = 5000
    MAX_BYTES = 2 << 30
    MAX_DEPTH = 8
    SKIP_DIRS = {"__pycache__", ".git"}

    def __init__(self, client: RESTClient, local_roots):
        self.client = client
        self.local_roots = [os.path.realpath(os.path.expanduser(r)) for r in local_roots]
        if not self.local_roots:
            raise ValueError("FileSync needs at least one local root directory")

    def local_path(self, path):
        real = os.path.realpath(os.path.expanduser(path))
        if not any(os.path.commonpath([root, real]) == root for root in self.local_roots):
            raise RESTClientError(f"{path} is outside the local directories this tool may use "
                                  f"({', '.join(self.local_roots)})")
        return real

    @staticmethod
    def _matches(name, pattern):
        return any(fnmatch.fnmatch(name, p.strip()) for p in (pattern or "*").split(",") if p.strip())

    def list_local(self, path):
        real = self.local_path(path)
        if not os.path.isdir(real):
            raise RESTClientError(f"{real} is not a directory")
        entries = []
        for entry in sorted(os.scandir(real), key=lambda e: e.name):
            info = entry.stat(follow_symlinks=False)
            entries.append({"name": entry.name, "type": "directory" if entry.is_dir() else "file",
                            "size": info.st_size})
        return {"path": real, "entries": entries}

    def push(self, local_path, remote_dir, pattern="*", overwrite=False):
        """Upload a file, or a directory's matching files (recursively), into remote_dir."""
        real = self.local_path(local_path)
        if os.path.isfile(real):
            files = [(real, os.path.basename(real))]
        elif os.path.isdir(real):
            files = []
            for dirpath, dirnames, filenames in os.walk(real):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in self.SKIP_DIRS)
                for fname in sorted(filenames):
                    if not fname.startswith(".") and self._matches(fname, pattern):
                        full = os.path.join(dirpath, fname)
                        if os.path.commonpath([real, os.path.realpath(full)]) == real:  # no symlink escapes
                            files.append((full, os.path.relpath(full, real)))
        else:
            raise RESTClientError(f"{real} does not exist")
        total = sum(os.path.getsize(f) for f, _ in files)
        if len(files) > self.MAX_FILES or total > self.MAX_BYTES:
            raise RESTClientError(f"{len(files)} files / {total} bytes is more than this tool moves at once "
                                  f"({self.MAX_FILES} files / {self.MAX_BYTES} bytes); use rsync")
        out = {"local": real, "remote_dir": remote_dir, "uploaded": [], "skipped": [], "errors": []}
        for full, rel in files:
            remote = remote_dir.rstrip("/") + "/" + rel.replace(os.sep, "/")
            with open(full, "rb") as f:
                data = f.read()
            try:
                self.client.upload(remote, data, overwrite=overwrite, parents=True)
                out["uploaded"].append(rel)
            except RESTClientError as e:
                if e.status == 409 and not overwrite:
                    out["skipped"].append(rel)
                elif e.status in (401, 403):
                    raise
                else:
                    out["errors"].append({"file": rel, "error": str(e)})
        out["errors"] = out["errors"][:20]
        return out

    def _remote_files(self, remote_dir, pattern, prefix="", depth=0):
        listing = self.client.list_files(remote_dir)
        if listing.get("type") != "directory":
            return [(listing["path"], os.path.basename(listing["path"]), listing.get("size", 0))]
        found = []
        for entry in listing["entries"]:
            rel = prefix + entry["name"]
            if entry["type"] == "directory" and depth < self.MAX_DEPTH and not entry["name"].startswith("."):
                found += self._remote_files(entry["path"], pattern, rel + "/", depth + 1)
            elif entry["type"] == "file" and self._matches(entry["name"], pattern):
                found.append((entry["path"], rel, entry["size"]))
        return found

    def pull(self, remote_path, local_dir, pattern="*", overwrite=False):
        """Download a file, or a directory's matching files (recursively), into local_dir."""
        local_dir = self.local_path(local_dir)
        files = self._remote_files(remote_path, pattern)
        total = sum(size for _, _, size in files)
        if len(files) > self.MAX_FILES or total > self.MAX_BYTES:
            raise RESTClientError(f"{len(files)} files / {total} bytes is more than this tool moves at once; "
                                  f"use rsync")
        out = {"remote": remote_path, "local_dir": local_dir, "downloaded": [], "skipped": [], "errors": []}
        for remote, rel, _ in files:
            target = self.local_path(os.path.join(local_dir, rel))
            if os.path.exists(target) and not overwrite:
                out["skipped"].append(rel)
                continue
            try:
                data = self.client.download(remote)
            except RESTClientError as e:
                if e.status in (401, 403):
                    raise
                out["errors"].append({"file": rel, "error": str(e)})
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + ".hpclib-partial"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
            out["downloaded"].append(rel)
        out["errors"] = out["errors"][:20]
        return out
