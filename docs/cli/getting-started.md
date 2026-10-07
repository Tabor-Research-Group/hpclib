# Getting started on the command line

This page goes from a fresh terminal to JupyterLab on a compute node in your browser.

## 1. Load hpclib

```bash
source ~/hpclib/hpclib/hpclib.sh        # or put this line in ~/.bashrc
```

## 2. The shared ssh connection

hpclib never opens a new ssh login when it can reuse one. `pssh` is `ssh` with connection sharing turned on:
the first command logs in (password, second factor), and every later hpclib command to the same host rides on
that connection without asking again.

```bash
pssh user@login.example hostname        # logs in once
pssh user@login.example squeue --me     # reuses the login
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `HPCLIB_SSH_PERSIST` | `12h` | how long the shared connection stays open after its last use |
| `HPCLIB_SSH_KEEPALIVE` | on | keepalives, so a dead connection errors out in about two minutes; set it empty to turn them off |

The connection's socket lives in `~/.ssh/connections/`. Any `ssh` options go before the address, exactly as with
`ssh` (`pssh -J jump.example -p 2222 user@login.example`); every hpclib command that takes an address accepts
them the same way. `ssh -O exit -S ~/.ssh/connections/USER@HOST:22 HOST` closes a connection early.

## 3. Install hpclib on the cluster

```bash
install_hpclib user@login.example
```

This copies your hpclib to `~/hpclib` on the cluster if the copy there is missing or older. Run it again after
each `git pull`. See [Installation](../installation.md#2-install-hpclib-on-a-cluster) for the options.

## 4. Start a tunnel

```bash
launch_tunnel -P 8950 user@login.example jupyter
```

What happens (the full story is in [Tunnel architecture](../architecture.md)):

1. Your machine opens `pssh -t -L 127.0.0.1:8950:127.0.0.1:8950` to the login node and runs
   `~/hpclib/tunnels/start_tunnel.sh jupyter -P 8950` there.
2. The login node immediately serves a small "please wait" page on port 8950, so your browser has something to
   show, and submits the tunnel's SLURM job.
3. A browser tab opens on `http://localhost:8950`; it shows the job's queue state.
4. When the job runs, the login node replaces the waiting page with an ssh forward to the compute node, and the
   page reloads into JupyterLab.

The terminal shows the job's output. Leave it open: the tunnel lives as long as this command.

### launch_tunnel options

```text
launch_tunnel [-P PORT] [-A APP] [-b MODE] [--browser-arg=ARG] [ssh options] [user@]host TUNNEL
              [start_tunnel options and sbatch options...] [-- arguments for the tunnel's job]
```

| Option | Meaning |
| --- | --- |
| `-P PORT` | the port on your machine and the login node (random if not given) |
| `-A APP` | the browser to open: `Safari` (the default), `Chrome`, `Firefox`, ...; `-A none` opens nothing |
| `--process-port N` | the port the service uses on the compute node, if not the tunnel's default |
| `--login-node` | run the service on the login node instead of in a job (only for tunnels that allow it) |
| `--env=NAME=value,...` | environment variables for the job |
| anything else before `--` | passed to `sbatch`, e.g. `--time=12:00:00 --mem=30gb --partition=gpu` |
| after `--` | arguments for the tunnel's own job script |

sbatch options you give are added after the tunnel's defaults, so they win where they overlap and the rest of
the defaults stay.

## 5. Stop it

Press **Ctrl-C** in the tunnel's terminal: the forward closes and the job is cancelled.

If the terminal is gone (laptop asleep, VPN dropped), clear the port from your machine:

```bash
stop_tunnel -P 8950 user@login.example
```

It stops the tunnel's pieces on the login node, cancels its job, and drops your local forward. Starting a new
tunnel on the same port does the same cleanup first, so you can also just start again.

## 6. Where things are kept on the cluster

| Path | What |
| --- | --- |
| `~/.local/tunnels/config.sh` | your defaults for every tunnel (sbatch defaults, conda module, ...) |
| `~/.local/tunnels/settings/TUNNEL.sh` | one tunnel's settings, from `tunnel_setup --set` or the console |
| `~/.local/tunnels/sessions/TUNNEL/session-JOB.log` | each tunnel job's output |
| `~/.local/tunnels/sessions/ports/` | which tunnel holds which port on which login node |

## Next

- [Tunnels](tunnels.md): JupyterLab, VS Code, Flask, PAI, NGL and file transfers.
- [Agents](agents.md): letting an LLM client run jobs.
