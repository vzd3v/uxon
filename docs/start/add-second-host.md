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

- The same `uxon` version on both hosts (peer protocol requires
  match).
- An aggregator host (typically your laptop or operator
  workstation).
- One or more peer hosts already set up as in
  [`start/solo-1-quickstart.md`](solo-1-quickstart.md) (solo·N)
  or [`start/team-1-bootstrap.md`](team-1-bootstrap.md) (team·N).

## Step 1 — SSH config on the aggregator

The aggregator polls each peer with
`ssh <alias> uxon list --all-users --json`. SSH alias resolution
is the source of truth — `uxon` deliberately does not duplicate
ssh user / port / identity in its own config.

In the aggregator's `~/.ssh/config`:

```
Host vz-prod1
    HostName     10.0.0.42
    User         vasily          # solo·N: your shell user, paired with vasily_agent on the peer
                                 # team·N: an operator-class user with sudo to *_agent on the peer
    IdentityFile ~/.ssh/id_ed25519_uxon
    Port         22
    # Reuse one TCP connection — saves handshake round trips for the periodic poller:
    ControlMaster auto
    ControlPath  ~/.ssh/cm-%r@%h:%p
    ControlPersist 10m
```

`ControlMaster` / `ControlPersist` matter once you have more than
two or three peers — without them every refresh tick reopens a
fresh TCP + auth handshake to every peer. `uxon`'s default
`ssh_multiplex = "auto"` adds matching options to the fetch
command itself, but a stanza in `ssh_config` plays nicely with
both `uxon` and any other tool that hits the host.

For bastion / kubectl-exec / docker-exec patterns see
[`guides/customise/override-ssh-per-peer.md`](../guides/customise/override-ssh-per-peer.md).

## Step 2 — `[[remote_hosts]]` on the aggregator

In the aggregator's `config/config.toml`:

```toml
[[remote_hosts]]
name        = "vz-prod1"
ssh_alias   = "vz-prod1"
description = "primary EU"   # optional, shown in TUI tooltips
remote_uxon = "uxon"         # optional, default "uxon"
```

`name` must be unique across the array, ASCII, and match
`[A-Za-z0-9_.-]+` — it ends up in a cache filename.

Required fields are `name` and `ssh_alias`. Per-peer overrides
(`interval`, `connect_timeout`, `total_timeout`,
`extra_ssh_options`, `command_template`) are optional and
documented in
[`reference/configuration.md`](../reference/configuration.md#remote_hosts-table-array).

## Step 3 — Verify

```bash
uxon doctor --remote
# Probes every configured peer once. Reports reachability,
# latency, and session count. Default `uxon doctor` does NOT
# probe peers — `--remote` is the explicit operator gesture.

uxon list --all-hosts        # local block + one block per peer
uxon list --host vz-prod1    # one peer
uxon                         # TUI: HOST column appears automatically
```

In the TUI, peer rows are grouped under the `HOST` column with
per-host colour glyphs; locals come first, then peers. Per-host
health badges (`[ok]`, `[cache 12s]`, `[err: …]`, `[loading]`)
live in the section header.

## Step 4 (team·N only) — peer-side `enable_all_users_list`

For the aggregator to see the peer's other-user sessions, two
requirements must hold on the peer:

1. The peer's `config.toml` sets `enable_all_users_list = true`.
2. The SSH user landing on the peer has passwordless sudo
   (per-target NOPASSWD or root NOPASSWD) to the launch users in
   the peer's `session_users`.

If (1) is not set, the peer exits with the stable error tag
`uxon-error: all-users-disabled`. The aggregator detects that tag
and falls back to the legacy own-only command, stamping the
snapshot with `scope_limited = true`. The TUI labels that peer
`(own only)` in the HOST column. **No silent partial data.**

## Repeat for the next peer

Add another `[[remote_hosts]]` block and another `~/.ssh/config`
stanza. Refresh the TUI (`r`). New peers don't need an aggregator
restart.

## Operations to read next

- **Roll fleet upgrades** — peer protocol requires version match:
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
