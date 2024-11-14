#! /bin/bash

. ~/.bashrc

if [ "$ENABLE_WEB_PROXY" = "true" ]; then
  module load WebProxy
fi
if [ "$CONDA_ENVIRONMENT" != "" ]; then
  conda activate $CONDA_ENVIRONMENT
fi
if [ "$START_SLURM_SERVER" = "true" ]; then
  python $HPCSERVERS_DIR/slurm_server.py &
fi