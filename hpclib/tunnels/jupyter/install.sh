#!/usr/bin/env bash
# hpclib-install: --check
# JupyterLab comes from where the job looks for it (see sbatch_script.sh): the environment of a uv or pixi
# project (HPCLIB_JUPYTER_PROJECT), modules (HPCLIB_JUPYTER_MODULES), or the conda environment
# (CONDA_ENVIRONMENT). --check reports whether jupyter is found there. Installing adds jupyterlab to the
# project: `pixi add` in a pixi project, otherwise a uv environment in PROJECT/.venv (created if need be).
set -e

project_env() {
  local env
  for env in "$HPCLIB_JUPYTER_PROJECT/.venv" "$HPCLIB_JUPYTER_PROJECT/.pixi/envs/default"; do
    if [ -x "$env/bin/jupyter" ]; then printf '%s\n' "$env"; return 0; fi
  done
  return 1
}

where=''
if [ -n "${HPCLIB_JUPYTER_PROJECT:-}" ] && env=$(project_env); then
  where="the project environment $env"
elif [ -n "${HPCLIB_JUPYTER_MODULES:-}" ] && command -v module > /dev/null 2>&1 &&
     (module load $(printf '%s' "$HPCLIB_JUPYTER_MODULES" | tr ':' ' ') > /dev/null 2>&1 &&
      command -v jupyter > /dev/null 2>&1); then
  where="the modules $HPCLIB_JUPYTER_MODULES"
elif [ -z "${HPCLIB_JUPYTER_PROJECT:-}" ] && [ -n "${CONDA_ENVIRONMENT:-}" ] && command -v conda > /dev/null 2>&1 &&
     conda run -n "$CONDA_ENVIRONMENT" jupyter --version > /dev/null 2>&1; then
  where="the conda environment $CONDA_ENVIRONMENT"
fi
if [ -n "$where" ]; then
  printf 'installed: jupyter from %s\n' "$where"
  exit 0
fi
if [ "${1:-}" = --check ]; then
  if [ -n "${HPCLIB_JUPYTER_PROJECT:-}" ]; then
    printf 'not installed: %s has no environment with jupyter in it\n' "$HPCLIB_JUPYTER_PROJECT"
  else
    printf 'not installed: jupyter is not in the conda environment %s or the modules given\n' "${CONDA_ENVIRONMENT:-(none)}"
  fi
  exit 1
fi

if [ -z "${HPCLIB_JUPYTER_PROJECT:-}" ]; then
  echo "set a uv or pixi project (HPCLIB_JUPYTER_PROJECT) to install jupyterlab there, or install jupyterlab" \
    "in your conda environment or load a module that has it" >&2
  exit 1
fi
project="$HPCLIB_JUPYTER_PROJECT"
mkdir -p "$project"
if [ -f "$project/pixi.toml" ]; then
  command -v pixi > /dev/null 2>&1 || { echo "pixi isn't on this node's PATH" >&2; exit 1; }
  (cd "$project" && pixi add jupyterlab && pixi install)
else
  command -v uv > /dev/null 2>&1 || { echo "uv isn't on this node's PATH (https://docs.astral.sh/uv/)" >&2; exit 1; }
  if [ -f "$project/pyproject.toml" ]; then
    (cd "$project" && uv add jupyterlab)
  else
    [ -x "$project/.venv/bin/python" ] || uv venv "$project/.venv"
    uv pip install --python "$project/.venv/bin/python" jupyterlab
  fi
fi
env=$(project_env) || { echo "jupyterlab was installed but $project has no environment with jupyter in it" >&2; exit 1; }
printf 'installed: jupyter from the project environment %s\n' "$env"
