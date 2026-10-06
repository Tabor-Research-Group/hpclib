"""
Apps and settings for the agent console that aren't part of hpclib: installable packages.

A package is a zip file with hpclib-package.json at its top level:

  {"format": 1,
   "name": "my-app",                        # letters, digits, . _ -; the package's id
   "version": "0.1.0",
   "description": "What it adds, in a sentence or two.",
   "apps": {                                 # console apps (each a page with a session per cluster)
     "myapp": {"title": "My app",
                "tunnel": "my-tunnel",       # a tunnel in this package, or one of hpclib's
                "description": "shown on its page",
                "open_path": "/", "health_path": "/health",
                "token_regex": null,         # optional: finds a login token in its log, for Open
                "settings": [{"name": "MY_DIR", "label": "Directory", "hint": "...", "choices": null}],
                "secrets": [{"name": "MY_TOKEN", "label": "API token", "hint": "..."}]}},   # see below
   "tunnels": {"my-tunnel": "tunnels/my-tunnel"},      # directories in the zip: an hpclib tunnel each
   "settings": {                             # defaults and files for tunnels (this package's or hpclib's)
     "data-transfer": {"values": {"SMB_HOST": "files.example.edu", "SMB_DOMAIN": "EXAMPLE"},
                       "files": {"rclone.conf": "settings/rclone.conf"}}}}

An app's `secrets` (tokens, OAuth client secrets) are typed into its settings panel and sent to a cluster over
ssh on standard input (setup_tunnel.sh --secrets), never kept by the console, logged or put on a command line.
The cluster keeps each in ~/.local/tunnels/secrets/TUNNEL/NAME (mode 600), out of the agents' reach; the
tunnel's scripts read them there. The tunnel must list them in TUNNEL_SECRETS (its tunnel_config.sh).

A package with only `settings` is a settings package: e.g. a group's SMB server and its rclone remotes, which
don't belong in hpclib itself.

What happens to it:
  - the console keeps it in ~/.config/hpclib/console/packages/NAME/ (unpacked, with its zip's hash);
  - its apps appear in the console like hpclib's own (a package can't replace one of those, or another
    package's app or tunnel);
  - settings `values` fill in a cluster's app settings where you haven't set your own (yours always win);
  - on a cluster, a packaged tunnel and the settings files are sent with the app's next Check, Install or
    Start (tunnel_setup --push): the tunnel into $HPCLIB_TUNNEL_INSTALL_LOCATION/TUNNEL, the files into
    $HPCTUNNELS_DATA_DIR/settings/TUNNEL.d/.

A package's tunnel scripts run on your clusters as you: install only packages you trust. The console checks
the zip's structure (no links, no paths outside it, sizes) and the manifest, not what the scripts do.

Build one with:  python3 console_packages.py build DIR [OUT.zip]   (DIR holds hpclib-package.json)

Standard library only.
"""
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import zipfile

__all__ = ["PackageError", "PackageStore", "build", "MANIFEST"]

MANIFEST = "hpclib-package.json"
FORMAT = 1
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
APP_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
SETTING_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
FILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}")
URL_PATH_RE = re.compile(r"/[A-Za-z0-9_./?=&%-]{0,200}")
VALUE_RE = re.compile(r"[^\0\n\r]{0,1024}")
MAX_ZIP = 64 << 20          # bytes, the zip itself
MAX_UNPACKED = 256 << 20    # bytes, everything in it
MAX_FILES = 5000
MAX_SETTINGS_FILE = 1 << 20
APP_KEYS = {"title", "tunnel", "description", "open_path", "health_path", "token_regex", "settings", "secrets"}
TOP_KEYS = {"format", "name", "version", "description", "apps", "tunnels", "settings"}


class PackageError(Exception):
    """A package that can't be installed; the message says why."""


def _text(value, key, limit, required=True):
    if not required and (value is None or (isinstance(value, str) and not value.strip())):
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise PackageError(f"`{key}` must be text of at most {limit} characters")
    return value.strip()


def _member_path(name):
    """A zip member's path, or None for a directory entry; refuses anything that could land outside."""
    if name.endswith("/"):
        return None
    if name.startswith(("/", "\\")) or "\\" in name or re.match(r"[A-Za-z]:", name):
        raise PackageError(f"the zip has a member with an absolute path: {name!r}")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise PackageError(f"the zip has a member with a path outside it: {name!r}")
    return name


def read_zip(data):
    """(manifest, {path: bytes}) from a package zip, with its structure checked."""
    if len(data) > MAX_ZIP:
        raise PackageError(f"the package is larger than {MAX_ZIP >> 20} MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PackageError("not a zip file")
    infos = zf.infolist()
    if len(infos) > MAX_FILES:
        raise PackageError(f"the zip has more than {MAX_FILES} entries")
    if sum(i.file_size for i in infos) > MAX_UNPACKED:
        raise PackageError(f"the zip unpacks to more than {MAX_UNPACKED >> 20} MB")
    members = {}
    for info in infos:
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise PackageError(f"the zip has a symbolic link, {info.filename!r}; packages hold plain files only")
        path = _member_path(info.filename)
        if path is None:
            continue
        if path in members:
            raise PackageError(f"the zip has {path!r} twice")
        members[path] = zf.read(info)
    # a zip made of a folder (zip -r pkg.zip pkg/) has everything under that folder: look there too
    prefix = ""
    if MANIFEST not in members:
        tops = {p.split("/", 1)[0] for p in members}
        if len(tops) == 1 and f"{next(iter(tops))}/{MANIFEST}" in members:
            prefix = next(iter(tops)) + "/"
            members = {p[len(prefix):]: b for p, b in members.items()}
        else:
            raise PackageError(f"no {MANIFEST} at the top of the zip")
    try:
        manifest = json.loads(members[MANIFEST].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise PackageError(f"{MANIFEST} is not valid JSON: {e}")
    return manifest, members


def check_manifest(manifest, members, builtin_tunnels):
    """The manifest, normalized; raises PackageError. `builtin_tunnels`: hpclib's own tunnel names."""
    if not isinstance(manifest, dict):
        raise PackageError(f"{MANIFEST} must hold an object")
    unknown = set(manifest) - TOP_KEYS
    if unknown:
        raise PackageError(f"unknown keys in {MANIFEST}: {sorted(unknown)} (known: {sorted(TOP_KEYS)})")
    if manifest.get("format") != FORMAT:
        raise PackageError(f"`format` must be {FORMAT}")
    name = manifest.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise PackageError("`name` must be letters, digits, . _ - (at most 64)")
    version = manifest.get("version", "0")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise PackageError("`version` must be a short version string such as 1.0.2")
    out = {"format": FORMAT, "name": name, "version": version,
           "description": _text(manifest.get("description"), "description", 2000, required=False) or "",
           "apps": {}, "tunnels": {}, "settings": {}}

    tunnels = manifest.get("tunnels") or {}
    if not isinstance(tunnels, dict):
        raise PackageError("`tunnels` maps tunnel names to directories in the zip")
    for tunnel, directory in tunnels.items():
        if not NAME_RE.fullmatch(tunnel):
            raise PackageError(f"invalid tunnel name {tunnel!r}")
        if tunnel in builtin_tunnels:
            raise PackageError(f"{tunnel} is one of hpclib's own tunnels; a package can't replace it")
        if not isinstance(directory, str) or not directory.strip("/"):
            raise PackageError(f"tunnel {tunnel}: give the directory in the zip that holds it")
        directory = directory.strip("/")
        _member_path(directory + "/x")
        files = [p for p in members if p.startswith(directory + "/")]
        if f"{directory}/sbatch_script.sh" not in members:
            raise PackageError(f"tunnel {tunnel}: {directory}/sbatch_script.sh is missing (every tunnel has one)")
        out["tunnels"][tunnel] = {"dir": directory, "files": len(files),
                                  "install": f"{directory}/install.sh" in members}

    apps = manifest.get("apps") or {}
    if not isinstance(apps, dict):
        raise PackageError("`apps` maps app ids to their definitions")
    for app_id, spec in apps.items():
        if not APP_ID_RE.fullmatch(app_id):
            raise PackageError(f"invalid app id {app_id!r} (lowercase letters, digits, _ -)")
        if not isinstance(spec, dict):
            raise PackageError(f"app {app_id} must be an object")
        unknown = set(spec) - APP_KEYS
        if unknown:
            raise PackageError(f"app {app_id}: unknown keys {sorted(unknown)} (known: {sorted(APP_KEYS)})")
        tunnel = spec.get("tunnel")
        if tunnel not in out["tunnels"] and tunnel not in builtin_tunnels:
            raise PackageError(f"app {app_id}: tunnel {tunnel!r} is neither in this package nor one of hpclib's")
        app = {"title": _text(spec.get("title"), f"apps.{app_id}.title", 60), "tunnel": tunnel,
               "description": _text(spec.get("description"), f"apps.{app_id}.description", 2000, required=False),
               "open_path": spec.get("open_path", "/"), "health_path": spec.get("health_path", "/"),
               "token_regex": spec.get("token_regex"), "settings": [], "secrets": []}
        for key in ("open_path", "health_path"):
            if not isinstance(app[key], str) or not URL_PATH_RE.fullmatch(app[key]):
                raise PackageError(f"app {app_id}: {key} must be a path such as /lab")
        if app["token_regex"] is not None:
            if not isinstance(app["token_regex"], str) or len(app["token_regex"]) > 200:
                raise PackageError(f"app {app_id}: token_regex must be a regular expression")
            try:
                if re.compile(app["token_regex"]).groups != 1:
                    raise PackageError(f"app {app_id}: token_regex needs exactly one group, the token")
            except re.error as e:
                raise PackageError(f"app {app_id}: token_regex: {e}")
        fields = spec.get("settings") or []
        if not isinstance(fields, list):
            raise PackageError(f"app {app_id}: settings is a list of fields")
        for f in fields:
            if not isinstance(f, dict) or set(f) - {"name", "label", "hint", "choices"}:
                raise PackageError(f"app {app_id}: each setting is {{name, label, hint, choices}}")
            if not isinstance(f.get("name"), str) or not SETTING_NAME_RE.fullmatch(f["name"]):
                raise PackageError(f"app {app_id}: setting names are like MY_SETTING")
            choices = f.get("choices")
            if choices is not None and (not isinstance(choices, list) or not choices or
                                        not all(isinstance(c, str) and VALUE_RE.fullmatch(c) for c in choices)):
                raise PackageError(f"app {app_id}: {f['name']}'s choices must be a list of strings")
            app["settings"].append({"name": f["name"], "label": _text(f.get("label"), "label", 80),
                                    "hint": _text(f.get("hint", ""), "hint", 400, required=False) or "",
                                    "choices": choices})
        secrets = spec.get("secrets") or []
        if not isinstance(secrets, list):
            raise PackageError(f"app {app_id}: secrets is a list of {{name, label, hint}}")
        for f in secrets:
            if not isinstance(f, dict) or set(f) - {"name", "label", "hint"}:
                raise PackageError(f"app {app_id}: each secret is {{name, label, hint}}")
            if not isinstance(f.get("name"), str) or not SETTING_NAME_RE.fullmatch(f["name"]):
                raise PackageError(f"app {app_id}: secret names are like MY_TOKEN")
            if f["name"] in {s["name"] for s in app["settings"]}:
                raise PackageError(f"app {app_id}: {f['name']} can't be both a setting and a secret")
            app["secrets"].append({"name": f["name"], "label": _text(f.get("label"), "label", 80),
                                   "hint": _text(f.get("hint", ""), "hint", 400, required=False) or ""})
        out["apps"][app_id] = app

    settings = manifest.get("settings") or {}
    if not isinstance(settings, dict):
        raise PackageError("`settings` maps tunnel names to {values, files}")
    for tunnel, entry in settings.items():
        if not NAME_RE.fullmatch(tunnel):
            raise PackageError(f"settings: invalid tunnel name {tunnel!r}")
        if not isinstance(entry, dict) or set(entry) - {"values", "files"}:
            raise PackageError(f"settings for {tunnel} are {{values, files}}")
        values = entry.get("values") or {}
        if not isinstance(values, dict):
            raise PackageError(f"settings.{tunnel}.values maps setting names to values")
        for k, v in values.items():
            if not SETTING_NAME_RE.fullmatch(k) or not isinstance(v, str) or not VALUE_RE.fullmatch(v):
                raise PackageError(f"settings.{tunnel}.values: {k} must be a NAME with a one-line value")
        files = entry.get("files") or {}
        if not isinstance(files, dict):
            raise PackageError(f"settings.{tunnel}.files maps file names to files in the zip")
        for fname, path in files.items():
            if not FILE_NAME_RE.fullmatch(fname):
                raise PackageError(f"settings.{tunnel}.files: invalid file name {fname!r}")
            if not isinstance(path, str) or path not in members:
                raise PackageError(f"settings.{tunnel}.files: {path!r} is not in the zip")
            if len(members[path]) > MAX_SETTINGS_FILE:
                raise PackageError(f"settings.{tunnel}.files: {path} is larger than 1 MB")
        out["settings"][tunnel] = {"values": dict(values), "files": dict(files)}
    if not out["apps"] and not out["settings"] and not out["tunnels"]:
        raise PackageError("the package adds nothing: give apps, tunnels or settings")
    return out


class PackageStore:
    """The installed packages, in DIR/NAME/ (the unpacked zip, plus package.json: the checked manifest)."""

    def __init__(self, root, builtin_apps, builtin_tunnels):
        self.root = root
        self.builtin_apps = dict(builtin_apps)       # id -> settings field names
        self.builtin_tunnels = set(builtin_tunnels)

    def _dir(self, name):
        return os.path.join(self.root, name)

    def installed(self):
        """[(manifest, directory)], by name."""
        out = []
        if not os.path.isdir(self.root):
            return out
        for name in sorted(os.listdir(self.root)):
            path = os.path.join(self._dir(name), "package.json")
            if not NAME_RE.fullmatch(name) or not os.path.isfile(path):
                continue
            try:
                with open(path) as f:
                    out.append((json.load(f), self._dir(name)))
            except (OSError, ValueError):
                continue
        return out

    def _conflicts(self, manifest):
        """Why it can't go in beside what's installed (another version of itself is replaced, not a conflict)."""
        problems = []
        others = [m for m, _ in self.installed() if m["name"] != manifest["name"]]
        for app_id, app in manifest["apps"].items():
            if app_id in self.builtin_apps:
                problems.append(f"app {app_id} is one of hpclib's own")
            for m in others:
                if app_id in m["apps"]:
                    problems.append(f"app {app_id} is already in package {m['name']}")
        for tunnel in manifest["tunnels"]:
            for m in others:
                if tunnel in m["tunnels"]:
                    problems.append(f"tunnel {tunnel} is already in package {m['name']}")
        apps_by_tunnel = self.fields_by_tunnel([manifest] + others)
        known_tunnels = self.builtin_tunnels | set(manifest["tunnels"]) | {t for m in others for t in m["tunnels"]}
        for tunnel, entry in manifest["settings"].items():
            if tunnel not in known_tunnels:
                problems.append(f"settings for {tunnel}: no such tunnel in hpclib or an installed package")
                continue
            fields = apps_by_tunnel.get(tunnel, set())
            for k in entry["values"]:
                if k not in fields:
                    problems.append(f"settings for {tunnel}: {k} is not a setting of its apps "
                                    f"({', '.join(sorted(fields)) or 'none'})")
            for m in others:
                theirs = m["settings"].get(tunnel) or {}
                for k in set(entry["values"]) & set(theirs.get("values") or {}):
                    problems.append(f"settings for {tunnel}: package {m['name']} already sets {k}")
                for fname in set(entry["files"]) & set(theirs.get("files") or {}):
                    problems.append(f"settings for {tunnel}: package {m['name']} already has {fname}")
        return problems

    def fields_by_tunnel(self, manifests=None):
        """tunnel -> the setting names its apps have (hpclib's and the packages')."""
        out = {}
        for app_id, (tunnel, fields) in self.builtin_apps.items():
            out.setdefault(tunnel, set()).update(fields)
        for m in manifests if manifests is not None else [m for m, _ in self.installed()]:
            for app in m["apps"].values():
                out.setdefault(app["tunnel"], set()).update(f["name"] for f in app["settings"])
        return out

    def inspect(self, data):
        """What installing `data` would do: the checked manifest, its hash and any problems."""
        raw, members = read_zip(data)
        manifest = check_manifest(raw, members, self.builtin_tunnels)
        current = {m["name"]: m for m, _ in self.installed()}.get(manifest["name"])
        return {"package": manifest, "sha256": hashlib.sha256(data).hexdigest(),
                "replaces": current["version"] if current else None, "problems": self._conflicts(manifest)}

    def install(self, data):
        report = self.inspect(data)
        if report["problems"]:
            raise PackageError("; ".join(report["problems"]))
        manifest = dict(report["package"], sha256=report["sha256"], installed=time.time())
        _, members = read_zip(data)
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".incoming-", dir=self.root)
        try:
            for path, content in members.items():
                target = os.path.join(staging, *path.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(content)
                os.chmod(target, 0o755 if path.endswith(".sh") or content.startswith(b"#!") else 0o644)
            with open(os.path.join(staging, "package.json"), "w") as f:
                json.dump(manifest, f, indent=1)
            target = self._dir(manifest["name"])
            old = target + ".old"
            shutil.rmtree(old, ignore_errors=True)
            if os.path.exists(target):
                os.replace(target, old)
            os.replace(staging, target)
            shutil.rmtree(old, ignore_errors=True)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return dict(report, package=manifest)

    def remove(self, name):
        if not NAME_RE.fullmatch(name or "") or not os.path.isdir(self._dir(name)):
            raise PackageError(f"no installed package {name!r}")
        shutil.rmtree(self._dir(name))

    # -- what the console uses ---------------------------------------------------

    def apps(self):
        """app id -> (manifest, app spec, the package's directory)."""
        return {app_id: (m, app, d) for m, d in self.installed() for app_id, app in m["apps"].items()}

    def tunnel_dir(self, tunnel):
        for m, d in self.installed():
            if tunnel in m["tunnels"]:
                return os.path.join(d, *m["tunnels"][tunnel]["dir"].split("/"))
        return None

    def defaults(self, tunnel):
        """{setting: (value, package)} the packages give a tunnel."""
        return {k: (v, m["name"]) for m, _ in self.installed()
                for k, v in ((m["settings"].get(tunnel) or {}).get("values") or {}).items()}

    def settings_files(self, tunnel):
        """{file name: path on this machine} the packages give a tunnel."""
        return {fname: os.path.join(d, *path.split("/")) for m, d in self.installed()
                for fname, path in ((m["settings"].get(tunnel) or {}).get("files") or {}).items()}

    def stage(self, tunnel, root):
        """
        The directory tunnel_setup --push sends for `tunnel`: tunnel/ (a packaged tunnel) and settings.d/ (the
        packages' files for it), under `root` and named by its content's hash, so it never changes once made;
        returns (path, hash). With nothing from the packages, an empty settings.d/ (which clears what an earlier
        push left on a cluster) and hash None.
        """
        source, files = self.tunnel_dir(tunnel), self.settings_files(tunnel)
        os.makedirs(root, mode=0o700, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=f".{tunnel}-", dir=root)
        try:
            os.makedirs(os.path.join(tmp, "settings.d"))
            if source is not None:
                shutil.copytree(source, os.path.join(tmp, "tunnel"))
            for fname, path in files.items():
                shutil.copyfile(path, os.path.join(tmp, "settings.d", fname))
            digest = hashlib.sha256()
            for base, dirs, names in os.walk(tmp):
                dirs.sort()
                for n in sorted(names):
                    p = os.path.join(base, n)
                    digest.update(os.path.relpath(p, tmp).encode() + b"\0")
                    with open(p, "rb") as f:
                        digest.update(f.read())
            empty = source is None and not files
            final = os.path.join(root, f"{tunnel}-{'empty' if empty else digest.hexdigest()[:24]}")
            try:
                os.replace(tmp, final)
            except OSError:              # made meanwhile (the same content): use that one
                if not os.path.isdir(final):
                    raise
                shutil.rmtree(tmp, ignore_errors=True)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return final, (None if empty else digest.hexdigest())

def summary(manifest):
    """One line per thing a package adds, for people."""
    lines = []
    for app_id, app in manifest["apps"].items():
        lines.append(f"app {app['title']} ({app_id}), tunnel {app['tunnel']}")
    for tunnel, t in manifest["tunnels"].items():
        lines.append(f"tunnel {tunnel}: {t['files']} files{', with install.sh' if t['install'] else ''}")
    for tunnel, s in manifest["settings"].items():
        for k, v in s["values"].items():
            lines.append(f"{tunnel} setting {k} = {v}")
        for fname in s["files"]:
            lines.append(f"{tunnel} settings file {fname}")
    return lines


def build(directory, out=None):
    """Zip DIR (holding hpclib-package.json) as a package, after checking it as the console would."""
    directory = os.path.abspath(directory)
    if not os.path.isfile(os.path.join(directory, MANIFEST)):
        raise PackageError(f"no {MANIFEST} in {directory}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, names in os.walk(directory):
            dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__"))
            for n in sorted(names):
                if n in (".DS_Store",) or n.startswith("._") or n.endswith(".zip"):
                    continue
                path = os.path.join(base, n)
                if os.path.islink(path):
                    raise PackageError(f"{path} is a symbolic link; packages hold plain files only")
                rel = os.path.relpath(path, directory).replace(os.sep, "/")
                info = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))
                info.external_attr = (0o755 if os.access(path, os.X_OK) else 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(path, "rb") as f:
                    zf.writestr(info, f.read())
    data = buf.getvalue()
    raw, members = read_zip(data)
    builtin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tunnels")
    manifest = check_manifest(raw, members, set(os.listdir(builtin)) if os.path.isdir(builtin) else set())
    out = out or os.path.join(os.path.dirname(directory), f"{manifest['name']}-{manifest['version']}.zip")
    with open(out, "wb") as f:
        f.write(data)
    return out, manifest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) in (2, 3) and argv[0] == "build":
        try:
            out, manifest = build(argv[1], argv[2] if len(argv) == 3 else None)
        except PackageError as e:
            print(f"console_packages: {e}", file=sys.stderr)
            return 1
        print(f"built {out}")
        for line in summary(manifest):
            print(f"  {line}")
        return 0
    print("usage: console_packages.py build DIR [OUT.zip]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
