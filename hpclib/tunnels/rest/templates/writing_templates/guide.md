# Writing a job template

Read this before using `propose_template`. A proposal is checked the same way as a real template. On most servers
it then waits until the cluster owner reviews and approves it, so make the reviewer's job easy. If `cluster_info`
shows `template_proposals` with `"review": "automatic"`, a valid proposal becomes a template at once: then you are
the only reviewer, so read your script again and start with a dry run and a single small job. To fix a proposal
that is still waiting, propose it again under the same name.

## Find the software first

- `search_modules` with a package name (e.g. `orca`, `gaussian`, `cp2k`) lists the versions on the cluster.
- `search_modules` with a full `name/version` shows which modules must be loaded before it (compilers, MPI). Put
  all of them, in load order, in the template's `modules` list.
- If nothing is found, say so in the rationale; don't propose a template that installs software.

## template.json

```json
{
  "description": "One or two sentences: what it runs, on what input, and what it produces.",
  "parameters": {"nprocs": {"type": "integer", "minimum": 1, "maximum": 32, "default": 4}},
  "array": {"task_parameters": {"input": {"type": "path", "kind": "file"}}},
  "resources": {"time": "02:00:00", "mem": "8G", "ntasks": "${nprocs}", "nodes": "1"},
  "overridable": ["time", "mem"],
  "modules": ["GCC/12.2.0", "OpenMPI/4.1.4", "ORCA/5.0.4"]
}
```

- Parameter types are `string` (with `enum`, `pattern` or `max_length`), `integer` and `number` (with
  `minimum`/`maximum`), `boolean`, and `path` (checked against your allowed directories; `kind` is `file`,
  `directory` or `any`, and `must_exist` defaults to true).
- Give every parameter the narrowest type and bounds that work. Prefer an `enum` of known methods over a free
  string.
- Resources can refer to parameters (`"${nprocs}"`). Keep the defaults small, and list in `overridable` only what
  a user should change.
- Include `array` only when the job runs once per input; the tasks then come from `tasks` or `tasks_from`.

## script.sh

- Bash, with no `#SBATCH` lines; resources come from template.json.
- Parameters arrive as `$HPC_PARAM_<NAME>` and per-task values as `$HPC_TASK_<NAME>`, already quoted. Always use
  them in double quotes (`"$HPC_TASK_INPUT"`) and never `eval` them.
- Start with `set -euo pipefail`. Check inputs early and exit with a clear message and a distinct exit code.
- Write results next to the input, or into the job's working directory; use `$TMPDIR` for scratch, and clean it up
  with a `trap`.
- End with a check that the program actually succeeded, not just its exit code.
- If `cluster_info` shows a `sandbox`, the script runs in a container that can write only to your allowed
  directories (plus `/tmp`, which is deleted with the job) and reads only the host's system directories and the
  sandbox `binds`. `~` is not writable: point programs' caches and settings at the job's directory or `/tmp`. It can't run `srun`,
  `sbatch` or other SLURM commands, so keep the job on one node. If the software lives in a directory the
  sandbox doesn't bind, say so in the rationale so the owner can add it.

## Rationale

State what you will run with it and why the existing templates don't fit, which modules you chose, and anything
the reviewer should check (for example, a resource default you were unsure of).
