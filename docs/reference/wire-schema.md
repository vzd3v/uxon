# `--json` wire schema

`uxon list`, `uxon doctor`, `uxon version`, `uxon kill`, and
`uxon kill-all` accept `--json` and emit a versioned envelope. The
multi-host aggregator consumes the same shape over SSH, so the
schema is part of the public contract.

## Envelope

```json
{
  "schema_version": "1",
  "uxon_version": "<emitter version>",
  "kind": "list" | "doctor" | "version" | "kill" | "kill-all",
  "data": { ... kind-specific },
  "host": "<peer name>"
}
```

- `schema_version` — bumped only when peers must be upgraded
  together. Mismatch fails the parse loud rather than silently
  dropping fields.
- `host` — added by the aggregator when the envelope came from a
  peer; absent on locally-emitted envelopes.

## `kind = "list"`

```json
{
  "kind": "list",
  "data": {
    "all_users": true,
    "scope_users": ["alice_agent", "bob_agent"],
    "scope_skipped": ["carol_agent"],
    "session_prefix": "uxon-",
    "sessions": [
      {
        "user": "alice_agent",
        "name": "uxon-myproj@claude",
        "short_id": "myproj@claude",
        "agent": "claude",
        "attached": false,
        "windows": "1",
        "created": "2026-05-07T09:11:24Z",
        "last_attached": "2026-05-07T09:42:01Z",
        "pane_pids": [12345],
        "active_pid": 12345,
        "active_cmd": "claude",
        "active_path": "/srv/projects/myproj",
        "cpu_pct": 1.4,
        "rss_kib": 2_408_192,
        "legacy": false
      }
    ]
  }
}
```

- `all_users` — `true` when invoked with `--all-users` (or peer
  invoked with the same flag through the aggregator).
- `scope_users` — the *reachable* subset of `session_users` (only
  users the caller can `sudo -niu` to without a password).
- `scope_skipped` — users in `session_users` that the caller
  cannot sudo into. Optional — older peers omit it; treat
  missing/null as `[]`.
- `legacy` — `true` when the session lives under one of
  `legacy_session_prefixes` rather than the active `session_prefix`.
- `windows` — kept as a string (tmux emits it as text via
  `#{session_windows}`).

## `kind = "doctor"`

```json
{
  "kind": "doctor",
  "data": {
    "caller_user": "alice",
    "launch_user": "alice_agent",
    "config_paths": {"repo": "...", "project": "..."},
    "allowed_roots": ["/srv/projects"],
    "new_project_root": "/srv/projects",
    "agents": [{"id": "claude", "path": "...", "version": "..."}],
    "sockets": [...],
    "sessions": [...],
    "audit": {"enabled": true, "sink": "journal"},
    "issues": [...],
    "remote_hosts": [...]   // only when --remote was passed
  }
}
```

`remote_hosts` is present only when `uxon doctor --remote` was
invoked; the default `uxon doctor` does zero SSH I/O.

`audit.sink` carries the raw sink id: `"journal"`, `"syslog"`,
or `"none"`. The human `uxon doctor` text output uppercases this
to `journald-native` / `syslog` / `no-sink` for readability — the
JSON envelope keeps the raw value.

## `kind = "version"`

```json
{
  "kind": "version",
  "data": {
    "uxon_version": "3.3.0",
    "commit": "5a50ec3",
    "commit_dirty": false
  }
}
```

`commit` / `commit_dirty` are only populated in dev checkouts;
released wheels carry version only.

## `kind = "kill"`

Local kill (own user):

```json
{
  "kind": "kill",
  "data": {
    "target": "uxon-myproj@claude",
    "user": "alice_agent",
    "target_user": "alice_agent",
    "reachable": true,
    "socket": "/tmp/uxon-alice_agent.sock",
    "action": "killed",
    "dry_run": false
  }
}
```

Remote kill (`--host <alias>`) carries an extra `ssh_argv`
field on `--dry-run` (the SSH command line that would be run);
non-dry-run remote kill goes through and emits the same
`action`/`dry_run` shape.

- `target` — the resolved session name as `tmux` sees it.
- `user` — the launch user the kill ran under (caller's launch
  user for self-only, the `--user` argument for cross-user).
- `target_user` — the target's launch user (identical to
  `user` on self-kill, distinct on cross-user / cross-host).
- `reachable` — `true` when the per-target sudo probe
  succeeded; only meaningful for cross-user `--user` calls.
- `action` — `"killed"`, `"would-kill"` (dry-run), or
  `"failed"`.

`kill --json` is non-interactive — refuses to run without
`--force` or `--dry-run`.

The audit channel records the canonical event (`session.kill` /
`kill.remote.in` / `kill.remote.out`) with the operational
fields (`session`, `target_user`, `force`, `dry_run`,
`outcome`); the JSON envelope describes the **operator-facing
result** of the call rather than the audit-record shape.

## `kind = "kill-all"`

```json
{
  "kind": "kill-all",
  "data": {
    "user": "alice_agent",
    "socket": "/tmp/uxon-alice_agent.sock",
    "dry_run": false,
    "sessions": [
      {"name": "uxon-foo@claude",  "action": "killed"},
      {"name": "uxon-bar@codex",   "action": "failed"}
    ]
  }
}
```

`sessions[].action` is `"killed"`, `"would-kill"`, or
`"failed"`. An empty `sessions` array means there were no
matching sessions to reap.

`kill-all --json` requires `--force` or `--dry-run`.

## Stable error tags

Peer-side `uxon` writes specific tags to **stderr** so the
aggregator can branch deterministically without parsing free-form
messages:

| Tag | Meaning |
|---|---|
| `uxon-error: not-reachable` | Caller cannot `sudo -niu <user>` (no NOPASSWD). Exit code 1. |
| `uxon-error: all-users-disabled` | Peer's config has `enable_all_users_list = false`. Exit code 1. The aggregator detects this tag and falls back to own-only `list --json`, stamping the snapshot with `scope_limited = true`. |

Anything else on stderr is treated as a generic SSH/peer failure
and falls through to the cache fallback path.

## JSON Lines (`--all-hosts --json`)

`uxon list --all-hosts --json` emits one envelope per source —
local first, then one per configured peer — separated by newline.
The reader processes each envelope independently; a failed peer
emits an envelope with empty `sessions` and `from_cache` /
`scope_limited` markers in the cache-fallback path.

## Versioning

Within `schema_version = "1"` `uxon` will not remove fields or
rename them; new optional fields may be added. Breaking changes
bump the major version and the `schema_version`.
