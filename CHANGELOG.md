# Changelog

User-facing changes only — what behaviour or surface differs between
versions. Implementation details, refactors, and internal helper
renames live in `git log`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Agents are now auto-detected by default. With `[agents].enabled` empty or absent in config, uxon probes the host on launch and treats every installed CATALOG agent (`claude`, `codex`, `cursor`) as launchable. Setting `enabled = ["claude", "codex"]` switches to a strict whitelist (only listed agents are launchable, even if more are installed). The old hardcoded `enabled = ["claude"]` default is gone. `agents.default` is also optional now — when unset, uxon falls back to the first available agent.
- Host probe runs once on mount; pick up freshly installed agents via the existing `r` refresh binding instead of the previous 2-second interval re-probe.
- Whichever agent is picked (`--agent`, `agents.default`, `agents.enabled[0]`, or the auto-mode probe) is now verified against the host probe before launch — a missing binary fails with a uxon-level message and install hint (exit code `1`) instead of an opaque tmux exec failure.
- TUI surfaces probe failures (sudo missing, NOPASSWD misconfig, etc.) via the "agents unavailable" modal with the error and install hints, instead of leaving the agent list silently empty.

### Removed
- "Detected but not enabled" banner on the main screen, plus the `[a]` add-to-config / `[x]` dismiss bindings and the per-user dismissed-list state file (`~/.local/state/uxon/dismissed.json`). With auto-mode, installed agents are launchable automatically; with strict mode, the operator owns the whitelist by editing config directly.

## [3.4.0] — 2026-05-08

### Added
- Session dashboard `by_host` view with a per-host tab strip and status bar; toggle to the default `flat` (single ranked list) with `v`. Configure the initial mode via `tui.table.default_view` (default `"flat"`).
- Top action row laid out as three side-by-side buttons; ←/→ cycles cyclically inside the row, ↓ exits to the dashboard.
- ←/→ on the dashboard cycles between hosts: in `by_host` it advances the active host tab cyclically; in `flat` it jumps the cursor across `(host, own/other)` transitions cyclically. Within the local host, own and other-user rows can interleave by recency, so each user transition is a jump point.
- Search bar across the dashboard: focused by default on TUI mount, refocus from anywhere with `s` (or `/`), clear with `Esc`. Configure searchable fields via `tui.search.fields` (default `name`, `user`; allowed `name`, `user`, `host`, `path`, `cmd`).
- "Open existing project" modal lands directly on a search input — type to narrow the project list (case-insensitive substring on name), `↑` / `↓` navigate the filtered view, `Enter` confirms, `Esc` clears a non-empty filter (otherwise cancels). Quick-pick by digit `1`–`9` removed in favour of typing.
- Per-host block colour: pin a hue with `[[remote_hosts]] color = "..."`, customise the auto-cycle palette with `[tui] color_palette`, and the local block colour with `[local_host] color`.
- `ssh_control_persist_seconds` setting (default `300`; must be `> 0`). Disable multiplexing with `ssh_multiplex = "off"`.
- Layout-invariant bindings: every dashboard key has a JCUKEN twin (`q`/`й`, `r`/`к`, `d`/`в`, `v`/`м`, …) so the keymap survives a Russian keyboard layout.
- Optional `host_stats` block in the `list` wire envelope, surfacing per-host CPU / RAM / load to aggregating peers (additive; no schema-version bump).

### Changed
- Sort is now a hard contract, not a setting: locals first (own then other-user), then `[[remote_hosts]]` declaration order, with within-block ranking by last-attach desc then name asc. The `tui.table.default_sort_by` key is silently ignored.
- Attached state is shown by a glyph in the NAME column — `●` filled when attached, `○` hollow otherwise — instead of a bold green name.
- NAME column drops the `@<agent>` suffix (the agent has its own column already). The disambiguator index `-N` is preserved so siblings stay distinct, e.g. `proj@claude-2` renders as `proj-2`.
- Quit is `q` / `й` only. `Esc` is a scoped cancel (clear search, close modal, leave field) and never quits the TUI.
- `PATH` and `CMD` columns hidden by default. For uxon-launched sessions `CMD` only echoed the agent name (already shown in the AGENT column); operators opt back in by listing `"path"` / `"cmd"` in `tui.table.columns`.

### Removed
- Sort cycle bindings (`s`, `S`) and the `tui.table.default_sort_by` setting.
- `[` / `]` host-cycling bindings — use ←/→ on the dashboard or the host tab strip instead.
- Digit `1`–`9` quick-jump on the main screen. The arrow keys + Enter now drive every reachable row; digit-jump conflicted with the search input (typing `1` to filter sessions used to fire a jump instead of inserting the character).

### Fixed
- Dashboard rows no longer briefly reorder on tab switches and large refresh diffs (an apply-order bug that dropped or shuffled inserted rows when several appeared in one tick).
- Remote attach (CLI `uxon attach --host` and TUI Enter on a remote row) opens a fresh ssh connection instead of reusing the poller's `ControlMaster`. A wedged multiplex master could previously hang the interactive ssh at `unix_wait_for_peer`, leaving the user with a frozen terminal and no remote tmux. Polling and remote `kill` continue to multiplex.
- Wedged `ControlMaster` now self-heals on every multiplex-using path: polling fetch, CLI `uxon kill --host`, and TUI remote-row kill. After a `subprocess.TimeoutExpired` against the default ssh template, the collector tries `ssh -O exit`, then resolves the `%C` socket path via `ssh -G`, finds the master pid in `/proc` and `SIGKILL`s it, then unlinks the stale socket. The next ssh attempt spawns a fresh master. Without this, an unresponsive master kept every subsequent slave hanging at `unix_wait_for_peer` until killed by hand — and a CLI-only kill (no TUI poller running) had no recovery path at all.

## [3.3.0] — 2026-05-07

### Documentation

- User-facing site under [`docs/`](docs/) reorganised to follow
  the [Diátaxis](https://diataxis.fr) model. Old `docs/configuration.md`,
  `docs/deployment.md`, `docs/getting-started.md` removed; bookmarks
  redirect via [`docs/index.md`](docs/index.md). README slimmed to
  pitch + install + pointers.
- New operations runbooks under
  [`docs/guides/operate/`](docs/guides/operate/) — onboarding, incident
  response, fleet upgrade, aggregator-loss recovery, central audit
  forwarding, backup/restore, credential rotation.
- New developer-privacy disclosure at
  [`docs/privacy.md`](docs/privacy.md).

### Changed (breaking)

- Audit events now go to the platform log (journald native on
  systemd hosts, `/dev/log` syslog fallback otherwise) instead of
  `~/.local/state/uxon/tui-{user}-{date}.log`.  That file is no
  longer written.  Query via `journalctl SYSLOG_IDENTIFIER=uxon` on
  systemd hosts.
- `uxon.tui.LOG_DIR` public import removed.  Out-of-tree code that
  imported `from uxon.tui import LOG_DIR` will fail at import.
- Peer protocol: `list`, `attach`, `kill` now accept an internal
  `--audit-correlation-id <uuid>` flag (hidden from `--help`).  All
  peers in a fleet must run the same major version (existing
  upgrade posture).

### Added

- `uxon doctor` reports the audit-channel state on its own line:
  `audit:    enabled, sink=journald-native` (or `sink=syslog` /
  `sink=no-sink`; `enabled` flips to `disabled` when the channel is
  off).  JSON envelope carries the same data under `data.audit`.
- New `[audit]` config table: `enabled` (bool, default `true`) and
  `syslog_facility` (string, default `"user"`, consulted only on
  the `/dev/log` fallback path).  No environment-variable override
  — the only kill-switch is the config table.
- 15 audit events covering CLI startup, TUI lifecycle, session
  create/attach/end/kill, cross-host dispatch, and `git.remote.create`
  / `config.error`.  Schema and per-event field reference in
  [`docs/reference/audit-events.md`](docs/reference/audit-events.md).
- Multi-host: configure peers under `[[remote_hosts]]` in `config.toml`;
  `uxon list --host <alias>` and `uxon list --all-hosts` aggregate
  sessions across the fleet.
- TUI session dashboard: a single sortable table that mounts local
  own, local other-user (when sudo block is active), and remote
  rows together. A HOST column appears automatically when peers
  are configured; per-host health badges
  (`[ok]`, `[cache 12s]`, `[err: …]`, `[loading]`) live in the
  section header.
- New `[tui.table]` config block: `columns` (list of column ids in
  display order) and `default_sort_by` (initial sort column).
  Empty/absent uses built-in defaults; unknown ids are silently
  dropped for forward-compat. Reference:
  [`docs/reference/configuration.md`](docs/reference/configuration.md);
  use cases:
  [`docs/guides/customise/customise-dashboard.md`](docs/guides/customise/customise-dashboard.md).
- `uxon attach --host <alias> --user <name> [--dry-run]` opens a remote
  session over SSH; pressing Enter on a TUI remote row does the same.
- `uxon kill --host <alias> [--user <name>] <id>` kills a single
  session on a peer; `d` on a TUI remote row dispatches the same.
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
  (kubectl-exec / docker-exec recipes in `docs/reference/configuration.md`).
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
- Press `s` in the TUI to cycle the dashboard sort across cpu / ram /
  last / name. `S` (Shift+s) toggles sort direction. The new sort
  applies across local own, local other-user, and every peer's rows
  in one flat list.

### Changed

- Local and remote sessions now render in a single sortable session
  dashboard. The HOST column appears automatically when peers are
  configured; the USER column appears when other-user rows are
  visible. The dedicated remote-sessions section is gone.
- `kill ALL uxon sessions` action renamed to `kill all reachable
  users`; confirmation phrase is now `kill-all-reachable` (was
  `kill-all-global`).
- `uxon list --all-users` scopes to the reachable subset of
  `session_users`; unreachable users surface on stderr and as
  `data.scope_skipped` in JSON.
- `uxon doctor` runs agent probes in parallel with a 2 s per-probe
  deadline; slow agents surface as `TIMEOUT (>2.0s)` instead of
  inflating wall time.

### Removed

- The `k` keybinding (remote-only kill) is removed. `d` covers all
  kills now — local rows and remote rows alike.

### Fixed

- Dashboard sort by `last` / `new` columns now ranks local sessions
  correctly (previously they sank to the bottom regardless of age).
- Offline peers no longer show a misleading "full visibility" badge
  from the cache fallback path; `scope_limited` / `scope_skipped`
  round-trip through the on-disk cache.

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
  [`docs/reference/cli.md`](docs/reference/cli.md#doctor).

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
