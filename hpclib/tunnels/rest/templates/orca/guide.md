# ORCA job arrays

Runs ORCA on `.inp` files that already exist on the cluster, as one SLURM job array: one task per input, at most
`throttle` running at once. Use it for any set of independent ORCA calculations, such as the points of a scan, a
conformer set, or a benchmark.

## Before submitting

- The inputs have to be on the cluster, inside your allowed directories. If they were generated on this computer
  (for example by Psience's `ScanManager.generate`), copy the whole directory with `push_files`, including any
  manifest such as `scan_info.json`.
- Set `nprocs` to the core count in the inputs' `%pal nprocs` block (1 if there is none). A mismatch makes each
  task stop immediately with exit code 4, and the error says which value to use.
- Memory: ORCA uses up to about `nprocs × MaxCore` MB, plus some overhead. Keep `mem` above that; for example,
  4 cores × 3500 MB of MaxCore needs `mem` of at least 16G.
- Keep `time` close to what one calculation needs. Each task gets it separately.

## Submitting

With a manifest (preferred: the task list stays small, and tasks can be traced back to manifest entries):

```json
{"template": "orca", "params": {"nprocs": 4}, "label": "sample_scan",
 "tasks_from": {"path": "scans/sample_scan/scan_info.json", "key": "steps", "fields": {"input": "file"}},
 "idempotency_key": "sample_scan-v1", "dry_run": true}
```

Paths inside the manifest (`"file": "scan_000_001.inp"`) are relative to the manifest. Without a manifest, pass
`"tasks": [{"input": "scans/a/x.inp"}, ...]`.

Run with `dry_run: true` first, then without it.

## Following progress

- `job_status` with `include_tasks: true` lists every task with its state, its manifest entry (`source_index`),
  and its SLURM log (`hpc-rest-JOB_TASK.out` in the job's working directory).
- ORCA's own output is `NAME.out` next to each input, written while the task runs; `tail_file` it to follow an
  optimization.
- A task succeeded only if its `.out` contains `ORCA TERMINATED NORMALLY`.

## Failures

- Look at the end of the task's `NAME.out`, then at its SLURM log.
- Rerun only the failed tasks: submit again with the same `tasks_from` plus `"select": [...]`, using the failed
  tasks' `source_index` values, and a new idempotency key. If a `.gbw` from the failed attempt exists, it is
  copied into the run, so an input with `! MORead` and `%moinp "NAME.gbw"` can restart from it.
- Exit 3 means ORCA isn't loaded: the template's `modules` list needs setting. Use `search_modules` with `orca`
  to find the right modules, and suggest the change to the owner with `propose_template`.

## Getting results back

`pull_files` with a pattern such as `*.out,*.xyz` copies results next to the local inputs. Psience's
`ScanManager.parse` then reads them unchanged, because it looks for `NAME.out` next to `NAME.inp`.
