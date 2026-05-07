# Multi-host philosophy

When `uxon` runs on more than one host, it polls peer machines
over SSH and surfaces their session lists alongside the local
ones. This page explains the model, what it deliberately is
*not*, and the operational properties you can rely on.

## The model: read-mostly aggregation

Each peer runs its own `uxon` install with its own users, sockets,
allowed-roots, and agents. The local (aggregator) machine only
reads what `uxon list --json` returns over SSH. There is **no**
shared state, **no** cluster coordinator, **no** remote auth
handshake — just `ssh <alias> uxon list --json` parsed locally.

Two destructive operations cross hosts:

- **Per-session `uxon kill --host <alias> [--user <name>] <id>`** —
  routes the kill to the peer over SSH. The peer's own per-target
  sudo gating applies.
- **Per-session `uxon attach --host <alias> --user <name> <id>`** —
  routes the attach to the peer over SSH. Same gate.

**Bulk** destructive ops are strictly local. There is no
`kill-all --host` and there will not be one — reaping every
session on a peer is the operator's deliberate SSH gesture, not
something `uxon` schedules over a fan-out.

## Per-peer authority

Cross-host operation does not delegate trust between peers. Each
peer evaluates its own `sudoers` independently against the SSH
user that lands on it. To revoke a lead's reach on host B you
edit `/etc/sudoers.d/` on host B — touching the central config or
the lead's laptop does not change what host B accepts.

This is the deliberate property that lets a team-N fleet stay
operationally simple: every host remains independently
configured, independently audited, independently authorised.

## SSH config is the source of truth

`uxon` deliberately does not accept `ssh_user`, `port`,
`identity_file`, or `proxy_command` in `[[remote_hosts]]` blocks.
Put those in `~/.ssh/config` for the launch user instead. Auth,
port, ProxyJump, IdentityAgent, ConnectTimeout — all standard
SSH options apply.

`ControlMaster`/`ControlPersist` matter when more than two or
three peers are configured: `ssh_multiplex = "auto"` (default)
adds them to the default fetch template (warm tick: 5–20 ms vs.
cold 200–500 ms). On hosts that prohibit `ControlPersist`
sockets, set `ssh_multiplex = "off"`.

## Fail-soft cache

The last successful payload per peer is cached at:

```
${XDG_STATE_HOME:-~/.local/state}/uxon/remote/<name>.json
```

Atomic write (temp + rename). When a live fetch fails, the
collector falls back to the cache and returns the last-good
sessions with a `from_cache=True` marker so the TUI can show
`(stale)` hints. The disk file is **only** written by a fresh
successful fetch — a failed poll never overwrites the last
good data.

## Per-host circuit breaker

Three consecutive failures open a peer for one interval before
the next probe attempts a half-open. Exponential backoff (factor
2.0) up to 60 s, with ±25 % jitter to spread half-opens across a
recovering fleet. Prevents a 50-host fleet from saturating its
own `ulimit` with 50 simultaneous reconnects when an upstream
network blip ends.

The breaker is per-peer, not fleet-wide. A dead peer does not
slow down healthy ones.

## Wire schema

The collector consumes the same versioned envelope `uxon list
--json` emits locally:

```json
{
  "schema_version": "1",
  "uxon_version": "<peer's version>",
  "kind": "list",
  "data": { ... }
}
```

A `schema_version` mismatch fails the parse loud rather than
silently dropping fields. Peers must run the same major version
(see [`guides/operate/roll-fleet-upgrade.md`](../guides/operate/roll-fleet-upgrade.md)
for the rolling-upgrade procedure).

The full wire-schema reference is in
[`reference/wire-schema.md`](../reference/wire-schema.md).

## Cross-user scope on peers

`--all-users` makes each peer enumerate *its own* reachable
users for the SSH user — same per-target sudo gate as the local
TUI. Two requirements must hold on the peer for cross-user
sessions to come back over the wire:

1. The peer's `config.toml` sets `enable_all_users_list = true`.
2. The SSH user has passwordless sudo (per-target NOPASSWD or
   root NOPASSWD) to the launch users in the peer's
   `session_users`.

If the peer's config has `enable_all_users_list = false`, the
peer exits with code 1 and the stable stderr tag
`uxon-error: all-users-disabled`. The collector detects that tag
and retries once with the legacy `list --json` (own-only)
command, stamping the snapshot with `scope_limited = true`. The
TUI labels that peer `(own only)` — single-host case appends to
the section header, multi-host case appends to the peer's name in
the HOST column. **No silent partial data**: the badge is always
shown when a peer's view is degraded.

## Cross-host audit correlation

For each remote gesture the caller generates a UUIDv4 and passes
it to the peer via an internal `--audit-correlation-id <uuid>`
flag (hidden from `--help`). Both sides emit it under
`correlation_id`, so a single
`journalctl … CORRELATION_ID=<uuid>` query returns the full
pair. Older peers without the flag reject the SSH invocation
outright — silent fallback would lose the correlation property
exactly when an operator is debugging across hosts.

For why this matters and how to make it pay off across the
fleet, see
[`explain/audit-channel-design.md`](audit-channel-design.md) and
[`guides/operate/forward-audit-to-collector.md`](../guides/operate/forward-audit-to-collector.md).

## What this model is not

- **Not a control plane.** `uxon` does not push config, install
  agents, restart services, or rotate keys on peers. Those are
  the operator's tooling (Ansible / Salt / Puppet).
- **Not a metrics pipeline.** The audit channel hits journald;
  shipping it to a collector is operator work.
- **Not a scheduler.** `uxon` does not move sessions between
  hosts, balance load, or fail over.
- **Not a permission service.** Each peer's `sudoers` is the
  authority; `uxon` does not cache credentials, mint tokens, or
  wrap a directory service.

## Related

- [`scenarios/team-n.md`](../scenarios/team-n.md) — the
  scenario hub for team multi-host.
- [`guides/operate/roll-fleet-upgrade.md`](../guides/operate/roll-fleet-upgrade.md) —
  the procedure for keeping versions in lockstep.
- [`guides/operate/survive-aggregator-loss.md`](../guides/operate/survive-aggregator-loss.md) —
  what happens when the aggregator dies.
- [`reference/configuration.md`](../reference/configuration.md) — the `[[remote_hosts]]` schema.
