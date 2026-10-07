# Installation

hpclib is installed in two places: on **your machine**, where you start tunnels and run the console, and on
each **cluster**, where tunnels and the REST server run. The copy on your machine installs the cluster's copy
for you, so in practice you install once locally and then point hpclib at each cluster.

## Requirements

| Where | Needs |
| --- | --- |
| your machine | macOS or Linux, `bash` 3.2 or newer, OpenSSH, Python 3.7 or newer (for the console), `git` |
| your machine, for agents | a Python with the MCP SDK (`pip install mcp`) for the MCP server |
| cluster login node | `bash`, Python 3.9 or newer (found or loaded from a module by `setup_agents`), SLURM (`sbatch`, `squeue`) or podman |
| cluster, for sandboxed agent jobs | Singularity or Apptainer; or rootless podman on a server without SLURM |
| cluster, for some apps | conda or uv/pixi (JupyterLab), Singularity (VS Code, PAI, Data transfer) |


## 1. Install hpclib on your machine

Clone the repository somewhere permanent; updating is then `git pull`.

```bash
git clone https://github.com/Tabor-Research-Group/hpclib.git ~/hpclib
```

Load the shell functions in every new terminal by adding this line to `~/.bashrc` (or `~/.zshrc` through
`bash`, since hpclib is bash):

```bash
source ~/hpclib/hpclib/hpclib.sh
```

Open a new terminal and check:

```bash
type launch_tunnel       # "launch_tunnel is a function"
```

To use the tunnel manager from anywhere, link its launcher onto your `PATH` once:

```bash
~/hpclib/hpclib/launch-tunnel-manager --install-link     # a link in ~/.local/bin; or --install-link DIR
launch-tunnel-manager                                    # starts the console and opens it in your browser
```

The link follows itself back to the clone, so `git pull` there updates what it runs.
`launch-tunnel-manager --where` prints which hpclib and which web page it uses. `HPCLIB_PYTHON` chooses the
Python it runs with.

### The MCP dependency

Only the MCP server (`hpclib/servers/rest_mcp.py`, which runs on your machine) needs a third-party package.
Install it into the Python your LLM client will launch:

```bash
python3 -m pip install mcp            # or: python3 -m pip install "$HOME/hpclib[mcp]"
```

Nothing on the cluster depends on it.

## 2. Install hpclib on a cluster

From your machine, over the same ssh connection every hpclib command reuses:

```bash
install_hpclib user@login.example                       # to ~/hpclib on the cluster
install_hpclib -J jump.example user@login.example       # any ssh options go before the address
install_hpclib --target /scratch/user/me/hpclib user@login.example
install_hpclib --check user@login.example               # say what it would do
```

`install_hpclib` compares versions (`HPCLIB_VERSION` in `hpclib/hpclib.sh`) and only replaces an older copy.
The new copy is unpacked and checked beside the old one before it is swapped in, so a failed transfer leaves
the old copy working; the replaced copy is kept as `TARGET.previous`. `--force` installs even when the versions
match. If you install somewhere other than `~/hpclib`, set `HPCLIB_REMOTE_INSTALL_LOCATION` on your machine to
that path so `launch_tunnel` finds it.

Without a local copy you can also install directly on a login node with pip:

```bash
pip install --target=$SCRATCH --no-dependencies --upgrade --ignore-installed \
  git+https://github.com/Tabor-Research-Group/hpclib
```

In the console, **Update hpclib** on the Clusters page runs `install_hpclib` for you, and each cluster shows
the version it runs next to your machine's, so you can tell when it is behind.

## 3. Set a cluster up for agents (optional)

If you want an LLM client to run jobs on the cluster, run `setup_agents` once per cluster, from your machine:

```bash
setup_agents --work-dir /scratch/user/me/llm user@login.example
```

It installs hpclib on the cluster, finds a Python there, copies the starter job templates, writes the REST
server's configuration with a job sandbox, creates the tokens, and prints the entry to add to your MCP client.
The console's **Add cluster** button does the same thing. Both are described step by step in
[the interface guide](interface/getting-started.md) and [the command-line guide](cli/agents.md).

**setup_agents never edits your LLM client's configuration.** Add the entry it prints yourself, then quit the
client completely and reopen it.

## 4. Optional per-cluster preparation

| For | Do this on the cluster |
| --- | --- |
| tunnels that use conda (JupyterLab, NGL) | make conda available from `~/.bashrc`, or set `CONDA_MODULE` in `~/.local/tunnels/config.sh` |
| fewer password prompts between nodes | `ssh-keygen -t ed25519`, then add `~/.ssh/id_ed25519.pub` to `~/.ssh/authorized_keys` |
| new compute nodes' host keys accepted without asking | `HPCLIB_COMPUTE_HOST_KEYS=accept-new` in `~/.local/tunnels/config.sh` |
| podman jobs on a server without SLURM | ask the admin for subordinate ids (`/etc/subuid`, `/etc/subgid`) and `loginctl enable-linger` |

## Updating

```bash
cd ~/hpclib && git pull                 # your machine
install_hpclib user@login.example       # each cluster (or Update hpclib in the console)
```

Then restart what runs the old code: the console (`launch-tunnel-manager`), any running tunnels, and your LLM
client if hpclib's MCP server changed.

## Uninstalling

Remove the clone and the `source` line from `~/.bashrc` on your machine, and `~/hpclib` on each cluster.
hpclib's own state is in a few directories you can delete as well:

| Where | What |
| --- | --- |
| your machine: `~/.config/hpclib/` | agent profiles and tokens, console state and packages |
| your machine: `~/.ssh/connections/` | shared ssh connections (sockets only) |
| cluster: `~/.local/tunnels/` | tunnel sessions and settings, REST server config, templates, tokens (hashed) |
| cluster: `~/.local/share/hpclib/tunnels/` | tunnels installed with `install_tunnel` or from packages |
