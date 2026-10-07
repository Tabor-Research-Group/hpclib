# Tunnels on the command line

Every tunnel is started the same way:

```bash
launch_tunnel -P LOCAL_PORT user@login.example TUNNEL [sbatch options] [-- job arguments]
```

and stopped with Ctrl-C or `stop_tunnel -P LOCAL_PORT user@login.example`. This page covers what is specific to
each bundled tunnel, and how to configure and install tunnels on a cluster.

## Configuring a tunnel on a cluster: `tunnel_setup`

Many tunnels need something on the cluster first (a container image, a checkout, a Python environment) and
have settings that say where it is. `tunnel_setup` handles both from your machine:

```bash
tunnel_setup user@login.example vscode --set VSCODE_CONTAINER=/scratch/user/me/images/vscode.sif \
  --save --install --check
```

| Option | Meaning |
| --- | --- |
| `--set NAME=VALUE` | one of the tunnel's settings (the names in `TUNNEL_SETTINGS` in its `tunnel_config.sh`); repeatable |
| `--save` | keep the settings in `~/.local/tunnels/settings/TUNNEL.sh` on the cluster, where the job and the installer read them |
| `--install` | run the tunnel's `install.sh` |
| `--force` | with `--install`: install again (pull a newer image, update a checkout) |
| `--check` | report whether it is installed, as a last line `HPCLIB_TUNNEL_STATUS installed\|missing\|unknown\|nothing MESSAGE` |
| `--push DIR` | send a tunnel folder from your machine to the cluster first (how console packages reach a cluster) |

Settings saved this way are what the console's app Settings edit, so the two stay in step.

## Your defaults for every tunnel

`~/.local/tunnels/config.sh` on the cluster is read by every tunnel before its own configuration:

```bash
DEFAULT_SBATCH_ARGS="--time=0-8:00:00 --mem=1gb --ntasks=1"   # the base for tunnels that don't set their own
CONDA_MODULE=Anaconda3            # load conda from a module where it isn't on PATH
REQUIRE_CONDA=true                # fail rather than continue without the conda environment
HPCLIB_COMPUTE_HOST_KEYS=accept-new   # trust new compute nodes' host keys without asking
```

## JupyterLab: `jupyter`

```bash
launch_tunnel -P 8950 user@login.example jupyter --time=12:00:00 --mem=30gb
```

Default job: 8 hours, 30 GB, 4 tasks on one node; Jupyter listens on 8888 on the compute node. JupyterLab is
taken from, in order of preference:

| Setting | Source |
| --- | --- |
| `HPCLIB_JUPYTER_PROJECT` | the environment of a uv or pixi project in that directory |
| `HPCLIB_JUPYTER_MODULES` | modules to load, colon-separated (e.g. `GCCcore/13.2.0:JupyterLab/4.2.0`) |
| `CONDA_ENVIRONMENT` | a conda environment (conda must be available from `~/.bashrc` or `CONDA_MODULE`) |

```bash
tunnel_setup user@login.example jupyter --set HPCLIB_JUPYTER_PROJECT=/scratch/user/me/analysis \
  --save --install --check        # installs jupyterlab into the project if it is missing
```

The job's log contains the URL with Jupyter's token; open that URL, or use the console, which adds the token
for you.

## VS Code: `vscode`

Runs the `codercom/code-server` container with Singularity.

```bash
tunnel_setup user@login.example vscode --set VSCODE_CONTAINER=/scratch/user/me/vscode.sif --save --install
launch_tunnel -P 8900 user@login.example vscode
```

| Setting | Default |
| --- | --- |
| `VSCODE_CONTAINER` | `/scratch/user/USER/vscode.sif` |
| `VSCODE_ROOT_DIR` | `/scratch/user/USER`: where VS Code opens and keeps its config |
| `VSCODE_BIND_PATHS` | directories bound into the container (`singularity --bind` syntax) |

code-server asks for a password; the tunnel prints it (from `VSCODE_ROOT_DIR/.config/code-server/config.yaml`)
once the job is up.

## Flask: `flask`

Runs a Flask app from your code on a compute node. The app's import path goes after `--`:

```bash
launch_tunnel -P 5050 user@login.example flask \
  --chdir=/scratch/user/me/project --env=CONDA_ENVIRONMENT=myenv \
  -- 'mypackage.web:app'
```

Flask must be installed in the job's conda environment. It runs Flask's development server bound to the compute
node's loopback, on port 5000, with the debugger and reloader off. Arguments after the app name go to
`flask run`.

## PAI: `pai`

The proto-auto-interface database. It is a **shared instance**: a PAI tunnel connects to a PAI database job
that is already running, if there is one, and otherwise submits one that keeps running after the tunnel
closes.

```bash
tunnel_setup user@login.example pai --set PAI_REPO=https://github.com/Tabor-Research-Group/proto-auto-interface.git \
  --save --install                  # clones the code and pulls the two images
launch_tunnel -P 3100 user@login.example pai
tunnel_setup user@login.example pai --install --force    # update: fast-forward the checkout, pull the app image
tunnel_setup user@login.example pai --instances          # running PAI database jobs
tunnel_setup user@login.example pai --stop-instance JOB  # end one (only a registered PAI job of yours)
```

| Setting | Meaning |
| --- | --- |
| `PAI_ROOT_DIR` | holds the checkout and the images; default `/scratch/user/USER/pai` |
| `PAI_REPO` | the git URL to clone |
| `PAI_IMAGE` | where the app image is pulled from; default `docker://ghcr.io/tabor-research-group/proto-auto-interface:master` |
| `PAI_BIND_SOURCE` | `1` (default): run the checkout's code in the containers; `0`: the image's own |
| `INCLUDE_DEV_ENDPOINTS` | `true` (default) or `false` |

Updating leaves the database's own image alone, since a newer Postgres may refuse the old data directory. A
running database job keeps what it started with until you end it and start again.

## NGL: `ngl`

Runs MDsrv (the NGL molecular viewer's server). `mdsrv` must be installed in the conda environment the job
activates.

```bash
launch_tunnel -P 8999 user@login.example ngl
```

## REST: `rest`

The REST server for scripts and agents. It is usually started for you by `agent_tunnel`; see
[Agents](agents.md). By hand:

```bash
launch_tunnel -A none -P 5050 user@login.example rest -- --allow /scratch/user/me/project
```

## Services on the login node

Some sites prefer small services on the login node to a job on a compute node, and some machines have no SLURM.
`--login-node` runs a tunnel's job script on the login node itself, as a child of the tunnel, ending when the
tunnel ends:

```bash
launch_tunnel -P 5050 user@login.example rest --login-node
```

There is no job, so sbatch options are ignored. Tunnels whose `tunnel_config.sh` sets `RUN_ON_LOGIN_NODE=true`
always run this way. Shared-instance tunnels (like PAI) can't.

## Installing tunnels that don't come with hpclib

A tunnel is a folder. Copy or clone it to the cluster, then:

```bash
install_tunnel ./my-tunnel                          # into ~/.local/share/hpclib/tunnels/my-tunnel
install_tunnel --target /some/other/root ./my-tunnel
```

If the folder has an `install.sh`, it runs in the installed copy, and the copy is removed again if it fails. An
existing installation is left alone. A custom `--target` must be on `HPCLIB_TUNNEL_PATH` to be found by name.

`resolve_tunnel NAME` prints which folder a name resolves to; installed tunnels come before hpclib's own, so you
can override a bundled tunnel by installing one with the same name. To write a tunnel, see
[Extending hpclib](../extending.md#adding-a-tunnel).

## File transfers with SMB: `smbshell`

`smbshell` moves files between a cluster and an SMB file server with rclone, from the data-transfer-tools image.
Configure and install once:

```bash
tunnel_setup user@login.example data-transfer --set SMB_HOST=files.example.edu \
  --set SMB_ROOT=research/our_group --set SMB_DOMAIN=EXAMPLE --set SMB_REALM=AUTH.EXAMPLE.EDU \
  --save --install
```

Then, on the cluster (or from your machine with `smbshell --on user@login.example ...`):

```bash
smbshell login                                   # kinit: a Kerberos ticket jobs can use too
smbshell save-credentials                        # or: a saved password, for unattended jobs
smbshell ls                                      # SMB_ROOT (or the server's shares)
smbshell get proj/raw /scratch/user/me/raw --include '*.h5'
smbshell put /scratch/user/me/results proj/results
smbshell sync pull proj/raw /scratch/user/me/raw --dry-run     # sync deletes what the source lacks
smbshell submit --time=2:00:00 get proj/raw /scratch/user/me/raw  # the same, as a SLURM job
smbshell submit --manifest transfers.json        # [["get", "proj/a", "/scratch/.../a"], ...]: a job array
smbshell gui --port 27555                        # rclone's web interface on the login node
smbshell status                                  # how it would sign in, and what rclone supports
```

Paths are relative to `SMB_ROOT` when it is set; `/SHARE/PATH` and `//HOST/SHARE/PATH` are absolute. Signing in
uses a Kerberos ticket if there is one, else the saved password, else asks; jobs never ask.
