# Not a port-forwarding tunnel: SMB file transfers with rclone, from the data-transfer-tools container.
# `smbshell` (smbshell.sh here) runs them on the login node; `smbshell submit` runs them as SLURM jobs
# (sbatch_script.sh); `smbshell gui` serves a web GUI on the login node (the console's Data transfer Start).
# Settings (setup_tunnel.sh --set, the console's Data transfer settings):
TUNNEL_SETTINGS="SMB_HOST SMB_ROOT SMB_USER SMB_DOMAIN SMB_REALM SMB_SPN SMB_AUTH SMB_GUI SMB_IMAGE SMB_IMAGE_SOURCE"
DEFAULT_SBATCH_ARGS="--time=0-4:00:00 --mem=2gb --ntasks=1 --cpus-per-task=2"
