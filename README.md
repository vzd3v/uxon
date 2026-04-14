# vz_devagent_cli_tool

Single source of truth for the `ccw` CLI used to launch and manage Claude Code
tmux sessions on VPS hosts.

## Canonical layout
- Canonical repo: GitHub `vz_devagent_cli_tool`
- Canonical host checkout: `/srv/apps/vz_devagent_cli_tool`
- User-facing command: `/usr/local/bin/ccw` -> symlink to `bin/ccw`
- Repo-local config: `/srv/apps/vz_devagent_cli_tool/config/config.toml`
- Repo version file: `VERSION`

The `infra` repo deploys this tool, but it is not the canonical source for the
`ccw` executable or config-rendering logic.

## Repo structure
- `VERSION`: human-owned tool version for releases and host verification
- `bin/ccw`: main CLI entrypoint
- `install/install_ccw.py`: installs `/usr/local/bin/ccw` as a symlink
- `install/render_ccw_config.py`: renders `config/config.toml` from JSON
- `tests/test_ccw.py`: unit tests for config and launch-user behavior
- `examples/ccw-config.json`: example payload for config rendering
- `config/`: local host config directory, intentionally gitignored

## Versioning
- Bump `VERSION` when the user-visible `ccw` behavior changes.
- `ccw --version` prints the repo version and, when available, the current git commit.
- On a host, verify both the installed command and checkout with:

```bash
ccw --version
git -C /srv/apps/vz_devagent_cli_tool rev-parse --short HEAD
cat /srv/apps/vz_devagent_cli_tool/VERSION
```

## Local checks
```bash
python3 -m py_compile bin/ccw tests/test_ccw.py install/install_ccw.py install/render_ccw_config.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Render config
```bash
python3 install/render_ccw_config.py --config-json examples/ccw-config.json --output config/config.toml
```

## Install command
```bash
sudo python3 install/install_ccw.py --repo-dir /srv/apps/vz_devagent_cli_tool --install-path /usr/local/bin/ccw
```
