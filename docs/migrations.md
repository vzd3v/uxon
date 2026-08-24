# Migrate to Uxon 4.0

Uxon 4.0 is intentionally breaking. Upgrade every controller and peer in one
rollout; mixed wire majors are unsupported.

## Install the host-wide config

Uxon reads only `/etc/uxon/config.toml`. There is no checkout, XDG,
environment-variable, or project fallback.

```bash
sudo install -d -o root -g root -m 0755 /etc/uxon
sudo install -o root -g root -m 0644 config/config.example.toml \
  /etc/uxon/config.toml
```

Move any operator-owned settings from the previous checkout config into this
file. Move project `.uxon.toml` policy into `[[launch.path_rules]]`, then remove
those project files so they cannot be mistaken for active policy.

## Replace agent selection with launch profiles

The user-facing selector is `--profile`; `--agent` is now an ordinary argument
forwarded to the selected agent.

```toml
[launch]
enabled_profiles = ["claude_work", "codex_safe"]
default_profile = "claude_work"

[launch.profiles.claude_work]
agent = "claude"
launch_user = "team-agent"
runtime = "direct"
```

Replace removed `agents.enabled`, `agents.default`, per-project agent defaults,
and top-level git-remote defaults with launch-profile policy. Unknown fields are
rejected at every config level.

## Replace container configuration with workload runtimes

Container engines are one implementation of a generic workload runtime.

| 3.x concept | 4.0 concept |
|---|---|
| `[container]` / container profile | `[runtimes.<id>]` |
| `container_profile` | `launch.profiles.<id>.runtime` |
| `name_template` | `resource_name_template` |
| `exec_template` | `exec_prefix` |
| `runtime_namespace` | `resource_scope` |
| `probe/start/create` | `readiness.ready_command/start_command/create_command` |
| `stop_template` | `session.stop_command` plus mandatory `identity.resolve_command` |

The old keys are not translated. They fail as unknown fields.

## Configure execution backends explicitly

The built-in `local` backend preserves argv and uses
`sudo -H -u USER --` for interactive cross-user work and
`sudo -n -H -u USER --` for non-interactive probes.

A command backend supplies one argv template containing exactly one `{user}`.
Use a fixed, root-owned helper which enters the intended execution boundary,
drops to the target user, preserves argv, and returns the fixed UID/GID/group
probe unchanged.

```toml
[execution]
default_backend = "boundary"

[execution.backends.boundary]
kind = "command"
command_prefix = ["/usr/local/libexec/uxon-exec", "{user}", "--"]
probe_timeout_seconds = 3.0
```

Drain sessions before changing a command helper, backend mapping, or socket
template. Uxon records the static backend fingerprint for diagnosis but cannot
prove lifecycle continuity across an operator-managed boundary.

## Provision launch records for multi-controller hosts

The default launch-record store is controller-private. If several trusted
controller accounts supervise the same sessions, provision one root-owned
setgid directory, exclude launch users from its group, and set
`launch_record_dir` explicitly.

```bash
sudo install -d -o root -g uxon-control -m 2770 /var/lib/uxon/launch-records
```

```toml
launch_record_dir = "/var/lib/uxon/launch-records"
```

## Roll out

1. Drain active sessions with the old installation.
2. Install Uxon 4.x and `/etc/uxon/config.toml` on every host.
3. Run `uxon doctor --json` locally on each host.
4. Upgrade all peers before re-enabling multi-host operations; wire schema 3
   rejects a different major.
5. Start one session per execution backend/runtime combination, verify list,
   attach, kill, launch-record cleanup, and audit events.

For exact keys see [configuration](reference/configuration.md); for operational
commands see the [CLI reference](reference/cli.md).
