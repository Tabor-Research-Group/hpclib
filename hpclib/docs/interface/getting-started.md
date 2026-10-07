# Getting started with the interface

This page takes you from nothing to an LLM client running a test job on a cluster, entirely from the browser.

## 1. Start the console

```bash
launch-tunnel-manager
```

It starts the console on `http://127.0.0.1:27180` and opens the page with a fresh session key in the address.
The page keeps the key in the browser's storage for the console's address (so links opened in new tabs work too)
and removes it from the address bar. If you open the address yourself, the
page asks for the key; it is printed in the terminal and saved in `~/.config/hpclib/console/session`.

Useful options:

| Option | Effect |
| --- | --- |
| `--no-open` | don't open a browser |
| `--port N` | listen on another port |
| `--where` | print which hpclib and web page it would use, and exit |

Leave the terminal open: closing it (or Ctrl-C) stops the console. Tunnels it started run in sessions of their
own and keep running; when you start the console again, it finds them on their ports, and **Stop** works as
before.

## 2. Add a cluster

On **Agents → Clusters**, click **Add cluster** and fill in the form:

| Field | What to enter |
| --- | --- |
| Login | `user@login.cluster.edu`, as you would type it for `ssh` |
| Port, Jump host | only if you need them to reach the login node |
| Work directory for agents | an absolute path on the cluster where agents may write, e.g. `/scratch/user/me/llm` |
| Extra read-only directories | software trees jobs need to read, e.g. `/software` (optional) |

Click **Add, then log in and set up**.

## 3. Log in

A dialog asks for your cluster password. The console passes it to ssh once and forgets it; it is never saved.
If the cluster uses Duo or another second factor, approve the push on your phone. The row then shows
**logged in**.

The login is an ordinary ssh connection that hpclib shares between all its commands. It stays open for 12 hours
after it was last used (change this per cluster on the Settings page). With ssh keys and no second factor,
**Log in** doesn't ask for anything.

## 4. Set up

As soon as you are logged in, the console runs `setup_agents` on the cluster. Its output appears under the
cluster's row. It:

1. installs (or updates) hpclib on the cluster;
2. finds a Python 3.9+ there, loading a module if it has to;
3. copies the starter job templates (`hello`, `orca`, `python_project`) and the `writing_templates` guide;
4. writes the REST server's configuration, with a sandbox so agent jobs can write only to the work directory;
5. runs a test container to check the sandbox works;
6. creates the owner token and an agent token, giving the cluster only their hashes;
7. prints the MCP client entry for this cluster.

Setup takes a minute or two. You can rerun it later with **Set up…** on the row (for example after adding a
work directory). Reruns keep your templates, configuration and tokens and only add what is new. The
**Rebuild** checkbox regenerates them instead, keeping the old copies on the cluster.

## 5. Connect your LLM client

Copy the MCP entry from the end of the setup output into your client:

- **Claude Desktop**: Settings → Developer → Edit Config, and add the entry under `"mcpServers"`.
- **Claude Code**: run the `claude mcp add-json ...` line the output shows.

Then **quit the client completely and reopen it**. Until you do, it has no tools for the new cluster. The entry
is named after the cluster (for example `hpclib-grace`), so several clusters can be configured side by side.

## 6. Start the agent tunnel

Click **Start** on the cluster's row. The console runs the REST server tunnel: by default as a SLURM job, so the
row shows **starting** and then **queued** while the job waits for a node. When it shows **up**, the agent can
use the cluster.

On some clusters the login node asks for your password again to reach the compute node. A password dialog then
opens on whatever page you are on. The first time a login node reaches a new compute node, a dialog shows that
node's host-key fingerprint and asks whether to trust it.

**Log** on the row shows the tunnel's output. **Stop** ends the tunnel and cancels its job.

## 7. Try it

Ask your LLM client something like:

> Use hpclib-grace to run the hello template with the message "hi from the agent", and show me the output.

It will call `cluster_info`, submit the `hello` template (first as a dry run), wait for the job and read its
output. You can watch each request on **Agents → Activity**.

## Next

- [Agents](agents.md): reviewing templates, browsing files, changing limits.
- [Apps](apps.md): JupyterLab, VS Code and the other tunnel apps.
- [Troubleshooting](../troubleshooting.md), if a step above failed.
