# Use `uxon doctor`

`uxon doctor` is read-only diagnostics. Always safe to run.
Use it when an in-line TUI hint isn't enough — to script host
inspection, capture a snapshot for a bug report, or audit
several launch users at once.

## Default invocation

```bash
uxon doctor
```

Prints:

- caller user vs. launch user;
- active config paths (repo + project);
- `allowed_roots`, `new_project_root`;
- `repeat_noninteractive_mode` and any env override;
- `tmux` and agent binary paths for the launch user;
- dedicated `tmux` socket details;
- current sessions on the dedicated socket;
- any sessions on the default `tmux` socket that match
  `legacy_session_prefixes`;
- audit-channel state (`audit:    enabled, sink=...`);
- per-profile status for `[[git_remote_profiles]]`;
- a list of detected configuration issues.

Default `uxon doctor` does **zero SSH I/O**.

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

**Verify after onboarding a developer:**

```bash
sudo -niu nadia uxon doctor       # caller=nadia, launch=nadia-agent
```

**Spot-check the fleet from cron:**

```bash
# /etc/cron.daily/uxon-fleet-health (run as the operator):
#!/bin/bash
set -e
uxon doctor --remote --json | \
  jq -e '.data.remote_hosts[] | select(.status != "ok")' \
  && echo "uxon: fleet has unhealthy peers" >&2
```

**Bug-report capture:**

```bash
uxon doctor --json > /tmp/uxon-doctor-$(hostname)-$(date +%s).json
uxon --version > /tmp/uxon-version.txt
journalctl SYSLOG_IDENTIFIER=uxon --since "10 minutes ago" -o json \
  > /tmp/uxon-recent-audit.jsonl
```

## Container diagnostics

When `[container]` is enabled, `uxon doctor` adds a container section
for the current directory's target:

```
container: enabled
- name_template=proj-{project_slug}
- resolved_name=proj-myapp
- is_running=yes  exists=yes
- host agent binaries absent from PATH are EXPECTED (provisioned in the container)
- no container warnings
```

It resolves the container name, runs your `exists_cmd` / `is_running_cmd`
once against that representative target (best-effort — a wedged daemon
shows `?`, never an abort), and surfaces these warnings:

- **`stop_template` unset** — the in-container agent is not reaped on
  kill (it orphans); set `[container].stop_template` so uxon terminates
  it for you.
- **`path_map` doesn't cover the current directory** — launches from
  here may hit a path the container has no mount for.
- **`create_template` definition under a mapped path** — the container
  definition (compose / Dockerfile) resolves to a path inside the
  agent-writable bind mount, so a misbehaving agent could edit it to
  escape the container. Move it to an operator-owned path outside the
  mount.

A missing agent binary on the host PATH is **not** flagged as a fault
under container mode — the agent is provisioned inside the container,
not on the host. The container section is absent entirely when
`[container]` is disabled. Also available under `uxon doctor --json`
as `data.container`.

## What it does *not* do

- Doesn't change state. Read-only by contract.
- Doesn't probe peers by default — use `--remote` explicitly.
- Doesn't check the TUI's Textual-import path (different code
  path, lazy import).

For TUI-specific debugging see
[`enable-debug-logs.md`](enable-debug-logs.md).

## Related

- [`../../reference/cli.md`](../../reference/cli.md#doctor) — every flag.
- [`../../reference/wire-schema.md`](../../reference/wire-schema.md) — `kind = "doctor"` envelope.
- [`diagnose-multi-host.md`](diagnose-multi-host.md) — when `--remote` shows an unhealthy peer.
