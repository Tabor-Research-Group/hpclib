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
| `~/.config/hpclib/console/sync/specs.json` | versioned directory sync registrations and cached setup status (mode 600) |
| `~/.config/hpclib/console/sync/removed/` | unregistered sync records retained for review/history |
| `~/.config/hpclib/console/sync/git/GROUP/` | persistent private snapshot index, metadata inventory, excludes and private bare repository when needed |
| `~/.config/hpclib/console/sync/plans/GROUP/` | immutable, revision-bound snapshot approval records |
| `~/.config/hpclib/console/sync/runs/GROUP/` | latest 32 durable run outcomes; recovery journals and Git history are preserved |
| `~/.config/hpclib/console/sync/root-locks/`, `run-locks/` | endpoint root serialization and cross-process group-run locks |
| `~/.ssh/connections/` | sockets of the shared ssh connections |

Git sync setup also writes `.hpclib-sync.json` inside the selected local root after explicit confirmation. It
records the portable ignore choice/default-list version and may contain warning overrides; cluster bindings and
credentials remain outside it. Snapshot checks retain private remote state under
`$HPCTUNNELS_DATA_DIR/sync/GROUP/ENDPOINT/` (default `~/.local/tunnels/sync/`). Indexes/locks/inventories do not live
inside the synced working directory. Git manages objects and explicitly owned refs in an existing repository.
`refs/heads/BRANCH` and `refs/sync/GROUP/owner/ROOT_KEY` move together using Git's compare-and-swap ref transaction;
`refs/sync/GROUP/remote/TARGET` retains fetched remote history and `refs/sync/GROUP/incoming/RUN` retains incoming
objects. No acknowledged pair base or ordinary branch/index is advanced by object exchange alone.

Snapshot routes: `POST /api/sync/ID/run` and `/dry-run` require the current `revision` and return a queued run ID.
Optional `max_files` (1–1000000), `seconds` (greater than 0, up to 60), and `full_verify` (boolean) bound/choose
the check. `/dry-run` never stages file content. `GET /api/sync/ID/status` is cached;
`GET /api/sync/ID/plan/PLAN` returns an immutable snapshot review. `POST /api/sync/ID/resolve` with current
`revision`, `plan`, `action: "approve-snapshot"` and `confirm: true` queues revalidation and ingestion of that
exact complete inventory. `POST /api/sync/ID/review` queues snapshots/object exchange and a Git group merge plan.
Resolve a clean plan with `action: "apply-sync"`, its `plan`, current `revision` and `confirm: true` to apply it.
Conflicts cannot be approved. The digest binds settings/policy, coverage, root checkpoints, tips/bases and candidate;
later edits require a new review. Retry the same apply after interruption to reconcile journals, not recompute a
different candidate under its approval. Endpoint `apply.json` journals are fsynced along with their parent
directory; ordinary indexes and HEADs are never application indexes. Owned `refs/sync/GROUP/plans/PLAN` retain
reviewed candidates, `/conflicts/PLAN` retain conflict trees, and `/base/TARGET` retain per-pair acknowledged commits.

Background scheduling (1.14.8): `policy.background` is explicitly boolean and defaults to off for existing
registrations. When enabled without an `on_change`, uploads (`auto-push`, direction `push`) are the default.
`auto-both` requires direction `both`; `notify` performs checks only. `interval` remains 0 (manual) or 10–86400
seconds. The existing console thread admits at most two group workers and shares their locks, shutdown and
maintenance admission. Reviews/recovery block scheduling; transient failures back off up to one hour.

`GET /api/sync/ID/provision` probes setup and pins observed endpoint path/inode identities in the registration;
legacy Git identity records are honored. Missing previously claimed roots are inactive. `POST` to that route
requires current `revision`, preview `review`, `confirm: true`, an `action` (`mkdir`, `init`, `private-git`,
`ignore-apply`), and `endpoint` for endpoint writes. `ignore-apply` also requires `mode` and `ignore_review`
from `GET /api/sync/ID/provision-ignore?endpoint=ENDPOINT`. Setup writes share group-run admission locks;
creation uses held ancestor descriptors and refuses changed parents/symlinks. Git initialization uses a held
root descriptor and leaves files/ordinary branches intact. Setup performs no sync. Remote setup RPCs remain
owner-side cached-SSH operations; agent actions cannot choose paths, grant access, initialize Git or mkdir.

Uploads reuse native push, scoped private indexes/inventories (`upload-index`, `upload-inventory.json`), Git
merge-tree/read-tree and existing apply journals. Remote `upload.json` acknowledges the source/candidate;
`refs/sync/GROUP/upload-source` retains the previous source base. Source/previous-source paths alone form the
remote scan/apply scope. No remote objects are fetched locally in upload review/apply. Initial application and
managed remote edits require review; clean later uploads may apply automatically under the saved policy.
A successful upload means local inputs were delivered, not equality of complete working directories.

## Files on a cluster

Agent sync control (1.14.5): Data transfer actions `sync_query` and `sync_manage` are disabled by default.
The local consent route is `POST /api/sync/ID/agent-control` with current `revision`, boolean `allow` and
`confirm: true`. Consent is a separate registration permission, so revocation does not invalidate recovery plans.
The console polls remote action tickets through its existing SSH master only for enabled groups. Tokens can
operate only groups whose targets are all on that cluster and within their directory scope; paths and consent
cannot be changed by these actions.

Remote `$HPCTUNNELS_DATA_DIR/sync/agent-requests/` holds private actor-scoped requests, leases and results.
Requests expire after five minutes; only the most recent bounded terminal results are retained per actor.
`sync_query operation=request-status` polls a ticket. A successful ticket can contain an accepted run ID;
follow that run with `sync_query operation=status`. Local `console/sync/agent-requests/` receipts prevent
re-execution after lost replies, and uncertain dispatch reports an error instead of replaying a mutation.
The REST server's private scope files include the authenticated actor name; no console key is sent remotely.
Restart both the console and REST tunnel after upgrading these modules.

Native object transport (1.14.7) invokes Git fetch/push with controlled upload-pack/receive-pack endpoints over
the existing SSH master. No named remote is added. The receiving endpoint exposes only the owned incoming-run
ref; normal branch refs are not push destinations. Git verifies/stages objects before reviewed working-file
application. `$HPCTUNNELS_DATA_DIR/sync/locks/ROOT_DIGEST.apply.json` records unfinished directory application
across per-group private repositories; locally the same record lives under the existing root-lock directory.
An acknowledged record permits later work; an unfinished record requires the owning reviewed plan to reconcile.
The existing detailed `apply.json` journal remains the authority for phases and per-endpoint acknowledgements.
These guards do not establish a shared identity for independent local consoles.

| Path | What |
| --- | --- |
| `~/hpclib/` | hpclib (`HPCLIB_REMOTE_INSTALL_LOCATION`); the previous copy is `~/hpclib.previous` |
| `~/.local/tunnels/config.sh` | your defaults for every tunnel |
| `~/.ssh/id_ed25519_hpclib_compute`, `.pub` | login-to-compute identity created by `setup_cluster_ssh`, in the shared home |
| `~/.ssh/authorized_keys` | incoming public-key authorizations, managed one entry at a time |
| `~/.ssh/hpclib_keys.conf` | console-managed host identity bindings included from `~/.ssh/config` |
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
| `~/.local/tunnels/rest/jobs.sqlite` | agent-API job history, token/label attribution and observed scheduler details; configurable with `jobs_db` |
| `~/.local/tunnels/rest/python` | the Python launcher `setup_agents` chose |
| `~/.local/share/hpclib/tunnels/` | installed tunnels (`HPCLIB_TUNNEL_INSTALL_LOCATION`) |
| `~/.config/hpclib/smb/` | `smbshell`'s private Kerberos ticket; old password files are unused |

## Environment variables

### Your machine

| Variable | Default | Meaning |
| --- | --- | --- |
| `HPCLIB_SSH_PERSIST` | `12h` | how long a shared ssh connection stays open after its last use |
| `HPCLIB_SSH_KEEPALIVE` | 30 s keepalives | ssh options for keepalives; empty turns them off |
| `HPCLIB_REMOTE_INSTALL_LOCATION` | `hpclib` | where hpclib is on clusters, relative to the remote home unless absolute |
| `HPCLIB_AGENTS_DIR` | `~/.config/hpclib/agents` | where agent profiles are kept |
| `HPCLIB_AGENT_LOCAL_ROOT` | none | the local folder for agents' files when `setup_agents` isn't given `--local-root` (it asks in a terminal otherwise) |
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
| `HPCLIB_COMPUTE_IDENTITY_FILE` | `~/.ssh/id_ed25519_hpclib_compute` when it exists | automatic compute identity; empty disables; caller `-i` takes precedence |
| `CONNECT_TO_JOB_SSH_ARGS` | unset | Bash array of extra SSH options; preserves spaces in option values |
| `CONDA_MODULE`, `REQUIRE_CONDA` | | load conda from a module; fail without it |
| `HPCLIB_PYTHON_NAMES` | `python3` and `python3.X` | Python names `setup_agents` tries |
| `HPC_REST_PYTHON` | the launcher | the Python the REST tunnel runs |
| `HPC_REST_TOKEN_FILE` | `~/.local/tunnels/rest_token` | the REST owner token file |
| `HPC_REST_AUTO_APPROVE_TEMPLATES` | | the proposal policy for a server started by hand |
| `HPC_REST_DISABLE_FILE_CHANGES` | | `1`: refuse uploads, `mkdir`, deletes, environment syncs, and SSH management mutations |
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
| `data-transfer` | `smbshell` and its web GUI (rclone's, or `SMB_GUI`'s) | | `smbshell gui` on the login node |

## Console HTTP API

The console's routes are listed in the module docstring of `hpclib/servers/agent_console.py`. Every `/api` route
needs `Authorization: Bearer KEY` with the session key, and the console accepts only `Host: 127.0.0.1` or
`localhost`. `--allow-origin ORIGIN` lets a page served elsewhere (a dev server) call it.
