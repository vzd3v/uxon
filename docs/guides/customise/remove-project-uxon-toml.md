# Move project overrides to operator config

Project-owned `.uxon.toml` files are no longer read. Runtime policy now
lives only in the operator-owned `config/config.toml`.

Use this guide when upgrading a host that used project-local overrides.

## Replace project defaults with path rules

Old project-local default:

```toml
# /srv/projects/nadia/cursor-project/.uxon.toml
[agents]
default = "cursor"
```

New operator-owned rule:

```toml
[launch]
enabled_profiles = ["claude", "cursor"]
default_profile = "claude"

[[launch.path_rules]]
path_prefix = "/srv/projects/nadia/cursor-project"
allowed_profiles = ["cursor"]
default_profile = "cursor"
```

The path rule is matched before launch side effects, including
worktree creation, container readiness, and GitHub repo creation.

## Replace project dashboard tweaks

Move dashboard settings to the host config:

```toml
[tui.table]
columns      = ["name", "agent", "path", "cpu", "ram", "last"]
default_view = "flat"
```

These settings are host-wide. `uxon` does not currently support
per-project dashboard layouts.

## Clean up old files

After migrating the needed settings, delete stale `.uxon.toml` files
from project trees so future readers do not mistake them for active
policy.

```bash
find /srv/projects -name .uxon.toml -print
```

Review each file before deleting it.

## Reference

- [`../../reference/configuration.md`](../../reference/configuration.md) — `[launch]`, `[[launch.path_rules]]`, and `[tui.table]`.
- [`choose-launch-profile.md`](choose-launch-profile.md) — launch-profile examples.
