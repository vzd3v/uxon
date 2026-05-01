# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [3.0.0] — 2026-05-01

First PyPI release.

### Added

- Released on PyPI as [`uxon`](https://pypi.org/project/uxon/).
  Install with `uv tool install uxon`, `pipx install uxon`, or
  `pip install --user uxon`.
- `python -m uxon` works alongside the generated `uxon` console
  script — same entrypoint, useful for in-tree development.
- PyPI release pipeline at `.github/workflows/release.yml`: pushing
  a `v*` tag builds sdist + wheel, publishes via OIDC trusted
  publisher (no API token in GitHub Secrets), and creates a GitHub
  Release with auto-generated notes. PEP 740 attestations are
  generated automatically.
- `legacy_session_prefixes` config key — array of additional `tmux`
  prefixes that `uxon list` / `attach` / `kill` recognise alongside
  `session_prefix`. Useful when migrating from a previous prefix.
  New sessions are always created under `session_prefix`.
- `docs/cli.md` — full CLI reference (every flag, exit code,
  identifier resolution, repeat behaviour).
- `docs/configuration.md` — use-case-driven configuration guide
  (single-user laptop, multi-user host, sandboxed agent user,
  pinning agents to directories, GitHub repo creation).

### Changed

- **`uxon` ships as a `src/uxon/` Python package** built with
  `hatchling`. Out-of-tree consumers import from `uxon.*` (e.g.
  `from uxon.settings import ...`, `from uxon.tui.app import ...`).
- **`install/install_uxon.py`** now creates a dedicated venv at
  `--venv-dir` (default `/opt/uxon/venv`), installs the package
  into it, and symlinks the venv's console script to
  `--install-path`. Used for the shared-host / multi-user VPS
  scenario only — solo laptops use `uv tool install` /
  `pipx install`. Adds `--dry-run` and `--reinstall` flags.
- README rewritten around the TUI as the primary interface, with a
  compact CLI summary linking to `docs/cli.md`. Install section
  leads with `uv tool install uxon` and `pipx install uxon`.

### Dependencies

- `textual >=0.80,<9` and `tomlkit` are hard runtime dependencies,
  pulled in automatically by `pip install` / `pipx install` /
  `uv tool install`. The optional `dev` extra ships `pytest`,
  `pytest-xdist`, `ruff`, and `pyright`.
