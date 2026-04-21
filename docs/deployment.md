# Deployment Model

`vz_devagent_cli_tool` is the only canonical home for `ccw`.

## Host layout
- checkout: `/srv/apps/vz_devagent_cli_tool`
- executable: `/usr/local/bin/ccw`
- config: `/srv/apps/vz_devagent_cli_tool/config/config.toml`
- default dedicated tmux socket: `/tmp/ccw-{user}.sock`

`/usr/local/bin/ccw` should remain a symlink to `bin/ccw` inside the canonical
checkout. Do not keep a second copied executable as a parallel source of truth.

## Runtime dependencies
- Python 3.11+ (stdlib `tomllib` is used for config reads).
- `tomlkit` — required for config writes (TUI Settings screen). Install via
  `apt install python3-tomlkit` or `pip install tomlkit` into the Python env
  `ccw` runs under. Without it, TUI saves fail while CLI (`list`, `doctor`,
  `run`, `new`, `attach`, `kill`) keeps working.
- `textual>=0.80,<9` — required for the interactive TUI only; lazy-imported
  inside ``do_interactive`` so non-TUI subcommands (`list`, `doctor`,
  `version`) run without it.
- `gh` — required on hosts that use `auth = "gh"` git-remote profiles.

## Infra integration
The infra repo may:
- clone/update this repo on hosts
- choose the target git ref (`tag`, branch, or commit)
- pass host-specific settings to `install/render_ccw_config.py`
- manage host-specific ACLs for editable checkouts

Use an explicit ref in infra when you want deterministic rollout, and keep
`VERSION` bumped in this repo so operators can correlate `ccw --version` with
the deployed checkout state.

The infra repo must not become a second canonical location for:
- the `ccw` executable source
- the config schema
- config rendering logic

## Config contract
Important config keys expected during rollout:
- `repeat_noninteractive_mode`: `fail`, `attach`, or `new`
- `tmux_socket_template`: absolute per-user socket template; supports `{user}` and `{uid}`

Recommended rollout defaults:
- keep `repeat_noninteractive_mode = "fail"` unless the host explicitly wants unattended attach/new
- keep `tmux_socket_template = "/tmp/ccw-{user}.sock"` unless the host needs a different absolute socket path

### Multi-agent config schema (2026-04-21)

The flat `default_claude_args` key is **removed**. Config now uses nested tables:

```toml
[agents]
enabled = ["claude", "cursor"]
default = "claude"

[agents.claude]
default_args = []

[agents.codex]
default_args = []

[agents.cursor]
default_args = []
```

**Manual migration required per host.** No automatic migration code runs.
Steps:
1. Edit `config/config.toml` on each host and replace the flat
   `default_claude_args = [...]` line with the nested `[agents]` tables above.
2. Include only agents that are installed on that host in `enabled`.
3. Run `ccw doctor` to verify the config loads and all enabled agents probe OK.

If the old flat key is still present when `ccw` loads, it will fail with a
clear error message pointing here.

### Git remote profiles (optional)
`git_create_enabled`, `default_git_remote_profile`, and
`[[git_remote_profiles]]` are **hand-edited** in `config.toml` on the host —
they are intentionally not part of the `install/render_ccw_config.py`
JSON-to-TOML flow (profiles reference `creds_user` and `token_file`, and
infra shouldn't hard-code those across hosts). The TUI shows them read-only.
See `README.md` "Git remote on new project" for field reference and examples.

## Verification checklist
After deploying a new ref:
1. `ccw --version`
2. `ccw doctor` — includes per-profile status for configured
   `[[git_remote_profiles]]`, if any (read-only probe)
3. repeat plain `ccw -n <name>` behavior
4. repeat worktree `ccw -n <name> -w <branch>` behavior
5. `ccw kill-all --dry-run` and guarded `ccw kill-all`
6. confirm the reported dedicated socket path matches the deployed config
7. if git-remote profiles are enabled: `ccw -n <throwaway> --git-remote <profile> --dry-run`
   should print the full command plan without executing
