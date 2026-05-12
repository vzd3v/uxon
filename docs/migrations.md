# Migration notes

Per-version upgrade notes for changes that need operator
attention beyond a routine `pipx upgrade`. For the full release
log see [`CHANGELOG.md`](../CHANGELOG.md).

## 3.4.0

### Dashboard views, search, and a hard sort contract

- **Sort is no longer configurable.** Rows now appear in a
  fixed order: locals first (own then other-user), then
  remotes in `[[remote_hosts]]` declaration order, with
  within-block ranking by last-attach descending then name
  ascending. The `tui.table.default_sort_by` key is silently
  ignored on load (one `UXON_DEBUG=tui` line per occurrence) —
  no error, no fallback. The `s` / `S` cycle bindings are gone.
- **`tui.table.default_view` (new)** — `"flat"` (default) is a
  single ranked list across the fleet; `"by_host"` shows a
  per-host tab strip and status bar. Toggle at runtime with
  `v`. An active search forces `flat` until the query is
  cleared. ←/→ on the dashboard cycles between hosts: tabs
  in `by_host`, `(host, own/other)` transitions in `flat`.
  The `[` / `]` shortcut is gone.
- **Search bar.** Summoned on demand — hidden by default, press
  `s` (or `/`) from anywhere to reveal it. `Esc` clears the query
  and returns focus to the summoning widget. Configure searchable
  fields via `tui.search.fields` (default `["name", "user"]`).
- **`PATH` and `CMD` columns hidden by default.** For
  uxon-launched sessions `CMD` only echoed the agent name (already
  shown in the AGENT column). Operators who relied on either column
  must now list `"path"` / `"cmd"` in `tui.table.columns` to opt
  back in.

### Block colour and attach indicator

- **Per-host block colour.** Each remote host gets a hue
  applied to its tab, status-bar name token, and rows. Pin a
  hue with `[[remote_hosts]] color = "..."`; otherwise the
  TUI auto-cycles through `tui.color_palette` (default
  `["cyan", "blue"]`). Local rows take `local_host.color`
  (default `"green"`).
- **Attached state shown by glyph.** `●` filled when attached,
  `○` hollow otherwise — replacing the previous bold-green
  name colour.

### Keymap

- **`Esc` no longer quits.** Quit is `q` / `й` only. `Esc`
  is a scoped cancel: clear search, close modal, leave field.
  Operators who muscle-memoried `Esc → quit` should rebind in
  their head.
- **Layout-invariant twins.** Every dashboard key has a
  JCUKEN twin (`q`/`й`, `r`/`к`, `d`/`в`, `D`/`В`, `v`/`м`)
  so the keymap survives a Russian keyboard layout without
  touching `xkb`.

### SSH multiplex

- **`ssh_control_persist_seconds` (new)** — `ControlPersist`
  lifetime in seconds. Default `300` (was a hard-coded `60`).
  Must be `> 0`; to disable multiplexing entirely set
  `ssh_multiplex = "off"` rather than zeroing this out.

### Peer protocol

- **`host_stats` envelope field (additive).** The `list` JSON
  envelope now carries an optional `host_stats` block (CPU /
  RAM / loadavg / uptime / kernel) used by the aggregator's
  per-host status bar. The wire schema version stays `"1"` —
  3.3.0 peers continue to aggregate cleanly, the field is
  silently absent in their output.

## 3.3.0

### Audit channel — new sink

The 3.3.0 release introduces a dedicated audit channel and
removes the per-day TUI event-log file.

- **TUI event log removed.** The per-day JSONL file at
  `${XDG_STATE_HOME:-~/.local/state}/uxon/tui-{user}-{date}.log`
  is no longer written. Application-level audit now goes to the
  platform log channel (journald native, `/dev/log` syslog
  fallback). Query via `journalctl SYSLOG_IDENTIFIER=uxon` on
  systemd hosts; on syslog-only hosts `grep '@cee:' /var/log/syslog`.
  Old `tui-*.log` files left in place — no automatic cleanup.
  Operators remove the old directory manually if desired.
- **`uxon.tui.LOG_DIR` import removed.** Out-of-tree consumers
  that imported `from uxon.tui import LOG_DIR` will fail at
  import. The constant still lives in `uxon.tui.events.LOG_DIR`
  as an internal detail of the developer-facing `debug` /
  `metrics` channels.

### Multi-host

- `[[remote_hosts]]` config table-array. Configure peers; aggregate over SSH.
- `uxon list --host`, `uxon list --all-hosts`.
- `uxon attach --host <alias> --user <name>` / `uxon kill --host <alias> [--user <name>]`.
- `uxon doctor --remote` probes every peer once.
- TUI session dashboard mounts local + remote rows in one table
  with a `HOST` column.
- Per-host overrides: `interval`, `connect_timeout`, `total_timeout`,
  `extra_ssh_options`, `command_template`.
- `ssh_multiplex = "auto"` (default) / `"off"`.
- `fetch_concurrency = 16` (default).
- Per-host circuit breaker: 3 consecutive failures open the peer
  for one interval; exponential backoff (factor 2.0, cap 60 s).

### Peer protocol

- `list`, `attach`, `kill` now accept an internal
  `--audit-correlation-id <uuid>` flag (hidden from `--help`).
- **All peers in a fleet must run the same major version.**
  Silent fallback would lose the correlation property exactly
  when an operator is debugging across hosts. Drain procedure
  in
  [`guides/operate/roll-fleet-upgrade.md`](guides/operate/roll-fleet-upgrade.md).

### TUI dashboard

- Single sortable table mounting local own, local other-user
  (with sudo), and remote rows. The dedicated remote-sessions
  section is gone.
- `kill ALL uxon sessions` action renamed to
  `kill all reachable users`; confirmation phrase is now
  `kill-all-reachable` (was `kill-all-global`).
- The `k` keybinding (remote-only kill) is removed. `d` covers
  all kills now — local rows and remote rows alike.
- `s` cycles dashboard sort (cpu → ram → last → name); `S`
  toggles direction.
- `[tui.table]` config block (columns, default_sort_by).

## 1.x → 2.0

- **Defaults moved.** `allowed_roots` defaults to `[]` and
  `new_project_root` defaults to `~/projects`. Existing
  deployments override both — no action required if your
  `config.toml` already sets them.
- **Log directory default.** The developer-facing `debug` and
  `metrics` channels default to `${XDG_STATE_HOME:-~/.local/state}/uxon`.
  Set `UXON_LOG_DIR=/old/path/here` in the launch user's
  environment to preserve the previous location. (Audit events
  go to journald / syslog regardless — `UXON_LOG_DIR` only ever
  scoped the developer channels.)
- **Internal agent material untracked.** `AGENTS.md`,
  `CLAUDE.md`, `.claude/`, `docs/plans/`, `docs/superpowers/`,
  `docs/prototypes/` are no longer tracked. Operators do not
  need to do anything.

## Multi-agent config schema (1.3)

The flat `default_claude_args` key is removed. Config uses
nested tables:

```toml
[agents]
enabled = ["claude", "cursor"]
default = "claude"

[agents.claude]
default_args = []

[agents.codex]
default_args = []

[agents.cursor]
default_args = []
```

Manual migration per host: replace the flat
`default_claude_args = [...]` line with the nested `[agents]`
tables, include only agents installed on that host in
`enabled`, then run `uxon doctor` to verify. If the flat
`default_claude_args` key is still present on load, `uxon` fails
with a clear error pointing here.

## Related

- [`CHANGELOG.md`](../CHANGELOG.md) — full per-version log.
- [`guides/operate/roll-fleet-upgrade.md`](guides/operate/roll-fleet-upgrade.md) — rolling-upgrade procedure for team·N.
