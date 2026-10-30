# SSH keys and first-login setup

Open a cluster's **SSH keys** tab (`#/c/NAME/ssh`). It is visible without an agent tunnel;
old `#/c/NAME/agents/keys` bookmarks open the same tab. Setup uses the active SSH login,
while general cluster key management uses the owner REST tunnel.

## This computer → cluster

After the first password/MFA login, Overview offers **Set up login key…**. Choose an identity
pair already in this computer's `~/.ssh`, or explicitly create a dedicated Ed25519 login key.
New keys have no passphrase and are stored locally with private-file mode 600. Existing keys
are never overwritten; encrypted identities use the normal SSH agent or passphrase prompt.

**Install login key** sends only the public key over the active login and adds it to the cluster's
`~/.ssh/authorized_keys`. It preserves other lines and existing restrictions for that fingerprint.
The console saves `-i /local/path/to/key` in this cluster's profile for future hpclib logins,
retaining ports, jump hosts, and other login arguments. Your ordinary SSH config is unchanged.
For standalone SSH, use that identity with `ssh -i /local/path/to/key [other login options] user@host`.

Setup does not close the current login or claim to have tested a fresh connection. The cluster
must permit this authorization file and key type, and may still require MFA or separate site
registration. Sites marked `ssh_keys: site-managed` disable these mutations. No saved password,
REST tunnel, remote hpclib installation, or agent token is required. The remote helper needs
Python 3.7+ and native `ssh-keygen`, as compute setup does.

If remote installation fails after creating a local key, that pair is retained for retry. Choose
**Reload identities**, select it, and install again. A successful remote authorization followed
by a local save conflict is reported explicitly; refresh and retry. Switching the selected
identity does not revoke older authorizations.

## Login node → compute nodes

After login, choose **Set up compute SSH** on the same tab. The old **Agents → Tunnel and setup →
Set up SSH** control remains available. The console runs the same
command available in the shell:

```bash
setup_cluster_ssh -p 22 user@login.example
```

This creates `~/.ssh/id_ed25519_hpclib_compute` and its public file if neither exists,
and adds that public key to `~/.ssh/authorized_keys`. Login and compute nodes must use the
same account and shared home directory. No keys are copied between nodes or returned to
your machine. The cluster's SSH service must permit public-key authentication with that
authorization file; setup cannot change the site's SSH policy or filesystem mounts.

Repeated setup preserves the pair and does not duplicate authorization. It recovers a
missing public file from an existing unencrypted private key. An orphan public file,
mismatched pair, encrypted compute private key, or invalid key produces an error without
replacing key contents. Existing restrictions on an already-authorized fingerprint are
preserved; those restrictions may still prevent the requested tunnel connection.
The directory is mode 700; compute private key and authorization file are mode 600.

The helper runs over the current login, so setup needs no REST tunnel. It uses the Python
launcher selected by `setup_agents`, or `python3` when that launcher is absent (Python 3.7+
and OpenSSH `ssh-keygen` required). **Update hpclib** on the cluster so its `connect_to_job`
uses the new identity, then restart existing tunnels.

## Compute connections

`connect_to_job` uses the compute key with `IdentitiesOnly=yes` when it exists in the login node's home. A caller's
`-i` takes precedence. To change or disable the automatic identity, put this in the cluster's
`~/.local/tunnels/config.sh`:

```bash
HPCLIB_COMPUTE_IDENTITY_FILE="$HOME/.ssh/my_compute_key"
# Or disable the automatic identity selection:
# HPCLIB_COMPUTE_IDENTITY_FILE=
```

OpenSSH still reads the user's SSH configuration and agent. Existing compute host-key
checking remains unchanged; `HPCLIB_COMPUTE_HOST_KEYS=accept-new` remains an opt-in setting.
SSH options now retain their original argument boundaries, including identity filenames
and `-o` values with spaces. One remote command argument is shell source; several command
arguments are individually quoted before OpenSSH joins them.

`CONNECT_TO_JOB_SSH_OPTS` remains a whitespace-separated legacy defaults string. For
additional options containing spaces, set a Bash array in the same configuration:

```bash
CONNECT_TO_JOB_SSH_ARGS=(-o "IdentityFile=$HOME/.ssh/another key")
```

## General key management

The **Keys stored on the cluster** section needs a live REST tunnel and the cluster owner token. It supports:

- Listing named identity pairs, key types, SHA256 fingerprints, comments, and public keys.
- Creating Ed25519 or RSA-3072 pairs, copying public keys, changing public-file comments,
  and deleting a selected pair. Generated keys have no passphrase for unattended services.
- Adding, editing, or removing one `authorized_keys` entry, including its SSH restrictions.
  Other lines, comments, and unrecognized entries are retained.
- Choosing identity keys for host names or patterns, with optional user, hostname, and port.
  This is useful for GitHub and private Git repositories. Copy the public key into
  [GitHub's SSH key settings](https://github.com/settings/keys), then bind `github.com` to
  that key with user `git`. Registering the key is a separate user action.

Private key material is never exported or accepted by these endpoints. Existing encrypted
identities can be listed and bound; using them requires the user's normal SSH agent or
passphrase handling. Place imported private identities through your existing SSH/file tools.
Registry tokens/passwords and automatic GitHub registration are outside this feature.

Managed host blocks live in `~/.ssh/hpclib_keys.conf`. The first binding prepends an Include
and `Host *` reset to `~/.ssh/config`, retaining all original contents. Managed blocks have
precedence for their user/hostname/port settings. OpenSSH adds IdentityFile directives, so
identities already listed in matching native configuration can still be tried. Other native
config blocks are shown read-only. Referenced keys cannot be deleted until their parsed
host bindings are removed. Included third-party config files are not expanded or edited.
The configuration parser displays simple Host blocks; it does not evaluate Match, Include,
token expansion, or the final effective SSH configuration. The native client does that.
Include and home-relative identity paths follow [OpenSSH's configuration rules](https://man.openbsd.org/ssh_config).

## REST interface

Every route below requires the owner token (`*` scope). Standard agent scopes, including
`files:write`, cannot access it. All mutations also honor `HPC_REST_DISABLE_FILE_CHANGES`.
The console proxies these at `/api/clusters/NAME/rest/admin/ssh/...` using its existing
authenticated session and owner connection.

| Method and route | JSON fields | Result |
| --- | --- | --- |
| `POST /admin/ssh/setup` | `{}` | compute setup result and public metadata |
| `GET /admin/ssh/keys` | none | `directory`, `keys`, `errors` |
| `POST /admin/ssh/keys` | `name`, optional `algorithm` (`ed25519` or `rsa`), `comment` | `key` metadata |
| `PUT /admin/ssh/keys` | `name`, `comment` | updated public-file comment and `key` metadata |
| `DELETE /admin/ssh/keys` | `name` | `deleted` name |
| `GET /admin/ssh/authorized` | none | `entries`, `errors`, file `revision` |
| `POST /admin/ssh/authorized` | `line`, optional `revision` | append a public authorization; identical line is idempotent |
| `PUT /admin/ssh/authorized` | `id`, `line`, optional `revision` | replace only the selected line |
| `DELETE /admin/ssh/authorized` | `id`, optional `revision` | remove only the selected line |
| `GET /admin/ssh/hosts` | none | `hosts`, managed-file `revision` |
| `PUT /admin/ssh/hosts` | `host`, `key`, optional `user`, `hostname`, integer `port`, `revision` | create/update one managed host block |
| `DELETE /admin/ssh/hosts` | `host`, optional `revision` | remove one managed block |

Keys use simple filenames under `~/.ssh`, with paths, reserved filenames, symlinks, foreign
ownership, special files, and oversized files rejected. OpenSSH validates key blobs and
generates pairs. Writes use a shared advisory lock and staged replacement. Revision values
detect stale edits; the console sends them for authorizations and host bindings. Exact duplicate
authorization entries are reported as ambiguous rather than removed together. Manual editors
do not take the advisory lock; refresh before editing after an external file change.
Deleting a pair does not remove remote registrations or authorization entries automatically.

The authenticated local console exposes `GET /api/clusters/NAME/ssh/login-keys` (local public metadata,
the suggested key name, and the configured identity) and `POST /api/clusters/NAME/ssh/login-key`
with `{"key": "simple-filename", "create": false}`. The latter needs an active login; `create: true`
creates a new Ed25519 pair without a passphrase before authorization. Key paths and private-key
uploads are rejected. On success the profile's `ssh_login_key` records public metadata, and its
`login` arguments select the identity. These endpoints belong to the local console, not the REST/MCP
agent interface. SSH bootstrap uses batch mode so an expired master cannot start a password prompt.

The console also exposes `POST /api/clusters/NAME/ssh/setup` with `{}`. It requires an active
login and starts a tracked `ssh_setup` operation; read its state and log through the existing
`GET /api/clusters/NAME/operation` route. This bootstrap action does not use the REST server,
so the server's file-change switch does not control it.
