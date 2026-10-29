# Solo on multiple hosts

Use one dashboard to list and attach to sessions on several Linux hosts. Each peer retains its own configuration and authority.

## Tasks

- [Set up the first host](../start/solo-1-quickstart.md).
- [Add a peer](../start/add-second-host.md).
- [Customize peer transport](../guides/customise/override-ssh-per-peer.md).
- [Diagnose peer access](../guides/debug/diagnose-multi-host.md).
- [Upgrade the fleet](../guides/operate/roll-fleet-upgrade.md).
- [Recover an aggregator](../guides/operate/survive-aggregator-loss.md).

## Reference and background

- [CLI](../reference/cli.md), [configuration](../reference/configuration.md) and [keybindings](../reference/keybindings.md).
- [Isolation model](../explain/isolation-model.md) and [supervision](../explain/supervision-without-impersonation.md).
- [Peer compatibility](../reference/wire-schema.md#versioning) and [multi-host model](../explain/multi-host-philosophy.md).
