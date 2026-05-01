# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [3.0.0] — 2026-05-01

Project rename: `ccw` → `uxon`. The CLI binary, Python modules,
session prefix, environment variables, and project-level config
filename all change. Existing deployments need a one-time config
update; running tmux sessions started under the old prefix remain
reachable via `legacy_session_prefixes`.

### Changed (breaking)

- **CLI binary renamed.** `bin/ccw` → `bin/uxon`; default install
  path is now `/usr/local/bin/uxon`. The `bin/ccw` back-compat
  symlink has been removed. Re-run `install/install_uxon.py` to
  refresh the system symlink.
- **Default tmux session prefix renamed.** `ccw-` → `uxon-`. To keep
  pre-rename tmux sessions visible to `uxon list` / `attach` /
  `kill`, add the old prefix to the new `legacy_session_prefixes`
  config key (e.g. `legacy_session_prefixes = ["ccw-"]`). New
  sessions are always created under `session_prefix`.
- **Environment variables renamed.** `CCW_LOG_DIR` → `UXON_LOG_DIR`,
  `CCW_REPEAT_NONINTERACTIVE_POLICY` →
  `UXON_REPEAT_NONINTERACTIVE_POLICY`. The default log directory
  base is `${XDG_STATE_HOME:-~/.local/state}/uxon`.
- **Project config filename renamed.** `.ccw.toml` → `.uxon.toml`
  (the per-project override file picked up beneath
  `allowed_roots`). Rename existing files in checked-out projects.
- **Python modules renamed.** `lib/ccw_*` → `lib/uxon_*`,
  `lib/ccw_tui/` → `lib/uxon_tui/`. Tests and import paths
  updated. Out-of-tree consumers importing these modules need to
  update imports.
- **Tmux socket path default changed.** `/tmp/ccw-{user}.sock` →
  `/tmp/uxon-{user}.sock` (configurable via `tmux_socket_template`).
- **Outbound HTTP `User-Agent` changed** for the GitHub REST API
  backend: `ccw-git-remote` → `uxon-git-remote`.

### Added

- `legacy_session_prefixes` config key — array of additional tmux
  prefixes that `uxon list` / `attach` / `kill` recognise. Used to
  keep pre-rename sessions reachable. New sessions are never
  created under a legacy prefix.
- `docs/cli.md` — full CLI reference (every flag, exit code,
  identifier resolution, repeat behaviour).
- `docs/configuration.md` — use-case-driven configuration guide
  (single-user laptop, multi-user host, sandboxed agent user,
  pinning agents to directories, GitHub repo creation, prefix
  migration). Linked from the top of `config/config.example.toml`.

### Changed

- `README.md` rewritten around the TUI as the primary interface,
  with a compact CLI summary linking to `docs/cli.md`.

## [2.0.0] — 2026-05-01

First open-source release. Existing deployments that already set
`allowed_roots`, `new_project_root`, and `CCW_LOG_DIR` in their
own `config/config.toml` and launch environment are unaffected by
the breaking-default changes below.

### Changed (breaking)

- **Default config values are no longer site-specific.**
  `allowed_roots` now defaults to `[]` (operators must declare
  their own writable roots) and `new_project_root` defaults to
  `~/projects`. The launch user's home stays implicitly allowed
  for `ccw run` so first-run usability in `$HOME` still works.
  Hosts that already pin both keys in `config/config.toml` need
  no action; fresh installs must set them.
- **TUI event-log default location moved off `/srv`.**
  `lib/uxon_tui/events.py` now defaults to
  `${XDG_STATE_HOME:-~/.local/state}/ccw` instead of
  `/srv/work/logs/ccw`. The `CCW_LOG_DIR` environment override is
  unchanged — set it in the launch user's environment to keep the
  old path.

### Added

- MIT [`LICENSE`](LICENSE) (with `SPDX-License-Identifier` headers
  on source files), [`SECURITY.md`](SECURITY.md) (threat model and
  disclosure policy), [`CONTRIBUTING.md`](CONTRIBUTING.md), and
  this changelog.
- `pyproject.toml` carrying project metadata and tool
  configuration for `ruff`, `pyright`, and `pytest`.
- `config/config.example.toml` shipped alongside the gitignored
  real `config/config.toml` as a working starting point. Rendered
  from `examples/uxon-config.json`, which was refreshed to the
  current schema (removed `default_claude_args`, fixed
  `session_prefix` to `ccw-`, switched to neutral `agent` user
  and `/srv/projects` paths, switched to nested `[agents]`
  tables).
- `docs/architecture.md` — public architecture overview, replacing
  the old "Repo structure" annex previously embedded in
  `README.md`.
- GitHub issue and pull-request templates under `.github/`, plus
  a `CODEOWNERS` file.
- CI matrix expanded to Python 3.11, 3.12, and 3.13, with new
  jobs running `ruff check`, `ruff format --check`, `pyright`,
  and `gitleaks`.
- README badges for CI status, license, and Python version.

### Changed

- README rewritten for an open-source audience: neutral
  "what/why" lead-in, quick start built around `~/projects`, and
  install instructions based on `git clone` + symlink rather than
  a fixed `/srv/apps/...` layout. Operator-only material moved to
  `docs/deployment.md`; contributor checklists moved to
  `CONTRIBUTING.md`.
- `docs/deployment.md` rewritten to lead with single-host install
  and treat multi-host topology as an extension; 1.x→2.0 and
  multi-agent migration notes are now a labelled appendix.
- The CI compile step now derives its file list from
  `git ls-files` instead of a hand-maintained inline list in
  `.github/workflows/ci.yml`.

## [1.3.3] and earlier

Pre-OSS history. See `git log` for details.
