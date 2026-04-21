"""Pure data structures + CallbackError for the ccw TUI.

This module imports no UI framework (no blessed, no textual). The
TUI's screens and the outer runner both depend on these types, but
non-UI callers (tests, bin/ccw context builders) can import them
without pulling in any terminal dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ccw_agents import AgentAvailability


# ── Errors ───────────────────────────────────────────────────────────


class CallbackError(Exception):
    """Raised by a TUI callback when the underlying ccw operation failed.

    The message is user-facing: the main loop renders it on the status
    line (or in the post-launch banner) in red. ``bin/ccw`` wraps every
    callback with ``_wrap_tui_callback`` so that ``fail() → SystemExit``
    paths inside ccw surface here with their stderr message intact,
    instead of killing the process silently under the fullscreen TUI.
    """


# ── Data ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LaunchRequest:
    """Describes a tmux invocation the TUI wants the outer loop to fork-and-wait.

    The TUI itself never spawns subprocesses; activation handlers return
    one of these, the main loop exits the fullscreen context, runs
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
    existing_projects: list[tuple[str, str]]  # (name, compact_mtime) under new_project_root

    # Whether ``cwd`` is under one of ``allowed_roots`` — i.e. whether
    # "New session in current folder" can actually launch. Computed by
    # ccw before constructing the context so the TUI itself stays off
    # the filesystem. When False, the row is dimmed and activation
    # shows a clear status-line hint instead of silently exiting ccw.
    cwd_allowed: bool = True

    current_user: str = ""
    has_sudo: bool = False
    other_sessions: list[TuiSession] = field(default_factory=list)  # sessions of other users

    # Multi-agent fields (Task 7+)
    enabled_agents: tuple[str, ...] = ("claude",)
    default_agent: str = "claude"
    launch_user: str = ""
    # Maps agent_id → AgentAvailability (status: "pending"|"ok"|"missing"|"timeout")
    agent_availability: dict[str, Any] = field(default_factory=dict)

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
    on_launch_cwd: Callable[[str, str], "LaunchRequest"] = (
        lambda agent_id, mode_id: LaunchRequest(cmd=("true",), label="noop-launch-cwd")
    )
    on_launch_new: Callable[[str, str, str, str], "LaunchRequest"] = (
        lambda name, agent_id, mode_id, git_profile: LaunchRequest(cmd=("true",), label="noop-launch-new")
    )
    on_launch_existing: Callable[[str, str, str], "LaunchRequest"] = (
        lambda name, agent_id, mode_id: LaunchRequest(cmd=("true",), label="noop-launch-existing")
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


# Number of action items at the top of the main list.
#
# Historically a loose module-level constant; as of PR 9 (2026-04-18)
# this is derived from :data:`_ACTION_KINDS` which is itself the
# canonical description of "what are the action rows". Keep
# ``ACTION_COUNT`` for backward compatibility with tests and for
# readability in segment arithmetic; new code should use
# :func:`build_items` / item.kind dispatch instead of raw indices.
_ACTION_KINDS: tuple[str, ...] = ("action-cwd", "action-new", "action-open")
ACTION_COUNT = len(_ACTION_KINDS)


@dataclass(frozen=True)
class Item:
    """One row on the main TUI screen, described by identity rather than index.

    ``kind`` names the semantic role of the row. ``digit_hint`` is the
    digit that would activate it on keypress, or None if the row cannot
    be reached by a digit jump (Settings, Kill-ALL).

    This is the type-safe replacement for the integer-cursor scheme.
    Activation should dispatch on ``kind``, not on the position of the
    row inside the flat list — session-count changes shift every index
    below the new/removed session, but ``kind`` is stable.
    """

    kind: str  # one of: action-cwd, action-new, action-open,
    #                    own-session, other-session,
    #                    settings, kill-all-global
    label: str
    enabled: bool = True
    # Payloads (only one is populated per kind):
    session: "TuiSession | None" = None
    digit_hint: "int | None" = None  # 1..9, or None if not digit-reachable


def build_items(ctx: "TuiContext") -> list[Item]:
    """Materialise the main-screen row list as a typed list of :class:`Item`.

    The returned list is the source of truth for what's on the main
    screen. Its order is the same order the current renderer uses, so
    integer indices computed by :func:`_segments` align 1:1 with
    positions in this list.

    Invariant: item identity (kind + label) is stable under session
    count changes for the action rows and for Settings / Kill-ALL;
    session rows have identity "own-session:<name>" / "other-session:
    <user>/<name>" so their position shifts but the item that used to
    be "Open existing project" remains the same Item.
    """
    items: list[Item] = []
    # Actions (indices 0..ACTION_COUNT-1). Digit hints are 1..3.
    items.append(Item(
        kind="action-cwd",
        label="New session in current folder",
        enabled=ctx.cwd_allowed,
        digit_hint=1,
    ))
    items.append(Item(
        kind="action-new",
        label="Create new project",
        enabled=True,
        digit_hint=2,
    ))
    items.append(Item(
        kind="action-open",
        label="Open existing project",
        enabled=bool(ctx.existing_projects),
        digit_hint=3,
    ))
    # Own sessions
    for i, s in enumerate(ctx.sessions):
        pos = ACTION_COUNT + i  # 0-based position in the final list
        hint = pos + 1 if 1 <= pos + 1 <= 9 else None
        items.append(Item(
            kind="own-session",
            label=s.short,
            enabled=True,
            session=s,
            digit_hint=hint,
        ))
    # Superuser block: other-user sessions, settings, kill-all-global.
    if ctx.has_sudo:
        for i, s in enumerate(ctx.other_sessions):
            pos = ACTION_COUNT + len(ctx.sessions) + i
            hint = pos + 1 if 1 <= pos + 1 <= 9 else None
            items.append(Item(
                kind="other-session",
                label=f"{s.user}/{s.short}",
                enabled=True,
                session=s,
                digit_hint=hint,
            ))
        # Settings: no digit_hint — PR 2 invariant.
        items.append(Item(
            kind="settings",
            label="⚙ Settings",
            enabled=True,
        ))
        total_sessions = len(ctx.sessions) + len(ctx.other_sessions)
        if total_sessions > 0:
            # Kill-ALL: no digit_hint — PR 2 invariant.
            items.append(Item(
                kind="kill-all-global",
                label=f"⚡ Kill ALL ({total_sessions})",
                enabled=True,
            ))
    return items


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
