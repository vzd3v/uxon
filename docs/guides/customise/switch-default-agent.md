# Switch default agent

Out of the box `uxon` runs in **auto-mode** (`agents.enabled`
empty or absent) and picks the first installed CATALOG agent for
bare `uxon run`. To pin a specific default, or to restrict to an
approved subset of agents, declare it explicitly:

```toml
[agents]
enabled = ["claude", "codex"]
default = "claude"
```

`agents.default` is optional in either mode. In strict-whitelist
mode (non-empty `agents.enabled`) it must be listed in
`enabled`; if unset uxon falls back to `agents.enabled[0]`. In
auto-mode it falls back to the first installed agent. The TUI's
"New session" picker shows the launchable agents only.

## Per-invocation override

```bash
uxon run --agent codex
uxon new myproj --agent cursor
```

`--agent` overrides the config default for that one
invocation.

## Per-agent default flags

```toml
[agents.claude]
default_args = ["--model", "claude-sonnet-4-6"]

[agents.codex]
default_args = ["--reasoning-effort", "high"]
```

These flags are prepended to every agent invocation. CLI flags
passed through `uxon run -- ...` are appended after these, so
explicit invocations override config defaults.

## Permission-mode flags

`--auto` and `--dsp` (yolo) are universal `uxon` flags that
translate to per-agent equivalents:

| Agent | `--auto` | `--dsp` |
|---|---|---|
| `claude` | `--permission-mode auto` | `--dangerously-skip-permissions` |
| `codex` | `--full-auto` | `--dangerously-bypass-approvals-and-sandbox` |
| `cursor` | (not supported, error) | `--yolo` |

`--auto` and `--dsp` are mutually exclusive.

## Worktree mode

`-w <branch>` (worktree) is currently **claude-only**. Using
`-w` with `codex` or `cursor` is an error.

## Auto-mode and host probe

With `agents.enabled` empty or absent (the default), uxon runs in
**auto-mode**: it probes the host once on launch and treats every
installed CATALOG agent (`claude`, `codex`, `cursor`) as
launchable. Install a new agent (`npm i -g …`) and re-probe with
the existing `r` refresh binding on the main screen — no banner,
no per-user state file, no opt-in.

Set `agents.enabled` to a non-empty list to flip into **strict
whitelist** mode — only the listed agents are launchable even if
more are installed on the host. Use this to pin a fleet to an
approved subset.

Whichever agent ends up selected (`--agent`, `agents.default`,
`agents.enabled[0]`, or the auto-mode probe) is verified against
the probe before launch. A missing binary fails with a uxon-level
error and install hint (exit code `1`) instead of an opaque tmux
exec failure. Probe failures (sudo missing, NOPASSWD
misconfigured, etc.) surface in the TUI via the "agents
unavailable" modal with the error and install hints, rather than
leaving the agent list silently empty.

## Reference

- [`../../reference/configuration.md`](../../reference/configuration.md) — `[agents]` table.
- [`../../reference/cli.md`](../../reference/cli.md) — `--agent`, `--auto`, `--dsp`.
- README's [Supported agents](../../../README.md#supported-agents) — install commands per agent.
