"""Shared base for uxon's session-listing DataTables.

:class:`SessionDashboardTable` is the only consumer today. The base
exists so future row-listing tables (e.g. peer health, audit
inspector) inherit the navigation contract without re-deriving it:
the table sits between the action rows above and other focusable
widgets below, and *releases* focus to the surrounding focus chain
when the cursor would otherwise hit a row boundary. Without that,
``↑`` on row 0 and ``↓`` on the last row leave the user trapped
inside the table — the bug that made this base necessary.

This base owns three concerns:

1. **Boundary-aware navigation.** ``action_cursor_up`` /
   ``action_cursor_down`` delegate to ``app.action_focus_previous`` /
   ``action_focus_next`` at the edges. In the middle of the list the
   stock DataTable behaviour wins (``super()`` call).
2. **Cursor visibility on focus.** The row cursor is hidden
   (``cursor_type = "none"``) until the table actually receives focus,
   so the first row doesn't look pre-selected while the user is
   navigating sibling widgets.
3. **Visual baseline.** Width/height/min-height and the
   ``--hover`` highlight live here as a single source of truth;
   subclasses opt into divergence by overriding their own
   ``DEFAULT_CSS``.

Subclasses keep ownership of their column shape, row builders, and
``populate``/``update_*`` plumbing — only the cross-cutting
navigation/visual contract is hoisted up.
"""

from __future__ import annotations

from textual.widgets import DataTable


class FocusReleasingDataTable(DataTable):
    """``DataTable`` that releases focus to siblings at row boundaries."""

    DEFAULT_CSS = """
    FocusReleasingDataTable {
        width: 1fr;
        height: 1fr;
        min-height: 3;
    }
    FocusReleasingDataTable > .datatable--hover {
        background: $boost;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.cursor_type = "none"
        self.zebra_stripes = True

    def on_focus(self) -> None:
        self.cursor_type = "row"

    def on_blur(self) -> None:
        self.cursor_type = "none"

    def action_cursor_up(self) -> None:
        if self.cursor_row <= 0:
            self.app.action_focus_previous()
            return
        super().action_cursor_up()

    def action_cursor_down(self) -> None:
        if self.cursor_row >= self.row_count - 1:
            self.app.action_focus_next()
            return
        super().action_cursor_down()
