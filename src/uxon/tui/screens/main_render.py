"""Pure render helpers for :class:`uxon.tui.screens.main.MainScreen`.

Free functions that build the screen's string content (action-row
label/detail text) from explicit, already-resolved inputs.

This module imports **no Textual** on purpose: the branchy "what text to
show" logic is fast-testable here, while the thin Textual-mutation shell
(``self.query_one(...).update(...)`` / ``yield Static(...)``) stays on the
Screen. Same pattern as the focus-key codec in :mod:`uxon.tui.state` and
the dashboard predicates in :mod:`uxon.tui.dashboard.ui_state`.
"""

from __future__ import annotations

from collections.abc import Iterable


def kill_all_global_detail(
    reachable_users: Iterable[str],
    scope_skipped_users: Iterable[str],
) -> str:
    """Detail line for the "Kill ALL reachable users" action row.

    Lists the reachable target users. When the per-target probe filtered
    any candidates (caller's sudoers rule covers some users in
    ``session_users`` but not all), append a ``N/M users reachable``
    hint so the operator notices a colleague is missing rather than
    silently absent.
    """
    reachable = sorted(reachable_users)
    if not reachable:
        return ""
    skipped = list(scope_skipped_users)
    total = len(reachable) + len(skipped)
    if skipped:
        return f"({', '.join(reachable)} + self · {len(reachable)}/{total} users reachable)"
    return f"({', '.join(reachable)} + self)"


def cwd_detail(
    *,
    cwd_writable: bool | None,
    cwd_short: str,
    error: str | None = None,
) -> str:
    """Detail line for the "Open in this folder" action row.

    ``None`` means checking; ``True`` requires write access through at least
    one enabled launch profile's execution backend.
    """
    if error:
        return f"({cwd_short} — access check failed: {error})"
    if cwd_writable is None:
        return f"({cwd_short} — checking access…)"
    if cwd_writable is False:
        return f"({cwd_short} — no writable launch profile)"
    return f"({cwd_short})"


def open_detail(*, loading: bool, new_project_root: str) -> str:
    """Detail line for the "Open project" action row."""
    if loading:
        return f"({new_project_root}/… — loading)"
    return f"({new_project_root}/…)"


def kill_all_global_label(total_sessions: int) -> str:
    """Label for the "Kill ALL reachable users" action row."""
    return f"⚡ Kill ALL uxon sessions (reachable users, {total_sessions} total)"


def new_project_detail(new_project_root: str) -> str:
    """Detail line for the "Create new project" action row."""
    return f"({new_project_root}/…)"
