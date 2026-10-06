# agent-console

A minimal browser front end for `agent_console`, the local backend in
`hpclib/servers/agent_console.py`. Plain HTML, CSS and JavaScript modules (with native custom elements), no build step.

```bash
launch-tunnel-manager              # hpclib/launch-tunnel-manager, linked onto your PATH (see hpclib's README)
agent_console --static ~/path/to/hpclib/agent-console --open      # the same, by hand
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

A top bar switches between the console's apps, each with its own header and pages: **Agents** (the agents'
REST tunnels: the pages below) and the tunnel apps the console lists (`GET /api/apps`): **JupyterLab**,
**VS Code** and **PAI**, each a session per cluster in a SLURM job behind its own tunnel (Start, Open, Stop, Log,
Settings: the job's time/memory/CPUs/partition, JupyterLab's conda environment, modules or uv/pixi project, and
the tunnel's own settings such as VS Code's container path). Open goes straight in, with the token JupyterLab
printed; VS Code shows a **Copy password** button. Once you're logged in, each row checks whether the tunnel is
installed on the cluster (its `install.sh --check`) and offers **Install** (or Reinstall), whose output shows
below the row. PAI's row says which job serves the shared database, and whether this tunnel started it or only
connected to it. **Data transfer** isn't a tunnel: per cluster it installs the data-transfer-tools image and
signs you in to the SMB server for `smbshell` (Kerberos log in with kinit where the cluster has it, otherwise a
password saved for sync jobs), both through the password dialog. Agents keeps its addresses (`#/clusters`, `#/files?...`); other apps live under
`#/APP/PAGE`, e.g. `#/vscode/sessions`. A new tunnel app is an entry in `APPS` in
`hpclib/servers/agent_console.py`; the page picks it up.

Agents pages:

| Page | Shows | Actions |
| --- | --- | --- |
| Clusters | every agent profile, its tunnel state and hpclib version, ssh login, port and token | add a cluster; log in (password, then a Duo push) or out; update hpclib or rerun setup_agents, with the log; start or stop the tunnel, view its log |
| Proposals | templates waiting for review on every live cluster | diff against the current template, approve, reject with a reason |
| Activity | one cluster's audit log, newest first | filter by token or errors, follow live (every 5 s) |
| Files | a cluster's allowed directories | browse; view text (whole up to 5 MB, else the first 1 MB), images, SVG and HTML (up to 25 MB; HTML in a sandboxed frame, scripts off unless you allow them); download (asks first above 25 MB, since the browser holds the file in memory) |
| Settings | per cluster: this machine's tunnel settings, and the server's config.json | auto-approve mode, the tunnel job's time/memory/partition, how long the ssh login is kept; sync modules, uv/pixi, job limits, sandbox directories, environment variables for jobs and syncs, notes for agents (or the editable sections as JSON) |

Links: the Files page keeps its place in the address, so a folder or a file can be linked to (and bookmarked):

```
http://127.0.0.1:27180/#/files?cluster=maboyer@grace.hprc.tamu.edu&path=/scratch/user/maboyer/llm/run/conf_00.log
```

A file opens in the viewer inside its folder, with the same size rules as clicking View (and Download still
asks above 25 MB); a folder opens that folder. `cluster` may be the profile name, the login host or the MCP
name (`hpclib-grace`). The session key is kept for the console's address (localStorage), so links work in new
tabs; after the console restarts, the page asks for the new key once.

## Components

`components.js` defines the page's building blocks as native custom elements (no library, no build step);
`app.js` builds pages from them:

| Element | What it is |
| --- | --- |
| `<hpc-panel heading status tone dismissible>` | a bordered panel with a title, a status and an optional × in the corner; × hides it (or whatever a `dismiss` listener does instead). Buttons with `slot="actions"` sit in its header. |
| `<hpc-log src field every empty>` | output that fetches itself from an `/api` path, refreshes every `every` seconds and keeps to the bottom unless you scroll up; it stops when removed. Fires `update` with each answer. |
| `<hpc-dialog heading transient>` | a modal with a title and ×; Escape closes it; `transient` removes it once closed. |
| `<hpc-state state label tone>` | an API state word (`up`, `queued`, `failed`, ...) in its colour. |
| `<hpc-field label hint>` | a form control with its label and hint. |
| `<hpc-action-button label primary result>` | a button running an async `.action`; it is off while that runs, and an error (or, with `result`, the returned message) shows beside it. |
| `<hpc-cluster-picker live value>` | a select of clusters; `live` keeps those whose tunnel is up with an owner token. `value` may be the profile name, login host or MCP name. |
| `<hpc-env-editor label>` | `NAME=value` lines, checked as you type against the server's own rules (`rules.environment` from `GET /admin/config`). |
| `<hpc-cluster-row cluster local>` | an agent profile's row in the Clusters table, with a detail row for its log, setup form or install/setup output. |
| `<hpc-app-session app app-title cluster>` | a tunnel app's session on one cluster (Open, Start, Stop, Log, Settings). |
| `<hpclib-toolbar>` of `<hpclib-toolbar-tool label>` | tunnel-specific tools in a row under a session's row: text, selects, anything wider than a button. Hidden while empty. |
| `<hpclib-extra-controls>` of `<hpclib-control hint>` | tunnel-specific controls under a session's buttons, usually `<hpc-action-button>`s. Hidden while empty. |

`confirmDialog(heading, text, ok)` asks a yes/no question in an `<hpc-dialog>`. A session's state from the
console carries the `tools` and `controls` for its row (see `APPS` in `agent_console.py`); controls run with
`POST /api/apps/APP/NAME/control/ID`. PAI uses both: the toolbar names the job serving the database (and
whether this tunnel started it or only connected), and **End database job** cancels that job after asking,
only if it is yours and registered as a PAI database, and stops this tunnel. Stop itself still only
disconnects.

The two row elements fetch their own state, every 3 s while something is in motion (a login, a starting tunnel
or session, an install) and every 30 s otherwise. They redraw only themselves, so pages aren't reloaded to follow
a tunnel. For what only the page does (the login dialog, the setup and app settings forms, the password dialog)
they fire events that bubble to `document`: `hpc-login`, `hpc-setup`, `hpc-app-settings` and `hpc-prompt`.

**Second logins.** A tunnel's ssh can ask for a password minutes after Start, once its job runs (the login node's
ssh to the compute node). The page asks the console's local `GET /api/prompts` every 2 s while something it
started is running, so the dialog opens on whatever page you're on. "Not now" leaves the request on the cluster's
row ("Enter password…") until it asks again.

Panels and dialogs draw their frames in shadow DOM; colours come from the CSS variables in `style.css`, so the
light and dark themes reach inside. Install and setup output, tunnel and app logs, the file viewer, the setup and
app settings forms, and errors are all dismissible panels; a closed install/setup panel stays closed (this browser
remembers it), and an open log stays open while you move between pages.

This folder depends only on the documented `/api` routes; hpclib never imports it.
