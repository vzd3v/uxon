"""SessionListView — render-on-demand session list on Textual's Line API.

The render-on-demand replacement for ``SessionDashboardTable`` /
``FocusReleasingDataTable`` (spec D1). A :class:`~textual.scroll_view.ScrollView`
that owns the current row list and paints **only the visible viewport lines**
via :meth:`render_line`. A telemetry tick is ``set_rows(new)`` → repaint visible
lines; ``virtual_size`` (and thus a layout) changes **only when the row count
changes** — a real arrival/departure, the one legitimate relayout. Compare to
``DataTable``, whose every structural mutation forces a screen-global relayout
(spec § Problem).

Phase-1 status: this widget is built **alongside** the live dashboard and wired
in by Phase 2. It re-exposes every public surface the current call sites use
(spec § "New ``SessionListView`` public API") so the swap is a rename, not a
reshape:

* ``set_rows`` / ``set_columns`` / ``set_block_meta`` / ``set_block_starts``;
* ``block_starts`` (property), ``row_count`` (property);
* ``cursor_row`` (int), ``selected_row_key`` (property) / ``cursor_row_key()``,
  ``pin_cursor_to(row_key | None)``, ``move_cursor(row=...)``;
* ``RowSelected`` (carries ``cursor_row``) and ``HostNavigate`` (carries a
  direction) messages;
* mouse select / hover;
* edge-release via ``BINDINGS`` actions only (no ``on_key`` — AGENTS.md hard
  rule), reproducing the ``FocusReleasingDataTable`` contract.

Layout
------

The column header is rendered as the **sticky** top line (mirrors
``DataTable.show_header``): viewport line 0 is always the header; data rows
scroll beneath it. ``virtual_size`` height is ``1 (header) + row_count``.
``render_line(y)`` maps viewport ``y == 0`` → header and ``y >= 1`` →
data row ``(y - 1) + scroll_y``. Cell widths are Unicode-safe via
:func:`rich.cells.cell_len` — no 1-byte-equals-1-column assumption.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip

from uxon.infra.events import debug

if TYPE_CHECKING:
    from ..dashboard.columns import ColumnSpec
    from ..dashboard.row import SessionRow

# One space between columns, matching the DataTable cell gutter feel.
_COL_GAP = 2
# Minimum column content width so a short header/value still reads.
_MIN_COL_WIDTH = 3


class SessionListView(ScrollView):
    """Virtualized, render-on-demand session list (Line API).

    Construction takes the active ``columns`` tuple (already filtered by
    layout flags). No rows until :meth:`set_rows`.
    """

    DEFAULT_CSS = """
    SessionListView {
        width: 1fr;
        height: 1fr;
        min-height: 3;
    }
    """

    BINDINGS = [
        # ←/→ dispatch to the screen, which interprets them by view mode
        # (cycle host tabs in by_host, jump blocks in flat). The widget
        # posts a message rather than acting directly because the rule
        # needs the screen's view-mode state.
        Binding("left", "host_navigate(-1)", "", show=False),
        Binding("right", "host_navigate(1)", "", show=False),
        Binding("up", "cursor_up", "", show=False),
        Binding("down", "cursor_down", "", show=False),
        Binding("home", "cursor_home", "", show=False),
        Binding("end", "cursor_end", "", show=False),
        Binding("pageup", "page_up", "", show=False),
        Binding("pagedown", "page_down", "", show=False),
        Binding("enter", "select_cursor", "", show=False),
    ]

    class RowSelected(Message):
        """Posted on Enter / click — the screen attaches the row.

        Carries ``cursor_row`` so the handler indexes its row list
        exactly as the old ``DataTable.RowSelected`` did.
        """

        def __init__(self, cursor_row: int) -> None:
            super().__init__()
            self.cursor_row = cursor_row

    class HostNavigate(Message):
        """Posted on ←/→ — the screen cycles tabs or jumps blocks."""

        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    def __init__(
        self,
        columns: tuple[ColumnSpec, ...],
        *,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._columns: tuple[ColumnSpec, ...] = columns
        self._rows: tuple[SessionRow, ...] = ()
        self._block_meta: dict[str, tuple[str, int]] = {}
        self._block_starts: tuple[int, ...] = ()
        self._cursor_row: int = 0
        # Mirrors the DataTable ``cursor_type`` none↔row toggle: the row
        # cursor is hidden until the widget actually has focus, so the
        # first row doesn't look pre-selected while the operator is on a
        # sibling widget.
        self._focused: bool = False
        self._hover_row: int | None = None
        self._update_virtual_size()

    # ── data setters ───────────────────────────────────────────────

    def set_rows(self, rows: tuple[SessionRow, ...]) -> None:
        """Replace the row list and repaint the visible lines.

        ``virtual_size`` (and thus the widget layout) changes **only**
        when the row *count* changes — a steady tick that swaps cell
        values at the same length repaints visible lines with no
        relayout. The cursor is clamped into the new bounds.
        """
        count_changed = len(rows) != len(self._rows)
        self._rows = rows
        if self._rows:
            self._cursor_row = max(0, min(self._cursor_row, len(self._rows) - 1))
        else:
            self._cursor_row = 0
        if count_changed:
            self._update_virtual_size()
        self.refresh()

    def set_columns(self, columns: tuple[ColumnSpec, ...]) -> None:
        """Replace the active column set (cross_user / multi_host latch).

        No-op when unchanged. A column-set change can alter content
        width, so recompute ``virtual_size`` and repaint.
        """
        if columns == self._columns:
            return
        self._columns = columns
        self._update_virtual_size()
        self.refresh()

    def set_block_meta(self, meta: dict[str, tuple[str, int]]) -> None:
        """Update the ``row_key → (block_color, row_in_block)`` map.

        Seeds the block-hue (NAME / HOST foreground) and zebra parity
        at render time. Called every tick before :meth:`set_rows`.
        """
        self._block_meta = meta

    def set_block_starts(self, starts: tuple[int, ...]) -> None:
        """Update the row indices that begin a logical block.

        Consumed by the screen's flat-mode block-jump (``←/→``). The
        screen recomputes this after every model rebuild and feeds it
        back in.
        """
        self._block_starts = starts

    @property
    def block_starts(self) -> tuple[int, ...]:
        return self._block_starts

    @property
    def row_count(self) -> int:
        return len(self._rows)

    @property
    def cursor_row(self) -> int:
        return self._cursor_row

    @property
    def selected_row_key(self) -> str | None:
        """Row-key under the cursor, or ``None`` for an empty/oob cursor."""
        idx = self._cursor_row
        if idx < 0 or idx >= len(self._rows):
            return None
        return self._rows[idx].key

    def cursor_row_key(self) -> str | None:
        """Alias for :attr:`selected_row_key` (call-site parity)."""
        return self.selected_row_key

    # ── cursor management ──────────────────────────────────────────

    def move_cursor(self, *, row: int) -> None:
        """Move the cursor to ``row`` (clamped) and scroll it into view."""
        if not self._rows:
            self._cursor_row = 0
            return
        self._cursor_row = max(0, min(row, len(self._rows) - 1))
        self._scroll_cursor_into_view()
        self.refresh()

    def pin_cursor_to(self, row_key: str | None) -> None:
        """Restore the cursor to ``row_key`` after a refresh.

        ``None`` leaves the cursor unchanged. When the key is missing
        (the row was just removed), the cursor falls back to the nearest
        surviving sibling — ``min(prev_row, row_count - 1)`` — so the
        operator never lands on a non-existent row.
        """
        if row_key is None:
            return
        for i, row in enumerate(self._rows):
            if row.key == row_key:
                self.move_cursor(row=i)
                return
        if not self._rows:
            return
        fallback = min(max(0, self._cursor_row), len(self._rows) - 1)
        self.move_cursor(row=fallback)

    def _scroll_cursor_into_view(self) -> None:
        """Scroll so the cursor row sits within the data viewport.

        Data rows live below the 1-line sticky header, so the usable
        height for data is ``size.height - 1``.
        """
        height = max(1, self.size.height - 1)
        scroll_y = round(self.scroll_offset.y)
        if self._cursor_row < scroll_y:
            self.scroll_to(y=self._cursor_row, animate=False)
        elif self._cursor_row >= scroll_y + height:
            self.scroll_to(y=self._cursor_row - height + 1, animate=False)

    # ── BINDINGS actions (edge-release contract, no on_key) ────────

    def action_cursor_down(self) -> None:
        before = self._cursor_row
        if self._cursor_row >= len(self._rows) - 1:
            # At the bottom edge — hand focus to the next sibling
            # (the FocusReleasingDataTable contract).
            self.app.action_focus_next()
            return
        self.move_cursor(row=self._cursor_row + 1)
        debug(
            "keys",
            at="dashboard_cursor_down",
            from_row=before,
            to_row=self._cursor_row,
            row_count=self.row_count,
        )

    def action_cursor_up(self) -> None:
        before = self._cursor_row
        debug(
            "keys",
            at="dashboard_cursor_up",
            from_row=before,
            row_count=self.row_count,
        )
        if self._cursor_row <= 0:
            # At the top edge — hand focus up. The base contract walks
            # the focus chain backwards, which lands on the *last*
            # ActionRow in #top-actions (rightmost). Operators expect ↑
            # to land on the leftmost button (action-cwd), matching how
            # the row reads left-to-right. Walk #top-actions explicitly
            # when present; fall back to the base contract otherwise.
            from .action_row import ACTION_GROUP_CONTAINER_ID, ActionRow

            group = None
            try:
                group = self.screen.query_one(f"#{ACTION_GROUP_CONTAINER_ID}")
            except Exception:
                group = None
            if group is not None:
                for child in group.children:
                    if isinstance(child, ActionRow):
                        child.focus()
                        return
            self.app.action_focus_previous()
            return
        self.move_cursor(row=self._cursor_row - 1)
        debug("keys", at="dashboard_cursor_up_after", to_row=self._cursor_row)

    def action_cursor_home(self) -> None:
        if self._rows:
            self.move_cursor(row=0)

    def action_cursor_end(self) -> None:
        if self._rows:
            self.move_cursor(row=len(self._rows) - 1)

    def action_page_down(self) -> None:
        height = max(1, self.size.height - 1)
        self.move_cursor(row=self._cursor_row + height)

    def action_page_up(self) -> None:
        height = max(1, self.size.height - 1)
        self.move_cursor(row=self._cursor_row - height)

    def action_host_navigate(self, direction: int) -> None:
        debug("keys", at="dashboard_host_navigate", direction=direction)
        self.post_message(self.HostNavigate(direction))

    def action_select_cursor(self) -> None:
        if 0 <= self._cursor_row < len(self._rows):
            self.post_message(self.RowSelected(self._cursor_row))

    # ── focus / mouse ──────────────────────────────────────────────

    def on_focus(self) -> None:
        self._focused = True
        self.refresh()

    def on_blur(self) -> None:
        self._focused = False
        self.refresh()

    def on_click(self, event: events.Click) -> None:
        idx = self._row_at(event.y)
        if idx is None:
            return
        self.focus()
        self.move_cursor(row=idx)
        self.post_message(self.RowSelected(idx))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        idx = self._row_at(event.y)
        if idx != self._hover_row:
            self._hover_row = idx
            self.refresh()

    def on_leave(self, event: events.Leave) -> None:
        if self._hover_row is not None:
            self._hover_row = None
            self.refresh()

    def _row_at(self, viewport_y: int) -> int | None:
        """Map a viewport y (widget-relative) to a data-row index.

        Viewport line 0 is the sticky header → ``None``. Below it, data
        row ``(y - 1) + scroll_y``; out-of-range → ``None``.
        """
        if viewport_y <= 0:
            return None
        idx = (viewport_y - 1) + round(self.scroll_offset.y)
        if 0 <= idx < len(self._rows):
            return idx
        return None

    # ── virtual size ───────────────────────────────────────────────

    def _content_width(self) -> int:
        """Total rendered content width across all columns (no scroll)."""
        widths = self._column_widths()
        if not widths:
            return 0
        return sum(widths) + _COL_GAP * (len(widths) - 1)

    def _update_virtual_size(self) -> None:
        # Height = sticky header line + one line per data row. The header
        # line is part of virtual_size so the scrollbar geometry matches
        # the viewport's line accounting (DataTable does the same).
        self.virtual_size = Size(self._content_width(), len(self._rows) + 1)

    def _column_widths(self) -> tuple[int, ...]:
        """Per-column display width.

        Sized to the widest of the header label and every rendered cell
        (Unicode-safe via ``cell_len``), floored at ``_MIN_COL_WIDTH``.
        Computed across the full row set (not just the viewport) so the
        column geometry is stable while scrolling — a viewport-only
        width would make columns jitter as rows scroll past.
        """
        if not self._columns:
            return ()
        widths = [max(_MIN_COL_WIDTH, cell_len(col.label)) for col in self._columns]
        for row in self._rows:
            for i, col in enumerate(self._columns):
                text = self._cell_plain(row, col)
                w = cell_len(text)
                if w > widths[i]:
                    widths[i] = w
        return tuple(widths)

    @staticmethod
    def _cell_plain(row: SessionRow, col: ColumnSpec) -> str:
        value = col.format(row)
        if isinstance(value, Text):
            return value.plain
        return str(value)

    # ── rendering ──────────────────────────────────────────────────

    def render_line(self, y: int) -> Strip:
        """Render viewport line ``y`` (widget-relative).

        Line 0 is the sticky column header; lines below render data row
        ``(y - 1) + scroll_y``. Only the visible lines are ever built —
        this is the render-on-demand contract.
        """
        width = self.size.width
        scroll_x = round(self.scroll_offset.x)
        scroll_y = round(self.scroll_offset.y)
        if y == 0:
            return self._render_header(width, scroll_x)
        data_idx = (y - 1) + scroll_y
        if 0 <= data_idx < len(self._rows):
            return self._render_row(data_idx, width, scroll_x)
        return Strip.blank(width, self.rich_style)

    def _render_header(self, width: int, scroll_x: int) -> Strip:
        widths = self._column_widths()
        style = self.rich_style + Style(bold=True)
        segments = self._compose_segments(
            [(col.label, col.align, style) for col in self._columns],
            widths,
        )
        return self._finish(segments, width, scroll_x, self.rich_style + Style(bold=True))

    def _render_row(self, data_idx: int, width: int, scroll_x: int) -> Strip:
        row = self._rows[data_idx]
        widths = self._column_widths()
        is_cursor = self._focused and data_idx == self._cursor_row
        is_hover = data_idx == self._hover_row
        base = self._row_base_style(row, data_idx, is_cursor, is_hover)

        cells: list[tuple[str, str, Style]] = []
        meta = self._block_meta.get(row.key)
        for col in self._columns:
            value = col.format(row)
            plain = value.plain if isinstance(value, Text) else str(value)
            cell_style = base
            # Block hue applies to NAME / HOST foreground only, matching
            # the old ``_wrap_cell`` contract; the cursor/zebra background
            # still wins as the base. CPU danger styling is per-cell from
            # the formatter — layer it on when present.
            if col.id in ("name", "host") and meta is not None:
                cell_style = base + Style.parse(meta[0])
            elif isinstance(value, Text) and value.style:
                cell_style = base + Style.parse(str(value.style))
            cells.append((plain, col.align, cell_style))
        segments = self._compose_segments(cells, widths)
        return self._finish(segments, width, scroll_x, base)

    def _row_base_style(
        self,
        row: SessionRow,
        data_idx: int,
        is_cursor: bool,
        is_hover: bool,
    ) -> Style:
        if is_cursor:
            return self.rich_style + Style(reverse=True)
        if is_hover:
            return self.rich_style + Style(bgcolor="grey30")
        # Zebra: dim alternate rows by block parity (row_in_block) so the
        # stripe survives reorder, matching the old DataTable zebra.
        meta = self._block_meta.get(row.key)
        parity = meta[1] if meta is not None else data_idx
        if parity % 2 == 1:
            return self.rich_style + Style(dim=True)
        return self.rich_style

    def _compose_segments(
        self,
        cells: list[tuple[str, str, Style]],
        widths: tuple[int, ...],
    ) -> list[Segment]:
        """Pad/truncate each cell to its column width, join with the gap."""
        segments: list[Segment] = []
        gap = " " * _COL_GAP
        for i, (text, align, style) in enumerate(cells):
            if i >= len(widths):
                break
            col_w = widths[i]
            padded = self._fit(text, col_w, align)
            segments.append(Segment(padded, style))
            if i < len(cells) - 1 and i < len(widths) - 1:
                segments.append(Segment(gap, style))
        return segments

    @staticmethod
    def _fit(text: str, width: int, align: str) -> str:
        """Pad or truncate ``text`` to exactly ``width`` display columns."""
        w = cell_len(text)
        if w == width:
            return text
        if w < width:
            pad = " " * (width - w)
            return pad + text if align == "right" else text + pad
        # Truncate to width display columns (Unicode-safe).
        out: list[str] = []
        acc = 0
        for ch in text:
            cw = cell_len(ch)
            if acc + cw > width:
                break
            out.append(ch)
            acc += cw
        truncated = "".join(out)
        if acc < width:
            truncated += " " * (width - acc)
        return truncated

    def _finish(
        self,
        segments: list[Segment],
        width: int,
        scroll_x: int,
        fill_style: Style,
    ) -> Strip:
        """Crop horizontally to the viewport and pad to full width."""
        strip = Strip(segments).adjust_cell_length(self._content_width(), fill_style)
        strip = strip.crop(scroll_x, scroll_x + width)
        return strip.extend_cell_length(width, fill_style)
