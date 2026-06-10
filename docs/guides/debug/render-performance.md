# Tune dashboard render performance

The TUI dashboard is built to stay quiet when you are idle: a steady
telemetry tick repaints nothing visible, and arrow navigation repaints
only the rows the cursor leaves and lands on. If you still see the
terminal working harder than expected, the usual culprit is your
terminal multiplexer forwarding focus events.

## Turn off terminal focus events

When a `tmux` pane gains or loses focus (you switch windows, or the
terminal app loses focus), `tmux` forwards a focus event to `uxon`.
Textual responds by re-evaluating styles across the whole screen.
`uxon` uses **no** focus-dependent styling, so this restyle changes
nothing you can see — it is pure cost.

If focus-event churn is bothering you, turn it off at the `tmux`
level:

```bash
tmux set -g focus-events off
```

Nothing is lost visually — `uxon` has no app-focus styles to drive.
This is the recommended setting for operators who switch windows
frequently while a dashboard is open.

## See also

- [Enable debug logs](enable-debug-logs.md) — the `debug` channel,
  including the `keys` topic for tracing keypress handling.
