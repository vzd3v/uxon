"""Interactive TUI session picker for ccw.

Uses blessed for terminal rendering with colors, arrow-key navigation,
and inline confirmations for kill/kill-all actions.

Main screen layout:
  - Action items (new session in cwd, create project, open existing project)
  - ── sessions ── (own sessions for the current launch user)
  - ── superuser ── (other users' sessions + ⚙ Settings + global kill-all) —
    shown whenever passwordless sudo is detected

Sub-screens:
  - Permission prompt (regular vs --dsp) before any launch
  - Project name input for "Create new project"
  - Project picker for "Open existing project"
  - Settings list + per-type editor (see ccw_tui_settings.py)

Per-screen key bindings are documented in the :data:`SCREEN_KEYMAP`
registry at the bottom of this module. Each screen's inline ``t.inkey``
handler is the source of truth at runtime; :data:`SCREEN_KEYMAP`
is the hand-curated declaration that reviewers and tests use to
detect silent overloads or drift. A regression test
(``KeymapRegistryTests`` in tests/test_ccw_tui.py) walks the module
source and verifies every runtime binding is declared in the registry
for the screen that owns it.
"""

from __future__ import annotations

import os
import subprocess  # noqa: F401 — tests monkey-patch ccw_tui.subprocess
import sys
import traceback
from enum import Enum
from typing import TYPE_CHECKING, Any

from ccw_tui_widgets import confirm_phrase as _confirm_phrase_widget
from ccw_tui_widgets import dim as _dim_widget

# Re-exported pure data / events / launch helpers live in sibling modules.
from .context import (
    ACTION_COUNT,
    CallbackError,
    Item,
    LaunchRequest,
    TuiContext,
    TuiSession,
    _ACTION_KINDS,
    _digit_hinted_indices,
    _segments,
    _total_items,
    build_items,
)
from .events import LOG_DIR, _log_dir, _log_event
from .launch import (
    FAST_EXIT_THRESHOLD_SEC,
    _drain_stdin,
    _format_launch_status,
    _pause_on_launch_failure,
    _run_launch_request,
)

if TYPE_CHECKING:
    from blessed import Terminal


BLESSED_MISSING_HINT = (
    "ccw: interactive mode requires the 'blessed' package.\n"
    "  Install system-wide:  sudo apt install python3-blessed\n"
    "  Or per-user:          pip install --user blessed"
)


def _dim(t: "Terminal", text: str) -> str:
    """Kept as a local alias for backwards compatibility with existing tests."""
    return _dim_widget(t, text)


# ── Rendering helpers ────────────────────────────────────────────────


def _build_header(t: "Terminal", ctx: TuiContext) -> str:
    count = len(ctx.sessions) + len(ctx.other_sessions)
    title = " ccw interactive "
    stats = f" {count} sessions  cpu={ctx.total_cpu}  ram={ctx.total_ram} "
    if ctx.has_sudo:
        stats += " ⚡superuser "
    return t.bold_white_on_blue(title) + "  " + _dim(t, stats)


def _render_action_row(t: "Terminal", num: int, label: str, detail: str, selected: bool) -> str:
    cursor = t.bold_cyan("▸ ") if selected else "  "
    num_str = _dim(t, f"{num} ") if not selected else f"{num} "
    text = num_str + t.bold_green("+ ") + t.bold(label) + "  " + _dim(t, detail)
    if selected:
        return t.reverse(t.ljust(cursor + text, t.width))
    return cursor + text


def _render_session_row(
    t: "Terminal",
    s: TuiSession,
    selected: bool,
    col_widths: dict[str, int],
    num: int = 0,
    show_user: bool = False,
) -> str:
    """Render one session row with color coding. When show_user=True, prepends
    a yellow-highlighted USER column (for other-user sessions)."""
    nw = col_widths["name"]
    pw = col_widths["pid"]
    cw = col_widths["cpu"]
    rw = col_widths["ram"]
    cmw = col_widths["cmd"]

    cursor = t.bold_cyan("▸ ") if selected else "  "
    if 1 <= num <= 9:
        num_str = _dim(t, f"{num} ") if not selected else f"{num} "
    else:
        num_str = "  "

    if show_user:
        uw = col_widths.get("user", 4)
        user_str = t.bold_yellow(f"{s.user:<{uw}}") + "  "
    else:
        user_str = ""

    if s.attached:
        name_str = t.bold_green(f"{s.short:<{nw}}") + t.green(" ●")
    else:
        name_str = f"{s.short:<{nw}}" + "  "

    pid_str = f"{s.pid:>{pw}}"

    cpu_val = s.cpu
    if cpu_val != "-":
        try:
            v = float(cpu_val)
            if v > 50:
                cpu_str = t.bold_red(f"{cpu_val:>{cw}}")
            elif v > 10:
                cpu_str = t.yellow(f"{cpu_val:>{cw}}")
            else:
                cpu_str = _dim(t, f"{cpu_val:>{cw}}")
        except ValueError:
            cpu_str = _dim(t, f"{cpu_val:>{cw}}")
    else:
        cpu_str = _dim(t, f"{cpu_val:>{cw}}")

    ram_str = f"{s.ram:>{rw}}"
    created_str = f"{s.created:<5}"
    last_str = f"{s.last_activity:<5}"
    cmd_str = _dim(t, f"{s.cmd:<{cmw}}")
    path_str = _dim(t, s.path)

    row = f"{cursor}{num_str}{user_str}{name_str}  {pid_str}  {cpu_str}  {ram_str}  {created_str}  {last_str}  {cmd_str}  {path_str}"

    if selected:
        return t.reverse(t.ljust(row, t.width))
    return row


def _compute_col_widths(sessions: list[TuiSession], include_user: bool = False) -> dict[str, int]:
    if not sessions:
        widths = {"name": 4, "pid": 3, "cpu": 3, "ram": 3, "cmd": 3}
        if include_user:
            widths["user"] = 4
        return widths
    widths = {
        "name": max(4, max(len(s.short) for s in sessions)),
        "pid": max(3, max(len(s.pid) for s in sessions)),
        "cpu": max(3, max(len(s.cpu) for s in sessions)),
        "ram": max(3, max(len(s.ram) for s in sessions)),
        "cmd": max(3, max(len(s.cmd) for s in sessions)),
    }
    if include_user:
        widths["user"] = max(4, max(len(s.user) for s in sessions))
    return widths


def _render_column_header(t: "Terminal", col_widths: dict[str, int], show_user: bool = False) -> str:
    nw = col_widths["name"]
    pw = col_widths["pid"]
    cw = col_widths["cpu"]
    rw = col_widths["ram"]
    cmw = col_widths["cmd"]
    if show_user:
        uw = col_widths.get("user", 4)
        user_hdr = f"{'USER':<{uw}}  "
    else:
        user_hdr = ""
    return _dim(
        t,
        f"    {user_hdr}{'NAME':<{nw}}    {'PID':>{pw}}  {'CPU':>{cw}}  {'RAM':>{rw}}  "
        f"{'NEW':<5}  {'LAST':<5}  {'CMD':<{cmw}}  PATH",
    )


def _render_kill_all_global_row(t: "Terminal", num: int, selected: bool, total_count: int) -> str:
    cursor = t.bold_cyan("▸ ") if selected else "  "
    if 1 <= num <= 9:
        num_str = _dim(t, f"{num} ") if not selected else f"{num} "
    else:
        num_str = "  "
    text = num_str + t.bold_yellow("⚡ ") + t.bold_red(f"Kill ALL ccw sessions (all users, {total_count} total)")
    if selected:
        return t.reverse(t.ljust(cursor + text, t.width))
    return cursor + text


def _render_settings_row(t: "Terminal", num: int, selected: bool) -> str:
    cursor = t.bold_cyan("▸ ") if selected else "  "
    if 1 <= num <= 9:
        num_str = _dim(t, f"{num} ") if not selected else f"{num} "
    else:
        num_str = "  "
    text = num_str + t.bold_yellow("⚙ ") + t.bold("Settings") + "  " + _dim(t, "(repo-level config.toml)")
    if selected:
        return t.reverse(t.ljust(cursor + text, t.width))
    return cursor + text


# ── Confirmation prompts ─────────────────────────────────────────────


def _confirm_kill(t: "Terminal", session_name: str, y: int, user: str | None = None) -> bool:
    if user:
        prompt = t.bold_red(f"  Kill {session_name} (user={user})? ") + _dim(t, "y/N ")
    else:
        prompt = t.bold_red(f"  Kill {session_name}? ") + _dim(t, "y/N ")
    with t.location(0, y):
        print(t.clear_eol + prompt, end="", flush=True)
    key = t.inkey(timeout=10)
    return key.lower() == "y"


def _confirm_kill_all(t: "Terminal", count: int, y: int) -> bool:
    prompt = t.bold_red(f"  Kill ALL {count} sessions? ") + _dim(t, "Type 'kill-all' to confirm: ")
    return _confirm_phrase_widget(t, prompt, "kill-all", y)


def _confirm_kill_all_global(t: "Terminal", count: int, y: int) -> bool:
    prompt = t.bold_red(f"  Kill ALL {count} sessions across ALL users? ") + _dim(
        t, "Type 'kill-all-global' to confirm: "
    )
    return _confirm_phrase_widget(t, prompt, "kill-all-global", y)


# ── Sub-screens ──────────────────────────────────────────────────────


def _prompt_permissions(t: "Terminal") -> bool | None:
    """Show permission choice. Returns True for dsp, False for regular, None for cancel."""
    from ccw_tui_mouse import MouseEvent, HitRegion, hit_test, read_input
    options = [
        ("Regular permissions", "default, safe mode"),
        ("ALL PERMISSIONS", "--dangerously-skip-permissions"),
    ]
    cursor = 0

    while True:
        print(t.home + t.clear, end="")
        print(t.bold_white_on_blue(" Launch permissions "))
        print(_dim(t, "─" * t.width))
        print()

        regions: list[HitRegion] = []
        # Header(1) + separator(1) + blank(1) = 3 lines before first row.
        row_y = 3
        for i, (label, detail) in enumerate(options):
            sel = i == cursor
            prefix = t.bold_cyan("▸ ") if sel else "  "
            num_str = _dim(t, f"{i + 1} ") if not sel else f"{i + 1} "
            if i == 0:
                text = num_str + t.bold(label) + "  " + _dim(t, detail)
            else:
                text = num_str + t.bold_yellow(label) + "  " + t.red + t.dim + detail + t.normal
            line = prefix + text
            if sel:
                print(t.reverse(t.ljust(line, t.width)))
            else:
                print(line)
            regions.append(HitRegion(y=row_y, action="row", payload=i))
            row_y += 1

        footer_y = t.height - 1
        with t.location(0, footer_y):
            print(_build_footer(t, "permissions"), end="")

        ev = read_input(t, timeout=None)
        if isinstance(ev, MouseEvent):
            if ev.wheel < 0:
                cursor = max(0, cursor - 1)
                continue
            if ev.wheel > 0:
                cursor = min(len(options) - 1, cursor + 1)
                continue
            hit = hit_test(regions, y=ev.y - 1)
            if hit and hit.action == "row":
                if ev.pressed:
                    cursor = int(hit.payload)
                else:
                    return int(hit.payload) == 1  # True = dsp
            continue
        key = ev
        if key.name == "KEY_ESCAPE" or key == "q":
            return None
        if key == "1":
            return False  # regular
        if key == "2":
            return True  # dsp
        if key.name == "KEY_UP" or key == "k":
            cursor = max(0, cursor - 1)
        elif key.name == "KEY_DOWN" or key == "j":
            cursor = min(len(options) - 1, cursor + 1)
        elif key.name == "KEY_ENTER" or key == "\n" or key == "\r":
            return cursor == 1  # True = dsp


def _prompt_project_name(t: "Terminal", root: str) -> str | None:
    """Text input for new project name. Returns name or None for cancel."""
    buf = ""
    error_msg = ""

    while True:
        print(t.home + t.clear, end="")
        print(t.bold_white_on_blue(" Create new project "))
        print(_dim(t, "─" * t.width))
        print()
        print(f"  {_dim(t, 'Directory:')} {root}/")
        print()
        print(f"  {t.bold('Project name:')} {buf}" + t.bold_cyan("█"))

        if error_msg:
            print()
            print(f"  {t.bold_red(error_msg)}")

        footer_y = t.height - 1
        with t.location(0, footer_y):
            print(_build_footer(t, "input"), end="")

        key = t.inkey(timeout=None)
        if key.name == "KEY_ESCAPE":
            return None
        if key.name == "KEY_ENTER" or key == "\n" or key == "\r":
            name = buf.strip()
            if not name:
                error_msg = "Name cannot be empty"
                continue
            if "/" in name or name in (".", ".."):
                error_msg = "Invalid name"
                continue
            return name
        if key.name == "KEY_BACKSPACE" or key == "\x7f":
            if buf:
                buf = buf[:-1]
            error_msg = ""
            continue
        if key.is_sequence:
            continue
        buf += str(key)
        error_msg = ""


def _prompt_git_profile(
    t: "Terminal",
    options: list[tuple[str, str]],
    default_name: str,
) -> str | None:
    """Ask whether to create a git remote and which profile to use.

    Returns:
      - "" if the user chose to skip git creation,
      - profile name if selected,
      - None if the user pressed Esc to cancel the whole new-project flow.
    """
    if not options:
        return ""

    from ccw_tui_mouse import MouseEvent, HitRegion, hit_test, read_input

    # Menu rows: 0 = skip, then one per profile.
    rows = [("", "skip — don't create any git remote")] + list(options)
    default_cursor = 0
    if default_name:
        for i, (name, _) in enumerate(options):
            if name == default_name:
                default_cursor = i + 1
                break
    cursor = default_cursor

    while True:
        print(t.home + t.clear, end="")
        print(t.bold_white_on_blue(" Create git remote? "))
        print(_dim(t, "─" * t.width))
        print()
        regions: list[HitRegion] = []
        # Header(1) + separator(1) + blank(1) = 3 lines before first row.
        row_y = 3
        for i, (name, desc) in enumerate(rows):
            selected = i == cursor
            prefix = t.bold_cyan("▸ ") if selected else "  "
            num = _dim(t, f"{i} ") if not selected else f"{i} "
            label = t.bold("skip") if not name else t.bold(name)
            row = prefix + num + label + "  " + _dim(t, desc)
            if selected:
                print(t.reverse(t.ljust(row, t.width)))
            else:
                print(row)
            regions.append(HitRegion(y=row_y, action="row", payload=i))
            row_y += 1
        footer_y = t.height - 1
        with t.location(0, footer_y):
            print(_build_footer(t, "git_profile"), end="")

        ev = read_input(t, timeout=None)
        if isinstance(ev, MouseEvent):
            if ev.wheel < 0:
                cursor = max(0, cursor - 1)
                continue
            if ev.wheel > 0:
                cursor = min(len(rows) - 1, cursor + 1)
                continue
            hit = hit_test(regions, y=ev.y - 1)
            if hit and hit.action == "row":
                if ev.pressed:
                    cursor = int(hit.payload)
                else:
                    return rows[int(hit.payload)][0]
            continue
        key = ev
        if key.name == "KEY_ESCAPE":
            return None
        if key.name == "KEY_UP" or key == "k":
            cursor = max(0, cursor - 1)
        elif key.name == "KEY_DOWN" or key == "j":
            cursor = min(len(rows) - 1, cursor + 1)
        elif key.name == "KEY_ENTER" or key == "\n" or key == "\r":
            return rows[cursor][0]
        elif str(key).isdigit():
            idx = int(str(key))
            if 0 <= idx < len(rows):
                cursor = idx


def _prompt_existing_project(t: "Terminal", projects: list[str], root: str) -> str | None:
    """Project picker from existing directories. Returns name or None for cancel."""
    if not projects:
        return None
    from ccw_tui_mouse import MouseEvent, HitRegion, hit_test, read_input
    cursor = 0
    scroll_offset = 0

    while True:
        print(t.home + t.clear, end="")
        print(t.bold_white_on_blue(" Open existing project "))
        print(_dim(t, "─" * t.width))
        print(f"  {_dim(t, root)}/")
        print()

        available = max(1, t.height - 6)
        if cursor < scroll_offset:
            scroll_offset = cursor
        elif cursor >= scroll_offset + available:
            scroll_offset = cursor - available + 1

        visible_end = min(scroll_offset + available, len(projects))
        regions: list[HitRegion] = []
        # Header(1) + separator(1) + root(1) + blank(1) = 4 lines printed
        # before the first item. First item row sits at y=4 (0-based).
        row_y = 4
        for i in range(scroll_offset, visible_end):
            name = projects[i]
            sel = i == cursor
            prefix = t.bold_cyan("▸ ") if sel else "  "
            num = i + 1
            if 1 <= num <= 9:
                num_str = _dim(t, f"{num} ") if not sel else f"{num} "
            else:
                num_str = "  "
            text = t.bold(name) if sel else name
            line = prefix + num_str + text
            if sel:
                print(t.reverse(t.ljust(line, t.width)))
            else:
                print(line)
            regions.append(HitRegion(y=row_y, action="row", payload=i))
            row_y += 1

        footer_y = t.height - 1
        with t.location(0, footer_y):
            print(_build_footer(t, "projects"), end="")

        ev = read_input(t, timeout=None)
        if isinstance(ev, MouseEvent):
            if ev.wheel < 0 and cursor > 0:
                cursor -= 1
                continue
            if ev.wheel > 0 and cursor < len(projects) - 1:
                cursor += 1
                continue
            hit = hit_test(regions, y=ev.y - 1)
            if hit and hit.action == "row" and not ev.pressed:
                return projects[int(hit.payload)]
            if hit and hit.action == "row" and ev.pressed:
                cursor = int(hit.payload)
            continue
        key = ev

        if key.name == "KEY_ESCAPE" or key == "q":
            return None
        if key.name == "KEY_UP" or key == "k":
            if cursor > 0:
                cursor -= 1
        elif key.name == "KEY_DOWN" or key == "j":
            if cursor < len(projects) - 1:
                cursor += 1
        elif key.name == "KEY_HOME" or key == "g":
            cursor = 0
        elif key.name == "KEY_END" or key == "G":
            cursor = len(projects) - 1
        elif key.name == "KEY_ENTER" or key == "\n" or key == "\r":
            return projects[cursor]
        elif not key.is_sequence and key in "123456789":
            idx = int(str(key)) - 1
            if 0 <= idx < len(projects):
                return projects[idx]


# ── Main screen ──────────────────────────────────────────────────────


def _draw_main_screen(
    t: "Terminal",
    ctx: TuiContext,
    cursor: int,
    scroll_offset: int,
    status_msg: str,
) -> "list":
    """Full redraw of the main TUI screen.

    Returns the list of :class:`HitRegion` entries describing which
    0-based output rows correspond to which item index — consumed by
    the main loop to route mouse clicks to the same activation handler
    as ``Enter``.
    """
    from ccw_tui_mouse import HitRegion
    regions: list = []
    own_start, other_start, settings_idx, kill_idx, has_super = _segments(ctx)
    own_widths = _compute_col_widths(ctx.sessions)
    other_widths = _compute_col_widths(ctx.other_sessions, include_user=True)
    total = _total_items(ctx)

    # Reserve: header(1) + separator(1) + footer separator(1) + footer(1) + status(1) = 5
    # Plus in-body decorations (segment separators + column headers) when applicable.
    decor = 0
    if ctx.sessions:
        decor += 2  # "─ sessions ─" + column header
    if has_super:
        decor += 1  # "─ superuser ─"
        if ctx.other_sessions:
            decor += 1  # USER column header
    available = max(1, t.height - 5 - decor)

    print(t.home + t.clear, end="")
    print(_build_header(t, ctx))
    print(_dim(t, "─" * t.width))

    visible_start = scroll_offset
    visible_end = min(scroll_offset + available, total)
    row_line = 0
    # Header (1) + separator (1) were printed via raw print() — they
    # consume 2 physical lines. The first in-loop _print lands at y=2.
    row_y = 2

    def _print(line: str) -> None:
        nonlocal row_line, row_y
        if row_line < available:
            print(line)
            row_line += 1
            row_y += 1

    for i in range(visible_start, visible_end):
        # Segment separators / column headers are drawn before the first row
        # of a new segment so layout remains consistent even when a segment
        # is empty (e.g. superuser block with only Settings and no sessions).
        if i == own_start and ctx.sessions:
            _print(_dim(t, "  ─ sessions ─" + "─" * max(0, t.width - 14)))
            _print(_render_column_header(t, own_widths))
        if has_super and i == other_start:
            _print(_dim(t, "  ─ superuser ─" + "─" * max(0, t.width - 15)))
            if ctx.other_sessions:
                _print(_render_column_header(t, other_widths, show_user=True))

        item_row_y = row_y  # the y this item row is about to occupy
        if i < own_start:
            num = i + 1
            if i == 0:
                cwd_hint = f"({ctx.cwd_short})" if ctx.cwd_allowed else f"({ctx.cwd_short} — not under allowed_roots)"
                _print(_render_action_row(t, num, "New session in current folder", cwd_hint, i == cursor))
            elif i == 1:
                _print(_render_action_row(t, num, "Create new project", f"({ctx.new_project_root}/...)", i == cursor))
            elif i == 2:
                _print(_render_action_row(t, num, "Open existing project", f"({ctx.new_project_root}/...)", i == cursor))
        elif i < other_start:
            si = i - own_start
            _print(_render_session_row(t, ctx.sessions[si], i == cursor, own_widths, num=i + 1))
        elif has_super and i < settings_idx:
            si = i - other_start
            _print(
                _render_session_row(
                    t, ctx.other_sessions[si], i == cursor, other_widths, num=i + 1, show_user=True
                )
            )
        elif has_super and i == settings_idx:
            _print(_render_settings_row(t, i + 1, i == cursor))
        elif has_super and i == kill_idx:
            total_count = len(ctx.sessions) + len(ctx.other_sessions)
            _print(_render_kill_all_global_row(t, i + 1, i == cursor, total_count))
        # Record hit region only if the row actually rendered (row_y advanced).
        if row_y > item_row_y:
            regions.append(HitRegion(y=item_row_y, action="row", payload=i))

    if total == ACTION_COUNT:
        # No sessions at all (superuser has none too). Print a hint under actions.
        empty_y = t.height // 2
        with t.location(0, empty_y):
            print(_dim(t, "  No active sessions."))

    # Footer
    footer_y = t.height - 2
    with t.location(0, footer_y):
        print(_dim(t, "─" * t.width), end="")
    with t.location(0, footer_y + 1):
        print(_build_footer(t, "main", has_sudo=ctx.has_sudo), end="")

    # Status line
    if status_msg:
        with t.location(0, t.height - 3):
            print(t.clear_eol + "  " + status_msg, end="")

    return regions


def _activate_item(
    t: "Terminal", ctx: TuiContext, index: int
) -> tuple[str | None, "LaunchRequest | None"]:
    """Handle activation (Enter / digit) for item at index.

    Returns ``(status_msg, launch_request)``:
      * ``status_msg`` — text to show in the status line on next redraw
        (``None`` = leave current message).
      * ``launch_request`` — if set, the outer :func:`run` loop will exit
        the fullscreen context, run it, and re-enter the main screen.
    """
    own_start, other_start, settings_idx, kill_idx, has_super = _segments(ctx)

    if index < own_start:
        if index == 0:
            if not ctx.cwd_allowed:
                return (
                    f"cwd {ctx.cwd_short} is not under allowed_roots; "
                    "cd into one of them first (use option 2/3 to create/open under "
                    f"{ctx.new_project_root})",
                    None,
                )
            dsp = _prompt_permissions(t)
            if dsp is not None:
                print(t.normal + t.clear + t.home, end="", flush=True)
                return None, ctx.on_launch_cwd(dsp)
        elif index == 1:
            name = _prompt_project_name(t, ctx.new_project_root)
            if name is not None:
                git_profile = ""
                if ctx.git_create_enabled and ctx.git_remote_profile_options:
                    git_profile = _prompt_git_profile(
                        t,
                        ctx.git_remote_profile_options,
                        ctx.default_git_remote_profile,
                    )
                    if git_profile is None:
                        return None, None
                dsp = _prompt_permissions(t)
                if dsp is not None:
                    print(t.normal + t.clear + t.home, end="", flush=True)
                    return None, ctx.on_launch_new(name, dsp, git_profile)
        elif index == 2:
            if not ctx.existing_projects:
                return t.yellow(f"No projects in {ctx.new_project_root}"), None
            name = _prompt_existing_project(t, ctx.existing_projects, ctx.new_project_root)
            if name is not None:
                dsp = _prompt_permissions(t)
                if dsp is not None:
                    print(t.normal + t.clear + t.home, end="", flush=True)
                    return None, ctx.on_launch_existing(name, dsp)
        return None, None

    if index < other_start:
        si = index - own_start
        if 0 <= si < len(ctx.sessions):
            session = ctx.sessions[si]
            print(t.normal + t.clear + t.home, end="", flush=True)
            return None, ctx.on_attach(ctx.current_user, session.name)
        return None, None

    if has_super and index < settings_idx:
        si = index - other_start
        if 0 <= si < len(ctx.other_sessions):
            session = ctx.other_sessions[si]
            print(t.normal + t.clear + t.home, end="", flush=True)
            return None, ctx.on_attach(session.user, session.name)
        return None, None

    if has_super and index == settings_idx:
        from ccw_tui_settings import SettingsCallbacks, show_settings

        cbs = SettingsCallbacks(
            get_entries=ctx.get_settings_entries,
            save_setting=ctx.on_setting_save,
            remove_setting=ctx.on_setting_remove,
            save_mapping=ctx.on_setting_save_mapping,
            get_git_remote_profile_rows=ctx.get_git_remote_profile_rows,
        )
        show_settings(t, cbs)
        return _dim(t, "Settings closed"), None

    if has_super and index == kill_idx:
        confirm_y = t.height - 3
        total_count = len(ctx.sessions) + len(ctx.other_sessions)
        # Resolve through the package namespace so tests can still
        # ``mock.patch.object(ccw_tui, "_confirm_kill_all_global")``.
        import ccw_tui as _pkg  # noqa: PLC0415 — lazy to avoid circular import
        if _pkg._confirm_kill_all_global(t, total_count, confirm_y):
            try:
                ctx.on_kill_all_global()
                return t.green(f"Killed all {total_count} sessions (all users)"), None
            except CallbackError as exc:
                return t.red(f"Kill all (global) failed: {exc}"), None
        return None, None

    return None, None


# ── Entry point ──────────────────────────────────────────────────────




def _interactive_loop(
    t: "Terminal",
    ctx: TuiContext,
    cursor: int,
    scroll_offset: int,
    status_msg: str,
) -> tuple[str, Any]:
    """Run one blessed-fullscreen pass of the main screen.

    Returns one of:
      * ``("quit", rc)`` — user asked to quit the TUI.
      * ``("launch", (req, cursor, scroll_offset, ctx))`` — user picked a
        session/project; outer loop should run ``req`` outside fullscreen
        and re-enter.
    """
    from ccw_tui_mouse import MouseEvent, hit_test, read_input
    _press_y: int | None = None
    while True:
        own_start, other_start, settings_idx, kill_idx, has_super = _segments(ctx)
        total = _total_items(ctx)
        if total > 0:
            cursor = max(0, min(cursor, total - 1))
        else:
            cursor = 0

        decor = 0
        if ctx.sessions:
            decor += 2
        if has_super:
            decor += 1
            if ctx.other_sessions:
                decor += 1
        available = max(1, t.height - 5 - decor)
        if cursor < scroll_offset:
            scroll_offset = cursor
        elif cursor >= scroll_offset + available:
            scroll_offset = cursor - available + 1

        regions = _draw_main_screen(t, ctx, cursor, scroll_offset, status_msg)
        status_msg = ""

        ev = read_input(t, timeout=None)

        if isinstance(ev, MouseEvent):
            if ev.wheel < 0:
                if cursor > 0:
                    cursor -= 1
                continue
            if ev.wheel > 0:
                if cursor < total - 1:
                    cursor += 1
                continue
            # Protocol y is 1-based; blessed rows are 0-based.
            hit = hit_test(regions, y=ev.y - 1)
            if hit is None or hit.action != "row":
                if ev.pressed:
                    _press_y = None
                continue
            idx = int(hit.payload)
            if ev.pressed:
                cursor = idx
                _press_y = ev.y
                continue
            # Release: only activate if on the same row as the press.
            if _press_y != ev.y:
                _press_y = None
                continue
            _press_y = None
            # Same gate as digit_jump: don't auto-open Settings / Kill-ALL.
            if has_super and idx in (settings_idx, kill_idx):
                status_msg = _dim(
                    t,
                    "Press Enter to open Settings / Kill-ALL (click selects only)",
                )
                continue
            try:
                msg, req = _activate_item(t, ctx, idx)
            except CallbackError as exc:
                msg, req = t.red(f"Error: {exc}"), None
            status_msg = msg or ""
            if req is not None:
                return "launch", (req, idx, scroll_offset, ctx)
            continue

        key = ev  # Keystroke — fall through to existing branches

        if key == "q" or key.name == "KEY_ESCAPE":
            return "quit", 0

        elif key.name == "KEY_UP" or key == "k":
            if cursor > 0:
                cursor -= 1

        elif key.name == "KEY_DOWN" or key == "j":
            if cursor < total - 1:
                cursor += 1

        elif key.name == "KEY_HOME" or key == "g":
            cursor = 0

        elif key.name == "KEY_END" or key == "G":
            cursor = max(0, total - 1)

        elif key.name == "KEY_ENTER" or key == "\n" or key == "\r":
            try:
                msg, req = _activate_item(t, ctx, cursor)
            except CallbackError as exc:
                msg, req = t.red(f"Error: {exc}"), None
            status_msg = msg or ""
            if req is not None:
                return "launch", (req, cursor, scroll_offset, ctx)
            # Items that come back (Settings/Kill-all) need a context refresh
            if has_super and cursor in (settings_idx, kill_idx):
                try:
                    ctx = ctx.on_refresh()
                except CallbackError as exc:
                    status_msg = t.red(f"Refresh failed: {exc}")
                total = _total_items(ctx)
                if cursor >= total:
                    cursor = max(0, total - 1)

        elif not key.is_sequence and str(key) in "123456789":
            idx = int(str(key)) - 1
            # Digit jumps route through the digit-hinted allow-list — they
            # can never auto-activate Settings or Kill-ALL. Stray keystrokes
            # must not open destructive or surprising screens. Enter on the
            # item still works for deliberate access.
            digit_allowed = _digit_hinted_indices(ctx)
            if idx in digit_allowed:
                cursor = idx
                try:
                    msg, req = _activate_item(t, ctx, cursor)
                except CallbackError as exc:
                    msg, req = t.red(f"Error: {exc}"), None
                status_msg = msg or ""
                if req is not None:
                    return "launch", (req, cursor, scroll_offset, ctx)
            elif idx < total and has_super and idx in (settings_idx, kill_idx):
                # Digit pointed at Settings or Kill-ALL. Move the cursor
                # but do NOT activate — make this explicit so users see why.
                cursor = idx
                status_msg = _dim(
                    t, "Press Enter to open Settings / Kill-ALL (digit moves cursor only)"
                )

        elif key == "d":
            confirm_y = t.height - 3
            if own_start <= cursor < other_start and ctx.sessions:
                si = cursor - own_start
                session = ctx.sessions[si]
                if _confirm_kill(t, session.name, confirm_y):
                    try:
                        ctx.on_kill(ctx.current_user, session.name)
                        status_msg = t.green(f"Killed {session.name}")
                    except CallbackError as exc:
                        status_msg = t.red(f"Kill {session.name} failed: {exc}")
                    try:
                        ctx = ctx.on_refresh()
                    except CallbackError as exc:
                        status_msg = t.red(f"Refresh failed: {exc}")
                    total = _total_items(ctx)
                    if cursor >= total:
                        cursor = max(0, total - 1)
            elif has_super and other_start <= cursor < settings_idx:
                si = cursor - other_start
                session = ctx.other_sessions[si]
                if _confirm_kill(t, session.name, confirm_y, user=session.user):
                    try:
                        ctx.on_kill(session.user, session.name)
                        status_msg = t.green(f"Killed {session.name} (user={session.user})")
                    except CallbackError as exc:
                        status_msg = t.red(f"Kill {session.name} failed: {exc}")
                    try:
                        ctx = ctx.on_refresh()
                    except CallbackError as exc:
                        status_msg = t.red(f"Refresh failed: {exc}")
                    total = _total_items(ctx)
                    if cursor >= total:
                        cursor = max(0, total - 1)

        elif key == "D":
            if ctx.sessions:
                n = len(ctx.sessions)
                confirm_y = t.height - 3
                if _confirm_kill_all(t, n, confirm_y):
                    try:
                        ctx.on_kill_all()
                        status_msg = t.green(f"Killed all {n} sessions")
                    except CallbackError as exc:
                        status_msg = t.red(f"Kill all failed: {exc}")
                    try:
                        ctx = ctx.on_refresh()
                    except CallbackError as exc:
                        status_msg = t.red(f"Refresh failed: {exc}")
                    cursor = 0
                    scroll_offset = 0

        elif key == "r":
            try:
                ctx = ctx.on_refresh()
                status_msg = _dim(t, "Refreshed")
            except CallbackError as exc:
                status_msg = t.red(f"Refresh failed: {exc}")
            total = _total_items(ctx)
            if cursor >= total:
                cursor = max(0, total - 1)


def run(ctx: TuiContext) -> int:
    """Run the interactive TUI.

    Loop: enter blessed fullscreen → run inner screen until quit or launch.
    On launch, exit fullscreen, run the LaunchRequest (fork-and-wait), then
    re-enter with a refreshed context. Cursor/scroll position survive the
    round-trip so the user lands back on the item they launched from.
    """
    try:
        from blessed import Terminal
    except ImportError:
        print(BLESSED_MISSING_HINT, file=sys.stderr)
        return 1

    import atexit
    from ccw_tui_mouse import enable as _mouse_enable, disable as _mouse_disable

    t = Terminal()
    cursor = 0
    scroll_offset = 0
    status_msg = ""

    # atexit safety net: if we crash inside fullscreen, ensure mouse
    # reporting is disabled so the user's post-crash shell doesn't see
    # CSI bytes as garbage text.
    atexit.register(_mouse_disable, t)

    caller_user = os.environ.get("SUDO_USER") or os.environ.get("USER", "")
    launch_user = ctx.current_user
    _log_event(
        "tui_start",
        caller_user=caller_user,
        launch_user=launch_user,
        screen=Screen.MAIN,
        extra={"version": ctx.version, "has_sudo": ctx.has_sudo},
    )

    try:
        while True:
            with t.fullscreen(), t.cbreak(), t.hidden_cursor():
                _mouse_enable(t)
                try:
                    outcome, payload = _interactive_loop(t, ctx, cursor, scroll_offset, status_msg)
                finally:
                    _mouse_disable(t)

            if outcome == "quit":
                _log_event(
                    "tui_quit",
                    caller_user=caller_user,
                    launch_user=launch_user,
                    screen=Screen.MAIN,
                    outcome=f"rc={int(payload)}",
                )
                return int(payload)

            # outcome == "launch"
            req, cursor, scroll_offset, ctx = payload
            _log_event(
                "launch",
                caller_user=caller_user,
                launch_user=launch_user,
                screen=Screen.MAIN,
                extra={"label": req.label, "cmd": list(req.cmd)[:2]},
            )
            sys.stdout.flush()
            rc, stage, wall_seconds = _run_launch_request(req)
            status_msg = _format_launch_status(t, req, rc, stage)
            _log_event(
                "launch_completed",
                caller_user=caller_user,
                launch_user=launch_user,
                screen=Screen.MAIN,
                outcome=f"rc={rc}",
                extra={"label": req.label, "stage": stage, "wall_seconds": round(wall_seconds, 3)},
            )
            _pause_on_launch_failure(t, req, rc, stage, wall_seconds)
            # Flush any keys the user typed while tmux was attached or
            # during the post-launch pause. Without this drain, a
            # fast-exit launch (rc=0, <1s) could hand buffered bytes to
            # the main screen's inkey() and surprise-activate an item.
            _drain_stdin(t)
            try:
                ctx = ctx.on_refresh()
            except CallbackError as exc:
                status_msg = t.red(f"Refresh failed: {exc}")
    except KeyboardInterrupt:
        _mouse_disable(t)
        return 130
    except Exception as exc:  # pragma: no cover — last-ditch guard
        # We're already outside blessed fullscreen here (context manager
        # exits on exception propagation), so writing to stderr is safe.
        _mouse_disable(t)
        print(file=sys.stderr)
        print(t.bold_red(f"ccw: interactive mode crashed: {exc}"), file=sys.stderr)
        print(_dim(t, "─" * 40), file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return 1


# ── Key-binding registry ─────────────────────────────────────────────
#
# This is a hand-curated declaration of what each screen's input
# handler is supposed to do for every key it accepts. It is NOT the
# runtime dispatch — the per-screen handlers (``_interactive_loop``,
# ``_prompt_existing_project``, ``_prompt_permissions``,
# ``_prompt_project_name``, ``_prompt_git_profile``, and
# ``ccw_tui_settings.show_settings``) still match keys inline. The
# registry exists so:
#
#   * A test (``KeymapRegistryTests``) can walk the module and verify
#     every ``key == "X"`` / ``key.name == "KEY_X"`` comparison in the
#     runtime code is declared here for the screen that owns it —
#     silent overloads like the old ``g → open git remotes`` in
#     Settings show up as an entry mismatch.
#   * Reviewers have one place to read "what does key X do on
#     screen Y".
#   * Future refactors to a true dispatch-table architecture have a
#     ground-truth to migrate against.
#
# When you add or remove a key binding in any screen handler, update
# this registry to match. The registry test will fail if they drift.


class Screen(Enum):
    MAIN = "main"
    PICKER_EXISTING = "picker-existing"
    PROMPT_NEW_PROJECT = "prompt-new-project"
    PROMPT_GIT_PROFILE = "prompt-git-profile"
    PROMPT_PERMISSIONS = "prompt-permissions"
    SETTINGS = "settings"


#: For each screen, a dict from an "action label" to the set of key
#: representations that may trigger it. Keys are either literal
#: strings (``"q"``, ``"d"``) or special names (``"KEY_UP"``,
#: ``"KEY_ENTER"``). A test verifies every key in the runtime source
#: is declared in exactly one action for its screen.
SCREEN_KEYMAP: dict[Screen, dict[str, tuple[str, ...]]] = {
    Screen.MAIN: {
        "quit": ("q", "KEY_ESCAPE"),
        "cursor_up": ("KEY_UP", "k"),
        "cursor_down": ("KEY_DOWN", "j"),
        "cursor_home": ("KEY_HOME", "g"),
        "cursor_end": ("KEY_END", "G"),
        "activate": ("KEY_ENTER", "\n", "\r"),
        "digit_jump": ("1", "2", "3", "4", "5", "6", "7", "8", "9"),
        "kill": ("d",),
        "kill_all_own": ("D",),
        "refresh": ("r",),
    },
    Screen.PICKER_EXISTING: {
        "cancel": ("KEY_ESCAPE", "q"),
        "cursor_up": ("KEY_UP", "k"),
        "cursor_down": ("KEY_DOWN", "j"),
        "cursor_home": ("KEY_HOME", "g"),
        "cursor_end": ("KEY_END", "G"),
        "activate": ("KEY_ENTER", "\n", "\r"),
        "digit_pick": ("1", "2", "3", "4", "5", "6", "7", "8", "9"),
    },
    Screen.PROMPT_PERMISSIONS: {
        "cancel": ("KEY_ESCAPE", "q"),
        "cursor_up": ("KEY_UP", "k"),
        "cursor_down": ("KEY_DOWN", "j"),
        "activate": ("KEY_ENTER", "\n", "\r"),
        "digit_regular": ("1",),
        "digit_dsp": ("2",),
    },
    Screen.PROMPT_NEW_PROJECT: {
        "cancel": ("KEY_ESCAPE",),
        "submit": ("KEY_ENTER", "\n", "\r"),
        "backspace": ("KEY_BACKSPACE", "\x7f"),
    },
    Screen.PROMPT_GIT_PROFILE: {
        "cancel": ("KEY_ESCAPE",),
        "cursor_up": ("KEY_UP", "k"),
        "cursor_down": ("KEY_DOWN", "j"),
        "activate": ("KEY_ENTER", "\n", "\r"),
        # Digit picks in this prompt: 0 = "skip", 1..N = profile choices.
        "digit_pick": ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9"),
    },
    Screen.SETTINGS: {
        "back": ("KEY_ESCAPE", "q"),
        "cursor_up": ("KEY_UP", "k"),
        "cursor_down": ("KEY_DOWN", "j"),
        "cursor_home": ("KEY_HOME",),  # Note: "g" deliberately absent — see PR 1.
        "cursor_end": ("KEY_END", "G"),
        "activate": ("KEY_ENTER", "\n", "\r"),
        "reset": ("x",),
        "digit_jump": ("1", "2", "3", "4", "5", "6", "7", "8", "9"),
    },
}


# Short display labels per action. Only actions that appear here render
# in the footer. Other actions (e.g. backspace) are intentionally hidden.
_FOOTER_LABELS: dict[str, tuple[str, str]] = {
    # action_name -> (hint_key_display, description)
    "cursor_up":    ("↑↓", "navigate"),
    "cursor_down":  ("↑↓", "navigate"),   # collapsed with cursor_up
    "digit_jump":   ("1-9", "jump"),
    "digit_pick":   ("1-9", "pick"),
    "activate":     ("Enter", "select"),
    "kill":         ("d", "kill"),
    "kill_all_own": ("D", "kill all (mine)"),
    "refresh":      ("r", "refresh"),
    "quit":         ("q", "quit"),
    "cancel":       ("Esc", "back"),
    "back":         ("Esc", "back"),
    "submit":       ("Enter", "confirm"),
    "reset":        ("x", "reset"),
    "digit_regular": ("1", "regular"),
    "digit_dsp":     ("2", "all perms"),
}

_MODE_TO_SCREEN: dict[str, Screen] = {
    "main":        Screen.MAIN,
    "projects":    Screen.PICKER_EXISTING,
    "permissions": Screen.PROMPT_PERMISSIONS,
    "input":       Screen.PROMPT_NEW_PROJECT,
    "git_profile": Screen.PROMPT_GIT_PROFILE,
    "settings":    Screen.SETTINGS,
}


def _build_footer(t: "Terminal", mode: str = "main", has_sudo: bool = False) -> str:
    """Build the footer hint line from :data:`SCREEN_KEYMAP`.

    The footer displays one pill per declared action whose name appears
    in :data:`_FOOTER_LABELS`. Duplicate pills (e.g. ``cursor_up`` /
    ``cursor_down`` both mapping to ``↑↓ navigate``) are de-duplicated.
    When ``has_sudo`` is true on the main screen, the superuser hint is
    inserted next to the refresh action so operators see it without
    scanning.
    """
    screen = _MODE_TO_SCREEN.get(mode)
    if screen is None:
        return ""
    keymap = SCREEN_KEYMAP.get(screen, {})
    seen: set[tuple[str, str]] = set()
    pills: list[str] = []
    order = [
        "cursor_up", "cursor_down", "digit_jump", "digit_pick",
        "activate", "submit", "kill", "kill_all_own",
        "refresh", "reset", "cancel", "back", "quit",
        "digit_regular", "digit_dsp",
    ]
    for action in order:
        if action not in keymap:
            continue
        label = _FOOTER_LABELS.get(action)
        if label is None:
            continue
        if label in seen:
            continue
        seen.add(label)
        k, desc = label
        # Color cues match the prior hand-rolled footer.
        if action in ("kill", "kill_all_own"):
            pills.append(f"{t.bold_red(k)} {desc}")
        else:
            pills.append(f"{t.bold(k)} {desc}")
    if mode == "main" and has_sudo:
        # Insert next to destructive actions, mirroring the old footer.
        pills.insert(
            min(len(pills), 5),
            f"{t.bold_yellow('⚡')} superuser",
        )
    return "  ".join(pills)
