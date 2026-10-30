# Read-only supervision

`uxon attach` is currently read-write. uxon does not expose tmux's `attach -r`
mode, so there is no backend-safe read-only attach command to configure.

Do not invoke `tmux` directly against a guessed socket. That bypasses the
selected execution backend, breaks for operator-managed namespaces, and emits
no uxon attach audit event.

For observation without pane input, use the dashboard telemetry and `uxon
list --json`. If interactive read-only supervision is mandatory, keep normal
attach access disabled until uxon provides it as a first-class CLI/TUI mode.

## Current alternatives

- Use `uxon list --all-users --json` for session metadata, active PID, resource
  use, working directory, and runtime state.
- Review the structured audit channel for launch, attach, and kill gestures.
- For an incident that requires intervention, follow
  [`respond-to-rogue-agent.md`](../operate/respond-to-rogue-agent.md).

## Related

- [`../../reference/cli.md`](../../reference/cli.md) — current attach and list
  commands.
- [`../../../SECURITY.md`](../../../SECURITY.md) — supervision threat model.
- [`../../explain/supervision-without-impersonation.md`](../../explain/supervision-without-impersonation.md)
  — the paired-account supervision model.
