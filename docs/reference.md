# Reference

## Files on your machine

| Path | What |
| --- | --- |
| `~/.config/hpclib/agents/USER@HOST/profile.json` | a cluster's agent profile: login, tunnel ports, work directories, token name, MCP name, app ports |
| `~/.config/hpclib/agents/USER@HOST/agent_token`, `owner_token` | that cluster's tokens (mode 600) |
| `~/.config/hpclib/agents/USER@HOST/mcp.json` | the MCP client entry |
| `~/.config/hpclib/console/session` | the console's current session key (mode 600) |
| `~/.config/hpclib/console/logs/` | tunnel, app and operation logs |
| `~/.config/hpclib/console/packages/` | installed console packages |
| `~/.ssh/connections/` | sockets of the shared ssh connections |
| `~/Documents/Claude` | the default folder agents may push from and pull into |

## Files on a cluster

| Path | What |
| --- | --- |
| `~/hpclib/` | hpclib (`HPCLIB_REMOTE_INSTALL_LOCATION`); the previous copy is `~/hpclib.previous` |
| `~/.local/tunnels/config.sh` | your defaults for every tunnel |
| `~/.local/tunnels/settings/TUNNEL.sh` | a tunnel's saved settings |
| `~/.local/tunnels/settings/TUNNEL.d/` | settings files from packages (e.g. `data-transfer.d/rclone.conf`) |
| `~/.local/tunnels/secrets/TUNNEL/NAME` | secrets sent from the console (mode 600) |
| `~/.local/tunnels/sessions/TUNNEL/session-JOB.log` | a tunnel job's output |
| `~/.local/tunnels/sessions/ports/LOGINNODE-PORT` | which tunnel holds a port |
| `~/.local/tunnels/instances/TUNNEL/JOB` | running shared-instance jobs and their ports |
| `~/.local/tunnels/rest_token` | the REST owner token, or its `sha256:` hash |
| `~/.local/tunnels/rest/config.json` | the REST server's configuration |
| `~/.local/tunnels/rest/tokens.json` | scoped tokens (hashes only) |
| `~/.local/tunnels/rest/templates/` | job templates; `.replaced/` holds replaced ones |
| `~/.local/tunnels/rest/proposals/` | template proposals; `.rejected/`, `.superseded/` |
| `~/.local/tunnels/rest/audit.log` | every REST request |
| `~/.local/tunnels/rest/python` | the Python launcher `setup_agents` chose |
| `~/.local/share/hpclib/tunnels/` | installed tunnels (`HPCLIB_TUNNEL_INSTALL_LOCATION`) |
| `~/.config/hpclib/smb/` | `smbshell`'s Kerberos ticket and saved password |

## Environment variables

### Your machine

| Variable | Default | Meaning |
| --- | --- | --- |
| `HPCLIB_SSH_PERSIST` | `12h` | how long a shared ssh connection stays open after its last use |
| `HPCLIB_SSH_KEEPALIVE` | 30 s keepalives | ssh options for keepalives; empty turns them off |
| `HPCLIB_REMOTE_INSTALL_LOCATION` | `hpclib` | where hpclib is on clusters, relative to the remote home unless absolute |
| `HPCLIB_AGENTS_DIR` | `~/.config/hpclib/agents` | where agent profiles are kept |
| `HPCLIB_AGENT_LOCAL_ROOT` | `~/Documents/Claude` | the default local folder for agents |
| `HPCLIB_PYTHON` | `python3` | the Python `launch-tunnel-manager` runs |
| `HPCLIB_CONSOLE_STATIC` | the clone's `agent-console/` | another folder for the console's web page |
| `NO_COLOR`, `HPCLIB_COLOR=never` | | no colour in `setup_agents`' summary |
| `HPC_REST_URL`, `HPC_REST_TOKEN`, `HPC_REST_TOKEN_FILE` | | what `RESTClient.from_env()` and `rest_mcp.py` use; `agent_env` prints them |

### Cluster

| Variable | Default | Meaning |
| --- | --- | --- |
| `HPCLIB_TUNNEL_PATH` | `$HPCLIB_TUNNEL_INSTALL_LOCATION:$HPCLIB_DIR/tunnels` | where tunnels are looked up, first match wins |
| `HPCLIB_TUNNEL_INSTALL_LOCATION` | `~/.local/share/hpclib/tunnels` | where `install_tunnel` and packages put tunnels |
| `HPCTUNNELS_DATA_DIR` | `~/.local/tunnels` | hpclib's state directory |
| `HPCSESSIONS_DIR` | `$HPCTUNNELS_DATA_DIR/sessions` | tunnel session logs and port records |
| `HPCLIB_COMPUTE_HOST_KEYS` | ask | `accept-new`: trust new compute nodes' host keys without asking |
| `CONDA_MODULE`, `REQUIRE_CONDA` | | load conda from a module; fail without it |
| `HPCLIB_PYTHON_NAMES` | `python3` and `python3.X` | Python names `setup_agents` tries |
| `HPC_REST_PYTHON` | the launcher | the Python the REST tunnel runs |
| `HPC_REST_TOKEN_FILE` | `~/.local/tunnels/rest_token` | the REST owner token file |
| `HPC_REST_AUTO_APPROVE_TEMPLATES` | | the proposal policy for a server started by hand |
| `HPC_REST_DISABLE_FILE_CHANGES` | | `1`: refuse uploads, `mkdir`, deletes and environment syncs |
| `PROCESS_PORT_WAIT` | `900` | seconds a tunnel waits for a job to register its port |

### Set by hpclib

| Variable | Where | Meaning |
| --- | --- | --- |
| `HPCLIB_TUNNEL_PORT` | everything a tunnel starts | the port it belongs to; how leftovers are recognized |
| `PROCESS_PORT`, `SESSION_ID`, `TUNNEL_DIR` | a tunnel's job | the service port, the job id, the tunnel's folder |
| `GIT_SOCKET_PORT`, `SLURM_SOCKET_PORT` | a tunnel's job | the git helper (login node) and SLURM helper (in the job) |
| `HPC_PARAM_NAME`, `HPC_TASK_NAME` | a template job | parameters and per-task values, shell-quoted |
| `HPC_JOB_CPUS`, `HPC_JOB_MEMORY`, `HPC_JOB_SCHEDULER` | a template job under the local scheduler | its budget |

## Ports

| Port | Chosen by | Used for |
| --- | --- | --- |
| `-P` / `DEFAULT_PORT` | you, or the tunnel's default | your machine and the login node |
| `PROCESS_PORT` | the tunnel, or `--process-port` | the service on the compute node |
| agent tunnel ports | `setup_agents`, at random in 20000 to 32000, then kept | the REST server (local/login and compute) |
| app ports | the console, at random, then kept per cluster | each console app |
| `27180` | `agent_console --port` | the console |

## Bundled tunnels

| Tunnel | Service | Default ports (local, process) | Notes |
| --- | --- | --- | --- |
| `jupyter` | JupyterLab | 8950, 8888 | conda, modules or a uv/pixi project |
| `vscode` | code-server in Singularity | 8900, 3000 | `install.sh` pulls the image |
| `flask` | a Flask app | 5000, 5000 | app import path after `--` |
| `rest` | hpclib's REST server | 5000, 5000 | started by `agent_tunnel` |
| `pai` | proto-auto-interface | 8080, 3100 (or another free port the job picks) | shared instance |
| `ngl` | MDsrv | 8999, 8080 | `mdsrv` in the conda environment |
| `data-transfer` | `smbshell` and rclone's web GUI | | not a port-forwarding tunnel |

## Console HTTP API

The console's routes are listed in the module docstring of `hpclib/servers/agent_console.py`. Every `/api` route
needs `Authorization: Bearer KEY` with the session key, and the console accepts only `Host: 127.0.0.1` or
`localhost`. `--allow-origin ORIGIN` lets a page served elsewhere (a dev server) call it.
