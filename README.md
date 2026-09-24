# hpclib

A collection of shell scripts and python TCP servers to simplify the process of developing code
across different HPC systems

## installation

On a login node run

```commandline
pip install --target=$SCRATCH --no-dependencies --upgrade --ignore-installed git+https://github.com/Tabor-Research-Group/hpclib
```

## hpclib.sh

The core library for simplifying HPC workflows. Provides assorted bash functions.

## servers

A set of TCP-socket based servers to allow users to communicate with login nodes from compute nodes and containers

## tunnels

The primary utility package. Provides a generic architecture for creating port-forwarding tunnels to programs like
`Jupyter` or `coder:code server` that expose interfaces via ports.
Runs jobs via `sbatch` so that processes can take advantage of compute nodes and starts servers for further `git` and
`slurm` communication with the login node.

To create and connect to a Jupyter session, from your local machine run

```commandline
ssh -L 8895:8895 <username>@<host> ./hpclib/tunnels/start_tunnel jupyter -P 8895
```

where `8895` is just an example port.

Then by going to `http://localhost:8895` you will see your Jupyter notebook appear

### Tunnel Configuration

Source `hpclib/hpclib.sh` to use the tunnel management functions. `resolve_tunnel NAME`
prints the first tunnel directory with an `sbatch_script.sh`. `resolve_tunnel_file FILE NAME`
prints a file from the first matching tunnel directory, falling back to the bundled
shared file (for example `configure_job.sh`). `start_tunnel.sh` uses these same rules.

`HPCLIB_TUNNEL_PATH` is a colon-separated list of parent directories, searched in
order. By default it is `$HPCLIB_TUNNEL_INSTALL_LOCATION:$HPCLIB_DIR/tunnels`.
`HPCLIB_TUNNEL_INSTALL_LOCATION` defaults to `~/.local/share/hpclib/tunnels`.
Set either variable before sourcing `hpclib.sh` to change the defaults.
The older `USER_TUNNEL_DIR` and `HPCTUNNELS_DIR` variables still provide defaults
when set.

Download or clone a tunnel folder, then install it on the HPC login node:

```bash
source /path/to/hpclib/hpclib.sh
install_tunnel ./my-tunnel
install_tunnel --target /another/tunnel/root ./my-tunnel
```

`--target` names a parent directory; the installed path is
`TARGET/my-tunnel`. Existing installations are left untouched. When a tunnel
includes `install.sh`, it runs during installation; if that script fails, the
tunnel folder is not installed. A custom target must be included in
`HPCLIB_TUNNEL_PATH` to be found by name in future sessions.

The Jupyter and VS Code tunnels require some level of configuration to get the resources installed on the HPC system.

**Flask**: install Flask in the Conda environment used by the batch job. Pass
the application import path after `--`; arguments before `--` remain tunnel or
`sbatch` options, while arguments after it reach `sbatch_script.sh`:

```bash
launch_tunnel -P 5050 user@login.example flask \
  --chdir=/scratch/user/me/project \
  --env=CONDA_ENVIRONMENT=myenv \
  -- 'mypackage.web:app'
```

This forwards local port 5050 to Flask on compute-node port 5000. The Flask
tunnel runs `flask --app APP run` bound to the compute node's loopback address,
using `PROCESS_PORT` (5000 by default), with the debugger and reloader off.
`FLASK_APP` can be set instead of supplying an app argument. Additional
arguments after the app name are passed to `flask run`. This uses Flask's
development server for interactive work.

**VS Code**: this runs the `codercom/code-server` container. Installing the
bundled tunnel with `install_tunnel /path/to/hpclib/tunnels/vscode` runs its
`install.sh`, which pulls the image with Singularity to the path the tunnel uses:
`/scratch/user/<username>/vscode.sif` by default. Set `VSCODE_CONTAINER` before
installation and tunnel launch to use another path. Singularity must be available
on the login node.

**Jupyter**: this requires jupyter lab to be installed in whatever `conda` environment one uses by default, and requires
that `conda` is set up when loading the environment from `~/.bashrc`

To configure that, one will either need to load a module that supplies conda in `~/.bashrc` or install something like
`miniconda` in one's scratch directory

**NGL**: this, like Jupyter, requires a package to be installed in the base conda environment, although this time it's `mdsrv`
from the people who make ngl
