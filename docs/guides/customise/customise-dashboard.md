# Customise dashboard columns

The TUI's session dashboard ships a default column layout that
suits most setups. The `[tui.table]` block lets you override it.

```toml
[tui.table]
columns         = ["name", "user", "cpu", "ram", "last", "cmd"]
default_sort_by = "cpu"
```

## What each key does

- **`tui.table.columns`** — list of column ids in display order.
  Leave empty (or omit) to use the registry defaults: every
  column whose `default_visible` is true plus any that the
  runtime layout promotes (`host` in multi-host setups, `user`
  when other-user rows are visible). Listing columns explicitly
  opts into a fixed visual order; ids unknown to the running
  `uxon` version are silently dropped (an older config carrying
  a since-removed column id stays loadable).
- **`tui.table.default_sort_by`** — column id used as the
  initial sort on TUI startup. Defaults to `"cpu"`. Unknown
  values fall back to `"cpu"` (with a debug-log entry on
  `UXON_DEBUG=tui`); the TUI never refuses to start because of
  a cosmetic setting.

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

**Multi-host operator view:**

```toml
[tui.table]
columns = ["host", "user", "name", "agent", "cpu", "ram", "last"]
default_sort_by = "host"
```

**Path-focused for navigation:**

```toml
[tui.table]
columns = ["name", "path", "last"]
default_sort_by = "last"
```

## Sort keys

The TUI's `s` cycles through cpu → ram → last → name; `S`
toggles direction. `default_sort_by` only sets the initial
column; users can change at runtime.

## Colour and accessibility

The HOST column uses a per-host colour glyph in multi-host
setups, and local-user rows render in a distinct colour.
Colours are decorative — every row's HOST and USER are also
present as text. There is currently no `UXON_COLOR=0` knob; if
your team needs a no-colour mode, file a feature request.

## Reference

- [`../../reference/configuration.md`](../../reference/configuration.md) — `[tui.table]` keys.
- [`../../reference/keybindings.md`](../../reference/keybindings.md) — TUI keys including `s` / `S`.
