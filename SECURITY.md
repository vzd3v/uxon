# Security policy

## Supported versions

| Version | Status         |
|---------|----------------|
| 2.x     | Security fixes |
| < 2.0   | Unsupported    |

## Reporting a vulnerability

Please use GitHub's **"Report a vulnerability"** form on the
[Security tab](https://github.com/vzd3v/vz_devagent_cli_tool/security)
of this repository, or email `vz@vz.team` with the subject
`ccw security:`.

Expected acknowledgement within 72 hours. Coordinated-disclosure window
is 30 days unless agreed otherwise. Please do not open public issues
for security reports.

## Threat model

`ccw` is a privileged orchestrator on a shared host. The trust
boundaries are:

1. **Caller → launch user.** `ccw` uses `sudo -iu <user>` to fork
   `tmux` and the agent binary as a different OS user. Authorization
   is enforced by the operator's `sudoers` configuration; `ccw` never
   elevates beyond what `sudoers` already grants.
2. **Allowed roots.** Sessions cannot be started outside
   `allowed_roots` (config) plus the launch user's home directory.
   New projects are created only under `new_project_root`, which
   itself must be inside an allowed root.
3. **Git remote profiles.** Repo creation is limited to the explicit
   `git_remote_profiles` whitelist. With `auth = "token"`, `ccw`
   reads the PAT from `token_file` (read by `creds_user`), holds it
   in memory only for the duration of the API call, never logs it,
   and never echoes it in `--dry-run` output.
4. **Config writes.** The TUI Settings screen rewrites
   `config/config.toml` in place via a `tomlkit` round-trip. If the
   file is not directly writable, `ccw` shells out to `sudo tee`.
   The new content is staged in a temporary file and then atomically
   replaced.

## Out of scope

- Sandbox escape from inside the agent binary. `ccw` does not
  constrain what `claude`, `codex`, or `cursor-agent` can do once
  launched.
- The operator's `sudoers` configuration. A misconfigured
  `NOPASSWD: ALL` entry is the operator's responsibility.
- Container/VM isolation between users. `ccw` is a thin wrapper, not
  a jail.

## Hardening recommendations

- Run agents as a dedicated, non-privileged OS user.
- Keep `git_remote_profiles` short and explicit. Prefer
  `auth = "gh"` (delegated to a logged-in `gh` CLI) over storing a
  long-lived PAT on disk.
- Set `enable_all_users_list = false` unless multi-user inspection
  is genuinely required; the cross-user list relies on `sudo -niu`
  probes.
- Restrict write access to `config/config.toml` to administrators;
  the TUI's `sudo tee` fallback is a convenience, not an authorisation
  model.
