# Team on a single host

Pair developers with separate launch accounts on a shared Linux host. Supervisors can access workload accounts without a grant to impersonate the developers. Cross-controller session removal also requires a protected shared launch-record store, including for direct workloads.

## Tasks

- [Bootstrap the team host](../start/team-1-bootstrap.md).
- [Add a developer](../guides/operate/onboard-developer.md).
- [Remove a developer](../guides/operate/offboard-developer.md).
- [Set project access](../guides/harden/lay-out-shared-projects.md).
- [Inspect without attaching](../guides/harden/configure-readonly-attach.md).
- [Stop a runaway session](../guides/operate/respond-to-rogue-agent.md).
- [Run a workload in a container](../guides/customise/run-agents-in-a-container.md).
- [Explain audit collection to developers](../privacy.md).

## Reference and background

- [CLI](../reference/cli.md), [configuration](../reference/configuration.md) and [keybindings](../reference/keybindings.md).
- [Isolation model](../explain/isolation-model.md) and [supervision](../explain/supervision-without-impersonation.md).
- [Shared launch records](../reference/configuration.md#launch-records) and [audit events](../reference/audit-events.md).
