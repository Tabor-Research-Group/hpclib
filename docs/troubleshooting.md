# Troubleshooting

Each entry gives the symptom, why it happens, and what to do, from the interface and from a terminal.

## Logging in and connections

**The login dialog says the login failed, or nothing happens after the password.**
The password went to ssh, which then waits for your second factor. Approve the push on your phone. If your
cluster offers a passcode menu instead of a push, log in once in a terminal (`pssh user@login.example true`),
which leaves the shared connection open for the console to reuse.

**A command hangs after your laptop slept or the VPN changed.**
The shared connection died. With keepalives (the default) it errors out within about two minutes. To drop it at
once: **Log out** then **Log in** in the console, or
`ssh -O exit -S ~/.ssh/connections/USER@HOST:22 HOST`.

**"Host key verification failed" for the login node.**
ssh doesn't know the cluster's key yet. Connect once in a terminal and accept it.

**A dialog asks about a new compute node's key.**
The login node is reaching a compute node for the first time. Check the fingerprint if your site publishes them,
then trust it. To stop being asked for new nodes, put `HPCLIB_COMPUTE_HOST_KEYS=accept-new` in
`~/.local/tunnels/config.sh` on the cluster.

**A tunnel asks for your password again after it starts.**
Your cluster's login node needs a password to ssh to compute nodes. The console asks for it in a dialog. To
avoid it, give the cluster an ssh key for itself: on the cluster, `ssh-keygen -t ed25519`, then add
`~/.ssh/id_ed25519.pub` to `~/.ssh/authorized_keys`.

## Tunnels

**"port N is in use by something that isn't one of your tunnels".**
Something hpclib didn't start, or another user's program, listens on that port on the login node. hpclib lists
what holds it and leaves it alone. Pick another port with `-P`; if the listed process is yours and you are done
with it, stop it yourself (`kill PID` on that login node). Leftovers hpclib started (a forward, a waiting page,
rclone's interface, a podman forwarder) are stopped automatically.

**The browser shows the waiting page for a long time.**
The job is queued. The page shows SLURM's reason (`Priority`, `Resources`, ...). Ask for fewer resources or a
different partition (`--time`, `--mem`, `--partition`, or the app's Settings).

**"Job N ended before the tunnel connected".**
The job failed during start-up. The message includes the end of its log; the full log is
`~/.local/tunnels/sessions/TUNNEL/session-N.log` on the cluster, or **Log** in the console. Common causes: a
missing conda environment or module, an image that isn't installed (run Install or `tunnel_setup ... --install`).

**The tunnel closed but the job keeps running**, or **a new tunnel on the same port fails**.
Clear it: `stop_tunnel -P PORT user@login.example`, or simply start again on the same port, which clears it first.
Shared-instance jobs (PAI) are kept on purpose; end them with **End database job** or
`tunnel_setup HOST pai --stop-instance JOB`.

**"Tunnel 'X' not found in HPCLIB_TUNNEL_PATH".**
The tunnel isn't on the cluster. Update hpclib there (`install_hpclib`, or **Update hpclib**), or install the
tunnel (`install_tunnel`, or a package's Check/Install).

**An app says "install status unknown".**
Its `install.sh` doesn't declare `--check`, so hpclib can't ask. Start it; if it fails, run Install.

## Agents

**The LLM client has no tools for the cluster.**
The MCP entry isn't in the client's configuration, or the client hasn't been restarted. Add the entry from
`~/.config/hpclib/agents/USER@HOST/mcp.json`, then quit the client completely (not just close the window) and
reopen it.

**Tools fail with "REST server not reachable at http://127.0.0.1:PORT".**
The agent tunnel isn't running. **Start** it in the console, or `agent_tunnel user@login.example`.

**Tools fail with a 401.**
The token in the profile doesn't match the cluster, for example after `setup_agents --rebuild` from another
machine. Rerun `setup_agents user@login.example` on this machine.

**A feature is missing, or the Clusters row shows an older hpclib on the cluster than on your machine.**
**Update hpclib**, then **Stop** and **Start** the agent tunnel, so the REST server runs the new code.

**Submissions fail with 429, "concurrent slots".**
The agent has as many jobs running as `max_concurrent_jobs` allows (array jobs count their throttle). Raise
**Jobs at once** on the Settings page, or wait.

**Submissions fail with 422 and a list of violations.**
The requested resources exceed the server's limits (`max_time`, `max_mem`, ...). Ask for less, or raise the
limits on the Settings page.

**A proposal didn't become a template.**
Proposals are approved automatically only while jobs are sandboxed and the policy allows it. Look at
**Proposals** in the console; `cluster_info` tells the agent which policy applies.

**The sandbox self-test fails (`sandbox_info`).**

| Message | Cause | Fix |
| --- | --- | --- |
| apptainer or singularity, or podman was not found on the server's PATH | no container runtime where the REST server runs | load its module in `~/.bashrc`, or set the sandbox's `runtime` to its full path |
| `chown ...: operation not permitted` (podman) | podman's storage is on NFS | update hpclib (newer versions move it to `/var/tmp` automatically), or set `"sandbox": {"storage": "/var/tmp/USER/hpclib-podman"}` |
| `no subordinate ids` (podman) | your account has no range in `/etc/subuid` | the admin adds one: `echo USER:START:65536 \| sudo tee -a /etc/subuid /etc/subgid`, then `podman system migrate` |
| CPU and memory limits can't be enforced | cgroups v2 doesn't delegate the controllers to you | set the scheduler's `"enforce_limits"` to `"memory"` or `false` in `config.json` |

## The console

**The page asks for a key.**
The console was restarted, so the key changed. `launch-tunnel-manager` opens the page with the new key; or copy
it from the terminal or `~/.config/hpclib/console/session`.

**"Address already in use" when starting the console.**
Another console is running. Use it, or stop it, or start this one with `--port`.

**Rows say "error" right after the console restarts.**
Tunnels outlive the console and are found again by their ports; give the rows a few seconds to refresh. If one
stays in error, **Stop** and **Start** it.
