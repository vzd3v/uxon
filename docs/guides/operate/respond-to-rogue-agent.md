# Stop a runaway session

Use Uxon to stop the exact session and inspect cleanup. Project recovery,
credential revocation and host incident response belong to your existing
operational procedures.

## Identify and stop the session

Find its host, launch user and full name in the dashboard or
`uxon list --all-users --json`. CPU/RAM are diagnostic signals, not proof of
which process caused damage. On the affected host:

```bash
uxon kill --user nadia-agent uxon-project@claude --json --force
```

For a configured peer:

```bash
uxon kill --host build-host --user nadia-agent uxon-project@claude --json --force
```

In the TUI, select the session, press `d`, then confirm `kill`. Uxon captures
its identity and refuses to act on a replacement with the same name. A peer
applies its own execution and protected-record checks.

If you need terminal output before stopping, attach through Uxon and inspect
scrollback. Attaching is read-write. Do not delay an urgent stop solely to
collect output.

## Check the result

Terminal removal and workload cleanup have separate outcomes. Inspect the exit
status and [structured kill result](../../reference/wire-schema.md#kind--kill).

- An unreachable backend does not prove the session is gone.
- A missing or mismatched protected record prevents automated removal.
- A failed runtime stop requires investigation in the configured runtime.
  Preserve the record while resolving that failure.

Cross-controller removal requires the
[shared launch-record store](../../reference/configuration.md#launch-records),
even for direct workloads. Do not bypass a refusal by guessing a tmux socket
or signaling a pane PID: that PID is not an authoritative workload identity.

## Container path: also stop the container

For a managed runtime, Uxon runs `session.stop_command` after verifying terminal
removal. The supported adapter stops the launch-specific process; it leaves the
container running. See [runtime teardown](../customise/run-agents-in-a-container.md#teardown--reap-the-agent-on-kill).

An opaque PATH wrapper or a runtime without `session.stop_command` cannot
provide that cleanup. Inspect and stop remaining workloads using the runtime
owner's procedure in the same execution boundary. Stopping a shared container
may interrupt unrelated sessions.

Direct workloads may also leave processes that detached from the terminal.
Uxon is not a host-wide process supervisor.

## Review Uxon events

```bash
journalctl SYSLOG_IDENTIFIER=uxon TARGET_USER=nadia-agent --since "1 hour ago"
```

Use the recorded session ID, launch nonce and correlation ID to distinguish
launches and join peer operations. Fields and outcomes are in the
[audit reference](../../reference/audit-events.md).
A [central collector](forward-audit-to-collector.md) can join cross-host history.

Bulk removal is local only. The TUI's reachable-user action affects the
confirmed set on that host; there is no fleet-wide kill operation.
