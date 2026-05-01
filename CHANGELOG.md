# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
  `lib/ccw_tui/events.py` now defaults to
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
  from `examples/ccw-config.json`, which was refreshed to the
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
