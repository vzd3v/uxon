# Changelog

User-facing changes only — what behaviour or surface differs between
versions. Implementation details, refactors, and internal helper
renames live in `git log`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased] — target version 3.3.0

### Added

- Multi-host: configure peers under `[[remote_hosts]]` in `config.toml`;
  `uxon list --host <alias>` and `uxon list --all-hosts` aggregate
  sessions across the fleet.
- TUI Remote sessions block with HOST column and per-host health badge
  (`[ok]`, `[cache 12s]`, `[err: …]`, `[loading]`).
- `uxon attach --host <alias> --user <name> [--dry-run]` opens a remote
  session over SSH; pressing Enter on a TUI remote row does the same.
- `uxon kill --host <alias> [--user <name>] <id>` kills a single
  session on a peer; `k` on a TUI remote row dispatches the same.
  Bulk `kill-all` stays local.
- `uxon kill --user <name> <id>` kills another launch user's session
  when the caller has per-target NOPASSWD to that user.
- TUI superuser block now scopes to users you can `sudo -niu` into;
  header shows `(N/M users reachable)` when `session_users` lists more
  candidates than the caller can reach. Probed once at TUI startup.
- Cross-host `--all-users` aggregation: peers with
  `enable_all_users_list = false` are labelled `(own only)` in the
  section header or HOST column.
- `--json` output for `uxon list`, `doctor`, `version`, `kill`,
  `kill-all` — one wire-schema envelope per call.
- SSH `ControlMaster=auto` is the default for `[[remote_hosts]]`
  fetches; control socket under `${XDG_CACHE_HOME:-~/.cache}/uxon/ssh-%C`,
  60 s lifetime. Set `ssh_multiplex = "off"` to opt out.
- Per-host overrides in `[[remote_hosts]]`: `interval`,
  `connect_timeout`, `total_timeout` (`"5s"`, `"500ms"`, `"2m"`, or
  bare seconds), `extra_ssh_options`, and `command_template`
  (kubectl-exec / docker-exec recipes in `docs/configuration.md`).
- `fetch_concurrency` (default `16`) caps concurrent SSH workers
  fleet-wide.
- Per-host circuit breaker: three consecutive failures open a peer for
  one interval before the next probe.
- `uxon doctor --remote` probes every configured peer once and reports
  reachability, latency, and session count; default `uxon doctor`
  keeps zero SSH I/O.
- `UXON_DEBUG=startup` logs `mount_started` / `first_paint` /
  `first_data_landed` timestamps to the per-day debug log.
- `UXON_METRICS=1` writes one JSON line per source attempt to
  `${state_dir}/metrics.jsonl` (rotated at 1 MiB, cap 3 files).

### Changed

- `kill ALL uxon sessions` action renamed to `kill all reachable
  users`; confirmation phrase is now `kill-all-reachable` (was
  `kill-all-global`).
- `uxon list --all-users` scopes to the reachable subset of
  `session_users`; unreachable users surface on stderr and as
  `data.scope_skipped` in JSON.
- `uxon doctor` runs agent probes in parallel with a 2 s per-probe
  deadline; slow agents surface as `TIMEOUT (>2.0s)` instead of
  inflating wall time.

### Fixed

- TUI remote-sessions table no longer flickers or empties on refresh
  ticks; focus on a remote row is preserved across re-composes.
- Remote-host on-disk cache round-trips `scope_limited` /
  `scope_skipped`, so an offline peer no longer shows a misleading
  "full visibility" badge from the cache fallback path.

## [3.2.2] — 2026-05-02

### Fixed

- The TUI session list and metrics no longer freeze a few seconds
  after launch while the rest of the interface stays responsive.

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
