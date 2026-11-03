# The fsdiag package (Filesystem diagnostics)

Why are my tunnels slow? Tunnel requests are slow when the file system calls behind them are: the REST server stat-s, opens and lists files on a shared file system, and each call can wait on a busy server. This app measures those calls on the login node and on compute nodes, counts what your folders hold, reads your quotas, and says what the REST server's slow requests were waiting for. See [Filesystem diagnostics](../../docs/interface/apps.md#filesystem-diagnostics-a-package) for using it.

It comes with hpclib but is not installed like the Workspace: **Packages → Come with hpclib → Filesystem diagnostics → Install…**, then **Install**. Or build the zip:

```bash
python3 hpclib/servers/console_packages.py build packages/fsdiag
```

| File | What it is |
| --- | --- |
| `hpclib-package.json` | the app (Open adds the token the server prints) and its settings |
| `tunnels/fsdiag/tunnel_config.sh` | runs on the login node (`RUN_ON_LOGIN_NODE=true`), port 8650 |
| `tunnels/fsdiag/sbatch_script.sh`, `install.sh`, `user.sh` | start `fsdiag_server.py` with `FSDIAG_PYTHON`; check for Python 3.6+; the settings' defaults |
| `tunnels/fsdiag/fsdiag_server.py` | the HTTP side: the token, the routes, the page's files |
| `tunnels/fsdiag/fsprobe.py` | the probe: per-call latency, mounts and NFS statistics, node load, a directory survey, quotas. A standalone script: it runs on the login node, from a SLURM job, or by hand |
| `tunnels/fsdiag/fsruns.py` | starts and follows runs: a login process, or one SLURM job per compute target (the probe's source travels inside the job script, so a job never reads code from the file system it is measuring) |
| `tunnels/fsdiag/fsrequests.py` | reads the REST server's audit log and stack dumps: slow routes, waiting against CPU time, what stuck requests were inside |
| `tunnels/fsdiag/fsfindings.py` | the rules: what the numbers mean, and the text report |
| `tunnels/fsdiag/fsdiag_cli.py` | the same from a shell: `probe`, `requests`, `all` (`--json` for the findings as data) |
| `tunnels/fsdiag/actions.json`, `fsdiag_action.sh` | the [tunnel actions](../../docs/extending.md#tunnel-actions) agents run through the REST server: `probe`, `survey`, `requests` and `probe_node` (a job) |
| `tunnels/fsdiag/ui/` | the page: `index.html`, `app.js`, `client.js`, `views/` (probe, results, requests, directories), `bridge.js` |
| `tunnels/fsdiag/ui/hpclib-ui/` | not in the repository: a copy of `hpclib/ui/` that `bundle` puts in the zip when it is built |

Tests: `tests/test_fsdiag_package.py`.

Everything is Python 3.6+ standard library on the clusters. A probe creates files only inside a directory named `.hpclib-fsdiag-HOST-PID` in each place and removes it again.
