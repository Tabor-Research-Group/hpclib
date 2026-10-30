# The data-transfer settings with their defaults; sourced by smbshell.sh, install.sh and sbatch_script.sh.
#   SMB_HOST           the SMB server, e.g. files.example.edu or 10.0.0.12 (just the host)
#   SMB_ROOT           a SHARE/FOLDER that paths are relative to, e.g. research/our_group (optional)
#   SMB_USER           your user name there (default: your user name here)
#   SMB_DOMAIN         its (NetBIOS) domain, e.g. EXAMPLE, if it wants one
#   SMB_SPN            optional cifs/NAME override; otherwise a definite unknown FQDN principal is retried with
#                      the short name on a read-only probe, keeping the original host's destination address
#   SMB_REALM          the Kerberos realm, e.g. AUTH.EXAMPLE.EDU (kinit defaults to krb5.conf; transfers default
#                      to the current ticket's realm instead of guessing from the SMB server's DNS suffix)
#   SMB_AUTH           legacy setting, ignored: CLI/jobs always use Kerberos; the optional GUI uses a session password
#   SMB_GUI            what smbshell gui serves: rclone (default; rclone's web GUI), NAME for NAME.gui.sh in
#                      settings/data-transfer.d/ (from a settings package), or a program (see smbshell --help)
#   SMB_IMAGE          the data-transfer-tools image (default /scratch/user/USER/images/data-transfer-tools.sif)
#   SMB_IMAGE_SOURCE   where install.sh gets it (default docker://ghcr.io/tabor-research-group/data-transfer-tools:latest):
#                      oras://..., docker://..., library://..., a .def file to build (singularity build --fakeroot),
#                      or a .sif to copy
#   SMB_CREDENTIALS    old saved-password path, used only by forget-credentials; never read for sign-in
#   SMB_KRB5CCNAME     the app's FILE ticket cache, shared with jobs and explicitly mounted in the image
#                      (default ~/.config/hpclib/smb/krb5cc); a path or FILE:/path, never KCM:/KEYRING:
#   SMB_KRB5_CONF      optional host Kerberos profile(s), colon separated, instead of KRB5_CONFIG or /etc/krb5.conf
_smb_settings="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/settings/data-transfer.sh"
if [ -f "$_smb_settings" ]; then
  . "$_smb_settings"
fi
SMB_USER="${SMB_USER:-$(id -un)}"
SMB_AUTH="${SMB_AUTH:-auto}"
SMB_GUI="${SMB_GUI:-rclone}"
SMB_IMAGE="${SMB_IMAGE:-/scratch/user/$(id -un)/images/data-transfer-tools.sif}"
SMB_IMAGE_SOURCE="${SMB_IMAGE_SOURCE:-docker://ghcr.io/tabor-research-group/data-transfer-tools:latest}"
SMB_CREDENTIALS="${SMB_CREDENTIALS:-$HOME/.config/hpclib/smb/credentials}"
SMB_KRB5CCNAME="${SMB_KRB5CCNAME:-$HOME/.config/hpclib/smb/krb5cc}"
export SMB_HOST SMB_ROOT SMB_SPN SMB_USER SMB_DOMAIN SMB_REALM SMB_AUTH SMB_GUI SMB_IMAGE SMB_IMAGE_SOURCE SMB_CREDENTIALS SMB_KRB5CCNAME
