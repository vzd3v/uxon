# What `uxon` records about you

This page is for developers using a team `uxon` host. Operators
can paste this into their team's onboarding wiki or share the
link as part of new-developer setup.

## The short version

`uxon` records one structured audit event per substantive
gesture you make — opening the TUI, launching a session,
attaching, killing. The trail goes to the host's system log
channel (journald or syslog). Your operator queries it with
`journalctl`, possibly forwarded to a central log collector.

There is no telemetry to a third party. The audit channel is
local to your host.

## What's recorded

Every event carries:

- The kernel audit login identity (`process_user`, `process_uid`). Uxon ignores
  `USER` and `SUDO_*` when constructing this audit identity.
- The concrete target account on events that act on one or more users
  (`target_user` / `target_users`).
- A timestamp, the host's hostname, and the `uxon` version.
- The subcommand (`run`, `attach`, `kill`, …) and a
  list of parsed Uxon option names. Option values, forwarded agent flags,
  prompts, and credentials are never included in `cli.start`.

Per-event extras:

- **Launching a session** → the launch profile, the underlying
  agent (`claude` / `codex` / `cursor`), the absolute project path,
  the worktree branch if any, the session name.
- **Attaching** → the session name, the target user.
- **Killing** → the session name, the target user, whether
  it was forced or a dry-run.
- **Cross-host** → the peer's name and SSH alias, final SSH return code when a
  process ran, cache/scope status for remote listing, a bounded error summary
  on failure, and a UUID `correlation_id` joining related operation and runtime
  events across the boundary.

Full per-event reference: [`reference/audit-events.md`](reference/audit-events.md).

Managed sessions also have an authoritative controller-side launch record. It
contains the session name/id/creation time, launch nonce, launch user, profile,
agent, execution backend, workload runtime/resource identifiers, and record
timestamps. It does not contain prompts, transcript content, credentials, or
the project path. One-controller installs keep records private to that
controller; multi-controller installs may use a group-readable control
directory that launch users cannot access.

## What's not recorded

- The contents of your prompts to the agent.
- The agent's responses.
- The contents of files in your project tree.
- Your shell history.
- Your SSH key, browser cookies, or other personal credentials.
- Your network activity.

`uxon` is a session manager — it sees the **shape** of what you
do, not the **contents**.

## Where it goes

By default, the audit channel writes to the host's `journald`
(systemd hosts) or `/dev/log` syslog (others). In a team setup,
this is **root-owned** — you can append events but cannot edit
the trail. Operators query it via `journalctl`.

In a `team·N` fleet, your operator may forward audit events to
a central collector (Loki, journal-remote, rsyslog upstream,
etc.) so cross-host queries work. Ask your operator if they're
running central forwarding and where it lives.

## Why it exists

For team setups, the audit channel exists for three reasons:

1. **Incident response.** When an agent goes rogue or someone
   makes a mistake, `journalctl SYSLOG_IDENTIFIER=uxon` gives
   the operator a complete sweep of what happened, including
   denied/errored gestures (`outcome != "ok"`).
2. **Cross-host correlation.** Without `correlation_id`,
   chasing an issue that crossed multiple hosts means SSH'ing
   to each and grepping by hand.
3. **Attribution under supervision.** When the team lead can
   attach to your agent's session, the audit trail records
   that attach as the lead's gesture (`process_user=lead`,
   `target_user=nadia-agent`) — not yours. You're never
   credited (or blamed) for the lead's actions.

## Disabling

Operators can disable the channel host-wide via
`audit.enabled = false` in `/etc/uxon/config.toml`. There's no per-user
opt-out; if your team's policy requires audit, the operator
sets it on. If you want a personal `uxon` (solo·1) without the
trail, set `audit.enabled = false` in your own config.

## Retention

Audit retention is whatever the host's journald rotation policy
says (typically a few weeks of `/var/log/journal/` until the
filesystem hits its cap). For teams that ship to a central
collector, retention is whatever the collector's policy is. Ask
your operator.

Launch records are deleted after a verified successful kill. Bounded garbage
collection removes abandoned pending records after 10 minutes and finalized
records after 7 days once their tmux session is no longer live. A protected
per-launch-user cursor stores only the last examined record filename so bounded
collection eventually covers a large shared store without crossing user scope.

## Related

- [`reference/audit-events.md`](reference/audit-events.md) — exact event schema.
- [`explain/audit-channel-design.md`](explain/audit-channel-design.md) — why journald, what `correlation_id` does.
- [`guides/operate/forward-audit-to-collector.md`](guides/operate/forward-audit-to-collector.md) — operator's guide to central forwarding.
- [`SECURITY.md`](../SECURITY.md) — full threat model.
