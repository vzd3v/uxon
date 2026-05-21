"""Monotonic ``seen_users`` accumulator and its accessors.

The USER column on the main dashboard is gated by a *latched* predicate:
once two distinct usernames have been observed anywhere across the
three model sources (``state.main.sessions``,
``state.main.other_sessions``, ``state.remote[*].snapshot.sessions``),
the column stays mounted for the rest of the process. Auto-shrinking
the latch when a user's sessions die — or when the operator filters
the table — would hide a column they are actively using, which is
the broken behaviour this latch exists to prevent.

Two helpers live here:

* :func:`collect_user_set` — pure, walks a :class:`TuiState` snapshot
  and returns the deduped, non-empty usernames present in it.
* :func:`cross_user_latched` — reads the monotonic accumulator on
  :class:`MainScreenUiState` and returns the layout-decision bool.

The accumulator itself is stored on ``MainScreenUiState.seen_users``
(see :mod:`uxon.tui.dashboard.ui_state`) so it survives the
``apply_loaded_ctx`` recompose path on the same App lifetime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..tui_state import TuiState
    from .ui_state import MainScreenUiState


def collect_user_set(state: TuiState) -> frozenset[str]:
    """Return the deduped, non-empty usernames in ``state``.

    Walks the three sources the dashboard model folds together:

    1. ``state.main.sessions`` — local rows owned by the launch user.
    2. ``state.main.other_sessions`` — local rows from other users
       reachable via the sudo probe.
    3. ``state.remote[*].snapshot.sessions`` — per-host wire records
       from the remote collector.

    Empty / falsy usernames are skipped: an unparsed peer record with
    a missing ``user`` field must not auto-flip the cross_user latch.
    Tolerates ``state.main is None`` (never-loaded sentinel) and
    ``slot.value is None`` (host probed but no landing yet).
    """
    out: set[str] = set()
    if state.main is not None:
        for s in state.main.sessions:
            if s.user:
                out.add(s.user)
        for s in state.main.other_sessions:
            if s.user:
                out.add(s.user)
    for slot in state.remote.values():
        snap = slot.value
        if snap is None:
            continue
        for rec in snap.sessions:
            user = rec.get("user") if isinstance(rec, dict) else None
            if user:
                out.add(str(user))
    return frozenset(out)


def cross_user_latched(ui: MainScreenUiState) -> bool:
    """Return ``True`` iff at least two distinct users have ever been
    seen in this process.

    Reads :attr:`MainScreenUiState.seen_users` — the monotonic set fed
    by the rebuild dispatcher on every state mutation. Once the latch
    flips True it stays True for the App's lifetime; the column does
    not auto-hide.
    """
    return len(ui.seen_users) > 1
