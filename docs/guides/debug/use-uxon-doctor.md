# Use `uxon doctor`

`uxon doctor` inspects configuration and configured execution/runtime probes.
Use it when an in-line TUI hint isn't enough — to script host
inspection, capture a snapshot for a bug report, or audit
several launch users at once.

## Default invocation

```bash
uxon doctor
```

Prints:

- caller user vs. launch user;
- active operator config path;
- `allowed_roots`, `new_project_root`;
- `repeat_noninteractive_mode` and any env override;
- `tmux` and agent binary paths for the launch user;
- enabled launch profiles and their underlying agents;
- execution backend status for each effective launch user;
- dedicated `tmux` socket details;
- current sessions on the dedicated socket;
- any sessions on the default `tmux` socket that match
  `legacy_session_prefixes`;
- audit-channel state (`audit:    enabled, sink=...`);
- per-profile status for `[[git_remote_profiles]]`;
- a list of detected configuration issues.

Default `uxon doctor` does not poll `remote_hosts`; a configured execution
helper can itself use a remote transport.

## Probe the fleet

```bash
uxon doctor --remote
```

Probes every configured `[[remote_hosts]]` peer once and
reports reachability, latency, and session count. Use after a
fleet upgrade, after rolling out config changes, or as a
periodic health-check (cron / Ansible).

## JSON output for scripts

```bash
uxon doctor --json
uxon doctor --remote --json
```

Returns the wire-schema envelope with `kind = "doctor"`. See
[`../../reference/wire-schema.md`](../../reference/wire-schema.md)
for the contract. Suitable for piping into observability
pipelines or assertion-based health checks in CI.

## Common patterns

**Check fleet health in a script:**

```bash
uxon doctor --remote --json | jq -e 'all(.data.remote_hosts[]; .ok == true)'
```

The predicate exits zero when every configured peer is healthy (including an
empty peer list), nonzero otherwise. Inspect the report for failure details.

**Bug-report capture:**

```bash
uxon doctor --json > /tmp/uxon-doctor-$(hostname)-$(date +%s).json
uxon --version > /tmp/uxon-version.txt
journalctl SYSLOG_IDENTIFIER=uxon --since "10 minutes ago" -o json \
  > /tmp/uxon-recent-audit.jsonl
```

## Workload runtime diagnostics

When a launch profile names a command workload runtime, `uxon doctor` adds
runtime details for a representative current-directory target:

```
runtimes:
- launch_profile=claude_work runtime=workbox resource=proj-myapp ready=yes exists=yes
```

It resolves the runtime resource, runs `exists_command` / `ready_command`
once against that representative target (best-effort — a wedged daemon
shows `?`, never an abort), and surfaces these warnings:

- **`stop_command` unset** — the workload agent is not reaped on
  kill (it orphans); set `session.stop_command` on the runtime so
  uxon terminates it for you.
- **`path_map` doesn't cover the current directory** — launches from
  here may hit a path the workload resource has no mapping for.
- **`create_command` definition under a mapped path** — the runtime
  definition (compose / Dockerfile) resolves to a path inside the
  agent-writable mapped tree, so a misbehaving agent could edit it to
  change the workload definition. Move it to an operator-owned path outside the
  mount.

A missing agent binary on the host PATH is **not** flagged as a fault
for a command-runtime launch profile — the agent may be provisioned inside
the workload resource, not on the host. Runtime details are absent when no
enabled launch profile uses a command runtime. Also available under
`uxon doctor --json`.

## What it does *not* do

- Does not launch or kill workloads. Configured probe helpers must themselves
  be suitable for inspection.
- Doesn't probe peers by default — use `--remote` explicitly.
- Doesn't check the TUI's Textual-import path (different code
  path, lazy import).

For TUI-specific debugging see
[`enable-debug-logs.md`](enable-debug-logs.md).

## Related

- [`../../reference/cli.md`](../../reference/cli.md#doctor) — every flag.
- [`../../reference/wire-schema.md`](../../reference/wire-schema.md) — `kind = "doctor"` envelope.
- [`diagnose-multi-host.md`](diagnose-multi-host.md) — when `--remote` shows an unhealthy peer.
