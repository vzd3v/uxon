"""Custom widgets for the uxon TUI.

The textual migration leans on stock widgets wherever possible. The
custom surface is small and focused:

- :class:`ActionRow` — clickable, hoverable, focusable action row
  used on MainScreen.
- :class:`DetectedAgentsBanner` — top-of-MainScreen agent-availability
  banner.
- :class:`SessionTable` — local sessions (own / other-users).
- :class:`RemoteSessionTable` — peer-aggregated sessions for
  multi-host.

Both session tables share boundary-aware navigation and visual
defaults via :class:`FocusReleasingDataTable` (internal — not
re-exported); only the data-shape-specific column / population
logic lives in the concrete subclasses.
"""

from .action_row import ActionRow
from .detected_banner import DetectedAgentsBanner
from .remote_session_table import RemoteSessionTable
from .session_table import SessionTable

__all__ = ["ActionRow", "DetectedAgentsBanner", "RemoteSessionTable", "SessionTable"]
