# Override SSH per peer

Each `[[remote_hosts]]` entry accepts five optional knobs. Mix
and match per peer.

```toml
# A nearby peer — tighter SSH budget, faster polling.
[[remote_hosts]]
name       = "lab-fast"
ssh_alias  = "lab-fast"
interval         = "3s"
connect_timeout  = "1s"
total_timeout    = "5s"

# A peer reachable through a bastion. extra_ssh_options is
# inserted immediately before {ssh_alias} in the default template.
[[remote_hosts]]
name       = "edge-bastioned"
ssh_alias  = "edge1"
extra_ssh_options = ["-o", "ProxyJump=bastion.example.com"]

# A Kubernetes pod running uxon. command_template replaces the
# entire argv — extra_ssh_options and ssh_multiplex are ignored
# when set, because the operator owns the transport. The
# collector substitutes {remote_command} with the standard
# "<remote_uxon> list ..." string.
[[remote_hosts]]
name       = "k8s-east"
ssh_alias  = "ignored"          # required by schema; unused with command_template
remote_uxon = "/usr/local/bin/uxon"
command_template = [
  "kubectl", "exec", "-n", "ops", "uxon-pod-0", "--",
  "/bin/sh", "-c", "{remote_command}",
]

# A Docker container.
[[remote_hosts]]
name             = "docker-staging"
ssh_alias        = "ignored"
remote_uxon      = "uxon"
command_template = [
  "docker", "exec", "uxon-container", "/bin/sh", "-c", "{remote_command}",
]
```

Exact template placeholders, timeout defaults and multiplexing behavior are in
the [peer configuration reference](../../reference/configuration.md#remote_hosts-table-array).
Custom command templates own their transport and must support the intended
interactive and non-interactive operations.

## When to use each

- **Bastion / ProxyJump** → `extra_ssh_options`. Standard SSH
  pattern; keep credentials in `~/.ssh/config` rather than
  duplicating in `uxon` config.
- **kubectl-exec / docker-exec / nspawn-exec** → `command_template`.
  The "transport" isn't really SSH at all.
- **Per-peer cadence** → `interval`, `connect_timeout`,
  `total_timeout`. Tune for the latency / busyness of each peer.

## Reference

- [`../../reference/configuration.md`](../../reference/configuration.md) — `[[remote_hosts]]` schema with all fields.
- [`../../explain/multi-host-philosophy.md`](../../explain/multi-host-philosophy.md) — why SSH config is the source of truth.
