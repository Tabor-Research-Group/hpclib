# Agents in the interface

The **Agents** app manages the clusters your LLM clients use. Its pages are Clusters, Proposals, Activity,
Files, SSH keys and Settings. Pages that read from a cluster (everything but Clusters) need that cluster's agent tunnel
to be **up**, since they talk to its REST server.

## Clusters

One row per cluster set up on this machine (one per agent profile). Each row shows:

| Column | What |
| --- | --- |
| Cluster | the login and the MCP server name (`hpclib-NAME`), which is what your LLM client calls it |
| Tunnel | the agent tunnel's state: **down**, **starting**, **queued** (with SLURM's reason), **up**, **error**; while up, the hpclib version the cluster's REST server runs |
| Login | the shared ssh login: **logged in**, **not logged in**, **login ended**, with **Log in** / **Log out** |
| Port | the tunnel's port on this machine |
| Token | the agent token's name |

The toolbar shows this machine's hpclib version, to compare with each cluster's.

and these actions:

| Action | What it does |
| --- | --- |
| **Log in** / **Log out** | open or close the shared ssh login (password, then a second-factor push) |
| **Start** / **Stop** | start or stop the agent tunnel (`agent_tunnel` / `agent_stop`) |
| **Log** | the tunnel's output, refreshed while open |
| **Update hpclib** | copy this machine's hpclib to the cluster (`install_hpclib`); a newer copy there is left alone |
| **Set up…** (**Setup…** once set up) | rerun `setup_agents` with its work directories, read-only directories and the Rebuild option |
| **Set up SSH** | initialize or reuse the shared-home compute key and its authorization; login required, REST tunnel not required |

When hpclib on the cluster is older than yours, **Update hpclib**, then **Stop** and **Start** the tunnel so the
REST server runs the new code.

**Add cluster** in the toolbar opens the form described in [Getting started](getting-started.md).

## Proposals

Agents can draft new job templates with the `propose_template` tool. This page lists the proposals waiting on
every live cluster. **Diff** shows the proposal's files next to the template it would replace, if any.
**Approve** installs it as a template; **Reject** asks for a reason and sets the proposal
aside on the cluster.

Whether proposals wait for you depends on the cluster's approval mode (Settings → Tunnel):

| Mode | Behaviour |
| --- | --- |
| Approve every valid proposal (default) | a proposal that passes validation becomes a template at once, even one that replaces an existing template |
| Approve new templates; review replacements | new names are approved at once; changes to existing templates wait here |
| Review every proposal | everything waits here |

Automatic approval only happens while jobs are sandboxed. On a cluster without a sandbox, every proposal waits
for you regardless of the mode.

## Activity

Choose a live cluster with an owner token. Activity has **Jobs** and **Requests** views, with an
agent-token filter shared between them. **Check now** refreshes the history; **Follow every 5 seconds**
updates jobs, an open job detail, and the request log. Changing clusters clears the previous details.

**Jobs** lists agent-API submissions newest first, including finished outcomes. Summary buttons count
waiting, running, completed, and failed/stopped jobs. Filter by state, submission time, or job ID,
template, and label. Load the newest 100, 250, 500, or 1000 jobs; filters apply to the loaded history,
so increase the history size when looking further back. An agent is identified by its token name;
agents sharing that token need distinct submission labels to tell their work apart.

**Details** shows parameters, resources, working directory, output path, and available scheduler
information such as elapsed time, exit code, node, and start/end time. Arrays also show task counts
and individual task states and outputs, with a task-state filter (up to 500 matching tasks displayed).
**Preview output** reads at most the last 200 lines / 64 KiB into a log panel using your log size
settings. **Open in Files** opens the full output. Output may not exist yet for a waiting job; array
output previews use the selected task's actual output filename.

If a refresh fails, an error identifies jobs or requests and marks retained history as possibly stale.
Job history and the audit log can fail independently. These controls are read-only; they do not submit,
cancel, restart, or resubmit jobs. Jobs managed only through `job-queue`, or submitted outside the
agent template API, do not appear in this registry.

**Requests** retains the audit view: request time, token, route, status, and job or file involved,
newest first. Filter by agent or **Errors only**. It loads the latest 500 matching requests and
keeps at most 2000 while following.

## Files

Browse the directories the agent token may use. Click a folder to open it; **View** shows a file in the page:

| Kind | How it is shown |
| --- | --- |
| text | in full up to 5 MB, otherwise the first 1 MB |
| images, SVG | inline, up to 25 MB |
| HTML | in a sandboxed frame with scripts off, unless you allow them, up to 25 MB |

**Download** saves a file; above 25 MB it asks first, since the browser holds the file in memory.

The address bar follows what you open, so you can bookmark or share a link to a folder or file:

```
http://127.0.0.1:27180/#/files?cluster=hpclib-grace&path=/scratch/user/me/llm/run/out.log
```

`cluster` may be the profile name, the login host or the MCP name.

## SSH keys

Choose a live owner cluster to list and create identity keys, copy their public keys, edit public
comments, manage individual authorized-key entries, and bind keys to SSH hosts such as GitHub.
Private keys remain on the cluster. See [Cluster SSH keys](../ssh-keys.md) for file behavior,
compute connection settings, and the REST API.

## Settings

Each cluster has two cards.

### Tunnel (kept on this machine)

| Setting | Effect |
| --- | --- |
| Template proposals | the approval mode (see Proposals above) |
| Keep the ssh login for (hours) | how long the shared login stays open after its last use; 1 to 168, default 12 |
| Run the REST server | **in a SLURM job** (default) or **on the login node**, for sites that ask for that and for machines without SLURM |
| Tunnel job: time, memory, partition | the agent tunnel job's own sbatch options |

These apply the next time the tunnel starts.

### Server (`config.json` on the cluster)

Shown while the tunnel is up. Changes are checked by the server and take effect immediately, without a restart;
the previous file is kept on the cluster as `config.json.replaced-TIME`.

| Setting | Effect |
| --- | --- |
| Job limits | longest job, memory, CPUs, nodes and GPUs per job, **jobs at once**, tasks per array, allowed partitions; blank means no cap |
| Environment syncs | modules to load before `uv sync`/`pixi install` (e.g. a web proxy), where uv and pixi are, the sync timeout |
| Environment variables | `NAME=value` lines for all jobs and syncs, template jobs only, or syncs only (e.g. a license server, `HTTPS_PROXY`) |
| Sandbox directories | extra read-only directories jobs can see, and extra writable ones |
| Notes for agents | free text the agent reads in `cluster_info`, e.g. which modules or partitions to prefer |

**Jobs at once** is the cap on concurrent agent jobs (default 4). An array job counts as many slots as its
throttle. Jobs beyond your account's SLURM limits still wait in SLURM's queue, and on a machine without SLURM they
also wait for the local scheduler's CPU and memory budget.

Settings the form doesn't cover (for example the sandbox method or the local scheduler) are edited in
`~/.local/tunnels/rest/config.json` on the cluster; see [the MCP server and REST API](../mcp-server.md#configuration).
