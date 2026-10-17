# Apps in the interface

Besides the agents' tunnel, the console runs **apps**: programs on a cluster that you open in your browser. Each
app has a **Sessions** page with one row per cluster. Apps use the same clusters and the same ssh login as the
Agents pages, so add a cluster and log in there first ([Getting started](getting-started.md), steps 2 and 3).
You don't need to set a cluster up for agents to use its apps.

## A session row

| Element | What it does |
| --- | --- |
| state | **not installed** (until **Install** in Settings…), **stopped**, **starting**, **queued** (with SLURM's reason), **running**, **error** |
| **Start** | start the app's tunnel; for most apps this submits a SLURM job |
| **Open** | open the app in a new browser tab, signed in where the app allows it |
| **Stop** | end the tunnel and cancel its job |
| **Log** | the tunnel's and the job's output |
| **Settings…** | whether the app is installed (with **Check** and **Install**), the job's resources and the app's own settings for this cluster |

**Start** is refused while the app is known to be missing on the cluster: open **Settings…** and **Install** first.

### Install

The top of the Settings panel says whether what the app needs is installed on the cluster (**installed**, **not
installed**, **install status unknown**) and what the cluster reported, such as the image's path. It is checked
automatically once you are logged in.

| Button | What it does |
| --- | --- |
| **Check** | check again (runs the tunnel's `install.sh --check` on the cluster) |
| **Install** / **Reinstall** | install what the app needs; its output appears under the row. Some apps call this **Update** once installed |

Each app on each cluster gets ports of its own, chosen once and kept, so apps never collide with each other,
with the agents' tunnel or with other users. Starting an app on a port an earlier session left behind clears
that port first; see [Ending tunnels](../architecture.md#ending-tunnels-and-stale-ports).

### Settings

The settings form has up to three parts:

- **Job**: time limit, memory, CPUs and partition for the app's SLURM job.
- **App settings**: the tunnel's own settings, such as where VS Code's container image is kept. They are saved
  on the cluster (`~/.local/tunnels/settings/TUNNEL.sh`) the next time you Check, Install or Start, so the
  job and the installer use the same values. A blank field means the tunnel's default; a package's default
  shows as the placeholder.
- **Secrets** (only apps that declare them): password fields. Values go to the cluster over the ssh login on
  standard input and are stored there in a private file; the console keeps only whether each is set.

Settings apply the next time the app starts.

### Second password prompts

When the app's job starts, the login node connects to the compute node SLURM gave it. On some clusters that asks
for your password again, and the first time a new compute node is reached, ssh asks whether to trust its host
key. The page notices either prompt and opens a dialog wherever you are. **Not now** leaves a reminder on the
row.

## JupyterLab

Runs JupyterLab in a SLURM job. **Open** goes straight in with the token Jupyter printed.

Settings: the conda environment to activate, modules to load first, or a uv/pixi project directory whose
environment has JupyterLab. **Check** looks for `jupyter` there. With a project set, **Install** adds JupyterLab
to it (`pixi add` in a pixi project, otherwise a uv environment in `PROJECT/.venv`); for a conda environment or
modules, install JupyterLab there yourself.

## VS Code

Runs code-server (VS Code in the browser) from a Singularity image in a SLURM job. **Install** pulls the image.
code-server asks for a password: **Copy password** copies it, then **Open**.

Settings: where the image is kept (`VSCODE_CONTAINER`), the start directory, and directories to bind into the
container.

## PAI

The proto-auto-interface database and its web interface. It is **shared**: Start connects to a database job
that is already running (yours or one another tunnel started), and only submits a new one if there is none. The
row's toolbar names the job serving the database and whether this tunnel started it.

- **Stop** only disconnects; the database job keeps running for the next session.
- **End database job** cancels that job (after asking).
- Once installed, the install button is **Update**: it fast-forwards the checkout from its git remote and pulls
  the app's image again. A running database job keeps the old code until you end it and Start again.

Settings: the install directory, the repository to clone, the image to pull, whether development endpoints are
on, and **Bind source** (run your checkout's code instead of the image's).

## Data transfer

Moving files between a cluster and an SMB file server with rclone. Per cluster, one row:

- **Install** pulls the data-transfer-tools image.
- **Start** runs a web GUI on the cluster's **login node** (no job), reached through your login: by default
  rclone's own, for browsing and copying between the SMB server (`smb`), the cluster's files (`cluster`), and any
  remotes a settings package adds. **Open** signs you in to it automatically; **Copy username** and **Copy
  password** are there if the browser asks anyway. **Stop** ends it.
- **Kerberos log in (kinit)** gets a ticket with `kinit`, where the cluster has Kerberos; the GUI and jobs use it
  while it lasts. It can run while the GUI does.
- Otherwise **Save password for sync jobs** stores the SMB password on the cluster (mode 600, in rclone's
  reversible encoding) so batch transfers (`smbshell submit`) can run unattended.

Without a ticket or a saved password, Start asks for the SMB password, which is kept only in memory until Stop.

Settings: the SMB server, the share and folder paths are relative to, user name, domain, Kerberos realm, how to
sign in, which **GUI** Start opens, and where the image is kept. GUI is `rclone` (the default), the name of a GUI
a settings package provides, or a program on the cluster (see `smbshell --help`, and the README's Data transfer
section, for what such a program gets and must print).

## Adding apps and settings from packages

Apps that don't belong in hpclib itself (a group's file server, a project's test instance) come as **package**
zip files. Click **Add App or Settings** at the top right:

1. Choose the zip. The console checks it and shows what it adds: apps, tunnels, and default settings with their
   values.
2. **Install** it. Its apps join the top bar.
3. The packaged tunnel and settings files go to a cluster the next time you Check, Install or Start the app
   there.

The same dialog lists installed packages and removes them. A package can't replace hpclib's own apps or another
package's. Its scripts run on your clusters as you, so install only packages you trust.

To build one, see [Extending hpclib](../extending.md#console-packages).
