"""Custom widgets for the uxon TUI.

The textual migration leans on stock widgets wherever possible. The
custom surface is small and focused:

- :class:`ActionRow` — clickable, hoverable, focusable action row
  used on MainScreen.
- :class:`SessionListView` — the render-on-demand session list (local
  own + other-user + every remote peer) on Textual's Line API; the
  dashboard widget on MainScreen. Owns viewport-only render, the row
  cursor with edge-release navigation, block hue, zebra, and
  selection-by-key.
- :class:`GatedFooter` — stock ``Footer`` with the recompose gate
  (spec D3); mounted in place of stock ``Footer`` on MainScreen.
"""

from .action_row import ActionRow
from .fleet_status_bar import FleetStatusBar
from .gated_footer import GatedFooter
from .search_bar import FilterChanged, SearchBar
from .session_list_view import SessionListView

__all__ = [
    "ActionRow",
    "FleetStatusBar",
    "FilterChanged",
    "GatedFooter",
    "SearchBar",
    "SessionListView",
]
