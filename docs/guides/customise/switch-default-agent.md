# Switch default agent

`uxon` ships with `claude` as the default agent. To change to
`codex` or `cursor`, or to run several agents side by side:

```toml
[agents]
enabled = ["claude", "codex"]
default = "claude"
```

`agents.default` must be in `agents.enabled`. The TUI's
"New session" picker shows enabled agents only; bare
`uxon run` uses `agents.default`.

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

## Auto-detection

When you install a new agent on the host, the TUI detects it on
the next launch and offers a banner: press `a` to add it to
`agents.enabled` (writes the repo config via `tomlkit`
round-trip), `x` to dismiss. Dismissals are per-user, persisted
under `~/.local/state/uxon/dismissed.json`.

## Reference

- [`../../reference/configuration.md`](../../reference/configuration.md) — `[agents]` table.
- [`../../reference/cli.md`](../../reference/cli.md) — `--agent`, `--auto`, `--dsp`.
- README's [Supported agents](../../../README.md#supported-agents) — install commands per agent.
