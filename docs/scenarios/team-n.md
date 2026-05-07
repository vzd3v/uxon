# Team on multiple hosts

Several developers, several Linux hosts. Each host is configured
as [Team on a single host](team-1.md) with its own users, its own
low-priv accounts, its own allowed-roots. One designated host
(typically the operator's workstation) aggregates the rest over
SSH and shows everything in one TUI.

## What you get

- Everything in [`scenarios/team-1.md`](team-1.md), per host.
- Plus: a single dashboard with `HOST` and `USER` columns mounting
  every reachable session on every reachable peer.
- Per-host health badges (`[ok]`, `[cache 12s]`, `[err: …]`,
  `[loading]`) so an operator can tell at a glance whether a
  silent peer is empty or unreachable.
- Cross-host audit correlation: each remote attach / kill pair
  emits `*.remote.out` on the caller and `*.remote.in` on the
  peer, joined by a UUID `correlation_id`. One
  `journalctl … CORRELATION_ID=<uuid>` query (against a central
  collector) returns the full pair.
- **Per-peer authority.** Cross-host operation does not delegate
  trust between peers — each peer's `sudoers` is evaluated
  independently. Revoking a lead's reach on host B is an edit to
  host B's `sudoers`; touching the central config or the lead's
  laptop changes nothing on host B.
- Bulk destructive actions stay strictly local — there is no
  `kill-all --host`. Reaping every agent on a peer is the
  operator's deliberate SSH gesture.

## Get started

1. **Install on every host** — [`start/install.md`](../start/install.md).
   Pin the **same version** on every host (peer protocol requires
   match).
2. **Bootstrap each host as `team·1`** —
   [`start/team-1-bootstrap.md`](../start/team-1-bootstrap.md), per host.
3. **Wire up the aggregator** —
   [`start/add-second-host.md`](../start/add-second-host.md) sets
   up SSH multiplex and adds `[[remote_hosts]]` blocks. Repeat per
   peer.
4. **Verify** — `uxon doctor --remote` probes every peer once and
   reports reachability, latency, and session count.
5. **Centralise the audit channel** —
   [`guides/operate/forward-audit-to-collector.md`](../guides/operate/forward-audit-to-collector.md)
   ships per-host journald to a central collector, which is the
   only way `correlation_id` queries actually pay off across the
   fleet.

## Operations runbooks

- [`guides/operate/onboard-developer.md`](../guides/operate/onboard-developer.md) — including which hosts they get access to.
- [`guides/operate/offboard-developer.md`](../guides/operate/offboard-developer.md) — across the fleet.
- [`guides/operate/respond-to-rogue-agent.md`](../guides/operate/respond-to-rogue-agent.md) — on a remote host.
- [`guides/operate/roll-fleet-upgrade.md`](../guides/operate/roll-fleet-upgrade.md) — rolling-upgrade procedure across N hosts.
- [`guides/operate/survive-aggregator-loss.md`](../guides/operate/survive-aggregator-loss.md) — your laptop dies; what's lost vs. recoverable.
- [`guides/operate/forward-audit-to-collector.md`](../guides/operate/forward-audit-to-collector.md) — central log forwarding.
- [`guides/operate/back-up-and-restore.md`](../guides/operate/back-up-and-restore.md).
- [`guides/operate/rotate-credentials.md`](../guides/operate/rotate-credentials.md).

## Likely customisations

- [`guides/customise/override-ssh-per-peer.md`](../guides/customise/override-ssh-per-peer.md) — bastion / kubectl-exec / docker-exec per peer.
- [`guides/customise/tune-refresh-cadence.md`](../guides/customise/tune-refresh-cadence.md) — per-peer or fleet-wide.

## Reference

- [`reference/cli.md`](../reference/cli.md) — `--host`, `--all-hosts`, `attach --host --user`, `kill --host`, `doctor --remote`.
- [`reference/configuration.md`](../reference/configuration.md) — `[[remote_hosts]]` (with `interval`, `connect_timeout`, `total_timeout`, `extra_ssh_options`, `command_template`), `ssh_multiplex`, `fetch_concurrency`.
- [`reference/wire-schema.md`](../reference/wire-schema.md) — what travels over the SSH wire.
- [`reference/audit-events.md`](../reference/audit-events.md) — `*.remote.out` / `*.remote.in` semantics, `correlation_id`.

## Worth understanding once

- [`explain/multi-host-philosophy.md`](../explain/multi-host-philosophy.md)
  — read-mostly, per-peer authority, no cluster coordinator.
- [`explain/audit-channel-design.md`](../explain/audit-channel-design.md)
  — why `correlation_id` is the headline 3.3.0 feature and what it costs you operationally.
- [`explain/supervision-without-impersonation.md`](../explain/supervision-without-impersonation.md)
  — same property as team·1, valid per-host across the fleet.
