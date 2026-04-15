# vz_devagent_cli_tool

Single source of truth for the `ccw` CLI used to launch and manage Claude Code
tmux sessions on VPS hosts.

## Canonical layout
- Canonical repo: GitHub `vz_devagent_cli_tool`
- Canonical host checkout: `/srv/apps/vz_devagent_cli_tool`
- User-facing command: `/usr/local/bin/ccw` -> symlink to `bin/ccw`
- Repo-local config: `/srv/apps/vz_devagent_cli_tool/config/config.toml`
- Dedicated tmux socket: `/tmp/ccw-<launch-user>.sock` by default
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

## CI
- GitHub Actions runs on pushes to `main` and on pull requests.
- Baseline checks:
  - `python3 -m py_compile ...`
  - `python3 -m unittest discover -s tests -p 'test_*.py'`

## Repeated `ccw new`
- First plain `ccw -n <name>` still creates or reuses `/srv/.../<name>` and starts a tmux session there.
- Repeating either plain `ccw -n <name>` or worktree `ccw -n <name> -w <branch>` no longer silently creates `-2/-3`.
- If a compatible session already exists and `ccw` has an interactive TTY, it prompts:
  - default: attach to the existing session
  - alternative: start a new parallel session
- Use explicit flags to skip the prompt:
  - `--attach-existing`: attach to the compatible session immediately
  - `--new-session`: create a parallel session immediately
- Without a TTY, precedence is:
  - explicit CLI flag
  - `CCW_REPEAT_NONINTERACTIVE_POLICY=fail|attach|new`
  - `repeat_noninteractive_mode` from config
  - default `fail`

## `ccw doctor`
- `ccw doctor` is the main read-only diagnostic entrypoint for operators.
- It reports:
  - caller user and resolved launch user
  - active config path(s)
  - `allowed_roots` and `new_project_root`
  - `tmux` path, dedicated socket path, and socket-parent writability
  - whether `claude` resolves for the launch user
  - current dedicated-socket sessions and any legacy default-socket sessions
  - obvious config/runtime mismatches

## Dedicated tmux socket
- `ccw` now uses a dedicated tmux socket per launch user by default via `tmux_socket_template`.
- Default template: `/tmp/ccw-{user}.sock`
- This isolates `ccw` sessions from a user's default tmux server and makes `ccw list/attach/kill/kill-all` deterministic.
- Migration note: legacy `cc-*` sessions on the user's default tmux socket are not automatically managed by the new socket. Use `ccw doctor` to spot that state before cleanup/migration.

## Guardrails
- `ccw kill-all` now requires either:
  - an interactive confirmation, or
  - `--force`
- When `ccw new` sees compatible sessions only on the legacy default tmux socket, it fails with guidance instead of silently creating duplicate sessions on the dedicated socket.

## Render config
```bash
python3 install/render_ccw_config.py --config-json examples/ccw-config.json --output config/config.toml
```

Config keys relevant to this release:
- `repeat_noninteractive_mode`: `fail`, `attach`, or `new`
- `tmux_socket_template`: absolute socket path template; supports `{user}` and `{uid}`

## Install command
```bash
sudo python3 install/install_ccw.py --repo-dir /srv/apps/vz_devagent_cli_tool --install-path /usr/local/bin/ccw
```

## Release / rollout checklist
1. Update code, tests, docs, and `VERSION` in this repo.
2. Run local checks plus `ccw doctor` against a rendered repo-local config.
3. Commit and push this repo.
4. Deploy the exact ref to each host.
5. Verify `ccw --version`, `ccw doctor`, repeat-session behavior, and socket path on each host.
6. Update infra runbooks, host passports, and change logs to match the live state.
