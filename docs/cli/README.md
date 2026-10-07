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
| `tunnel_setup HOST TUNNEL ...` | save a tunnel's settings on the cluster, install what it needs, check it |
| `smbshell --on HOST ...` | SMB file transfers, run on the cluster |
| `setup_agents HOST` | set a cluster up for an LLM client (REST server, templates, sandbox, tokens) |
| `agent_tunnel HOST`, `agent_stop HOST` | start or stop the agents' REST tunnel |
| `agent_list`, `agent_env HOST` | the clusters set up on this machine; environment variables for scripts |
| `agent_console`, `launch-tunnel-manager` | the web interface's backend (see [Using the interface](../interface/README.md)) |

Run on **a cluster**:

| Command | What it does |
| --- | --- |
| `hpclib/tunnels/start_tunnel.sh TUNNEL -P PORT` | what `launch_tunnel` runs on the login node |
| `install_tunnel DIR` | install a tunnel folder into your tunnel path |
| `smbshell ...` | SMB file transfers |
| `python3 hpclib/servers/rest_server.py --list-tokens` etc. | manage the REST server's tokens and proposals |

Most functions print their usage with `-h` or when given no arguments.
