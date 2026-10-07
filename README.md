# hpclib

A collection of shell scripts and python TCP servers to simplify the process of developing code
across different HPC systems

**Documentation** is in [`hpclib/docs/`](hpclib/docs/README.md): [installation](hpclib/docs/installation.md),
using [the interface](hpclib/docs/interface/README.md) or [the command line](hpclib/docs/cli/README.md),
[the tunnel architecture](hpclib/docs/architecture.md), [the MCP server](hpclib/docs/mcp-server.md) and
[extending hpclib](hpclib/docs/extending.md). This README is the detailed reference.

## installation

On a login node run

```commandline
pip install --target=$SCRATCH --no-dependencies --upgrade --ignore-installed git+https://github.com/Tabor-Research-Group/hpclib
```

or, from a local copy of `hpclib`, install it on the cluster over the same persistent connection `pssh`/`psftp` use:

```bash
source /path/to/hpclib/hpclib/hpclib.sh
install_hpclib user@login.example                     # installs to ~/hpclib on the cluster
install_hpclib -i ~/.ssh/id_hpc -J jump.example user@login.example
install_hpclib --target /scratch/user/me/hpclib user@login.example
install_hpclib --check user@login.example             # report what would happen
```

`install_hpclib` takes the same login arguments as `pssh`/`psftp` (any `ssh` options, then `[user@]host`).
It installs only when the cluster's copy is missing or older than the local one, comparing the `HPCLIB_VERSION`
set in `hpclib/hpclib.sh` (which `setup.py` also reads), so bump that version for each release. A newer or equal
copy is left alone unless `--force` is given, and a directory that isn't an hpclib install is never replaced
without `--force`. The upload is unpacked and checked beside the target before it is swapped in, so a failed
transfer leaves the old copy in place, and the replaced copy is kept as `TARGET.previous`. Relative targets are
relative to the remote home directory. `launch_tunnel` starts tunnels from
`$HPCLIB_REMOTE_INSTALL_LOCATION` (default `hpclib`), so set that variable when installing somewhere else.

To let agents (LLM clients on your machine) run jobs through the REST server, `setup_agents` does the rest of the
cluster setup in one go, over the same connection, and `agent_tunnel` starts the tunnel:

```bash
setup_agents --work-dir /scratch/user/me/llm user@login.example    # once per cluster
agent_tunnel user@login.example                                     # each time you want the agent to work there
agent_stop user@login.example
agent_list                                                          # every cluster set up on this machine
```

`setup_agents` runs `install_hpclib`, copies the bundled `hello`, `orca` and `python_project` templates and the
`writing_templates` guide (`--templates LIST` or `all`), and writes the REST server's `config.json` with a job sandbox: template jobs
run in Singularity/Apptainer and can write only to the `--work-dir` directories (see *Sandboxed jobs* below).
Module trees on the cluster's `MODULEPATH` and any `--bind` directories are made readable to jobs, and a test
container is run on the login node. It then creates the owner token, giving the cluster only its hash, and mints
an agent token limited to the `--work-dir` directories (`--scopes`, default `read,submit,propose,files:write,envs`;
a later run adds any of these an existing token lacks, and never removes one).
It ends with a getting-started summary (in colour on a terminal; `NO_COLOR` or `HPCLIB_COLOR=never` turns that
off): how to start the tunnel, the MCP client entry for Claude Desktop as JSON, the `claude mcp add-json` line for
Claude Code, and a `curl` check. **`setup_agents` never edits your LLM client's config**: add the printed entry
yourself (the summary shows that step in red), then quit the client completely and reopen it. Until then the agent
has no tools for the new cluster, or keeps an older entry's port, token and local folders.

**Python on the cluster.** hpclib's servers need Python 3.9 or newer there (the REST server alone needs 3.7).
`setup_agents` looks for one: `python3` and `python3.X` on `PATH` (`$HPCLIB_PYTHON_NAMES` changes the list),
then the newest Python module (or Anaconda/Miniconda/Miniforge) from `module spider`/`module avail`, loading
whatever Lmod says it needs first. It records the choice on the cluster as the launcher
`~/.local/tunnels/rest/python` (and `python.json`), which the REST tunnel and `setup_agents` itself run
through, and in the profile. `--python-module MODULE` (repeatable, in load order) or `--remote-python PATH`
chooses explicitly; a later run keeps the choice unless you pass `--rebuild` or one of those, or it stops working.
The launcher saves the environment from before it loads any module, and the server gives that to `sbatch`,
`module` and the sandbox, so jobs don't inherit the server's Python modules. `HPC_REST_PYTHON` on the
cluster still overrides it for the tunnel.

Everything about a cluster is kept in its **agent profile**, `~/.config/hpclib/agents/USER@HOST/`
(`$HPCLIB_AGENTS_DIR` moves it), private to you:

| File | What |
| --- | --- |
| `profile.json` | the login, the tunnel's two ports, the work directories and binds, the token name, the MCP server name |
| `agent_token`, `owner_token` | that cluster's tokens (mode 600) |
| `mcp.json` | the MCP client entry |

The ports, one for your machine and the login node and one for the compute node, are picked at random (20000 to
32000) when the profile is made and then kept, so you don't collide with other users of the cluster or with your
other clusters; `--new-ports` picks new ones, and `--port`/`--process-port` set them. The agent token is named
after this machine (`agent-HOSTNAME`, or `--token-name`), so each of your machines can have its own and be revoked
on its own, and the MCP server is named after the cluster (`hpclib-entropy`, or `--mcp-name`), so several
clusters can be configured side by side. Options given on a later run update the profile, and a later run needs
only the address: `setup_agents user@login.example`. The MCP entry lets the agent push files from and pull them
into `~/Documents/Claude`, the Claude desktop app's own working folder (created if missing;
`$HPCLIB_AGENT_LOCAL_ROOT` changes the default). `--local-root DIR` (repeatable) replaces it with your own
directories, and `--no-local-root` leaves the agent no local file access. `--config FILE` starts the cluster's config from your own
JSON, `--no-sandbox` leaves jobs unsandboxed, `--no-install` skips `install_hpclib`, and ssh options go before the
address as for `pssh`. Scripts that use `RESTClient.from_env()` pick a cluster with `eval "$(agent_env
user@login.example)"`. Tokens from before profiles (`~/.config/hpclib/llm_token` and `rest_token`) are copied into
the profile the first time, if the cluster recognizes them.

Rerunning `setup_agents` keeps your templates, config and tokens (a config without a sandbox gets one).
`--rebuild` replaces them: the templates and config are regenerated (the old copies stay on the cluster, in
`~/.local/tunnels/rest/templates/.replaced/` and as `config.json.replaced-TIME`), the sandbox's host image is
rebuilt, the owner token's hash is reinstalled from the profile (a new token is made if there is none), and the
agent token is replaced, revoking both the token in the profile and any token with the same name. A running MCP
server picks up the new token by itself: the client re-reads its token file when the server rejects a token.

**The Agent Console** is a local backend for a front end (kept in its own repository) or for `curl`: it shows
every cluster's tunnel, jobs, template proposals and audit log, and holds the cluster tokens itself so the
browser never sees them.

To run it with its web page (`agent-console/`) from anywhere, link the launcher onto your PATH once:

```bash
git clone https://github.com/Tabor-Research-Group/hpclib.git ~/hpclib
~/hpclib/hpclib/launch-tunnel-manager --install-link   # a link in ~/.local/bin (or: --install-link DIR)
launch-tunnel-manager                                  # starts the console and opens it in your browser
```

The launcher follows its link back to the clone, so `git pull` there updates what it runs. It takes
`agent_console`'s options (`--port`, ...), `--no-open`, and `--where` (which hpclib and page it uses);
`HPCLIB_PYTHON` picks the Python (3.7 or newer). For a system-wide link use `/usr/local/bin` (with `sudo`);
macOS doesn't let anything be added to `/usr/bin`.

```bash
agent_console                                   # http://127.0.0.1:27180; prints a fresh session key
KEY=$(python3 -c 'import json,os; print(json.load(open(os.path.expanduser("~/.config/hpclib/console/session")))["key"])')
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:27180/api/clusters
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:27180/api/proposals
curl -s -H "Authorization: Bearer $KEY" -X POST -d '{"name": "xtb"}' \
  http://127.0.0.1:27180/api/clusters/user@login.example/rest/admin/proposals/approve
```

It listens on 127.0.0.1 only, refuses other `Host` headers, and needs the session key
(`Authorization: Bearer KEY`) on every `/api` call; the key is new at each launch and is written to
`~/.config/hpclib/console/session` (mode 600). Browsers on other origins are refused unless you name one with
`--allow-origin http://127.0.0.1:5173`; `--static DIR [--open]` serves a built front end from the same origin
instead: `agent_console --static agent-console --open` serves the minimal front end in this repository's
`agent-console/` folder (see its README). Routes (all JSON; the module docstring of `hpclib/servers/agent_console.py` is the reference):

| Route | What |
| --- | --- |
| `GET /api/health` | the console is up |
| `GET /api/clusters`, `GET /api/clusters/NAME` | profiles and tunnel state (`down`, `starting`, `up`, `error`); never token values |
| `GET /api/clusters/NAME/mcp` | the MCP client entry |
| `GET`/`PUT /api/clusters/NAME/settings` | this machine's tunnel settings for the cluster: `auto_approve_templates` (`all`, `new`, `review`) and `tunnel_args` (sbatch options for the tunnel job, e.g. `--time=12:00:00`), `connection_hours` (how long the ssh login is kept, default 12), and `rest_on` (`job`, the default: the REST server runs in a SLURM job; `login`: on the login node, for sites that ask for that; template jobs still go to SLURM, and `tunnel_args` don't apply); `agent_tunnel` reads them, and its own options win |
| `POST /api/clusters/NAME/tunnel/start` (`{"auto_approve_templates": "all"\|"new"\|"review"}`, default `all`), `.../tunnel/stop`, `GET .../tunnel/log` | `agent_tunnel` and `agent_stop`, logged to `~/.config/hpclib/console/logs/` |
| `ANY /api/clusters/NAME/rest/ROUTE` | the cluster's REST route, with the owner token (`?as=agent`: the agent token); downloads (`files/content`) are streamed through |
| `GET /api/jobs`, `GET /api/proposals` | jobs and pending proposals from every live cluster, with each cluster's `ok`/`error` |

The console starts tunnels without a terminal, so it first needs an ssh login it can reuse. `pssh` (and so
`agent_tunnel`) shares one ssh connection per host (`~/.ssh/connections/`), kept for 12 hours after it was last
used (`$HPCLIB_SSH_PERSIST`, e.g. `4h`; per cluster, the profile's `connection_hours`). The console's **Log in**
button opens that connection: it asks for your cluster password, ssh sends it (through `SSH_ASKPASS`, to
`hpclib/servers/console_askpass.py`, which asks the console over a private socket), and the Duo prompt that
follows is answered with a push to your phone. The password is kept in memory only until ssh has used it and is
never written anywhere. Then **Start** runs `agent_tunnel` over that login. The tunnel runs in a pseudo-terminal, as in a terminal: on a
cluster whose login node needs your password again to reach the compute node the job got (the tunnel's second
hop), the console notices the prompt and asks you for it in the page (`"prompt"` in the tunnel's state;
`POST .../tunnel/answer`; `GET /api/prompts` lists every tunnel and app session waiting for one, which the page
asks every few seconds, so the dialog opens on any page); the password goes straight to that ssh and is not
stored or logged. While that hop waits, the tunnel reads `starting`, not `error`. The first time the login node
reaches a compute node, its ssh asks whether to trust that node's host key; the page shows the key's
fingerprint and answers yes or no for you (`"kind": "hostkey"` in the prompt), and yes adds it to
`~/.ssh/known_hosts` on the cluster, as in a terminal. To take new compute nodes' keys without asking (a changed
key is still refused), set `HPCLIB_COMPUTE_HOST_KEYS=accept-new` in `~/.local/tunnels/config.sh` on the cluster. Where a cluster
allows it, ssh keys between its nodes skip that step (on the cluster: `ssh-keygen -t ed25519`, then add
`~/.ssh/id_ed25519.pub` to `~/.ssh/authorized_keys`). Other prompts (an
unknown host key, a passcode menu without a push) end the attempt with the prompt shown; accept a new host key
once in a terminal. With ssh keys and no second factor, **Log in** works without a password.

| Route | What |
| --- | --- |
| `GET /api/clusters/NAME/login` | the login's state: `none`, `starting`, `password_sent`, `push_sent`, `connected`, `expired`, `failed` |
| `POST /api/clusters/NAME/login` (`{"password": ...}`), `POST .../logout` | log in, log out (`ssh -O exit`) |
| `GET /api/apps`, `GET /api/apps/APP`, `POST /api/apps/APP/NAME/start` / `stop`, `GET .../log`, `GET`/`PUT .../settings` | tunnel apps besides the agents' (JupyterLab, VS Code, PAI): each cluster's session (`down`, `starting`, `queued` with the job's queue status, `up` with the URL to open, token included, or VS Code's password), started with `launch_tunnel` over the console's login on ports of its own; settings are the job's sbatch options, where `jupyter` comes from (`conda_env`, `modules`, a uv/pixi `project`), and the tunnel's own `settings` (e.g. `VSCODE_CONTAINER`), saved on the cluster with the next check, install or start. PAI's state names the job serving the shared database (`instance`) |
| `POST /api/apps/APP/NAME/check`, `POST .../install` (`{"force"?}`) | whether the tunnel is installed on the cluster (`install`: `installed`, `missing`, `unknown`, `nothing`), and its `install.sh` as the cluster's operation (`GET /api/clusters/NAME/operation`), through `tunnels/setup_tunnel.sh`; Start is refused while it is known to be missing |
| `POST /api/clusters` (`{"host": "user@host", "port"?, "jump"?}`) | a profile for a new cluster, to log in to and set up |
| `POST /api/clusters/NAME/install` (`{"force"?}`), `POST .../setup` (`{"work_dirs", "binds", "templates", "rebuild"}`), `GET .../operation` | `install_hpclib` or `setup_agents` over the console's login, in the background, with its log; the cluster must be logged in |

The Clusters page puts these together: **Add cluster** takes the login and a work directory, logs in, and runs
`setup_agents`; **Update hpclib** copies this machine's hpclib to a cluster (`install_hpclib`), and **Setup** reruns
`setup_agents` (it adds templates, scopes and config that new versions bring, keeping what is there). Each cluster
shows the hpclib version its REST server runs (`GET /health`'s `hpclib_version`) next to this machine's. After a
first setup, add the MCP entry `setup_agents` printed (in the operation's log) to your LLM client.

Owner-only REST routes, which the console uses (they need the owner token; agent tokens get 403):

| Route | What |
| --- | --- |
| `GET /admin/proposals` | pending proposals and the approval policy |
| `GET /admin/proposals/diff?name=` | a proposal's files, the template it would replace, a unified diff per file, and whether it is valid |
| `POST /admin/proposals/approve` (`{"name", "replace"}`) | approve; 409 if it would replace a template and `replace` is not set |
| `POST /admin/proposals/reject` (`{"name", "reason"}`) | set it aside in `proposals/.rejected/` with the reason |
| `GET /admin/audit?since=&limit=&token=&route=&status=` | audit log entries, oldest first; `latest` is the `since` for the next poll |
| `GET /admin/config`, `PUT /admin/config` (`{"changes": {SECTION: VALUE or null}}`) | the server's config.json (`~/.local/tunnels/rest/config.json`); the sections `limits`, `cluster_notes`, `environments`, `environment`, `sandbox` and `poll_interval` can be changed, are validated first, take effect at once, and the old file is kept as `config.json.replaced-TIME` |
| `GET /admin/tokens`, `POST /admin/tokens/revoke` (`{"name"}`) | token names, scopes, directories and last use (no hashes); revoke one |

## hpclib.sh

The core library for simplifying HPC workflows. Provides assorted bash functions.

## servers

A set of TCP-socket based servers to allow users to communicate with login nodes from compute nodes and containers

## tunnels

The primary utility package. Provides a generic architecture for creating port-forwarding tunnels to programs like
`Jupyter` or `coder:code server` that expose interfaces via ports.
Runs jobs via `sbatch` so that processes can take advantage of compute nodes and starts servers for further `git` and
`slurm` communication with the login node.

To create and connect to a Jupyter session, from your local machine run

```commandline
ssh -L 8895:8895 <username>@<host> ./hpclib/tunnels/start_tunnel jupyter -P 8895
```

where `8895` is just an example port.

Then by going to `http://localhost:8895` you will see your Jupyter notebook appear

### Ending tunnels and stale ports

A tunnel's pieces end with its SLURM job. When the job finishes, the session on the compute node returns, so
the ssh forward the login node holds for it closes too. Ending the tunnel yourself (Ctrl-C, closing the
terminal) cancels the job.

Each tunnel records itself under `~/.local/tunnels/sessions/ports/` by login node and port. A new tunnel on the
same port first stops anything an earlier one left behind on that login node: its script, its job, a stale
forward or waiting page. It also looks at what is actually listening on the port, so a piece that outlived
its tunnel some other way (rclone's web GUI from Data transfer, a podman pod's port forwarder) goes too: whatever
hpclib starts for a port carries `HPCLIB_TUNNEL_PORT=PORT` in its environment, and a process of yours listening
on that port with that mark is stopped (TERM, then KILL after 5 s). `smbshell gui` clears its port the same way
before it starts, and stops rclone when its own session ends, even if the connection dropped without a hangup.
If the port is held by something else, such as another user's program on a shared login node or a program of
yours hpclib didn't start, `start_tunnel.sh` stops, lists what holds it, and leaves it alone; pick another `-P`.

To clear a port from your own machine, for example after your laptop slept or the VPN dropped:

```bash
stop_tunnel -P 5050 user@login.example
```

This stops the tunnel's pieces on the login node, cancels its job, and drops the forward your local ssh
connection holds. Login nodes behind one hostname may differ between connections. If the stale tunnel was
started on another login node, the output says which one, so you can `ssh` there directly. `pssh` and the
tunnels' second hop send keepalives, so a connection that has died errors out within about two minutes instead
of hanging. Set `HPCLIB_SSH_KEEPALIVE=` to turn that off.

### Tunnel Configuration

Source `hpclib/hpclib.sh` to use the tunnel management functions. `resolve_tunnel NAME`
prints the first tunnel directory with an `sbatch_script.sh`. `resolve_tunnel_file FILE NAME`
prints a file from the first matching tunnel directory, falling back to the bundled
shared file (for example `configure_job.sh`). `start_tunnel.sh` uses these same rules.

`HPCLIB_TUNNEL_PATH` is a colon-separated list of parent directories, searched in
order. By default it is `$HPCLIB_TUNNEL_INSTALL_LOCATION:$HPCLIB_DIR/tunnels`.
`HPCLIB_TUNNEL_INSTALL_LOCATION` defaults to `~/.local/share/hpclib/tunnels`.
Set either variable before sourcing `hpclib.sh` to change the defaults.
The older `USER_TUNNEL_DIR` and `HPCTUNNELS_DIR` variables still provide defaults
when set.

Download or clone a tunnel folder, then install it on the HPC login node:

```bash
source /path/to/hpclib/hpclib.sh
install_tunnel ./my-tunnel
install_tunnel --target /another/tunnel/root ./my-tunnel
```

`--target` names a parent directory; the installed path is
`TARGET/my-tunnel`. Existing installations are left untouched. When a tunnel
includes `install.sh`, it runs during installation; if that script fails, the
tunnel folder is not installed. A custom target must be included in
`HPCLIB_TUNNEL_PATH` to be found by name in future sessions.

**Setting up a tunnel on a cluster.** What a tunnel needs there (VS Code's container image, PAI's checkout,
JupyterLab in a uv/pixi project) is installed by its `install.sh`, which `tunnels/setup_tunnel.sh` runs on the
login node; from your own machine:

```bash
tunnel_setup user@grace.hprc.tamu.edu vscode --set VSCODE_CONTAINER=/scratch/user/me/images/vscode.sif \
  --save --install --check
```

`--set NAME=VALUE` gives one of the tunnel's settings, the names in `TUNNEL_SETTINGS` in its
`tunnel_config.sh` (install paths and the like); `--save` keeps them in `~/.local/tunnels/settings/TUNNEL.sh`
on the cluster, which `start_tunnel.sh` reads, so the job and `install.sh` use the same paths. `--install` runs
`install.sh` (`--force`: e.g. pull the image again); `--check` ends with a line
`HPCLIB_TUNNEL_STATUS installed|missing|unknown|nothing MESSAGE`. An `install.sh` that can check says so in a
comment, `# hpclib-install: --check` (and `--force` if it takes it), and then answers `install.sh --check`
with exit 0 when installed and 1 when not; one without that line is never run for a check. The console's
tunnel apps use this for their **Check** and **Install** buttons and their settings.

**Shared instances.** A tunnel whose `tunnel_config.sh` sets `SHARED_INSTANCE=true` first looks for a running
instance of its service that another job serves, and connects to that job instead of submitting one (it never
cancels a job it only attached to). The job registers itself with `tunnel_register_instance TUNNEL PORT` (from
`tunnels/instances.sh`, which `configure_job.sh` loads) in `~/.local/tunnels/instances/TUNNEL/`; entries of
jobs that have left the queue are dropped. `PROCESS_PORT_FROM_JOB=true` lets the job pick its port
(`tunnel_pick_port PREFERRED`: that one if free on its node, else another) and the tunnel waits for the port the
job registers; `KEEP_INSTANCE=true` leaves the job running when the tunnel closes. PAI uses all three.

The Jupyter and VS Code tunnels require some level of configuration to get the resources installed on the HPC system.

**Flask**: install Flask in the Conda environment used by the batch job. Pass
the application import path after `--`; arguments before `--` remain tunnel or
`sbatch` options, while arguments after it reach `sbatch_script.sh`:

```bash
launch_tunnel -P 5050 user@login.example flask \
  --chdir=/scratch/user/me/project \
  --env=CONDA_ENVIRONMENT=myenv \
  -- 'mypackage.web:app'
```

This forwards local port 5050 to Flask on compute-node port 5000. The Flask
tunnel runs `flask --app APP run` bound to the compute node's loopback address,
using `PROCESS_PORT` (5000 by default), with the debugger and reloader off.
`FLASK_APP` can be set instead of supplying an app argument. Additional
arguments after the app name are passed to `flask run`. This uses Flask's
development server for interactive work.

**REST**: a dependency-free (standard library only) JSON API for SLURM and file
access, served by `hpclib/servers/rest_server.py`. Arguments after `--` go to the
server; `--allow` (repeatable) restricts file access to those directories and
their children, and without it file access is unrestricted:

```bash
launch_tunnel -P 5050 user@login.example rest \
  --chdir=/scratch/user/me/project \
  -- --allow /scratch/user/me/project --allow /scratch/user/me/data
```

Every request needs `Authorization: Bearer <token>`. The owner token is read from
`~/.local/tunnels/rest_token` on the cluster. If that file doesn't exist when the server first starts, the
server writes a random token there (mode 600) and reuses it afterwards; copy it to your machine once, and
delete the file to rotate it. The file can instead hold just `sha256:<hash of the token>`, which keeps the token
itself off the cluster; `setup_agents` (above) sets it up that way from the start.
`HPC_REST_TOKEN_FILE` points at a different file. The server refuses to start if the token file is readable by
other users. Scoped tokens are separate, and are stored as hashes in `~/.local/tunnels/rest/tokens.json`.

```bash
TOKEN=$(ssh user@login.example cat .local/tunnels/rest_token)
H="Authorization: Bearer $TOKEN"
curl -H "$H" localhost:5050/health
curl -H "$H" 'localhost:5050/slurm/squeue?arg=--me'
curl -H "$H" -X POST localhost:5050/slurm/sbatch \
  -d '{"args": ["--parsable", "run.sh"], "cwd": "jobs/a"}'
curl -H "$H" -T input.xyz 'localhost:5050/files/content?path=jobs/a/input.xyz&parents=1'
curl -H "$H" -o out.log 'localhost:5050/files/content?path=jobs/a/out.log'
```

SLURM routes are `POST /slurm/{sbatch,squeue,sacct,scontrol,scancel}` with a JSON
body `{"args": [...], "cwd": "...", "input": "..."}` (`input` is sent on stdin, so
`sbatch` can take a script inline), plus `GET` for `squeue` and `sacct` using
repeated `arg=` query parameters. They return `returncode`, `stdout` and `stderr`
with HTTP 200 whenever the command ran. File routes are `GET /files?path=` (list or
stat), `GET`/`PUT /files/content?path=` (raw download/upload; `overwrite=1`,
`parents=1`), `POST /files/mkdir?path=` and `DELETE /files?path=` (files, symlinks
and empty directories only). Relative paths resolve against the first `--allow`
directory, or the job's working directory. Uploads need a `Content-Length` and are
capped by `--max-upload` (1G by default); use `rsync` over SSH for large or
many files. Pass `--disable-file-changes` (or set
`HPC_REST_DISABLE_FILE_CHANGES=1`) to refuse uploads, `mkdir` and deletes with
403 while keeping downloads, listings and the SLURM routes; jobs submitted
through `sbatch` can still write files when they run. The whitelist limits the file routes and the `cwd` of SLURM commands;
it does not sandbox the jobs those commands submit. While the job is queued the
forwarded port serves the HTML waiting page, so clients should wait for
`/health` to return JSON.

**REST jobs for LLMs and other limited clients**: besides the raw `/slurm` routes, the REST server can run
*job templates* that you write, check them against resource limits, and give out *scoped tokens* that can
only use those templates and a few directories. `hpclib/servers/rest_mcp.py` exposes this to LLM clients
on your machine as MCP tools.

- **Templates** live in `~/.local/tunnels/rest/templates/NAME/` on the cluster: a `template.json` (a
  description, typed parameters, default resources, and which resources a client may override) and a
  `script.sh` (the job body, with no `#SBATCH` lines). Parameters reach the script as shell-quoted
  `HPC_PARAM_<NAME>` environment variables, never by text substitution, and resources become sbatch
  options only after validation. Examples are in `hpclib/tunnels/rest/templates/`; copy the ones you want.
  Templates are re-read on each request. Without a sandbox (below), a template that runs code a client wrote
  (like `python_script`), or a program whose input a client wrote (ORCA can write its output files to any
  path named in the input), runs with your account's full permissions on the cluster.
- **Limits and settings** go in `~/.local/tunnels/rest/config.json` (all optional):

  ```json
  {
    "limits": {"partitions": ["short", "gpu"], "accounts": null, "qos": null, "max_time": "04:00:00",
               "max_mem": "64G", "max_cpus": 16, "max_nodes": 1, "max_gpus": 1, "max_concurrent_jobs": 4},
    "cluster_notes": "Free text for the model, e.g. which modules or conda environments to use",
    "audit_log": "~/.local/tunnels/rest/audit.log"
  }
  ```

  Unset limits fall back to `max_time` 1 day, `max_mem` 128G, `max_cpus` 32, `max_nodes` 1, `max_gpus` 0
  and 4 concurrent jobs, with any partition, account or qos. The limits apply to template jobs only.
- **Sandboxed jobs**: add a `sandbox` section to the config to run every template body in Singularity or
  Apptainer, as you, without root:

  ```json
  "sandbox": {"method": "auto", "binds": ["/sw"]}
  ```

  The job script still loads the template's modules on the host; then the body runs in a container that
  can write only to the submitting token's directories (plus any `writable` ones), sees `/usr`, `/etc`,
  `/opt` and the `binds` read-only, and sees nothing else of the host: not your home directory, other
  projects or `~/.local/tunnels`. Its `/tmp`, `/var/tmp` and `/dev/shm` are private scratch, deleted with the
  job; its home directory is not writable, so a program that writes to `~` fails rather than losing its output. The default image is a "host image", an empty directory whose system
  directories are the host's own, so host programs and modules work unchanged and nothing has to be built.
  Your account usually comes from LDAP through SSSD, which the container can't reach, so the job writes
  `passwd` and `group` files with the host's local entries plus your own account and groups (and an
  `nsswitch.conf` that reads only those), bound read-only over the host's: programs that look up their uid,
  such as Postgres, work, and nothing beyond your own account information enters the sandbox.
  Set `image` to a `.sif` or sandbox directory to use an image of your own, `flags` for runtime options
  such as `--nv`, and `scratch` for where the container's `/tmp` lives (by default a per-job directory under
  `$TMPDIR`). With `"method": "auto"`, jobs are refused if neither runtime is on the server's PATH, unless
  `"allow_unsandboxed": true`. Sandboxed jobs run on one node: `srun` and other SLURM commands don't work
  inside them. `GET /sandbox` (the `sandbox_info` MCP tool) reports the node's security features, the
  container runtime and its site-wide bind paths, the module trees to add to `binds`, and the result of
  running a test container, with a recommended `sandbox` section. Without a `sandbox` section jobs run
  unsandboxed, as before, and the server warns about it at startup.
- **Machines without SLURM** (development servers with podman instead of Apptainer, and no `/scratch`):
  `setup_agents` notices that there is no `sbatch` and sets the cluster's `rest_on` to `login`, so the REST
  server runs on the machine itself, and writes `"scheduler": {"type": "local"}` into its config, so
  template jobs run there too. The local scheduler (`rest_local.py`) takes the place of `sbatch`, `squeue`
  and `scancel`: jobs wait their turn first in, first out within a CPU and memory budget (`cpus`, `memory`;
  by default the whole machine and 80% of its memory), are held to their `--time` (`default_time` 24 h,
  `max_time` 7 days), and keep running if the server stops. Tokens, templates, limits, proposals and the
  audit log work as on a cluster. With `"method": "podman"` (or `auto` where there's no Apptainer) the
  sandbox runs the same host image under rootless podman, as you, with no Linux capabilities,
  no-new-privileges, seccomp and no network (`"network": "default"` gives it back; environment syncs always
  have it). CPU and memory limits are only enforced where cgroups v2 delegates the cpu and memory
  controllers to you (`GET /sandbox` says whether they are); elsewhere jobs are refused, unless the
  scheduler's `"enforce_limits"` is `false` (run them unlimited) or `"memory"` (only memory must be held,
  for machines such as RHEL 9 that delegate memory but not cpu). Rootless podman can't keep its storage on
  NFS (`chown ...: operation not permitted`), so where your home is on a network file system jobs use
  `/var/tmp/<you>/hpclib-podman` instead; the sandbox's `"storage"` (`"auto"`, `"default"`, or a directory)
  sets this. Ask the machine's admin for subordinate ids (`/etc/subuid`), and
  for `loginctl enable-linger` so running jobs outlive your logins. Multi-node jobs and `--gres` GPUs are
  refused; for GPUs, give the sandbox `"flags": ["--device", "nvidia.com/gpu=all"]`.
- **Scoped tokens** are minted on the cluster and stored there only as hashes. Copy the printed token to your
  machine:

  ```bash
  python3 ~/hpclib/servers/rest_server.py --add-token llm --scopes read,submit \
    --token-allow /scratch/user/me/llm-jobs > llm_token        # shown once
  python3 ~/hpclib/servers/rest_server.py --list-tokens
  python3 ~/hpclib/servers/rest_server.py --revoke-token llm  # takes effect immediately
  ```

  The scopes are `read` (cluster info, templates, job status, reading files), `submit` (submitting and
  cancelling template jobs), `files:write`, `slurm` (the raw routes, and every API job) and `*`.
  Scoped tokens must name directories; a token only sees its own jobs; and `~/.local/tunnels` (tokens,
  templates, the job registry, the audit log, session files) is never reachable through the API, even with
  the owner token. Because a job runs as you and could read a plaintext `~/.local/tunnels/rest_token` (it
  exists once the server has started), copy the token to your machine and then run
  `rest_server.py --hash-token-file`, which leaves only a hash. Or create the file hashed from the start, as
  `setup_agents` does.
- **Endpoints**: `GET /sandbox` (see above), `GET /cluster` (partitions and node types from `sinfo`, accounts from `sacctmgr`, limits,
  templates, the token's directories, `cluster_notes`), `GET /templates`, `POST /jobs` (`{"template",
  "params", "resources", "workdir", "idempotency_key", "dry_run"}`; `dry_run` runs `sbatch --test-only`),
  `GET /jobs`, `GET /jobs/status?id=`, `GET /jobs/wait?id=&timeout=` (up to 300 s), `POST /jobs/cancel?id=`,
  plus bounded `GET /files/read?path=&offset=&length=` and `GET /files/tail?path=&lines=`. Every request is
  logged to the audit log.
- **MCP**: `rest_mcp.py` is built on the official MCP Python SDK (mcp 1.x or 2.x), which only it needs. Install
  it for the Python your LLM client will launch (`python3 -m pip install mcp`, or `pip install "hpclib[mcp]"`).
  Nothing on the cluster or in `launch_tunnel` depends on it. Start the tunnel without a browser, then point
  your LLM client at `rest_mcp.py` with the scoped token in a mode-600 file:

  ```bash
  launch_tunnel -A none -P 5050 user@login.example rest -- --allow /scratch/user/me
  ```

  ```json
  {"mcpServers": {"hpclib": {"command": "/path/to/python-with-mcp",
    "args": ["/path/to/hpclib/hpclib/servers/rest_mcp.py", "--url", "http://127.0.0.1:5050",
             "--token-file", "/Users/me/.config/hpclib/llm_token"]}}}
  ```

  The token is read from `--token-file` (unless `$HPC_REST_TOKEN` is set) and read again whenever the server
  rejects it, so replacing the file switches tokens without restarting your LLM client; restart it only to pick
  up changes to hpclib's code. Its tools are `cluster_info`, `sandbox_info`, `list_templates`, `submit_job`, `list_jobs`, `job_status`, `wait_for_job`,
  `cancel_job`, `list_files`, `read_file` and `tail_file`. `write_file` and `make_directory` are added with
  `--enable-file-writes`, and the token also needs `files:write` for them. Read-only tools are marked as such, so clients that auto-approve read-only tools can do so. Raw `sbatch`/`scontrol` are never
  exposed. `hpclib/servers/rest_client.py` is the same client for your own scripts.

**Workflows: config directories, job arrays, modules**

- **Config directories.** Each template folder in `~/.local/tunnels/rest/templates/` is a config directory. Besides
  `template.json` and `script.sh`, it can hold a `guide.md` that a client reads before planning (inputs to prepare,
  how to check results, what to do about failures), and `examples/*.json` with example submissions. A folder with
  only a `guide.md` is a *planning guide*: a multi-step workflow with no job of its own, for example "generate
  inputs locally with X, then run template Y over them". `GET /templates` and `GET /cluster` list both kinds, and
  `GET /templates/guide?name=` returns the text. The bundled `orca` template and the `writing_templates` guide are
  examples.
- **Modules in templates.** `"modules": ["GCC/12.2.0", "OpenMPI/4.1.4", "ORCA/5.0.4"]` in `template.json` loads those
  modules, in order, before the script body. They are validated as plain module names.
- **Job arrays.** A template with `"array": {"task_parameters": {...}}` runs one SLURM array task per input. A
  submission gives either `"tasks": [{...}, ...]` or `"tasks_from"`, which reads the tasks out of a JSON manifest on
  the cluster:

  ```json
  {"template": "orca", "params": {"nprocs": 4}, "label": "sample_scan", "throttle": 4,
   "tasks_from": {"path": "scans/sample_scan/scan_info.json", "key": "steps", "fields": {"input": "file"},
                  "select": [3, 7]}}
  ```

  - `fields` maps task parameters to manifest fields.
  - Paths in the manifest are relative to the manifest, and are checked against the token's directories like any
    other path.
  - `select` reruns chosen entries.
  - Each task's values reach the script as `$HPC_TASK_<NAME>`.
  - `throttle` (at most `max_concurrent_jobs`) caps how many tasks run at once. It also counts as that many of the
    concurrent-job slots.
  - `limits.max_array_tasks` (default 1000) caps the array size.
  - `GET /jobs/status?id=&tasks=1` gives every task's state, its manifest entry and its log file, and arrays report
    `task_counts` and `failed_tasks`. `label` tags a submission, and `GET /jobs?label=` filters by it.
- **Module discovery.** `GET /modules/avail?query=` and `GET /modules/spider?query=` run `module avail` and
  `module spider` through a login shell. Queries are module names only, and results are cached for 10 minutes. A
  `name/version` spider explains what must be loaded first. If `module` needs a different setup on your cluster,
  set `"module_command": [...]` in the config.
- **Template proposals.** A token with the `propose` scope can submit a template with
  `POST /templates/propose` (`name`, `template`, `script`, `guide`, `rationale`). It is validated like a real
  template and stored in `~/.local/tunnels/rest/proposals/`, and it can't run until you approve it:

  ```bash
  python3 ~/hpclib/servers/rest_server.py --list-proposals
  python3 ~/hpclib/servers/rest_server.py --approve-template xtb   # --replace to swap an existing one
  python3 ~/hpclib/servers/rest_server.py --reject-template xtb   # kept in proposals/.rejected/
  ```

  or from your machine through the Agent Console (above), or the owner-only `/admin/proposals` routes.

  A client can revise its own pending proposal by proposing again under the same name (the earlier draft is
  kept in `proposals/.superseded/`). Approved templates keep a record of who proposed and approved them in
  `templates/NAME/.proposal.json`, and a template that was replaced goes to `templates/.replaced/`.

  `agent_tunnel` skips the review by default: it starts the server with `--auto-approve-templates=all`, so a
  valid proposal becomes a template at once, including one that replaces an existing template.
  `agent_tunnel ... --auto-approve-templates=new` still holds replacements for you, and `--review-templates`
  holds every proposal. `rest_server.py` started by hand reviews everything unless given
  `--auto-approve-templates[=all]` (`$HPC_REST_AUTO_APPROVE_TEMPLATES`). Either way, this only applies while template jobs are sandboxed
  (*Sandboxed jobs* above), so an auto-approved template still writes only to the token's directories; on a
  server without a sandbox, proposals keep waiting for you. `cluster_info` tells clients which policy applies,
  and the server prints it at startup.

- **Python environments (uv and pixi).** A token with the `envs` scope (`setup_agents` gives agents one) can set
  up a project's environment in its directories: put `pyproject.toml` (and `uv.lock`) or `pixi.toml` (and
  `pixi.lock`) in a project directory, then `POST /envs/sync {"project": DIR}` (MCP `sync_environment`) runs
  `uv sync` or `pixi install` in the background, `--locked` when there is a lockfile (`"update": true`
  re-resolves). `GET /envs/sync?id=&wait=` reports its state and log, and `GET /envs?project=`
  (`environment_info`) says what a directory holds and whether its environment is ready. A template with
  `"environment": {"manager": "auto", "project": "${project}"}` runs its body in that environment; the bundled
  `python_project` template runs a script that way:

  ```json
  {"template": "python_project", "params": {"project": "projects/analysis", "script": "projects/analysis/analyze.py"}}
  ```

  Jobs only activate an environment, inside their sandbox (pixi's activation scripts included); they never
  install, so compute nodes need no network. The sync itself runs where the REST server runs, also in the
  sandbox, with only the project, the tool's cache and uv's Python directory writable, and the tool binary
  read-only. uv and pixi are found on `PATH` or in `~/.local/bin`, `~/.cargo/bin` and `~/.pixi/bin`; both are
  single binaries you can install in your home directory. The server's config takes an `environments` section:
  `{"uv": "auto"|PATH|null, "pixi": ..., "modules": ["WebProxy"], "timeout": 1800, "max_running": 2}`, where
  `modules` are loaded before a sync (e.g. a cluster's web proxy module, if the node needs one to reach PyPI or
  conda-forge). Environment variables go in a separate `environment` section,
  `{"all": {"NAME": "value"}, "jobs": {...}, "syncs": {...}}`: `jobs` and `syncs` add to (and override) `all`
  for template jobs (exported after their modules load, so they reach the sandbox) and for syncs (e.g.
  `UV_INDEX_URL`, `HTTPS_PROXY`). Names that would change `PATH`, the sandbox, hpclib or SLURM (`PATH`, `LD_*`,
  `SINGULARITY*`, `APPTAINER*`, `BASH_ENV`, `HPC_*`, `SLURM_*`, ...) are refused. Agents see the names in
  `cluster_info` but not the values, though a job can print them. The console's Settings page edits this section (and the job limits, sandbox directories and
  notes) without a restart. Prefer pixi for conda packages (xtb, openmm, rdkit); uv for pure-Python projects.
- **Moving files.** `hpclib/servers/rest_client.py`'s `FileSync` copies files between your machine and the cluster
  through the API. That means the token's scopes and directories apply, and pushing needs `files:write`. With
  `rest_mcp.py --local-root DIR` (repeatable), the MCP server offers `list_local_files`, `push_files` and
  `pull_files`, limited to those local directories.
- **New MCP tools.** `read_guide`, `list_modules`, `search_modules`, `list_template_proposals`, `environment_info`, `sync_environment`, `sync_status` and
  `propose_template`. `submit_job` takes `tasks`, `tasks_from`, `throttle` and `label`, and `job_status` takes
  `include_tasks`.

For example, a Psience scan, with a token that has `read,submit,files:write` on `/scratch/user/me/llm`:

1. Generate the scan locally with `ScanManager.generate`.
2. `push_files` the scan directory to `scans/NAME`.
3. `submit_job` the `orca` template with `tasks_from` pointing at `scans/NAME/scan_info.json`, `key` `steps` and
   `fields` `{"input": "file"}`. Run it with `dry_run` first.
4. Follow it with `job_status`.
5. `pull_files` the `*.out` files back next to the inputs, and run `ScanManager.parse` unchanged.

**Worked example**: `hpclib/examples/orca_scan/` generates a Psience scan locally, runs every point on the cluster as one ORCA job array (from Claude through the MCP server, or from a script), and brings the outputs back for `ScanManager.parse`. Its README covers the one-time cluster setup (`setup_cluster.sh`).

**VS Code**: this runs the `codercom/code-server` container. Installing the
bundled tunnel with `install_tunnel /path/to/hpclib/tunnels/vscode` runs its
`install.sh`, which pulls the image with Singularity to the path the tunnel uses:
`/scratch/user/<username>/vscode.sif` by default. Set `VSCODE_CONTAINER` before
installation and tunnel launch to use another path, or save it with `tunnel_setup ... vscode --set
VSCODE_CONTAINER=... --save` (as the console's VS Code settings do). Singularity (or Apptainer) must be
available on the login node. code-server asks for the password in `VSCODE_ROOT_DIR/.config/code-server/config.yaml`,
which the tunnel prints (and the console offers to copy).

**Data transfer (SMB)**: `smbshell` moves files between a cluster and an SMB server with rclone from the
data-transfer-tools image. It isn't a port-forwarding tunnel; `tunnels/data-transfer/` holds the command, its
SLURM job and the image's `install.sh`.

```bash
tunnel_setup user@grace.hprc.tamu.edu data-transfer --set SMB_HOST=files.example.edu \
  --set SMB_ROOT=research/our_group --set SMB_DOMAIN=EXAMPLE \
  --set SMB_REALM=AUTH.EXAMPLE.EDU --save --install   # once; the image comes from
                                                   # docker://ghcr.io/tabor-research-group/data-transfer-tools:latest
smbshell login                                            # on the cluster: kinit, a ticket jobs can use too
smbshell ls                                               # SMB_ROOT (or, without one, the server's shares)
smbshell ls proj/raw                                      # under SMB_ROOT if set, else SHARE/PATH; /SHARE/PATH
                                                          # and //HOST/SHARE/PATH are absolute
smbshell get proj/raw /scratch/user/me/raw --include '*.h5'
smbshell put /scratch/user/me/results proj/results
smbshell sync pull proj/raw /scratch/user/me/raw --dry-run  # sync deletes what the source lacks
smbshell submit --time=2:00:00 get proj/raw /scratch/user/me/raw   # the same, as a SLURM job
smbshell submit --manifest transfers.json                 # [["get", "proj/a", "/scratch/.../a"], ...]: an array job
smbshell --on user@grace.hprc.tamu.edu ls proj            # from your own machine, over ssh
smbshell gui --port 27555                                 # rclone's web GUI on the login node (see below)
```

**Rclone** (a console app, like JupyterLab): rclone's own web GUI (`rclone rcd --rc-web-gui`) on the cluster's
login node, started by `smbshell gui` over the console's ssh login, which forwards its port; Open goes straight
in. Its remotes are `smb` (the server) and `cluster` (the node's files: home, `/scratch`), for browsing and
copying between them, plus any in `~/.local/tunnels/settings/data-transfer.d/rclone.conf` on the cluster (a
settings package puts them there, e.g. an alias for your group's folder; see Console packages below). Start asks for the SMB password (unless there is a Kerberos ticket or a saved
one), which rclone keeps only in a private in-memory file until Stop; nothing is saved. rclone runs without
retries, so a refused password is one refused logon. It uses Data transfer's image and settings; rclone fetches
the GUI itself from GitHub the first time (into `~/.cache/rclone`).

Signing in (`SMB_AUTH=auto`): a Kerberos ticket from `smbshell login` (kinit on the login node, or in the image
if the node has none; kept in `~/.config/hpclib/smb/krb5cc`, so jobs use it while it lasts), else a password
saved with `smbshell save-credentials` (`~/.config/hpclib/smb/credentials`, mode 600, in rclone's reversible
encoding), else `smbshell` asks. Jobs never ask: they need the ticket or the saved password. Kerberos needs an
rclone with SMB Kerberos support (`smbshell status` says). The console's **Data transfer** page does the
signing in: Kerberos log in (kinit) where the cluster has kinit, otherwise Save password for sync jobs, both
through its password dialog, plus Install/Check for the image and the settings above.

**Console packages**: apps and settings that don't belong in hpclib (one group's server, a project's test
instance) come as zip files, installed with the console's **Add App or Settings** button, which shows what a
package adds before installing it, and lists and removes the installed ones. A package is a folder with
`hpclib-package.json` at its top, zipped:

```json
{"format": 1, "name": "our-lab-smb", "version": "0.1.0", "description": "Our SMB share and its rclone remote",
 "apps": {"myapp": {"title": "My app", "tunnel": "my-tunnel", "description": "shown on its page",
                    "open_path": "/", "health_path": "/",
                    "settings": [{"name": "MY_DIR", "label": "Directory", "hint": "where it keeps things"}]}},
 "tunnels": {"my-tunnel": "tunnels/my-tunnel"},
 "settings": {"data-transfer": {"values": {"SMB_HOST": "files.example.edu", "SMB_DOMAIN": "EXAMPLE"},
                                "files": {"rclone.conf": "settings/rclone.conf"}}}}
```

`apps` are console apps (like JupyterLab), each on a tunnel: one in the package (`tunnels`: a directory with
`sbatch_script.sh`, `tunnel_config.sh` and optionally `install.sh`, as in `hpclib/tunnels/`, and `clear_port.sh PORT`,
which `start_tunnel.sh` runs before it checks the port, to clear what an earlier run left there that hpclib can't
recognize, such as a podman pod; with
`RUN_ON_LOGIN_NODE=true` it runs on the login node rather than in a job) or one of hpclib's. `settings` give a
tunnel's apps default values (yours, per cluster, win; the settings form shows the package's value in an empty
field) and files. An app may also declare `secrets` (`[{"name": "MY_TOKEN", "label": ..., "hint": ...}]`, listed in
its tunnel's `TUNNEL_SECRETS`): its settings panel then has password fields whose values go to the cluster over
the console's ssh login on standard input (`setup_tunnel.sh --secrets`, never on a command line or in a log), into
`~/.local/tunnels/secrets/TUNNEL/NAME` (mode 600, out of the agents' reach), where the tunnel's scripts read
them; the console keeps only whether each is set. The console keeps packages in `~/.config/hpclib/console/packages/`; a packaged tunnel and the
settings files go to a cluster with the app's next Check, Install or Start (`tunnel_setup --push`, received by
`setup_tunnel.sh --receive` into `$HPCLIB_TUNNEL_INSTALL_LOCATION` and `~/.local/tunnels/settings/TUNNEL.d/`). A
package can't replace hpclib's own apps or tunnels, or what another package sets; its scripts run on your clusters
as you, so install only packages you trust. Build and check one with
`python3 hpclib/servers/console_packages.py build DIR` (it writes `NAME-VERSION.zip` next to `DIR`).

**PAI**: the proto-auto-interface database, run with `singularity-compose` from
`PAI_ROOT_DIR/proto-auto-interface` (default `/scratch/user/<username>/pai`; `install.sh` clones `PAI_REPO` there,
and pulls the app's image, `proto-auto-interface.sif`, from `PAI_IMAGE`, default
`docker://ghcr.io/tabor-research-group/proto-auto-interface:master`, and the database's,
`docker-postgres-rdkit.sif`, if they aren't there). Once it is installed, the console's **Update** (`tunnel_setup
HOST pai --install --force`) fast-forwards the checkout from its remote and pulls the app's image again; the
database's image is kept, since a newer postgres may not read the old data directory (delete the `.sif` to pull it
again). A database job that is already running keeps what it started with: End database job, then Start.
`PAI_BIND_SOURCE` (default `1`) has `singularity-compose.sh` bind that checkout's source into the containers, so
your changes run; `0` runs the images' own. It is one of PAI's settings (console: PAI → Settings → Bind source)
and applies to the next database job, not one already running.
It is shared: a PAI tunnel connects to the database another job already runs, if one does, and otherwise starts
one, on port 3100 or another free one on its node, that keeps running after the tunnel closes. End it with the
console's **End database job**, `tunnel_setup HOST pai --stop-instance JOB` (only a registered PAI job of yours;
`--instances` lists them), or `scancel`.

**Jupyter**: this requires jupyter lab to be installed in whatever `conda` environment one uses by default, and requires
that `conda` is set up when loading the environment from `~/.bashrc`

To configure that, one will either need to load a module that supplies conda in `~/.bashrc` or install something like
`miniconda` in one's scratch directory

**NGL**: this, like Jupyter, requires a package to be installed in the base conda environment, although this time it's `mdsrv`
from the people who make ngl
