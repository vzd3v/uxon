# Customise dashboard columns

Choose a layout for your terminal and workflow. Exact column ids, defaults,
sorting and display semantics belong to the [configuration reference](../../reference/configuration.md#tuitable-table);
interactive controls belong to the [keybinding reference](../../reference/keybindings.md).

## Compact for narrow terminals

```toml
[tui.table]
columns = ["name", "cpu", "ram", "last"]
```

## Multi-host operator view

```toml
[tui.table]
columns = ["host", "user", "name", "agent", "cpu", "ram", "last"]
default_view = "by_host"
```

## Path-focused navigation

```toml
[tui.table]
columns = ["name", "path", "last"]
```

Configure searchable fields separately through [tui.search](../../reference/configuration.md#tuisearch-table).
Explicit column lists give you a fixed layout; remove the list to return to the
runtime-aware defaults.

## Distinguish hosts with colour

```toml
[local_host]
color = "green"

[tui]
color_palette = ["cyan", "blue", "magenta"]
```

A peer can pin its own colour in its existing `[[remote_hosts]]` block. See
[colour configuration](../../reference/configuration.md#tui-colour-palette).
HOST and USER remain textual identifiers; do not rely on colour alone.

## Check runtime telemetry

A container row should show workload usage rather than the idle host exec
client. If several sessions show the same container total, check whether the
controller can read the workload process environments needed for nonce-based
attribution. A down marker means the resource is stopped or unresolved, not an
idle workload. See the [runtime telemetry contract](../../reference/configuration.md#runtimesid-table)
and [container setup checks](run-agents-in-a-container.md#verify-the-rootless-setup).

## Related

- [Diagnose multi-host state](../debug/diagnose-multi-host.md).
- [Profile refresh/render cost](../debug/render-performance.md).
