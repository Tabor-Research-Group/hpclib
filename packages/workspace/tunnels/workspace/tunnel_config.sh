# The Workspace: files, an editor, viewers, terminals and job submission in the browser, served by
# workspace_server.py on the login node (no job: sbatch and your files are there). Python 3.6+ only.
DEFAULT_PORT=8640
PROCESS_PORT=8640
RUN_ON_LOGIN_NODE=true
CONDA_ENVIRONMENT=""
ENABLE_WEB_PROXY=false
START_GIT_SERVER=false
START_SLURM_SERVER=false
# Settings (the console's Workspace settings, or tunnel_setup HOST workspace --set NAME=VALUE --save):
TUNNEL_SETTINGS="WORKSPACE_PYTHON WORKSPACE_ROOT WORKSPACE_EDIT_LIMIT_MB WORKSPACE_CONFIRM_MB WORKSPACE_CDN"
