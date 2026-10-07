# Using the interface

The tunnel manager, also called the Agent Console, is a web page served by a small program on your own machine.
It runs hpclib's commands for you: it logs in to clusters, installs and sets them up, starts and stops tunnels,
opens apps in your browser, and shows what agents are doing. The page never sees your passwords or tokens; the
console program keeps them and talks to the clusters.

This section assumes hpclib is installed on your machine and `launch-tunnel-manager` is on your `PATH`
([Installation](../installation.md), step 1). Everything else, including installing hpclib on clusters, can be
done from the page.

1. [Getting started](getting-started.md): start the console, add a cluster, log in, connect an LLM client.
2. [Agents](agents.md): the Clusters, Proposals, Activity, Files and Settings pages.
3. [Apps](apps.md): JupyterLab, VS Code, PAI, Data transfer, Rclone, and apps added from packages.

## How the page is laid out

The **top bar** switches between apps. **Agents** is always first; then come the tunnel apps hpclib ships
(**JupyterLab**, **VS Code**, **Data transfer**, **Rclone**, **PAI**) and any added from packages. On the right,
**Add App or Settings** installs packages.

Each app has its own pages under its name. Agents has Clusters, Proposals, Activity, Files and Settings; the
tunnel apps have a Sessions page with one row per cluster.

Rows refresh themselves: every few seconds while something is happening (a login, a tunnel starting, an
install), and every 30 seconds otherwise. You don't need to reload the page to follow a tunnel.

## What the console keeps, and where

| What | Where on your machine |
| --- | --- |
| the session key for the page | `~/.config/hpclib/console/session` (new at each launch) |
| each cluster's profile and tokens | `~/.config/hpclib/agents/USER@HOST/` |
| tunnel and app logs | `~/.config/hpclib/console/logs/` |
| installed packages | `~/.config/hpclib/console/packages/` |

The console listens only on `127.0.0.1` and refuses requests without the session key, so other users of your
machine and other web pages can't use it.
