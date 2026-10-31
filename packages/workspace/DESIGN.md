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
| Third-party libraries | xterm.js 6 (+ fit and web-links addons; no search addon yet), Wunderbaum from a CDN, pinned, with integrity hashes; CodeMirror 6 as a bundle that ships in `hpclib/ui/vendor/` (section 16). PDFs use the browser's viewer. | 2.3 |
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
| Pins were behind (fixed in phase 1; CodeMirror 5 is gone since 1.6.0) | CodeMirror 5.65.18, xterm 5.5.0, fit 0.10.0 | Now: CodeMirror 6 bundle, xterm 6.0.0, fit 0.11.0 | Section 2.3 |
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
| `hpc-code` | new | Editor wrapper: `value`, `language`, `readonly`; events `change`, `save`. The diff is a separate `<hpc-diff>` (`show(original, modified)`), in the same file. **Light DOM**, because CodeMirror's styles do not cross a shadow boundary. |
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
| Terminal | `@xterm/xterm` 6.0.0; addons `fit` 0.11.0, `web-links` 0.12.0 (the `search` 0.16.0 addon was planned and is not added) | No alternative worth considering. The 6.0.0 tarball keeps `lib/xterm.js` and `css/xterm.css`, so the current loader paths still work; test before bumping from 5.5.0 (renderer and API changes). |
| Editor (done, 1.6.0) | CodeMirror 6 as built: `@codemirror/view` 6.43.15, `state` 6.7.6, `language` 6.13.1, `commands` 6.11.1, `search` 6.7.2, `lang-python` 6.2.1, `legacy-modes` 6.5.5 for the other languages (no Markdown yet), `theme-one-dark` 6.1.3 | CodeMirror 5 is in maintenance mode and has accessibility and large-file limits CodeMirror 6 does not. CodeMirror 6 has no official no-build bundle, so how it is delivered needs a short spike (see below). The spike's answer is section 16: a committed esbuild bundle. |
| Script diff (done, 1.6.0) | `@codemirror/merge` 6.12.3 | The unified merge view in `<hpc-diff>` shows what the form's version would change in an opened `.slurm`. |
| PDF, images, video | The browser | No pdf.js: 6.x is heavy and browsers already have viewers. |
| Tabs, split, menu, toast, dialog | Shared custom elements | About 400 lines in total; none of these has a CDN library that is smaller than the glue to adapt it. |
| Bootstrap 5.3.8, jQuery 4.0.0 | Not used | hpclib's UI is variables plus custom elements, with a user-editable theme (Interface Settings). Bootstrap's own variables and resets would compete with that; jQuery adds nothing over `el()`. |
| Icons | Planned: inline SVG sprite (Lucide, ISC licence) in `hpclib/ui/icons.svg` | Not built: the page uses text labels and Wunderbaum's own icons. No icon-font CDN either way. |
| Markdown preview | Later: `marked` plus DOMPurify inside a sandboxed viewer | Not v1. |

**CodeMirror 6 spike (done: section 16; the committed bundle was the candidate that passed).** It had to pass if one pinned URL set loads without duplicate-instance errors and
covers: Python, shell, YAML, TOML, JSON, Markdown, C/C++, Fortran, Julia, R; save on Ctrl/⌘-S; in-file search;
a 50 MB file; colours from the CSS variables (dark and light); and `@codemirror/merge`. Candidates for delivery:
jsDelivr's `+esm` endpoints, esm.sh with pinned versions, or a single bundle built once with esbuild and committed
under `hpclib/ui/vendor/`. If none passes, ship CodeMirror 5.65.21.

**Loader and manifest.** `hpclib/ui/libs.json` lists each library with an exact version, its files and their
sha384 hashes. `libs.js` loads them lazily (first terminal, first editor) from `WORKSPACE_CDN` (default jsDelivr),
or from `ui/vendor/` when that exists. Classic scripts (xterm, CodeMirror 5) get `integrity` attributes;
for ES modules, integrity through import maps is recent and uneven across browsers, so pin exact versions and verify
hashes at build time instead. The `--vendor` build option was planned and not built; `libs.js` can read a vendor directory laid out like the CDN
(`configureLibs({vendor})`), but nothing fills one yet. A missing library degrades as today: a plain textarea, and a terminal that says why it cannot
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
| 0 (done: section 9) | Section 1.1 authentication changes and their regression test | The test passes; the existing Workspace tests still pass |
| 1 (done: section 10) | `hpclib/ui/` extracted; `<hpc-tabs>`, `<hpc-split>`, `<hpc-menu>`, `<hpc-toast>`; the shell with the registry; package `bundle`; theme bridge; file manager slots in | The Workspace follows the console's theme; no hand-rolled tab, menu, splitter or toast remains |
| 2 (done: section 13) | `ws/` split; detached terminals; login environment; `fs/tail` and the log follower; Runs from here; My jobs removed; layout persistence | A reload keeps tabs, drafts and terminals; a tunnel drop and reconnect reattaches |
| 3 (done: section 11) | Spec, generator and Check; `<hpc-resources>` with `/api/cluster`; the environment card with detect, status, Prepare (uv and pixi), directories; classic templates preserved | A script in a uv project goes from file to followed output with no text field edited; the generated script traps TERM; a stale lock blocks Submit |
| 4 (done except squash: sections 14, 15, 16) | `.slurm` round trip with diff; profiles; arrays; dependencies; interactive mode; CodeMirror 6 (4a: arrays, sweeps, dependencies, profiles, 1.4.0; 4b: round trip and interactive, 1.5.0; 4c: CodeMirror 6 and the merge view, 1.6.0). Squash stays deferred (decision 5) | Each item has its tests; squash waits on 5.6 |

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

## 10. Phase 1 status: done (Workspace 1.1.0)

What landed:

- **`hpclib/ui/`** holds the shared elements and helpers (`index.js`, `core.js`, `libs.js` with `libs.json` and
  `update_libs.py`, `base.css`, `components/*.js`). The console imports them (`agent-console/components.js`
  re-exports `./hpclib-ui/index.js` and keeps its own elements; `style.css` imports `base.css`), served at
  `/hpclib-ui/` by `agent_console`. New elements: `<hpc-tabs>`, `<hpc-split>`, `<hpc-menu>` (`showMenu`), `<hpc-toasts>`
  (`toast`), `<hpc-code>`, `<hpc-terminal>`, and `promptDialog` beside `confirmDialog`.
- **`bundle`** in `hpclib-package.json`: `console_packages.py build` copies `hpclib/ui` (without `.py`, `.pyc` and
  `.md`), `default-styles.css` and `theme.js` into `ui/hpclib-ui/` of the zip. The checks refuse unsafe paths,
  missing sources and collisions; the output is deterministic.
- **The shell** (`ui/index.html`, `app.js`, `shell.js`, `client.js`, `features/`): two `<hpc-split>`s (side, dock;
  plus the jobs list under the files), two `<hpc-tabs>`, a registry (`registerOpener`, `registerAction`,
  `registerDock`) and the page events of 2.1 (`ws:cwd`, `ws:open`, `ws:select`, `ws:reveal`, `ws:fs-changed`,
  `ws:job-submitted`). Opening the same file twice shows one tab, even while the first is loading. Tabs that can be
  restored come back after a reload. `features/files.js` is the stand-in file manager; its header is the contract a
  replacement keeps. `features/jobs.js` ("My jobs") stays until phase 2 and nothing depends on it.
- **Theme bridge**: the console's Open link adds `#theme=…&console=http://127.0.0.1:PORT` for apps with
  `"theme_link": true` (a new manifest key; `appUrl()` in `agent-console/components.js`). `ui/bridge.js` sets
  `data-theme`, keeps both for the next visit and loads that console's `/user.css`; only a loopback address is
  accepted. `workspace.css` is layout only: it has no colour of its own and uses hpclib's variable names.
- **Libraries**: xterm.js 6.0.0 with fit 0.11.0 and web-links 0.12.0, CodeMirror 5.65.21, pinned in `libs.json`
  with sha384 hashes (checked on the default CDN and on a vendor directory, left off for another mirror).
  CodeMirror 6 followed in phase 4c (section 16), and CodeMirror 5 is gone.

Found while testing: a custom element's constructor must not set attributes (`document.createElement` refuses the
element), which broke `<hpc-menu>` and `<hpc-toasts>`; and an automatic popover closes on the mouse-up of the
right-click that opened it on Linux and macOS, so `<hpc-menu>` is a manual popover that closes itself. A test now
creates every shared element with `createElement`.

Tests: `TestShellStructure` and `TestBundle` in `tests/test_workspace_package.py`; the page tests in
`tests/test_workspace_ui.py` run against the built package (theme bridge, de-duplicated opens, keyboard tabs and
splitters, the menu, toasts); `tests/test_app_url.mjs`.

Open from this phase when it closed, and closed since: unsaved editor drafts and the dock's terminals now survive a
reload (phase 2, section 13). Still open: the bottom bar for narrow screens is the stacked layout only.

## 11. Phase 3 status: done, on the Phase 1 code (Workspace 1.2.0)

Phase 3 was built before phase 2 (the order was changed on 2026-10-30), on the 1.1.0 code: none of phase 2's work
(the `ws/` split, detached terminals, login-environment capture, `fs/tail`, Runs from here, My jobs removed) is
assumed or in it.

What landed:

- **Shared, pure, 3.6-safe modules in `hpclib/servers/`**, embedded into the package by `core_helpers`:
  `envinfo.py` (detect, find the project, status, the activation lines; `rest_envs.py` now delegates to it, so the
  REST server and the Workspace agree on what "ready" means and on `.venv.hpclib.json` and its marker; the 26
  `test_rest_envs` tests are the guard) and `clusterinfo.py` (sinfo, gres, sacctmgr, `sbatch --test-only` parsing;
  time and memory parsing and formatting). `console_packages.CORE_HELPERS` lists them. `rest_jobs.py` still has its
  own copies of the parsing: unify them when it is next touched (it was not changed so that the REST server's tests
  keep their meaning).
- **`jobspec.py`** (Workspace tunnel): `validate(spec, cluster)`, `render(spec, cluster, env)`, `embedded_spec(script)`.
  The spec is JSON; the script carries it as `#HPCLIB-SPEC {...}`, so a generated script opens in the form again and
  renders to the same text. Every value that reaches the shell is `shlex.quote`d, arguments are split with
  `shlex.split` and quoted again, `extra_sbatch` lines must start `#SBATCH `, names and partitions have character
  sets, and a single-line rule refuses newlines. The script traps TERM (`--signal=B:TERM@60`; the program runs in
  the background and the script waits in a loop, so the trap runs at once and the program is told).
  Problems the cluster would raise (time limit, memory, GPUs per node, unknown partition) are warnings here, and
  `sbatch --test-only` (Check) has the last word.
- **`ws_envs.py`** (Workspace tunnel): the card (status words ready, not prepared, stale, busy, failed, tool missing,
  none found), Prepare for uv and pixi (a child of the server on the login node; marker `.venv.hpclib-syncing`,
  record `.venv.hpclib.json` with the interpreter and the package list, log under `workspace/operations/`),
  `hpclib_pytools install` and `set`, and the directory settings. Prepare does not do cores, layers or squash (the
  REST server's), and an operation lost with the server leaves no record (the card then says "not prepared" or
  "stale" again).
- **Routes**: `GET /api/cluster`, `/api/modules`, `/api/env`, `/api/env/operation`, `/api/job`, `/api/tail`;
  `POST /api/env/{prepare,tool,dirs}`, `/api/jobs/{render,check,submit}`. Submit answers 409 while the environment
  would block it. Each submit writes `workspace/runs/JOBID.json` (job id, spec, script path and hash, the environment's
  record, git HEAD and whether the tree is dirty, the output path). `/api/tail` is the smallest follower the guided
  path needed; phase 2's `fs/tail` supersedes it.
- **UI**: `<hpc-resources>` and `units.js` in `hpclib/ui` (shared with the console's later use), the guided
  `features/submit.js` (form, environment card, read-only script with "Edit by hand…", Check, Submit, Submit and
  follow), `features/follow.js` (a dock view opened with `ws.dockOpen("follow", …)`; features still do not import
  each other) and `features/classic.js` (the old template form, opener `submit-classic`, one link away).
- **Tests**: `tests/test_workspace_jobspec.py` (golden scripts under `bash -n`, a real bash forwarding TERM to its
  child, quoting against hostile values, round trip, warnings), `TestSubmission` in `tests/test_workspace_package.py`
  (fake `sinfo`, `sacctmgr`, `sbatch --test-only`, `module` and `uv`: file to followed output, a stale
  environment blocks Submit, a busy one and a missing tool say so, hand-edited scripts), the browser path in
  `tests/test_workspace_ui.py`, `tests/units.test.mjs` for the browser's parsers, and the 3.6 grammar guard now covers
  the new modules.

Known limits: the environment card looks at one project (the nearest to the script) and the default pixi
environment; "none found" does not block Submit; a hand-edited script is checked and submitted as written (an
embedded spec only decides the environment gate); `module -t avail` is read in a login shell and cached ten minutes;
the job's state comes from `squeue`, then `sacct` (needs accounting).

## 12. Phase 4, as it looks after phase 3

1. **`.slurm` round trip.** Opening a `.slurm` or `.sbatch` file shows the form when it carries a spec (done: the
   script does) and, for a hand-written one, an import: parse `#SBATCH` lines into a spec where they map (time,
   nodes, tasks, CPUs, memory, GPUs, partition, account, QOS, output), keep the rest as `extra_sbatch`, keep the body as
   the command, and show a diff between the file and what the form would make, with **Adopt** (write the form's) or
   **Keep mine**. Needs the merge view of CodeMirror 6 (below).
2. **Profiles.** A named spec (resources, modules, shell init, environment mode) saved per user in
   `workspace/profiles/` (not browser storage), plus an optional `.hpclib/job.json` in a project that is offered when
   a script there is submitted. "Last used" becomes the first profile.
3. **Arrays and sweeps.** `array: {range, limit}` and `sweep: {name: [values]}` in the spec; a sweep writes
   `NAME.params.tsv` beside the script and the script picks its row by `SLURM_ARRAY_TASK_ID`; output becomes
   `%x-%A_%a.out`. The follower and the run record need to know a run has many tasks (state per task from
   `squeue -r`/`sacct`).
4. **Dependencies and chains.** `--dependency=afterok:ID` chosen from Runs from here (phase 2) rather than typed;
   "submit these two as a chain".
5. **Interactive mode.** `salloc`/`srun --pty` built from the same resources and opened as a dock terminal; no
   script.
6. **Squash.** After one convention exists for squashed environments (the REST server's): Prepare gains "squash after
   preparing" and the card shows image size and "unsquash"; jobs then mount through the existing prelude.
7. **CodeMirror 6.** Replace `<hpc-code>`'s internals behind its current attributes and methods; gains the merge
   view (1), better large files, language packs. Pin and hash it in `libs.json` like the others.
8. **What phase 3 left plain on purpose:** named pixi environments and cores/layers in the card; sandboxed or
   squashed Prepare; mail and requeue options (`--mail-type`, `--requeue`, checkpoint signals beyond TERM); several
   clusters in one page; `rest_jobs.py` using `clusterinfo.py`.

Phase 2 has since landed (section 13) and did what this paragraph said it would: the routes moved into `ws/`
modules, `/api/tail` and `follow.js` became `fs/tail` and the shared follower, the run records are the data behind
Runs, the login environment captured once replaced `login_shell_run` (Check's stdin variant included), and My jobs and
`/api/jobs` went. What that changes in this list is at the end of section 13.

## 13. Phase 2 status: done, with the Wunderbaum file browser (Workspace 1.3.0; tree fixes 1.3.1; 1.3.2: one-click fold, the "null" in the editor bar, the Submit tab no longer waits on a slow Slurm)

Two pieces of work are in this step: the file browser from the `hpclib-wunderbaum` draft, ported onto the new shell,
and phase 2 itself.

### 13.1 The file browser (from `chatgpt_drafts/hpclib-wunderbaum.patch`)

The draft was written against the old, pre-shell Workspace. What was kept is its design: one shared element,
`<hpc-file-browser>` (`hpclib/ui/components/file-browser.js`), used by the Workspace's `features/files.js` and the
console's Files page, with Wunderbaum 0.14.1 as the Tree view (pinned and hashed in `libs.json`, loaded from the
CDN like CodeMirror and xterm), a Details table, breadcrumbs, and a plain `<details>` tree as the fallback when the
library does not load. What was changed in porting:

- It sits behind the shell's registry like everything else: `files.js` configures the element (list, open, menu,
  directory and error callbacks) and keeps the path bar, Places, Upload, Usage and the ⋯ menu. The file manager still
  sends `ws:cwd`, `ws:open` and `ws:select`, listens to `ws:reveal` and `ws:fs-changed`, and now `ws:tab-path`
  (the shell sends it when the shown tab is a file, so the tree selects it).
- The draft's CSS went into `base.css`; the Workspace's own table styles were removed.
- `update_libs.py` asked for `dist/wunderbaum.min.css`, which the package does not have; the entry uses
  `dist/wunderbaum.css`.
- A bug: Wunderbaum fires `activate` after `setActive`, not during it, so selecting the open file from code
  activated the node, which opened the file, which selected it again, without end. `activateQuietly` marks the key
  as programmatic and the `activate` handler ignores exactly that key once.
- Follow-up fixes after first use: the tree's icons were font glyphs the page never loaded (a box for folders, a dot
  for files, in spans of no fixed width), so a folder's files lined up with the folder names above and looked like
  its siblings. Folders and files now have drawn icons (inline SVG, no font) in boxes of fixed width, with one visible
  step of indent per level. Double-clicking a folder, or Enter on the active one, makes it the top of the tree
  (`enter(path)`): the breadcrumbs, Up and Top lead back out.
- Tests: `tests/test_file_browser.mjs` (the element's logic with stubs), the Workspace UI tests (both views; the
  Wunderbaum ones need `WORKSPACE_TEST_NPM` to hold the pinned package and are skipped without it).

### 13.2 What phase 2 did

- **`ws/` split.** `workspace_server.py` is the HTTP side only (token, routes, static files, signed file URLs). The
  work is in `ws/`: `common` (Refused, `resolve`, directories), `shellenv`, `fs`, `terminal`, `cluster`, `envs` (was
  `ws_envs.py`), `jobspec` (moved), `jobs` and `templates`. They know nothing of HTTP and are tested through it.
  Not done as planned: authentication stayed in `workspace_server.py` (it is the HTTP side's, and `token_auth.py` is
  already its own module), and the classic templates stayed in `templates.py` rather than a renamed `recipes.py`
  (the guided path has no recipes yet).
- **The login environment, once.** `ShellEnv` runs `bash -lc 'env -0'` after a marker (so a profile's banner is not
  mistaken for the environment), keeps it, and runs `sbatch`, `sinfo`, `squeue`, `sacct`, `sacctmgr` and `scancel`
  directly with it: no shell parses a job id or a file name, and no profile runs per query. A program that is not found
  asks for a fresh capture once. `module` is a shell function, so `login()` runs it in a login shell, dropping what the
  profile printed. The capture leaves out `WORKSPACE_TOKEN`.
- **Terminals that outlive the page.** A session is a login shell in a pty that the server keeps; the page attaches
  over the WebSocket (`/terminal?ticket=&session=ID`), and leaving detaches. The last 256 KiB of output is kept and
  replayed on attach, several pages can watch one session, 16 may run, and an ended session is forgotten once nobody is
  attached. `GET /api/terms` lists them and `POST /api/term/close` hangs one up. `<hpc-terminal>` reconnects by itself
  (1, 2, 4 … 15 s, eight times) when a named session's connection drops, resets its screen and takes the replay; the
  page remembers its sessions' ids per user and host and attaches after a reload. Closing a tab ends its shell, as it
  did before; leaving does not. The sessions end with the Workspace (a tunnel restart loses them: discovering
  sessions that were started by an earlier server is not possible, since they were its children).
- **`fs/tail` and one follower.** `GET /api/fs/tail?path=&offset=&id=` returns the new bytes and an id for the file
  (device and inode); a follower that sends it back learns when the file was replaced and starts again from its end
  (`restarted`), as it does for a file that shrank. `follow.js` follows any file ("Follow as it grows" in the file
  menus) and, given a job, also its state and a Cancel. Keystrokes typed while a terminal is disconnected are dropped,
  not queued.
- **Runs from here.** `GET /api/runs[?dir=]` lists the run records newest first, with state from one `squeue` and one
  `sacct` call for all of them; a finished run's state is written into its record, so it is never asked about again.
  `POST /api/runs/ID/cancel` cancels a run of the Workspace's (a job from elsewhere is the terminal's). `features/runs.js`
  shows them under the file list, "From here" (the folder shown and below) or "All", with Follow, Script, Output and
  Cancel. My jobs, `/api/jobs` and `/api/cancel` are gone.
- **Layout.** The open tabs are kept under a key per user and host (the unscoped key of 1.1 is read once if the
  scoped one is absent). Unsaved editor changes are kept as drafts (20 files of up to 1 MB, a second after the last
  keystroke) and put back when the file is opened again if it has not changed on the cluster, or after asking if it
  has; saving, reloading from disk and closing the tab drop the draft. The editor limit is 64 MB by default (it was
  800; a JavaScript string cannot hold that).
- **Tests.** `TestTerminal` gained reattach-with-replay, two pages on one session and close; `TestSubmission` gained
  runs (state, scope, remembering the end, cancel) and `fs/tail`; `TestShellEnv` is new; the 3.6 grammar guard reads
  every `ws/*.py`; the browser tests cover Runs, a reload with a terminal, a dropped connection, drafts and the key
  scope.

### 13.3 Known limits

A run whose record could not be written (a read-only data directory) is not listed. A terminal's shell is a child of
the server: stop the server and the sessions go. `squeue` and `sacct` are asked for at most 100 unfinished runs at a
time. The "From here" scope compares working directories as strings (a symbolic link is not followed).

## 14. Phase 4a status: arrays, sweeps, dependencies, profiles (Workspace 1.4.0)

Done, in the order section 12 lists them (items 2 to 4; 1, 5 and 7 are 4b and 4c; 6 stays deferred):

- **Arrays and sweeps.** Spec keys `array {range, limit}` and `sweep {NAME: [values]}` (only present when set).
  A sweep is an array `0-(N-1)` over the combinations (first name slowest). *Change from section 12:* the combinations
  are written into the script itself (a quoted heredoc and one `awk`), not into `NAME.params.tsv` beside it, so a
  script is self-contained and a second submission of the same name can't change the table under tasks still queued.
  Each task exports its row's names; `{NAME}` in the arguments becomes `"${NAME}"`. Names and values are restricted
  (plain names; letters, digits and `. : + / = @ - _`), so the `eval` of an awk-made `export` list cannot be abused.
  Output defaults to `%x-%A_%a.out`; the follower shows the first task's file.
- **Run state per task.** `squeue -r` and `sacct -X` rows `ID_N` / `ID_[a-b%c]` are folded into one state per run
  (running beats pending beats the worst end) with the counts in `tasks`; the run record keeps `tasks` (the total) and
  the Runs list says "3/10 done, 1 failed".
- **Dependencies.** Spec key `dependency {type, jobs}` becomes `--dependency=TYPE:ids` and
  `--kill-on-invalid-dep=yes`; the form lists the runs from the working directory that are still going, and takes
  other job numbers.
- **Profiles.** `ws/profiles.py`; `GET/POST /api/profiles`, `POST /api/profiles/delete`. A profile is resources,
  modules, environment mode and shell init, output and extra `#SBATCH` lines; never the script, folder, arguments,
  sweep or dependency. Sources: last used (kept by the server on every submission), the project's
  `.hpclib/job.json` (found from the script's folder up to home), and saved ones under `workspace/profiles/`. The form
  opens with the project's, else the last used.

What 4b and 4c still hold: interactive mode, the `.slurm` import with a diff and Adopt / Keep mine, CodeMirror 6.

### 14.1 Follow-ups (1.4.1)

- The Submit form opens at once; Slurm and the environment answer into it (each gives up after a while) and it says how
  long each took. Finding uv and pixi no longer runs a login shell (`Environments.tools()` reads the PATH and the
  installers' directories); `hpclib_pytools --json`, which also starts an interactive shell for `~/.bashrc`, is asked only
  when the card's directory section is opened (`GET /api/env/settings`). A slow login node had made the card say "not
  installed" for tools that were there, and the form wait for it.
- The file filter applies to folders, and leaves out those with nothing to show.
- Fixed: `<hpc-resources>` printed its datalists as "[object HTMLDataListElement]" (an array passed as a child); the job
  script panel is taller.

## 15. Phase 4b status: the `.slurm` round trip and interactive sessions (Workspace 1.5.0)

- **Your own script as a spec.** Spec kind `body` keeps the script's commands verbatim in `body`; `render` writes the
  #SBATCH lines from the fields, then the body as it is (no TERM forwarding: there is no program of the form's to
  forward it to, and no `--signal` line). `jobspec.parse_script` reads a hand-written script: the options the form has
  fields for (name, time, nodes, tasks per node, CPUs, memory, GPUs by `--gres` or `--gpus`, partition, account, QOS,
  chdir, output, array with limit, a dependency of one kind) become the spec; every other option stays as an
  `extra_sbatch` line; the leading comments and commands are the body. Anything the parser can't place is kept, never
  dropped. A script with no `--time` gets a warning, since the form's default would change its limit.
- **Opening one.** "Open as job form…" on a `.slurm` or `.sbatch` file (`GET /api/jobs/import`): the form's own file
  comes back as its embedded spec; a hand-written one is imported. When the file differs from what the form would write,
  a line diff (`ui/diff.js`: prefix and suffix set aside, LCS for the middle, remove-all/add-all past two million
  cells) shows the difference with **Adopt the form's version** (`POST /api/jobs/adopt`: the file is rewritten and the
  original kept as `NAME.bak`) and **Keep mine** (the text stays as written; Submit runs `sbatch` on the file where it
  lies, refusing if it changed on disk since it was shown: `as_is` in `POST /api/jobs/submit`). Submitting the form's
  version of an opened script writes it over the file (`script_dir` keeps it where it was).
  *Change from section 12:* the diff is our own small view, not CodeMirror's merge view; 4c swapped it (section 16).
- **Interactive sessions.** The "Interactive session" button turns the form's resources into
  `srun --job-name=… --time=… --pty bash -il` (with the modules loaded first through `bash -lc 'module load …; exec bash -il'`)
  and opens a dock terminal in the working directory with that line typed into it, so it sits in the shell's history
  and can be edited. Arrays, sweeps and dependencies don't apply (the page says so). The environment (a uv project's venv)
  isn't activated: run `uv run` or activate it in the session.

What 4c held, CodeMirror 6, is section 16. Squash stays deferred (section 8).

## 16. Phase 4c status: CodeMirror 6 and the merge view (Workspace 1.6.0)

- **Delivery (the spike's answer).** CodeMirror 6 has no no-build script on npm, so hpclib builds one: `hpclib/ui/vendor/_build/`
  holds `cm6-entry.js`, a `package.json` with every package at an exact version (view 6.43.15, state 6.7.6, language 6.13.1,
  commands 6.11.1, search 6.7.2, merge 6.12.3, lang-python 6.2.1, legacy-modes 6.5.5, theme-one-dark 6.1.3, esbuild 0.28.2),
  a `package-lock.json`, and `build.sh` (`npm ci`, esbuild, minified IIFE; prints the sha384). The result,
  `hpclib/ui/vendor/codemirror6.js` (about 570 KB), is committed and defines one global, `CM6`. It is the same bytes on every
  build (checked: two builds compare equal). `libs.json` has the entry `{"local": "vendor/codemirror6.js"}` with its sha384
  (`update_libs.py` hashes it; a test checks the two agree), and `libs.js` loads a `local` library from beside itself with
  `integrity`. So the editor needs no CDN and no `--cdn` mirror; xterm.js and Wunderbaum still do. The Workspace's zip
  carries the bundle but not `_build/` (`bundle` skips directories that start with `_`).
- **`<hpc-code>`** keeps its attributes (`language`, `readonly`), methods and events; underneath it is a CM6 `EditorView`
  with compartments for language, read-only and theme. `languageFor()` still answers with the old mode names, which
  `CM6.language()` maps (Python by its Lezer grammar, the rest by the legacy stream modes: shell, YAML, TOML, JSON, JS/TS,
  XML/HTML, CSS, C/C++/Java, Fortran, R, Julia, SQL, properties). Markdown is plain text for now (it would need
  `@codemirror/lang-markdown`). The colours come from the page's tokens (`--bg`, `--fg`, `--line`, `--accent`, `--good`,
  `--bad`), the syntax colours follow dark mode. Loading a file isn't an undo step; `isClean` compares documents, so undoing
  back to the saved text is clean again (CM5 behaved the same). The textarea fallback stays for a page where the script
  can't load; `view` exposes the `EditorView`.
- **`<hpc-diff>`** (same file) is CM6's unified merge view, read-only: `diff.show(original, modified)`. It replaces
  `ui/diff.js` and its tests and the `.diff*` CSS; the import banner of an opened `.slurm` uses it, so changed lines are
  shaded, changed words are marked inside them, and runs of unchanged lines fold (two lines of context).
- **Behaviour changes to know**: Tab indents the line (CM5 inserted spaces at the cursor); the search panel is CM6's
  (Ctrl/⌘-F), Ctrl/⌘-G finds next; the editor is a bigger script than CM5's (570 KB against about 250 KB of files, but local).
- Tests now locate `.cm-content` / `.cm-editor` and read `hpc-code.value`; new ones: the editor highlights and undo returns
  to clean, the diff test reads the merge view, the bundle's hash matches `libs.json`, the zip carries the bundle.

### 16.1 Follow-up (1.6.1)

- The diff is easier to read: solid soft backgrounds for added and removed lines (the page's `--good` and `--bad` mixed into
  `--bg`, so they work in dark mode), a stronger tint for changed words, no underline or strike-through, no syntax colours
  inside the diff (they turned comments brown on red), and up to 480 px of height. `<hpc-diff>.show()` does nothing when
  called again with the same two texts, so a form that re-renders doesn't rebuild the view.

### 16.2 Follow-up (1.6.2): creating the same file twice

- `PUT /api/file?how=create` is now exclusive on the server: the temporary file is hard-linked into place (which fails when the
  name exists) instead of renamed over it, so two creates of one name at once give one 200 and the rest 409, and no
  `.workspace-tmp` file is left. (Before, both could pass the existence check and the later rename replaced the earlier file.)
- **New file** and **New folder** ignore a second request while one is still asking or creating, so a repeated click or
  Enter can't start a second create. Reported as a freeze in Zen (Firefox) when the first create hadn't returned; the
  freeze itself did not reproduce in Chromium, so this removes the race rather than a found loop.

## 17. Where the plan stands (Workspace 1.6.2, 2026-10-31)

Done: phases 0 to 4 as in the table in section 7, except squash. Not built, on purpose or not yet:

- **Squash** (5.6): deferred by decision 5; needs the two conventions unified first.
- **Markdown highlighting** in the editor (needs `@codemirror/lang-markdown` in the bundle); Markdown preview ("later" in 2.3).
- **xterm search addon**, the icon sprite and `console_packages.py build --vendor`: planned in section 2.3, not built (see the rows there).
- **Section 12 item 8** (named pixi environments and layers in the card, mail and requeue options, several clusters in one page,
  `rest_jobs.py` using `clusterinfo.py`) and the limits in 13.3.
- Not yet tried on real clusters: arrays, sweeps, dependencies, profiles and interactive sessions on Entropy, and Grace and
  Faster for the environment card; the editor and diff in Firefox-based browsers (Zen); the "create twice" freeze (16.2).

## 18. Links to a file (Workspace 1.7.0)

- **In the Workspace page**: `#file=/path[&line=N]` shows the file's folder in the browser and, once last time's tabs are
  back, opens the file (`ws.open` with `line`; an already open tab is brought to the front and moved to the line).
  `<hpc-code>.goto(line)` does the scrolling (CM6: selection plus `scrollIntoView` centred; textarea: a selection range).
  `dir=` still opens a folder alone. `bridge.js` removes only `theme` and `console` from the address, so `file` and
  `line` survive it. A path that is not absolute is read from the home directory.
- **In the console** (what a model gives you): `#/c/CLUSTER/apps/workspace?file=…&line=N`, beside the Files page's
  `#/c/CLUSTER/files?path=…`. The app page reads `file` and `line` for an app with `theme_link`; a running app is opened in
  the same tab at once (not when the page is reached with Back), otherwise the page says to start it and **Open** carries
  the file. `appUrl(url, state, link)` adds `file` and `line` after `theme` and `console`.
- Not done: a "Copy link" action inside the Workspace (it does not know the cluster's name in the console, nor the
  console's address beyond the loopback port in its address).
