# Workspace redesign: layout, shared UI, submission and environments

Status: design for review, 2026-10-29. Written against `packages/workspace` 1.0.1 and hpclib 1.13.6.

This is the contract between three pieces of work: the overall layout and shell (here), the file manager (being
redone by another contributor), and job submission with environment management (specified here, built once the
shell exists). It says what to build, where the seams are, and why. It is not the implementation.

## What the Workspace is for

An intermediate layer between the console and a cluster's shell, doing three things in this order of importance:

1. **Submit a job from a file without writing an `sbatch` script**, with the Python environment handled for you.
2. **Browse** the cluster's files, and open and edit the ones you need.
3. **Give you a terminal** that survives a dropped connection.

It is not a job browser (the console's Jobs tab, `check_quota`, the job queue and the agent API are), not an IDE
(VS Code and JupyterLab are apps), and not a sync tool (Data transfer is).

## Summary of decisions

| Question | Decision | Section |
| --- | --- | --- |
| Bootstrap or jQuery? | Neither: hpclib's CSS variables and custom elements already cover it, and Bootstrap would fight `default-styles.css`. | 2.3 |
| Third-party libraries | xterm.js 6 (+ fit, web-links, search addons) and CodeMirror 6, from a CDN, pinned, with integrity hashes and an offline vendor mode. PDFs use the browser's viewer. | 2.3 |
| Where does shared UI live? | New `hpclib/ui/`. The console serves it; the Workspace package gets a copy at build time, so it never depends on the hpclib version on the cluster. | 2.2 |
| What replaces "My jobs"? | "Runs from here": only jobs this Workspace submitted. No queue browser. | 3 |
| How is a job described? | A small JSON spec (command, environment, resources). The script is generated from it and embeds it, so a `.slurm` file reopens as the same form. | 4 |
| How are environments handled? | Jobs only activate; a visible Prepare step builds them where there is network. Same record files as hpclib's REST environments, so agents and the Workspace share them. | 5 |
| Request authentication | Tighten before anything else (header-only token, JSON content type, origin checks, short-lived file URLs). | 1.1 |

## 1. Audit of the current Workspace

The current code is small and readable (about 2,500 lines, standard library only on the server, no build step on
the page). The problems are in what it assumes, not in how it is written.

### 1.1 Request authentication (do this first)

- The token is accepted in the query string on every route (`authorized()` reads `?token=`), and is embedded in
  every `/raw/TOKEN/...` URL. Anything that learns a URL learns a credential that can call every API route.
- State-changing routes parse the body as JSON regardless of `Content-Type` (`json_body()`), and only the terminal
  route checks `Origin`.
- The viewer serves user-supplied HTML with scripts enabled (`sandbox allow-scripts allow-popups allow-forms
  allow-modals`). Untrusted content should never be near a credential.

Required changes, all server-side and small:

1. API routes and the terminal take the token only in the `X-Workspace-Token` header. The WebSocket, which cannot set
   headers from page JavaScript, gets a single-use ticket from `POST /api/term/ticket` (valid ten seconds).
2. `/raw/` URLs are signed per directory and expire: `/raw/EXP.HMAC/dir/file`, with HMAC over the directory and
   expiry, minted by `POST /api/raw-url {path}`. Relative resources of an HTML page still resolve under the same
   signed prefix. The long-lived token never appears in a URL.
3. Every non-GET route requires `Content-Type: application/json` and refuses requests whose `Origin` is present and
   not the page's own (including `Origin: null`) or whose `Sec-Fetch-Site` is `cross-site`.
4. Drop `allow-forms` and `allow-modals` from the HTML viewer's sandbox.
5. Add a regression test beside the existing Playwright tests: a hostile HTML file previewed in the viewer cannot
   cause any API call to succeed.

### 1.2 Robustness

| Finding | Where | Consequence | Fix |
| --- | --- | --- | --- |
| Terminals die with the WebSocket | `bridge()` calls `term.close()` (SIGHUP) when the socket ends | A page reload or a tunnel blip kills the shell and whatever ran in it | Detached sessions (section 6) |
| A login shell per query | `login_shell_run` is `bash -lc` for every `squeue`, `scancel`, `sbatch` | Slow on busy login nodes; profile banners can pollute output | Capture the login environment once, reuse it |
| Jobs polled every 30 s per open page | `refreshJobs()` | Load on the scheduler for a feature hpclib already has | Remove the pane (section 3) |
| Editor limit 800 MB | `WORKSPACE_EDIT_LIMIT_MB`; `TextDecoder.decode` | JavaScript strings top out near 512 MB, and editors stall long before that | Default 64 MB; larger text opens in a paged, follow-capable viewer |
| No way to follow a running job's output | Output opens as a whole-file read | The most common thing to do after submitting does not work | `GET /api/fs/tail` with offsets, shown in `<hpc-log>` |
| A reload loses everything | Only `#dir=` is kept | Open files, unsaved edits and terminals vanish | Persist layout and drafts; reattach terminals |
| The file list is not keyboard-operable | `<tr onclick>`, "⋯" is `visibility:hidden` until hover | Not usable without a mouse | File manager's job; the shell supplies roving focus helpers |
| Tabs are `div`s with `role="tab"` | `drawTabs()` | Roles without focus or arrow-key handling | Shared `<hpc-tabs>` |
| Pins are behind | CodeMirror 5.65.18, xterm 5.5.0, fit 0.10.0 | Current: 5.65.21, 6.0.0, 0.11.0 | Section 2.3 |
| Theme ignores the console | Only `prefers-color-scheme`; own palette names (`--ok`, `--danger`, `--hover`) | The console's dark switch and Interface Settings do not apply | Shared tokens, theme bridge (2.2) |

### 1.3 Hand-rolled pieces

| Piece | Where | Replace with |
| --- | --- | --- |
| `el()`, two incompatible copies | `workspace.js`, `components.js` | The shared one |
| Tabs | `drawTabs`, `drawTermTabs` | `<hpc-tabs>` (new, shared) |
| Splitters | `dragResize` | `<hpc-split>` (new, shared; keyboard, persisted) |
| Context menu | `showMenu` | `<hpc-menu>` on the native `popover` attribute |
| Toast | `toast()` | `<hpc-toast>` with an `aria-live` region |
| Dialog | `ask()` on `<dialog>` | `<hpc-dialog>` (exists) plus a shared `ask()` |
| Panels, log, state words, fields | partly | `<hpc-panel>`, `<hpc-log>`, `<hpc-state>`, `<hpc-field>` (exist) |
| Disk-usage table | `openUsage`, duplicating the console's `storageCard` in `cluster.js` | One shared `<hpc-usage-table>` |
| WebSocket and pty bridge | `ws_read`, `ws_send`, `Terminal` | Keep: the server is standard library only. Add server pings and tests. |

### 1.4 Submission today

The Submit tab is a form over a text template. What it gets wrong:

- **Duplication.** The same 25-line header and prologue is copied into four templates; fields are declared four
  times. They will drift.
- **Untyped fields.** Time, memory and GPUs are free text; nothing knows the cluster's partitions, limits, accounts
  or GPU types, so errors arrive from `sbatch` after the fact.
- **The environment is a text field.** `uv project` defaults to the script's directory whether or not it contains
  a `pyproject.toml`. Nothing says whether it is synced, stale or missing.
- **Jobs resolve at run time.** `uv run` in the job syncs on first use. On a compute node without internet that
  fails; with an array it races. hpclib's rule is that jobs only activate environments and never resolve or
  download (`rest_envs.py`). The template's own hint ("`--no-sync` where compute nodes have no internet") pushes
  that on the user.
- **A hand edit disconnects the form.** After editing the preview, the form no longer drives the script.
- **A script is written beside your source on every submit**, and replacing one needs a checkbox.
- **The termination warning is not wired up.** `--signal=B:TERM@60` is set but nothing traps it or forwards it
  to the Python process, so the minute of warning is unused.
- **Values are substituted unquoted** (`python -u "{{input}}" {{args}}`), so a file name with `"`, `$` or a
  backtick, or a stray quote in Arguments, changes what the script does.
- **Hard dependency on hpclib's path.** `. "/home/.../hpclib.sh"` fails the job outright if that file is gone.
- **`. ~/.bashrc` under `--export=NONE`** breaks on `.bashrc` files that return early for non-interactive shells;
  there is no choice and no warning.

## 2. Architecture

### 2.1 Modules and the seam with the file manager

```text
packages/workspace/tunnels/workspace/
  workspace_server.py      HTTP, auth, routing, static files (stays standard library, Python 3.6+)
  ws/auth.py               token, tickets, signed file URLs, origin checks
  ws/fs.py                 files API                                   <- file manager owns this
  ws/terminal.py           detached pty sessions and the WebSocket
  ws/shellenv.py           login environment captured once; run() helper
  ws/cluster.py            sinfo, sacctmgr, modules, quota (shared, see 5.5)
  ws/envs.py               environment detection and Prepare (shared, see 5.5)
  ws/jobs.py               spec -> script, check, submit, runs registry
  ws/recipes.py            replaces script_templates.py; classic templates still work
  ui/index.html            the shell only
  ui/app.js                layout, registry, persisted state
  ui/features/files/       <- file manager owns this
  ui/features/{editor,viewers,terminal,submit,env,usage}/
  ui/hpclib-ui/            build-time copy of hpclib/ui (2.2)
```

The file manager and the rest meet in three places, so neither needs to read the other's code.

**Server.** The file manager keeps the existing `/api/list`, `/api/stat`, `/api/file`, `/api/mkdir`, `/api/rename`,
`/api/delete` and `/api/usage` shapes while the client is replaced, then moves them under `/api/fs/`. Two additions
are needed from it: `GET /api/fs/tail?path=&offset=&lines=` (bytes since `offset`, for following a log) and a
change signal after writes. Detection of project roots (the nearest directory above a script with `pyproject.toml`,
`pixi.toml`, `uv.lock` or `.venv`) is in `ws/envs.py`, not `fs.py`.

**Page.** Features contribute to a small registry on `window.ws`; the shell renders what is registered.

```js
ws.registerOpener({ id, match(entry) -> score, open(path, entry) })      // editor, image, pdf, log follower ...
ws.registerAction({ id, label, group, when(entry) -> bool, run(entry) }) // "Submit job...", "Prepare environment..."
ws.registerDock({ id, title, create(ctx) -> HTMLElement })               // terminals, output
// the file manager emits, on document:
//   ws:select {paths, cwd}     ws:open {path}     ws:cwd {path}
// and listens for:
//   ws:reveal {path}           ws:fs-changed {dir}
```

The file manager asks the registry for the "⋯" and context menus (`registry.actions(entry)`) and for what to do on
open (`registry.opener(entry)`), instead of knowing about Submit. That is how "Submit job..." appears for
`.py`, `.sh` and `.slurm` files, and "Prepare environment..." for `pyproject.toml`, `pixi.toml`, `uv.lock` and
`.venv`, without the file manager changing.

**House style.** Follow the rest of hpclib: a module docstring that lists the routes or elements, standard library
only on the server, native custom elements and plain ES modules on the page, no build step, CSS variables from
`default-styles.css` and nothing hard-coded, comments that explain why, `unittest` with optional Playwright.

### 2.2 Shared UI: `hpclib/ui/`

`agent-console/components.js` already holds the components the Workspace needs, but it also holds console-only
ones (`hpc-cluster-row`, `hpc-app-session`, `connectClients`) that depend on the cluster API. Split it:

```text
hpclib/ui/                   inside the hpclib package, so it is in package_data and ships to clusters
  index.js                   re-exports everything below
  core.js                    el(), action(), shellJoin, formatBytes, ask(), toast(), confirmDialog()
  base.css                   control styles extracted from agent-console/style.css (buttons, inputs, tables)
  default-styles.css         moved from agent-console (the variables); the console keeps serving it
  theme.js                   moved from agent-console
  components/*.js            one file per element
  libs.js, libs.json         the third-party loader and its pinned manifest (2.3)
agent-console/components.js  keeps the console-only elements and re-exports hpclib/ui
```

| Element | Status | Notes |
| --- | --- | --- |
| `hpc-panel`, `hpc-dialog`, `hpc-state`, `hpc-field`, `hpc-action-button`, `hpc-log`, `hpc-env-editor` | move | `hpc-state` gains Slurm words (pending, completing, completed, timeout, out_of_memory, node_fail, cancelled). `hpc-env-editor` becomes the "environment variables" control in Submit. |
| `hpc-tabs`, `hpc-tab` | new | ARIA tablist, roving tabindex, closable, dirty marker, overflow scroll. The console's sub-tabs (`subTabs` in `cluster.js`) can use it. |
| `hpc-split` | new | Pointer and arrow-key resizing, minimum sizes, `persist="key"`. |
| `hpc-menu` | new | Native `popover`, array or child-button items, keyboard navigation, context-menu mode. |
| `hpc-toast` | new | One live region; `toast(text, {tone})`. |
| `hpc-code` | new | Editor wrapper: `value`, `language`, `readonly`, `diff-against`; events `change`, `save`. **Light DOM**, because CodeMirror's styles do not cross a shadow boundary. |
| `hpc-terminal` | new | xterm wrapper: `connect` callback returning a WebSocket; reattach on reload; light DOM for the same reason. |
| `hpc-resources` | new | Typed Slurm resource form (4.3). The console's app settings (job time, memory, CPUs, partition) can reuse it. |
| `hpc-usage-table` | extract | From `cluster.js`'s `storageCard` and the Workspace's usage tab, which duplicate each other. |
| `hpc-cluster-picker`, `hpc-cluster-row`, `hpc-app-session`, `connectClients`, `ownerRecovery` | stay | Need the console's API. |

**Getting it into the Workspace.** The page is served from the login node by `workspace_server.py`, on another
origin from the console, so it cannot import the console's files. The Workspace package must carry its own copy:
add a `bundle` entry to `hpclib-package.json` (`{"tunnels/workspace/ui/hpclib-ui": "hpclib/ui"}`) that
`console_packages.py build` resolves against the repo root. The page imports through an import map, so moving the
shared stack to a CDN later (publishing `hpclib/ui` as an npm package) is a one-line change:

```html
<script type="importmap">{"imports": {"hpclib-ui/": "./hpclib-ui/"}}</script>
<script type="module" src="app.js"></script>
```

**Why not read it from the cluster's hpclib?** `install_hpclib` packs the `hpclib/` directory only
(`_hpclib_pack`), so `hpclib/ui/` would ship, but the cluster's hpclib can be any version. A package that carries
its copy cannot be broken by skew.

**Theme bridge.** The console's dark switch is stored in the console's origin, which the Workspace cannot read.
The console's Open link adds `#theme=dark|light|system` (the Workspace stores it) and, optionally,
`console=http://127.0.0.1:PORT`, from which the page adds
`<link rel="stylesheet" href="PORT/user.css">`: the console serves `/user.css` without a key, and cross-origin
stylesheets need no CORS, so Interface Settings changes carry over. A failed load is harmless. The Workspace's own
CSS then contains layout only and uses hpclib's variable names (`--good`, `--bad`, `--radius-control`,
`--font-mono`), not its own.

### 2.3 Third-party libraries

Versions are what `npm view` reported on 2026-10-29.

| Need | Choice | Notes |
| --- | --- | --- |
| Terminal | `@xterm/xterm` 6.0.0; addons `fit` 0.11.0, `web-links` 0.12.0, `search` 0.16.0 | No alternative worth considering. The 6.0.0 tarball keeps `lib/xterm.js` and `css/xterm.css`, so the current loader paths still work; test before bumping from 5.5.0 (renderer and API changes). |
| Editor | CodeMirror 6 (`codemirror` 6.0.2, `@codemirror/lang-python` 6.2.1, `@codemirror/legacy-modes` 6.5.5 for shell, TOML, Fortran, Julia and R, `@codemirror/search` 6.7.2) | CodeMirror 5 is in maintenance mode and has accessibility and large-file limits CodeMirror 6 does not. CodeMirror 6 has no official no-build bundle, so how it is delivered needs a short spike (see below). Fallback: CodeMirror 5.65.21 behind the same `<hpc-code>`. |
| Script diff | `@codemirror/merge` 6.12.3 | Shows "generated" against "edited by hand" in Submit. Free if CodeMirror 6 is chosen. |
| PDF, images, video | The browser | No pdf.js: 6.x is heavy and browsers already have viewers. |
| Tabs, split, menu, toast, dialog | Shared custom elements | About 400 lines in total; none of these has a CDN library that is smaller than the glue to adapt it. |
| Bootstrap 5.3.8, jQuery 4.0.0 | Not used | hpclib's UI is variables plus custom elements, with a user-editable theme (Interface Settings). Bootstrap's own variables and resets would compete with that; jQuery adds nothing over `el()`. |
| Icons | Inline SVG sprite (Lucide, ISC licence) in `hpclib/ui/icons.svg` | No icon-font CDN. The file manager can use it too. |
| Markdown preview | Later: `marked` plus DOMPurify inside a sandboxed viewer | Not v1. |

**CodeMirror 6 spike (one day).** It passes if one pinned URL set loads without duplicate-instance errors and
covers: Python, shell, YAML, TOML, JSON, Markdown, C/C++, Fortran, Julia, R; save on Ctrl/⌘-S; in-file search;
a 50 MB file; colours from the CSS variables (dark and light); and `@codemirror/merge`. Candidates for delivery:
jsDelivr's `+esm` endpoints, esm.sh with pinned versions, or a single bundle built once with esbuild and committed
under `hpclib/ui/vendor/`. If none passes, ship CodeMirror 5.65.21.

**Loader and manifest.** `hpclib/ui/libs.json` lists each library with an exact version, its files and their
sha384 hashes. `libs.js` loads them lazily (first terminal, first editor) from `WORKSPACE_CDN` (default jsDelivr),
or from `ui/vendor/` when that exists. Classic scripts (xterm, CodeMirror 5) get `integrity` attributes;
for ES modules, integrity through import maps is recent and uneven across browsers, so pin exact versions and verify
hashes at build time instead. `console_packages.py build --vendor` fills `ui/vendor/` for sites whose browsers
cannot reach a CDN. A missing library degrades as today: a plain textarea, and a terminal that says why it cannot
open.

## 3. Layout

Three regions and a dock. The file manager fills the left column, the work area holds tabs, and the dock holds
things that run (terminals, followed output, environment preparation).

```text
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ ☰ Workspace   maboyer@grace · /home/maboyer/encoding        quota 71%     [ Submit… ]   ◐    │ topbar
├────────────────────────┬─────────────────────────────────────────────────────────────────────┤
│ FILES (file manager)   │ [save_zarr.py ●] [Submit: save_zarr.py ✕] [train-4242.out ⟳ ✕]      │ tabs
│  breadcrumb            │ ┌─────────────────────────────────────────────────────────────────┐ │
│  list                  │ │ editor | viewer | submit | usage | log follower                 │ │
│                        │ │                                                                 │ │
│                        │ └─────────────────────────────────────────────────────────────────┘ │
│ RUNS FROM HERE         ├─────────────────────────────────────────────────────────────────────┤
│ ● 4242 save_zarr 1:12  │ DOCK  [Terminal 1] [Terminal 2] [Output: save_zarr-4242.out] [Prepare…]│
│ ◌ 4251 sweep  pending  │ $ _                                                                 │
└────────────────────────┴─────────────────────────────────────────────────────────────────────┘
```

- **Top bar** (shared look with the console): menu, host and current directory, quota chip (from `lib/quota.sh`),
  **Submit…** (enabled whenever a runnable file is selected or open: the main action gets the main button), theme.
- **Left column**: the file manager above, **Runs from here** below (at most 20 jobs this Workspace submitted, kept in
  `~/.local/tunnels/workspace/runs/`). Each shows state, elapsed/limit, and Open output, Cancel, Submit again.
  State comes from one `squeue -j` plus `sacct -j` call per refresh, only while a run is unfinished and the page is
  visible (5 s, backing off to 30 s). There is no queue browser; "All jobs" links to the console when it passed
  its address in `console=`.
- **Work area**: tabs of `<hpc-tabs>`; each tab is an opener's view (editor, viewer, Submit, usage, log follower).
- **Dock**: `<hpc-tabs>` of terminals and of output followers. Hidden by default, remembered once shown.
- **Narrow screens**: one column with a bottom bar (Files, Work, Terminal, Runs); the splitters disappear.
- **Persistence**: split sizes, dock height, open tabs and the active one, and unsaved editor drafts go to
  `localStorage` under a key that includes `info.host` and the user (the origin is per tunnel port, and ports are
  reused). Terminals reattach by id (section 6).
- **Keyboard**: tabs and the dock are `<hpc-tabs>` (arrows, Home/End, Delete closes); splitters move with arrows;
  Ctrl/⌘-S saves; the file manager supplies list navigation.
- **Removed**: the "My jobs" pane and its `/api/jobs` polling.

## 4. Submitting a job

### 4.1 Principles

1. The form is a view of a **spec**; the script is generated from the spec. Nobody writes `#SBATCH` lines.
2. The **script is the source of truth on disk**. It embeds its spec, so a `.slurm` file reopens as the same form.
3. Anything the cluster can tell us, it tells us (partitions, limits, GPU types, accounts, modules, quota), and the
   form validates against it before `sbatch` does.
4. **Check before submit** with `sbatch --test-only`, which validates the script and estimates the start without
   submitting.
5. Environments follow hpclib's rule: jobs activate, they never resolve or download (section 5).
6. After submitting, the next click should be "follow the output".

### 4.2 The spec and the generated script

```json
{
  "v": 1,
  "name": "save_zarr",
  "workdir": "/home/maboyer/encoding",
  "command": {"kind": "python", "file": "save_zarr.py", "args": ""},
  "env": {"kind": "uv", "project": "/home/maboyer/encoding"},
  "resources": {"time": "6:00:00", "nodes": 1, "ntasks_per_node": 1, "cpus_per_task": null,
                "mem": "8G", "gres": "", "partition": "", "account": "", "constraint": "", "array": ""},
  "modules": [],
  "shell": {"init": "bashrc"},
  "extra_sbatch": [],
  "output": "%x-%j.out"
}
```

The generator is a pure function `render(spec, cluster) -> script`, with these rules:

- Every substituted value is shell-quoted (`shlex.quote`). The Arguments field is split with `shlex.split` and each
  word quoted again, so quotes behave as typed and unbalanced quotes are flagged in the form.
- `extra_sbatch` lines must begin with `#SBATCH `; nothing else is accepted.
- `. "…/hpclib.sh"` is guarded, with no-op `slurm_job_info` and `slurm_job_footer` when it is missing.
- **Shell init** is a choice: `~/.bashrc` (today's behaviour, with a warning when the file returns early for
  non-interactive shells), a login shell (`#!/bin/bash -l`), or none.
- The command runs in the background with a trap, so the warning signal reaches it:

```bash
#!/bin/bash
# hpclib-workspace v1: generated; "Open in Submit" re-reads the next line
# spec: {"v":1,"name":"save_zarr", ...}
#SBATCH --export=NONE
#SBATCH --job-name=save_zarr
#SBATCH --time=6:00:00
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --mem=8G
#SBATCH --output=%x-%j.out
#SBATCH --signal=B:TERM@60

curdir="$PWD"; . ~/.bashrc; cd "$curdir"
[ -f /home/maboyer/hpclib/hpclib.sh ] && . /home/maboyer/hpclib/hpclib.sh
type slurm_job_info > /dev/null 2>&1 || { slurm_job_info() { :; }; slurm_job_footer() { :; }; }
SLURM_JOB_START_TIME=$(date +%s)
slurm_job_info

# environment: uv project /home/maboyer/encoding (prepared 2026-10-27, Python 3.12.4)
export VIRTUAL_ENV=/home/maboyer/encoding/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"; unset PYTHONHOME
[ -x "$VIRTUAL_ENV/bin/python" ] || { echo "environment missing: prepare it in the Workspace" >&2; exit 4; }

child=0
forward() { [ "$child" != 0 ] && kill -TERM "$child" 2> /dev/null; }
trap forward TERM INT
cd /home/maboyer/encoding
python -u save_zarr.py &
child=$!
wait "$child"; status=$?
while kill -0 "$child" 2> /dev/null; do wait "$child"; status=$?; done   # wait returns early when a trap runs
trap - TERM INT
slurm_job_footer
exit "$status"
```

The activation block is the same one `rest_envs.EnvironmentManager.prelude()` writes for REST jobs, so a job
behaves the same however it was submitted.

**Round trip.** Opening a `.slurm` file that has the marker offers "Open in Submit". Submit regenerates the script
from the embedded spec and compares: if the file differs, the form says **edited by hand** and shows a diff (the
generated text against the file), with *Keep my edits* and *Regenerate*. No event tracking: the comparison is the
rule. Replacing an existing script that has the marker needs no checkbox (the diff is shown); a file without the
marker is never overwritten, and the default name moves to `name-2.slurm`.

### 4.3 The Submit tab

```text
┌ Submit  save_zarr.py ────────────────────────────────────────────────────────────────────────┐
│ 1  RUN        python  save_zarr.py   Arguments [ ____________________ ]   in /home/maboyer/encoding │
│ 2  ENVIRONMENT                                                                              │
│    (●) uv project  /home/maboyer/encoding   ✔ ready · Python 3.12.4 · 142 packages · 2 d ago   [Prepare again] │
│    ( ) pixi   ( ) modules only   ( ) venv or conda   ( ) none                               │
│    ⚠ uv's cache is in your home directory (quota)                         [Use scratch…]    │
│ 3  RESOURCES   profile [ GPU · A100 · 1 day ▾ ] [Save as…]                                  │
│    Time [ 6h ]   Memory [ 8 GB ▾ per node ]   CPUs [ 4 ]   GPUs [ none ▾ ]                   │
│    Partition [ Auto ▾ ]   Account [ ▾ ]                                                     │
│    ▸ More: nodes, tasks per node, constraint, array, dependency, mail, environment variables, extra #SBATCH │
│ 4  SCRIPT     [ generated ▾ ]   read-only; [Edit by hand…]                                  │
│ ┌──────────────────────────────────────────────────────────────────────────────────────────┐ │
│ │ #!/bin/bash …                                                                            │ │
│ └──────────────────────────────────────────────────────────────────────────────────────────┘ │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ ✔ valid · would start about now on gpu (sbatch --test-only)    [Check]  [Submit]  [Submit and follow] │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

At 1200 px or wider the script sits to the right of the form instead of below it. The action bar is sticky.

`<hpc-resources>` fields are typed and use what the cluster reports:

| Field | Behaviour |
| --- | --- |
| Time | Accepts `6h`, `1d 2h`, `90m`, `6:00:00`; writes Slurm's `D-HH:MM:SS`; warns above the partition's limit. |
| Memory | Number plus unit, per node or per CPU (`--mem` or `--mem-per-cpu`); warns above the node's memory. |
| GPUs | Type (from the `gres` values `sinfo` reports) and count, written as `--gres=gpu:TYPE:N`; hides partitions with none. |
| Partition | "Auto" or one of the partitions, showing availability and time limit. |
| Account | From `sacctmgr show assoc` for the user; remembers the last choice. |
| Modules | Tokens with autocomplete from `module -t avail` (cached 10 minutes, as `ModuleSystem` does in `rest_jobs.py`). |
| Environment variables | `<hpc-env-editor>`. |

**Check** runs `sbatch --test-only` on the generated script and reports either Slurm's start estimate or its error,
mapped back to the field when it can be. It also reports what Submit would do first (for example, "environment
needs preparing").

**Profiles** are named resource sets, per cluster: the user's own (kept in `~/.local/tunnels/workspace/profiles.json`)
plus "Last used". Nothing is pre-populated with site guesses.

### 4.4 After Submit

Submit writes the script, runs `sbatch`, and records a run: job id, spec, script path, environment path,
output path. The result card offers **Follow output** (opens the log follower in the dock, which waits for the file
to appear), **Cancel**, **Open script** and **Submit again**. **Submit and follow** does the first automatically.
The run appears in Runs from here.

### 4.5 Recipes and classic templates

The guided flow covers Python and bash scripts and arbitrary commands. For anything else (ORCA, Gaussian, a site's
own wrapper) a **recipe** supplies only the command part, with `#@ field` lines for its extra inputs; resources,
environment, shell setup and footer still come from the form. Existing templates (full scripts with `#SBATCH`
lines) keep working unchanged under **Custom templates (classic)**, with the old form, because users have written
their own. Typed fields are added to the header backwards-compatibly: `#@ field time [duration]: Time limit | 6:00:00`.

### 4.6 Later

Arrays and parameter sweeps (a list of argument lines becomes `--array=1-N%M` and a `sed -n` selection), job
dependencies ("after run 4242"), and an interactive mode (`srun --pty` with the same resources form, opened as a
terminal). They fit the spec without changing it (`array`, `dependency`, `mode`).

## 5. Environments

Environment management is where a job submitter succeeds or fails. hpclib already has most of the model; the
Workspace should adopt it rather than invent a second one.

### 5.1 What hpclib already has

| Piece | Where | What it gives |
| --- | --- | --- |
| Detection | `rest_envs.EnvironmentManager.detect()` | uv or pixi from `pyproject.toml`, `uv.lock`, `pixi.toml`; whether `.venv` or `.pixi/envs/NAME` exists; `ready`, `problem`, `busy`, `squashed`, `core`. |
| The record | `.venv.hpclib.json` (pixi: `.pixi-NAME.hpclib.json`) | What the last sync left: `synced` (epoch), interpreter, package list, core, whether squashed, `failed`. |
| The busy marker | `.venv.hpclib-syncing` | An operation is working on it; jobs are not submitted then. |
| Activation | `EnvironmentManager.prelude()` | The bash lines a job runs to use the environment, with a clear failure if it is missing. |
| Sync, squash, layers | `sync`, `squash`, `unsquash`, `core=` | `uv sync --locked` or `pixi install`; one SquashFS file per environment to save inodes; environments layered on a frozen core. |
| Tools and directories | `lib/pytools.sh` (`hpclib_pytools --json`) | Where uv and pixi are; `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `PIXI_CACHE_DIR` per cluster (sourcing `hpclib.sh` exports the saved ones); `hpclib_pytools install uv|pixi`. |
| Modules, quota | `ModuleSystem`, `lib/quota.sh` | `module avail` and `spider` cached; storage and allocation checks. |

The rule underneath all of it: **jobs only activate environments; they never resolve or download**, so compute nodes
need no network, and syncing is an explicit step on a node that has it (the server's `environments.modules`, such
as `WebProxy`, say how).

### 5.2 What goes wrong without it

Template jobs run `uv run`, which syncs on first use: no internet on a compute node means a failed job, and an array
means several tasks racing on one `.venv`. Nothing says whether an environment is current, so a changed
`pyproject.toml` or lock is silently ignored or silently resolved. Caches and managed Pythons default to the home
directory, where quotas are small and file counts are the real limit. And an environment prepared by an agent is
invisible to the Workspace, and the reverse.

### 5.3 Model for the Workspace

An **environment** has a kind, a location, a status and a record.

| Kind | Detected from | Prepare | Job activation |
| --- | --- | --- | --- |
| uv project | `pyproject.toml` (and `uv.lock`) at or above the script | `uv sync --locked` (`--upgrade` on request) | `prelude()` |
| uv script | a `# /// script` block (PEP 723) in the file | warm the cache with the resolver, on a node with network | `uv run --offline --script` |
| pixi project | `pixi.toml`, or `[tool.pixi]` in `pyproject.toml` | `pixi install --locked` | `prelude()` |
| venv or conda | a chosen path or name | none (user-managed) | `source …/bin/activate`, `conda activate` |
| modules only | modules chosen in the form | none | `module load …` |
| none | | | the cluster's `python3` |

**Status** (one word and a reason, from `detect()` plus the record):

| Status | Meaning | Action |
| --- | --- | --- |
| ready | environment exists, record not failed, manifest and lock not newer than `synced` | Submit enabled |
| not prepared | manifest found, no environment | **Prepare** |
| stale | `pyproject.toml` or the lock changed after `synced` (the file is named) | **Prepare again** |
| busy | the marker file exists and its process is alive | wait; follow its log |
| failed | the record says the last sync failed | **Prepare again**, with the log |
| tool missing | no uv or pixi where the Workspace runs | **Install uv** (`hpclib_pytools install uv`) |
| none found | no manifest above the script | pick another kind; offer `uv init` |

Submit is blocked on *not prepared*, *stale*, *busy* and *failed* unless the user chooses **Let the job sync**
(`uv run` in the job, for sites with network on compute nodes); that choice is remembered per cluster.

**Where Prepare runs.** Default: on the login node, as a child of the Workspace server, because the Workspace
already runs there as the user and that node usually has network. Output streams into a dock tab. For heavy work
(large pixi solves, squashing) the form offers **Prepare as a job**, using the same Submit machinery with the
server's `environments.modules` loaded. The marker, record and sync log use the REST server's formats, so the agent
API and the Workspace see the same state and refuse the same conflicts. Before Prepare, check `runs/` and `squeue`
for queued or running jobs that use this environment, and warn (the REST server refuses outright without `force`).

**Directories.** The environment card reads `hpclib_pytools --json`. When the cache or managed-Python directory
resolves to home (`source: default`), it warns and offers **Use scratch…**, which runs `hpclib_pytools set` with a
scratch path. Home quotas and file counts are the usual way a first sync fails.

### 5.4 Reproducibility

Every run record stores the spec, the environment path, the record's `synced` time and package count, the script's
hash and, when the directory is a git checkout, `git rev-parse HEAD` and whether it is dirty. Submit again can warn
"the environment was prepared again since this run".

### 5.5 Shared code: one environment module

`rest_envs.py` mixes pure logic (detection, record and marker reading, activation lines, `side_path`) with the
sandboxed sync. The Workspace needs the pure part and a way to run `uv sync` without the agents' sandbox. Extract:

```text
hpclib/lib/envinfo.py     detect(), read_record(), marker(), prelude(), job_images(), side_path()
hpclib/lib/clusterinfo.py sinfo and sacctmgr parsing (from rest_jobs.py's ClusterInfo), modules, quota wrapper
rest_envs.py              imports envinfo; keeps the sandboxed sync, squash and layers
Workspace ws/envs.py      imports envinfo; runs the same commands unsandboxed, as the user
```

Both new modules must stay **Python 3.6-safe and standard library only** (the Workspace's floor): `rest_envs.py`
and `rest_manifests.py` parse as 3.6 syntax, but `_ask` uses `subprocess.run(capture_output=True, text=True)`
(3.7). Either use `stdout=PIPE, universal_newlines=True` in the extracted code, or raise the Workspace's floor to 3.8
(see the decisions).

### 5.6 The squash question (decide before building it)

hpclib has two conventions for packing an environment into SquashFS, and they disagree:

- `rest_envs.py`: the image sits beside the project (`.venv.sqfs`), the environment directory is emptied, and jobs
  mount the image back on that directory with Singularity or Apptainer (`--bind IMAGE:DIR:image-src=/`), inside the
  agents' sandbox runtime.
- `lib/uvsquash.sh`: the image can be anywhere and is mounted at `$HPCLIB_UVSQUASH_ROOT/NAME` with `squashfuse`, or
  bound in a container. It is not sourced by `hpclib.sh` and nothing else in the repository references it.

The Workspace runs jobs without the sandbox, so a squashed environment there needs either `squashfuse` on compute
nodes or an Apptainer wrapper in the script. Recommendation: v1 shows squashed environments and refuses to submit
against one it cannot mount; Prepare offers **Pack into one file** only after the two conventions are unified on
`rest_envs.py`'s record, and `uvsquash.sh` is either removed or turned into a thin command over `envinfo.py`.

## 6. Server changes

| Route | Purpose |
| --- | --- |
| `POST /api/term` `GET /api/term` `DELETE /api/term/ID` | Create, list and end detached terminal sessions. |
| `POST /api/term/ticket` | Single-use ticket (10 s) for the WebSocket `/terminal?id=&ticket=`. |
| `POST /api/raw-url` | Signed, expiring file URL for the viewer (1.1). |
| `GET /api/cluster` | Partitions, GPU types, limits, accounts, modules, quota, the Slurm version; cached 60 s. |
| `GET /api/envs?path=` | Candidates above a path with status (5.3), tools and directories. |
| `POST /api/envs/prepare` `GET /api/envs/ops/ID` | Start Prepare; poll or follow its log. |
| `POST /api/submit/check` | `{spec}` gives the script, problems, warnings and the `--test-only` result. |
| `POST /api/submit` | `{spec}` or `{script}` writes (by the overwrite rules in 4.2) and runs `sbatch`; returns the run. |
| `GET /api/runs` `POST /api/runs/ID/cancel` | The list for Runs from here; `scancel`. |
| `GET /api/fs/tail` | Bytes since an offset, for following a log (the file manager's). |
| removed | `/api/jobs` and `/api/cancel` (the queue browser), `/api/render` and the template routes once classic templates are served from `ws/recipes.py`. |

**Terminals.** A session owns the pty and a one-megabyte ring buffer; a WebSocket attaches, receives the buffer,
then live output, and detaching does not signal the shell. Sessions end when the shell exits, on explicit delete, or
after a configurable idle period with nobody attached (default eight hours). The page keeps the ids in
`sessionStorage`, so a reload or a reconnect after a tunnel drop reattaches. The server sends a WebSocket ping
every 20 seconds, which also keeps the SSH forward alive. Restarting the Workspace ends sessions; wrapping the shell
in `tmux` when it exists is a later option.

**Login environment.** At start (and on demand) run `bash -lc 'env -0'` once, keep the result, and run `sbatch`,
`sinfo`, `squeue`, `sacctmgr` directly with it. Only `module` queries need a login shell (it is a shell function),
and those are cached.

## 7. Delivery plan

| Phase | Contents | Done when |
| --- | --- | --- |
| 0 | Section 1.1 authentication changes and their regression test | The test passes; the existing Workspace tests still pass |
| 1 | `hpclib/ui/` extracted; `<hpc-tabs>`, `<hpc-split>`, `<hpc-menu>`, `<hpc-toast>`; the shell with the registry; package `bundle`; theme bridge; file manager slots in | The Workspace follows the console's theme; no hand-rolled tab, menu, splitter or toast remains |
| 2 | `ws/` split; detached terminals; login environment; `fs/tail` and the log follower; Runs from here; My jobs removed; layout persistence | A reload keeps tabs, drafts and terminals; a tunnel drop and reconnect reattaches |
| 3 | Spec, generator and Check; `<hpc-resources>` with `/api/cluster`; the environment card with detect, status, Prepare (uv and pixi), directories; classic templates preserved | A script in a uv project goes from file to followed output with no text field edited; the generated script traps TERM; a stale lock blocks Submit |
| 4 | `.slurm` round trip with diff; profiles; arrays; dependencies; interactive mode; squash (after 5.6); CodeMirror 6 if not done in phase 1 | |

Tests: extend `tests/test_workspace_package.py` and `tests/test_workspace_ui.py`; put fake `sbatch`, `sinfo`,
`sacctmgr` and `squeue` on `PATH` (see `tests/process_queue_tests/slurm_test_helpers.sh`) so Submit, Check and Runs
run without a cluster. The generator is pure, so test it with golden scripts, and run each golden script under
`bash -n` and `shellcheck` when available.

Coordination: `docs/interface/apps.md` and `hpclib/servers/agent_console.py` have uncommitted changes in the working
tree right now; `docs/interface/apps.md`'s Workspace section and `packages/workspace/README.md` need updating with
phases 1 to 3.

## 8. Decisions (2026-10-29)

1. **"My jobs" is removed**, replaced by Runs from here (phase 2).
2. **`hpclib/ui/` with a build-time copy** into the package.
3. **CodeMirror 6**, after the one-day spike in 2.3 (fall back to 5.65.21 only if it fails).
4. **The Workspace stays at Python 3.6**; the shared modules avoid 3.7+ calls. `test_tunnel_scripts_parse_and_python36`
   guards the server files; extend it to the new ones.
5. **Squash is deferred** until there is usage data. `lib/uvsquash.sh` has been removed, so the only convention left is
   `rest_envs.py`'s; 5.6's unification question is closed for now.
6. **Prepare runs on the login node** by default.
7. **Authentication first**: phase 0 is done (below).

## 9. Phase 0 status: done (Workspace 1.0.2)

Section 1.1's changes are in `workspace_server.py` and `ui/workspace.js`: the token is accepted only in
`X-Workspace-Token`; the terminal opens with a single-use ticket (`POST /api/term/ticket`); the viewer's files come
from signed, expiring, per-directory URLs (`POST /api/raw-url`, `/raw/EXPIRES.DEPTH.SIGNATURE/PATH`, one hour);
state-changing routes require `application/json`; requests naming another `Origin` (including `null`) or marked
cross-site are refused; the HTML viewer's sandbox lost `allow-forms` and `allow-modals`. New tests are in
`TestAuthentication` and `TestTerminal` (`tests/test_workspace_package.py`), and `tests/test_workspace_ui.py` runs
the real viewer and terminal through the new flows. The package version is 1.0.2.

Two small behaviours to know: a viewer file link covers its own directory only, so an HTML page that reaches for
`../` resources gets a 401 (the signed prefix can be widened later if that matters); and clicking a file twice
quickly can open two viewer tabs, because the signed URL is fetched before the tab is added (phase 1's shell
should de-duplicate opens).
