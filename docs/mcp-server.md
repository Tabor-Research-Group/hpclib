# The MCP server and REST API

hpclib lets an LLM client run jobs on a cluster without giving it a shell. The client gets a fixed set of typed
tools; each tool is a request to a REST server on the cluster; the REST server accepts only what the request's
token allows, runs only job templates you have approved, and runs them in a sandbox that can write only to the
directories you chose. This page describes that design and how to configure it.

## The pieces

```text
 your machine                                              cluster
 ────────────                                              ───────
 LLM client ──stdio──► rest_mcp.py ──HTTP, over the agent tunnel──► rest_server.py ──► sbatch / local scheduler
 (Claude)              MCP server     127.0.0.1:PORT                 REST server          │
                       holds the token                               checks token,        ▼
                       ~/.config/hpclib/agents/…                     scopes, template,   job body in the
                                                                     limits; audit log   sandbox (Apptainer,
                                                                                         Singularity or podman)
```

| Piece | Runs | Role |
| --- | --- | --- |
| `hpclib/servers/rest_mcp.py` | your machine, started by the LLM client | translates MCP tool calls into REST requests; keeps the token out of the model's context |
| `hpclib/servers/rest_client.py` | inside `rest_mcp.py`, or your scripts | the HTTP client: token handling, file push and pull |
| the agent tunnel (`rest`) | login node to compute node | forwards a port on your machine to the REST server; `agent_tunnel` starts it |
| `hpclib/servers/rest_server.py` | cluster, in a job or on the login node | authenticates, authorizes, validates and submits; standard library only |
| `rest_jobs.py`, `rest_sandbox.py`, `rest_envs.py`, `rest_local.py` | cluster, inside the REST server | templates and the job registry, the sandbox, uv/pixi environments, the local scheduler for machines without SLURM |

Only `rest_mcp.py` needs a third-party package (the MCP SDK). Nothing on the cluster does.

## Design principles

**The model never holds a credential.** The token lives in a mode-600 file on your machine that `rest_mcp.py`
reads; requests carry it, but no tool returns it. Replacing the file (for example after `setup_agents --rebuild`)
takes effect without restarting the client, because the client reads the file again when the server rejects a
token.

**The cluster stores no usable secret.** Tokens are stored on the cluster only as SHA-256 hashes. A job runs as
you and could read any file you can, so a plaintext token on the cluster would let a job escalate; hashes don't.

**Allow-lists, not deny-lists.** An agent can submit only templates that exist in the templates directory, with
parameters of the declared types and bounds, resources within the configured limits, in directories its token
names. Raw `sbatch` and `scontrol` are never exposed through MCP.

**Parameters are data, never code.** Template parameters reach the job script as shell-quoted environment
variables (`$HPC_PARAM_NAME`), never by substituting text into the script. Resources become sbatch options only
after validation.

**Jobs are contained.** With a sandbox (the default from `setup_agents`), the job body runs in a container that
can write only to the token's directories and private scratch, sees system directories read-only, and sees
nothing else of your account: not your home directory, other projects, or hpclib's own state.

**Everything is recorded.** Every request, allowed or refused, goes to an audit log with the token name, route,
status and the job or file involved. The console's Activity page shows it.

**Content is not instruction.** The MCP server's instructions tell the model that file contents and job output
are data from the cluster and must not be followed as directions.

## Tokens and scopes

| Token | Can do | Stored |
| --- | --- | --- |
| owner | everything, including the `/admin` routes (approving proposals, editing config, revoking tokens) | `~/.local/tunnels/rest_token` on the cluster, as `sha256:HASH` when made by `setup_agents`; the token itself in your agent profile |
| scoped | what its scopes allow, only in its directories, only its own jobs | hashes in `~/.local/tunnels/rest/tokens.json` |

| Scope | Allows |
| --- | --- |
| `read` | cluster information, templates and guides, module searches, job status, listing and reading files |
| `submit` | submitting and cancelling template jobs |
| `propose` | proposing new templates |
| `files:write` | uploading files, making directories, deleting files and empty directories |
| `envs` | setting up uv/pixi environments in its directories |
| `slurm` | the raw `/slurm` routes; never given to agents |
| `*` | all of the above |

`setup_agents` gives the agent token `read,submit,propose,files:write,envs`. Whatever the token,
`~/.local/tunnels` (tokens, templates, the job registry, the audit log) is never reachable through the file
routes.

## The tools

| Tool | Route | Read-only |
| --- | --- | --- |
| `cluster_info` | `GET /cluster`: partitions, limits, templates, guides, allowed directories, notes, proposal policy | yes |
| `sandbox_info` | `GET /sandbox`: the node's isolation features and a self-test | yes |
| `list_templates`, `read_guide` | `GET /templates`, `GET /templates/guide` | yes |
| `submit_job` | `POST /jobs`, with `dry_run`, `idempotency_key`, `label`, and `tasks`/`tasks_from`/`throttle` for arrays | |
| `list_jobs`, `job_status`, `wait_for_job` | `GET /jobs`, `/jobs/status`, `/jobs/wait` (up to 300 s) | yes |
| `cancel_job` | `POST /jobs/cancel` | |
| `list_files`, `read_file`, `tail_file` | `GET /files`, `/files/read`, `/files/tail`, bounded in size | yes |
| `list_modules`, `search_modules` | `module avail` / `module spider`, cached 10 minutes | yes |
| `environment_info`, `sync_environment`, `sync_status` | uv/pixi environments (`/envs`) | partly |
| `list_template_proposals`, `propose_template` | `/templates/proposals`, `/templates/propose` | partly |
| `list_local_files`, `push_files`, `pull_files` | files on your machine, only under `--local-root` folders | partly |
| `write_file`, `make_directory` | only with `--enable-file-writes` and the `files:write` scope | |

Read-only tools are marked as such, so clients that auto-approve read-only tools can.

The suggested flow, which the server gives the model as instructions: `cluster_info` → `read_guide` →
`list_templates` → prepare inputs (`push_files`) → `submit_job` with `dry_run` → `submit_job` for real with an
idempotency key → `job_status` → `tail_file` → `pull_files`.

## Templates

A template is a folder in `~/.local/tunnels/rest/templates/` holding `template.json` (description, typed
parameters, default resources, which resources a client may override, modules, optionally an array definition or
a Python environment) and `script.sh` (the job body, no `#SBATCH` lines). A folder may also have a `guide.md`
the model reads before planning, and `examples/*.json`. A folder with only a `guide.md` is a planning guide for a
multi-step workflow. The format is in [Extending hpclib](extending.md#writing-job-templates).

### Proposals

When no template fits, the model can draft one with `propose_template`. The proposal is validated like a real
template and stored in `~/.local/tunnels/rest/proposals/`. It can't run until approved:

| Policy | Started with | Behaviour |
| --- | --- | --- |
| all (default for `agent_tunnel`) | `--auto-approve-templates=all` | a valid proposal becomes a template at once |
| new | `--auto-approve-templates=new` | new names at once; replacements wait for you |
| review | `--review-templates` | everything waits for you |

Automatic approval only applies while jobs are sandboxed, so an auto-approved template still writes only to the
token's directories. Approved templates record who proposed and approved them; replaced ones are kept in
`templates/.replaced/`.

## The sandbox

| Method | Used where | Isolation |
| --- | --- | --- |
| `singularity` / `apptainer` | clusters | a container as you, without root, writable only in the token's directories, `writable` paths and private `/tmp`; the host's `/usr`, `/etc`, `/opt` and `binds` read-only |
| `podman` | servers without SLURM | the same file view under rootless podman, plus no Linux capabilities, no new privileges, seccomp, and no network (environment syncs get the network) |
| `auto` | default | Singularity/Apptainer if present, else podman |

The default image is a **host image**: an empty directory whose system directories are bound from the host, so
host programs and modules work unchanged and nothing has to be built. The job writes `passwd` and `group` files
with your own account so programs that look up their user work without the sandbox reaching LDAP.

Sandboxed jobs run on one node and can't call SLURM commands. `sandbox_info` reports the node's features, the
container runtime, the module trees to bind, and runs a test container.

## Machines without SLURM

On a development server with podman and no SLURM, the REST server runs on the machine itself
(`rest_on: login`) and a **local scheduler** (`rest_local.py`) takes SLURM's place: jobs wait first in, first out
within a CPU and memory budget, are held to their time limit, and keep running if the server restarts. CPU and
memory limits are enforced through cgroups v2 where they are delegated to you; `"enforce_limits": "memory"`
accepts machines (such as RHEL 9) that delegate memory but not CPU.

Rootless podman can't keep its storage on NFS, so where your home directory is on a network file system, jobs
use `/var/tmp/USER/hpclib-podman` (the sandbox's `"storage"` setting). The machine needs subordinate user ids for
you in `/etc/subuid` and `/etc/subgid`, and lingering (`loginctl enable-linger`) so jobs outlive your logins.

## Configuration

`~/.local/tunnels/rest/config.json` on the cluster. `setup_agents` writes it; every section is optional.

```json
{
  "limits": {"partitions": ["short", "gpu"], "accounts": null, "qos": null, "max_time": "04:00:00",
             "max_mem": "64G", "max_cpus": 16, "max_nodes": 1, "max_gpus": 1,
             "max_concurrent_jobs": 4, "max_array_tasks": 1000},
  "cluster_notes": "Prefer the short partition; ORCA is ORCA/5.0.4 after GCC and OpenMPI.",
  "sandbox": {"method": "auto", "binds": ["/sw"], "writable": [], "flags": []},
  "environments": {"uv": "auto", "pixi": "auto", "modules": ["WebProxy"], "timeout": 1800, "max_running": 2},
  "environment": {"all": {"LM_LICENSE_FILE": "27000@licenses"}, "jobs": {}, "syncs": {"HTTPS_PROXY": "http://proxy:3128"}},
  "scheduler": {"type": "local", "cpus": 32, "memory": "100G", "enforce_limits": "memory"},
  "audit_log": "~/.local/tunnels/rest/audit.log"
}
```

| Section | Meaning | Changeable while running |
| --- | --- | --- |
| `limits` | per-job caps and **`max_concurrent_jobs`**; `null` means no cap | yes |
| `cluster_notes` | free text the model reads in `cluster_info` | yes |
| `sandbox` | method, image, read-only `binds`, extra `writable` paths, runtime `flags` (e.g. `--nv`), `scratch`, `network`, `storage`, `allow_unsandboxed` | yes |
| `environments` | uv and pixi locations, modules to load before a sync, timeout, concurrent syncs | yes |
| `environment` | variables for all jobs and syncs, template jobs only, or syncs only; names that would change `PATH`, the sandbox, hpclib or SLURM are refused | yes |
| `poll_interval` | how often job states are refreshed | yes |
| `scheduler` | `{"type": "local"}` and its budget on machines without SLURM | restart |
| `module_command` | how `module` is run (default `["bash", "-lc", "module \"$@\" 2>&1", "hpclib-module"]`), if a login shell isn't right | restart |
| paths (`templates_dir`, `proposals_dir`, `jobs_db`, `audit_log`, `tokens_file`) | where things are kept | restart |

"Changeable while running" sections can be edited from the console's Settings page or with
`PUT /admin/config`; the server validates them, applies them at once and keeps the old file as
`config.json.replaced-TIME`. Others need the agent tunnel restarted.

## The REST API

All routes need `Authorization: Bearer TOKEN` and return JSON. While the tunnel's job is queued, the forwarded
port serves the waiting page instead, so clients should wait for `GET /health` to return JSON.

| Group | Routes |
| --- | --- |
| health | `GET /health` (includes `hpclib_version`) |
| cluster | `GET /cluster`, `GET /sandbox`, `GET /modules/avail`, `GET /modules/spider` |
| templates | `GET /templates`, `GET /templates/guide?name=`, `GET /templates/proposals`, `POST /templates/propose` |
| jobs | `POST /jobs`, `GET /jobs`, `GET /jobs/status?id=&tasks=1`, `GET /jobs/wait?id=&timeout=`, `POST /jobs/cancel?id=` |
| files | `GET /files?path=`, `GET`/`PUT /files/content?path=`, `GET /files/read`, `GET /files/tail`, `POST /files/mkdir`, `DELETE /files` |
| environments | `GET /envs?project=`, `POST /envs/sync`, `GET /envs/sync?id=&wait=` |
| raw SLURM (`slurm` scope) | `POST /slurm/{sbatch,squeue,sacct,scontrol,scancel}`, `GET /slurm/{squeue,sacct}` |
| owner only | `GET /admin/proposals`, `GET /admin/proposals/diff`, `POST /admin/proposals/approve`, `POST /admin/proposals/reject`, `GET /admin/audit`, `GET`/`PUT /admin/config`, `GET /admin/tokens`, `POST /admin/tokens/revoke` |

## Adding an MCP tool

Tools are defined in `build_server()` in `rest_mcp.py`, each a thin async wrapper over a `RESTClient` method:

1. Add the route to `rest_server.py` (with its scope in `ROUTE_SCOPES`) and the logic to the module it belongs in.
2. Add a `RESTClient` method in `rest_client.py`.
3. Add the tool in `build_server()`, with `Annotated` parameter descriptions and bounds, and
   `ToolAnnotations(readOnlyHint=True)` if it changes nothing.
4. Add tests: the MCP tools are tested in `tests/test_rest_jobs.py`, the routes in `tests/test_rest_server.py`.

Keep tools narrow: a tool that would let the model run arbitrary commands defeats the design above.
