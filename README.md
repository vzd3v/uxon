# vz_devagent_cli_tool

Single source of truth for the `ccw` CLI used to launch and manage Claude Code
tmux sessions on VPS hosts.

## Canonical layout
- Canonical repo: GitHub `vz_devagent_cli_tool`
- Canonical host checkout: `/srv/apps/vz_devagent_cli_tool`
- User-facing command: `/usr/local/bin/ccw` -> symlink to `bin/ccw`
- System config: `/etc/ccw/config.toml`

The `infra` repo deploys this tool, but it is not the canonical source for the
`ccw` executable or config-rendering logic.

## Repo structure
- `bin/ccw`: main CLI entrypoint
- `install/install_ccw.py`: installs `/usr/local/bin/ccw` as a symlink
- `install/render_ccw_config.py`: renders `/etc/ccw/config.toml` from JSON
- `tests/test_ccw.py`: unit tests for config and launch-user behavior
- `examples/ccw-config.json`: example payload for config rendering

## Local checks
```bash
python3 -m py_compile bin/ccw tests/test_ccw.py install/install_ccw.py install/render_ccw_config.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Render config
```bash
python3 install/render_ccw_config.py --config-json examples/ccw-config.json --output -
```

## Install command
```bash
sudo python3 install/install_ccw.py --repo-dir /srv/apps/vz_devagent_cli_tool --install-path /usr/local/bin/ccw
```
