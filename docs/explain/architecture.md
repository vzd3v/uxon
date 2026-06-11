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
- `config/config.toml` (host config) and `.uxon.toml` (per-project);
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

## Top-level layout

The package is layered; the layering is enforced by import-linter
in CI (`lint-imports`). Each layer may import only from the ones
above it.

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
                              SSH transport + on-disk snapshot cache for peers.
  app/                        Use-cases wiring domain + infra: list / attach /
                              kill / new / run / doctor, the launch planner
                              (`plan_worktree_launch`), and the `--json` emitters.
  gitremote/                  New-project GitHub repo creation (`gh` + PAT backends).
  cli/                        Argv parse → dispatch → main. The thin entry layer.
  tui/                        Textual TUI (lazy-imported — never pulled in by the
                              non-TUI subcommands `list` / `doctor` / `version`).
install/
  install_uxon.py             Multi-host venv-and-symlink installer.
  render_uxon_config.py       JSON-to-TOML config renderer.
config/
  config.example.toml         Tracked example. Real config.toml is gitignored.
examples/
  uxon-config.json            Example payload for render_uxon_config.py.
tests/                        unittest.TestCase, run via `pytest -n auto`.
```

Per-file detail (every module's role, the exact import boundaries)
lives in the agent-facing code map under `docs/agents/`.

## Data flow

```
caller user                                       launch user
─────────────                                    ──────────────
$ uxon run             ──▶  parse args      ──▶  sudo -iu <launch_user> tmux ...
                                                                  │
                                                                  ▼
                                                          fork(claude|codex|cursor)
                            ┌──────────┐                          │
$ uxon                  ──▶  │ TUI loop │──── attach ──────────────┘
(no args, TTY)              │ (textual)│
                            └──────────┘
```

The TUI runs **inside** a re-entrant outer loop. When the user picks
an action, the TUI calls `App.exit()` returning a `LaunchRequest`.
The outer loop in `src/uxon/tui/app.py::run()` then forks `tmux`
**outside** the textual context (so the agent gets the real terminal),
waits for it, and re-creates the `App` for the next round-trip.

## TUI internals

Sub-modules under `src/uxon/tui/`:

- `context.py` — pure data: `TuiContext`, `TuiSession`,
  `LaunchRequest`, `Item`, `build_items`, `CallbackError`.
  No `textual` imports, no I/O.
- `state.py` — pure UI state decisions (filter, validation, key
  routing, focus transitions). Tested with plain `unittest`,
  no `Pilot`.
- `events.py` — `debug` and `metrics` channels (both off by default).
  The audit channel lives in `uxon.audit`, not here.
- `launch.py` — fork-and-wait helper plus the failure-pause banner.
  Runs **outside** the Textual `App` between round-trips.
- `hints.py` — `TEXTUAL_MISSING_HINT` install guidance.
- `app.py` — `UxonApp(App)` and the outer `run(ctx)` re-entrant loop.
- `refresh.py` — pluggable refresh-source registry (`SourceSpec`,
  `SourceResult`, `run_source`). One source per stream — local
  tmux, each `[[remote_hosts]]` peer — runs in its own worker
  group so a slow peer never stalls the others.
- `screens/` — one module per screen and per multi-step flow:
  `main`, `confirm`, `launch_options`, `launch_flow`, `kill_flow`,
  `session_choice` (the attach-vs-new prompt), `worktree_branch`
  (new-worktree name input), `new_project`, `git_profile`,
  `existing`, `settings`, `agents_unavailable`, plus the shared
  `modal_base` chrome.
- `widgets/` — `ActionRow`, `SessionListView` (the unified session
  table — a render-on-demand `ScrollView` on Textual's Line API),
  `HostTabStrip` (per-host tabs above the table in `by_host`
  view), `HostStatusBar` (compact per-host CPU/RAM line under the
  active tab in `by_host`), `FleetStatusBar` (collapsible fleet
  summary below the table in both views — counts + alerts collapsed,
  per-host detail when expanded with `h`), `SearchBar` (summoned
  filter input), and `GatedFooter` (footer that only repaints when
  the binding set changes). Everything else is stock `textual`.
- `dashboard/` — pure layers behind `SessionListView`
  (`row.py`, `columns.py`, `layout.py`, `ui_state.py`, `model.py`,
  `order.py`, `buckets.py`). See § "Session dashboard" below.
- `keymap.py` — `bindings_with_aliases(...)` decorator that
  attaches JCUKEN twins to QWERTY bindings so the keymap survives
  a Russian layout without `xkb`.
- `styles.tcss` — Textual CSS for the whole app.

## Session dashboard

`SessionListView` (one row per visible session — local own,
local other-user under sudo, and one row per session on every
configured peer) is built on pure layers under
[`src/uxon/tui/dashboard/`](../../src/uxon/tui/dashboard/) plus the
widget shell at
[`src/uxon/tui/widgets/session_list_view.py`](../../src/uxon/tui/widgets/session_list_view.py):

1. **`row.py` — `SessionRow`.** A single frozen dataclass is the
   unified row type. Two adapters land the source shapes onto it:
   `from_tui_session(...)` for local rows (own + sudo), and
   `from_wire_record(host, rec)` for one row of a peer
   `RemoteSnapshot`. Equality is value-based — two ticks producing
   identical rows compare equal under `is`-stable identity once
   they go through the model selector.
2. **`columns.py` — `ColumnSpec` registry.** The single source of
   truth for which columns exist, how each one renders, and how
   each one sorts. `REGISTRY` is the column id → spec map;
   formatters return `rich.text.Text` with inline style (no CSS
   class names). `sort_keys` exposes the key function used by the
   model layer. The attach indicator is a glyph in the NAME
   column (`●` filled / `○` hollow); per-host block colour is
   carried by the same `assign_block_colors` map shared with
   `HostTabStrip` and `HostStatusBar`.
3. **`layout.py` — `build_active_columns(flags, cfg)`.** Pure
   selector that picks the active column subset from `REGISTRY`
   based on runtime flags (`multi_host`, `cross_user`) and the
   operator-supplied `[tui.table] columns`. Unknown ids in `cfg`
   are silently dropped — older operator configs survive a column
   removal. `path` and `cmd` are hidden by default and require an
   explicit opt-in via `[tui.table] columns`.
4. **`ui_state.py` — `DashboardUiState`.** Frozen dataclass
   holding `view_mode` (`"by_host" | "flat"`), `filter_text`, and
   `active_tab_index`. `set_view_mode`, `set_filter`, and
   `set_active_tab_index` are pure reducers, wired to `v`, the
   search bar, and the tab strip respectively. An active filter
   forces `flat` until the query is cleared. Sort is **not** UI
   state — it is a fixed contract owned by the model selector
   (locals → cfg-order remotes → within-block by recency).

The selector, bucket layer, and stable-order placer tie those to
the widget:

5. **`model.py` — `select_dashboard_model(...)`.** Identity-stable
   selector: returns the same `(rows, columns, ui)` tuple by `is`
   when nothing changed since the previous call, so a no-op tick
   repaints nothing. The cache lives in `_LAST_OUTPUT`.
6. **`buckets.py` — `select_host_buckets(...)`,
   `select_host_status_block(...)`.** Two pure selectors layered
   on the row tuple. The first groups rows into a `HostBucket`
   per configured host (locals + each `[[remote_hosts]]` peer,
   preserved across empty hosts so the tab strip is stable). The
   second aggregates per-bucket CPU / RAM / uptime / kernel into a
   `HostStatusLine` tuple consumed by `HostStatusBar`. (Load
   average is carried on the `host_stats` envelope but no longer
   rendered — see the fleet-status-bar redesign.) Per-host metrics
   ride the wire envelope as the optional additive `host_stats`
   block; absence renders the bar without metrics.
7. **`order.py` — `place(persisted_order, current_rows)`.** Pure
   stable-order placer that replaced the per-tick re-sort. An
   existing key keeps its established slot, so a telemetry tick
   that only changes recency never moves a row; a new key lands at
   its position within its own host block; a vanished key drops
   out in place. Cold start (no persisted order) yields
   `current_rows` verbatim.

The widget at `widgets/session_list_view.py` is a `ScrollView` on
Textual's Line API: it owns the current row list and paints **only
the visible viewport lines** via `render_line`. `set_rows(new)`
repaints the visible lines; the virtual size (and thus a layout)
changes only when the row *count* changes — a real arrival or
departure. This is the render-on-demand replacement for the old
`DataTable`-backed table, whose every structural mutation forced a
screen-global relayout. Edge-aware navigation (releasing focus at
the list boundary) is reproduced through `BINDINGS` actions only —
no `on_key` override, per the AGENTS.md hard rule.

## Module boundaries

These are enforced by tests and CI (`lint-imports` runs the
import-linter contracts):

- **The layers import only downward: `cli` → `tui` → `app` →
  `gitremote` → `infra` → `domain` → `errors`.** `domain` imports
  nothing from the others; lower layers never reach back up. (The
  layered import-linter contract lists this exact order.)
- **`src/uxon/tui/*` may not import `subprocess` or `pwd`** or touch
  the filesystem directly. Side effects flow through callbacks on
  `TuiContext` / `SettingsCallbacks`. This keeps the TUI testable
  with Textual `Pilot` without spawning real processes.
- **`textual` is imported lazily inside the interactive dispatch.**
  Non-TUI subcommands (`uxon list`, `uxon doctor`, `uxon version`)
  must run with `textual` absent — `python -c "import uxon.cli"`
  must not pull it in.
- **All key handling goes through `BINDINGS`.** No `on_key`
  overrides on screen classes; a drift guard test
  (`tests/test_uxon_tui_bindings.py`) refuses any PR that adds one.
- **One launch builder.** `_build_tmux_launch_request` in
  `src/uxon/infra/tmux.py` is the single place that builds agent
  command lines (it consults `uxon.infra.agents.CATALOG`). Don't
  add direct `claude` / `codex` / `cursor-agent` exec calls
  anywhere else.
- **One worktree-creation site.** Both the CLI `-w` flag and the
  TUI new-worktree path route through `plan_worktree_launch`
  (`src/uxon/app/launch.py`); there is no second `git worktree add`
  call site.
- **Config writes use `tomlkit`.** The round-trip writer in
  `src/uxon/infra/settings_toml.py` preserves comments and
  formatting. CLI read paths stay on stdlib `tomllib`.
- **One tmux socket per launch user.** No code path silently falls
  back to the default socket.

## Session naming

```
uxon-<stem>@<agent>           plain sessions
uxon-<repo>-<branch>@<agent>  worktree sessions
```

Parallels append `-2`, `-3`, … *after* the agent suffix:
`uxon-myproj@codex-2`. The default prefix is `uxon-`, configurable
via `session_prefix`. Names matching any prefix listed in
`legacy_session_prefixes` are recognised by `list` / `attach` /
`kill` (so existing sessions stay reachable across renames) but
are never *created*.

`parse_session_name` and `candidate_session_name` (in the
session-naming module under `src/uxon/domain/`) must move
together. Don't touch one without the other.

## Tests

- `tests/test_uxon*.py` — pure unit tests; the bulk of branchy logic
  lives here.
- `tests/test_uxon_tui*.py` — Textual `Pilot` and `pty` tests; one
  smoke path per feature, batched via
  `tests/harness/textual_scenarios.py::run_screen_scenarios` where
  possible.
- `tests/test_uxon_tui_bindings.py` — drift guards (`BINDINGS`,
  destructive bindings have `show=True`).
- `tests/test_tui_integration.py` — end-to-end pty harness.

Add a new branchy decision? Put it in `src/uxon/tui/state.py` and
test it with plain `unittest`. Reach for `Pilot` only when the
behaviour depends on Textual lifecycle.

## Configuration

Two layers, merged in order (later wins):

1. **Repo config** — `config/config.toml`, host-wide.
2. **Project config** — nearest `.uxon.toml` in cwd or a parent that
   is itself inside an `allowed_roots` entry. The TUI never writes
   project config.

The single source of truth for known keys is
`src/uxon/infra/settings.py::SETTINGS_SPECS`. Add a key there in
the same commit as the matching `DEFAULT_CONFIG` / `Config`
changes in `src/uxon/domain/config.py` and the `load_config`
read path in `src/uxon/infra/config_loader.py`.

## Security boundaries

See [`SECURITY.md`](../../SECURITY.md) for the threat model. The short
version: the operator's `sudoers` config is the authorisation model;
`uxon` enforces `allowed_roots`, the `git_remote_profiles` whitelist,
and atomic config writes; everything inside the launched agent
binary is out of scope.
