"""Custom widgets for the uxon TUI.

The textual migration leans on stock widgets wherever possible. The
custom surface is small and focused:

- :class:`ActionRow` — clickable, hoverable, focusable action row
  used on MainScreen.
- :class:`DetectedAgentsBanner` — top-of-MainScreen agent-availability
  banner.
- :class:`SessionDashboardTable` — the unified session table (local
  own + other-user + every remote peer) used on MainScreen.

Boundary-aware navigation and visual defaults come from
:class:`FocusReleasingDataTable` (internal — not re-exported).
"""

from .action_row import ActionRow
from .detected_banner import DetectedAgentsBanner
from .search_bar import FilterChanged, SearchBar
from .session_dashboard_table import SessionDashboardTable

__all__ = [
    "ActionRow",
    "DetectedAgentsBanner",
    "FilterChanged",
    "SearchBar",
    "SessionDashboardTable",
]
