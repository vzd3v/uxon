# Audit events

`uxon` emits one structured audit event per substantive operator
gesture to the host's platform log channel — journald native protocol
on systemd hosts, `/dev/log` syslog otherwise.  Sink detection is
automatic and one-shot per process; the wire layer is stdlib-only.

This document is the **event reference**: what each event means, when
it fires, what fields it carries.  For operational topology (where
events land, ACLs, rotation) and for copy-pasteable `journalctl`
recipes see [`../guides/operate/forward-audit-to-collector.md`](../guides/operate/forward-audit-to-collector.md).
For the config keys that gate the channel (`audit.enabled`,
`audit.syslog_facility`) see
[`configuration.md`](configuration.md).

## Envelope

Every event carries the same envelope.  Fields are written in the
order shown; readers must not assume order, but writers commit to
this set.

| Field           | Type    | Notes                                                                                                  |
|-----------------|---------|--------------------------------------------------------------------------------------------------------|
| `v`             | int     | Schema version. Currently `2`.                                                                         |
| `event`         | string  | Event name from the [alphabet below](#event-alphabet).                                                 |
| `outcome`       | string  | One of `ok`, `denied`, `error`, `not_found`, `skipped`. Default `ok`.                                  |
| `ts`            | string  | ISO-8601 UTC, millisecond precision (`2026-05-06T10:11:12.345Z`).                                       |
| `host`          | string  | `socket.gethostname()` of the emitting host.                                                            |
| `uxon_version`  | string  | Package version of the emitter.                                                                         |
| `process_user`  | string  | Login identity from `/proc/self/loginuid`; falls back to the real process UID when unavailable.        |
| `process_uid`   | int     | UID corresponding to `process_user`. Environment variables cannot override it.                         |
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
| `skipped`   | A safe optional action was deliberately not run because its recorded identity or config no longer matched. |

**State-changing events emit on both success and failure.** Launch and kill
events report their terminal outcome. Attach replaces the uxon process image,
so its `*.dispatch` event truthfully records policy resolution and handoff;
`ok` means dispatch was attempted, not that the interactive tmux/SSH client
eventually exited successfully.

## Event alphabet

| Event                | When it fires                                                                                            | Extra fields beyond envelope                                                                                                  | Outcomes observed                |
|----------------------|----------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------|----------------------------------|
| `cli.start`          | Non-TUI subcommand startup, after argv parse.  Skipped for `--help`, `--version`, and the bare-TUI invocation (which emits `tui.open` instead). | `flags` (names of parsed Uxon-owned options only; values and forwarded agent args are excluded), `profiles_enabled` (launch profile ids), `enable_all_users_list` (bool), `audit_enabled` (bool, always `true`), `allowed_roots_count` (int), `remote_hosts_count` (int) | `ok` |
| `tui.open`           | TUI process started (`uxon` with no args on a TTY).                                                      | (envelope only)                                                                                                              | `ok`                             |
| `session.new`        | Managed launch record was finalized and fsynced, then the pane was released. Dry runs emit after planning without creating a session. | `profile` (launch profile id), `agent` (underlying agent id), `target_user`, `project` (abs path), `branch` (or empty), `session`, `dry_run`; `error` and `error_type` only on `outcome=error` | `ok`, `error`                    |
| `worktree.create`    | uxon created a git worktree (`-w` / TUI new-worktree). Emitted **in addition to** the launched session's `session.new`. | `profile` (launch profile id), `agent`, `project` (repo root, abs path), `branch`, `path` (worktree dir), `base` (`local` \| `remote`), `session`          | `ok`                             |
| `session.attach.dispatch` | Local `uxon attach` or TUI Enter resolved the target and dispatched the tmux client. | `session`, `target_user` | `ok`, `denied`, `not_found` |
| `session.ended`      | A wrapped subprocess (TUI launch) returned.                                                              | `session`, `rc`, `wall_seconds`, `error` (string ≤256 chars; only on `outcome=error`)                                       | `ok`, `error`                    |
| `session.kill`       | Local `uxon kill` or TUI `d` on a local row.                                                             | `session`, `target_user`, `profile`, `agent`, `force` (bool), `dry_run`; `rc` when tmux fails, or a generic `error` when post-kill cleanup fails | `ok`, `denied`, `error`, `not_found` |
| `session.kill_all`   | `uxon kill-all` or TUI `D`.                                                                              | `target_users` (list), `killed_count` (int), `dry_run`                                                                       | `ok`, `error`                    |
| `runtime.prepare` | uxon started or created the selected workload resource before launch. A ready resource is a no-op. | `action` (`start` \| `create`), `runtime_resource`, `error` (only on error) | `ok`, `error` |
| `runtime.session_stop` | uxon ran the selected runtime's `session.stop_command`, or safely skipped it. | `runtime`, `runtime_resource`, `action` (`stop` \| `skip`), `session`, `target_user`, typed `reason` on a safe skip, `error` only on execution error | `ok`, `error`, `skipped` |
| `attach.remote.out.dispatch` | Local TUI/CLI resolved a peer attach and dispatched SSH (caller side). | `peer_name`, `ssh_alias`, `target_user`, `target_session`, `correlation_id` | `ok`, `not_found` |
| `attach.remote.in.dispatch` | Peer's `uxon attach` resolved and dispatched tmux. Replaces `session.attach.dispatch` on the peer. | `target_user`, `target_session`, `correlation_id` | `ok`, `denied`, `not_found` |
| `kill.remote.out`    | Local `uxon kill --host` / TUI `d` on a remote row (caller side).                                         | `peer_name`, `ssh_alias`, `target_user`, `target_session`, `force`, `dry_run`, `correlation_id`, `rc` (int) + `error` (string ≤256 chars) on `outcome=error` | `ok`, `error`                    |
| `kill.remote.in`     | Peer's own `uxon kill` invoked over SSH (peer side).  Replaces `session.kill` on peer.                    | `session`, `target_user`, `force`, `dry_run`, `correlation_id`                                                              | `ok`, `denied`, `error`, `not_found` |
| `list.peek`          | Local enumeration of *other* users' sessions (`uxon list --all-users` / TUI with the gate enabled).      | `scope_users` (list), `scope_skipped` (list)                                                                                 | `ok`                             |
| `list.remote.in`     | Peer's own `uxon list --json` invoked over SSH.  Replaces `list.peek` on peer.                            | `scope` (`own` \| `all-users`), `correlation_id`                                                                             | `ok`, `denied`                   |
| `git.remote.create`  | `uxon new --git-remote <profile>` reached the external-repo create step.                                 | `profile` (launch profile id), `git_remote_profile`, `repo`, `creds_user`, `rc`                                               | `ok`, `error`                    |
| `config.error`       | Startup config load failed and `main()` is about to exit non-zero.                                       | `path`, `error` (first 256 chars)                                                                                            | `error`                          |

### Local vs peer-side: `replaces` semantics

When a gesture crosses an SSH boundary (`uxon attach --host`,
`uxon kill --host`, `uxon list --host`), two events are emitted —
one per side:

- **Caller side** emits `attach.remote.out.dispatch` or `kill.remote.out`.
  No `list.remote.out` exists; the local
  enumeration emits `list.peek` instead.
- **Peer side**, detected by `SSH_CONNECTION` in the env, emits
  `attach.remote.in.dispatch`, `kill.remote.in`, or `list.remote.in`.
  `attach.remote.in.dispatch`
  replaces `session.attach.dispatch`, `kill.remote.in` replaces `session.kill`,
  `list.remote.in` replaces `list.peek` — never both.  This keeps the
  cross-host audit trail at one record per side, no double-counting.

### Cross-host correlation

For each remote gesture the caller generates a UUIDv4 and passes it
to the peer via an internal CLI flag (`--audit-correlation-id
<uuid>`, hidden from `--help`).  Both sides emit it under
`correlation_id`, so a single `journalctl … CORRELATION_ID=<uuid>`
query returns the full pair across the two hosts. A mismatched peer rejects
the SSH invocation outright — silent fallback
would lose the correlation property exactly when an operator is
debugging across hosts.

## Disabling the channel

Set `audit.enabled = false` in `/etc/uxon/config.toml` to silence the channel
entirely (no events, no sink detection).  There is no
environment-variable override — the only kill-switch is the config
table.  See [`configuration.md`](configuration.md) for the
`[audit]` table reference.

## Schema stability

The envelope and event alphabet are versioned by the `v` field
(currently `2`).  Within a major release line `uxon` will not remove
events or rename fields; new events and new optional fields may be
added.  Breaking schema changes bump the major version and `v`.
