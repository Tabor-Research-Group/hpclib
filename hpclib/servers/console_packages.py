"""
Apps, settings and cluster configurations for the agent console: installable packages.

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
                "password_regex": null,      # optional: finds a password it prints there instead (Copy password)
                "login_user": null,          # optional, with password_regex: the user name of its basic-auth
                                             #   login; Open then signs in with both (user:password@ in the URL)
                "shared": false,             # optional: one running instance serves everyone (see below)
                "instance_label": "database",   # with shared: what the instance is called in the console
                "reinstall": {"label": "Update", "title": "what it does"},   # optional: what Install does once installed
                "install_files": {"install.sh": "install/install.sh", "helpers/setup.py": "install/setup.py"},
                "install_modules": ["WebProxy"],   # defaults; users may replace or clear these in Settings
                "settings": [{"name": "MY_DIR", "label": "Directory", "hint": "...", "choices": null}],
                "secrets": [{"name": "MY_TOKEN", "label": "API token", "hint": "..."}],   # see below
                "tokens": {"scopes": ["read", "write", "admin"], "default_scope": "write",
                           "days": 90, "label": "API tokens"},   # optional: manage the app's own tokens (below)
                "downloads": {"label": "Model Download", "description": "...",   # optional: things to download
                              "kinds": [{"id": "ollama", "label": "Ollama", "placeholder": "granite4.2:30b",
                                         "pattern": "[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?"}]}}},   # (below)
   "tunnels": {"my-tunnel": "tunnels/my-tunnel"},      # directories in the zip: an hpclib tunnel each
   "settings": {                             # defaults and files for tunnels (this package's or hpclib's)
     "data-transfer": {"values": {"SMB_HOST": "files.example.edu", "SMB_DOMAIN": "EXAMPLE"},
                       "files": {"rclone.conf": "settings/rclone.conf"}}}}

A `shared` app's tunnel attaches to a running instance another job serves instead of starting a second one, and
leaves the one it starts running after Stop (the tunnel's tunnel_config.sh sets SHARED_INSTANCE and KEEP_INSTANCE,
and its job registers itself with tunnel_register_instance); the app's row then shows the job and a control that
ends it. `reinstall` renames what Install does once the app is installed (for PAI: Update).

An app's `secrets` (tokens, OAuth client secrets) are typed into its settings panel and sent to a cluster over
ssh on standard input (setup_tunnel.sh --secrets), never kept by the console, logged or put on a command line.
The cluster keeps each in ~/.local/tunnels/secrets/TUNNEL/NAME (mode 600); the tunnel's scripts read them there.
Other processes with file access as the same Unix account can also read them. The tunnel must list them in
TUNNEL_SECRETS (its tunnel_config.sh).

An app's `tokens` section puts a Tokens panel in its settings, for apps that keep their own bearer tokens (PAI's
write API). The tunnel ships tokens.sh, which the console runs on the cluster over its login (setup_tunnel.sh
--tokens) as `tokens.sh --json ACTION ...`, where it prints ONE line of JSON on its standard output:

  list                         {"tokens": [{"id", "label", "scopes": [...], "created", "expires", "revoked"}]}
  create --label L --scopes a,b --days N [--save-to PATH]
                               {"id", "token", "label", "scopes", "expires"}; with --save-to it writes the token to
                               that file (mode 600, refusing one that exists) and prints "saved_to" instead of "token"
  revoke ID                    {"revoked": ID}

The secret of a new token is shown once (or goes straight to a file on the cluster) and is never kept by the
console. What a token may do is the app's business: the console only offers the `scopes` the manifest lists.

An app's `downloads` section puts a download toolbar under its rows, for apps that need large things on the cluster
(OpenCode's models): pick a kind, type a name (which must match the kind's `pattern`), Download; and a list of
what is there, with Remove. The tunnel ships downloads.sh, which the console runs on the cluster's login node over
its login (setup_tunnel.sh --downloads):

  --json list                  {"items": [{"kind", "name", "size" (bytes), "in_use" (true/false), "note"}],
                                "note": "..."}: ONE line of JSON on its standard output
  pull KIND NAME               downloads it, printing progress for people; exit 0 when it is there. The console runs
                               this as an operation with its log, like Install (it can take hours)
  --json remove KIND NAME      {"removed": NAME}; refuses (exit 1, saying why on standard error) what is in use

`widgets` adds small dashboards to the console's pages: [{"id": "gpu-queue", "title": "GPU queue",
"slot": "home", "entry": "widgets/gpu-queue.html", "reads": ["clusters", "jobs"], "height": 220}]. A slot is
`home` (above the clusters list), `cluster.overview` (on each cluster's Overview) or `cluster.tab` (a tab on each
cluster's page). The entry is one self-contained HTML file (inline scripts and styles; images as data: URLs),
shown in a sandboxed frame with no network access: it can't read the console's key, call its API or act on a
cluster. It gets data by subscribing to the topics it `reads` (clusters, jobs, storage, apps; and theme, the
page's colours, always), through the helper the console puts in the page:

    const hpc = await hpclibWidget();          // {widget, context: {cluster}, subscribe, navigate, resize}
    hpc.subscribe("jobs", ({ byCluster }) => draw(byCluster));

See docs/extending.md, "Console widgets".

A package with only `settings` is a settings package: e.g. a group's SMB server and its rclone remotes, which
don't belong in hpclib itself.

An app can appear as a tab beside another app's Sessions page using
`tab: {"parent": "app_id", "id": "preview", "label": "Preview"}`. The parent must
be a different top-level app in the same package. Tab IDs are unique per parent
and cannot be `sessions`; nested tabs are not supported. The service's app ID,
tunnel, settings, secrets and API routes stay independent. The manifest's app
order determines tab order; Sessions always comes first.

`clusters` maps preset IDs to {title, host, port, description, documentation_url,
ssh_keys, defaults}. Defaults holds tunnel_args, rest_on, connection_hours, optional work_dir,
app_settings and rest_config. The latter are native profile/setup defaults, filled with the
user's username when a profile is added; installing a package does not connect to a cluster.
ssh_keys is hpclib or site-managed. A preset supplies configuration, not a user's
credentials: installing it only adds a choice to Clusters > Add cluster. Existing
profiles remain independent when the package is replaced or removed.

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

An app's `install_files` maps paths relative to its packaged tunnel to source files in the zip. A list is
shorthand for identical source and destination paths (e.g. ["install.sh", "requirements.txt"]). These files
join the tunnel in the existing SSH push before Check, Install or Start. An install.sh already inside the
tunnel is always sent, even without this field. Declarations cannot replace built-in tunnels or conflict
with another file in the same tunnel. Installers run from the received tunnel directory, beside their files.

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
import urllib.parse
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
APP_KEYS = {"title", "tunnel", "description", "open_path", "health_path", "token_regex", "password_regex", "login_user",
            "settings", "secrets", "downloads", "install_files", "install_modules",
            "shared", "instance_label", "reinstall", "tokens", "tab"}
LOGIN_USER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
DOWNLOAD_KEYS = {"label", "description", "kinds"}
DOWNLOAD_KIND_KEYS = {"id", "label", "placeholder", "pattern"}
DOWNLOAD_KIND_RE = re.compile(r"[a-z][a-z0-9_-]{0,19}")
# whatever a kind's pattern allows, a name to download is at most this (it goes on a command line on the cluster)
DOWNLOAD_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
TOKEN_KEYS = {"label", "scopes", "default_scope", "days", "description"}
SCOPE_RE = re.compile(r"[a-z][a-z0-9_-]{0,19}")
TOP_KEYS = {"format", "name", "version", "description", "apps", "tunnels", "settings", "clusters", "widgets"}
WIDGET_KEYS = {"id", "title", "slot", "entry", "reads", "height", "description"}
WIDGET_SLOTS = ("home", "cluster.overview", "cluster.tab")
WIDGET_TOPICS = ("clusters", "jobs", "storage", "apps")      # and "theme", which every widget gets
MAX_WIDGETS = 20
MAX_WIDGET_FILE = 1 << 20
CLUSTER_KEYS = {"title", "host", "port", "description", "documentation_url", "defaults", "ssh_keys"}
CLUSTER_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
CLUSTER_ARG_RE = re.compile(r"--(time|mem|partition|account|qos|cpus-per-task|constraint)=[A-Za-z0-9:._,+-]{1,64}")
CLUSTER_DEFAULT_KEYS = {"tunnel_args", "rest_on", "connection_hours", "work_dir", "rest_config", "app_settings"}
APP_SETTING_KEYS = {"tunnel_args", "conda_env", "modules", "install_modules", "project", "settings"}
INSTALL_MODULE_RE = re.compile(r"[A-Za-z0-9_.+][A-Za-z0-9_.+/-]{0,127}")
SECRET_FIELD_RE = re.compile(r"(?:^|_)(?:pass|password|passwd|pwd|passphrase|passcode|secret|secrets|token|tokens|"
                             r"credential|credentials|api_key|access_key|private_key|authorization|auth_header|"
                             r"cookie|cookies)(?:_|$)", re.I)
URL_CREDENTIAL_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s/]*:[^\s/]*@", re.I)
QUERY_CREDENTIAL_RE = re.compile(r"[?&;](?:password|passwd|passphrase|secret|token|access_token|api_key)=[^&#\s]+", re.I)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")
AUTH_VALUE_RE = re.compile(r"^(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+\s*$|"
                           r"(?:^|\s)Authorization:\s*(?:Bearer|Basic)\s+\S+", re.I)


def check_public_defaults(value, path="cluster defaults", depth=0):
    """Persistable defaults may contain configuration, never credential fields or key material."""
    if depth > 32:
        raise PackageError(f"{path}: defaults are nested too deeply")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PackageError(f"{path}: setting names must be strings")
            name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).replace("-", "_").replace(".", "_")
            if SECRET_FIELD_RE.search(name):
                raise PackageError(f"{path}.{key}: credentials cannot be stored in plaintext defaults")
            check_public_defaults(item, f"{path}.{key}", depth + 1)
    elif isinstance(value, list):
        for item in value:
            check_public_defaults(item, path, depth + 1)
    elif isinstance(value, str) and (URL_CREDENTIAL_RE.search(value) or QUERY_CREDENTIAL_RE.search(value)
                                   or PRIVATE_KEY_RE.search(value) or AUTH_VALUE_RE.search(value)):
        raise PackageError(f"{path}: credentials or private keys cannot be stored in plaintext defaults")
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[", '"')):
        try:
            decoded = json.loads(value)
        except RecursionError as e:
            raise PackageError(f"{path}: defaults are nested too deeply") from e
        except ValueError:
            return
        check_public_defaults(decoded, path, depth + 1)


def check_cluster_config(config):
    """Validate portable REST defaults using the REST server's own validators."""
    try:
        from . import rest_server
    except ImportError:  # standalone package builder / console scripts
        import rest_server
    if not isinstance(config, dict):
        raise PackageError("rest_config must be an object")
    check_public_defaults(config, "rest_config")
    try:
        if len(json.dumps(config, allow_nan=False)) > 65536:
            raise ValueError("rest_config is larger than 64 KiB")
        normalized, _ = rest_server.updated_config({}, config)
    except (ValueError, TypeError, KeyError) as e:
        raise PackageError(f"invalid rest_config: {e}") from e
    return json.loads(json.dumps(normalized))


def check_install_modules(value, *, allow_default=False):
    """An ordered module list, serialized with colons for setup_tunnel.sh; None restores app defaults."""
    if value is None and allow_default:
        return None
    if (not isinstance(value, list) or len(value) > 32 or
            not all(isinstance(m, str) and INSTALL_MODULE_RE.fullmatch(m) for m in value) or
            len(":".join(value)) > 1024):
        raise PackageError("install_modules must be up to 32 module names, at most 1024 characters together")
    return list(value)


def _cluster_app_settings(apps):
    if not isinstance(apps, dict):
        raise PackageError("app_settings must map app IDs to their settings")
    check_public_defaults(apps, "app_settings")
    for app, settings in apps.items():
        if not APP_ID_RE.fullmatch(app) or not isinstance(settings, dict) or set(settings) - APP_SETTING_KEYS:
            raise PackageError("app_settings contains an invalid app ID or setting field")
        for key, value in settings.items():
            if key == "tunnel_args":
                if not isinstance(value, list) or len(value) > 16 or not all(isinstance(v, str) and CLUSTER_ARG_RE.fullmatch(v) for v in value):
                    raise PackageError("invalid app tunnel_args")
            elif key == "install_modules":
                check_install_modules(value, allow_default=True)
            elif key == "modules":
                if not isinstance(value, list) or not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.+-][A-Za-z0-9_.+/-]{0,127}", v) for v in value):
                    raise PackageError("invalid app modules")
            elif key == "settings":
                if not isinstance(value, dict) or not all(SETTING_NAME_RE.fullmatch(k) and isinstance(v, str) and VALUE_RE.fullmatch(v) for k, v in value.items()):
                    raise PackageError("invalid app settings values")
            elif key == "conda_env":
                if value is not None and (not isinstance(value, str) or (value and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value))):
                    raise PackageError("invalid app conda_env")
            elif not isinstance(value, str) or (value and (not value.startswith("/") or not VALUE_RE.fullmatch(value))):
                raise PackageError("invalid app project")
    return json.loads(json.dumps(apps))


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


def _check_install_files(value, app_id, members):
    """Destination paths in the tunnel -> package-relative source paths, all plain zip members."""
    where = f"apps.{app_id}.install_files"
    if value is None:
        return {}
    if isinstance(value, list):
        if not all(isinstance(p, str) for p in value) or len(set(value)) != len(value):
            raise PackageError(f"{where} must list distinct file paths")
        value = {p: p for p in value}
    if not isinstance(value, dict):
        raise PackageError(f"{where} must map tunnel-relative destinations to files in the zip, or list file paths")
    for dest, source in value.items():
        if (not isinstance(dest, str) or not dest or len(dest) > 512 or
                any(ord(c) < 32 for c in dest) or _member_path(dest) is None):
            raise PackageError(f"{where}: invalid destination {dest!r}")
        if not isinstance(source, str) or source not in members:
            raise PackageError(f"{where}: {source!r} is not a file in the zip")
        if source == "package.json":
            raise PackageError(f"{where}: root package.json is reserved for installed metadata; "
                               "put the source file in a subdirectory")
    return dict(value)


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
           "apps": {}, "tunnels": {}, "settings": {}, "clusters": {}, "widgets": []}

    clusters = manifest.get("clusters") or {}
    if not isinstance(clusters, dict):
        raise PackageError("`clusters` maps preset IDs to cluster configuration objects")
    for preset_id, spec in clusters.items():
        if not APP_ID_RE.fullmatch(preset_id) or not isinstance(spec, dict) or set(spec) - CLUSTER_KEYS:
            raise PackageError("invalid cluster preset ID or fields")
        check_public_defaults(spec, f"cluster {preset_id}")
        host = spec.get("host")
        if not isinstance(host, str) or not CLUSTER_HOST_RE.fullmatch(host) or ".." in host:
            raise PackageError(f"cluster {preset_id}: host must be a hostname without a username or SSH options")
        port = spec.get("port", 22)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise PackageError(f"cluster {preset_id}: port must be 1 to 65535")
        url = spec.get("documentation_url") or ""
        if not isinstance(url, str) or len(url) > 1024:
            raise PackageError(f"cluster {preset_id}: invalid documentation URL")
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError as e:
            raise PackageError(f"cluster {preset_id}: invalid documentation URL") from e
        if url and (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or any(c in url for c in "\0\r\n")):
            raise PackageError(f"cluster {preset_id}: documentation URL must be HTTPS without credentials")
        ssh_keys = spec.get("ssh_keys", "hpclib")
        if ssh_keys not in ("hpclib", "site-managed"):
            raise PackageError(f"cluster {preset_id}: ssh_keys is hpclib or site-managed")
        defaults = spec.get("defaults") or {}
        if not isinstance(defaults, dict) or set(defaults) - CLUSTER_DEFAULT_KEYS:
            raise PackageError(f"cluster {preset_id}: defaults use {sorted(CLUSTER_DEFAULT_KEYS)}")
        args = defaults.get("tunnel_args", [])
        if not isinstance(args, list) or len(args) > 16 or not all(isinstance(a, str) and CLUSTER_ARG_RE.fullmatch(a) for a in args):
            raise PackageError(f"cluster {preset_id}: invalid tunnel_args")
        hours = defaults.get("connection_hours", 12)
        if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 168:
            raise PackageError(f"cluster {preset_id}: connection_hours must be 1 to 168")
        rest_on = defaults.get("rest_on", "job")
        if rest_on not in ("job", "login"):
            raise PackageError(f"cluster {preset_id}: rest_on is job or login")
        extra = {}
        if "work_dir" in defaults:
            work = defaults["work_dir"]
            if not isinstance(work, str) or not work.startswith("/") or not VALUE_RE.fullmatch(work):
                raise PackageError("work_dir must be an absolute path; {username} is filled when adding the cluster")
            extra["work_dir"] = work
        if "rest_config" in defaults:
            extra["rest_config"] = check_cluster_config(defaults["rest_config"])
        if "app_settings" in defaults:
            extra["app_settings"] = _cluster_app_settings(defaults["app_settings"])
        out["clusters"][preset_id] = {"title": _text(spec.get("title"), "cluster title", 80), "host": host,
            "port": port, "description": _text(spec.get("description"), "cluster description", 2000, required=False) or "",
            "documentation_url": url, "ssh_keys": ssh_keys,
            "defaults": {"tunnel_args": list(args), "rest_on": rest_on, "connection_hours": hours, **extra}}

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
        install_files = _check_install_files(spec.get("install_files"), app_id, members)
        if install_files and tunnel not in out["tunnels"]:
            raise PackageError(f"app {app_id}: install_files needs a tunnel owned by this package; "
                               "a package can't replace hpclib's own tunnel files")
        app = {"title": _text(spec.get("title"), f"apps.{app_id}.title", 60), "tunnel": tunnel,
               "description": _text(spec.get("description"), f"apps.{app_id}.description", 2000, required=False),
               "open_path": spec.get("open_path", "/"), "health_path": spec.get("health_path", "/"),
               "token_regex": spec.get("token_regex"), "password_regex": spec.get("password_regex"),
               "login_user": spec.get("login_user"),
               "settings": [], "secrets": [],
               "shared": spec.get("shared", False), "instance_label": None, "reinstall": None, "tokens": None,
               "downloads": None, "install_files": install_files,
               "install_modules": check_install_modules(spec.get("install_modules", []))}
        if spec.get("tab") is not None:
            tab = spec["tab"]
            if not isinstance(tab, dict) or set(tab) != {"parent", "id", "label"}:
                raise PackageError(f"app {app_id}: tab is {{parent, id, label}}")
            if not isinstance(tab["parent"], str) or not APP_ID_RE.fullmatch(tab["parent"]):
                raise PackageError(f"app {app_id}: tab.parent must be an app id")
            if (not isinstance(tab["id"], str) or not APP_ID_RE.fullmatch(tab["id"]) or
                    tab["id"] == "sessions"):
                raise PackageError(f"app {app_id}: tab.id must be a page id other than sessions")
            app["tab"] = {"parent": tab["parent"], "id": tab["id"],
                          "label": _text(tab["label"], f"apps.{app_id}.tab.label", 40)}
        if not isinstance(app["shared"], bool):
            raise PackageError(f"app {app_id}: shared is true or false")
        if spec.get("instance_label") is not None:
            if not app["shared"]:
                raise PackageError(f"app {app_id}: instance_label is for shared apps")
            app["instance_label"] = _text(spec["instance_label"], f"apps.{app_id}.instance_label", 40)
        if spec.get("reinstall") is not None:
            re_spec = spec["reinstall"]
            if not isinstance(re_spec, dict) or set(re_spec) - {"label", "title"}:
                raise PackageError(f"app {app_id}: reinstall is {{label, title}}")
            app["reinstall"] = {"label": _text(re_spec.get("label"), f"apps.{app_id}.reinstall.label", 20),
                                "title": _text(re_spec.get("title", ""), f"apps.{app_id}.reinstall.title", 300,
                                               required=False) or ""}
        if spec.get("tokens") is not None:
            t = spec["tokens"]
            if not isinstance(t, dict) or set(t) - TOKEN_KEYS:
                raise PackageError(f"app {app_id}: tokens is {{{', '.join(sorted(TOKEN_KEYS))}}}")
            scopes = t.get("scopes")
            if (not isinstance(scopes, list) or not 1 <= len(scopes) <= 8 or len(set(scopes)) != len(scopes) or
                    not all(isinstance(x, str) and SCOPE_RE.fullmatch(x) for x in scopes)):
                raise PackageError(f"app {app_id}: tokens.scopes is a list of 1 to 8 distinct names (lowercase)")
            default = t.get("default_scope", scopes[0])
            if default not in scopes:
                raise PackageError(f"app {app_id}: tokens.default_scope must be one of the scopes")
            days = t.get("days", 90)
            if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 3650:
                raise PackageError(f"app {app_id}: tokens.days is a whole number of days, 0 for no expiry")
            if tunnel in out["tunnels"] and f"{out['tunnels'][tunnel]['dir']}/tokens.sh" not in members:
                raise PackageError(f"app {app_id}: tokens needs {out['tunnels'][tunnel]['dir']}/tokens.sh in the tunnel")
            app["tokens"] = {"label": _text(t.get("label", "API tokens"), f"apps.{app_id}.tokens.label", 40),
                             "scopes": scopes, "default_scope": default, "days": days,
                             "description": _text(t.get("description", ""), f"apps.{app_id}.tokens.description", 400,
                                                  required=False) or ""}
        for key in ("open_path", "health_path"):
            if not isinstance(app[key], str) or not URL_PATH_RE.fullmatch(app[key]):
                raise PackageError(f"app {app_id}: {key} must be a path such as /lab")
        for key, what in (("token_regex", "the token"), ("password_regex", "the password")):
            if app[key] is None:
                continue
            if not isinstance(app[key], str) or len(app[key]) > 200:
                raise PackageError(f"app {app_id}: {key} must be a regular expression")
            try:
                if re.compile(app[key], re.M).groups != 1:
                    raise PackageError(f"app {app_id}: {key} needs exactly one group, {what}")
            except re.error as e:
                raise PackageError(f"app {app_id}: {key}: {e}")
        if spec.get("downloads") is not None:
            app["downloads"] = _check_downloads(spec["downloads"], app_id)
            if tunnel in out["tunnels"] and f"{out['tunnels'][tunnel]['dir']}/downloads.sh" not in members:
                raise PackageError(f"app {app_id}: downloads needs {out['tunnels'][tunnel]['dir']}/downloads.sh in the tunnel")
        if app["login_user"] is not None:
            if not isinstance(app["login_user"], str) or not LOGIN_USER_RE.fullmatch(app["login_user"]):
                raise PackageError(f"app {app_id}: login_user is a user name (letters, digits, . _ -)")
            if app["password_regex"] is None:
                raise PackageError(f"app {app_id}: login_user needs password_regex, which finds the password")
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

    # Tabs belong to a top-level app in this package. Services keep their own IDs,
    # settings and tunnels; only their location in the console changes.
    tab_ids = set()
    for app_id, app in out["apps"].items():
        tab = app.get("tab")
        if not tab:
            continue
        parent = out["apps"].get(tab["parent"])
        if parent is None or tab["parent"] == app_id or parent.get("tab"):
            raise PackageError(f"app {app_id}: tab.parent must name a different top-level app in this package")
        page = (tab["parent"], tab["id"])
        if page in tab_ids:
            raise PackageError(f"app {app_id}: duplicate tab id {tab['id']!r} in {tab['parent']}")
        tab_ids.add(page)

    # All apps sharing a tunnel must agree on its files. Refuse file/directory collisions before staging.
    for tunnel, entry in out["tunnels"].items():
        prefix = entry["dir"] + "/"
        destinations = {p[len(prefix):]: p for p in members if p.startswith(prefix)}
        for app in out["apps"].values():
            if app["tunnel"] != tunnel:
                continue
            for dest, source in app["install_files"].items():
                if dest in destinations and destinations[dest] != source:
                    raise PackageError(f"install_files for {tunnel}: conflicting destination {dest!r}")
                destinations[dest] = source
        for dest in destinations:
            parts = dest.split("/")
            if any("/".join(parts[:i]) in destinations for i in range(1, len(parts))):
                raise PackageError(f"install_files for {tunnel}: file/directory conflict at {dest!r}")
        entry["install"] = "install.sh" in destinations

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
    out["widgets"] = _check_widgets(manifest.get("widgets") or [], members)
    if not out["apps"] and not out["settings"] and not out["tunnels"] and not out["clusters"] and not out["widgets"]:
        raise PackageError("the package adds nothing: give apps, tunnels, settings, cluster presets or widgets")
    return out


def _check_widgets(widgets, members):
    """
    The `widgets` section: [{id, title, slot, entry, reads, height, description}]. A widget is one self-contained
    HTML file (inline scripts and styles, images as data: URLs) the console shows in a sandboxed frame (see
    agent-console/widgets.js): it can't reach the console's key or API, only the topics it `reads`.
    """
    if not isinstance(widgets, list) or len(widgets) > MAX_WIDGETS:
        raise PackageError(f"`widgets` is a list of at most {MAX_WIDGETS} widgets")
    out, seen = [], set()
    for w in widgets:
        if not isinstance(w, dict) or set(w) - WIDGET_KEYS:
            raise PackageError(f"a widget has the keys {sorted(WIDGET_KEYS)}")
        wid = w.get("id")
        if not isinstance(wid, str) or not APP_ID_RE.fullmatch(wid) or wid in seen:
            raise PackageError(f"widget id {wid!r}: lower-case letters, digits, _ and -, unique in the package")
        seen.add(wid)
        if w.get("slot") not in WIDGET_SLOTS:
            raise PackageError(f"widget {wid}: slot is one of {', '.join(WIDGET_SLOTS)}")
        entry = w.get("entry")
        if not isinstance(entry, str) or not entry.endswith(".html") or entry not in members:
            raise PackageError(f"widget {wid}: entry must be an .html file in the zip")
        if len(members[entry]) > MAX_WIDGET_FILE:
            raise PackageError(f"widget {wid}: {entry} is larger than 1 MB")
        try:
            members[entry].decode("utf-8")
        except UnicodeDecodeError as e:
            raise PackageError(f"widget {wid}: {entry} isn't UTF-8") from e
        reads = w.get("reads") or []
        if not isinstance(reads, list) or not all(r in WIDGET_TOPICS for r in reads) or len(set(reads)) != len(reads):
            raise PackageError(f"widget {wid}: reads is a list of {', '.join(WIDGET_TOPICS)}")
        height = w.get("height", 200)
        if isinstance(height, bool) or not isinstance(height, int) or not 60 <= height <= 1600:
            raise PackageError(f"widget {wid}: height is pixels from 60 to 1600")
        out.append({"id": wid, "title": _text(w.get("title"), f"widget {wid} title", 80), "slot": w["slot"],
                    "entry": entry, "reads": list(reads), "height": height,
                    "description": _text(w.get("description"), f"widget {wid} description", 500, required=False) or ""})
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
        for preset in manifest.get("clusters", {}):
            for m in others:
                if preset in m.get("clusters", {}):
                    problems.append(f"cluster preset {preset} is already in package {m['name']}")
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
        self.check_cluster_app_fields(manifest)
        current = {m["name"]: m for m, _ in self.installed()}.get(manifest["name"])
        return {"package": manifest, "sha256": hashlib.sha256(data).hexdigest(),
                "replaces": current["version"] if current else None, "problems": self._conflicts(manifest)}

    def check_cluster_app_fields(self, manifest):
        """Fail closed: a preset may only fill declared non-secret settings of a known app."""
        fields = {app: (set(names), set()) for app, (_, names) in self.builtin_apps.items()}
        others = [m for m, _ in self.installed() if m["name"] != manifest["name"]]
        for m in others + [manifest]:
            for app, spec in m.get("apps", {}).items():
                fields[app] = ({f["name"] for f in spec.get("settings", [])},
                               {f["name"] for f in spec.get("secrets", [])})
        for preset, spec in manifest.get("clusters", {}).items():
            check_public_defaults(spec, f"cluster {preset}")
            for app, config in spec.get("defaults", {}).get("app_settings", {}).items():
                if app not in fields:
                    raise PackageError(f"cluster {preset}: install app {app} first so its non-secret settings can be checked")
                allowed, secrets = fields[app]
                names = set(config.get("settings", {}))
                if names & secrets:
                    raise PackageError(f"cluster {preset}: app {app} secret fields cannot be stored in plaintext defaults")
                if names - allowed:
                    raise PackageError(f"cluster {preset}: app {app} defaults must use declared non-secret settings")

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
            # a hidden name of its own (NAME_RE can't start with "."), never another package's directory ("foo.old")
            old = tempfile.mkdtemp(prefix=".replaced-", dir=self.root)
            if os.path.exists(target):
                os.replace(target, os.path.join(old, "package"))
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

    def cluster_presets(self):
        """Installed configuration data; importing a package never creates a personal profile."""
        out = {}
        for manifest, _ in self.installed():
            self.check_cluster_app_fields(manifest)
            for key, spec in manifest.get("clusters", {}).items():
                out[key] = dict(spec, id=key, package=manifest["name"], package_version=manifest["version"])
        return out

    def widgets(self):
        """Every installed widget, with its package: [{key: PACKAGE/ID, package, version, ...the manifest's}]."""
        return [dict(w, key=f"{m['name']}/{w['id']}", package=m["name"], version=m["version"])
                for m, _ in self.installed() for w in m.get("widgets") or []]

    def widget_html(self, package, widget_id):
        """The widget's HTML, as text."""
        for m, d in self.installed():
            if m["name"] != package:
                continue
            for w in m.get("widgets") or []:
                if w["id"] == widget_id:
                    with open(os.path.join(d, *w["entry"].split("/")), encoding="utf-8") as f:
                        return f.read()
        raise PackageError(f"no widget {package}/{widget_id}")

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

    def install_files(self, tunnel):
        """Declared installer files, including payloads outside the package's tunnel directory."""
        return {dest: os.path.join(directory, *source.split("/"))
                for manifest, directory in self.installed() for app in manifest["apps"].values()
                if app["tunnel"] == tunnel for dest, source in app.get("install_files", {}).items()}

    def stage(self, tunnel, root):
        """
        The directory tunnel_setup --push sends for `tunnel`: tunnel/ (a packaged tunnel) and settings.d/ (the
        packages' files for it). Declared install_files join tunnel/, including an external install.sh.
        Under `root` and named by its content's hash, so it never changes once made;
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
            for dest, path in self.install_files(tunnel).items():
                target = os.path.join(tmp, "tunnel", *dest.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(path, target)
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

def _check_downloads(d, app_id):
    """An app's `downloads` section, normalized."""
    where = f"apps.{app_id}.downloads"
    if not isinstance(d, dict) or set(d) - DOWNLOAD_KEYS:
        raise PackageError(f"{where} is {{{', '.join(sorted(DOWNLOAD_KEYS))}}}")
    kinds = d.get("kinds")
    if not isinstance(kinds, list) or not 1 <= len(kinds) <= 8:
        raise PackageError(f"{where}.kinds is a list of 1 to 8 kinds of download")
    out = {"label": _text(d.get("label", "Downloads"), f"{where}.label", 40),
           "description": _text(d.get("description", ""), f"{where}.description", 600, required=False) or "",
           "kinds": []}
    for k in kinds:
        if not isinstance(k, dict) or set(k) - DOWNLOAD_KIND_KEYS:
            raise PackageError(f"{where}: each kind is {{{', '.join(sorted(DOWNLOAD_KIND_KEYS))}}}")
        kid = k.get("id")
        if not isinstance(kid, str) or not DOWNLOAD_KIND_RE.fullmatch(kid) or kid in [x["id"] for x in out["kinds"]]:
            raise PackageError(f"{where}: kind ids are distinct short names (lowercase letters, digits, _ -)")
        pattern = k.get("pattern", DOWNLOAD_NAME_RE.pattern)
        try:
            if not isinstance(pattern, str) or len(pattern) > 200:
                raise re.error("not a short string")
            re.compile(pattern)
        except re.error as e:
            raise PackageError(f"{where}: kind {kid}: pattern must be a regular expression ({e})")
        out["kinds"].append({"id": kid, "label": _text(k.get("label", kid), f"{where}.{kid}.label", 40),
                             "placeholder": _text(k.get("placeholder", ""), f"{where}.{kid}.placeholder", 80,
                                                  required=False) or "",
                             "pattern": pattern})
    return out


def download_name_ok(kind, name):
    """Whether NAME may be downloaded as KIND (a normalized kind from a `downloads` section)."""
    return (isinstance(name, str) and DOWNLOAD_NAME_RE.fullmatch(name) is not None and ".." not in name.split("/")
            and re.fullmatch(kind["pattern"], name) is not None)


def summary(manifest):
    """One line per thing a package adds, for people."""
    lines = []
    for preset, spec in manifest.get("clusters", {}).items():
        lines.append(f"cluster preset {spec['title']} ({preset}): {spec['host']}:{spec['port']}, SSH keys {spec['ssh_keys']}")
    for app_id, app in manifest["apps"].items():
        tab = app.get("tab")
        if tab:
            lines.append(f"tab {tab['label']} in {manifest['apps'][tab['parent']]['title']} "
                         f"({app_id}), tunnel {app['tunnel']}")
        else:
            lines.append(f"app {app['title']} ({app_id}), tunnel {app['tunnel']}")
        for dest in app.get("install_files", {}):
            lines.append(f"app {app_id}: install file {dest}")
        if app.get("downloads"):
            lines.append(f"app {app_id}: {app['downloads']['label']} ("
                         f"{', '.join(k['label'] for k in app['downloads']['kinds'])})")
    for tunnel, t in manifest["tunnels"].items():
        lines.append(f"tunnel {tunnel}: {t['files']} files{', with install.sh' if t['install'] else ''}")
    for w in manifest.get("widgets") or []:
        where = {"home": "the clusters page", "cluster.overview": "each cluster's Overview",
                 "cluster.tab": "a tab on each cluster's page"}[w["slot"]]
        lines.append(f"widget {w['title']} ({w['id']}) on {where}; reads {', '.join(w['reads']) or 'nothing'} "
                     f"(sandboxed: no access to the console's key or actions)")
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
