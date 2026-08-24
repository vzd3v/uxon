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
    # FleetStatusBar collapsed/expanded toggle. Recompose-safe here (not
    # a MainScreen instance attr) so the ``h`` choice survives the
    # ``apply_loaded_ctx`` → ``switch_screen`` rebuild on layout flips.
    hosts_expanded: bool = False
    seen_users: set[str] = field(default_factory=set)
    # Persisted row order (tuple of ``SessionRow.key`` strings) read and
    # rewritten by :func:`uxon.tui.dashboard.order.place`. Frozen order is
    # a UX choice (visual stability — ): existing rows keep their
    # slot across telemetry ticks, new rows are placed by recency at
    # arrival. Stored here (on the App-owned state, not a MainScreen
    # instance) so it survives the ``apply_loaded_ctx`` rebuild and a
    # background refresh never re-sorts.
    row_order: tuple[str, ...] = ()


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


# ── Pure dashboard-tick predicates ────────────────────────────────────
#
# The branchy view/tab/block-jump math the main screen handlers used to
# carry inline. Pure (no Textual): the Screen reads the live widget
# state, calls these, and applies the result back onto the widget.


def toggle_view_mode(mode: Literal["by_host", "flat"]) -> Literal["by_host", "flat"]:
    """Return the opposite view mode (``v`` toggle)."""
    return "flat" if mode == "by_host" else "by_host"


def clamp_tab_index(index: int, count: int) -> int | None:
    """Clamp ``index`` into ``[0, count)``, wrapping cyclically.

    Returns ``None`` when there is nothing to cycle (``count <= 1``) so
    the caller no-ops without mutating the strip.
    """
    if count <= 1:
        return None
    return index % count


def step_tab_index(index: int, count: int, direction: int) -> int | None:
    """Advance ``index`` by ``direction`` (±1) within ``count``, wrapping.

    Returns ``None`` when ``count <= 1`` (nothing to cycle).
    """
    if count <= 1:
        return None
    return (index + direction) % count


def active_block_index(starts: tuple[int, ...] | list[int], cursor: int) -> int:
    """Index of the block whose start is the greatest ``<= cursor``.

    ``starts`` is the ascending block-start row list; ``cursor`` is the
    current cursor row. Falls back to block ``0`` when ``cursor`` sits
    before the first start.
    """
    block_idx = 0
    for i, s in enumerate(starts):
        if s <= cursor:
            block_idx = i
        else:
            break
    return block_idx


def next_block_start(
    starts: tuple[int, ...] | list[int],
    cursor: int,
    direction: int,
) -> int | None:
    """Row index of the next/previous block start, wrapping cyclically.

    ``direction > 0`` jumps to the next block, ``< 0`` to the previous.
    Returns ``None`` when there are fewer than two blocks (nothing to
    jump between).
    """
    if len(starts) <= 1:
        return None
    block_idx = active_block_index(starts, cursor)
    new_block = (block_idx + (1 if direction > 0 else -1)) % len(starts)
    return starts[new_block]
