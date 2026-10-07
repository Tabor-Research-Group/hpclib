# hpclib documentation

hpclib connects your own machine to HPC clusters and development servers. It gives you three things:

- **Tunnels.** Start a program on a cluster (JupyterLab, VS Code, a database, a web app) and reach it from
  your browser as if it ran locally, through ssh port forwarding.
- **Agents.** Let an LLM client on your machine (Claude Desktop, Claude Code, any MCP client) run jobs on a
  cluster, limited to job templates and directories you approve, through a REST server on the cluster and an
  MCP server on your machine.
- **The tunnel manager.** A local web interface (the Agent Console) that does both from a browser: logging in,
  setting up clusters, starting and stopping tunnels and apps, reviewing what agents do.

Everything the interface does is also a shell command, so you can use either one, or both.

## Where to start

| If you want to... | Read |
| --- | --- |
| install hpclib on your machine and a cluster | [Installation](installation.md) |
| work from the browser | [Using the interface](interface/README.md) |
| work from a terminal | [Using the command line](cli/README.md) |
| understand how a tunnel gets from your browser to a compute node | [Tunnel architecture](architecture.md) |
| understand what an agent can and can't do on a cluster | [The MCP server and REST API](mcp-server.md) |
| add a tunnel, a console app or a job template | [Extending hpclib](extending.md) |
| look up a file, setting or environment variable | [Reference](reference.md) |
| fix something that went wrong | [Troubleshooting](troubleshooting.md) |

The interface and command-line sections are written to be read on their own: each starts from an installed
hpclib and goes through a complete session without depending on the other.

## Words used in these documents

| Term | Meaning |
| --- | --- |
| your machine | the laptop or workstation you work on; the browser, the console and the MCP server run here |
| login node | the cluster machine you `ssh` to; tunnels start here |
| compute node | the machine SLURM gives a job; most services run here |
| tunnel | a named folder (`hpclib/tunnels/NAME`) that says how to start one service and forward its port |
| app | a tunnel the console shows as a page of its own, with Start, Open and Stop |
| agent profile | what hpclib keeps on your machine about one cluster set up for agents |
| template | a job an agent may submit: a `template.json` and a `script.sh` on the cluster |
| token | a secret an API client sends; the owner token can do anything, scoped tokens only what their scopes allow |
| package | a zip file that adds apps, tunnels or settings to the console without changing hpclib |
