# Extending hpclib

hpclib has five extension points:

| To add | Write | Lives |
| --- | --- | --- |
| a service you can tunnel to | a **tunnel** folder | `hpclib/tunnels/NAME`, or installed on a cluster |
| a page in the console for it | an **app**: an entry in `APPS`, or a package | `hpclib/servers/agent_console.py`, or a zip |
| apps, tunnels or settings that don't belong in hpclib | a **console package** | a zip installed with Packages |
| a dashboard on the console's pages | a **widget**: one HTML file, in a package | a zip installed with Packages |
| a kind of job an agent may run | a **job template** | `~/.local/tunnels/rest/templates/NAME` on a cluster |

## Adding a tunnel

A tunnel is a folder with at least an `sbatch_script.sh`:

```text
my-tunnel/
├── sbatch_script.sh     required: the job; starts the service on $PROCESS_PORT
├── tunnel_config.sh     ports, sbatch defaults, switches, settings (sourced by start_tunnel.sh)
├── install.sh           optional: installs what the job needs; run by tunnel_setup --install
├── user.sh              optional: defaults for settings, sourced by the job and install.sh
├── postconnect.sh       optional: replaces hpclib's (runs in the ssh session to the job)
├── preconnect.sh        optional: sourced on the login node just before connecting to the job
└── clear_port.sh        optional: clears what an earlier run left on the port (see below)
```

### sbatch_script.sh

It runs as the SLURM job (or on the login node, see below), with the tunnel's settings in its environment.
Start from this:

```bash
#!/bin/bash
# the shared job setup: ~/.bashrc, conda environment, web proxy, helper servers
if [ -f "$TUNNEL_DIR/configure_job.sh" ]
  then source "$TUNNEL_DIR/configure_job.sh"
  else source "$HPCTUNNELS_DIR/configure_job.sh"
fi
[ -f "$TUNNEL_DIR/user.sh" ] && source "$TUNNEL_DIR/user.sh"

echo "starting my service on 127.0.0.1:$PROCESS_PORT"
exec my-service --host 127.0.0.1 --port "$PROCESS_PORT" "$@"   # arguments after -- arrive as "$@"
```

Bind to `127.0.0.1`: the tunnel reaches the service through ssh on the same node, and nothing else should.
Anything printed goes to the session log, which the tunnel streams to your terminal and the console reads (for
example a login token).

### tunnel_config.sh

Shell assignments that override hpclib's defaults for this tunnel. Command-line options still win.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEFAULT_PORT` | `8080` | the port on your machine and the login node when `-P` isn't given |
| `PROCESS_PORT` | `8080` | the service's port on the compute node |
| `DEFAULT_SBATCH_ARGS` | `--time=0-8:00:00 --mem=1gb --ntasks=1` | the job's sbatch options; the user's are added after |
| `CONDA_ENVIRONMENT` | `default` | conda environment the job activates; empty for none |
| `ENABLE_WEB_PROXY` | `true` | load the `WebProxy` module in the job |
| `START_GIT_SERVER`, `START_SLURM_SERVER` | `true` | start the git helper (login node) and SLURM helper (in the job) |
| `RUN_ON_LOGIN_NODE` | `false` | run the script on the login node instead of in a job |
| `SHARED_INSTANCE` | `false` | attach to a running job of this tunnel instead of submitting one |
| `PROCESS_PORT_FROM_JOB` | `false` | the job picks and registers its port (with `SHARED_INSTANCE`) |
| `KEEP_INSTANCE` | `false` | leave the job running when the tunnel closes |
| `TUNNEL_SETTINGS` | empty | space-separated names of settings users may set (`tunnel_setup --set`, the console) |
| `TUNNEL_SECRETS` | empty | space-separated names of secrets the console may send (see below) |

Tunnels that don't need conda, the web proxy or the helper servers should turn them off; the `rest` and `flask`
tunnels are examples.

### Settings

Name each setting in `TUNNEL_SETTINGS` and give it a default in `user.sh`:

```bash
# tunnel_config.sh
TUNNEL_SETTINGS="MYAPP_IMAGE MYAPP_DATA"
# user.sh
export MYAPP_IMAGE="${MYAPP_IMAGE:-/scratch/user/$(whoami)/myapp.sif}"
export MYAPP_DATA="${MYAPP_DATA:-/scratch/user/$(whoami)/myapp-data}"
```

`tunnel_setup HOST my-tunnel --set MYAPP_IMAGE=... --save` writes `~/.local/tunnels/settings/my-tunnel.sh`,
which `start_tunnel.sh` sources before the job starts and `setup_tunnel.sh` before `install.sh` runs, so both see
the same values. Only names listed in `TUNNEL_SETTINGS` are accepted, plus the common
`HPCLIB_INSTALL_MODULES` installation setting described below.

### install.sh

Installs what the job needs (pull an image, clone a repository, create an environment). To let hpclib and the
console check and update it, declare which options it understands in a comment:

```bash
#!/usr/bin/env bash
# hpclib-install: --check --force
set -e
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/user.sh"
if [ -f "$MYAPP_IMAGE" ] && [ "${1:-}" != --force ]; then
  echo "installed: $MYAPP_IMAGE"; exit 0
fi
if [ "${1:-}" = --check ]; then
  echo "not installed: no image at $MYAPP_IMAGE"; exit 1
fi
partial="$MYAPP_IMAGE.partial.$$"; trap 'rm -f "$partial"' EXIT
singularity pull "$partial" docker://example/myapp:latest
mv -f "$partial" "$MYAPP_IMAGE"          # never leave half an image where the job looks
echo "installed: $MYAPP_IMAGE"
```

| Call | Must |
| --- | --- |
| `install.sh --check` | change nothing; exit 0 if installed, 1 if not; the last line is shown to the user |
| `install.sh` | install if missing; succeed quietly if already installed |
| `install.sh --force` | install again or update (the console shows this as Reinstall, or a label the app chooses) |

An `install.sh` without the `hpclib-install:` line is never run for a check; the console then reports the status
as unknown.

#### Modules before installation

Every installable app has an **Installation modules** field in Settings, independent of the job's modules.
Modules load in order before `install.sh`, including `--check` and `--force`, on both the login node and the
REST node. For example, `WebProxy` enables image downloads on HPRC compute nodes. A package can suggest defaults
with `apps.APP.install_modules: ["WebProxy"]`; a user can replace the list or clear it to load none.
The app settings API accepts `install_modules` as a list, `[]` to disable preloading, or `null` for package defaults.
Cluster presets may supply the same field in `defaults.app_settings.APP`.

The command-line equivalent is:

```bash
tunnel_setup HOST my-tunnel --set HPCLIB_INSTALL_MODULES=GCC/12.2.0:WebProxy --save --install --check
```

This common setting needs no entry in `TUNNEL_SETTINGS`. A tunnel may also provide a command-line default in
`tunnel_config.sh`, using `HPCLIB_INSTALL_MODULES="${HPCLIB_INSTALL_MODULES-WebProxy}"` so an explicit empty value
disables it. Modules are ordinary names, not shell commands (up to 32 names and 1024 characters in total).
If a module cannot load on the login node, Install defers to the REST node with exit 3, like an installer that
cannot run there. A failure on the REST node remains a failed installation. A standalone Check reports unknown
when its modules cannot load. Module environments are local to the installer; they do not change the REST
server or other jobs. Starting the database job still uses its own module configuration.

### Secrets

For values that must not appear in settings files or command lines (an OAuth client secret, an API key), list
their names in `TUNNEL_SECRETS`. The console's settings panel then shows password fields for them and sends the
values over ssh on standard input to `setup_tunnel.sh --secrets`, which stores each in
`~/.local/tunnels/secrets/TUNNEL/NAME` (mode 600). This excludes other Unix accounts; a process with file access
as the same account can still read them. The installer or job reads them from there:

```bash
token=$(cat "${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/secrets/my-tunnel/MYAPP_TOKEN" 2>/dev/null)
```

### App tokens

An app that keeps its own bearer tokens (PAI's write API) can have the console manage them. Ship a `tokens.sh` in
the tunnel and give the app a `tokens` section in the package manifest (`scopes`, `default_scope`, `days`, `label`,
`description`). The settings panel then has a Tokens panel: it lists the tokens, makes one (label, scopes, lifetime;
the secret is shown once, or written straight to a new file on the cluster with `save_to`), and revokes one. The
console runs `setup_tunnel.sh TUNNEL --tokens ACTION ...` over its login, which runs `tokens.sh --json ACTION ...`
in the installed tunnel with the tunnel's settings in the environment and relays the one line of JSON it prints:

| Call | Prints |
| --- | --- |
| `tokens.sh --json list` | `{"tokens": [{"id", "label", "scopes", "created", "expires", "revoked"}]}` (never a hash) |
| `tokens.sh --json create --label L --scopes a,b --days N [--save-to FILE]` | `{"id", "token", "label", "scopes", "expires"}`; with `--save-to` it writes the token to that new file (mode 600, never overwriting or following a link) and prints `"token": null, "saved_to": FILE` |
| `tokens.sh --json revoke ID` | `{"revoked": ID}` |

What a token allows is the app's business: it checks tokens itself, and the console only offers the manifest's
scopes. The console keeps no token and logs none.

A `tokens.sh` that can't work on the login node (PAI's runs the app's image, and some clusters have Singularity
only on compute nodes) exits 3, with the reason on standard error. `setup_tunnel.sh` passes that on as
`HPCLIB_TOKENS_ELSEWHERE REASON`, and the console runs the same call on the node of the cluster's REST server
instead (`POST /admin/tunnel-tokens`, owner token only), through the agent tunnel, which must then be up.

### App downloads

An app that needs large things on the cluster (OpenCode's models) can have the console download and remove them.
Ship a `downloads.sh` in the tunnel and give the app a `downloads` section in the package manifest:

```json
"downloads": {"label": "Model Download", "description": "shown under the toolbar",
              "kinds": [{"id": "ollama", "label": "Ollama", "placeholder": "granite4.2:30b",
                         "pattern": "[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?"},
                        {"id": "vllm", "label": "vLLM (Hugging Face)", "placeholder": "org/model",
                         "pattern": "[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"}]}
```

Each of the app's rows then has a toolbar: the kind, a name (which must match the kind's `pattern`, and be letters,
digits and `. _ : / @ + -`, at most 200, with no `..`), **Download**, and **Downloaded…**, a list of what is there
with sizes and **Remove**. The console runs `setup_tunnel.sh TUNNEL --downloads ACTION ...` over its login, on the
login node, which runs `downloads.sh` in the installed tunnel with the tunnel's settings in the environment:

| Call | Prints |
| --- | --- |
| `downloads.sh --json list` | `{"items": [{"kind", "name", "size" (bytes), "in_use", "note"}], "note"}` |
| `downloads.sh pull KIND NAME` | progress, for people; exit 0 once it is there. The console runs it as an operation with its log, like Install, followed by a `--check` |
| `downloads.sh --json remove KIND NAME` | `{"removed": NAME}`; refuses (exit 1, saying why on standard error) what is in use |

### clear_port.sh

`start_tunnel.sh` runs `clear_port.sh PORT` before it checks the port, so a tunnel whose service hpclib can't
stop by itself (a podman pod, say) can clean up after an earlier run. hpclib already stops anything of yours on
the port that it started (see [Ending tunnels](architecture.md#ending-tunnels-and-stale-ports)); use this for
state beyond the listening process, such as removing the pod.

### Services on the login node

With `RUN_ON_LOGIN_NODE=true`, `sbatch_script.sh` runs on the login node as a child of the tunnel, listening on
the forwarded port itself (`PROCESS_PORT` is set to it). Handle `TERM` and clean up, since that is how
the tunnel ends it, and remember that the login node is shared. `SHARED_INSTANCE` can't be combined with it.

### Shared instances

For a service several sessions should use at once (a database), set `SHARED_INSTANCE=true`,
`PROCESS_PORT_FROM_JOB=true` and `KEEP_INSTANCE=true`, and register the port from the job once the service
listens:

```bash
PROCESS_PORT=$(tunnel_pick_port "$PROCESS_PORT")       # that one if free on this node, else another
tunnel_register_instance my-tunnel "$PROCESS_PORT"
trap 'tunnel_unregister_instance my-tunnel' EXIT
```

The `pai` tunnel is a complete example.

### Testing a tunnel

```bash
install_tunnel ./my-tunnel                                   # on the cluster
launch_tunnel -P 8123 user@login.example my-tunnel           # from your machine
tunnel_setup user@login.example my-tunnel --install --check  # its installer
```

Tunnels added to hpclib itself go in `hpclib/tunnels/` and need a line in `setup.py`'s `package_data` only if
they contain files other than `*.sh` and `*.py`.

## Adding a console app

An app is a tunnel the console shows as a page, with Start, Open, Stop, Log, Settings and Install. There are
two ways to add one.

### In hpclib: an entry in `APPS`

For a tunnel that ships with hpclib, add an entry to `APPS` in `hpclib/servers/agent_console.py`:

```python
"myapp": {
    "title": "My app",
    "tunnel": "my-tunnel",
    "open_path": "/",                 # where Open goes
    "health_path": "/healthz",        # any HTTP answer here (even 401) means the service is up
    "token_re": re.compile(r"[?&]token=([0-9A-Za-z]{16,})"),   # optional: a token in the log, added to Open's URL
    "settings": [
        ("MYAPP_IMAGE", "Image", "where the image is kept; default /scratch/user/USER/myapp.sif"),
        ("MYAPP_MODE", "Mode", "fast or careful", ("fast", "careful")),   # with choices: a select
    ],
},
```

| Key | Meaning |
| --- | --- |
| `title`, `tunnel`, `open_path`, `health_path` | required for a port-forwarding app |
| `token_re` / `password_re` | read a token (added to the URL) or a password (shown with a Copy button) from the log |
| `settings` | the tunnel's `TUNNEL_SETTINGS` the settings form edits: `(NAME, label, hint[, choices])` |
| `env_settings` | JupyterLab's conda environment, modules and project fields |
| `kind: "login"` | the service runs on the login node over the console's ssh login (like Data transfer's GUI) |
| `gui_re` | with `kind: "login"`: the `HPCLIB_TRANSFER_GUI` line saying where to open it (`TRANSFER_GUI_RE`) |
| `smb` | the row also signs in to the SMB server for `smbshell`, beside the session (Data transfer) |
| `shared`, `instance_label` | a shared-instance tunnel: the row names the job serving it and offers to end it |
| `reinstall` | `{"label", "title"}`: what the install button is called once installed (PAI: Update) |

The page needs no change: it builds every app's Sessions page from `GET /api/apps`. Add a test in
`tests/test_agent_console.py`.

### Outside hpclib: a package

For an app that doesn't belong in hpclib (one group's server, a project's test instance), use a console package
instead, described next. Package apps take a subset of these keys: `title`, `tunnel`, `description`, `open_path`,
`health_path`, `token_regex` (a regular expression string with one group, the token), `password_regex` (the same for a
password the app prints, shown with **Copy password** as for VS Code), `login_user` (with `password_regex`, the user
name of the app's basic-auth login: Open then signs in with both, like Data transfer's GUI), `settings`, `secrets`, and for an
app that one running job serves for everyone, `shared`, `instance_label` and `reinstall` (what Install is called once it is
installed; PAI's is Update). An app with its own bearer tokens adds `tokens` (see App tokens), and one that
downloads things to the cluster `downloads` (see App downloads).

## Console packages

A package is a zip with `hpclib-package.json` at its top. It can add apps, the tunnels they run, and default
settings or settings files for any tunnel, including hpclib's own, without changing hpclib.

```text
our-lab/
├── hpclib-package.json
├── tunnels/my-tunnel/          sbatch_script.sh, tunnel_config.sh, install.sh, ...
└── settings/rclone.conf
```

```json
{
  "format": 1,
  "name": "our-lab",
  "version": "0.1.0",
  "description": "Our group's app and our SMB share",
  "apps": {
    "myapp": {
      "title": "My app",
      "tunnel": "my-tunnel",
      "description": "shown on its page",
      "open_path": "/",
      "health_path": "/",
      "settings": [{"name": "MY_DIR", "label": "Directory", "hint": "where it keeps things", "choices": null}],
      "secrets": [{"name": "MY_TOKEN", "label": "API token", "hint": "from the service's settings page"}]
    }
  },
  "tunnels": {"my-tunnel": "tunnels/my-tunnel"},
  "settings": {
    "data-transfer": {
      "values": {"SMB_HOST": "files.example.edu", "SMB_DOMAIN": "EXAMPLE"},
      "files": {"rclone.conf": "settings/rclone.conf"}
    }
  }
}
```

| Section | Meaning |
| --- | --- |
| `apps` | console apps: `title`, `tunnel` (one in the package or one of hpclib's), `description`, `open_path`, `health_path`, `token_regex`, `password_regex`, `login_user`, `settings`, `secrets`; for a shared app (one job for everyone, see `SHARED_INSTANCE`): `shared`, `instance_label`, `reinstall` (`{label, title}`); `tokens` (`{scopes, default_scope, days, label, description}`, with `tokens.sh` in the tunnel: see App tokens); `downloads` (`{label, description, kinds}`, with `downloads.sh` in the tunnel: see App downloads) |
| `tunnels` | tunnel folders in the zip, by name |
| `clusters` | installable cluster configurations (hostname, resource defaults and SSH policy); see [Cluster presets](cluster-presets.md) |
| `settings.TUNNEL.values` | default settings; a user's own values (per cluster) win, and the form shows the package's as placeholders |
| `settings.TUNNEL.files` | files placed in `~/.local/tunnels/settings/TUNNEL.d/` on the cluster (e.g. extra rclone remotes, or a `NAME.gui.sh` for Data transfer's `SMB_GUI`) |
| `secrets` | per app; each name must be in its tunnel's `TUNNEL_SECRETS` |
| `apps.APP.install_files` | installation files copied from the zip into that app's packaged tunnel before Check, Install or Start; see below |
| `apps.APP.install_modules` | default ordered list of modules loaded before the installer; users can replace or clear it in app Settings |

Build and check it, then install it from **Packages**:

```bash
python3 hpclib/servers/console_packages.py build our-lab/       # writes our-lab-0.1.0.zip next to the folder
```

A package can't replace hpclib's apps or tunnels or what another package provides. Its tunnel and settings files
reach a cluster with the app's next Check, Install or Start (`tunnel_setup --push`). Raise `version` with each
change; installing a newer version replaces the old one.

### Installation files

Every file inside a packaged tunnel, including its `install.sh`, already travels through the console's SSH
connection. If an app's installer or payload is elsewhere in the package, declare it in `apps.APP.install_files`:

```json
"install_files": {
  "install.sh": "install/install.sh",
  "helpers/setup.py": "install/setup.py",
  "requirements.txt": "requirements.txt"
}
```

Keys are destination paths relative to the tunnel directory; values are file paths relative to the package
root. A list such as `["install.sh", "requirements.txt"]` uses each path as both source and destination.
All sources must be plain files included in the zip. Paths cannot escape the tunnel or conflict with its files
or with another app's declarations for the same tunnel. A declaration needs a tunnel owned by the package;
packages cannot replace hpclib's built-in tunnel files.
The package root's `package.json` is reserved for installed metadata; place a payload with that name in a
subdirectory (for example, `install/package.json`) and map it to the desired destination.

HPCLIB stages these files with the tunnel and sends them in its existing `tunnel_setup --push` archive over the
console's SSH login. The cluster receives them before running `install.sh` or its `--check`. Installers run with
the tunnel directory as their working directory, so they can open the declared files using relative paths.
Changing a payload changes the staged content hash; replacing the package replaces the received tunnel tree
on its next push. Existing packages without `install_files` keep their current behavior.

### Installers that need a different node

An `install.sh` can exit 3 to say that it cannot run on the current node, for example because images need
Singularity and the login node has none. It should check that before cloning, updating or pulling images.
`setup_tunnel.sh` preserves this as exit 3 and `HPCLIB_INSTALL_ELSEWHERE`. Other failures remain failures.

In HPCLIB 0.2.71 the console responds by asking the cluster's REST server to install the same tunnel, with
the same saved settings and force flag. The package and settings have already arrived over SSH; the REST
node needs to see those files and the install directory. It also needs access to the app's download sources
(including SSH Git credentials for private repositories). The agent tunnel must be up with its REST job
running, and the console must have its owner token, as for the token-management fallback.

The owner-only `POST /admin/tunnel-install` accepts `{"tunnel": "NAME", "force": false}` and returns 202 with
an installation `id`. It starts only `setup_tunnel.sh NAME --install [--force] --check`; callers cannot supply
commands or paths. `GET /admin/tunnel-install?id=ID&offset=N` returns its state, exit code and next log chunk
(`output`, next byte `offset`, `more`). Repeating a start while the same installation is running attaches to
it. Progress is appended to the console's existing installation log, without keeping one HTTP request open
for an entire image pull. A failure on the REST node ends the operation; it is not delegated again.

How the escalation stays safe (0.2.75):

- **Only the owner can start it.** It needs the console's owner token. The REST server runs a fixed command,
  `setup_tunnel.sh NAME --install [--force] --check`, on files and saved settings that already arrived over your
  SSH login. Nothing in the request is a path, a command or a setting.
- **The installer knows where it runs.** It sees `HPCLIB_INSTALL_VIA=rest`. There an exit 3 is reported as "can't
  run on the REST server's node either"; it never asks to move again.
- **Agents can't change what it runs.** Scoped tokens can't write to the installed tunnels, hpclib or your shell
  startup files, and sandboxed jobs see them read-only (see [the MCP server](mcp-server.md#tokens-and-scopes)).
- **One installation of a tunnel at a time.** `setup_tunnel.sh` holds a lock in `~/.local/tunnels/locks` while it
  receives or installs a tunnel, so the login node and the REST node never replace each other's files. A second
  run waits for the first.
- **The console bounds what it accepts.** It waits between answers that bring nothing new and stops after 6 hours.
  It copies at most 64 MB of output, rejects oversized answers, and logs the node name only if it is a plain
  hostname.
- **Old installations are forgotten.** The REST server keeps the last 20 installations and their logs.

An installer should exit 3 only on purpose, after checking for the runtime and before changing anything, as PAI's
does. Under `set -e`, a command that happens to fail with status 3 looks the same, so end such commands with
`|| exit 1`. The console continues `HPCLIB_TOKENS_ELSEWHERE` only when `setup_tunnel.sh` also exited 3.

### Running a program in the job sandbox

A tunnel whose program should be held like a template job (an agent that runs commands, say) can run it in the
sandbox the REST server's config gives template jobs. Its job writes the program's start as a short shell script and
asks the REST server for the lines that run it there:

```bash
"$rest_python" "$HPCSERVERS_DIR/rest_server.py" --sandbox-script "$dir/service.sh" \
  --sandbox-writable "$WORK_DIR" --sandbox-read-only "$TOOLS_DIR" --sandbox-network host > "$dir/launch.sh"
bash "$dir/launch.sh"
```

- `--sandbox-writable` and `--sandbox-read-only` add to the config's own `writable` and `binds`; nothing of hpclib's
  data (`~/.local/tunnels`, templates, tokens, the job database) may be bound, either way.
- `--sandbox-network host` shares the node's network, loopback included, so the program can reach servers the job
  runs beside it (podman `--network=host`; Apptainer always shares it).
- It exits 3, saying why, when template jobs aren't sandboxed (no `sandbox` in the config, or method `none`), so
  the tunnel can refuse to run unsandboxed.
- The script is on the container's command line, which others on the node can read: keep secrets in files it reads.

The `opencode` package (OpenCode with an open-weight model) does this, and starts a REST server of its own with a
token for the job alone, for hpclib's MCP server inside the sandbox.

## Console widgets

A widget is a small dashboard: one HTML file that a package shows on the clusters page, on each cluster's
Overview, or as a tab on each cluster's page. It is lighter than an app (no tunnel, nothing on a cluster) and can
do less: it only displays data the console gives it.

```json
{"format": 1, "name": "gpu-queue", "version": "0.1.0",
 "widgets": [{"id": "gpu-queue", "title": "GPU jobs", "slot": "home",
              "entry": "widgets/gpu-queue.html", "reads": ["clusters", "jobs"], "height": 160,
              "description": "shown on Interface Settings"}]}
```

| Field | |
| --- | --- |
| `slot` | `home` (above the clusters list), `cluster.overview` (on each cluster's Overview, below its tiles) or `cluster.tab` (a tab on each cluster's page) |
| `entry` | the widget's HTML file in the zip (at most 1 MB): self-contained, with inline scripts and styles and images as `data:` URLs |
| `reads` | the topics it may subscribe to (below); the Packages dialog shows them before install |
| `height` | its starting height in pixels (60 to 1600); it then follows its content |

**The sandbox.** The console shows a widget in an `<iframe sandbox="allow-scripts">` (no `allow-same-origin`),
with a Content-Security-Policy at the top of its page that allows inline scripts and styles and nothing from the
network. So a widget can't read the console's session key or anything else on its page, can't call the console's
API or a cluster's, and can't fetch or send anything. It talks to the console only through the helper the console
puts in its page:

```html
<div id="out"></div>
<script type="module">
  const hpc = await hpclibWidget();     // {widget, context, subscribe, navigate, resize}
  hpc.subscribe("theme", ({ vars }) => {  // the page's CSS variables, so it matches light and dark
    for (const [k, v] of Object.entries(vars)) document.documentElement.style.setProperty(k, v);
  });
  hpc.subscribe("jobs", ({ byCluster }) => {
    document.getElementById("out").textContent = Object.keys(byCluster).join(", ");
  });
</script>
```

| Topic | What it gets (again on every refresh) | Refreshed |
| --- | --- | --- |
| `clusters` | `[{name, host, mcp_name, set_up, work_dirs, login, tunnel, hpclib_version}]` | 30 s |
| `jobs` | `{at, byCluster: {NAME: {queued, running, finished, counts}}}` for clusters you're logged in to (each cluster's Jobs tab, read with squeue and sacct over your login) | 60 s |
| `storage` | `{byCluster: {NAME: the last storage scan}}` | 2 min |
| `apps` | `{apps: [{id, title}], byCluster: {NAME: {APP: {state, version, port, install, ...}}}}`, never an app's URL, token or password | 30 s |
| `theme` | `{dark, vars: {"--bg": ..., ...}}`; every widget may read it | on change |

The console fetches each topic once for all the widgets on screen, and only while one wants it, so widgets add no
logins of their own; they mustn't (and can't) poll. A topic the manifest doesn't `read` gets an
`hpclib-error` event instead of data. `hpc.context.cluster` is the cluster on a cluster's page.
`hpc.navigate("#/c/grace/jobs")` moves the console's page (only to its own `#/` addresses). The frame follows
the height of its content; `hpc.resize(px)` sets it. A widget that navigates itself away from its page is
removed.

The data a widget reads (cluster names, job names, sizes) isn't secret, but it is yours: install widgets, like
any package, from people you trust. On Interface Settings → Widgets, each can be hidden and moved within its
place. `examples/console-widgets/gpu-queue` is a complete package with two widgets from one file; build it with
`python3 hpclib/servers/console_packages.py build examples/console-widgets/gpu-queue`.

## Writing job templates

A template is a folder on the cluster in `~/.local/tunnels/rest/templates/NAME/`, re-read on every request:

```text
orca/
├── template.json       required
├── script.sh           required: the job body
├── guide.md            optional: what the model reads before planning
└── examples/*.json     optional: example submissions
```

### template.json

```json
{
  "description": "Run ORCA on .inp files as one job array, one task per input.",
  "parameters": {
    "nprocs": {"type": "integer", "minimum": 1, "maximum": 32, "default": 4,
               "description": "cores per calculation; must match %pal nprocs"}
  },
  "array": {"task_parameters": {"input": {"type": "path", "kind": "file", "description": "an ORCA .inp file"}}},
  "resources": {"time": "04:00:00", "mem": "16G", "nodes": "1", "ntasks": "${nprocs}", "cpus_per_task": "1"},
  "overridable": ["time", "mem", "partition"],
  "modules": ["GCC/12.2.0", "OpenMPI/4.1.4", "ORCA/5.0.4"]
}
```

| Key | Meaning |
| --- | --- |
| `description` | one or two sentences the model reads |
| `parameters` | typed inputs; see below |
| `resources` | default sbatch resources: `time`, `mem`, `cpus_per_task`, `ntasks`, `nodes`, `partition`, `account`, `qos`, `gres`, `constraint`, `exclude`, `nodelist`; values may refer to parameters as `${name}` |
| `overridable` | resources a submission may change (default `time` and `mem`); always within the server's limits. Where a job runs is open to every submission (see below) |
| `modules` | modules loaded in order before the body |
| `array` | `task_parameters` for one array task per input, given as `tasks` or read from a manifest with `tasks_from` |
| `environment` | `{"manager": "auto", "project": "${project}"}`: run the body in a uv or pixi project's environment |
| `workdir` | the job's working directory, if not the submission's |

**Choosing nodes.** A submission may always keep its job off nodes, and pick nodes by feature or name, because
these only narrow where SLURM may run it inside the partition and limits it already has:

| Resource | sbatch | Value | Open to submissions |
| --- | --- | --- | --- |
| `exclude` | `--exclude` | a hostlist: `c03`, `c[03-05],gpu1` | always; added to the template's own `exclude`, unless `overridable` lists it |
| `constraint` | `--constraint` | node features joined by `&` or `\|`: `icelake\|cascadelake` | unless the template sets one |
| `nodelist` | `--nodelist` | one node: `c04` | unless the template sets one |

`nodelist` takes one node, since SLURM gives a job every node listed, past `max_nodes`; `constraint` takes no
counts or brackets (`[a*2&b*2]`), which ask for nodes too. Options that would get round the limits aren't
accepted at all: `--exclusive` (a whole node, past `max_cpus` and `max_mem`), `--mem-per-cpu`, `--reservation`,
`--dependency`, `--mail-user`, `--output`, `--chdir`, `--export`.

Parameter types:

| Type | Extra keys |
| --- | --- |
| `string` | `enum`, `pattern`, `max_length` |
| `integer` | `minimum`, `maximum`, `enum` |
| `number` | `minimum`, `maximum` |
| `boolean` | |
| `path` | `kind` (`file`, `directory`, `any`), `must_exist` (default true); always checked against the token's directories |

Every parameter may have a `description` and a `default`. Give each the narrowest type and bounds that work.

### script.sh

```bash
#!/bin/bash
set -euo pipefail
input="$HPC_TASK_INPUT"                        # per-task values; parameters are $HPC_PARAM_NAME
[ -f "$input" ] || { echo "no input $input" >&2; exit 2; }
cd "$(dirname "$input")"
orca "$(basename "$input")" > "${input%.inp}.out"
grep -q "ORCA TERMINATED NORMALLY" "${input%.inp}.out"      # check success, not just the exit code
```

- No `#SBATCH` lines: resources come from `template.json`.
- Use parameters only in double quotes, and never `eval` them.
- In a sandbox the script can write only to the token's directories and `/tmp`; `~` isn't writable, and it
  can't call SLURM commands.

### Guides

A `guide.md` explains what inputs to prepare, how to check results and what to do about failures; the model reads
it with `read_guide`. A folder with only a `guide.md` is a **planning guide** for a workflow with no job of its
own. The bundled `writing_templates` guide is what models read before proposing templates; it is also a good
checklist for writing one yourself.

### Trying a template

Copy the folder to the cluster, then from your machine with the agent tunnel up:

```python
client.submit_job("orca", params={"nprocs": 4}, tasks=[{"input": "scans/a/step_0.inp"}], dry_run=True)
```

A dry run validates the parameters and resources and asks SLURM for a start estimate without submitting.
