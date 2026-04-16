"""Interactive TUI session picker for ccw.

Uses blessed for terminal rendering with colors, arrow-key navigation,
and inline confirmations for kill/kill-all actions.

Main screen layout:
  - Action items (new session in cwd, create project, open existing project)
  - Separator
  - Existing sessions list

Sub-screens:
  - Permission prompt (regular vs --dsp) before any launch
  - Project name input for "Create new project"
  - Project picker for "Open existing project"
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from blessed import Terminal


# ── Data ─────────────────────────────────────────────────────────────


@dataclass
class TuiSession:
    """Flattened session data for TUI rendering (decoupled from ccw internals)."""

    name: str
    short: str
    attached: bool
    pid: str
    cpu: str
    ram: str
    created: str
    last_activity: str
    cmd: str
    path: str
    user: str


@dataclass
class TuiContext:
    """Everything the TUI needs from ccw to operate."""

    sessions: list[TuiSession]
    total_cpu: str
    total_ram: str
    version: str
    cwd: str
    cwd_short: str
    new_project_root: str
    existing_projects: list[str]  # sorted dir names under new_project_root

    # Callbacks — TUI calls these, ccw provides them
    on_attach: Callable[[str], None]  # session name -> execvp (no return)
    on_kill: Callable[[str], None]  # session name -> kill
    on_kill_all: Callable[[], None]  # kill all sessions
    on_refresh: Callable[[], "TuiContext"]  # -> fresh context
    on_launch_cwd: Callable[[bool], None]  # dsp -> launch in cwd (execvp)
    on_launch_new: Callable[[str, bool], None]  # name, dsp -> create & launch (execvp)
    on_launch_existing: Callable[[str, bool], None]  # name, dsp -> launch in existing project (execvp)


# Number of action items at the top of the main list
ACTION_COUNT = 3


# ── Rendering helpers ────────────────────────────────────────────────


def _build_header(t: "Terminal", ctx: TuiContext) -> str:
    count = len(ctx.sessions)
    title = " ccw interactive "
    stats = f" {count} sessions  cpu={ctx.total_cpu}  ram={ctx.total_ram} "
    return t.bold_white_on_blue(title) + "  " + t.dim(stats)


def _build_footer(t: "Terminal", mode: str = "main") -> str:
    if mode == "main":
        keys = [
            (t.bold("↑↓"), "navigate"),
            (t.bold("1-9"), "jump"),
            (t.bold("Enter"), "select"),
            (t.bold_red("d"), "kill"),
            (t.bold_red("D"), "kill all"),
            (t.bold("r"), "refresh"),
            (t.bold("q"), "quit"),
        ]
    elif mode == "projects":
        keys = [
            (t.bold("↑↓"), "navigate"),
            (t.bold("1-9"), "jump"),
            (t.bold("Enter"), "select"),
            (t.bold("Esc"), "back"),
        ]
    elif mode == "permissions":
        keys = [
            (t.bold("1"), "regular"),
            (t.bold("2"), "all perms"),
            (t.bold("Enter"), "select"),
            (t.bold("Esc"), "back"),
        ]
    elif mode == "input":
        keys = [
            (t.bold("Enter"), "confirm"),
            (t.bold("Esc"), "cancel"),
        ]
    else:
        keys = []
    return "  ".join(f"{k} {v}" for k, v in keys)


def _render_action_row(t: "Terminal", num: int, label: str, detail: str, selected: bool) -> str:
    cursor = t.bold_cyan("▸ ") if selected else "  "
    num_str = t.dim(f"{num} ") if not selected else f"{num} "
    text = num_str + t.bold_green("+ ") + t.bold(label) + "  " + t.dim(detail)
    if selected:
        return t.reverse(t.ljust(cursor + text, t.width))
    return cursor + text


def _render_session_row(
    t: "Terminal",
    s: TuiSession,
    selected: bool,
    col_widths: dict[str, int],
    num: int = 0,
) -> str:
    """Render one session row with color coding."""
    nw = col_widths["name"]
    pw = col_widths["pid"]
    cw = col_widths["cpu"]
    rw = col_widths["ram"]
    cmw = col_widths["cmd"]

    cursor = t.bold_cyan("▸ ") if selected else "  "
    # Number hint: show for items 1-9, blank for 10+
    if 1 <= num <= 9:
        num_str = t.dim(f"{num} ") if not selected else f"{num} "
    else:
        num_str = "  "

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
                cpu_str = t.dim(f"{cpu_val:>{cw}}")
        except ValueError:
            cpu_str = t.dim(f"{cpu_val:>{cw}}")
    else:
        cpu_str = t.dim(f"{cpu_val:>{cw}}")

    ram_str = f"{s.ram:>{rw}}"
    created_str = f"{s.created:<5}"
    last_str = f"{s.last_activity:<5}"
    cmd_str = t.dim(f"{s.cmd:<{cmw}}")
    path_str = t.dim(s.path)

    row = f"{cursor}{num_str}{name_str}  {pid_str}  {cpu_str}  {ram_str}  {created_str}  {last_str}  {cmd_str}  {path_str}"

    if selected:
        return t.reverse(t.ljust(row, t.width))
    return row


def _compute_col_widths(sessions: list[TuiSession]) -> dict[str, int]:
    if not sessions:
        return {"name": 4, "pid": 3, "cpu": 3, "ram": 3, "cmd": 3}
    return {
        "name": max(4, max(len(s.short) for s in sessions)),
        "pid": max(3, max(len(s.pid) for s in sessions)),
        "cpu": max(3, max(len(s.cpu) for s in sessions)),
        "ram": max(3, max(len(s.ram) for s in sessions)),
        "cmd": max(3, max(len(s.cmd) for s in sessions)),
    }


def _render_column_header(t: "Terminal", col_widths: dict[str, int]) -> str:
    nw = col_widths["name"]
    pw = col_widths["pid"]
    cw = col_widths["cpu"]
    rw = col_widths["ram"]
    cmw = col_widths["cmd"]
    return t.dim(
        f"    {'NAME':<{nw}}    {'PID':>{pw}}  {'CPU':>{cw}}  {'RAM':>{rw}}  "
        f"{'NEW':<5}  {'LAST':<5}  {'CMD':<{cmw}}  PATH"
    )


# ── Confirmation prompts ─────────────────────────────────────────────


def _confirm_kill(t: "Terminal", session_name: str, y: int) -> bool:
    prompt = t.bold_red(f"  Kill {session_name}? ") + t.dim("y/N ")
    with t.location(0, y):
        print(t.clear_eol + prompt, end="", flush=True)
    key = t.inkey(timeout=10)
    return key.lower() == "y"


def _confirm_kill_all(t: "Terminal", count: int, y: int) -> bool:
    prompt = t.bold_red(f"  Kill ALL {count} sessions? ") + t.dim("Type 'kill-all' to confirm: ")
    with t.location(0, y):
        print(t.clear_eol + prompt, end="", flush=True)
    buf = ""
    while True:
        key = t.inkey(timeout=30)
        if not key:
            return False
        if key.name == "KEY_ENTER" or key == "\n" or key == "\r":
            return buf == "kill-all"
        if key.name == "KEY_ESCAPE":
            return False
        if key.name == "KEY_BACKSPACE" or key == "\x7f":
            if buf:
                buf = buf[:-1]
                with t.location(0, y):
                    print(t.clear_eol + prompt + buf, end="", flush=True)
            continue
        if key.is_sequence:
            continue
        buf += str(key)
        with t.location(0, y):
            print(t.clear_eol + prompt + buf, end="", flush=True)


# ── Sub-screens ──────────────────────────────────────────────────────


def _prompt_permissions(t: "Terminal") -> bool | None:
    """Show permission choice. Returns True for dsp, False for regular, None for cancel."""
    options = [
        ("Regular permissions", "default, safe mode"),
        ("ALL PERMISSIONS", "--dangerously-skip-permissions"),
    ]
    cursor = 0

    while True:
        print(t.home + t.clear, end="")
        print(t.bold_white_on_blue(" Launch permissions "))
        print(t.dim("─" * t.width))
        print()

        for i, (label, detail) in enumerate(options):
            sel = i == cursor
            prefix = t.bold_cyan("▸ ") if sel else "  "
            num_str = t.dim(f"{i + 1} ") if not sel else f"{i + 1} "
            if i == 0:
                text = num_str + t.bold(label) + "  " + t.dim(detail)
            else:
                text = num_str + t.bold_yellow(label) + "  " + t.dim_red(detail)
            line = prefix + text
            if sel:
                print(t.reverse(t.ljust(line, t.width)))
            else:
                print(line)

        footer_y = t.height - 1
        with t.location(0, footer_y):
            print(_build_footer(t, "permissions"), end="")

        key = t.inkey(timeout=None)
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
        print(t.dim("─" * t.width))
        print()
        print(f"  {t.dim('Directory:')} {root}/")
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


def _prompt_existing_project(t: "Terminal", projects: list[str], root: str) -> str | None:
    """Project picker from existing directories. Returns name or None for cancel."""
    if not projects:
        return None
    cursor = 0
    scroll_offset = 0

    while True:
        print(t.home + t.clear, end="")
        print(t.bold_white_on_blue(" Open existing project "))
        print(t.dim("─" * t.width))
        print(f"  {t.dim(root)}/")
        print()

        available = max(1, t.height - 6)
        if cursor < scroll_offset:
            scroll_offset = cursor
        elif cursor >= scroll_offset + available:
            scroll_offset = cursor - available + 1

        visible_end = min(scroll_offset + available, len(projects))
        for i in range(scroll_offset, visible_end):
            name = projects[i]
            sel = i == cursor
            prefix = t.bold_cyan("▸ ") if sel else "  "
            num = i + 1
            if 1 <= num <= 9:
                num_str = t.dim(f"{num} ") if not sel else f"{num} "
            else:
                num_str = "  "
            text = t.bold(name) if sel else name
            line = prefix + num_str + text
            if sel:
                print(t.reverse(t.ljust(line, t.width)))
            else:
                print(line)

        footer_y = t.height - 1
        with t.location(0, footer_y):
            print(_build_footer(t, "projects"), end="")

        key = t.inkey(timeout=None)
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


def _total_items(ctx: TuiContext) -> int:
    return ACTION_COUNT + len(ctx.sessions)


def _draw_main_screen(
    t: "Terminal",
    ctx: TuiContext,
    cursor: int,
    scroll_offset: int,
    status_msg: str,
) -> None:
    """Full redraw of the main TUI screen."""
    col_widths = _compute_col_widths(ctx.sessions)
    # Reserve: header(1) + separator(1) + footer separator(1) + footer(1) + status(1) = 5
    available = max(1, t.height - 5)

    print(t.home + t.clear, end="")
    print(_build_header(t, ctx))
    print(t.dim("─" * t.width))

    total = _total_items(ctx)
    visible_start = scroll_offset
    visible_end = min(scroll_offset + available, total)
    row_line = 0  # lines printed in body area

    for i in range(visible_start, visible_end):
        if i < ACTION_COUNT:
            # Action items — numbered starting from 1
            num = i + 1
            if i == 0:
                print(_render_action_row(t, num, "New session in current folder", f"({ctx.cwd_short})", i == cursor))
            elif i == 1:
                print(_render_action_row(t, num, "Create new project", f"({ctx.new_project_root}/...)", i == cursor))
            elif i == 2:
                print(_render_action_row(t, num, "Open existing project", f"({ctx.new_project_root}/...)", i == cursor))
            row_line += 1
            # Separator after last action item
            if i == ACTION_COUNT - 1 and visible_end > ACTION_COUNT:
                if row_line < available:
                    print(t.dim("  ─ sessions ─" + "─" * max(0, t.width - 14)))
                    row_line += 1
        else:
            # Session rows
            si = i - ACTION_COUNT
            if si == 0 and visible_start >= ACTION_COUNT:
                # Column header when scrolled past actions
                if row_line < available:
                    print(_render_column_header(t, col_widths))
                    row_line += 1
            elif si == 0 and visible_start < ACTION_COUNT:
                # Column header right after separator
                if row_line < available:
                    print(_render_column_header(t, col_widths))
                    row_line += 1
            s = ctx.sessions[si]
            if row_line < available:
                # Continuous numbering: actions are 1..ACTION_COUNT, sessions continue
                item_num = i + 1
                print(_render_session_row(t, s, i == cursor, col_widths, num=item_num))
                row_line += 1

    if total == 0:
        empty_y = t.height // 2
        with t.location(0, empty_y):
            print(t.dim("  No active sessions."))

    # Footer
    footer_y = t.height - 2
    with t.location(0, footer_y):
        print(t.dim("─" * t.width), end="")
    with t.location(0, footer_y + 1):
        print(_build_footer(t, "main"), end="")

    # Status line
    if status_msg:
        with t.location(0, t.height - 3):
            print(t.clear_eol + "  " + status_msg, end="")


def _activate_item(t: "Terminal", ctx: TuiContext, index: int) -> str | None:
    """Handle activation (Enter / digit) for item at index. Returns status msg or None."""
    if index < ACTION_COUNT:
        if index == 0:
            # New session in cwd
            dsp = _prompt_permissions(t)
            if dsp is not None:
                print(t.normal + t.clear + t.home, end="", flush=True)
                ctx.on_launch_cwd(dsp)
        elif index == 1:
            # Create new project
            name = _prompt_project_name(t, ctx.new_project_root)
            if name is not None:
                dsp = _prompt_permissions(t)
                if dsp is not None:
                    print(t.normal + t.clear + t.home, end="", flush=True)
                    ctx.on_launch_new(name, dsp)
        elif index == 2:
            # Open existing project
            if not ctx.existing_projects:
                return t.yellow(f"No projects in {ctx.new_project_root}")
            name = _prompt_existing_project(t, ctx.existing_projects, ctx.new_project_root)
            if name is not None:
                dsp = _prompt_permissions(t)
                if dsp is not None:
                    print(t.normal + t.clear + t.home, end="", flush=True)
                    ctx.on_launch_existing(name, dsp)
    else:
        # Attach to existing session
        si = index - ACTION_COUNT
        if 0 <= si < len(ctx.sessions):
            session = ctx.sessions[si]
            print(t.normal + t.clear + t.home, end="", flush=True)
            ctx.on_attach(session.name)
    return None


# ── Entry point ──────────────────────────────────────────────────────


def run(ctx: TuiContext) -> int:
    """Run the interactive TUI. Returns exit code."""
    try:
        from blessed import Terminal
    except ImportError:
        print("ccw: interactive mode requires 'blessed' (pip install blessed)", file=sys.stderr)
        return 1

    t = Terminal()
    cursor = 0
    scroll_offset = 0
    status_msg = ""

    with t.fullscreen(), t.cbreak(), t.hidden_cursor():
        while True:
            total = _total_items(ctx)
            if total > 0:
                cursor = max(0, min(cursor, total - 1))
            else:
                cursor = 0

            # Account for separator line + column header between actions and sessions
            extra_lines = 0
            if ctx.sessions:
                extra_lines = 2  # separator + column header

            available = max(1, t.height - 5 - extra_lines)
            if cursor < scroll_offset:
                scroll_offset = cursor
            elif cursor >= scroll_offset + available:
                scroll_offset = cursor - available + 1

            _draw_main_screen(t, ctx, cursor, scroll_offset, status_msg)
            status_msg = ""

            key = t.inkey(timeout=None)

            if key == "q" or key.name == "KEY_ESCAPE":
                return 0

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
                status_msg = _activate_item(t, ctx, cursor) or ""

            elif not key.is_sequence and str(key) in "123456789":
                idx = int(str(key)) - 1  # 0-based
                if idx < total:
                    cursor = idx
                    status_msg = _activate_item(t, ctx, cursor) or ""

            elif key == "d":
                if cursor >= ACTION_COUNT and ctx.sessions:
                    si = cursor - ACTION_COUNT
                    session = ctx.sessions[si]
                    confirm_y = t.height - 3
                    if _confirm_kill(t, session.name, confirm_y):
                        try:
                            ctx.on_kill(session.name)
                            status_msg = t.green(f"Killed {session.name}")
                        except SystemExit:
                            status_msg = t.red(f"Failed to kill {session.name}")
                        ctx = ctx.on_refresh()
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
                        except SystemExit:
                            status_msg = t.red("Failed to kill all sessions")
                        ctx = ctx.on_refresh()
                        cursor = 0
                        scroll_offset = 0

            elif key == "r":
                ctx = ctx.on_refresh()
                status_msg = t.dim("Refreshed")
                total = _total_items(ctx)
                if cursor >= total:
                    cursor = max(0, total - 1)

    return 0
