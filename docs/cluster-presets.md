# Installable cluster configurations

A console package may contain `clusters` without adding a tunnel app or executable scripts.
Install its ZIP through **Add App or Settings**; the preset appears under
**Agents → Clusters → Add cluster → Configuration**. The user supplies their username,
absolute work directory and allocation, and can edit tunnel resource defaults.

```json
{
  "format": 1,
  "name": "example-cluster",
  "version": "1.0.0",
  "clusters": {
    "example": {
      "title": "Example cluster",
      "host": "login.example.edu",
      "port": 22,
      "documentation_url": "https://example.edu/cluster-guide",
      "ssh_keys": "hpclib",
      "defaults": {
        "rest_on": "job",
        "connection_hours": 12,
        "tunnel_args": ["--partition=normal", "--time=01:00:00", "--mem=4G", "--cpus-per-task=1"]
      }
    }
  }
}
```

Preset IDs use lowercase app-style identifiers. `host` is a hostname without a username or
SSH flags. Port defaults to 22. Optional description is displayed in the add form;
documentation links must be HTTPS URLs without credentials. The existing validated tunnel
resource options are allowed in `tunnel_args`; no arbitrary shell commands are accepted.
Defaults may set `rest_on` to job/login and connection hours to an integer from 1 to 168.

The optional `defaults.work_dir` prefills the add form. `{username}` in that path,
`defaults.app_settings` or `defaults.rest_config` is replaced with the username supplied
when adding a profile. The suggested work directory follows username changes until the
user edits it. The API also accepts the suggested directory when `work_dir` is omitted.

`defaults.app_settings` maps app IDs to the same fields as the app settings API:
`tunnel_args`, `conda_env`, `modules`, `install_modules`, `project`, and `settings`. These are copied into the
new profile, without ports, installation status, tokens or cached authentication state.
An app package must be installed before importing preset defaults for that app, so the
console can verify its declared non-secret fields. Unknown apps and undeclared or secret
settings are refused before package installation or profile creation. This does not
install app packages or contact a cluster.

`defaults.rest_config` holds the REST server's editable configuration sections: `limits`,
`cluster_notes`, `poll_interval`, `sandbox`, `environments`, and `environment`. The REST
server's own validators check it; database/token paths and executable module commands are
not portable defaults. The snapshot remains in the profile as `setup_config` after package
removal. **Set up…** writes a private local `setup-config.json` and passes it to native
`setup_agents --config`. A fresh remote install uses these defaults; reruns keep an existing
remote config unless **Rebuild** is selected. Existing profiles are not changed by package
installation. To adjust a live limit, use the owner settings API instead.

Cluster presets never accept passwords, passphrases, tokens, private keys or other
credentials. Credential-like field names are rejected recursively, including REST
environment variables, and embedded URL credentials, authorization values and private-key
material are refused. Errors do not echo rejected values. The same checks run when reading
previously installed presets and before saving local app settings or a setup configuration.
An app's explicitly declared secret fields cannot be used as defaults, even under a name
that does not look like a credential. Configuration text is for public settings only.

`SMB_AUTH: "password"` is an authentication-method choice, not a password. Actual SSH
passwords and MFA codes are supplied during login, held in memory only and cleared after
use. No credential-storage field or remembered-password option is added by presets.

For example, `"rest_config": {"limits": {"max_concurrent_jobs": 8}}` caps API-managed active
job slots at eight. Each ordinary job uses one slot; an array uses its throttle. The cap is
shared across tokens for this REST server, and does not count jobs submitted outside it.

Installing a package does not connect, generate keys, create a personal profile, or submit
jobs. Adding a preset creates a profile through the existing profile store, with fresh ports.
The chosen allocation is for the agent service's tunnel job; submitted templates keep their
own account/resource configuration. The initial work directory is prefilled for **Set up…**.
Duplicate username/host profiles are refused. Preset IDs cannot conflict across packages.

`ssh_keys: "site-managed"` preserves sites that administer their own SSH files. It disables
the console's compute-key setup and all SSH-write proxy requests. The SSH keys page permits
inspection and copying. This is a console profile policy; it does not reconfigure the remote
REST server or change the user's ability to manage files through other tools.

Profile metadata snapshots the preset's package, version, title, description, guide link and
SSH policy. Package replacement/removal changes available choices and preserves configured
profiles, tokens and personalized settings.

## Console API

These routes require the console session bearer token:

| Route | Purpose |
| --- | --- |
| `GET /api/cluster-presets` | installed preset list, including defaults and package provenance |
| `POST /api/cluster-presets/ID` | create a local profile from `username`, `work_dir`, optional `account`, `tunnel_args`, `jump` |
| `POST /api/clusters/NAME/login/answer` | send `code` and `challenge` for the currently waiting login |

Login status reports `code_required` with a prompt and challenge ID when SSH asks for a
numeric MFA code. Codes must contain 6–12 digits, remain in memory only until used, and are
bound to the current challenge. Missing, expired, wrong-profile or reused challenges are
refused. Password/Duo login behavior is retained. Unknown host keys still use the existing
first-connection trust workflow: the console asks the user to accept the key in a terminal.

Basic cluster presets are available with the hpclib 0.2.62 support patch. Portable REST/app
defaults and username directory suggestions require the hpclib 0.2.63 support patch.
The credential-default rejection checks require the hpclib 0.2.65 follow-up patch.
Earlier versions reject unsupported fields instead of silently dropping configuration.
