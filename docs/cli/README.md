# Using the command line

Everything hpclib does is a bash function you load with `source hpclib/hpclib.sh`, or a script you can run
directly. This section goes through using them from a terminal, without the web interface.

It assumes hpclib is cloned on your machine and loaded in your shell ([Installation](../installation.md),
step 1).

1. [Getting started](getting-started.md): the shared ssh connection, installing hpclib on a cluster, your first
   tunnel, stopping it.
2. [Tunnels](tunnels.md): each bundled tunnel, tunnel settings and installers, data transfer with `smbshell`.
3. [Agents](agents.md): setting a cluster up for an LLM client, running the agent tunnel, tokens, templates
   and the REST API.

## The commands at a glance

Run on **your machine**:

| Command | What it does |
| --- | --- |
| `pssh`, `psftp`, `psync`, `pscp` | `ssh`, `sftp`, `rsync`, `scp` over one shared, persistent connection per host |
| `install_hpclib HOST` | copy this hpclib to a cluster, if the cluster's is older |
| `launch_tunnel -P PORT HOST TUNNEL` | start a tunnel on the cluster and forward its port here |
| `stop_tunnel -P PORT HOST` | stop a tunnel and anything it left on the login node |
| `clear_tunnel_ports -P PORT [-P PORT]... HOST` | clear what earlier tunnels left on those ports on every login node it can reach (`--no-hop`: only the one it lands on) |
| `tunnel_setup HOST TUNNEL ...` | save a tunnel's settings on the cluster, install what it needs, check it |
| `smbshell --on HOST ...` | SMB file transfers, run on the cluster |
| `cluster_quota [storage\|project] [--json] HOST` | your storage quota and project balance on a cluster ([Quotas](#quotas)) |
| `setup_agents HOST` | set a cluster up for an LLM client (REST server, templates, sandbox, tokens) |
| `setup_cluster_ssh HOST` | initialize shared-home login-to-compute key authentication; [SSH key guide](../ssh-keys.md) |
| `agent_tunnel HOST`, `agent_stop HOST` | start or stop the agents' REST tunnel |
| `agent_list`, `agent_env HOST` | the clusters set up on this machine; environment variables for scripts |
| `agent_console`, `launch-tunnel-manager` | the web interface's backend (see [Using the interface](../interface/README.md)) |

Run on **a cluster**:

| Command | What it does |
| --- | --- |
| `hpclib/tunnels/start_tunnel.sh TUNNEL -P PORT` | what `launch_tunnel` runs on the login node |
| `install_tunnel DIR` | install a tunnel folder into your tunnel path |
| `smbshell ...` | SMB file transfers |
| `hpclib_quota [storage\|project] [--json]`, `hpclib_quota set storage\|project COMMAND` | the quota checks, and which commands they run here |
| `python3 hpclib/servers/rest_server.py --list-tokens` etc. | manage the REST server's tokens and proposals |

Most functions print their usage with `-h` or when given no arguments.

## Quotas

`hpclib_quota` (on a cluster) and `cluster_quota HOST` (from your machine) run two checks with the cluster's
own commands and print their output as the site writes it:

- **storage**: the first of `showquota`, `myquota` or `quota -s` the cluster has;
- **project** (your allocation's balance): `myproject`, as on TAMU HPRC clusters, when the cluster has it.

Where a cluster has other commands, change them in one of three ways (the first that is set wins):

```bash
export HPCLIB_PROJECT_QUOTA_CHECK='sshare -U -l'       # 1. this shell; cluster_quota passes yours on
hpclib_quota set storage 'lfs quota -h -u $USER /scratch' # 2. saved for the cluster ($HPCTUNNELS_DATA_DIR/settings/quota.sh)
hpclib_quota set project none                         #    none turns a check off; default forgets a setting
```

The console's cluster **Settings → Quota checks** writes the same file, so the console's Storage tab, the REST
server's `GET /quota` and agents' `check_quota` tool all use what you set. `--json` prints one
`HPCLIB_QUOTA {...}` line with each check's command, where it came from (`env`, `settings`, `default`, `none`),
its exit code and output.
