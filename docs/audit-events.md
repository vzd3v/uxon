# Audit events

`uxon` emits one structured audit event per substantive operator
gesture to the host's platform log channel — journald native protocol
on systemd hosts, `/dev/log` syslog otherwise.  Sink detection is
automatic and one-shot per process; the wire layer is stdlib-only.

This document is the **event reference**: what each event means, when
it fires, what fields it carries.  For operational topology (where
events land, ACLs, rotation) and for copy-pasteable `journalctl`
recipes see [`docs/deployment.md`](deployment.md#audit-channel).
For the config keys that gate the channel (`audit.enabled`,
`audit.syslog_facility`) see
[`docs/configuration.md`](configuration.md).

## Envelope

Every event carries the same envelope.  Fields are written in the
order shown; readers must not assume order, but writers commit to
this set.

| Field           | Type    | Notes                                                                                                  |
|-----------------|---------|--------------------------------------------------------------------------------------------------------|
| `v`             | int     | Schema version.  Currently `1`.                                                                        |
| `event`         | string  | Event name from the [alphabet below](#event-alphabet).                                                 |
| `outcome`       | string  | One of `ok`, `denied`, `error`, `not_found`.  Default `ok`.                                            |
| `ts`            | string  | ISO-8601 UTC, millisecond precision (`2026-05-06T10:11:12.345Z`).                                       |
| `host`          | string  | `socket.gethostname()` of the emitting host.                                                            |
| `uxon_version`  | string  | Package version of the emitter.                                                                         |
| `caller_user`   | string  | Human operator (resolved through `SUDO_USER` if `uxon` was sudo'd).                                     |
| `caller_uid`    | int     | UID of `caller_user`.                                                                                   |
| `launch_user`   | string  | The user `uxon` is operating *as* (post `sudo -iu`).  Same as `caller_user` for self-only gestures.    |
| `pid` / `ppid`  | int     | Emitter process and its parent.                                                                         |
| `subcmd`        | string  | The `uxon` subcommand under which the event fired (`attach`, `kill`, `run`, …).                         |
| `ssh_client`    | string  | Present **only** on peer-inbound emits, copied from `SSH_CONNECTION`.  Absent locally.                  |

On the journald native sink each envelope field is reachable as a
first-class `FIELD=value` selector (uppercased — journald wire
convention).  On the `/dev/log` syslog fallback the body lands as a
single `@cee: {…}` JSON line readable via `journalctl … -o json | jq`.

journald additionally stamps its own metadata for free (`_PID`,
`_UID`, `_AUDIT_LOGINUID`, `_CMDLINE`, `_HOSTNAME`,
`_SYSTEMD_UNIT`); `uxon` does not duplicate those.

## Outcome semantics

`outcome` is a closed enum:

| Value       | When                                                                                              |
|-------------|---------------------------------------------------------------------------------------------------|
| `ok`        | The operation completed as intended.  Default if `outcome` is omitted.                            |
| `denied`    | A policy / ACL gate refused the operation (sudo unreachable, `enable_all_users_list = false`, …). |
| `error`     | The operation failed for a reason other than policy (subprocess non-zero, exception, ssh fail).   |
| `not_found` | The named target did not exist (session id unknown, peer alias unknown).                          |

**State-changing events emit on both success and failure.**  A refused
or errored attach / kill / launch is more interesting to an auditor
than a successful one, so the failure path always lands a record with
the appropriate `outcome` — querying `OUTCOME != "ok"` is a complete
sweep of everything that didn't go through.

## Event alphabet

| Event                | When it fires                                                                                            | Extra fields beyond envelope                                                                                                  | Outcomes observed                |
|----------------------|----------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------|----------------------------------|
| `cli.start`          | Top of `main()` after argv parse.  Skipped for pure `--help`/`--version`.                                | `flags` (sanitised list), `agents_enabled` (list), `enable_all_users_list` (bool), `audit_enabled` (bool, continuity marker), `allowed_roots_count` (int), `remote_hosts_count` (int) | `ok`                             |
| `tui.open`           | TUI process started (`uxon` with no args on a TTY).                                                      | (envelope only)                                                                                                              | `ok`                             |
| `session.new`        | `uxon run` / `uxon new` / TUI launch-new created and dispatched a session.                               | `agent` (`claude` \| `codex` \| `cursor`), `project` (abs path), `branch` (or empty), `session`, `dry_run`                   | `ok`, `error`                    |
| `session.attach`     | Local `uxon attach` or TUI Enter on a local row.                                                         | `session`, `target_user`                                                                                                     | `ok`, `denied`, `error`, `not_found` |
| `session.ended`      | A wrapped subprocess (TUI launch) returned.                                                              | `session`, `rc`, `wall_seconds`                                                                                              | `ok`, `error`                    |
| `session.kill`       | Local `uxon kill` or TUI `d` on a local row.                                                             | `session`, `target_user`, `force` (bool), `dry_run`                                                                          | `ok`, `denied`, `error`, `not_found` |
| `session.kill_all`   | `uxon kill-all` or TUI `D`.                                                                              | `target_users` (list), `killed_count` (int), `dry_run`                                                                       | `ok`, `error`                    |
| `attach.remote.out`  | Local TUI/CLI dispatching a peer attach over SSH (caller side of the wire).                              | `peer_name`, `ssh_alias`, `target_user`, `target_session`, `correlation_id`                                                  | `ok`, `error`                    |
| `attach.remote.in`   | Peer's own `uxon attach` invoked over SSH (peer side of the wire).  Replaces `session.attach` on peer.   | `target_user`, `target_session`, `correlation_id`                                                                            | `ok`, `denied`, `error`, `not_found` |
| `kill.remote.out`    | Local `uxon kill --host` / TUI `d` on a remote row (caller side).                                         | `peer_name`, `ssh_alias`, `target_user`, `target_session`, `force`, `dry_run`, `correlation_id`                              | `ok`, `error`                    |
| `kill.remote.in`     | Peer's own `uxon kill` invoked over SSH (peer side).  Replaces `session.kill` on peer.                    | `session`, `target_user`, `force`, `correlation_id`                                                                          | `ok`, `denied`, `error`, `not_found` |
| `list.peek`          | Local enumeration of *other* users' sessions (`uxon list --all-users` / TUI with the gate enabled).      | `scope_users` (list), `scope_skipped` (list)                                                                                 | `ok`                             |
| `list.remote.in`     | Peer's own `uxon list --json` invoked over SSH.  Replaces `list.peek` on peer.                            | `scope` (`own` \| `all-users`), `correlation_id`                                                                             | `ok`, `denied`                   |
| `git.remote.create`  | `uxon new --git-remote <profile>` reached the external-repo create step.                                 | `profile`, `repo`, `creds_user`, `rc`                                                                                        | `ok`, `error`                    |
| `config.error`       | Startup config load failed and `main()` is about to exit non-zero.                                       | `path`, `error` (first 256 chars)                                                                                            | `error`                          |

### Local vs peer-side: `replaces` semantics

When a gesture crosses an SSH boundary (`uxon attach --host`,
`uxon kill --host`, `uxon list --host`), two events are emitted —
one per side:

- **Caller side** emits `*.remote.out` (`attach.remote.out`,
  `kill.remote.out`).  No `list.remote.out` exists; the local
  enumeration emits `list.peek` instead.
- **Peer side**, detected by `SSH_CONNECTION` in the env, emits
  `*.remote.in` instead of the local equivalent.  `attach.remote.in`
  replaces `session.attach`, `kill.remote.in` replaces `session.kill`,
  `list.remote.in` replaces `list.peek` — never both.  This keeps the
  cross-host audit trail at one record per side, no double-counting.

### Cross-host correlation

For each remote gesture the caller generates a UUIDv4 and passes it
to the peer via an internal CLI flag (`--audit-correlation-id
<uuid>`, hidden from `--help`).  Both sides emit it under
`correlation_id`, so a single `journalctl … CORRELATION_ID=<uuid>`
query returns the full pair across the two hosts.  Older peers
without the flag reject the SSH invocation outright — silent fallback
would lose the correlation property exactly when an operator is
debugging across hosts.

## Disabling the channel

Set `audit.enabled = false` in `config.toml` to silence the channel
entirely (no events, no sink detection).  There is no
environment-variable override — the only kill-switch is the config
table.  See [`docs/configuration.md`](configuration.md) for the
`[audit]` table reference.

## Schema stability

The envelope and event alphabet are versioned by the `v` field
(currently `1`).  Within a major release line `uxon` will not remove
events or rename fields; new events and new optional fields may be
added.  Breaking schema changes bump the major version and `v`.
