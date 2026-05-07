# Customise dashboard columns

The TUI's session dashboard ships a default column layout that
suits most setups. The `[tui.table]` block lets you override it.

```toml
[tui.table]
columns      = ["name", "user", "cpu", "ram", "last", "cmd"]
default_view = "by_host"
```

## What each key does

- **`tui.table.columns`** — list of column ids in display order.
  Leave empty (or omit) to use the registry defaults: every
  column whose `default_visible` is true plus any that the
  runtime layout promotes (`host` in multi-host setups, `user`
  when other-user rows are visible). Listing columns explicitly
  opts into a fixed visual order; ids unknown to the running
  `uxon` version are silently dropped (an older config carrying
  a since-removed column id stays loadable). The `path` column
  is hidden by default — opt back in by listing `"path"` here.
- **`tui.table.default_view`** — `"by_host"` (default) or
  `"flat"`. `by_host` shows the per-host tab strip and status
  bar; `flat` is a single ranked list across the fleet. Toggle
  at runtime with `v`.

There is no sort setting. Sort is a fixed contract owned by
the model selector — locals first (own then other-user), then
remotes in `[[remote_hosts]]` declaration order, with
within-block ranking by last-attach descending then name
ascending. The legacy `tui.table.default_sort_by` key is
silently ignored on load (one `UXON_DEBUG=tui` line per
occurrence).

## Available column ids

`host`, `user`, `name`, `agent`, `cpu`, `ram`, `new`, `last`,
`cmd`, `path`, `pid`, `wins`.

The full contract (which ids are gated by which runtime flags,
alignment, formatting) lives in
[`src/uxon/tui/dashboard/columns.py`](../../../src/uxon/tui/dashboard/columns.py).

## Examples

**Compact for narrow terminals:**

```toml
[tui.table]
columns = ["name", "cpu", "ram", "last"]
```

**Multi-host operator view (start in flat mode):**

```toml
[tui.table]
columns      = ["host", "user", "name", "agent", "cpu", "ram", "last"]
default_view = "flat"
```

**Path-focused for navigation:**

```toml
[tui.table]
columns = ["name", "path", "last"]
```

## View, search, attach indicator

- `v` toggles between `by_host` (per-host tabs + status bar)
  and `flat` (single ranked list). Configure the initial view
  with `tui.table.default_view`.
- The dashboard search bar takes focus on TUI mount; press `/`
  from anywhere to refocus, `Esc` to clear-and-blur. While a
  search query is active, the view is forced to `flat` so
  matches across hosts appear in one list; clearing the query
  restores the previous view mode. Configure searchable fields
  with `tui.search.fields` (default `["name", "user"]`; allowed
  `name`, `user`, `host`, `path`, `cmd`).
- Attached state is shown by a glyph in the NAME column: `●`
  filled when attached, `○` hollow otherwise. There is no bold
  green override.

## Colour and accessibility

Each host gets a block colour applied to its tab, status-bar
name token, and dashboard rows. Configure:

- Per-host pin: `[[remote_hosts]] color = "..."` (any Rich
  style spec; pin wins unconditionally over the auto-cycle).
- Auto-cycle palette: `[tui] color_palette = ["cyan", "blue", ...]`.
- Local block colour: `[local_host] color = "green"`.

Colours are decorative — every row's HOST and USER are also
present as text. There is currently no `UXON_COLOR=0` knob; if
your team needs a no-colour mode, file a feature request.

## Reference

- [`../../reference/configuration.md`](../../reference/configuration.md) — `[tui.table]`, `[tui.search]`, `[tui]`, `[local_host]` keys.
- [`../../reference/keybindings.md`](../../reference/keybindings.md) — TUI keys including `v`, `[`, `]`, `/`.
