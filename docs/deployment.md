# Deployment Model

`vz_devagent_cli_tool` is the only canonical home for `ccw`.

## Host layout
- checkout: `/srv/apps/vz_devagent_cli_tool`
- executable: `/usr/local/bin/ccw`
- config: `/srv/apps/vz_devagent_cli_tool/config/config.toml`

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
