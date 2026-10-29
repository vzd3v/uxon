# Architecture

Public architectural overview of `uxon`. Read
[`CONTRIBUTING.md`](../../CONTRIBUTING.md) first for setup and the
contribution workflow; this document focuses on *what the code looks
like and why*.

## What `uxon` is

`uxon` is a Python package that wraps `tmux` so a host can run several
terminal AI coding agents (`claude`, `codex`, `cursor-agent`)
concurrently for one or more OS users, with predictable session
naming, per-user tmux sockets, optional `git` worktree support,
optional GitHub repo creation on new projects, and an interactive
TUI session picker.

There is no daemon. There is no database. State lives in:
- `tmux` sessions on a per-user dedicated socket;
- `/etc/uxon/config.toml` (operator-owned host config);
- controller-local finalized launch records under the caller's XDG state
  directory;
- the host's platform log channel (journald native or `/dev/log`
  syslog), for the `audit` channel;
- `${XDG_STATE_HOME:-~/.local/state}/uxon/`, for the developer-facing
  `debug` and `metrics` channels (override with `UXON_LOG_DIR`).

### Logging channels

Three non-overlapping channels.  Audit is on by default; the other
two are off and operator-opt-in.

| Channel  | Sink                          | Default | Audience            |
|----------|-------------------------------|---------|---------------------|
| `audit`  | journald native / `/dev/log`  | on      | operator / lead     |
| `debug`  | `~/.local/state/uxon/…`       | off     | developer           |
| `metrics`| `~/.local/state/uxon/…`       | off     | developer           |

`audit` is the application-level operational record (who attached,
who killed, who launched, with cross-host correlation).  Per-event
schema and field reference in
[`../reference/audit-events.md`](../reference/audit-events.md); operational topology and
query recipes in [`../guides/operate/forward-audit-to-collector.md`](../guides/operate/forward-audit-to-collector.md).
`debug` is gated on `UXON_DEBUG=<topic>` and writes one JSONL line
per instrumentation point — left in code permanently because it
costs a single set-membership check when the env var is unset.
`metrics` is gated on `UXON_METRICS=1` and writes per-fetch latency
for the remote-collector pollers.

The emitters live in `infra/audit.py` and `infra/events.py` respectively.

## Top-level layout

The package is layered; the layering is enforced by import-linter
in CI (`lint-imports`). Imports follow the direction described under
[module boundaries](#module-boundaries).

```
src/uxon/                     Python package (pipx / uv tool / pip installable).
  __init__.py                 Package version (single source of truth).
  __main__.py                 `python -m uxon` shim → cli.main().
  errors.py                   Shared eprint / fail leaf (no deps).
  domain/                     Pure logic — no subprocess, no I/O. Config schema
                              and defaults, wire-schema envelope, session naming,
                              launch-request shape, host circuit-breaker,
                              [[git_remote_profiles]] schema, sudo/authz rules.
  infra/                     Impure adapters — subprocess / ssh / tmux / fs /
                              journald. Probes, layered TOML read (`config_loader`)
                              and round-trip write (`settings_toml`), audit emit,
                              agent catalog + probe, tmux launch builder, worktree
                              creation, debug/metrics channels, and the `remote/`
                              SSH transport + live peer snapshots.
  app/                        Use-cases wiring domain + infra: list / attach /
                              kill / new / run / doctor, the launch planner
                              (`plan_worktree_launch`), and the `--json` emitters.
  gitremote/                  New-project GitHub repo creation (`gh` + PAT backends).
  cli/                        Argv parse → dispatch → main. The thin entry layer.
  tui/                        Textual TUI (lazy-imported — never pulled in by the
                              non-TUI subcommands `list` / `doctor` / `version`).
install/
  install_uxon.py             Multi-host venv-and-symlink installer.
  render_uxon_config.py       Thin entry point for the package JSON-to-TOML renderer.
config/
  config.example.toml         Tracked example. Real config.toml is gitignored.
examples/
  uxon-config.json            Example payload for render_uxon_config.py.
tests/                        pytest functions and unittest.TestCase classes.
```

## Data flow

```
caller → policy/path resolution → execution backend → tmux server
                                                   └→ launch bootstrap
                                                      └→ workload runtime
                                                         └→ agent
```

The execution backend wraps every command that targets the launch user,
including tmux server creation, list/attach/kill, filesystem and git probes,
credential operations, and workload lifecycle. A command backend is one argv
prefix; `local` is built in. The tmux server itself crosses this boundary, so a
later tmux `new-window` or `run-shell` cannot escape it.

The workload runtime is a separate inner layer. `direct` executes the agent
unchanged; a configured command runtime prepends its `exec_prefix` only to the
final workload argv. The launch bootstrap stays outside that inner runtime.

Managed detached creation uses tmux's one-shot `wait-for` channel for ordering:
uxon creates a pane blocked on a random release channel, reads the live tmux
metadata, atomically finalizes and fsyncs the controller-local launch record,
then signals the channel once. Only then does the bootstrap `exec` the runtime
and agent argv. The channel is nonce-scoped ordering, not an authorization
boundary; another process with access to the same tmux server can signal it.
Ambiguous release failures are not retried: uxon kills the exact new session and
removes its pending record.

The TUI runs **inside** a re-entrant outer loop. When the user picks
an action, the TUI stores a pending `LaunchRequest` and exits the `App`.
The outer loop in `src/uxon/tui/runner.py::run()` hands the request to
`app.session_handoff` **outside** the Textual context, so the interactive
client gets the real terminal. It waits for the client and creates a fresh
`App` for the next round-trip.

## Session identity

Uxon already assigns its own random launch ID: `launch_nonce`, generated from
24 random bytes (192 bits). That single ID follows the launch through its
protected record, workload PID file and peer operations; no second UUID is
needed. A tmux session name is a display label. Its native `$id` locates the
terminal, and creation time provides an additional check.

The launch ID distinguishes a replacement even when tmux reuses a name or
native ID. It does not eliminate the interval between checking a terminal and
acting on it. Uxon therefore checks the captured identity and performs the
action together on tmux's command queue, then verifies removal before workload
cleanup. The protected record supplies cleanup authority and original runtime
context; pane metadata alone cannot replace it.
The same check covers launch handoff, rollback, and session-specific terminal
controls. Closing a reviewed error acknowledges that terminal's current failure;
a later failure after respawn requires a new acknowledgement.

See [launch records](../reference/configuration.md#launch-records) for storage,
shared-controller access and migration requirements.

## Module boundaries

Imports flow from CLI and TUI through application use cases and infrastructure
adapters to pure domain types. The TUI receives callbacks for side effects;
blocking probes run outside its event loop. Textual is loaded only for
interactive use, so listing and diagnostics do not load the UI framework.

One launch builder assembles coding-tool commands from the selected catalog and
profile. Both CLI and TUI worktree flows use the same planner. Configuration is
read from the operator-owned host file; project policy is expressed with path
rules rather than project-owned config files.

See [CONTRIBUTING.md](../../CONTRIBUTING.md) for development checks and
[configuration](../reference/configuration.md) for the public contract.

## Security boundaries

See [`SECURITY.md`](../../SECURITY.md) for the threat model. The short
version: the operator's `sudoers` config is the authorisation model;
`uxon` enforces `allowed_roots`, the `git_remote_profiles` whitelist,
and atomic config writes; everything inside the launched agent
binary is out of scope.
