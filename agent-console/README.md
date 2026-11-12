# agent-console

Directory sync is available under **Data transfer → Directory sync** (`#/transfer/sync`).
Both Data transfer pages show a dedicated **Directory sync** panel above the SMB controls, with a primary
**Manage directory syncs** link. A cluster's link preselects that cluster. `sync.js` uses the existing shared elements and console API to save
pairs, edit warning thresholds, and review local ignore setup. The compact editor places local and remote
paths on the same row with a direction arrow (↔, → or ←); saved cards use the same paired layout.
Advanced settings holds the branch, interval and warning thresholds. In 1.14.1, **Check files** queues metadata-only
inventories and **Create snapshots…** captures eligible local/remote edits on an owned sync branch. Size/file-count
limits pause ingestion for a complete, inventory-bound approval. **Continue scan…** increases a bounded scan's
budget. In 1.14.2, **Create verified snapshots…** explicitly asks Git to rehash all eligible content, create/update
sync commits and exchange objects; unchanged content creates no new commit. Inventories list up to 100 eligible
paths even when unchanged, and each endpoint shows its saved snapshot ID after a check. Zero changed files means
unchanged metadata relative to that endpoint's own snapshot; it does not mean the working folders match.
Snapshots preserve working files,
HEAD and ordinary indexes, and sharing objects declares no successful working-file sync.
In 1.14.4, **Review sync…** captures permitted snapshots and builds one Git merge for all online participants.
**Apply reviewed sync…** separately confirms the displayed per-directory additions, edits and deletions.
Conflicts block the group; later edits invalidate review. Successful working-file application advances each
acknowledged pair's base and Last sync separately. **Retry reviewed sync…** reconciles interrupted applies;
mixed content or later edits require recovery and are never blindly rolled back. That release enabled bidirectional Git apply; 1.14.8 adds uploads and background scheduling. Rclone remains deferred. Remote requests only reuse the existing
console SSH master. Local merge preview requires Git's explicit-base merge-tree support (Git 2.43+).

In 1.14.5, each saved Git sync has **Allow agent access…** / **Revoke agent access…**. Access is off by default,
is independent of the sync settings revision, and is cleared when local/remote paths change. Enable the Data
transfer **sync_query** and **sync_manage** actions in the cluster's Agents tab. The running console polls scoped
requests using only its existing SSH master and invokes the same sync manager used by the UI. All targets in an
agent-managed group must be on the requesting cluster and inside that token's directories. No console session
key is exported, and there is no new inbound callback service. Restart the console and REST tunnel after updating.

In 1.14.7, object transfer uses native Git fetch/push through the cached SSH master. Git negotiates missing
objects and streams packs, preserving named remotes/configuration, ordinary indexes and FETCH_HEAD. Transfers
stage owned refs only; the existing separate review/apply step changes working files. Native pack operations
share endpoint locks with application, and a shared per-root journal prevents another group/private repository
from bypassing an unfinished apply. Broader multiple-connection-point identity and coordinator handoff remain
separate design work. Update both local/remote installations and restart the console and REST tunnel.

In 1.14.8, **Background sync** on each card selects Off, Upload local changes, Sync both directions,
or Check only. Existing registrations stay off. Choosing automatic uploads changes the direction to →;
two-way scheduling uses ↔. First review/apply is required before automatic working-file changes. Keep the
console open and the cluster connected; this does not install a login service. Pause and an interval of 0
stop scheduling. Size/selection reviews, conflicts, stale plans, inactive roots and interrupted applies block
further automatic work; connection failures retry with bounded backoff through the cached SSH master only.
Update/restart admission is coordinated with the worker pool.

Upload mode pushes source objects, plans/applies on the cluster, and leaves local working files unchanged.
Only current and previously uploaded source paths are scanned and snapshotted remotely. Cluster-only outputs
are neither fetched to the Mac nor added to these upload snapshots. Changes to managed inputs on the cluster
pause for review. Unchanged uploads skip another object transfer and do not create commits. Git still retains
source history; switching from two-way sync does not delete previously downloaded files/history. The most
recent 32 run logs are kept; recovery journals and Git history are preserved.

**Set up directories…** asks before creating initially missing local/remote paths, then offers Git initialization
when neither side is a repository. Declining can explicitly select private Git storage; Git itself is required
in either case. Each endpoint has its own reviewed ignore choice, including the merge-first `.gitignore`
question. Existing private snapshot history keeps its backing repository. Registered directories are pinned
by path/inode (including legacy snapshot claims); removal, movement or replacement marks the sync inactive
at the next check. It never recreates the old path. Restore the original directory and review again, or make
a fresh registration for its new location using a fresh sync branch if the old one is still owned. Automatic
relocation, cross-computer identity and connection-point handoff remain separate design work.

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

The page imports its shared elements from `./hpclib-ui/` (the files in `hpclib/ui/`), which `agent_console` serves
itself. A plain static server does not have that path: link it first, `ln -s ../hpclib/ui agent-console/hpclib-ui`
(the link is not committed; `.gitignore` lists it).

The top bar holds what spans clusters: **Clusters** (`#/c`, one row per cluster), **Proposals** and
**Packages**. A cluster's page (`#/c/NAME/TAB`, `cluster.js`) has its Overview, Jobs, Apps, Installed, Storage,
Files, SSH keys, Agents (the agent profile's row and Activity) and Settings; `#/c/NAME/apps/APP` is one app there
with its settings in place, and a package's tabs for it. The older pages below still open from their addresses.
The tunnel apps the console lists (`GET /api/apps`): **JupyterLab**, **VS Code** and **PAI**, each a session per
cluster in a SLURM job behind its own tunnel (Start, Open, Stop, Log,
Settings: the job's time/memory/CPUs/partition, JupyterLab's conda environment, modules or uv/pixi project, and
the tunnel's own settings such as VS Code's container path). Open goes straight in, with the token JupyterLab
printed; VS Code shows a **Copy password** button. Once you're logged in, each row checks whether the tunnel is
installed on the cluster (its `install.sh --check`) and says **not installed** until it is; **Settings…** has what
the cluster reported, **Check**, and **Install** (or Reinstall), whose output shows below the row. PAI's row says which job serves the shared database, and whether this tunnel started it or only
connected to it, and once PAI is installed its Install button is **Update**: it fast-forwards the
proto-auto-interface checkout and pulls the app's image again (its Image setting says from where). **Data transfer** runs on the login node, not in a job: per cluster it installs the
data-transfer-tools image, starts a web GUI for the SMB server (rclone's, or the one its GUI setting names), and
signs you in to the server for `smbshell` beside it (Kerberos log in with kinit where the cluster has it,
or a password saved for sync jobs). Selecting password sign-in offers **Save password for sync jobs** even
when Kerberos is available; both methods use the password dialog. Agents keeps its addresses (`#/clusters`, `#/files?...`); other apps live under
`#/APP/PAGE`, e.g. `#/vscode/sessions`. A new tunnel app is an entry in `APPS` in
`hpclib/servers/agent_console.py`, or, for one that doesn't belong in hpclib, a package: **Packages**
(top right) checks a package zip, shows what it adds (apps, tunnels, settings and their values), installs it,
and lists and removes installed ones (see "Console packages" in hpclib's README). Its apps join each cluster's Apps tab.

**Open Data transfer** clears the stock rclone GUI's filesystem-client cache before opening it,
so it reconnects using the current session configuration, including remotes supplied by settings
packages. If rclone reports active transfers or file checks, Open leaves the cache alone and asks
you to retry when they finish. A failed reset shows an error without opening the GUI. Start already
creates a fresh rclone process; background status refreshes never clear the cache. Custom GUIs
keep their ordinary Open link. This refresh uses the running session's configuration; changes saved
in the console's Settings still require Stop and Start to reach that session.

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

## Logs and tunnel shutdown

An app's **Settings → Job: Node list (optional)** can request a particular node or Slurm host list.
Leave it blank to use the tunnel's defaults; PAI 0.2.6 has no node restriction, so Slurm chooses.
Clearing the field removes its `--nodelist` argument from subsequent submissions. A shared app still
attaches to an existing database job, whose node does not change until that database job is replaced.

Log panels have a **Copy last block** button. It fetches the newest command block, starting with its
`=== YYYY-MM-DD HH:MM:SS command ===` header, and copies plain text through the current end of that
log. This includes output beyond the short display tail and removes terminal colors and title codes.
The button is available for agent tunnels, app tunnels and installation/setup operations. A block over
8 MiB reports an error instead of copying an incomplete block.

Stopping or restarting an owned tunnel waits for Slurm to finish stopping its recorded job before
reusing the tunnel's port. If shutdown cannot be confirmed within 20 seconds, the console reports the
failure and keeps the record: wait for the job to finish and retry **Stop**. The command-line wait can
be adjusted with `HPCLIB_TUNNEL_STOP_WAIT` on the login node.

## Job activity

A cluster's **Agents → Activity → Jobs** tracks recent agent-API submissions and their finished outcomes.
Filter by agent token, state, submission time, or job ID/template/label. Job details include scheduler
information, array tasks, and bounded output previews; **Follow** refreshes every five seconds.
**Requests** keeps the existing audit log view. This does not import jobs from the separate `job-queue`
registry. See the [Activity guide](../docs/interface/agents.md#activity) for history limits and controls.

## Packaged cluster configurations

Packages can add configuration choices to **Clusters → Add cluster**. Install the
package through **Packages**, choose its configuration, then enter your own cluster
username, absolute work directory and allocation. Resource defaults are editable. Adding creates
a local profile; log in and choose **Set up…** afterwards. Package installation, replacement and
removal preserve existing profiles and credentials.

Login supports passwords, Duo pushes and numeric MFA codes. **Enter code** reopens a pending
code prompt. Codes are sent once for the current challenge and are not stored. For presets with
site-managed SSH keys, **Set up SSH** and console SSH-file changes are disabled; public-key
inspection and copying remain available.

See the [cluster preset package format](../docs/cluster-presets.md).

## SSH keys

A cluster's **SSH keys** tab (`#/c/NAME/ssh`) has separate controls for this computer's login key,
the cluster's compute key, and general management of keys stored on the cluster. After the first
login, Overview offers **Set up login key…**. Pick an existing local identity or explicitly create
and install a dedicated Ed25519 key (without a passphrase). The public key is authorized through
that active login, and future hpclib logins use the selected local identity. MFA may still be required.
**Set up compute SSH** creates or reuses a compute identity in the cluster's shared home. Both
setup actions work before agents or a REST tunnel exist. Sites that manage SSH keys retain that policy.
General cluster key management requires a live REST tunnel and owner token; its private keys
stay on the cluster. The old `#/c/NAME/agents/keys` link opens the restored tab. See the
[SSH key guide and REST reference](../docs/ssh-keys.md).

## The console menu

The top-left hamburger menu holds what belongs to this machine rather than a cluster (`settings.js`):

- a **dark-mode switch** (sun and moon). It is kept in this browser (`localStorage`, read by `theme.js` before
  the page draws) and sets `data-theme` on `<html>`; **Match system** removes the choice.
- **Interface Settings** (`#/interface`): every variable in `default-styles.css`, grouped and described, with
  light and dark values for the themed colours: a form over `user.css`. It shows what `user.css` sets, previews
  changes as you type, and **Save** writes them into `user.css` (console_styles.py). Links to the default
  stylesheet and `user.css`.
- **Console Settings** (`#/console`): browser notifications (a tunnel dropping, a password or host-key prompt, an
  agent's job finishing, a new proposal), the local port range and each tunnel's ports (**New ports** while it is
  stopped), the LLM clients that get each cluster's MCP entry (Claude Desktop, Claude Code, Cursor, and ChatGPT
  through Codex's `config.toml`; see `hpclib/servers/mcp_clients.py`), and defaults for new clusters (login time,
  work directory, MCP name pattern, GPU mode and limit). A cluster's MCP name is changed on its Agents tab.
- **About** (`#/about`): the hpclib this console runs and the one on disk (restart when they differ), paths, the
  console's link, and **Copy diagnostics** (no tokens, passwords or keys).
- **Open Stylesheet**, below.

Packages can also add **widgets** (`widgets.js`): one HTML file each, shown in a sandboxed iframe (no
`allow-same-origin`, a CSP with no network) on the clusters page, each cluster's Overview or a tab, fed read-only
topics (clusters, jobs, storage, apps, theme) by `postMessage`. See "Console widgets" in `docs/extending.md`.

## User styling

The menu's **Open Stylesheet** (also on Interface Settings) opens `user.css`. On its first use, the console copies the
bundled [`default-styles.css`](default-styles.css) to `~/.config/hpclib/console/user.css`, then asks
the operating system to open it with the default application associated with CSS files. If
`HPCLIB_CONSOLE_DIR` is set, the destination is `$HPCLIB_CONSOLE_DIR/user.css` instead.
An existing `user.css` is always preserved, including an empty one. The console does not select or
detect an editor. Saving changes and reloading the browser applies them.

With `launch-tunnel-manager` or `agent_console --static ...`, the page loads `default-styles.css`
(the complete default variable inventory), then `style.css` (the UI rules), then the optional `user.css`, which
wins over both. Interface Settings reads and writes `user.css` itself, so there is one place for your styling.
Keep personal styling in `user.css` so it survives updates to hpclib. You can also create it yourself:

```bash
mkdir -p "${HPCLIB_CONSOLE_DIR:-$HOME/.config/hpclib/console}"
# edit user.css in that directory, then reload the browser page
```

For example, `user.css` can contain:

```css
:root {
  --accent: #7c3aed;
  --font-size-base: 16px;
  --content-max-width: 1500px;
  --radius-panel: 12px;
  --dialog-max-width: 640px;
  --log-max-width: 800px;
}
/* dark: the system's (unless the menu's switch chose light), or the switch's */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --accent: #a78bfa; }
}
:root[data-theme="dark"] { --accent: #a78bfa; }
```

A `user.css` copied before the switch existed has a plain `@media (prefers-color-scheme: dark) { :root { ... } }`
block; it still works with the system's theme, but wins over the switch's Light. Change its selector as above.

The [complete UI variable reference](UI_VARIABLES.md) lists every variable, its default and its purpose,
including dark-theme defaults and component parts. Variables also reach panel and dialog shadow DOM.
Log panels default to a maximum width of `960px` and stay within their container and viewport.
Long lines scroll inside the log; to wrap them instead, set `--log-white-space: pre-wrap` and
`--log-overflow-wrap: anywhere`. Tunnel/app logs and installation/setup output share these settings.

Opening the stylesheet uses authenticated `POST /api/styles/open`; it always opens the fixed local
`user.css` path on the machine running the console. On macOS it uses `open`, on Windows `os.startfile`,
and on Linux `xdg-open`, leaving the CSS file association to the OS.

The console serves only this file at `/user.css`, without a session key, just like its bundled static
assets. An absent file returns an empty stylesheet. It reads the file on each request and sends
`Cache-Control: no-store`, so edits take effect after a full browser reload (the console's **Refresh**
button refreshes page data only). The rest of the console directory is not served.
This automatic loading applies to the page served by `agent_console --static`; a separately hosted
front end must arrange to serve its own `user.css` at the same URL on its own origin.

## Components

`components.js` defines the page's building blocks as native custom elements (no library, no build step);
`app.js` builds pages from them:

| Element | What it is |
| --- | --- |
| `<hpc-panel heading status tone dismissible>` | a bordered panel with a title, a status and an optional × in the corner; × hides it (or whatever a `dismiss` listener does instead). Buttons with `slot="actions"` sit in its header. |
| `<hpc-log src field every empty>` | output that fetches itself from an `/api` path, refreshes every `every` seconds and keeps to the bottom unless you scroll up; it stops when removed. Copy last block fetches that path with `latest=1`. Fires `update` with each display refresh. |
| `<hpc-dialog heading transient>` | a modal with a title and ×; Escape closes it; `transient` removes it once closed. |
| `<hpc-state state label tone>` | an API state word (`up`, `queued`, `failed`, ...) in its colour. |
| `<hpc-field label hint>` | a form control with its label and hint. |
| `<hpc-action-button label primary result>` | a button running an async `.action`; it is off while that runs, and an error (or, with `result`, the returned message) shows beside it. |
| `<hpc-cluster-picker live value>` | a select of clusters; `live` keeps those whose tunnel is up with an owner token. `value` may be the profile name, login host or MCP name. |
| `<hpc-env-editor label>` | `NAME=value` lines, checked as you type against the server's own rules (`rules.environment` from `GET /admin/config`). |
| `<hpc-cluster-row cluster local>` | an agent profile's row in the Clusters table, with a detail row for its log, setup form or install/setup output. |
| `<hpc-app-session app app-title cluster>` | a tunnel app's session on one cluster (Open, Start, Stop, Log, Settings). |
| `<hpclib-toolbar>` of `<hpclib-toolbar-tool label>` | tunnel-specific tools in a row under a session's row: text, selects, anything wider than a button. Hidden while empty. An app with `downloads` (OpenCode's Model Download) gets a tool there: kind, name, **Download** (an operation with its log below the row, like Install) and **Downloaded…** (what is on the cluster, with sizes and **Remove**). |
| `<hpclib-extra-controls>` of `<hpclib-control hint>` | tunnel-specific controls under a session's buttons, usually `<hpc-action-button>`s. Hidden while empty. |

`confirmDialog(heading, text, ok)` asks a yes/no question in an `<hpc-dialog>`. A session's state from the
console carries the `tools` and `controls` for its row (see `APPS` in `agent_console.py`); controls run with
`POST /api/apps/APP/NAME/control/ID`. PAI uses both: the toolbar names the job serving the database (and
whether this tunnel started it or only connected), and **End database job** cancels that job after asking,
only if it is yours and registered as a PAI database, and stops this tunnel. Stop itself still only
disconnects.

When launch metadata is unavailable (including after a verbose log has exceeded the console's log
tail), shared apps recover their jobs from the cluster's instance registry over the existing SSH login.
Registered jobs remain manageable while the local tunnel is stopped. Recovery results are cached for
30 seconds; **Refresh instances** checks immediately. Each registered job has its own **End database
job** control. Registry-query failures are shown explicitly rather than being reported as no jobs.

The two row elements fetch their own state, every 3 s while something is in motion (a login, a starting tunnel
or session, an install) and every 30 s otherwise. They redraw only themselves, so pages aren't reloaded to follow
a tunnel. For what only the page does (the login dialog, the setup and app settings forms, the password dialog)
they fire events that bubble to `document`: `hpc-login`, `hpc-setup`, `hpc-app-settings` and `hpc-prompt`.

**Second logins.** A tunnel's ssh can ask for a password minutes after Start, once its job runs (the login node's
ssh to the compute node). The page asks the console's local `GET /api/prompts` every 2 s while something it
started is running, so the dialog opens on whatever page you're on. "Not now" leaves the request on the cluster's
row ("Enter password…") until it asks again.

Panels and dialogs draw their frames in shadow DOM; colours come from the CSS variables in `default-styles.css`, so the
light and dark themes reach inside. Install and setup output, tunnel and app logs, the file viewer, the setup and
app settings forms, and errors are all dismissible panels; a closed install/setup panel stays closed (this browser
remembers it), and an open log stays open while you move between pages.

This folder depends only on the documented `/api` routes; hpclib never imports it.
