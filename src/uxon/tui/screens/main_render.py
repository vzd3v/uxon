"""Pure render helpers for :class:`uxon.tui.screens.main.MainScreen`.

Free functions that build the screen's string content (section headers,
action-row detail/caption text) from explicit, already-resolved inputs.

This module imports **no Textual** on purpose: the branchy "what text to
show" logic is fast-testable here, while the thin Textual-mutation shell
(``self.query_one(...).update(...)`` / ``yield Static(...)``) stays on the
Screen. Same pattern as the focus-key codec in :mod:`uxon.tui.state` and
the dashboard predicates in :mod:`uxon.tui.dashboard.ui_state`.
"""

from __future__ import annotations

from collections.abc import Iterable


def superuser_header(
    reachable_users: Iterable[str],
    scope_skipped_users: Iterable[str],
) -> str:
    """Header for the "Other users' sessions" / superuser block.

    When the per-target probe filtered any candidates (caller's sudoers
    rule covers some users in ``session_users`` but not all), append a
    ``(N/M users reachable)`` hint so the operator notices a colleague is
    missing rather than silently absent.
    """
    reachable = sorted(reachable_users)
    skipped = list(scope_skipped_users)
    total = len(reachable) + len(skipped)
    if skipped and total:
        return f"── superuser ── ({len(reachable)}/{total} users reachable)"
    return "── superuser ──"


def cwd_detail(
    *,
    cwd_writable: bool | None,
    cwd_short: str,
    launch_user: str,
    current_user: str,
) -> str:
    """Detail line for the "New session in current folder" action row.

    ``cwd_writable`` is the three-valued launchability flag (``False``
    only when a probe has resolved the cwd as not launchable for the
    launch user).
    """
    if cwd_writable is False:
        user = launch_user or current_user or "launch user"
        return f"({cwd_short} — not launchable for {user})"
    return f"({cwd_short})"


def open_detail(*, loading: bool, new_project_root: str) -> str:
    """Detail line for the "Open existing project" action row."""
    if loading:
        return f"({new_project_root}/… — loading)"
    return f"({new_project_root}/…)"


def top_actions_caption(*, cwd_short: str, new_project_root: str) -> str:
    """Single-line caption rendered under the top action buttons.

    Rebuilt on every ctx swap so the cwd / project-root detail stays
    current. Lives outside the bordered buttons because at 1/3 width per
    button there isn't room for both the label and the path.
    """
    return f"1: {cwd_short}   2 & 3: {new_project_root}/…"


def kill_all_global_label(total_sessions: int) -> str:
    """Label for the "Kill ALL reachable users" action row."""
    return f"⚡ Kill ALL uxon sessions (reachable users, {total_sessions} total)"


def new_project_detail(new_project_root: str) -> str:
    """Detail line for the "Create new project" action row."""
    return f"({new_project_root}/…)"
