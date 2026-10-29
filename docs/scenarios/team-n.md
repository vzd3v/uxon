# Team on multiple hosts

Configure each peer as a team host, then aggregate its sessions over the configured transport. Each peer enforces target-user access and protected launch-record authority locally. Bulk removal remains local to one host.

## Tasks

- [Bootstrap each host](../start/team-1-bootstrap.md).
- [Add peers to the dashboard](../start/add-second-host.md).
- [Add a developer](../guides/operate/onboard-developer.md).
- [Remove a developer](../guides/operate/offboard-developer.md).
- [Upgrade the fleet](../guides/operate/roll-fleet-upgrade.md).
- [Recover an aggregator](../guides/operate/survive-aggregator-loss.md).
- [Collect Uxon audit events](../guides/operate/forward-audit-to-collector.md).
- [Diagnose peer access](../guides/debug/diagnose-multi-host.md).

## Reference and background

- [CLI](../reference/cli.md), [configuration](../reference/configuration.md) and [keybindings](../reference/keybindings.md).
- [Isolation model](../explain/isolation-model.md) and [supervision](../explain/supervision-without-impersonation.md).
- [Peer compatibility](../reference/wire-schema.md#versioning) and [multi-host model](../explain/multi-host-philosophy.md).
- [Shared launch records](../reference/configuration.md#launch-records) and [audit events](../reference/audit-events.md).
