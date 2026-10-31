# The Workspace package

Your files on a cluster in the browser, with an editor, viewers, terminals and job submission (a form that writes the sbatch script, with environment management for uv and pixi; script templates remain). See [Workspace](../../docs/interface/apps.md#workspace-a-package) for using it.

Install it from the console's **Packages** (it is listed under **Come with hpclib**), or build the zip:

```bash
python3 hpclib/servers/console_packages.py build packages/workspace     # writes packages/workspace-1.2.0.zip
```

| File | What it is |
| --- | --- |
| `hpclib-package.json` | the app (Open adds the token the server prints) and its settings |
| `tunnels/workspace/tunnel_config.sh` | runs on the login node (`RUN_ON_LOGIN_NODE=true`), no conda, proxy or helpers |
| `tunnels/workspace/sbatch_script.sh` | starts `workspace_server.py` on the tunnel's port with `WORKSPACE_PYTHON` |
| `tunnels/workspace/install.sh` | `--check`: is there a Python 3.6+ |
| `tunnels/workspace/workspace_server.py` | the server's HTTP side: the token, the routes, the page's files, signed file URLs, raw files with Range |
| `tunnels/workspace/ws/` | the work, with no HTTP in it: `common`, `shellenv` (the login environment, captured once), `fs` (files, usage, `tail`), `terminal` (detached sessions and the WebSocket), `cluster`, `envs` (the environment card and Prepare, tool install and directories), `jobspec` (the job spec, its validation and the script made from it), `jobs` (check, submit, Runs), `templates` (the classic ones) |
| `tunnels/workspace/script_templates.py` | parsing and filling in script templates |
| `tunnels/workspace/envinfo.py`, `clusterinfo.py` | not in the repository: like `token_auth.py`, copies of `hpclib/servers/envinfo.py` (what an environment is, shared with the REST server) and `clusterinfo.py` (partitions, GPUs, accounts, `sbatch --test-only`) that `core_helpers` puts in the zip when it is built |
| `tunnels/workspace/script_templates/*.sh` | the templates it comes with (uv, pixi, venv/conda, bash) |
| `tunnels/workspace/ui/` | the page: `index.html` (layout and import map), `app.js` (start), `shell.js` (registry, tabs, dock), `client.js` (token, API, paths), `features/` (files, editor, submit with classic, follow, runs, usage, terminals: each registers itself), `bridge.js` (the console's theme) |
| `tunnels/workspace/ui/hpclib-ui/` | not in the repository: a copy of `hpclib/ui/` (plus `default-styles.css` and `theme.js`) that `bundle` in `hpclib-package.json` puts in the zip when it is built. CodeMirror 6 is a bundle in it (`vendor/codemirror6.js`, built by `vendor/_build/build.sh`, hash in `libs.json`); xterm.js and Wunderbaum load from the CDN `libs.json` pins |

Tests: `tests/test_workspace_package.py` (server, templates, terminal, installing it) and
`tests/test_workspace_ui.py` (the page in Chromium; set `WORKSPACE_TEST_NPM` to a folder laid out like the CDN to
test with xterm.js and Wunderbaum too).

The page is built from hpclib's shared elements (`<hpc-tabs>`, `<hpc-split>`, `<hpc-menu>`, `<hpc-code>`,
`<hpc-terminal>`, `toast()`, dialogs), so it follows the console's theme: the console's Open link adds
`#theme=…&console=http://127.0.0.1:PORT` (the manifest's `theme_link`), and `bridge.js` takes the theme and loads
that console's `/user.css`. Only a loopback address is accepted. A feature adds itself with `ws.registerOpener`,
`ws.registerAction` or `ws.registerDock` (see the top of `shell.js`); the file manager is `features/files.js`, which
a replacement keeps the contract of (its header says what it sends and listens for).

To try the page from a clone, build the package first (`console_packages.py build packages/workspace`) and unpack
it, or run `tests/test_workspace_ui.py`, which does that: `ui/hpclib-ui/` exists only in the built package.
