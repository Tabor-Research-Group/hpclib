# Tunnel architecture

A tunnel connects a port on your machine to a service running on a cluster, usually on a compute node that you
can't reach directly. hpclib does this with two ssh port forwards chained through the login node, plus a few
helpers that keep the connection usable while the job waits in the queue and clean up after it.

## The pieces

```text
 your machine                     login node                              compute node
 ─────────────                    ──────────                              ────────────
 browser ──► 127.0.0.1:P ══ssh -L══► 127.0.0.1:P ══ssh -L (to the job)══► 127.0.0.1:Q  service
             (launch_tunnel)        start_tunnel.sh                         sbatch_script.sh
                                    ├─ waiting page on P (until the job runs)
                                    ├─ sbatch → job
                                    ├─ git helper server (optional)
                                    └─ port record ~/.local/tunnels/sessions/ports/HOST-P
```

| Piece | Where | What it does |
| --- | --- | --- |
| `launch_tunnel` | your machine | opens `pssh -t -L 127.0.0.1:P:127.0.0.1:P` to the login node and runs `start_tunnel.sh` there; opens a browser once the port answers |
| `pssh` | your machine | `ssh` with connection sharing (`ControlMaster`), so one login (password, second factor) serves every command for 12 hours |
| `start_tunnel.sh` | login node | reads the tunnel's configuration, submits its job, waits for a node, and forwards port P to the job's port Q |
| `waiting_shim.py` | login node | a "please wait" page on port P that shows the job's queue state and refreshes every 3 seconds |
| `sbatch_script.sh` | compute node | the tunnel's own script: sets up the environment (`configure_job.sh`) and starts the service on port Q |
| `connect_to_job` | login node | `ssh -L P:127.0.0.1:Q` to the job's node, with keepalives and `ExitOnForwardFailure` |
| `postconnect.sh` | compute node | runs in that ssh session: streams the job's log while the job is in the queue, returns when it ends, and cancels the job if the session is hung up first |

P is the port you choose with `-P` (the same number on your machine and the login node). Q is the service's port
on the compute node (`PROCESS_PORT`), usually fixed by the tunnel.

## The life of a tunnel

1. **Start.** `launch_tunnel -P 8950 user@login jupyter` runs, over the shared ssh connection with a forward of
   port 8950, `bash ~/hpclib/tunnels/start_tunnel.sh jupyter -P 8950`.
2. **Configure.** `start_tunnel.sh` loads, in this order, each one overriding the last:
   1. hpclib's built-in defaults;
   2. your defaults on the cluster, `~/.local/tunnels/config.sh` (`DEFAULT_PORT`, `DEFAULT_SBATCH_ARGS`,
      `CONDA_ENVIRONMENT`, `CONDA_MODULE` and the like; the per-tunnel switches below it can't be set here);
   3. the tunnel's `tunnel_config.sh`;
   4. the tunnel's saved settings, `~/.local/tunnels/settings/TUNNEL.sh` (from `tunnel_setup` or the console);
   5. the command line, which always wins.
3. **Clear the port.** Anything an earlier session left on port P is stopped (see
   [Ending tunnels](#ending-tunnels-and-stale-ports)). If something else holds it, the tunnel stops and says
   what.
4. **Cover the port.** The waiting page starts on P, so your browser gets an answer at once instead of
   "connection refused" while SLURM queues the job.
5. **Submit.** `sbatch --parsable` with the tunnel's `DEFAULT_SBATCH_ARGS` followed by yours, exporting the
   tunnel's environment to the job. The job's output goes to `~/.local/tunnels/sessions/TUNNEL/session-JOB.log`.
6. **Wait.** It polls `squeue` until the job has a node, writing the queue state for the waiting page. A job
   that leaves the queue before it starts ends the tunnel with its final state and the end of its log.
7. **Connect.** It stops the waiting page and runs `connect_to_job`: an ssh forward from the login node's port
   P to port Q on the job's node. The browser's next refresh reaches the service.
8. **Follow.** In that ssh session, `postconnect.sh` streams the job's log to your terminal (and to the console,
   which reads tokens and passwords from it).
9. **End.** Ctrl-C, a closed terminal, `stop_tunnel`, or the job ending all tear down the chain. When the
   tunnel ends first, it cancels the job; when the job ends first, the ssh session returns and the tunnel exits.

## Variations

### Services on the login node

With `--login-node`, or `RUN_ON_LOGIN_NODE=true` in `tunnel_config.sh`, steps 4 to 8 are replaced: the tunnel's
`sbatch_script.sh` runs directly on the login node as a child of `start_tunnel.sh`, listening on port P itself.
It ends when the tunnel ends, the way a job would be cancelled. This is for sites that prefer a small service on
the login node to a job holding part of a compute node, and for development servers without SLURM.

### Shared instances

A tunnel with `SHARED_INSTANCE=true` serves something several sessions should share, such as a database. Before
submitting, it looks for a running job of the same tunnel and, if there is one, connects to that job instead. The
job registers itself on start with `tunnel_register_instance TUNNEL PORT` (from `tunnels/instances.sh`) under
`~/.local/tunnels/instances/TUNNEL/`. Two more switches go with it:

| Setting | Effect |
| --- | --- |
| `PROCESS_PORT_FROM_JOB=true` | the job picks its port (`tunnel_pick_port PREFERRED`: that one if free on its node, else another) and the tunnel waits for the port it registers |
| `KEEP_INSTANCE=true` | closing the tunnel leaves the job running for the next session |

A tunnel never cancels a job it only attached to.

### Helper servers

Tunnels can start two small socket servers for programs in the job to call: a `git` server on the login node
(`START_GIT_SERVER`), for git operations that need the login node's network or credentials, and a SLURM server
inside the job on the compute node (`START_SLURM_SERVER`), so programs in the job can submit and query jobs
through it. Their ports reach the job as `GIT_SOCKET_PORT` and `SLURM_SOCKET_PORT`. Tunnels that don't need them
(REST, Flask) turn them off.

## Ending tunnels and stale ports

A tunnel can lose its terminal without ending cleanly: your laptop sleeps, the VPN drops, a window is closed.
Its pieces on the login node may then keep the port. hpclib finds and stops them in two ways.

**Port records.** Each tunnel records itself in `~/.local/tunnels/sessions/ports/LOGINNODE-PORT` as its process id
and job id. A new tunnel on the same port, or `stop_tunnel -P PORT host` from your machine, stops the recorded
tunnel script, cancels its job, and stops the leftover forward and waiting page for that port.

**What is listening.** Everything hpclib starts for a port carries `HPCLIB_TUNNEL_PORT=PORT` in its environment,
and so does everything those processes start: podman's port forwarder, rclone, the ssh forward. hpclib reads the
kernel's socket tables to find the processes listening on the port, and stops yours that carry the mark: TERM,
then KILL after 5 seconds. This catches leftovers that a record doesn't describe, such as rclone's web interface
or a container's forwarder. A process that isn't marked (another user's program, or one of yours hpclib didn't
start) is listed and left alone, and the tunnel asks you to pick another port.

Login nodes behind one host name may differ between connections. If the stale tunnel was started on another
login node, `stop_tunnel` says which, so you can reach that node directly.

## Where a tunnel is found

A tunnel name is looked up in the directories on `HPCLIB_TUNNEL_PATH`, first match wins. By default that is:

1. `~/.local/share/hpclib/tunnels` (`HPCLIB_TUNNEL_INSTALL_LOCATION`): tunnels you installed with
   `install_tunnel` or that came from console packages;
2. `~/hpclib/tunnels`: hpclib's own.

So an installed tunnel can replace a bundled one of the same name. Shared files (`configure_job.sh`,
`postconnect.sh`) are taken from the tunnel's folder if it has its own, otherwise from hpclib's.

## How the console drives tunnels

The console (`hpclib/servers/agent_console.py`) doesn't reimplement any of this. It runs the same shell
functions (`launch_tunnel`, `agent_tunnel`, `stop_tunnel`, `tunnel_setup`, `setup_agents`) as child processes,
tunnels in a pseudo-terminal as a terminal would, and reads their output:

| Concern | How it is handled |
| --- | --- |
| logging in | it starts the shared ssh connection itself; ssh asks for the password through `SSH_ASKPASS`, which is `console_askpass.py`, which asks the console over a private socket; the console asks the page |
| a password asked later (the login node's ssh to the compute node) | it watches the tunnel's terminal for the prompt, shows a dialog, and writes the answer straight to that ssh |
| a new compute node's host key | the same, showing the key's fingerprint |
| app URLs and passwords | read from the job's log as `postconnect.sh` streams it (Jupyter's token, code-server's password) |
| state | from the port answering, the process, and the log, polled by the page |

Passwords pass through memory once and are never written to disk or logs. Tunnels run in sessions of their own,
so they outlive a console restart, and the console finds them again by their ports.

## Agent tunnels

The agents' tunnel is an ordinary `rest` tunnel whose service is hpclib's REST server. What makes it safe to hand
to an LLM (tokens, scopes, templates, the sandbox) is described in
[The MCP server and REST API](mcp-server.md).
