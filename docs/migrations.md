# Migration notes

Per-version upgrade notes for changes that need operator
attention beyond a routine `pipx upgrade`. For the full release
log see [`CHANGELOG.md`](../CHANGELOG.md).

## 3.3.0

### Audit channel — new sink

The 3.3.0 release introduces a dedicated audit channel and
removes the legacy TUI event log.

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
`enabled`, then run `uxon doctor` to verify. If the legacy flat
key is present on load, `uxon` fails with a clear error
pointing here.

## Related

- [`CHANGELOG.md`](../CHANGELOG.md) — full per-version log.
- [`guides/operate/roll-fleet-upgrade.md`](guides/operate/roll-fleet-upgrade.md) — rolling-upgrade procedure for team·N.
