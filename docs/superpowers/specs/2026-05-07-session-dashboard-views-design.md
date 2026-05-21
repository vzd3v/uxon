# Session dashboard — views, search, colors, keymap

## Goal

Make the unified session dashboard usable on a multi-host operator's
screen, predictable, and free from "perceived mixing" / colour
collisions. Concretely:

1. **Order is a contract, not a setting.** Locals first; remote hosts
   in `cfg.remote_hosts` order; within each block by last-attach
   recency, newest first.
2. **Two views.** Default **by-host** with a host tab strip and a
   per-host status bar. **Flat** view as a toggle, with a per-host
   status block above the table.
3. **Search-first.** Search input is the default focus on mount; `/`
   refocuses from anywhere; non-empty filter forces flat render.
4. **Zebra by host blocks**, weak shade alternation within a block;
   colours separate from danger/warning semantics; per-host colour
   pinning via config.
5. **Attached state is orthogonal to colour.** `●` filled glyph for
   attached, `○` hollow for not — no `bold green` override that
   collides with the local-block colour.
6. **Bindings work in any keyboard layout.** Central
   `LAYOUT_ALIASES` map auto-doubles every binding; quit on `q`
   only, Esc is scoped "cancel one level".
7. **One known reconciler bug fixed** so tab switches and large diffs
   don't reorder rows.

## Non-goals

- Width-aware auto-hide of columns. Out — adds a resize feedback
  loop; revisit later.
- New per-column TOML knobs (sort direction per column, column-width
  pinning). Out — the curated cycle is removed, not extended.
- Per-host tab persistence across runs. Active tab resets to first
  on every TUI restart.
- Mouse support for tabs / search. Keyboard-first; mouse comes for
  free where Textual already routes clicks.
- Detection of operator's keyboard layout. Static `LAYOUT_ALIASES`,
  no runtime locale gating.
- Config schema cleanup pass (e.g. moving `ssh_*` under `[ssh]`).
  Flagged as separate work in `docs/agents/conventions.md`; not
  done here.

## Background — what's wrong today (3.3.0)

| Observation | Root cause |
| --- | --- |
| "Sessions on different hosts are mixed" | `SessionDashboardTable._apply_add` appends to the end when `before_key` is not yet in the table. Reconciler walks `new` forward, so a moved row's `before_key` is the row after it which is added later in the op stream → the insert falls back to append. Reproducible on tab switches and large diffs. |
| "Order inside a host doesn't make sense" | `_dashboard_ui.sort_by="cpu"` is the within-block key (not last-attach). |
| "Useless sort buttons" | Footer-visible `s` (cycle cpu→ram→last→name) and `S` (toggle asc/desc). Operators trip them by accident. |
| "Red is back" | `_HOST_PALETTE` excludes red (since `f2b8bb7`); the **CPU `bold red`** at >50% CPU is a danger signal on the cell. Operators conflate it with row colour. |
| "Magenta is ugly / `kris` looks red" | Magenta in the per-host palette renders as warm pink/red on many terminals. Drop it. |
| "USER column missing" | `default_visible=False`, `show_when="cross_user"`. Correct in single-user mode; surfaces automatically when other-user sessions appear. No change needed. |
| "PATH eats horizontal space" | `default_visible=True`. Flip default to off; operators opt back in via `tui.table.columns`. |
| "Bindings don't work on RU layout" | No layout aliasing; operator switches keyboards. |
| "Esc closes the app by accident" | `Esc` is bound to `quit` on `MainScreen`. |
| "Attached remote session looks like a local one" | `_format_name` overrides the NAME text to `bold green` on attach. Collides with `_LOCAL_STYLE = green`. Attach signal needs to be orthogonal to host colour. |

## Architecture

### Single source of truth

`select_dashboard_model(state, cfg, ui) -> tuple[SessionRow, ...]`
remains the one selector both views consume. Two cheap derived
selectors layer on top:

* `select_host_buckets(rows, cfg, state) -> tuple[HostBucket, ...]`
  for by-host. Order = `host_priority` (locals first, then
  `cfg.remote_hosts` order). Empty hosts produce empty buckets so
  configured peers are always visible as tabs.
* `select_host_status_block(rows, state, host_stats_local) ->
  tuple[HostStatusLine, ...]` for the flat-view block list and the
  by-host single-line render — same data, two layouts.

No parallel models, no second source of truth.

### Sort contract

```python
def _within_block_key(row):
    last = row.last_attached_epoch if row.last_attached_epoch is not None else float("-inf")
    return (-last, row.short or row.name)


def _build(state, cfg, ui):
    rows = locals_from(state) + remotes_in_cfg_order_from(state, cfg)
    needle = ui.filter_text.strip().lower()
    if needle:
        rows = [r for r in rows if _matches_filter(r, needle, ui.search_fields)]
    rows.sort(key=_within_block_key)            # within-block recency
    rows.sort(key=_host_priority(cfg))          # stable: locals → cfg-order remotes
    return tuple(rows)
```

`DashboardUiState.sort_by` and `sort_dir` are removed. The selector
does not consult them. `tui.table.default_sort_by` is removed from
`SETTINGS_SPECS`; reading it from existing TOML emits one
`UXON_DEBUG=tui` log line and is otherwise ignored.

### Reconciler apply fix

`SessionDashboardTable.apply` sorts the incoming op tuple before
applying:

```python
def apply(self, ops):
    if not ops:
        return
    removes_and_updates = [op for op in ops if not isinstance(op, RowAdd)]
    adds = sorted(
        (op for op in ops if isinstance(op, RowAdd)),
        key=lambda op: -self._new_index_of(op.row_key),
    )
    for op in removes_and_updates:
        self._dispatch(op)
    for op in adds:
        self._dispatch(op)
```

Adds run in **reverse new-index order**, so for every `RowAdd` the
`before_key` either is `None` (the very last new row, append) or
points at a row that was added earlier in this same reverse walk —
i.e. already in the table. The "anchor not present → append"
fallback in `_apply_add` becomes dead code (kept as a defensive
log; never hit in production).

`new_index_of(row_key)` requires the apply path to know the new
row order. Rather than recomputing, the reconciler emits a paired
`apply_plan` carrying the new key list:

```python
@dataclass(frozen=True, slots=True)
class ApplyPlan:
    ops: tuple[Op, ...]
    new_keys: tuple[str, ...]
```

`SessionDashboardTable.apply(plan)` reads `plan.new_keys` for
ordering. This keeps the diff function pure (no widget contact)
and gives the apply path the order it needs without re-walking.

Regression test: build a 4-row reverse permutation
(`[A,B,C,D] → [D,C,B,A]`) and assert the final visual order
matches `new`. Today's code fails this.

### View modes

`DashboardUiState`:

```python
@dataclass(frozen=True, slots=True)
class DashboardUiState:
    view_mode: Literal["by_host", "flat"] = "by_host"
    filter_text: str = ""
    # search_fields is *not* in ui state — it's a static cfg
    # array consulted by the selector.
```

Reducers:

* `set_view_mode(ui, mode)` — identity-stable on no-op.
* `set_filter(ui, text)` — already exists; identity-stable on
  no-op.

`MainScreen` reads `view_mode` once per compose. **When
`filter_text != ""` the render path is forced to `flat` regardless
of `view_mode`** — `view_mode` is not mutated; only the renderer
branch is overridden. Clearing the filter returns the operator to
their chosen `view_mode` on the same active host tab (state
preserved).

Config: `tui.table.default_view = "by_host" | "flat"`. Default
`"by_host"`. Validated at load against the literal set; unknown
values fail config load with a clear error.

### Host tab strip

New widget `HostTabStrip` in `tui/widgets/host_tab_strip.py`.
Single-line `Horizontal` of one `Static` per bucket; layout wraps
to a second row when the strip would exceed the available width
(via splitting buckets across N `Horizontal` containers in
`compose`; Textual's current `Container` model handles flow
naturally with `width: auto`). Active tab gets `-active` class;
inactive tabs are dim.

Tab visual:

* Label = `bucket.label` — `RemoteHost.name` for remotes (already
  validated as filename-safe in `remote_hosts.py`), `local` for the
  locals bucket.
* Text colour = `block_color(bucket.host_name)` — same source as
  the rows in flat view, so a host's hue is consistent across
  views.
* Active tab: `bold` + `background: $accent 30%`.
* Inactive tab: `text-style: dim` of the same colour.
* No digit prefixes (1-9 are owned by ActionRow).

Reactive `active_index: int`. Posts a `HostTabActivated(index)`
message on change. Owned-by-screen state, **not** in
`DashboardUiState` (resets on recompose intentionally).

### Host status bar

New widget `HostStatusBar` in `tui/widgets/host_status_bar.py`.
Universal — one widget, two compose paths:

* **Compact single** — used in `by_host` view, mounted **under the
  active tab**. Renders a single `HostStatusLine` for the active
  bucket.
* **Expanded list** — used in `flat` view, mounted **above the
  table**. Renders one `HostStatusLine` per bucket (locals + every
  configured remote) in `host_priority` order.

`HostStatusLine` field shape:

```
<host>  <count> sess · <attached> attached · cpu <Σ%> · mem <used>/<total> · la <loadavg_1m> · up <uptime> · <state>
```

Where:
- `cpu Σ%` = sum of session `cpu_pct` (load attributed to our
  sessions, distinct from absolute host CPU which we don't claim
  to know without `host_stats`);
- `mem used/total` = from `host_stats.mem_used_kib /
  mem_total_kib`;
- `la` = `host_stats.loadavg_1m`;
- `up` = relative-formatted `host_stats.uptime_s`;
- `<state>` only when not normal: `(cached)` dim if
  `RemoteSnapshot.from_cache=True`, `pending…` dim if no
  snapshot yet, `unreachable` in `$uxon-danger` if breaker open
  with no cache.

Per-host name token in the status line is coloured with the same
`block_color` as its tab and rows.

### Search

New widget `SearchBar` in `tui/widgets/search_bar.py`. Always
visible between `── sessions ──` and the dashboard. Input field
with placeholder `/ to search` (dim) and a counter line
`<N> matches` (dim, hidden when filter empty).

**Default focus on `MainScreen` mount = SearchBar.** Operators with
many sessions can type immediately. Down arrow hops into the
dashboard; Up arrow into the last ActionRow.

`/` from anywhere on the screen focuses the SearchBar. Bound at
the screen level with `priority=True`.

Esc behaviour (search-local, `priority=True`):

| Focus | `filter_text` | Action |
| --- | --- | --- |
| SearchBar | non-empty | clear text; keep focus |
| SearchBar | empty | blur (focus → dashboard) |
| anywhere else | n/a | screen-level Esc — **no-op on MainScreen** (modals still close on Esc as they do today) |

Quit is **only** `q` (with `й` alias via `LAYOUT_ALIASES`). The
`Esc → quit` binding on `MainScreen` is removed. Modals keep their
current `Esc → close` semantics.

Search fields config: `tui.search.fields` (array of strings).
Default `["name", "user"]`. Allowed values: `name`, `user`, `host`,
`path`, `cmd`. Unknown values rejected at config load with a
specific error citing the offending entry.

`_matches_filter` becomes a pure helper that consumes the configured
field list:

```python
def _matches_filter(row, needle, fields):
    for f in fields:
        if f == "name" and needle in (row.short or row.name).lower():
            return True
        if f == "user" and needle in row.user.lower():
            return True
        if f == "host" and needle in (row.host or "local").lower():
            return True
        if f == "path" and needle in row.path.lower():
            return True
        if f == "cmd" and needle in row.cmd.lower():
            return True
    return False
```

### Block colours

`dashboard/columns.host_colour(host_name)` (md5-hashed) **deleted**.
Replaced by:

```python
def assign_block_colors(
    remote_hosts: tuple[RemoteHost, ...],
    *,
    local_color: str,
    palette: tuple[str, ...],
) -> dict[str | None, str]:
    """Map host_name → colour. None key = locals."""
```

Algorithm:

```
out[None] = local_color
prev = local_color
cycle_idx = 0
for host in remote_hosts:
    if host.color is not None:
        color = host.color           # operator pin; no validation against prev
    else:
        # auto-cycle with adjacency-skip
        color = palette[cycle_idx % len(palette)]
        cycle_idx += 1
        if color == prev:
            color = palette[cycle_idx % len(palette)]
            cycle_idx += 1
    out[host.name] = color
    prev = color
return out
```

`local_color` and per-host pinned `color` are **not** restricted to
the palette. Operators can pin any colour name Rich accepts; if a
remote pins `green` and `[local_host] color = "green"`, the visual
collision is the operator's choice — no validation rejects it.

Configuration:

```toml
[local_host]
color = "green"                # default

# tui.color_palette: for the auto-cycle (remotes without `color`)
[tui]
color_palette = ["cyan", "blue"]   # default; no magenta, no red, no yellow
```

(`[tui]` already exists — `tui.table.columns` etc.)

```toml
[[remote_hosts]]
name = "kris"
ssh_alias = "26s_kris"
color = "blue"                 # optional pin
```

Block hue and within-block alternation are applied **at the widget
render layer**, not inside `format(row)`. Rationale: `ColumnSpec.format`
is `Callable[[SessionRow], Any]` and is called from inside
`reconcile.diff()` (`reconcile.py:106`, `:170-171`) to detect cell
changes — widening it to `format(row, block_color, row_in_block)`
would either break the diff signature or force the diff to know
each row's old + new positional metadata, which it cannot derive
from `tuple[SessionRow, ...]` alone. Keeping `format(row)` pure
also preserves the *Single source of truth* contract: selector pure
→ reconciler pure → widget owns presentation.

Concretely:

* `format(row) -> Text | str` keeps its current signature. It emits
  *content + cell-local styling only*: the `●`/`○` glyph (attached
  marker), CPU danger colour, sudo highlight. No block hue, no
  zebra dim.
* The widget keeps a parallel `dict[str, tuple[str, int]]` —
  `row_key → (block_color, row_in_block)` — recomputed from the
  selector output once per tick and held alongside the
  `ApplyPlan`. When dispatching a `RowAdd` or `CellUpdate` for the
  NAME and HOST columns, the widget wraps the cell `Text` in a
  shallow `Text` carrying the block style (and `+ dim` on odd
  `row_in_block`) before calling `add_row` / `update_cell`.
* The wrap is idempotent: re-wrapping the same cell `Text` produces
  the same final cell, so position-only changes (zebra parity flip)
  emit a `CellUpdate` only when the diff already would (because the
  underlying `format(row)` output differs) or via an explicit
  parity-refresh pass after reconciliation. Position-only parity
  refresh is a separate widget-side step keyed off `new_keys` and
  the parallel position dict — it does not flow through the
  reconciler.

### Attached marker

`_format_name` keeps signature `format(row) -> Text` but emits the
`●`/`○` glyph plus the row's name without any host-block hue:

```python
def _format_name(row):
    glyph = "● " if row.attached else "○ "
    text = Text(glyph)
    text.append(row.short or row.name or "-")
    return text
```

No `bold green` override. Attach is communicated by glyph shape
only, working in any colour and on monochrome terminals. Per-host
hue is layered by the widget at render time (see *Block colours*
above); `_format_host` likewise emits plain text — the widget paints
the host-name token with the block colour during the wrap step.

CPU red and the `bold green` removal are **independent**:

- `format_cpu` keeps `Text(raw, style="bold red")` when
  `cpu_pct > 50` and `Text(raw, style="yellow")` when
  `cpu_pct > 10`. **Cell-only**, not row.
- Same principle reserved for any future RAM threshold.

### Keymap and bindings

New module `src/uxon/tui/keymap.py`:

```python
LAYOUT_ALIASES: dict[str, str] = {
    # JCUKEN ↔ QWERTY shared positions.
    "[": "х", "]": "ъ",
    "d": "в", "r": "к", "v": "м", "q": "й",
    "a": "ф", "x": "ч", "s": "ы",
    "/": ".",
    # Letters not yet bound but reserved for safe extension. Adding
    # a binding for any new physical key just adds one entry here.
}


def bindings_with_aliases(*specs: Binding) -> list[Binding]:
    """Each spec, plus a hidden RU twin where one exists in
    LAYOUT_ALIASES. Specs whose key is not in the map pass through
    untouched. No errors, no whitelist."""
```

All screens declare their `BINDINGS` through this helper. New
bindings inherit a twin automatically when the physical key is in
the map; otherwise they ship without a twin (no test failure, no
warning). The map grows when a new alias is needed.

Forward-compat: when bindings move to TOML in a future pass, the
same helper still applies — only the source of binding tuples
changes.

`MainScreen.BINDINGS` final:

| Key | Action | Footer |
| --- | --- | --- |
| `q` | quit | yes |
| `r` | refresh | yes |
| `d` | kill | yes |
| `D` | kill-all (mine) | yes |
| `v` | toggle view | yes |
| `[` / `]` | prev / next host tab | yes / yes |
| `/` | focus search | yes |
| `1`–`9` | digit jump | 1 yes, rest no |
| `Up` / `Down` | cross-widget focus | no |

Removed: `Esc → quit`, `s → cycle_sort`, `S → toggle_sort_dir`.

### SSH ControlPersist

`_default_template` in `remote_collector.py` substitutes:

```
"-o", f"ControlPersist={persist_seconds}s",
```

`persist_seconds` is read from the new top-level config key
`ssh_control_persist_seconds`. Default `300`. Validation: must be
a positive integer (`> 0`). Zero/negative rejected at config load.

Disabling multiplex remains the responsibility of
`ssh_multiplex = "off"`. No magic-zero semantics on the persist
key.

`SETTINGS_SPECS` gains both keys (the existing `ssh_multiplex`
default is unregistered today — we register it as part of this
work):

```python
SettingSpec("ssh_multiplex", "enum", "...", choices=("auto", "off")),
SettingSpec("ssh_control_persist_seconds", "number", "ControlPersist for the multiplexed SSH master, seconds. Must be > 0; disable multiplexing via ssh_multiplex=off."),
```

### Wire schema and host stats

`WIRE_SCHEMA_VERSION` stays at `"1"`. `host_stats` is an **additive
optional** envelope field — same pattern as `data.scope_skipped`
already documented in `wire_schema.py` ("forward-compatible
additions, no version bump"). The envelope-level gate
`remote_collector.py:447` (`schema_version != WIRE_SCHEMA_VERSION`)
must keep accepting old peers, so a bump here would reject every
3.3.0 peer wholesale before parsing — which is the opposite of what
we want. The version gate is reserved for **incompatible** changes
only (rename/remove field, semantic shift).

The envelope and `build_session_records` gain `host_stats: HostStats
| None`:

```python
@dataclass(frozen=True, slots=True)
class HostStats:
    cpu_pct: float            # /proc/stat delta over ~50ms
    mem_used_kib: int         # MemTotal - MemAvailable
    mem_total_kib: int        # MemTotal
    loadavg_1m: float         # /proc/loadavg field 0
    uptime_s: int             # /proc/uptime field 0
    kernel: str               # uname -r
```

Source helper `read_host_stats() -> HostStats` lives in
`probes.py` (or a new `host_stats.py`; staying in `probes.py`
keeps the proc-reader family co-located). Stdlib only. Two
`/proc/stat` samples 50ms apart for the CPU delta; everything
else is single-shot reads.

Used in two places, **same function**:

1. Remote: `cli.do_list` (or its envelope builder) calls
   `read_host_stats()` and embeds the result. One SSH round-trip
   returns sessions + host metrics. Warm-tick (5–20ms) cost from
   the existing ControlMaster path is unchanged.
2. Local: `tui_state` keeps `state.main.host_stats` populated by
   the same call from the local refresh worker.

Old peers (3.3.0, no `host_stats` in envelope) keep working: the
version gate accepts them as before, the parser treats absent
`host_stats` as `None`, and the status bar renders `pending…` for
that bucket until the peer is upgraded.

### Files

| File | Action |
| --- | --- |
| `src/uxon/__init__.py` | `__version__ = "3.4.0.dev0"` |
| `src/uxon/wire_schema.py` | envelope `host_stats: HostStats | None` (additive optional; **no version bump**) |
| `src/uxon/probes.py` | + `read_host_stats() -> HostStats` |
| `src/uxon/cli.py` | `do_list` populates `host_stats`; `DEFAULT_CONFIG` += `ssh_control_persist_seconds` |
| `src/uxon/tui/main_data.py` | + `host_stats: HostStats | None` field on `MainData`; populated by the local refresh worker |
| `src/uxon/remote_collector.py` | `_default_template` reads persist from cfg; `RemoteSnapshot` carries `host_stats` |
| `src/uxon/remote_hosts.py` | `RemoteHost.color: str | None` field; load-time validation |
| `src/uxon/settings.py` | register `ssh_multiplex`, `ssh_control_persist_seconds`, `tui.table.default_view`, `tui.search.fields`, `tui.color_palette`, `local_host.color`; remove `tui.table.default_sort_by` |
| `src/uxon/tui/dashboard/ui_state.py` | drop `sort_by`/`sort_dir`; add `view_mode` + `set_view_mode`; keep `filter_text`/`set_filter` |
| `src/uxon/tui/dashboard/model.py` | rewrite `_build` per the new contract; pure-fields filter |
| `src/uxon/tui/dashboard/columns.py` | drop `host_colour` + `_HOST_PALETTE`; add `assign_block_colors` (pure helper); `_format_name` emits `●`/`○` glyph + plain name; `_format_host` emits plain host token; **no signature change** — block hue / zebra applied by widget; `path.default_visible=False` |
| `src/uxon/tui/dashboard/buckets.py` | **new**: `HostBucket`, `select_host_buckets`, `select_host_status_block` |
| `src/uxon/tui/dashboard/reconcile.py` | `diff` returns `ApplyPlan` (ops + new_keys); apply path consumes both |
| `src/uxon/tui/widgets/session_dashboard_table.py` | `apply` applies `RowAdd` ops in reverse new-index order; wraps NAME/HOST cells with block-hue + zebra dim from a parallel `row_key → (block_color, row_in_block)` map held by the widget |
| `src/uxon/tui/widgets/host_tab_strip.py` | **new** |
| `src/uxon/tui/widgets/host_status_bar.py` | **new** |
| `src/uxon/tui/widgets/search_bar.py` | **new** |
| `src/uxon/tui/screens/main.py` | wire view toggle, host tabs, status bar, search bar; default focus to search; drop `s`/`S`/`Esc→quit`; use `bindings_with_aliases` |
| `src/uxon/tui/keymap.py` | **new**: `LAYOUT_ALIASES` + helper |
| `src/uxon/tui/styles.tcss` | tab-strip, status-bar, search-bar styles |
| `src/uxon/tui/context.py` | drop `tui_table_default_sort_by`; add `tui_table_default_view`, `tui_search_fields`, `tui_color_palette`, `local_host_color`, `ssh_control_persist_seconds` |
| `docs/configuration.md` | new keys, view contract, sort contract, search contract, keymap |
| `docs/agents/conventions.md` | flag `[ssh]/[tui]` namespace consolidation as backlog |
| `CHANGELOG.md` | one block under `[Unreleased]` |
| `tests/` | see Testing |

### Bindings — final table

| Key | RU twin | Action | Footer |
| --- | --- | --- | --- |
| `q` | `й` | quit | yes |
| `r` | `к` | refresh | yes |
| `d` | `в` | kill | yes |
| `D` | `В` | kill-all (mine) | yes |
| `v` | `м` | toggle view | yes |
| `[` | `х` | prev host tab | yes |
| `]` | `ъ` | next host tab | yes |
| `/` | `.` | focus search | yes |
| `1`–`9` | (same) | digit jump | 1 yes |
| `Up` / `Down` | (layout-invariant) | cross-widget focus | no |
| `Esc` | (layout-invariant) | scoped cancel: clear search → blur → no-op | no |

## Edge cases

- **Single-host config** (`cfg.remote_hosts == []`): by-host view
  collapses to one `local` tab. `[`/`]` are no-ops (footer entries
  remain visible — the strip is one tab wide). No auto-flip to flat.
- **Configured peer with no fetched snapshot**: bucket exists; tab
  rendered; status bar shows `pending…`; sessions area shows
  empty-state copy.
- **Configured peer unreachable** (breaker open + no cache): bucket
  exists; tab rendered; status bar shows `unreachable` in
  `$uxon-danger`.
- **Filter active + view_mode == "by_host"**: render forces flat;
  tab strip hidden; on filter clear, view returns to by-host on the
  previously-active tab (the index reactive is preserved across
  the override).
- **Operator pins same colour to multiple remotes**: rendered as
  pinned; adjacency-skip does not apply to pinned-pinned pairs (it
  only protects auto-cycle output). Operator's choice.
- **Operator pins `green` to a remote and keeps locals green**:
  visual collision; not flagged. Their choice.
- **Old peer (3.3.0) without `host_stats`**: schema version is
  unchanged, envelope parses normally, `host_stats=None`; status bar
  shows `pending…` for that bucket forever until the peer is
  upgraded. No error, no log spam.
- **Search input: paste of long string**: filter applies live; if
  filter matches nothing, table shows zero rows + the
  `0 matches` counter; pressing `Esc` clears.

## Testing

- `tests/test_dashboard_model_order.py` (new): `_within_block_key`
  contract over locals + 2 remote hosts + various last_attached
  combinations.
- `tests/test_dashboard_ui_state.py`: replace cycle/toggle tests
  with `set_view_mode` (identity-on-noop, flip behaviour) +
  `set_filter` (existing, kept).
- `tests/test_dashboard_buckets.py` (new): bucket order matches
  `cfg.remote_hosts`; empty buckets present; status block aggregates
  match.
- `tests/test_dashboard_reconcile.py`: extend with
  `[A,B,C,D] → [D,C,B,A]` reverse permutation; assert final visual
  order via the apply plan; today's code fails this test.
- `tests/test_dashboard_block_colors.py` (new): adjacency-skip;
  pin precedence; pinned-equal-prev allowed; locals colour
  configurable.
- `tests/test_uxon_keymap.py` (new): the helper produces twin
  bindings for every key in `LAYOUT_ALIASES` and passes through
  unknown keys without error. **No test enforces full coverage of
  bindings by aliases.**
- `tests/test_uxon_tui_bindings.py`: assert `s`/`S`/`Esc→quit` are
  not on `MainScreen`; assert `q`/`v`/`[`/`]`/`/` are present;
  assert default footer text matches.
- `tests/test_host_stats.py` (new): `read_host_stats()` against a
  fixture `/proc/*` directory; ranges sane.
- `tests/test_remote_hosts_color.py` (new): `[[remote_hosts]]
  color` field accepted, defaults to None, validation lenient.
- `tests/test_settings_ssh_keys.py` (new): `ssh_multiplex` and
  `ssh_control_persist_seconds` registered; persist must be > 0.
- One `Pilot` smoke scenario (joins existing batch where possible):
  mount with two configured hosts → SearchBar has focus → press
  `/` (already focused, no-op) → type `cosm` → assert flat render
  forced + counter `1 match` → press `Esc` → assert filter cleared,
  focus stays → press `Esc` → assert focus leaves SearchBar →
  press `]` → assert active tab advances → press `q` → app exits.

## Migration

- Operators with `tui.table.default_sort_by` in TOML: read on next
  run emits one `UXON_DEBUG=tui` line; key ignored. No CLI error.
- `tui.table.columns` keeps working unchanged.
- Operators with old peers (3.3.0, no `host_stats` field): peers
  continue to work — the wire-schema version is unchanged, only the
  envelope gains an optional field. Status bar shows `pending…` for
  their bucket until the peer is upgraded. Single-operator user
  (current case) updates everywhere in one pass.
- `__version__` 3.3.0 → 3.4.0.dev0 on feat-branch start. Bump to
  bare `3.4.0` only on release commit.

## Commit sequence

Implementation lands on `feat/dashboard-views` from `dev`. Each
commit is independently buildable and testable.

| # | Subject | Scope |
| --- | --- | --- |
| 0 | `chore(version): bump to 3.4.0.dev0` | `__version__` only; CHANGELOG `[Unreleased]` block opened |
| 1 | `fix(reconcile): apply RowAdd in reverse new-index order` | reconcile + apply + reverse-permutation regression test. Lands first so subsequent commits can rely on correct ordering. |
| 2 | `feat(wire): host_stats in envelope (additive optional)` | wire_schema, probes.read_host_stats, envelope builder, parser tolerance for absent field. **No `WIRE_SCHEMA_VERSION` bump** — additive field, see `wire_schema.py` "forward-compatible additions" note. |
| 3 | `feat(dashboard): hard sort contract; drop sort cycle` | model._build rewrite, drop sort_by/sort_dir, drop reducers, drop tui.table.default_sort_by from schema, drop s/S bindings |
| 4 | `feat(dashboard): block colors; attach via glyph` | drop host_colour, add assign_block_colors, ●/○ glyph, no bold-green; column path default off |
| 5 | `feat(tui): keymap aliases + remove Esc-quit` | tui/keymap.py, bindings_with_aliases applied screen-wide, q-only quit, scoped Esc; tests |
| 6 | `feat(tui): view modes (by_host default, flat toggle)` | DashboardUiState.view_mode, set_view_mode, `v` binding, tui.table.default_view setting |
| 7 | `feat(tui): host buckets + HostStatusBar` | dashboard/buckets.py, widgets/host_status_bar.py |
| 8 | `feat(tui): host tab strip` | widgets/host_tab_strip.py, `[`/`]` bindings, wrap behaviour |
| 9 | `feat(tui): search bar` | widgets/search_bar.py, default focus, `/` binding, scoped Esc, tui.search.fields |
| 10 | `feat(ssh): ControlPersist configurable; register schema` | ssh_control_persist_seconds, register ssh_multiplex, validation |
| 11 | `feat(tui): wire host_stats into HostStatusBar` | render path; pending/cached/unreachable states |
| 12 | `docs: configuration + agents conventions backlog` | docs/configuration.md, docs/agents/conventions.md flag |
| 13 | `chore(changelog): finalise [Unreleased] entries` | CHANGELOG; no functional change |

Each commit ships its own tests. CI gate stays green at every
commit.
