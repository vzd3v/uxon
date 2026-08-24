# Choose launch profiles

You want `uxon` to offer a specific set of runnable choices, pick a
default, or route one project tree to a different runtime.

Launch profiles are the runnable choices. Each profile selects an
agent from the catalog and can optionally pin a launch user, a
workload runtime, and the GitHub repo-creation profiles it may use.

## Pick a default profile

```toml
[launch]
enabled_profiles = ["claude_work", "codex_safe"]
default_profile = "claude_work"

[launch.profiles.claude_work]
agent = "claude"
display_name = "Claude work"

[launch.profiles.codex_safe]
agent = "codex"
display_name = "Codex safe"
```

`enabled_profiles` is an ordered whitelist. `default_profile` must be
listed there. The TUI shows enabled launch profiles; non-interactive
launches use the default unless `--profile <id>` is passed.

## Override one launch

```bash
uxon run --profile codex_safe
uxon new myproj --profile claude_work
```

`--profile` selects the launch profile for that invocation. The old
`--agent` selector is removed; passing it fails with a migration hint.

## Route a path to a profile

```toml
[[launch.path_rules]]
path_prefix = "/srv/projects/billing-api"
allowed_profiles = ["codex_safe"]
default_profile = "codex_safe"
```

Path rules match by path component and the longest matching
`path_prefix` wins. They can narrow which launch profiles and
git-remote profiles are allowed for a project tree.

## Agent defaults and modes

```toml
[agents.claude]
default_args = ["--model", "claude-sonnet-4-6"]

[agents.codex]
default_args = ["--reasoning-effort", "high"]
```

These flags are prepended whenever a launch profile selects that
agent. CLI flags passed through `uxon run -- ...` are appended after
these, so explicit invocations can override config defaults.

`--mode <id>` selects a permission mode from the selected profile's
underlying agent catalog (for example `--mode auto` or `--mode yolo`).
Omitting it picks the agent's first mode.

## Auto-mode

With `launch.enabled_profiles` empty or absent, `uxon` uses the
shipped built-in profiles `claude`, `codex`, and `cursor`, each mapped
to the same-named agent. On host-only launches it probes the launch
user's PATH and picks the first built-in profile whose agent is
installed. Command-runtime profiles are explicit config entries and must
be enabled by the operator.

## Reference

- [`../../reference/configuration.md`](../../reference/configuration.md) — `[launch]`, `[launch.profiles.<id>]`, path rules, and `[agents.<id>]`.
- [`../../reference/cli.md`](../../reference/cli.md) — `--profile`, `--mode`, and session naming.
- README's [Supported agents](../../../README.md#supported-agents) — install commands per agent.
