# `--json` wire schema

`uxon list`, `uxon doctor`, `uxon version`, `uxon kill`, and
`uxon kill-all` accept `--json` and emit a versioned envelope. The
multi-host aggregator consumes the same shape over SSH, so the
schema is part of the public contract.

The collector validates present known session and host-stat fields. Missing
fields and unknown additions remain usable within the same schema version.
Wrong types, invalid identities, duplicate session identities, nonpositive PIDs,
and negative or nonfinite metrics reject that peer's snapshot as
`invalid_response`, with the field or row in the diagnostic. Other peers remain
available; rejected rows cannot be selected for actions.

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
        "exited": false,
        "has_diagnostics": false,
        "active_exit_status": null,
        "legacy": false
      }
    ]
  }
}
```

- `all_users` — `true` when invoked with `--all-users` (or peer
  invoked with the same flag through the aggregator).
- `scope_users` — the launch user plus configured session users whose selected
  execution backend passes the fixed cross-user target probe. The launch user
  is always included and never appears in `scope_skipped`.
- `scope_skipped` — configured session users whose target probe failed.
- `legacy` — `true` when the session lives under one of
  `legacy_session_prefixes` rather than the active `session_prefix`.
- `identity` — optional object with string fields `user`, `name`, `session_id`,
  `created`, `launch_nonce`, `socket_path`, `execution_backend` and
  `execution_fingerprint`. `created` is the raw tmux creation value, distinct
  from the row's ISO timestamp. The nonce identifies a launch; tmux IDs are
  scoped to one server lifetime. Names are mutable labels. Consumers preserve
  this object through confirmation and send it as `--expect-session` for attach
  and kill. Missing identity permits display but does not enable a conditional
  row action; missing nonce or protected authority refuses removal. Identity
  fields are additive within wire version 3.
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
- `exited` — `true` when all inspected terminals have finished; live PID lists
  exclude finished terminals across all windows.
- `has_diagnostics` — `true` when any retained terminal has finished, including
  sessions that still have live terminals.
- `active_exit_status` — the selected terminal's numeric exit status, or `null`
  when it is live or no numeric status is available. These three fields are
  additive; older peers may omit them.

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
    "tmux": {"path": "/usr/bin/tmux", "socket": "/tmp/tmux-1001/uxon-local.sock"},
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
invoked; default doctor does not poll configured peers. Operator-defined
execution helpers may themselves use remote transports.

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

```json
{
  "kind": "kill",
  "data": {
    "target": "uxon-myproj@claude_work",
    "name": "uxon-myproj@claude_work",
    "user": "alice",
    "target_user": "alice",
    "session_id": "$1",
    "launch_nonce": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "socket": "/run/uxon/alice/tmux.sock",
    "action": "killed",
    "cleanup_outcome": "ok",
    "error": "",
    "dry_run": false
  }
}
```

- `target` / `name` — current session label when observed, or the captured
  label when the session is already absent.
- `user` / `target_user` — target launch account.
- `session_id` / `launch_nonce` — the captured identity of the requested session.
- `socket` — target tmux socket; local responses include it.
- `reachable` — local responses include `true` after target authorization.
- `action` — `"killed"`, `"already_absent"`, `"would-kill"` or `"failed"`.
  `already_absent` means the identity is proved absent without an acknowledged
  removal by this request, including a lost response followed by safe verification.
- `cleanup_outcome` — `"ok"`, `"error"` or `"not_run"`.
- `error` — concrete mutation, authority, reachability or cleanup reason;
  empty on success. Cleanup failure can accompany `action = "killed"`.

Remote responses include the configured peer in the envelope's `host`.
A remote dry-run also includes `ssh_argv`; it resolves the target but does
not dispatch a removal. `kill --json` requires `--force` or `--dry-run`.

The initiating host emits `kill.remote.out`; the target emits
`session.kill`. Their correlation UUID and captured session identity join
the operation. JSON describes its operator-facing result; see the
[audit reference](audit-events.md) for event fields.

## `kind = "kill-all"`

The `data` object contains `user`, `socket`, `dry_run`, `killed_count`,
`failed_count` and `sessions`. Each entry in `sessions` contains `name`,
`user`, `session_id`, `launch_nonce`, `action`, `cleanup_outcome` and `error`,
with the same semantics as single removal. `failed_count` includes required
cleanup failures; `killed_count` counts terminals removed even when their
subsequent cleanup failed. An empty list means no targets were selected.

The target set is frozen before confirmation. Every selected target receives
a result; one failure does not prevent independent targets from being attempted.
A partial failure returns exit code `1`. `kill-all --json` requires `--force`
or `--dry-run`.

## Stable error tags

Peer-side `uxon` writes specific tags to **stderr** so the
aggregator can branch deterministically without parsing free-form
messages:

| Tag | Meaning |
|---|---|
| `uxon-error: not-reachable` | Caller cannot `sudo -n -H -u <user> --` (no NOPASSWD). Exit code 1. |
| `uxon-error: all-users-disabled` | Peer's config has `enable_all_users_list = false`. Exit code 1. The aggregator detects this tag and falls back to own-only `list --json`, stamping the snapshot with `scope_limited = true`. |

Anything else on stderr is treated as a generic SSH/peer failure
and contributes no session rows or metrics. A decodable structured failure
on stdout preserves its diagnostic metadata even with a nonzero exit status.

## JSON Lines (`--all-hosts --json`)

`uxon list --all-hosts --json` emits one envelope per source —
local first, then one per configured peer — separated by newline.
The reader processes each envelope independently. Remote `--host` and
`--all-hosts` envelopes add optional fields under `data`:

| Field | Shape | Meaning |
|---|---|---|
| `peer` | `{ "uxon_version": string, "schema_version": string }` | Observed peer identity; either key may be absent. The enclosing `uxon_version` identifies the envelope producer. |
| `error` | `{ "code": string, "message": string }` | Failed live request. Sessions are empty; consumers must reject any non-null error, including a same-schema relayed response. |

Error codes: `update_remote`, `update_local`, `incompatible`, `timeout`,
`ssh_error`, `invalid_response`, `remote_error`, `fetch_error`. Package version
is diagnostic only. An older numeric schema requires updating the remote;
a newer one requires updating the local client; unknown/missing schemas are
incompatible. Metadata remains available on incompatible responses.
Local envelopes omit these fields. Remote session data is not persisted.
A failed peer never contributes preceding in-memory sessions, counts, or metrics.
Healthy peers remain visible.

## Versioning

Within `schema_version = "3"` `uxon` will not remove fields or
rename them; new optional fields may be added. Breaking changes
bump the major version and the `schema_version`.
