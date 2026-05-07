"""Dashboard UI state + pure reducers.

Holds the operator's view choice (``view_mode``) and substring filter
(``filter_text``). Sort is a hard contract owned by the model
selector — not part of UI state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal


@dataclass(frozen=True, slots=True)
class DashboardUiState:
    view_mode: Literal["by_host", "flat"] = "by_host"
    filter_text: str = ""


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
