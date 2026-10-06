# The data-transfer settings with their defaults; sourced by smbshell.sh, install.sh and sbatch_script.sh.
#   SMB_HOST           the SMB server, e.g. files.example.edu or 10.0.0.12 (just the host)
#   SMB_ROOT           a SHARE/FOLDER that paths are relative to, e.g. research/our_group (optional)
#   SMB_USER           your user name there (default: your user name here)
#   SMB_DOMAIN         its (NetBIOS) domain, e.g. EXAMPLE, if it wants one
#   SMB_SPN            the server's Kerberos name, if not cifs/SMB_HOST (smbshell find-spn finds it)
#   SMB_REALM          the Kerberos realm for kinit, e.g. AUTH.EXAMPLE.EDU (default: krb5.conf's default realm)
#   SMB_AUTH           auto (Kerberos when you have a ticket, else the saved password, else ask), kerberos or password
#   SMB_IMAGE          the data-transfer-tools image (default /scratch/user/USER/images/data-transfer-tools.sif)
#   SMB_IMAGE_SOURCE   where install.sh gets it (default docker://ghcr.io/tabor-research-group/data-transfer-tools:latest):
#                      oras://..., docker://..., library://..., a .def file to build (singularity build --fakeroot),
#                      or a .sif to copy
#   SMB_CREDENTIALS    the saved password for jobs (default ~/.config/hpclib/smb/credentials, mode 600)
#   SMB_KRB5CCNAME     the Kerberos ticket cache, shared with jobs (default ~/.config/hpclib/smb/krb5cc)
_smb_settings="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/settings/data-transfer.sh"
if [ -f "$_smb_settings" ]; then
  . "$_smb_settings"
fi
SMB_USER="${SMB_USER:-$(id -un)}"
SMB_AUTH="${SMB_AUTH:-auto}"
SMB_IMAGE="${SMB_IMAGE:-/scratch/user/$(id -un)/images/data-transfer-tools.sif}"
SMB_IMAGE_SOURCE="${SMB_IMAGE_SOURCE:-docker://ghcr.io/tabor-research-group/data-transfer-tools:latest}"
SMB_CREDENTIALS="${SMB_CREDENTIALS:-$HOME/.config/hpclib/smb/credentials}"
SMB_KRB5CCNAME="${SMB_KRB5CCNAME:-$HOME/.config/hpclib/smb/krb5cc}"
export SMB_HOST SMB_ROOT SMB_SPN SMB_USER SMB_DOMAIN SMB_REALM SMB_AUTH SMB_IMAGE SMB_IMAGE_SOURCE SMB_CREDENTIALS SMB_KRB5CCNAME
