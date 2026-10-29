# Configure GitHub repo creation on new project

You'd like `uxon new myproj --git-remote default` (or the TUI's
"Create new project" flow) to create a fresh GitHub repo before
launching the agent.

```toml
git_create_enabled = true

[[git_remote_profiles]]
name       = "personal"
host       = "github.com"
owner      = "your-username"
auth       = "gh"               # uses `gh repo create` under creds_user
creds_user = "your-os-user"     # credential owner for repository creation
visibility = "private"

[[git_remote_profiles]]
name       = "acme-org"
host       = "github.com"
owner      = "acme"
auth       = "token"            # fine-grained PAT via REST API
creds_user = "your-os-user"
token_file = "/run/secrets/uxon-acme-token"
visibility = "private"

[launch]
enabled_profiles = ["claude"]
default_profile = "claude"

[launch.profiles.claude]
agent = "claude"
allowed_git_remote_profiles = ["personal", "acme-org"]
default_git_remote_profile = "personal"
```

## How it composes

- `uxon` only ever creates repos for profiles in this whitelist —
  no `<owner>` outside the table is reachable.
- A launch profile must allow the git-remote profile. `--git-remote
  default` uses the selected launch profile's effective
  `default_git_remote_profile`; a matching `[[launch.path_rules]]`
  entry may narrow the allowed/default git profiles further.
- `auth = "token"` reads the PAT from `token_file` under
  `creds_user`. The token is held in memory for the preflight or creation
  operation, never logged, never echoed in `--dry-run`.
  Configure the [token permissions](../../reference/configuration.md#git_remote_profiles-table-array)
  for the selected owner. Organization membership listing is not required;
  the provider authorizes the actual repository-creation request.
- `creds_user` is the OS user whose credentials are used for the
  *create* step. Local `git init` / `commit` / `push` always run
  under the launch user. `creds_user` defaults to the launch
  user.
- `uxon doctor` prints one line per profile with `ok` /
  `warn:<reason>` for: passwordless sudo to `creds_user`,
  presence of `gh`, login status or `token_file` readability. It
  never attempts the create call.
- The CLI is non-interactive: `uxon new` only touches git when
  you pass `--git-remote <profile>`. The TUI prompts.

If a step fails, the local `.git` is left in place for
inspection. The error names which stage failed: `preflight` /
`local_init` / `remote_create` / `push`.

## Audit footprint

Every successful (or failed) repo-create emits a
`git.remote.create` audit event with the launch `profile`,
`git_remote_profile`, `repo`, `creds_user`, and `rc`. The token is
**not** in the event.

## Provision authentication

For `auth = "gh"`, authenticate the configured credential owner on the configured
GitHub host using [GitHub CLI login](https://cli.github.com/manual/gh_auth_login).
The credential filesystem is inside the selected execution boundary.

For `auth = "token"`, provision the configured absolute token file with access
restricted to its credential owner and trusted operators. Use your existing
secret manager or provisioning process. Replace its content through that same
mechanism; Uxon reads it on each operation and does not need a restart.

Revocation and reissue belong to the credential issuer. Doctor checks access
and authentication prerequisites; it does not prove repository-creation permission.
The launch user also needs its own Git push authentication.

## Reference

- [`../../reference/configuration.md`](../../reference/configuration.md) — `[[git_remote_profiles]]` and launch-profile git policy.
- [`../../reference/cli.md`](../../reference/cli.md) — `uxon new --git-remote`, `--git-visibility`, `--no-git`.
