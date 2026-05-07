# TUI keybindings

`uxon` with no arguments on a TTY opens the interactive picker. All
keys go through Textual `BINDINGS` declarations — the footer shows
the visible subset.

## Main screen

| Key | Action |
|---|---|
| `↑` / `↓` | Navigate items |
| `1`–`9` | Jump to item by number |
| `Enter` | Activate (launch / attach) |
| `d` | Kill highlighted session (with confirmation) |
| `D` (Shift+d) | Kill all *own* sessions (`kill-all` to confirm) |
| `s` | Cycle dashboard sort column (cpu → ram → last → name) |
| `S` (Shift+s) | Toggle sort direction |
| `r` | Refresh |
| `q` / `Esc` | Quit |
| `F1` | Help (hidden) |

When the agent-detection banner is showing:

| Key | Action |
|---|---|
| `a` | Enable detected agent in repo `config.toml` |
| `x` | Dismiss the suggestion (per-user, persisted) |

## "Open existing project" screen

| Key | Action |
|---|---|
| `↑` / `↓` (or `k` / `j`) | Navigate |
| `1`–`9` | Pick by number |
| `Enter` | Confirm |
| `Esc` | Cancel |

## "Pick git remote profile" screen

| Key | Action |
|---|---|
| `0`–`9` | Pick profile by number |
| `Enter` | Confirm |
| `Esc` | Cancel |

## ⚙ Settings screen (superuser block only)

| Key | Action |
|---|---|
| `Enter` | Edit selected key |
| `x` | Reset selected key to default |
| `Esc` / `q` | Back to main screen |

The edit modal accepts `Esc` to cancel and `↑` / `↓` to focus
between input and OK button.

## Confirm dialogs

| Key | Action |
|---|---|
| `y` / `Enter` | Yes (only when the typed-phrase guard is satisfied) |
| `n` / `Esc` | No / cancel |

Destructive actions (`d`, `D`, `kill-all-reachable`) require typing
the literal phrase — `kill`, `kill-all`, or `kill-all-reachable`
respectively — before `Enter` confirms.

## Drift guard

`tests/test_uxon_tui_bindings.py` enforces that all key handling
goes through `BINDINGS` (no `on_key` overrides) and that every
destructive binding has `show=True` plus a non-empty description.
The footer of the running TUI is the source of truth — if a
binding isn't shown, it's intentionally hidden, not missing.
