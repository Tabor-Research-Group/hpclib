# hpclib

A collection of shell scripts and python TCP servers to simplify the process of developing code
across different HPC systems

## installation

On a login node run

```commandline
pip install --target=$SCRATCH --no-dependencies --upgrade --ignore-installed git+https://github.com/Tabor-Research-Group/hpclib
```

or, from a local copy of `hpclib`, install it on the cluster over the same persistent connection `pssh`/`psftp` use:

```bash
source /path/to/hpclib/hpclib/hpclib.sh
install_hpclib user@login.example                     # installs to ~/hpclib on the cluster
install_hpclib -i ~/.ssh/id_hpc -J jump.example user@login.example
install_hpclib --target /scratch/user/me/hpclib user@login.example
install_hpclib --check user@login.example             # report what would happen
```

`install_hpclib` takes the same login arguments as `pssh`/`psftp` (any `ssh` options, then `[user@]host`).
It installs only when the cluster's copy is missing or older than the local one, comparing the `HPCLIB_VERSION`
set in `hpclib/hpclib.sh` (which `setup.py` also reads), so bump that version for each release. A newer or equal
copy is left alone unless `--force` is given, and a directory that isn't an hpclib install is never replaced
without `--force`. The upload is unpacked and checked beside the target before it is swapped in, so a failed
transfer leaves the old copy in place, and the replaced copy is kept as `TARGET.previous`. Relative targets are
relative to the remote home directory. `launch_tunnel` starts tunnels from
`$HPCLIB_REMOTE_INSTALL_LOCATION` (default `hpclib`), so set that variable when installing somewhere else.

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

**REST**: a dependency-free (standard library only) JSON API for SLURM and file
access, served by `hpclib/servers/rest_server.py`. Arguments after `--` go to the
server; `--allow` (repeatable) restricts file access to those directories and
their children, and without it file access is unrestricted:

```bash
launch_tunnel -P 5050 user@login.example rest \
  --chdir=/scratch/user/me/project \
  -- --allow /scratch/user/me/project --allow /scratch/user/me/data
```

Every request needs `Authorization: Bearer <token>`. On first launch the server
writes a random token to `~/.local/tunnels/rest_token` (mode 600) on the cluster
and reuses it afterwards; copy it to your machine once, and delete the file to
rotate it. `HPC_REST_TOKEN_FILE` points at a different file. The server refuses to
start if the token file is readable by other users.

```bash
TOKEN=$(ssh user@login.example cat .local/tunnels/rest_token)
H="Authorization: Bearer $TOKEN"
curl -H "$H" localhost:5050/health
curl -H "$H" 'localhost:5050/slurm/squeue?arg=--me'
curl -H "$H" -X POST localhost:5050/slurm/sbatch \
  -d '{"args": ["--parsable", "run.sh"], "cwd": "jobs/a"}'
curl -H "$H" -T input.xyz 'localhost:5050/files/content?path=jobs/a/input.xyz&parents=1'
curl -H "$H" -o out.log 'localhost:5050/files/content?path=jobs/a/out.log'
```

SLURM routes are `POST /slurm/{sbatch,squeue,sacct,scontrol,scancel}` with a JSON
body `{"args": [...], "cwd": "...", "input": "..."}` (`input` is sent on stdin, so
`sbatch` can take a script inline), plus `GET` for `squeue` and `sacct` using
repeated `arg=` query parameters. They return `returncode`, `stdout` and `stderr`
with HTTP 200 whenever the command ran. File routes are `GET /files?path=` (list or
stat), `GET`/`PUT /files/content?path=` (raw download/upload; `overwrite=1`,
`parents=1`), `POST /files/mkdir?path=` and `DELETE /files?path=` (files, symlinks
and empty directories only). Relative paths resolve against the first `--allow`
directory, or the job's working directory. Uploads need a `Content-Length` and are
capped by `--max-upload` (1G by default); use `rsync` over SSH for large or
many files. Pass `--disable-file-changes` (or set
`HPC_REST_DISABLE_FILE_CHANGES=1`) to refuse uploads, `mkdir` and deletes with
403 while keeping downloads, listings and the SLURM routes; jobs submitted
through `sbatch` can still write files when they run. The whitelist limits the file routes and the `cwd` of SLURM commands;
it does not sandbox the jobs those commands submit. While the job is queued the
forwarded port serves the HTML waiting page, so clients should wait for
`/health` to return JSON.

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
