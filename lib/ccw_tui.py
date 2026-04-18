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
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

from ccw_tui_widgets import confirm_phrase as _confirm_phrase_widget
from ccw_tui_widgets import dim as _dim_widget

if TYPE_CHECKING:
    from blessed import Terminal


# ── Errors ───────────────────────────────────────────────────────────


class CallbackError(Exception):
    """Raised by a TUI callback when the underlying ccw operation failed.

    The message is user-facing: the main loop renders it on the status
    line (or in the post-launch banner) in red. ``bin/ccw`` wraps every
    callback with ``_wrap_tui_callback`` so that ``fail() → SystemExit``
    paths inside ccw surface here with their stderr message intact,
    instead of killing the process silently under blessed's fullscreen.
    """


# ── Structured event log ─────────────────────────────────────────────
#
# Every user-visible transition in the TUI writes one JSON line to
# ``/srv/work/logs/ccw/tui-{launch_user}-YYYYMMDD.log``. Format is
# newline-delimited JSON; each line is self-describing. Log writes are
# best-effort — a failure here must NEVER crash the TUI or propagate
# into the blessed fullscreen context.
#
# Fields (all optional except ``ts`` and ``event``):
#   ts              ISO-8601 UTC timestamp with seconds precision
#   caller_user     real caller username (``os.getlogin()`` or $USER)
#   launch_user     effective launch user (may differ under sudo)
#   screen          the :class:`Screen` the event originated from
#   event           short event name (``key``, ``activate``, ``launch``,
#                    ``launch_completed``, ``refresh``, …)
#   action          mapped action name from SCREEN_KEYMAP, when known
#   key             raw key representation, for ``event == "key"``
#   item_kind       kind of item activated, for ``event == "activate"``
#   outcome         terminal outcome string (``ok``, ``cancel``,
#                    ``rc=5``, ``error:<msg>``)
#   extra           free-form dict for event-specific fields

LOG_DIR = "/srv/work/logs/ccw"


def _log_dir() -> str:
    """Return the log directory, honouring an env-var override for tests."""
    return os.environ.get("CCW_LOG_DIR", LOG_DIR)


def _log_event(
    event: str,
    *,
    screen: "Screen | None" = None,
    caller_user: str = "",
    launch_user: str = "",
    action: str = "",
    key: str = "",
    item_kind: str = "",
    outcome: str = "",
    extra: "dict[str, Any] | None" = None,
) -> None:
    """Append one JSON line to today's ccw TUI log.

    Silent on failure — logging must NEVER break the TUI. A missing
    directory is created on the first call. Permission / write errors
    are swallowed.
    """
    try:
        import datetime
        import json

        now = datetime.datetime.now(datetime.timezone.utc)
        record: dict[str, Any] = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": event,
        }
        if screen is not None:
            record["screen"] = screen.value
        if caller_user:
            record["caller_user"] = caller_user
        if launch_user:
            record["launch_user"] = launch_user
        if action:
            record["action"] = action
        if key:
            record["key"] = key
        if item_kind:
            record["item_kind"] = item_kind
        if outcome:
            record["outcome"] = outcome
        if extra:
            record["extra"] = extra

        log_dir = _log_dir()
        try:
            os.makedirs(log_dir, mode=0o2775, exist_ok=True)
        except OSError:
            # Directory creation may fail (parent missing, no perm).
            # Try to write anyway — the open() below will raise and we
            # swallow that too.
            pass

        user = launch_user or caller_user or "unknown"
        date_str = now.strftime("%Y%m%d")
        path = os.path.join(log_dir, f"tui-{user}-{date_str}.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # Any exception during logging is swallowed. Logging is telemetry,
        # not a correctness path.
        return


# ── Data ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LaunchRequest:
    """Describes a tmux invocation the TUI wants the outer loop to fork-and-wait.

    The TUI itself never spawns subprocesses; activation handlers return
    one of these, the main loop exits blessed's fullscreen context, runs
    the ``prelaunch`` commands and then ``cmd``, waits for exit, and
    re-enters the main screen with a refreshed context.
    """

    cmd: tuple[str, ...]
    prelaunch: tuple[tuple[str, ...], ...] = ()
    label: str = ""


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

    sessions: list[TuiSession]  # sessions owned by current_user
    total_cpu: str
    total_ram: str
    version: str
    cwd: str
    cwd_short: str
    new_project_root: str
    existing_projects: list[str]  # sorted dir names under new_project_root

    # Whether ``cwd`` is under one of ``allowed_roots`` — i.e. whether
    # "New session in current folder" can actually launch. Computed by
    # ccw before constructing the context so the TUI itself stays off
    # the filesystem. When False, the row is dimmed and activation
    # shows a clear status-line hint instead of silently exiting ccw.
    cwd_allowed: bool = True

    current_user: str = ""
    has_sudo: bool = False
    other_sessions: list[TuiSession] = field(default_factory=list)  # sessions of other users

    # Callbacks — TUI calls these, ccw provides them.
    # Launch/attach callbacks return a LaunchRequest; the outer run() loop
    # runs the command and re-enters the TUI main screen on exit.
    on_attach: Callable[[str, str], "LaunchRequest"] = (
        lambda user, name: LaunchRequest(cmd=("true",), label="noop-attach")
    )
    on_kill: Callable[[str, str], None] = lambda user, name: None  # (user, session) -> kill
    on_kill_all: Callable[[], None] = lambda: None  # kill all own sessions
    on_kill_all_global: Callable[[], None] = lambda: None  # kill all sessions across users
    on_refresh: Callable[[], "TuiContext"] = lambda: None  # type: ignore[return-value]
    on_launch_cwd: Callable[[bool], "LaunchRequest"] = (
        lambda dsp: LaunchRequest(cmd=("true",), label="noop-launch-cwd")
    )
    on_launch_new: Callable[[str, bool, str], "LaunchRequest"] = (
        lambda name, dsp, git_profile: LaunchRequest(cmd=("true",), label="noop-launch-new")
    )
    on_launch_existing: Callable[[str, bool], "LaunchRequest"] = (
        lambda name, dsp: LaunchRequest(cmd=("true",), label="noop-launch-existing")
    )

    # Git remote on new project — display only. The TUI never edits these.
    git_create_enabled: bool = False
    default_git_remote_profile: str = ""
    # Each entry: (profile_name, description string like "github.com/vzd3v via remdepl [gh]")
    git_remote_profile_options: list[tuple[str, str]] = field(default_factory=list)

    # Settings (superuser-only). The TUI delegates all file I/O through these.
    get_settings_entries: Callable[[], list] = lambda: []
    on_setting_save: Callable[[str, Any], None] = lambda key, value: None
    on_setting_remove: Callable[[str], None] = lambda key: None
    on_setting_save_mapping: Callable[[str, dict], None] = lambda key, mapping: None
    get_git_remote_profile_rows: Callable[[], list] = lambda: []


# Number of action items at the top of the main list
ACTION_COUNT = 3


def _dim(t: "Terminal", text: str) -> str:
    """Kept as a local alias for backwards compatibility with existing tests."""
    return _dim_widget(t, text)


# ── Segment / index map ─────────────────────────────────────────────
#
# Without sudo:
#   [actions: 0..ACTION_COUNT) | [own: ACTION_COUNT..ACTION_COUNT+len(own))
#
# With sudo (superuser block always available):
#   ... | [own] | [other-user sessions] | ⚙ Settings | [Kill ALL (all users)]
#                                                     ^ only when any session exists


def _segments(ctx: TuiContext) -> tuple[int, int, int, int, bool]:
    """Return (own_start, other_start, settings_idx, kill_global_idx, has_super).

    Indexes that don't apply return -1. ``has_super`` is True iff
    ``ctx.has_sudo``.
    """
    own_start = ACTION_COUNT
    other_start = own_start + len(ctx.sessions)
    if not ctx.has_sudo:
        return own_start, other_start, -1, -1, False
    settings_idx = other_start + len(ctx.other_sessions)
    total_sessions = len(ctx.sessions) + len(ctx.other_sessions)
    kill_global_idx = settings_idx + 1 if total_sessions > 0 else -1
    return own_start, other_start, settings_idx, kill_global_idx, True


def _total_items(ctx: TuiContext) -> int:
    _, _, settings_idx, kill_idx, has_super = _segments(ctx)
    if not has_super:
        return ACTION_COUNT + len(ctx.sessions)
    if kill_idx >= 0:
        return kill_idx + 1
    return settings_idx + 1


def _digit_hinted_indices(ctx: TuiContext) -> set[int]:
    """Return the set of item indices reachable via a digit keypress.

    Digit 1..9 maps to index 0..8. Only items whose index is in this set
    may be activated by a digit keypress. Settings and Kill-ALL are
    deliberately excluded — they are non-destructive-to-read but
    surprising-to-land-on for a new user, and on empty superuser state
    `settings_idx` collapses to `ACTION_COUNT` which makes a mis-typed
    digit dangerously ambiguous. Both remain reachable via
    arrow-down + Enter, which is a deliberate two-step gesture.
    """
    own_start, other_start, settings_idx, kill_idx, has_super = _segments(ctx)
    total = _total_items(ctx)
    allowed: set[int] = set()
    # Actions (0..ACTION_COUNT-1)
    for i in range(min(ACTION_COUNT, total)):
        allowed.add(i)
    # Own sessions
    for i in range(own_start, min(other_start, total)):
        allowed.add(i)
    # Other users' sessions (still session rows, safe to jump to)
    if has_super:
        other_end = settings_idx if settings_idx >= 0 else total
        for i in range(other_start, min(other_end, total)):
            allowed.add(i)
    # Settings and Kill-ALL are intentionally excluded.
    return allowed


# ── Rendering helpers ────────────────────────────────────────────────


def _build_header(t: "Terminal", ctx: TuiContext) -> str:
    count = len(ctx.sessions) + len(ctx.other_sessions)
    title = " ccw interactive "
    stats = f" {count} sessions  cpu={ctx.total_cpu}  ram={ctx.total_ram} "
    if ctx.has_sudo:
        stats += " ⚡superuser "
    return t.bold_white_on_blue(title) + "  " + _dim(t, stats)


def _build_footer(t: "Terminal", mode: str = "main", has_sudo: bool = False) -> str:
    if mode == "main":
        keys = [
            (t.bold("↑↓"), "navigate"),
            (t.bold("1-9"), "jump"),
            (t.bold("Enter"), "select"),
            (t.bold_red("d"), "kill"),
            (t.bold_red("D"), "kill all (mine)"),
            (t.bold("r"), "refresh"),
            (t.bold("q"), "quit"),
        ]
        if has_sudo:
            keys.insert(5, (t.bold_yellow("⚡"), "superuser"))
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
        footer_y = t.height - 1
        with t.location(0, footer_y):
            print(_build_footer(t, "permissions"), end="")

        key = t.inkey(timeout=None)
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


def _draw_main_screen(
    t: "Terminal",
    ctx: TuiContext,
    cursor: int,
    scroll_offset: int,
    status_msg: str,
) -> None:
    """Full redraw of the main TUI screen."""
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

    def _print(line: str) -> None:
        nonlocal row_line
        if row_line < available:
            print(line)
            row_line += 1

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
        if _confirm_kill_all_global(t, total_count, confirm_y):
            try:
                ctx.on_kill_all_global()
                return t.green(f"Killed all {total_count} sessions (all users)"), None
            except CallbackError as exc:
                return t.red(f"Kill all (global) failed: {exc}"), None
        return None, None

    return None, None


# ── Entry point ──────────────────────────────────────────────────────


def _run_launch_request(req: "LaunchRequest") -> tuple[int, str, float]:
    """Execute a LaunchRequest via fork-and-wait (blessed context already exited).

    Runs each prelaunch command in order, aborting if any returns non-zero,
    then runs the main ``cmd``. Returns ``(rc, stage, wall_seconds)`` where
    ``stage`` is ``"prelaunch"`` if a prelaunch failed, else ``"cmd"``, and
    ``wall_seconds`` is the total elapsed time across prelaunch + cmd.
    Wall time is used by the caller to detect silent fast-exit launches
    (rc=0 but sub-second duration) that would otherwise strip the user
    of any context about why tmux didn't stick.
    """
    import time as _time

    t0 = _time.monotonic()
    for pre in req.prelaunch:
        rc = subprocess.call(list(pre))
        if rc != 0:
            return rc, "prelaunch", _time.monotonic() - t0
    rc = subprocess.call(list(req.cmd))
    return rc, "cmd", _time.monotonic() - t0


def _format_launch_status(t: "Terminal", req: "LaunchRequest", rc: int, stage: str) -> str:
    """Render a short status-line message about a returned-from-tmux launch."""
    label = req.label or "launch"
    if stage == "prelaunch":
        return t.red(f"{label}: prelaunch failed (rc={rc})")
    if rc == 0:
        return ""
    if rc == 130:
        return _dim(t, f"{label}: cancelled")
    return t.yellow(f"{label}: exited rc={rc}")


def _drain_stdin(t: "Terminal", max_keys: int = 64) -> int:
    """Read-and-discard any buffered keystrokes on the TTY.

    Called after a launch round-trip returns and before the TUI re-enters
    fullscreen. blessed's ``t.cbreak()`` does not flush pending bytes on
    entry, so keys typed while tmux was running (or during the split
    second after ``_pause_on_launch_failure``) would otherwise be
    consumed by the next screen's ``t.inkey()`` — re-animating a stale
    cursor. Bounded at ``max_keys`` to dodge a pathological "stdin is a
    pipe of infinite bytes" scenario.

    Returns the number of keys drained (for testability / logging).
    """
    drained = 0
    try:
        with t.cbreak():
            while drained < max_keys:
                key = t.inkey(timeout=0)
                if not key:
                    break
                drained += 1
    except Exception:
        # Drain is best-effort. A broken tty must not crash the TUI.
        return drained
    return drained


#: Threshold below which an rc=0 launch is treated as a silent fast-exit.
#: Empirically a healthy tmux attach that actually landed the user in a
#: claude session will be in the foreground for at least a few seconds;
#: anything sub-second that returns rc=0 is almost certainly a broken
#: command (missing binary, bad argv) that printed to stderr and exited
#: before the user could read it.
FAST_EXIT_THRESHOLD_SEC = 1.0


def _pause_on_launch_failure(
    t: "Terminal",
    req: "LaunchRequest",
    rc: int,
    stage: str,
    wall_seconds: float | None = None,
) -> None:
    """Hold the terminal after a failed launch so the user can read stderr.

    Called after the blessed fullscreen context has exited and before we
    re-enter it. The failed subprocess's stderr is still on the physical
    terminal at this point; without a pause, re-entering fullscreen wipes
    it. We print a clear banner pointing at the output above and wait for
    a keypress. ``rc == 130`` (user Ctrl-C'd) is not treated as a failure.

    When ``wall_seconds`` is provided and the launch returned rc=0 in
    under :data:`FAST_EXIT_THRESHOLD_SEC`, also pause — a near-instant
    zero exit is almost always a silent launch failure (e.g. claude
    binary missing, bad tmux argv) and the user deserves to see any
    output that was printed before fullscreen wipes it.
    """
    fast_zero = (
        rc == 0
        and wall_seconds is not None
        and wall_seconds < FAST_EXIT_THRESHOLD_SEC
    )
    if rc == 130:
        return
    if rc == 0 and not fast_zero:
        return
    label = req.label or "launch"
    sys.stdout.write("\n")
    if fast_zero:
        sys.stdout.write(
            t.bold_yellow(
                f"ccw: {label} exited immediately (rc=0 in {wall_seconds:.2f}s, stage={stage})"
            )
            + "\n"
        )
    else:
        sys.stdout.write(t.bold_red(f"ccw: {label} failed (rc={rc}, stage={stage})") + "\n")
    if stage == "prelaunch":
        sys.stdout.write(
            _dim(t, f"  command: {' '.join(list(req.prelaunch[0]) if req.prelaunch else [])}") + "\n"
        )
    else:
        sys.stdout.write(_dim(t, f"  command: {' '.join(req.cmd)}") + "\n")
    sys.stdout.write(_dim(t, "  see output above for details") + "\n")
    sys.stdout.write(t.bold("press any key to return to the ccw menu...") + "\n")
    sys.stdout.flush()
    with t.cbreak():
        t.inkey(timeout=None)


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

        _draw_main_screen(t, ctx, cursor, scroll_offset, status_msg)
        status_msg = ""

        key = t.inkey(timeout=None)

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
        print("ccw: interactive mode requires 'blessed' (pip install blessed)", file=sys.stderr)
        return 1

    t = Terminal()
    cursor = 0
    scroll_offset = 0
    status_msg = ""

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
                outcome, payload = _interactive_loop(t, ctx, cursor, scroll_offset, status_msg)

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
        return 130
    except Exception as exc:  # pragma: no cover — last-ditch guard
        # We're already outside blessed fullscreen here (context manager
        # exits on exception propagation), so writing to stderr is safe.
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
