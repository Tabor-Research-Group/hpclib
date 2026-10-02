# Demo: an ORCA scan generated locally, run on SLURM

This demo generates a 5 × 5 scan of one atom's position with Psience's `ScanManager` on your own machine and runs
all 25 ORCA optimizations on the cluster as a single SLURM job array. It then brings the outputs back so
`ScanManager.parse` can read them. You can drive it two ways:

- **with an LLM:** Claude Desktop or Claude Code, through hpclib's MCP server, using a token that can only run the
  approved `orca` template in one directory;
- **with a script:** `run_scan.py` makes the same calls itself.

| File | Runs on | Purpose |
| --- | --- | --- |
| `setup_cluster.sh` | your machine | one-time: install hpclib on the cluster, add the `orca` template and a config, mint a scoped token |
| `cluster_config.json` | (copied to the cluster) | resource limits and notes for the model, as `~/.local/tunnels/rest/config.json` |
| `generate_scan.py` | your machine | write the scan's `.inp` files and `scan_info.json` (needs McUtils + Psience) |
| `mcp_config.json` | your machine | MCP client entry for Claude Desktop / Claude Code |
| `prompt.md` | your machine | an example prompt for the model |
| `run_scan.py` | your machine | the same workflow without an LLM |

Below, `user@login.example` is your cluster login and `/scratch/user/me/llm` is the cluster directory the model may
use.

## 1. One-time setup

```bash
cd hpclib/hpclib/examples/orca_scan
bash setup_cluster.sh user@login.example /scratch/user/me/llm
```

The script:

1. runs `install_hpclib`;
2. copies the `orca` template and the `writing_templates` guide into `~/.local/tunnels/rest/templates/` on the
   cluster;
3. installs `cluster_config.json` if you don't have a config yet;
4. creates the owner (full-access) token on your machine as `~/.config/hpclib/rest_token`, and gives the cluster
   only its hash, in `~/.local/tunnels/rest_token`. Nothing on the cluster, a job included, can read the token back;
5. mints a token named `llm-scan` with the `read,submit,propose,files:write` scopes, limited to
   `/scratch/user/me/llm`, and saves it as `~/.config/hpclib/llm_token`. With `propose`, the model can suggest
   template changes, such as the right `modules`, for you to approve.

Both local token files are mode 600. The cluster's `~/.local/tunnels/rest/tokens.json` holds only hashes of scoped
tokens like `llm-scan`, never the owner token.

Rerunning the script is safe: it upgrades hpclib only if your copy is newer, and keeps an existing template, config
and tokens. If the cluster already has a plaintext owner token (the REST server writes one on its first start when
there is none), the script prints the two commands to copy it to your machine and hash it.

Then, on the cluster:

1. **Load ORCA in the template.** Find the module names with `module spider orca`, then `module spider
   ORCA/<version>` to see what must be loaded first. Put them, in order, in the `"modules"` list in
   `~/.local/tunnels/rest/templates/orca/template.json`, for example
   `["GCC/12.2.0", "OpenMPI/4.1.4", "ORCA/5.0.4"]`. Alternatively, let the model do it: it can search modules
   and propose the change, which you then approve with
   `python3 ~/hpclib/servers/rest_server.py --list-proposals` and `--approve-template orca --replace`.
2. **Describe your cluster for the model.** Edit `cluster_notes` in `~/.local/tunnels/rest/config.json`: partitions
   to prefer, scratch rules, anything a model should know.

On your machine, install the MCP SDK for the Python your LLM client will use (`python3 -m pip install mcp`). Add the
entry from `mcp_config.json` to Claude Desktop's config (or `claude mcp add` for Claude Code), with your own paths.
`--local-root` is the only local directory the model can read from or write to; here it is `~/Desktop/scans`.

## 2. Generate the scan

```bash
python generate_scan.py ~/Desktop/scans/sample_scan     # --nprocs 4 --maxcore 3500 by default
```

The inputs ask for `%pal nprocs 4` and `MaxCore 3500`. Each task therefore needs `nprocs` 4 and at least ~16G of
`mem`, which is what `run_scan.py` and the example prompt use. If the counts don't match, the `orca` template
stops each task immediately with exit code 4 and says which value to use.

**Check the chemistry before running it.** The inputs are plain `Opt` jobs. Unless your job builder adds a
constraint on the scanned atom (an ORCA `%geom Constraints` block), every point will relax away from its grid
position, possibly to the same minimum.

## 3. Start the tunnel

```bash
source hpclib/hpclib/hpclib.sh
launch_tunnel -A none -P 5050 user@login.example rest -- --allow /scratch/user/me/llm
```

Leave it running. It's a SLURM job (8 hours by default), and the REST server lives inside it. Your jobs don't:
they keep running, and keep their records, when the tunnel ends. Start a new tunnel to check on them later.

## 4a. Run it with an LLM

Open Claude Desktop or Claude Code and use `prompt.md`, adjusted to your paths. A typical session:

1. `cluster_info`, then `read_guide("orca")`.
2. `push_files` from the scan directory to `scans/sample_scan`.
3. `submit_job` with `dry_run`, using `"tasks_from": {"path": "scans/sample_scan/scan_info.json", "key": "steps",
   "fields": {"input": "file"}}`. This shows the generated job script and SLURM's start estimate.
4. The real `submit_job`, with an idempotency key and the label `sample_scan`.
5. `job_status` with `include_tasks` to follow it, and `tail_file` on a running point's `.out`.
6. For failures: the end of each failed point's `.out`, then a resubmission of just those points with
   `tasks_from.select`.
7. `pull_files` with the pattern `*.out,*.xyz`, back into the scan directory.

The token limits what the model can do. It can only run the templates in `~/.local/tunnels/rest/templates`, within
the limits in the config, and only touch files under `/scratch/user/me/llm` on the cluster and `--local-root` on
your machine. It can't run `sbatch` or `scontrol` directly, and anything it proposes as a new template waits for you.

## 4b. Run it with the script

```bash
export HPC_REST_URL=http://127.0.0.1:5050 HPC_REST_TOKEN_FILE=~/.config/hpclib/llm_token
python run_scan.py ~/Desktop/scans/sample_scan --remote-dir scans/sample_scan
python run_scan.py ~/Desktop/scans/sample_scan --remote-dir scans/sample_scan --retry-failed JOB_ID
```

The script prints progress until the array finishes, lists any failed points with their logs, and downloads the
outputs. Rerunning it is safe: uploads and downloads skip files that are already there, and the submission is
idempotent.

## 5. Read the results

The `.out` files sit next to their `.inp` files again, so `ScanManager` reads them as if ORCA had run locally:

```python
import os
from Psience.Data import ScanManager
from Psience.Molecools import Molecule

manager = ScanManager(os.path.expanduser("~/Desktop/scans/sample_scan"))
results = manager.parse(
    lambda mol: {"coords": mol.coords},                      # the optimized geometry at each point
    output_file_generator=lambda inp: inp[:-len(".inp")] + ".out",
    molecule_loader=lambda out: Molecule.from_file(out, "orca"),
)
results["coords"].shape                                      # (5, 5, n_atoms, 3)
```

This passes the output-file mapping and the loader explicitly, which works with any Psience version. With
`chatgpt_drafts/psience-scanmanager-fixes.patch` applied to Psience, `manager.parse(extractor)` alone is enough:
the manifest records the job type, and `parse` picks the `.out` files and the ORCA reader itself.

## Where things are

| What | Where |
| --- | --- |
| ORCA output for a point | next to its input: `scans/sample_scan/scan_001_002.out` |
| SLURM log for a task | the job's working directory: `hpc-rest-JOB_TASK.out` (`job_status` gives the path) |
| Job records | the cluster's `~/.local/tunnels/rest/jobs.sqlite`, which outlives tunnels |
| Every API request | `~/.local/tunnels/rest/audit.log` |

## Troubleshooting

- **"tunnel is up but the REST server's job is still queued"**: the tunnel's own SLURM job is waiting for a node;
  retry in a minute.
- **Exit 3, `orca is not on PATH` or `could not load module`**: set the template's `modules` (step 1.1). A job
  script that loads modules runs as a login shell (`#!/bin/bash -l`), like `search_modules`, so a module that
  search finds should load in the job too.
- **429 "concurrent slots"**: API jobs plus array throttles already reach `max_concurrent_jobs` in the config.
  Lower `throttle` or wait.
- **422 with `violations`**: the request broke a limit or a parameter rule; the list says which.
- **Known issues in Psience** (all fixed by `psience-scanmanager-fixes.patch`; `generate_scan.py` and step 5 work
  either way):
  - `molecule_atom_position_scan_iterator` calls `Molecule.fragment_embedding`, which only exists on
    `MoleculeBuilder`.
  - `ScanManager.generate` records `index` in place of `values` when the scan values have no `.tolist()`.
  - `parse()` without arguments fails: the base class has no `output_file_ext`, and its default loader is the
    `Molecule` *module* rather than the class, because of a circular import.
