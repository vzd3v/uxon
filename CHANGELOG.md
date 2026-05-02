# Changelog

User-facing changes only — what behaviour or surface differs between
versions. Implementation details, refactors, and internal helper
renames live in `git log`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [3.2.1] — 2026-05-02

### Fixed

- `uxon doctor` and the TUI no longer report `tmux` and every agent
  as missing when the caller and launch user differ — cross-user
  host detection now finds binaries installed for the launch user.

## [3.2.0] — 2026-05-02

### Added

- TUI auto-detects coding agents installed on the host but not yet
  listed in `[agents].enabled`. A non-intrusive banner on the main
  screen offers `[a]` to add the agent to the repo config and `[x]`
  to dismiss the suggestion. Dismissals are stored per-user under
  `${XDG_STATE_HOME:-$HOME/.local/state}/uxon/dismissed.json` so they
  do not silence the banner for other users on a shared host.
- Friendly preflight on `uxon run` / `uxon new` / `uxon attach` /
  `uxon list`: if `tmux` or the requested coding agent is not on the
  launch user's `PATH`, `uxon` now exits with a one-line install hint
  instead of a Python `FileNotFoundError` traceback.

### Changed

- The TUI re-runs its host probe on every refresh tick, so installing
  an agent and pressing `r` (or just waiting) is enough to recover
  from the "all agents missing" modal — no longer requires quitting
  and restarting.
- `uxon doctor` is no longer mentioned in README's "After install"
  quick-start; the TUI surfaces the same issues in line. The full
  `doctor` reference still lives in
  [`docs/cli.md`](docs/cli.md#doctor).

### Fixed

- `allowed_roots = []` (the default) now means "any writable directory"
  uniformly across `uxon new`, the TUI new-project flow,
  `find_project_config`, and `uxon doctor`. Previously the 3.1.0 fix
  reached only `uxon run` and `uxon new -w`; the four other sites kept
  the strict-whitelist branch and rejected every path on an empty
  list (so `uxon new demo --dry-run` failed with "new target must be
  under allowed_roots", `uxon doctor` flagged a fake
  `new_project_root … is outside allowed_roots` issue, and project
  `.uxon.toml` files were silently ignored).

## [3.1.0] — 2026-05-01

### Changed (breaking)

- `allowed_roots` is now a strict whitelist when set. With
  `allowed_roots = []` (default), `uxon run`, `uxon new -w`, and the
  TUI's "new session in current folder" all launch in any directory
  the launch user can write to. With `allowed_roots = [...]` set,
  the same three accept only paths under the listed directories —
  no implicit `$HOME` or other side allowance.
- The TUI and the CLI now apply identical rules to the launch
  target.

## [3.0.0] — 2026-05-01

First release on PyPI as
[`uxon`](https://pypi.org/project/uxon/). Install via
`uv tool install uxon`, `pipx install uxon`, or
`pip install --user uxon`.
