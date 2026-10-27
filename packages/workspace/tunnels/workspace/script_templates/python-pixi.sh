#!/bin/bash
#@ title: Python with pixi
#@ description: pixi run: the pixi project's environment (pixi.toml, or pyproject.toml with [tool.pixi])
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
#@ field project: pixi project | {{dir}} | the directory with pixi.toml
#@ field environment: pixi environment | default
#@ field pixi_flags: pixi run options | | e.g. --frozen or --locked
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

PIXI="$(command -v pixi || echo "$HOME/.pixi/bin/pixi")"
"$PIXI" run {{pixi_flags}} --manifest-path "{{project}}" -e "{{environment}}" python -u "{{input}}" {{args}}
slurm_job_footer
