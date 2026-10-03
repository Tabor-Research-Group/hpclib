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
| Clusters | every agent profile, its tunnel state, port and token | start or stop the tunnel, view its log |
| Proposals | templates waiting for review on every live cluster | diff against the current template, approve, reject with a reason |
| Activity | one cluster's audit log, newest first | filter by token or errors, follow live (every 5 s) |

This folder depends only on the documented `/api` routes; hpclib never imports it.
