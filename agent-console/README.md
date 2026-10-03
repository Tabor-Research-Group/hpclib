# agent-console

A minimal browser front end for `agent_console`, the local backend in
`hpclib/servers/agent_console.py`. Plain HTML, CSS and JavaScript modules, no build step.

```bash
agent_console --static ~/path/to/hpclib/agent-console --open
```

`--open` opens `http://127.0.0.1:27180/#key=…`; the page keeps the key for that browser tab
(sessionStorage) and removes it from the address bar. Without it, the page asks for the key that
`agent_console` printed.

To serve it from somewhere else (a dev server, say), allow that origin and point the page at the API:

```bash
agent_console --allow-origin http://127.0.0.1:8000
python3 -m http.server 8000 --bind 127.0.0.1 -d agent-console
# open http://127.0.0.1:8000/?api=http://127.0.0.1:27180
```

Pages:

| Page | Shows | Actions |
| --- | --- | --- |
| Clusters | every agent profile, its tunnel state and hpclib version, ssh login, port and token | add a cluster; log in (password, then a Duo push) or out; update hpclib or rerun setup_agents, with the log; start or stop the tunnel, view its log |
| Proposals | templates waiting for review on every live cluster | diff against the current template, approve, reject with a reason |
| Activity | one cluster's audit log, newest first | filter by token or errors, follow live (every 5 s) |
| Files | a cluster's allowed directories | browse; view text (whole up to 5 MB, else the first 1 MB), images, SVG and HTML (up to 25 MB; HTML in a sandboxed frame, scripts off unless you allow them); download (asks first above 25 MB, since the browser holds the file in memory) |
| Settings | per cluster: this machine's tunnel settings, and the server's config.json | auto-approve mode, the tunnel job's time/memory/partition, how long the ssh login is kept; sync modules, uv/pixi, job limits, sandbox directories, environment variables for jobs and syncs, notes for agents (or the editable sections as JSON) |

This folder depends only on the documented `/api` routes; hpclib never imports it.
