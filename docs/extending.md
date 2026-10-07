# Extending hpclib

hpclib has four extension points:

| To add | Write | Lives |
| --- | --- | --- |
| a service you can tunnel to | a **tunnel** folder | `hpclib/tunnels/NAME`, or installed on a cluster |
| a page in the console for it | an **app**: an entry in `APPS`, or a package | `hpclib/servers/agent_console.py`, or a zip |
| apps, tunnels or settings that don't belong in hpclib | a **console package** | a zip installed with Add App or Settings |
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
the same values. Only names listed in `TUNNEL_SETTINGS` are accepted.

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

### Secrets

For values that must not appear in settings files or command lines (an OAuth client secret, an API key), list
their names in `TUNNEL_SECRETS`. The console's settings panel then shows password fields for them and sends the
values over ssh on standard input to `setup_tunnel.sh --secrets`, which stores each in
`~/.local/tunnels/secrets/TUNNEL/NAME` (mode 600, out of the agents' reach). The job reads them from there:

```bash
token=$(cat "${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/secrets/my-tunnel/MYAPP_TOKEN" 2>/dev/null)
```

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
| `kind: "login"` | the service runs on the login node over the console's ssh login (like Rclone) |
| `kind: "tool"` | not a tunnel: the row runs commands instead (like Data transfer) |
| `shared`, `instance_label` | a shared-instance tunnel: the row names the job serving it and offers to end it |
| `reinstall` | `{"label", "title"}`: what the install button is called once installed (PAI: Update) |

The page needs no change: it builds every app's Sessions page from `GET /api/apps`. Add a test in
`tests/test_agent_console.py`.

### Outside hpclib: a package

For an app that doesn't belong in hpclib (one group's server, a project's test instance), use a console package
instead, described next. Package apps take a subset of these keys: `title`, `tunnel`, `description`, `open_path`,
`health_path`, `token_regex` (a regular expression string with one group, the token), `settings` and `secrets`.

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
| `apps` | console apps: `title`, `tunnel` (one in the package or one of hpclib's), `description`, `open_path`, `health_path`, `token_regex`, `settings`, `secrets` |
| `tunnels` | tunnel folders in the zip, by name |
| `settings.TUNNEL.values` | default settings; a user's own values (per cluster) win, and the form shows the package's as placeholders |
| `settings.TUNNEL.files` | files placed in `~/.local/tunnels/settings/TUNNEL.d/` on the cluster (e.g. extra rclone remotes) |
| `secrets` | per app; each name must be in its tunnel's `TUNNEL_SECRETS` |

Build and check it, then install it from **Add App or Settings**:

```bash
python3 hpclib/servers/console_packages.py build our-lab/       # writes our-lab-0.1.0.zip next to the folder
```

A package can't replace hpclib's apps or tunnels or what another package provides. Its tunnel and settings files
reach a cluster with the app's next Check, Install or Start (`tunnel_setup --push`). Raise `version` with each
change; installing a newer version replaces the old one.

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
| `resources` | default sbatch resources: `time`, `mem`, `cpus_per_task`, `ntasks`, `nodes`, `partition`, `account`, `qos`, `gres`, `constraint`; values may refer to parameters as `${name}` |
| `overridable` | resources a submission may change (default `time` and `mem`); always within the server's limits |
| `modules` | modules loaded in order before the body |
| `array` | `task_parameters` for one array task per input, given as `tasks` or read from a manifest with `tasks_from` |
| `environment` | `{"manager": "auto", "project": "${project}"}`: run the body in a uv or pixi project's environment |
| `workdir` | the job's working directory, if not the submission's |

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
