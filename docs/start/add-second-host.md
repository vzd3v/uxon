# Add a second host

You have one working `uxon` host (solo·1 or team·1). Now you want
a second box visible in the same TUI. This page covers both
flavours: one developer with several boxes (`solo·N`) and a team
with several hosts (`team·N`). The mechanics are the same.

## What you'll learn

- How peers are configured (no shared state, no central
  authority).
- How to set up SSH for the aggregator's poll loop.
- How to add a `[[remote_hosts]]` block and verify it.
- How `enable_all_users_list` interacts with multi-host for team
  setups.

## What you'll need

- [Compatible wire versions](../reference/wire-schema.md#versioning) on both hosts.
- A Linux aggregator host.
- One or more peer hosts already set up as in
  [`start/solo-1-quickstart.md`](solo-1-quickstart.md) (solo·N)
  or [`start/team-1-bootstrap.md`](team-1-bootstrap.md) (team·N).

## Step 1 — SSH config on the aggregator

The aggregator polls each peer with
`ssh <alias> uxon list --all-users --json`. SSH alias resolution
is the source of truth — `uxon` deliberately does not duplicate
ssh user / port / identity in its own config.

In the aggregator's `~/.ssh/config`:

```sshconfig
Host gpu-box
    HostName 10.0.0.42
    User lead
```

Use an existing authorized SSH identity. Uxon configures polling multiplexing
through its [transport settings](../reference/configuration.md#remote_hosts-table-array).

For bastion / kubectl-exec / docker-exec patterns see
[`guides/customise/override-ssh-per-peer.md`](../guides/customise/override-ssh-per-peer.md).

## Step 2 — `[[remote_hosts]]` on the aggregator

In the aggregator's `/etc/uxon/config.toml`:

```toml
[[remote_hosts]]
name        = "gpu-box"
ssh_alias   = "gpu-box"
description = "leased GPU box"   # optional, shown in TUI tooltips
remote_uxon = "uxon"         # optional, default "uxon"
```

Use a unique peer name. Exact fields, name validation and transport overrides
are in the [peer reference](../reference/configuration.md#remote_hosts-table-array).

## Step 3 — Verify

```bash
uxon doctor --remote
# Probes every configured peer once. Reports reachability,
# latency, and session count. Default `uxon doctor` does NOT
# probe peers — `--remote` is the explicit operator gesture.

uxon list --all-hosts        # local block + one block per peer
uxon list --host gpu-box    # one peer
uxon                         # TUI: HOST column appears automatically
```

The dashboard groups peer rows by host. A failed peer has a compact error below
the table; unconfirmed rows stay hidden until a live response succeeds.

## Step 4 (team·N only) — peer-side `enable_all_users_list`

For the aggregator to see the peer's other-user sessions, two
requirements must hold on the peer:

1. The peer's `config.toml` sets `enable_all_users_list = true`.
2. The SSH user landing on the peer has passwordless sudo
   (per-target NOPASSWD for the local execution backend) to the launch users in
   the peer's `session_users`.

If (1) is not set, the peer exits with the stable error tag
`uxon-error: all-users-disabled`. The aggregator detects that tag
and falls back to the own-only command, stamping the
snapshot with `scope_limited = true`. The TUI labels that peer
`(own only)` in the HOST column. **No silent partial data.**

For cross-controller removal, the peer-side SSH controller must also belong to
its protected launch-record control group. Follow the
[team setup](team-1-bootstrap.md#share-protected-launch-records); listing and
attaching do not by themselves prove removal authority.

## Repeat for the next peer

Add another `[[remote_hosts]]` block and another `~/.ssh/config`
stanza. Refresh the TUI (`r`). New peers don't need an aggregator
restart.

## Operations to read next

- **Roll fleet upgrades** — preserve wire compatibility:
  [`guides/operate/roll-fleet-upgrade.md`](../guides/operate/roll-fleet-upgrade.md).
- **Aggregator dies** — what's lost vs. recoverable:
  [`guides/operate/survive-aggregator-loss.md`](../guides/operate/survive-aggregator-loss.md).
- **Forward audit centrally** — `correlation_id` only pays off
  when both sides are queryable from one console:
  [`guides/operate/forward-audit-to-collector.md`](../guides/operate/forward-audit-to-collector.md).

## Worth understanding once

[`explain/multi-host-philosophy.md`](../explain/multi-host-philosophy.md) —
read-mostly aggregation, per-peer authority, no shared state, no
cluster coordinator. Read this if anything about the multi-host
behaviour surprises you.
