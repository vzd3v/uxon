# Recover an aggregator

Peer sessions continue running when the machine displaying the fleet dashboard
fails. Each peer remains usable from its own authorized login.

## Restore the dashboard

1. Install a [compatible Uxon version](../../reference/wire-schema.md#versioning)
   on the replacement Linux machine.
2. Restore the operator-owned `/etc/uxon/config.toml`, including `remote_hosts`.
3. Provision SSH access through your normal access-management process.
4. Run `uxon doctor --remote`, then open `uxon`.

Polling reads live peer state; there is no session cache to restore. Failed
peers contribute no rows until a successful response.

## Preserve removal authority

The peer performs operations under its peer-side controller identity. A
replacement using that same identity can use its surviving protected launch
records. A different controller needs access to the peer's shared record store;
SSH access alone is insufficient.

This applies to direct workloads as well as managed runtimes. See the
[launch-record storage contract](../../reference/configuration.md#launch-records).
Do not delete records while workloads survive or reconstruct authority from
pane environment variables.

## Uxon state to preserve

- Operator configuration: `/etc/uxon/config.toml`.
- Protected records on each controller, or its shared `launch_record_dir`.
- Audit history under the host or collector's retention policy.

Records describe launches in their original trusted host/process context.
Old records from a replaced host do not authorize cleanup of new processes
there. Restore access to surviving controllers and workloads deliberately.

Project files, OS accounts, SSH keys and coding-tool credentials belong to their
respective backup/provisioning systems. Uxon does not back them up or restore
them. For upgrades and required drains, use the [fleet guide](roll-fleet-upgrade.md).
