# Audit channel design

`uxon` emits one structured event per substantive operator gesture
to the host's platform log channel — journald native protocol on
systemd hosts, `/dev/log` syslog otherwise. This page explains why,
how the channel is structured, and what `correlation_id` buys you
across hosts.

For the per-event field reference see
[`reference/audit-events.md`](../reference/audit-events.md).
For operational topology and copy-pasteable `journalctl` recipes
see [`guides/operate/forward-audit-to-collector.md`](../guides/operate/forward-audit-to-collector.md).

## Why journald (and `/dev/log` fallback)

- **The OS already has a log channel.** Reinventing one would mean
  another file path to chmod, rotate, ship, and recover from.
- **journald carries metadata for free.** `_PID`, `_UID`,
  `_AUDIT_LOGINUID`, `_CMDLINE`, `_HOSTNAME`, `_SYSTEMD_UNIT` —
  `uxon` does not duplicate those.
- **Tamper-evidence by file ownership.** Under the host-wide
  install (`/opt/uxon/venv`, root-owned), the launch user can
  append events but cannot edit the trail. `uxon` does **not**
  try to defend at runtime against a launch user running their
  own copy — that's what the install path enforces.
- **Stdlib only.** No `python-systemd` dependency; the wire layer
  speaks the journald native protocol directly. Systems without a
  journald socket fall through to `/dev/log` (RFC-style syslog
  with `@cee:` JSON body).

## What's recorded

Every event carries the same envelope: `v` (schema version), `event`
(name), `outcome` (`ok` / `denied` / `error` / `not_found`), `ts`
(ISO-8601 UTC ms), `host`, `uxon_version`, `caller_user`,
`caller_uid`, `launch_user`, `pid` / `ppid`, `subcmd`, plus
`ssh_client` only on peer-inbound events. Per-event extra fields
are listed in [`reference/audit-events.md`](../reference/audit-events.md).

The event alphabet covers:

- CLI lifecycle (`cli.start`, `config.error`).
- TUI lifecycle (`tui.open`).
- Session state changes (`session.new`, `session.attach`,
  `session.ended`, `session.kill`, `session.kill_all`).
- Local enumeration (`list.peek`).
- Cross-host pairs (`attach.remote.{out,in}`,
  `kill.remote.{out,in}`, `list.remote.in`).
- Git-remote creation (`git.remote.create`).

**State-changing events emit on both success and failure.** A
refused / errored / not-found gesture is more interesting to an
auditor than a successful one, so the failure path always lands a
record with the appropriate `outcome`. Querying `OUTCOME != "ok"`
is a complete sweep of everything that didn't go through.

## Local vs. peer-side: `replaces` semantics

When a gesture crosses an SSH boundary, two events are emitted —
one per side:

- **Caller side** emits `*.remote.out` (`attach.remote.out`,
  `kill.remote.out`). No `list.remote.out` exists; the local
  enumeration emits `list.peek` instead.
- **Peer side**, detected by `SSH_CONNECTION` in env, emits
  `*.remote.in` instead of the local equivalent.
  `attach.remote.in` replaces `session.attach`,
  `kill.remote.in` replaces `session.kill`, `list.remote.in`
  replaces `list.peek` — never both. This keeps the cross-host
  audit trail at one record per side, no double-counting.

## Cross-host correlation

For each remote gesture the caller generates a UUIDv4 and passes
it to the peer via an internal `--audit-correlation-id <uuid>`
flag (hidden from `--help`). Both sides emit it under
`correlation_id`, so a single
`journalctl … CORRELATION_ID=<uuid>` query returns the full pair
across the two hosts.

This is the **headline 3.3.0 feature**, but it pays off only if
both hosts' journalds are queryable from one console — i.e. if
there's a central collector. Per-host `journalctl` works for spot
checks, but chasing an incident across 5+ hosts at 3am without
forwarding is an exercise in patience. See
[`guides/operate/forward-audit-to-collector.md`](../guides/operate/forward-audit-to-collector.md)
for the central-forwarding patterns.

Older peers that don't recognise `--audit-correlation-id` reject
the SSH invocation outright. Silent fallback would lose the
correlation property exactly when an operator is debugging across
hosts. This is enforced as a wire-schema-version-equivalent
contract — peers must run the same major version.

## Outcome semantics

`outcome` is a closed enum:

| Value | When |
|---|---|
| `ok` | Operation completed as intended. Default if `outcome` omitted. |
| `denied` | Policy / ACL gate refused (sudo unreachable, `enable_all_users_list = false`, …). |
| `error` | Operation failed for reasons other than policy (subprocess non-zero, exception, ssh fail). |
| `not_found` | Named target did not exist (session id unknown, peer alias unknown). |

This is deliberately a closed alphabet so query patterns stay
stable: `OUTCOME != "ok"` is a complete sweep of what didn't go
through, regardless of which event.

## Disabling the channel

Set `audit.enabled = false` in `config.toml` to silence the
channel entirely (no events, no sink detection). There is no
environment-variable override — the only kill-switch is the
config table.

For solo·1 hosts where nobody reads the audit trail this is
harmless to flip off; for any team scenario it should stay on.

## What the channel is not

- **Not a debug log.** `UXON_DEBUG=<topic>` writes to a separate
  per-user JSONL channel (off by default). Audit events are about
  *what the operator did*, not what the code is doing internally.
- **Not a metrics pipeline.** Per-fetch latency goes to a separate
  `metrics.jsonl` (off by default, gated on `UXON_METRICS=1`).
- **Not for compliance certification.** `uxon`'s audit is
  application-level value-add — which session, which agent,
  which project, correlation across hosts. Privileged operations
  (`sudo -iu …`) appear in `sudo`'s own audit trail
  (`auth.log` / journald), which is the OS-level source of truth
  for who-did-what.

## Privacy

The audit channel records `caller_user` (the human) and
`launch_user` (the agent account) on every event. Developers
should know what's recorded and where it goes — see
[`privacy.md`](../privacy.md) for a one-page disclosure operators
can share with their team.

## Related

- [`reference/audit-events.md`](../reference/audit-events.md) —
  per-event field reference.
- [`guides/operate/forward-audit-to-collector.md`](../guides/operate/forward-audit-to-collector.md) —
  central forwarding patterns.
- [`privacy.md`](../privacy.md) — for the team.
