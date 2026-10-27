#!/usr/bin/env bash
# hpclib-install: --check --force
# Nothing to install: the Workspace needs Python 3.6+ (standard library only) on the login node.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$here/user.sh"
if version=$("$WORKSPACE_PYTHON" -c 'import sys; assert sys.version_info >= (3, 6); print(sys.version.split()[0])' 2> /dev/null); then
  echo "installed: the Workspace runs with $WORKSPACE_PYTHON (Python $version)"
  exit 0
fi
echo "not installed: $WORKSPACE_PYTHON isn't Python 3.6 or newer here (set WORKSPACE_PYTHON, or load a Python module in ~/.bashrc)"
exit 1
