VSCODE_USER=$(whoami)
export VSCODE_CONTAINER=/scratch/user/$VSCODE_USER/vscode.sif
export VSCODE_BIND_PATHS=/scratch/user/$VSCODE_USER:/scratch/user/$VSCODE_USER,/home/$VSCODE_USER:/home/$VSCODE_USER
export VSCODE_ROOT_DIR=/scratch/user/$VSCODE_USER