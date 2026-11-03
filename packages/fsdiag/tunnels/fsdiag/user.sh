# Defaults for the app's settings; the saved settings (~/.local/tunnels/settings/fsdiag.sh) win.
export FSDIAG_PYTHON="${FSDIAG_PYTHON:-python3}"
export FSDIAG_COMPUTE_PYTHON="${FSDIAG_COMPUTE_PYTHON:-python3}"   # the compute nodes' python for the probe jobs
export FSDIAG_PATHS="${FSDIAG_PATHS:-}"                 # places to time: LABEL=PATH:PATH...; empty: home, data, scratch, /tmp, /dev/shm
export FSDIAG_SURVEY_ROOTS="${FSDIAG_SURVEY_ROOTS:-}"   # folders to count; empty: your home directory
export FSDIAG_PARTITION="${FSDIAG_PARTITION:-}"
export FSDIAG_ACCOUNT="${FSDIAG_ACCOUNT:-}"
export FSDIAG_TIME_LIMIT="${FSDIAG_TIME_LIMIT:-00:20:00}"
export FSDIAG_MEM="${FSDIAG_MEM:-1G}"
