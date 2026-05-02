"""Custom widgets for the uxon TUI.

The textual migration leans on stock widgets wherever possible — the
only two custom widgets are :class:`ActionRow` (clickable, hoverable,
focusable action row used on MainScreen) and :class:`SessionTable`
(``DataTable`` subclass that owns the populate/colour logic for own
sessions and other-user sessions).
"""

from .action_row import ActionRow
from .detected_banner import DetectedAgentsBanner
from .session_table import SessionTable

__all__ = ["ActionRow", "DetectedAgentsBanner", "SessionTable"]
