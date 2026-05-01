# Architecture

Public architectural overview of `ccw`. Read
[`CONTRIBUTING.md`](../CONTRIBUTING.md) first for setup and the
contribution workflow; this document focuses on *what the code looks
like and why*.

## What `ccw` is

`ccw` is a Python script that wraps `tmux` so a host can run several
terminal AI coding agents (`claude`, `codex`, `cursor-agent`)
concurrently for one or more OS users, with predictable session
naming, per-user tmux sockets, optional `git` worktree support,
optional GitHub repo creation on new projects, and an interactive
TUI session picker.

There is no daemon. There is no database. State lives in:
- `tmux` sessions on a per-user dedicated socket;
- `config/config.toml` (host config) and `.ccw.toml` (per-project);
- `${XDG_STATE_HOME:-~/.local/state}/ccw/` (best-effort JSONL event
  log; override with `CCW_LOG_DIR`).

## Top-level layout

```
bin/ccw                      Single-file CLI entrypoint.
lib/
  ccw_settings.py            Settings schema, layered TOML read/write.
  ccw_agents.py              Pure-data agent catalog and probe.
  ccw_git_profiles.py        [[git_remote_profiles]] schema.
  ccw_git_backend_gh.py      `gh repo create` backend.
  ccw_git_backend_token.py   GitHub REST + fine-grained PAT backend.
  ccw_git_create.py          Orchestrator for the new-project git flow.
  ccw_tui/                   Textual TUI (lazy-imported by bin/ccw).
install/
  install_ccw.py             Symlink installer.
  render_ccw_config.py       JSON-to-TOML config renderer.
config/
  config.example.toml        Tracked example. Real config.toml is gitignored.
examples/
  ccw-config.json            Example payload for render_ccw_config.py.
tests/                       unittest.TestCase, run via `pytest -n auto`.
```

## Data flow

```
caller user                                       launch user
─────────────                                    ──────────────
$ ccw run             ──▶  parse args      ──▶  sudo -iu <launch_user> tmux ...
                                                                  │
                                                                  ▼
                                                          fork(claude|codex|cursor)
                            ┌──────────┐                          │
$ ccw                  ──▶  │ TUI loop │──── attach ──────────────┘
(no args, TTY)              │ (textual)│
                            └──────────┘
```

The TUI runs **inside** a re-entrant outer loop. When the user picks
an action, the TUI calls `App.exit()` returning a `LaunchRequest`.
The outer loop in `lib/ccw_tui/app.py::run()` then forks `tmux`
**outside** the textual context (so the agent gets the real terminal),
waits for it, and re-creates the `App` for the next round-trip.

## TUI internals

Sub-modules under `lib/ccw_tui/`:

- `context.py` — pure data: `TuiContext`, `TuiSession`,
  `LaunchRequest`, `Item`, `build_items`, `CallbackError`.
  No `textual` imports, no I/O.
- `state.py` — pure UI state decisions (filter, validation, key
  routing, focus transitions). Tested with plain `unittest`,
  no `Pilot`.
- `events.py` — best-effort JSONL event log.
- `launch.py` — fork-and-wait helper plus the failure-pause banner.
  Runs **outside** the Textual `App` between round-trips.
- `hints.py` — `TEXTUAL_MISSING_HINT` install guidance.
- `app.py` — `CcwApp(App)` and the outer `run(ctx)` re-entrant loop.
- `screens/` — one module per screen: `main`, `confirm`,
  `launch_options`, `new_project`, `git_profile`, `existing`,
  `settings`, `git_remotes`, `agents_unavailable`.
- `widgets/` — `ActionRow` and `SessionTable`. Everything else is
  stock `textual`.
- `styles.tcss` — Textual CSS for the whole app.

## Module boundaries

These are enforced by tests and CI:

- **`bin/ccw` may import from `lib/*`. `lib/*` may not import from
  `bin/ccw`.** The CLI assembles pieces; pieces don't reach back.
- **`lib/ccw_tui/*` may not import `subprocess` or `pwd`** or touch
  the filesystem directly. Side effects flow through callbacks on
  `TuiContext` / `SettingsCallbacks`. This keeps the TUI testable
  with Textual `Pilot` without spawning real processes.
- **`textual` is imported lazily inside `do_interactive`.** Non-TUI
  subcommands (`ccw list`, `ccw doctor`, `ccw version`) must run
  with `textual` absent.
- **All key handling goes through `BINDINGS`.** No `on_key`
  overrides on screen classes; a drift guard test
  (`tests/test_ccw_tui_bindings.py`) refuses any PR that adds one.
- **One launch builder.** `_build_tmux_launch_request` in `bin/ccw`
  is the single place that builds agent command lines. Don't add
  direct `claude` / `codex` / `cursor-agent` exec calls anywhere else.
- **Config writes use `tomlkit`.** The round-trip writer in
  `lib/ccw_settings.py` preserves comments and formatting. CLI read
  paths stay on stdlib `tomllib`.
- **One tmux socket per launch user.** No code path silently falls
  back to the default socket.

## Session naming

```
ccw-<stem>@<agent>           plain sessions
ccw-<repo>-<branch>@<agent>  worktree sessions (claude only)
```

Parallels append `-2`, `-3`, … *after* the agent suffix:
`ccw-myproj@codex-2`. The `ccw-` prefix is hardcoded for new
sessions; legacy `cc-<stem>` / `cc-<stem>-N` sessions (pre-2026-04-21)
are still recognised as read-only `claude` sessions but `ccw` does
not create them.

`parse_session_name` and `candidate_session_name` in `bin/ccw` must
move together. Don't touch one without the other.

## Tests

- `tests/test_ccw*.py` — pure unit tests; the bulk of branchy logic
  lives here.
- `tests/test_ccw_tui*.py` — Textual `Pilot` and `pty` tests; one
  smoke path per feature, batched via
  `tests/harness/textual_scenarios.py::run_screen_scenarios` where
  possible.
- `tests/test_ccw_tui_bindings.py` — drift guards (`BINDINGS`,
  destructive bindings have `show=True`).
- `tests/test_tui_integration.py` — end-to-end pty harness.

Add a new branchy decision? Put it in `lib/ccw_tui/state.py` and
test it with plain `unittest`. Reach for `Pilot` only when the
behaviour depends on Textual lifecycle.

## Configuration

Two layers, merged in order (later wins):

1. **Repo config** — `config/config.toml`, host-wide.
2. **Project config** — nearest `.ccw.toml` in cwd or a parent that
   is itself inside an `allowed_roots` entry. The TUI never writes
   project config.

The single source of truth for known keys is
`lib/ccw_settings.py::SETTINGS_SPECS`. Add a key there in the same
commit as the matching `DEFAULT_CONFIG` / `Config` / `load_config`
changes in `bin/ccw`.

## Security boundaries

See [`SECURITY.md`](../SECURITY.md) for the threat model. The short
version: the operator's `sudoers` config is the authorisation model;
`ccw` enforces `allowed_roots`, the `git_remote_profiles` whitelist,
and atomic config writes; everything inside the launched agent
binary is out of scope.
