# `--json` wire schema

`uxon list`, `uxon doctor`, `uxon version`, `uxon kill`, and
`uxon kill-all` accept `--json` and emit a versioned envelope. The
multi-host aggregator consumes the same shape over SSH, so the
schema is part of the public contract.

## Envelope

```json
{
  "schema_version": "3",
  "uxon_version": "<emitter version>",
  "kind": "list" | "doctor" | "version" | "kill" | "kill-all",
  "data": { ... kind-specific },
  "host": "<peer name>",
  "host_stats": { ... optional, see below }
}
```

- `schema_version` — bumped only when peers must be upgraded
  together. Mismatch fails the parse loud rather than silently
  dropping fields.
- `host` — added by the aggregator when the envelope came from a
  peer; absent on locally-emitted envelopes.
- `host_stats` — optional, present only on `kind = "list"`
  envelopes from peers that could read `/proc`. Older peers omit
  it; treat missing/null as absent. Additive — adding new keys
  inside the block does not bump `schema_version`, so consumers
  `.get(...)` defensively. Fields:
  - `cpu_pct` (float, host CPU %)
  - `mem_used_kib` / `mem_total_kib` (int)
  - `loadavg_1m` (float — carried on the wire but no longer
    rendered by the dashboard)
  - `uptime_s` (int)
  - `kernel` (string, `uname -r`)

## `kind = "list"`

```json
{
  "kind": "list",
  "data": {
    "all_users": true,
    "scope_users": ["nadia-agent", "liam-agent"],
    "scope_skipped": ["ethan-agent"],
    "session_prefix": "uxon-",
    "sessions": [
      {
        "user": "nadia-agent",
        "name": "uxon-myproj@claude_work",
        "short_id": "myproj@claude_work",
        "profile": "claude_work",
        "agent": "claude",
        "execution_backend": "local",
        "runtime": "direct",
        "runtime_kind": "direct",
        "runtime_resource": "",
        "attached": false,
        "windows": "1",
        "created": "2026-05-07T09:11:24Z",
        "last_attached": "2026-05-07T09:42:01Z",
        "pane_pids": [12345],
        "active_pid": 12345,
        "active_cmd": "claude",
        "active_path": "/srv/projects/myproj",
        "cpu_pct": 1.4,
        "rss_kib": 2408192,
        "runtime_down": false,
        "legacy": false
      }
    ]
  }
}
```

- `all_users` — `true` when invoked with `--all-users` (or peer
  invoked with the same flag through the aggregator).
- `scope_users` — the *reachable* subset of `session_users` (only
  users the caller can `sudo -n -H -u USER --` to without a password).
- `scope_skipped` — users in `session_users` that the caller
  cannot sudo into. Optional — older peers omit it; treat
  missing/null as `[]`.
- `legacy` — `true` when the session lives under one of
  `legacy_session_prefixes` rather than the active `session_prefix`.
- `windows` — kept as a string (tmux emits it as text via
  `#{session_windows}`).
- `profile` — launch profile id from the verified launch record. For
  unmanaged sessions, the session-name suffix is used as a display
  fallback.
- `agent` — underlying agent id from the verified launch record, or
  `""` for unmanaged sessions.
- `execution_backend` — verified target-user execution backend id.
- `runtime` / `runtime_kind` / `runtime_resource` — verified workload
  runtime id, implementation kind, and resolved resource. Direct and unmanaged
  sessions carry empty resource values.
- `runtime_down` — `true` when a record-backed workload resource is known to
  be stopped or unresolved during liveness probing.

## `kind = "doctor"`

```json
{
  "kind": "doctor",
  "data": {
    "caller_user": "nadia",
    "launch_user": "nadia-agent",
    "config_paths": ["/etc/uxon/config.toml"],
    "allowed_roots": ["/srv/projects"],
    "new_project_root": "/srv/projects",
    "tmux": {"path": "/usr/bin/tmux", "socket": "/tmp/uxon-nadia-agent-local.sock"},
    "agents": {"claude": {"path": "...", "status": "ok", "version": "...", "error": null}},
    "launch_profiles": [...],
    "execution_backends": [...],
    "current_socket_sessions": [...],
    "legacy_default_socket_sessions": [...],
    "git_create_enabled": true,
    "git_remote_profiles": [...],
    "runtimes": [...],
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

Each `execution_backends` row carries `launch_user`, `backend`, `kind`, a
static config `fingerprint`, probe `status`, and an `error` string when the
fixed target UID/GID probe fails. The fingerprint is diagnostic only.

## `kind = "version"`

```json
{
  "kind": "version",
  "data": {
    "uxon_version": "4.0.0",
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
    "target": "uxon-myproj@claude_work",
    "user": "nadia-agent",
    "socket": "/tmp/uxon-nadia-agent-local.sock",
    "action": "killed",
    "dry_run": false
  }
}
```

A cross-user kill (`--user <name>` where the target differs from
the caller's launch user) adds two fields:

```json
    "target_user": "liam-agent",
    "reachable": true
```

Remote kill (`--host <alias>`) carries an extra `ssh_argv`
field on `--dry-run` (the SSH command line that would be run);
non-dry-run remote kill goes through and emits the same
`action`/`dry_run` shape.

- `target` — the resolved session name as `tmux` sees it.
- `user` — the launch user the kill ran under (caller's launch
  user for self-only, the `--user` argument for cross-user).
- `socket` — path of the per-user tmux socket the kill ran
  against.
- `target_user` — **cross-user only.** The target's launch user.
  Absent on a self-kill (where it would just equal `user`).
- `reachable` — **cross-user only.** `true` when the per-target
  sudo probe succeeded. Absent on a self-kill.
- `action` — `"killed"`, `"would-kill"` (dry-run), or
  `"failed"`.

`kill --json` is non-interactive — refuses to run without
`--force` or `--dry-run`.

The audit channel records `kill.remote.out` on the initiating host and
`session.kill` on the target host, with the operational
fields (`session`, `target_user`, `force`, `dry_run`,
`outcome`); the JSON envelope describes the **operator-facing
result** of the call rather than the audit-record shape.

## `kind = "kill-all"`

```json
{
  "kind": "kill-all",
  "data": {
    "user": "nadia-agent",
    "socket": "/tmp/uxon-nadia-agent-local.sock",
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
| `uxon-error: not-reachable` | Caller cannot `sudo -n -H -u <user> --` (no NOPASSWD). Exit code 1. |
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

Within `schema_version = "3"` `uxon` will not remove fields or
rename them; new optional fields may be added. Breaking changes
bump the major version and the `schema_version`.
