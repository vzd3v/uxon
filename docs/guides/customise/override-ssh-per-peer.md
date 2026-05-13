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

## Per-peer knobs

- **`interval`** — poll cadence override. Duration string
  (`"3s"`, `"500ms"`, `"2m"`) or bare seconds.
- **`connect_timeout`** — `ssh ConnectTimeout` override. Default
  `5s`.
- **`total_timeout`** — hard wall on the whole fetch (connect +
  remote run + parse). Default `15s`.
- **`extra_ssh_options`** — extra `ssh` tokens inserted
  immediately before `{ssh_alias}` in the default template.
  Use for `ProxyJump`, per-peer `IdentityFile`, custom
  `Cipher`, etc.
- **`command_template`** — full argv override. Replaces the
  entire SSH command. Substitutes:
  - `{ssh_alias}`
  - `{remote_uxon}`
  - `{connect_timeout}`
  - `{ssh_control_dir}`
  - `{ssh_control_persist_seconds}`
  - `{remote_command}` (the standard `<remote_uxon> list
    --all-users --json …` string)

  When `command_template` is set, `extra_ssh_options` and
  `ssh_multiplex` are **ignored** — the operator owns the
  transport.

## Fleet-wide knobs

`ssh_multiplex = "off"` strips `ControlMaster` /
`ControlPersist` from the default fetch template. Useful for
environments that prohibit `ControlPersist` sockets entirely
(some hardened distros). Default `"auto"` gives ~5–20 ms
warm-tick SSH cost vs. 200–500 ms cold.

`ssh_control_persist_seconds` (default `300`, must be `> 0`)
sets the `ControlPersist` lifetime when `ssh_multiplex = "auto"`.
Bump it for fleets with sparse polling intervals — the master
stays alive between ticks instead of cold-starting each one. To
disable multiplexing entirely set `ssh_multiplex = "off"` rather
than zeroing this out. Ignored when `ssh_multiplex = "off"` and
per-host when `command_template` is set.

`fetch_concurrency` (default `16`) caps concurrent SSH workers
fleet-wide.

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
