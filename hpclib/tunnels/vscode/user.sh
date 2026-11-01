VSCODE_USER="${VSCODE_USER:-$(whoami)}"
# Where things go by default: /scratch/user/USER where a cluster has it (Entropy, Grace, Faster), else your home directory
_vscode_base="/scratch/user/$VSCODE_USER"
[ -d "$_vscode_base" ] || _vscode_base="/home/$VSCODE_USER"
if [ "$_vscode_base" = "/home/$VSCODE_USER" ]; then
  _vscode_binds="/home/$VSCODE_USER:/home/$VSCODE_USER"
else
  _vscode_binds="$_vscode_base:$_vscode_base,/home/$VSCODE_USER:/home/$VSCODE_USER"
fi
export VSCODE_CONTAINER="${VSCODE_CONTAINER:-$_vscode_base/vscode.sif}"
export VSCODE_BIND_PATHS="${VSCODE_BIND_PATHS:-$_vscode_binds}"
export VSCODE_ROOT_DIR="${VSCODE_ROOT_DIR:-$_vscode_base}"
# what you install and set in VS Code (extensions, settings, the folder you had open) lives here, between sessions
export VSCODE_DATA_DIR="${VSCODE_DATA_DIR:-$VSCODE_ROOT_DIR/.code-server-data}"
# a folder to open every time; empty: the one you had open last
export VSCODE_START_FOLDER="${VSCODE_START_FOLDER:-}"
# the code-server image's tag (docker://codercom/code-server:TAG); pin one to get the same version everywhere
export VSCODE_IMAGE_TAG="${VSCODE_IMAGE_TAG:-latest}"
unset _vscode_base _vscode_binds
