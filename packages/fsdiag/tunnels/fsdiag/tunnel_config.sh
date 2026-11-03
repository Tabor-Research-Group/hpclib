# Filesystem diagnostics, served by fsdiag_server.py on the login node (no job: sbatch and your files are there; the
# compute-node probes are jobs it submits). Python 3.6+ only.
DEFAULT_PORT=8650
PROCESS_PORT=8650
RUN_ON_LOGIN_NODE=true
CONDA_ENVIRONMENT=""
ENABLE_WEB_PROXY=false
START_GIT_SERVER=false
START_SLURM_SERVER=false
# Settings (the console's settings for this app, or tunnel_setup HOST fsdiag --set NAME=VALUE --save):
TUNNEL_SETTINGS="FSDIAG_PATHS FSDIAG_SURVEY_ROOTS FSDIAG_PARTITION FSDIAG_ACCOUNT FSDIAG_TIME_LIMIT FSDIAG_MEM FSDIAG_PYTHON FSDIAG_COMPUTE_PYTHON"
