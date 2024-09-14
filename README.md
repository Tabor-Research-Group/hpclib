# hpclib

A collection of shell scripts and python TCP servers to simplify the process of developing code
across different HPC systems

## installation

On a login node run

```commandline
cd ~
git clone https://github.com/Tabor-Research-Group/hpclib.git
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

The Jupyter and VS Code tunnels require some level of configuration to get the resources installed on the HPC system.

** VS Code **: this runs off the `coder:code-server` container, the default installation location is currently
`/scratch/user/<username>/vscode.sif`, and to install there we run

```commandline
cd /scratch/user/<username>
sinularity pull vscode.sif docker://codercom/code-server:latest 
```

** Jupyter **: this requires jupyter lab to be installed in whatever `conda` environment one uses by default, and requires
that `conda` is set up when loading the environment from `~/.bashrc`

To configure that, one will either need to load a module that supplies conda in `~/.bashrc` or install something like
`miniconda` in one's scratch directory

