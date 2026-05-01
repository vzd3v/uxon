# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [2.0.0] — 2026-05-01

First open-source release.

### Changed (breaking)

- **Default config paths are no longer site-specific.**
  `allowed_roots` defaults to `[]` (operators must declare their
  own); `new_project_root` defaults to `~/projects`. Existing
  deployments are unaffected because they override these in
  `config/config.toml`. New installations will need to set both.
- **TUI event log default location changed.**
  `lib/ccw_tui/events.py` now defaults to
  `${XDG_STATE_HOME:-~/.local/state}/ccw` instead of
  `/srv/work/logs/ccw`. Set `CCW_LOG_DIR` in the launch user's
  environment to keep the old location.

### Added

- MIT [`LICENSE`](LICENSE),
  [`SECURITY.md`](SECURITY.md),
  [`CONTRIBUTING.md`](CONTRIBUTING.md),
  this changelog.
- `pyproject.toml` for tool configuration (ruff, pyright, pytest)
  and project metadata.
- GitHub issue and pull-request templates under `.github/`.
- `CODEOWNERS`.
- `config/config.example.toml` is now shipped next to the
  gitignored real config; `examples/ccw-config.json` was refreshed
  to the current schema.
- `docs/architecture.md` — public architecture overview that
  replaces the old `Repo structure` annex in `README.md` and the
  agent-only `AGENTS.md`.
- CI now runs `ruff` lint, `ruff format --check`, `pyright`, and
  `gitleaks`. Tests run on Python 3.11, 3.12, and 3.13.
- README badges (CI status, license, Python version).

### Removed

- Internal agent material (`AGENTS.md`, `CLAUDE.md`, `.claude/`,
  `docs/plans/`, `docs/superpowers/`, `docs/prototypes/`) is no
  longer tracked. It still lives on disk in maintainers' working
  copies under `docs/agents/`.
- The hand-maintained explicit file list in
  `.github/workflows/ci.yml` was replaced with
  `python3 -m py_compile $(git ls-files '*.py' bin/ccw)`.

## [1.3.3] and earlier

Pre-OSS history. See `git log` for details.
