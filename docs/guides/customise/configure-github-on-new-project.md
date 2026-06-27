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
creds_user = "your-os-user"     # whose ~/.config/gh/hosts.yml token to use
visibility = "private"

[[git_remote_profiles]]
name       = "acme-org"
host       = "github.com"
owner      = "acme"
auth       = "token"            # fine-grained PAT via REST API
creds_user = "your-os-user"
token_file = "/home/your-os-user/.secrets/uxon-acme.token"
visibility = "private"

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
  `creds_user`. The token is held in memory only for the duration
  of the REST call, never logged, never echoed in `--dry-run`.
  `repo` scope is the minimum.
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

## Token rotation

See [`../operate/rotate-credentials.md`](../operate/rotate-credentials.md).

## Reference

- [`../../reference/configuration.md`](../../reference/configuration.md) — `[[git_remote_profiles]]` and launch-profile git policy.
- [`../../reference/cli.md`](../../reference/cli.md) — `uxon new --git-remote`, `--git-visibility`, `--no-git`.
