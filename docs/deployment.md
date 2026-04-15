# Deployment Model

`vz_devagent_cli_tool` is the only canonical home for `ccw`.

## Host layout
- checkout: `/srv/apps/vz_devagent_cli_tool`
- executable: `/usr/local/bin/ccw`
- config: `/srv/apps/vz_devagent_cli_tool/config/config.toml`
- default dedicated tmux socket: `/tmp/ccw-{user}.sock`

`/usr/local/bin/ccw` should remain a symlink to `bin/ccw` inside the canonical
checkout. Do not keep a second copied executable as a parallel source of truth.

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

## Verification checklist
After deploying a new ref:
1. `ccw --version`
2. `ccw doctor`
3. repeat plain `ccw -n <name>` behavior
4. repeat worktree `ccw -n <name> -w <branch>` behavior
5. `ccw kill-all --dry-run` and guarded `ccw kill-all`
6. confirm the reported dedicated socket path matches the deployed config
