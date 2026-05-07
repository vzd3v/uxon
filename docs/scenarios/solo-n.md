# Solo on multiple hosts

You're the only user, but agents run on more than one server (one
per project, or production / staging / scratch, or local dev box +
a beefy cloud machine for long runs). Each host runs its own
`uxon` install; one machine — typically your daily driver —
aggregates the others over SSH and shows everything in one TUI.

## What you get

- One sortable session dashboard with a `HOST` column that mounts
  remote rows next to local rows. Locals first, then peers grouped
  by host, with per-host colour glyphs.
- `Enter` on a remote row attaches over SSH; `d` kills a single
  remote session through the peer's own `uxon kill`.
- `uxon list --all-hosts --json` for piping into anything you want.
- Bulk destructive ops (`kill-all`) stay strictly local — fan-out
  reaping is a deliberate SSH gesture, not a `uxon` primitive.

## Get started

1. **Install on every host** — [`start/install.md`](../start/install.md).
   Use the same flavour and version on every box.
2. **Bring up the first host** — same as
   [`scenarios/solo-1.md`](solo-1.md) (a `<user>_agent` paired
   account is recommended even on a solo box, because you'll want
   it on every host eventually).
3. **Add the second host** — [`start/add-second-host.md`](../start/add-second-host.md)
   sets up the SSH config and adds a `[[remote_hosts]]` block on
   the aggregator. Copy that pattern for hosts 3, 4, …
4. **Verify the fleet** — `uxon doctor --remote` probes every peer
   once and reports reachability, latency, and session count.

## Operations you'll eventually need

- **Roll a fleet upgrade.** Peer protocol requires version match —
  [`guides/operate/roll-fleet-upgrade.md`](../guides/operate/roll-fleet-upgrade.md).
- **Survive aggregator loss.** Your laptop dies; the peers keep
  working — [`guides/operate/survive-aggregator-loss.md`](../guides/operate/survive-aggregator-loss.md).
- **Forward audit events to one place.** When chasing a
  `correlation_id` across hosts, central log aggregation pays off —
  [`guides/operate/forward-audit-to-collector.md`](../guides/operate/forward-audit-to-collector.md).
- **Back up project trees.** What lives on each host vs. what's
  recoverable — [`guides/operate/back-up-and-restore.md`](../guides/operate/back-up-and-restore.md).

## Likely customisations

- **Per-host transport overrides** (bastion, kubectl exec, docker
  exec) — [`guides/customise/override-ssh-per-peer.md`](../guides/customise/override-ssh-per-peer.md).
- **Slow links** — [`guides/customise/tune-refresh-cadence.md`](../guides/customise/tune-refresh-cadence.md).

## Reference

- [`reference/cli.md`](../reference/cli.md) — `--host`, `--all-hosts`, `attach --host --user`, `kill --host`.
- [`reference/configuration.md`](../reference/configuration.md) — `[[remote_hosts]]`, `ssh_multiplex`, `fetch_concurrency`.
- [`reference/wire-schema.md`](../reference/wire-schema.md) — what the SSH-piped `uxon list --json` returns.

## Worth understanding

- [`explain/multi-host-philosophy.md`](../explain/multi-host-philosophy.md)
  — read-mostly, per-peer authority, no cluster coordinator.
- [`explain/audit-channel-design.md`](../explain/audit-channel-design.md)
  — `correlation_id` joins caller-side and peer-side audit pairs.
