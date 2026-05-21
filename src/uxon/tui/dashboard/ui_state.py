"""Dashboard UI state + pure reducers.

Holds the operator's view choice (``view_mode``) and substring filter
(``filter_text``). Sort is a hard contract owned by the model
selector — not part of UI state.

:class:`MainScreenUiState` is the recompose-safe owner of every
transient piece of state the main screen carries (the
:class:`DashboardUiState` above plus tab strip position and the
focus-restore flag). It is created once on the App and survives the
``apply_loaded_ctx`` recompose path that builds a fresh ``MainScreen``
on layout-signature flips.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal


@dataclass(frozen=True, slots=True)
class DashboardUiState:
    view_mode: Literal["by_host", "flat"] = "flat"
    filter_text: str = ""


@dataclass
class MainScreenUiState:
    """Mutable bag of transient UI state for the main screen.

    Owned by :class:`uxon.tui.app.UxonApp`, not by any individual
    :class:`MainScreen` instance. ``apply_loaded_ctx`` replaces the
    screen on layout-signature flips (e.g. another user starts a
    session), and three pieces of state used to die with it:
    ``view_mode``/``filter_text`` (the dashboard's UI state), the
    active host tab, and the pending tab-focus-restore flag. The App
    is stable for the whole TUI session, so storing them here makes
    them recompose-safe.

    ``seen_users`` is the monotonic accumulator behind the USER
    column's cross_user latch: once two distinct usernames have been
    observed across any combination of local + remote sources, the
    column stays mounted for the rest of the process. Filtering or
    transient remote-snapshot loss never shrinks it — the column
    would otherwise disappear under the operator while they were
    using it.
    """

    ui: DashboardUiState = field(default_factory=DashboardUiState)
    active_tab_index: int = 0
    pending_tab_focus_restore: bool = False
    seen_users: set[str] = field(default_factory=set)


def set_view_mode(
    ui: DashboardUiState,
    mode: Literal["by_host", "flat"],
) -> DashboardUiState:
    """Set ``view_mode``. Returns ``ui`` by identity on no-op."""
    if mode == ui.view_mode:
        return ui
    return replace(ui, view_mode=mode)


def set_filter(ui: DashboardUiState, text: str) -> DashboardUiState:
    """Set ``filter_text``. Returns ``ui`` by identity on no-op."""
    if text == ui.filter_text:
        return ui
    return replace(ui, filter_text=text)
