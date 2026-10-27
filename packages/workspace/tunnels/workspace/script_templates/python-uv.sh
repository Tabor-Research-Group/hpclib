#!/bin/bash
#@ title: Python with uv
#@ description: uv run: the uv project's environment (its pyproject.toml), or the script's own inline dependencies when there's no project
#@ for: .py
#@ script: {{input_stem}}.slurm
#@ field job_name: Job name | {{input_stem}}
#@ field time: Time limit | 6:00:00 | e.g. 6:00:00 or 1-00:00:00
#@ field nodes: Nodes | 1
#@ field ntasks_per_node: Tasks per node | 1
#@ field cpus_per_task: CPUs per task | | empty: the cluster's default
#@ field mem: Memory per node | 8G | e.g. 55G
#@ field gres: GPUs | | e.g. gpu:a100:1; empty: none
#@ field partition: Partition | | empty: the cluster's default
#@ field account: Account | | the allocation to charge, when the cluster asks for one
#@ field modules: Modules | | loaded first, space-separated, e.g. GCC/13.3.0 CUDA/12.6.0
#@ field project: uv project | {{dir}} | the directory with pyproject.toml; empty: uv run --script (the script's inline dependencies)
#@ field python: Python version | | uv run --python, e.g. 3.12; empty: the project's
#@ field uv_flags: uv run options | | e.g. --no-sync where compute nodes have no internet (uv sync on the login node first)
#@ field args: Arguments | | passed to the script
##ENVIRONMENT SETTINGS; CHANGE WITH CAUTION
#SBATCH --export=NONE                # don't pass the submitting shell's environment on

##JOB SPECIFICATIONS
#SBATCH --job-name={{job_name}}
#SBATCH --time={{time}}
#SBATCH --nodes={{nodes}}
#SBATCH --ntasks-per-node={{ntasks_per_node}}
{{?cpus_per_task}}#SBATCH --cpus-per-task={{cpus_per_task}}
#SBATCH --mem={{mem}}
{{?gres}}#SBATCH --gres={{gres}}
{{?partition}}#SBATCH --partition={{partition}}
{{?account}}#SBATCH --account={{account}}
#SBATCH --output=%x-%j.out           # stdout and stderr, next to the script
#SBATCH --signal=B:TERM@60           # a minute's warning before the time limit

curdir="$PWD"
. ~/.bashrc                          # your environment: --export=NONE starts without it
cd "$curdir"
{{?modules}}module load {{modules}}
. "{{hpclib}}"                       # slurm_job_info and slurm_job_footer; the uv/pixi directories saved for this cluster
SLURM_JOB_START_TIME=$(date +%s)
slurm_job_info {{args}}

UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
UV_ARGS=({{uv_flags}})
{{?python}}UV_ARGS+=(--python "{{python}}")
{{?project}}"$UV" run "${UV_ARGS[@]}" --project "{{project}}" python -u "{{input}}" {{args}}
{{!project}}"$UV" run "${UV_ARGS[@]}" --script "{{input}}" {{args}}
slurm_job_footer
