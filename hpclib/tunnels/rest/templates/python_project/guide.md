# python_project

Runs a Python script inside a project's own environment, managed with **uv** or **pixi**. Use it for anything
that needs packages beyond the standard library; the bundled `python_script` only has the cluster's `python3`.

1. Make a project directory inside your allowed directories, e.g. `projects/scan-analysis/`, with either
   - `pyproject.toml` (and ideally `uv.lock`) for **uv**: pure-Python and PyPI packages, or
   - `pixi.toml` (and ideally `pixi.lock`) for **pixi**: conda-forge packages too (xtb, openmm, rdkit, ...).
   Create the lockfile on your own machine (`uv lock`, `pixi lock`) and push both files with `push_files`.
2. `sync_environment(project)` installs it on the cluster (`uv sync --locked` / `pixi install --locked`). It
   runs in the job sandbox where the REST server runs; check `state` and `log_tail`. `update=true` re-resolves
   when there is no lockfile or you changed the dependencies.
3. Submit `python_project` with `project` and `script`; `args` passes simple arguments. The job activates the
   environment inside its sandbox (jobs never install anything) and runs `python script args` in the project.

If `submit_job` says the environment isn't set up, sync it first. Keep inputs and outputs inside the project or
another allowed directory; the environment itself lives in `.venv` (uv) or `.pixi/envs/default` (pixi).
