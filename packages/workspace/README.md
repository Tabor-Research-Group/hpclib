# The Workspace package

Your files on a cluster in the browser, with an editor, viewers, terminals and job submission from script
templates. See [Workspace](../../docs/interface/apps.md#workspace-a-package) for using it.

Install it from the console's **Packages** (it is listed under **Come with hpclib**), or build the zip:

```bash
python3 hpclib/servers/console_packages.py build packages/workspace     # writes packages/workspace-1.0.0.zip
```

| File | What it is |
| --- | --- |
| `hpclib-package.json` | the app (Open adds the token the server prints) and its settings |
| `tunnels/workspace/tunnel_config.sh` | runs on the login node (`RUN_ON_LOGIN_NODE=true`), no conda, proxy or helpers |
| `tunnels/workspace/sbatch_script.sh` | starts `workspace_server.py` on the tunnel's port with `WORKSPACE_PYTHON` |
| `tunnels/workspace/install.sh` | `--check`: is there a Python 3.6+ |
| `tunnels/workspace/workspace_server.py` | the server: files, raw files with Range, terminals (a pty and a WebSocket), templates, sbatch and squeue |
| `tunnels/workspace/script_templates.py` | parsing and filling in script templates |
| `tunnels/workspace/script_templates/*.sh` | the templates it comes with (uv, pixi, venv/conda, bash) |
| `tunnels/workspace/ui/` | the page: plain JavaScript; CodeMirror 5 and xterm.js come from the CDN |

Tests: `tests/test_workspace_package.py` (server, templates, terminal, installing it) and
`tests/test_workspace_ui.py` (the page in Chromium; set `WORKSPACE_TEST_NPM` to a folder laid out like the CDN to
test with CodeMirror and xterm.js too).
