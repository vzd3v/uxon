"""Render the session list on Textual's line-oriented viewport API.

The widget owns the row list and paints only visible lines. Telemetry updates
repaint that viewport; arrivals and departures update ``virtual_size``. Its
public surface is:

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

Render contract
---------------

Cursor and hover changes repaint **only the affected rows**, not the whole
widget: :meth:`move_cursor`, :meth:`on_mouse_move`, and :meth:`on_leave` go
through :meth:`_refresh_rows`, which issues a region-scoped ``refresh(Region)``
for the old and new rows (content-area-relative; a data row's y is
``(idx - scroll_y) + 1``, the sticky header owning line 0). A whole-widget
repaint is reserved for data/column changes (:meth:`set_rows` /
:meth:`set_columns`). Focus/blur issue no uxon refresh — Textual's base
``Widget._on_focus`` / ``_on_blur`` each already do one whole-widget repaint
(framework gesture-rate, accepted; the private handlers are not overridden).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.geometry import Region, Size
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
    /* Hug the content: height tracks ``virtual_size`` (header + rows,
       via ScrollView.get_content_height) so the widgets below sit
       directly under the last row instead of being pushed to the
       bottom of the screen. ``max-height: 1fr`` caps growth at the
       remaining space — overflow scrolls inside the widget. */
    SessionListView {
        width: 1fr;
        height: auto;
        max-height: 1fr;
        min-height: 2;
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
        # no-op gate. ``set_block_meta`` runs before ``set_rows`` every
        # tick; a hue/zebra change with otherwise-unchanged rows must still
        # repaint, so ``set_block_meta`` flips this flag on a real change and
        # ``set_rows`` consults it. An identical-data tick (rows unchanged AND
        # meta unchanged) issues **zero** ``refresh`` calls.
        self._render_dirty: bool = False
        self._cursor_row: int = 0
        # Mirrors the DataTable ``cursor_type`` none↔row toggle: the row
        # cursor is hidden until the widget actually has focus, so the
        # first row doesn't look pre-selected while the operator is on a
        # sibling widget.
        self._focused: bool = False
        self._hover_row: int | None = None
        # Cached column widths. ``_column_widths`` is O(rows×cols) — called
        # once per visible ``render_line`` it would be O(viewport×rows×cols)
        # per repaint. Compute once, invalidate on ``set_rows`` / ``set_columns``
        # (the only inputs to the width calc). ``None`` = stale → recompute.
        self._widths_cache: tuple[int, ...] | None = None
        self._update_virtual_size()

    # ── data setters ───────────────────────────────────────────────

    def set_rows(self, rows: tuple[SessionRow, ...]) -> None:
        """Replace the row list and repaint the visible lines.

        ``virtual_size`` (and thus the widget layout) changes **only**
        when the row *count* changes — a steady tick that swaps cell
        values at the same length repaints visible lines with no
        relayout. The cursor is clamped into the new bounds.

        : an identical-data tick — ``rows`` value-equal to the
        current rows (``SessionRow`` is a frozen dataclass, so ``==`` is
        a deep value compare) and no block-meta change since the last
        paint (``_render_dirty`` unset) — issues **zero** ``refresh``
        calls and does not touch ``virtual_size``. Any real change (a
        cell value, an arrival/departure, or a hue/zebra flip via
        :meth:`set_block_meta`) repaints exactly as before.
        """
        rows_changed = rows != self._rows
        if not rows_changed and not self._render_dirty:
            # Identical data and no pending meta change → no-op. No
            # ``refresh``, no ``virtual_size`` write, cursor untouched.
            return
        count_changed = len(rows) != len(self._rows)
        self._rows = rows
        # Row content feeds the width calc — invalidate the cache. (Always:
        # a same-count tick can still change a cell's rendered width.)
        self._widths_cache = None
        if self._rows:
            self._cursor_row = max(0, min(self._cursor_row, len(self._rows) - 1))
        else:
            self._cursor_row = 0
        if count_changed:
            self._update_virtual_size()
        self.refresh()
        self._render_dirty = False

    def set_columns(self, columns: tuple[ColumnSpec, ...]) -> None:
        """Replace the active column set (cross_user / multi_host latch).

        No-op when unchanged. A column-set change can alter content
        width, so recompute ``virtual_size`` and repaint.
        """
        if columns == self._columns:
            return
        self._columns = columns
        self._widths_cache = None
        self._update_virtual_size()
        self.refresh()

    def set_block_meta(self, meta: dict[str, tuple[str, int]]) -> None:
        """Update the ``row_key → (block_color, row_in_block)`` map.

        Seeds the block-hue (NAME / HOST foreground) and zebra parity
        at render time. Called every tick before :meth:`set_rows`.

        : a no-op when ``meta`` equals the current map. On a real
        change it flips ``_render_dirty`` so the following
        :meth:`set_rows` repaints even if the rows themselves are
        unchanged (a hue/zebra change with stable rows still needs a
        repaint). The flag is reset inside ``set_rows`` after it paints.
        """
        if meta == self._block_meta:
            return
        self._block_meta = meta
        self._render_dirty = True

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

    def _refresh_rows(self, *data_indices: int | None) -> None:
        """Repaint specific data rows. Regions are content-area-relative
        (Widget._set_dirty translates by content_offset); data row y is
        (idx - scroll_y) + 1 — the sticky header owns line 0."""
        scroll_y = round(self.scroll_offset.y)
        height, width = self.size.height, self.size.width
        regions = []
        for idx in data_indices:
            if idx is None or not (0 <= idx < len(self._rows)):
                continue
            y = (idx - scroll_y) + 1
            if 1 <= y < height:
                regions.append(Region(0, y, width, 1))
        if regions:
            self.refresh(*regions)  # NEVER call refresh() with no regions here

    def move_cursor(self, *, row: int) -> None:
        """Move the cursor to ``row`` (clamped), scroll it into view, and
        repaint only the old and new cursor rows.

        A whole-widget ``refresh()`` is deliberately avoided. The two row
        regions are computed at the *post-scroll* offset; when the move
        crossed the viewport edge, ``ScrollView.watch_scroll_y`` already
        refreshed the full viewport region (the two rows are a harmless
        subset of it), so there is no scroll-offset read-back timing
        dependency.
        """
        if not self._rows:
            self._cursor_row = 0
            return
        old = self._cursor_row
        self._cursor_row = max(0, min(row, len(self._rows) - 1))
        self._scroll_cursor_into_view()
        self._refresh_rows(old, self._cursor_row)

    def pin_cursor_to(self, row_key: str | None) -> None:
        """Restore the cursor to ``row_key`` after a refresh.

        ``None`` leaves the cursor unchanged. When the key is missing
        (the row was just removed), the cursor falls back to the nearest
        surviving sibling — ``min(prev_row, row_count - 1)`` — so the
        operator never lands on a non-existent row.
        """
        if row_key is None:
            return
        # : the cursor is already on this key — no ``move_cursor``
        # (and thus no ``refresh``). A no-op tick that re-pins the same
        # selection must mutate nothing.
        if self.selected_row_key == row_key:
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
            # At the top edge — hand focus up the chain. The action
            # rows above are a vertical stack, so the previous focus
            # stop *is* the row directly above the table; spatial
            # navigation and the focus chain agree.
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
        # Textual's base ``Widget._on_focus`` already issues one whole-widget
        # refresh (framework gesture-rate, accepted per ) — no uxon refresh.
        self._focused = True

    def on_blur(self) -> None:
        # See ``on_focus``: the framework's ``_on_blur`` handles the repaint.
        self._focused = False

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
            old = self._hover_row
            self._hover_row = idx
            self._refresh_rows(old, idx)

    def on_leave(self, event: events.Leave) -> None:
        if self._hover_row is not None:
            old = self._hover_row
            self._hover_row = None
            self._refresh_rows(old)

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

        Memoized: this is O(rows×cols) and is read once per visible line
        in :meth:`render_line`; without the cache a repaint would be
        O(viewport×rows×cols). The cache is invalidated whenever the row
        list or column set changes (:meth:`set_rows` / :meth:`set_columns`).
        """
        if self._widths_cache is not None:
            return self._widths_cache
        if not self._columns:
            self._widths_cache = ()
            return self._widths_cache
        widths = [max(_MIN_COL_WIDTH, cell_len(col.label)) for col in self._columns]
        for row in self._rows:
            for i, col in enumerate(self._columns):
                text = self._cell_plain(row, col)
                w = cell_len(text)
                if w > widths[i]:
                    widths[i] = w
        self._widths_cache = tuple(widths)
        return self._widths_cache

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
