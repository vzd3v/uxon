# TUI keybindings

`uxon` with no arguments on a TTY opens the interactive picker. All
keys go through Textual `BINDINGS` declarations — the footer shows
the visible subset.

## Main screen

| Key | Action |
|---|---|
| `↑` / `↓` | Navigate items |
| `←` / `→` | Top action row: cycle the three buttons cyclically. Dashboard: in `by_host` advance the active host tab; in `flat` jump the cursor across `(host, own/other)` transitions cyclically. |
| `Enter` | Activate (launch / attach) |
| `d` | Kill highlighted session (with confirmation) |
| `D` (Shift+d) | Kill all *own* sessions (`kill-all` to confirm) |
| `v` | Toggle dashboard view (`flat` ↔ `by_host`) |
| `h` | Toggle the fleet status bar (collapsed counts+alerts ↔ per-host detail) |
| `s` (or `/`) | Focus the search bar from anywhere |
| `r` | Refresh |
| `q` | Quit |
| `Esc` | Scoped cancel: clear search / close modal / leave field. Never quits. |
| `F1` | Help (hidden) |

Sort is a fixed contract (locals → cfg-order remotes →
within-block by recency); there are no sort bindings.

JCUKEN twins: every dashboard key has a Russian-layout twin
(`q`/`й`, `r`/`к`, `d`/`в`, `D`/`В`, `s`/`ы`, `v`/`м`, `h`/`р`) so the
keymap survives a Cyrillic layout without `xkb` tweaks.

## "Open existing project" screen

The filter input owns focus on mount — start typing to narrow the
list. Cursor and selection bindings work without leaving the input.

| Key | Action |
|---|---|
| typing | Filter the list (case-insensitive substring on name) |
| `↑` / `↓` | Navigate the (filtered) list |
| `Enter` | Confirm the row under the cursor |
| `Esc` | Clear filter when non-empty, otherwise cancel |

## "Pick git remote profile" screen

| Key | Action |
|---|---|
| `0`–`9` | Pick profile by number |
| `Enter` | Confirm |
| `Esc` | Cancel |

## "Existing session" modal

Pushed by every launch action (`New session in current folder`,
`Create new project`, `Open existing project`) when a compatible
session for the target directory already exists. Lets the operator
attach to an existing session or knowingly start a parallel one.

| Key | Action |
|---|---|
| `↑` / `↓` | Navigate the existing-session list |
| `a` / `Enter` | Attach to the highlighted session |
| `n` | Start a new (parallel) session |
| `Esc` | Cancel the launch |

## ⚙ Settings screen (superuser block only)

| Key | Action |
|---|---|
| `Enter` | Edit selected key |
| `x` | Reset selected key to default |
| `q` | Back to main screen (`Esc` cancels in-flight edits) |

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
