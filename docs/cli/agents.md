# Agents on the command line

An **agent** is an LLM client on your machine (Claude Desktop, Claude Code, or any MCP client) that runs jobs on
a cluster through hpclib. The agent never gets a shell: it calls a fixed set of tools, which submit only the job
templates you allow, in the directories you allow. How that is enforced is in
[The MCP server and REST API](../mcp-server.md).

## 1. Set a cluster up: `setup_agents`

```bash
setup_agents --work-dir /scratch/user/me/llm user@login.example
```

Run once per cluster. It:

1. installs hpclib on the cluster (`install_hpclib`);
2. finds a Python 3.9+ there (`python3`, `python3.X`, or the newest Python or conda module) and records it as
   `~/.local/tunnels/rest/python`;
3. copies the `hello`, `orca` and `python_project` templates and the `writing_templates` guide;
4. writes `~/.local/tunnels/rest/config.json` with a sandbox, so template jobs can write only to the work
   directories, and runs a test container;
5. on a machine without `sbatch`, sets the REST server to run on the login node with the local scheduler;
6. creates the owner token and an agent token, giving the cluster only their hashes;
7. saves everything about the cluster in a profile on your machine, `~/.config/hpclib/agents/USER@HOST/`;
8. prints a getting-started summary with the MCP client entry.

Common options:

| Option | Meaning |
| --- | --- |
| `--work-dir DIR` | a directory agents may write to (repeatable); the first is where relative paths start |
| `--bind DIR` | a read-only directory jobs can see, e.g. a software tree (repeatable) |
| `--templates LIST` | which bundled templates to copy, or `all` |
| `--scopes LIST` | the agent token's scopes; default `read,submit,propose,files:write,envs` |
| `--local-root DIR` | a folder on your machine the agent may push from and pull into (repeatable); default `~/Documents/Claude` |
| `--no-local-root` | no local file access for the agent |
| `--python-module MOD` / `--remote-python PATH` | choose the cluster's Python explicitly |
| `--mcp-name NAME` | the MCP server's name (default `hpclib-` and the first part of the host name, e.g. `hpclib-grace`) |
| `--port N`, `--process-port N`, `--new-ports` | the tunnel's ports (picked at random, then kept) |
| `--no-sandbox` | leave jobs unsandboxed (not recommended) |
| `--rebuild` | regenerate templates, config, the sandbox image and the agent token; old copies are kept on the cluster |

Rerunning is safe: it keeps your templates, configuration and tokens and adds what a newer hpclib brings. A later
run needs only the address: `setup_agents user@login.example`.

## 2. Add the MCP entry to your client

The summary prints the entry. **setup_agents never edits your client's configuration**, so add it yourself:

- **Claude Code**: run the printed `claude mcp add-json ...` line.
- **Claude Desktop**: add the printed JSON under `"mcpServers"` in its configuration file.

It looks like this:

```json
{"mcpServers": {"hpclib-grace": {
  "command": "/path/to/python-with-mcp",
  "args": ["/path/to/hpclib/hpclib/servers/rest_mcp.py",
           "--url", "http://127.0.0.1:24117",
           "--token-file", "/Users/me/.config/hpclib/agents/me@grace.example/agent_token",
           "--local-root", "/Users/me/Documents/Claude"]}}}
```

Quit the client completely and reopen it. To print the entry again later:

```bash
cat ~/.config/hpclib/agents/USER@HOST/mcp.json
```

## 3. Run the agent tunnel

```bash
agent_tunnel user@login.example        # or the profile name; leave it running
agent_stop user@login.example          # from another terminal, or Ctrl-C
agent_list                             # every cluster set up on this machine
```

`agent_tunnel` starts the `rest` tunnel with the profile's ports and directories, without a browser. Options:

| Option | Meaning |
| --- | --- |
| `--review-templates` | hold every template proposal for your review |
| `--auto-approve-templates=new` | approve new templates automatically, hold replacements |
| anything else | passed to `launch_tunnel`, e.g. `--time=24:00:00` for the tunnel job |

By default valid proposals are approved at once, but only while jobs are sandboxed.

## 4. Templates

Templates live on the cluster in `~/.local/tunnels/rest/templates/NAME/`. Each is a `template.json` and a
`script.sh`; they are re-read on every request, so editing one takes effect immediately.

```json
{
  "description": "Print a message from a compute node.",
  "parameters": {"message": {"type": "string", "max_length": 200, "default": "hello"}},
  "resources": {"time": "00:15:00", "mem": "1G", "cpus_per_task": "1"},
  "overridable": ["time"]
}
```

```bash
#!/bin/bash
echo "job $SLURM_JOB_ID on $(hostname): $HPC_PARAM_MESSAGE"
```

See [Extending hpclib](../extending.md#writing-job-templates) for the full format.

Agents can propose templates of their own. To review proposals from a terminal on the cluster:

```bash
python3 ~/hpclib/servers/rest_server.py --list-proposals
python3 ~/hpclib/servers/rest_server.py --approve-template xtb      # --replace to swap an existing one
python3 ~/hpclib/servers/rest_server.py --reject-template xtb
```

## 5. Tokens

```bash
# on the cluster
python3 ~/hpclib/servers/rest_server.py --list-tokens
python3 ~/hpclib/servers/rest_server.py --add-token ci --scopes read,submit \
  --token-allow /scratch/user/me/ci > ci_token          # printed once; only its hash is stored
python3 ~/hpclib/servers/rest_server.py --revoke-token ci   # takes effect at once
```

`setup_agents --rebuild` replaces the agent token and revokes the old one. A running MCP server reads its token
file again whenever the server rejects the token, so the client doesn't need a restart.

## 6. Limits and settings

`~/.local/tunnels/rest/config.json` on the cluster holds the job limits, the sandbox, environment variables for
jobs, and notes for the agent. The most common change, how many jobs may run at once:

```json
{"limits": {"max_concurrent_jobs": 20}}
```

The server reads the file at startup; the console's Settings page (or `PUT /admin/config` with the owner token)
changes it while the server runs. All sections are described in
[The MCP server and REST API](../mcp-server.md#configuration).

## 7. Scripts: `RESTClient`

The MCP server's client is a plain Python class you can use in your own scripts. `agent_env` sets the
environment it reads:

```bash
eval "$(agent_env user@login.example)"      # HPC_REST_URL and HPC_REST_TOKEN_FILE
```

```python
import sys; sys.path.insert(0, "/path/to/hpclib/hpclib/servers")
from rest_client import RESTClient

client = RESTClient.from_env()
job = client.submit_job("hello", params={"message": "from a script"}, idempotency_key="hello-1")
print(client.wait_job(job["job_id"], timeout=300))
print(client.tail_file(job["output"]))
```

`FileSync` in the same module copies files between your machine and the cluster through the API.

## 8. The console's API from a terminal

The interface's backend has a JSON API you can script against:

```bash
agent_console --port 27180 &
KEY=$(python3 -c 'import json,os; print(json.load(open(os.path.expanduser("~/.config/hpclib/console/session")))["key"])')
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:27180/api/clusters
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:27180/api/proposals
```

The routes are listed in the module docstring of `hpclib/servers/agent_console.py`.
