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