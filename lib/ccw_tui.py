"""Interactive TUI session picker for ccw.

Uses blessed for terminal rendering with colors, arrow-key navigation,
and inline confirmations for kill/kill-all actions.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blessed import Terminal


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
    # Callbacks — TUI calls these, ccw provides them
    on_attach: object  # Callable[[str], NoReturn]  (session name -> execvp)
    on_kill: object  # Callable[[str], None]  (session name -> kill)
    on_kill_all: object  # Callable[[], None]  () -> kill all
    on_refresh: object  # Callable[[], TuiContext]  () -> fresh context


# ── Rendering ────────────────────────────────────────────────────────


def _build_header(t: Terminal, ctx: TuiContext) -> str:
    count = len(ctx.sessions)
    title = f" ccw interactive "
    stats = f" {count} sessions  cpu={ctx.total_cpu}  ram={ctx.total_ram} "
    return t.bold_white_on_blue(title) + t.blue_on_black("") + "  " + t.dim(stats)


def _build_footer(t: Terminal) -> str:
    keys = [
        (t.bold("↑↓"), "navigate"),
        (t.bold("Enter"), "attach"),
        (t.bold_red("d"), "kill"),
        (t.bold_red("D"), "kill all"),
        (t.bold("r"), "refresh"),
        (t.bold("q"), "quit"),
    ]
    return "  ".join(f"{k} {v}" for k, v in keys)


def _render_session_row(
    t: Terminal,
    s: TuiSession,
    selected: bool,
    col_widths: dict[str, int],
) -> str:
    """Render one session row with color coding."""
    nw = col_widths["name"]
    pw = col_widths["pid"]
    cw = col_widths["cpu"]
    rw = col_widths["ram"]
    cmw = col_widths["cmd"]

    if selected:
        cursor = t.bold_cyan("▸ ")
    else:
        cursor = "  "

    # Name: bold if attached
    if s.attached:
        name_str = t.bold_green(f"{s.short:<{nw}}") + t.green(" ●")
    else:
        name_str = f"{s.short:<{nw}}" + "  "

    # PID
    pid_str = f"{s.pid:>{pw}}"

    # CPU: color by load
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

    # RAM
    ram_str = f"{s.ram:>{rw}}"

    # Times
    created_str = f"{s.created:<5}"
    last_str = f"{s.last_activity:<5}"

    # CMD
    cmd_str = t.dim(f"{s.cmd:<{cmw}}")

    # Path — truncate to fit
    path_str = t.dim(s.path)

    row = f"{cursor}{name_str}  {pid_str}  {cpu_str}  {ram_str}  {created_str}  {last_str}  {cmd_str}  {path_str}"

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


def _render_column_header(t: Terminal, col_widths: dict[str, int]) -> str:
    nw = col_widths["name"]
    pw = col_widths["pid"]
    cw = col_widths["cpu"]
    rw = col_widths["ram"]
    cmw = col_widths["cmd"]
    return t.dim(
        f"  {'NAME':<{nw}}    {'PID':>{pw}}  {'CPU':>{cw}}  {'RAM':>{rw}}  "
        f"{'NEW':<5}  {'LAST':<5}  {'CMD':<{cmw}}  PATH"
    )


# ── Confirmation prompts ─────────────────────────────────────────────


def _confirm_kill(t: Terminal, session_name: str, y: int) -> bool:
    """Inline kill confirmation at bottom of screen. Returns True if confirmed."""
    prompt = t.bold_red(f"  Kill {session_name}? ") + t.dim("y/N ")
    with t.location(0, y):
        print(t.clear_eol + prompt, end="", flush=True)
    key = t.inkey(timeout=10)
    return key.lower() == "y"


def _confirm_kill_all(t: Terminal, count: int, y: int) -> bool:
    """Inline kill-all confirmation. Requires typing 'kill-all'."""
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


# ── Main loop ────────────────────────────────────────────────────────


def _draw_screen(
    t: Terminal,
    ctx: TuiContext,
    cursor: int,
    scroll_offset: int,
    status_msg: str,
) -> None:
    """Full redraw of the TUI screen."""
    col_widths = _compute_col_widths(ctx.sessions)

    # Available rows for session list (header=2, column header=1, footer=2, status=1)
    available = t.height - 6

    print(t.home + t.clear, end="")
    print(_build_header(t, ctx))
    print(t.dim("─" * t.width))
    print(_render_column_header(t, col_widths))

    if not ctx.sessions:
        empty_y = t.height // 2
        with t.location(0, empty_y):
            msg = "  No active sessions."
            print(t.dim(msg))
        with t.location(0, empty_y + 1):
            print(t.dim("  Use 'ccw run' or 'ccw new <name>' to create one."))
    else:
        visible_end = min(scroll_offset + available, len(ctx.sessions))
        for i in range(scroll_offset, visible_end):
            s = ctx.sessions[i]
            print(_render_session_row(t, s, i == cursor, col_widths))

    # Footer
    footer_y = t.height - 2
    with t.location(0, footer_y):
        print(t.dim("─" * t.width), end="")
    with t.location(0, footer_y + 1):
        print(_build_footer(t), end="")

    # Status line
    if status_msg:
        with t.location(0, t.height - 3):
            print(t.clear_eol + "  " + status_msg, end="")


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
            # Clamp cursor
            n = len(ctx.sessions)
            if n > 0:
                cursor = max(0, min(cursor, n - 1))
            else:
                cursor = 0

            # Adjust scroll
            available = max(1, t.height - 6)
            if cursor < scroll_offset:
                scroll_offset = cursor
            elif cursor >= scroll_offset + available:
                scroll_offset = cursor - available + 1

            _draw_screen(t, ctx, cursor, scroll_offset, status_msg)
            status_msg = ""

            key = t.inkey(timeout=None)

            if key == "q" or key.name == "KEY_ESCAPE":
                return 0

            elif key.name == "KEY_UP" or key == "k":
                if cursor > 0:
                    cursor -= 1

            elif key.name == "KEY_DOWN" or key == "j":
                if cursor < n - 1:
                    cursor += 1

            elif key.name == "KEY_HOME" or key == "g":
                cursor = 0

            elif key.name == "KEY_END" or key == "G":
                cursor = max(0, n - 1)

            elif key.name == "KEY_ENTER" or key == "\n" or key == "\r":
                if n > 0:
                    session = ctx.sessions[cursor]
                    # on_attach does execvp — won't return
                    # Restore terminal first
                    print(t.normal + t.clear + t.home, end="", flush=True)
                    ctx.on_attach(session.name)

            elif key == "d":
                if n > 0:
                    session = ctx.sessions[cursor]
                    confirm_y = t.height - 3
                    if _confirm_kill(t, session.name, confirm_y):
                        try:
                            ctx.on_kill(session.name)
                            status_msg = t.green(f"  Killed {session.name}")
                        except SystemExit:
                            status_msg = t.red(f"  Failed to kill {session.name}")
                        ctx = ctx.on_refresh()
                        n = len(ctx.sessions)
                        if cursor >= n:
                            cursor = max(0, n - 1)

            elif key == "D":
                if n > 0:
                    confirm_y = t.height - 3
                    if _confirm_kill_all(t, n, confirm_y):
                        try:
                            ctx.on_kill_all()
                            status_msg = t.green(f"  Killed all {n} sessions")
                        except SystemExit:
                            status_msg = t.red("  Failed to kill all sessions")
                        ctx = ctx.on_refresh()
                        cursor = 0
                        scroll_offset = 0

            elif key == "r":
                ctx = ctx.on_refresh()
                status_msg = t.dim("  Refreshed")
                n = len(ctx.sessions)
                if cursor >= n:
                    cursor = max(0, n - 1)

    return 0
