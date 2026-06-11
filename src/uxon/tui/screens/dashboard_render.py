"""DashboardRender — the refresh/render cluster for :class:`MainScreen`.

Holds the bodies of the dashboard per-tick lifecycle (model compute,
stable-order placement, ``SessionListView`` drive, status bars, the
empty-note toggle, and the block colour/meta helpers). The Screen keeps
``_refresh_dashboard`` as a thin delegator so existing call sites (and the
unit tests that drive it directly) stay valid; the controller takes the
owning Screen as ``host`` and mutates ``host._dashboard_rows`` exactly as
before.

Render model (spec D6): the model selector emits the current row set; the
pure :func:`uxon.tui.dashboard.order.place` function freezes it against the
persisted order (existing rows keep their slot, new rows land in their host
block, dead rows drop); the result is pushed to the :class:`SessionListView`
in one ``set_rows`` call and the cursor is re-pinned by row-key. No diff, no
reconciler — the widget repaints only the visible viewport. Each widget is
queried once, and aux widgets (status bars, tab strip, superuser rows) are
written **only when their value changed** vs the last tick (AC11), so an
identical-data tick mutates nothing. The change-gate extends to the
``apply_ctx_refresh`` tail too: the tab-strip ``display`` write goes through
``_changed("tabs_display", …)``, and the status line through the screen's own
gated ``_update_status_line`` writer (AC2/AC3).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from textual.widgets import Static

from uxon.infra.events import debug as _debug

from ..dashboard.buckets import (
    compute_block_starts,
    select_fleet_summary,
    select_host_buckets,
    select_host_status_block,
)
from ..dashboard.model import select_dashboard_model
from ..dashboard.order import place
from ..widgets import ActionRow
from ..widgets.fleet_status_bar import FleetStatusBar
from ..widgets.host_status_bar import HostStatusBar
from ..widgets.host_tab_strip import HostTabStrip
from ..widgets.session_list_view import SessionListView
from . import main_render

if TYPE_CHECKING:
    from ..dashboard.buckets import HostBucket, HostStatusLine
    from ..dashboard.row import SessionRow
    from .main import MainScreen


class DashboardRender:
    """Dashboard refresh/render controller bound to one :class:`MainScreen`."""

    def __init__(self, host: MainScreen) -> None:
        self.host = host
        # Last-applied aux-widget values, for the AC11 changed-only gate:
        # an identical-data tick must not call ``Static.update`` / ``label``
        # / ``display`` on an unchanged value. ``_UNSET`` distinguishes
        # "never written" from a legitimate ``None``/``False`` value.
        self._last: dict[str, object] = {}

    _UNSET = object()

    def _changed(self, key: str, value: object) -> bool:
        """True iff ``value`` differs from the last write under ``key``.

        Records ``value`` as the new last-write. Used to gate aux-widget
        mutations so an identical-data tick touches nothing (AC11).
        """
        prev = self._last.get(key, self._UNSET)
        if prev is value or prev == value:
            return False
        self._last[key] = value
        return True

    def block_colors(self) -> dict[str | None, str]:
        """Map ``host_name → block colour``, shared by tab strip + table glyphs.

        Single source for the palette/local-host pair so the strip
        and the dashboard rows can never disagree on hue. Local
        import keeps the module graph tidy.
        """
        from ..dashboard.columns import assign_block_colors

        cfg = self.host.cfg
        palette = tuple(getattr(cfg, "tui_color_palette", ("cyan", "blue")))
        local_color = getattr(cfg, "local_host_color", "green")
        return assign_block_colors(
            tuple(cfg.remote_hosts),
            local_color=local_color,
            palette=palette,
        )

    def build_dashboard_cfg_view(self) -> SimpleNamespace:
        """Minimal cfg view consumed by :func:`select_dashboard_model`.

        The selector reads only ``cfg.remote_hosts`` today; the namespace
        includes ``current_user`` for symmetry / future widening. This
        avoids importing :class:`uxon.tui.config.TuiConfig` here (its
        constructor demands the full callback bundle, which the bridge
        does not need).
        """
        cfg = self.host.cfg
        return SimpleNamespace(
            remote_hosts=cfg.remote_hosts,
            current_user=cfg.current_user,
        )

    def cursor_row_key(self, widget: SessionListView) -> str | None:
        """Read the dashboard cursor's row-key for pin-after-refresh.

        The widget owns its row list, so the selected key reads directly
        off :attr:`SessionListView.selected_row_key` — ``None`` for an
        empty list or out-of-range cursor, which the widget's
        :meth:`pin_cursor_to` accepts as "leave alone".
        """
        try:
            return widget.selected_row_key
        except Exception:  # pragma: no cover — defensive (widget not ready)
            return None

    def refresh_dashboard_note(self, all_rows: tuple[SessionRow, ...]) -> None:
        """Toggle the ``#sessions-note`` placeholder above the dashboard.

        Visible when no rows are present (Loading… on cold start,
        "No active sessions." once the rebuild has landed). The class
        toggle keeps the layout signature stable across the
        empty/non-empty transition — the Static is mounted
        unconditionally.

        Changed-only (AC11): the ``-hidden`` class and the placeholder
        text are each written only when their value changed vs the last
        tick, so an identical-data tick issues no ``set_class`` / ``update``
        at the call site (not merely relies on Textual's self-no-op).
        """
        host = self.host
        try:
            note = host.query_one("#sessions-note", Static)
        except Exception:  # pragma: no cover — note not yet mounted
            return
        hidden = bool(all_rows)
        if self._changed("note_hidden", hidden):
            note.set_class(hidden, "-hidden")
        if not all_rows:
            text = "Loading sessions…" if host.cfg.loading else "No active sessions."
            if self._changed("note_text", text):
                note.update(text)

    def refresh_dashboard(self) -> None:
        """Compute the model, place it in frozen order, drive the list view.

        Owns the dashboard's per-tick lifecycle (spec D6): pull state,
        build the row tuple via :func:`select_dashboard_model` (full model
        — local own + local other-user + remote per-host rows), freeze it
        against the persisted order with :func:`place` (existing rows keep
        their slot, new rows land in their host block, dead rows drop),
        persist the new order back onto ``MainScreenUiState.row_order``,
        push the displayed rows to the :class:`SessionListView` in one
        ``set_rows`` call, then re-pin the cursor by row-key so a no-op
        tick leaves it where it was.

        Each widget is queried once; aux widgets (tab strip, status bars,
        superuser rows) are written only when their value changed vs the
        last tick (AC11).

        ``cross_user`` is *not* recomputed here — the active column tuple
        is fixed at ``__init__`` time, and a flip in
        ``bool(ctx.other_sessions)`` forces a recompose via the layout
        signature.
        """
        host = self.host
        state = getattr(host.app, "state", None)
        if state is None:
            return
        # ``keys`` channel: bracket every dashboard refresh with entry +
        # elapsed_ms so the operator can correlate "arrow press swallowed"
        # with "main thread blocked here". Wall-clock is unsuitable
        # (NTP jitter); ``time.monotonic`` is the safe diff source.
        import time as _time  # noqa: PLC0415

        _refresh_t0 = _time.monotonic()
        _debug("keys", at="refresh_dashboard_enter", ts=_refresh_t0)
        cfg_view = self.build_dashboard_cfg_view()
        model_rows = select_dashboard_model(state, cfg_view, host._dashboard_ui)  # type: ignore[arg-type]
        # Freeze the model against the persisted order: existing rows keep
        # their slot, new rows land in their host block, dead rows drop
        # (spec D2). Persist the new order back onto the App-owned state so
        # a ctx rebuild / background refresh never re-sorts.
        placed = place(host.app.main_ui.row_order, model_rows)  # type: ignore[attr-defined]
        host.app.main_ui.row_order = tuple(r.key for r in placed)  # type: ignore[attr-defined]
        # Full (frozen) model — local (host=None) + remote (host=peer).
        all_rows = placed
        rows = placed
        # A non-empty filter forces flat render; the tab strip is
        # hidden (no buckets) so the operator sees every match across
        # hosts in one list.
        needle = host._dashboard_ui.filter_text.strip()
        forced_flat = bool(needle)
        in_by_host = host._dashboard_ui.view_mode == "by_host" and not forced_flat
        # Single query for the tab strip; visible only when in_by_host.
        try:
            tab_strip: HostTabStrip | None = host.query_one("#host-tabs", HostTabStrip)
        except Exception:
            tab_strip = None
        if tab_strip is not None and self._changed("tabs_display", in_by_host):
            tab_strip.display = in_by_host
        active_bucket = None
        if in_by_host:
            buckets = select_host_buckets(rows, cfg_view)
            # The App holds the surviving tab index so a recompose
            # doesn't snap the operator back to "local". Apply it
            # before ``set_buckets`` so the strip mounts already
            # showing the right tab (avoids a one-frame flicker).
            saved_idx = host.app.main_ui.active_tab_index  # type: ignore[attr-defined]
            if buckets and saved_idx >= len(buckets):
                saved_idx = max(0, len(buckets) - 1)
                host.app.main_ui.active_tab_index = saved_idx  # type: ignore[attr-defined]
            if tab_strip is None:
                active_idx = saved_idx if buckets else 0
            else:
                if tab_strip.active_index != saved_idx:
                    tab_strip.active_index = saved_idx
                tab_strip.set_buckets(list(buckets), colors=self.block_colors())
                active_idx = tab_strip.active_index
            if buckets:
                active_bucket = (
                    buckets[active_idx] if 0 <= active_idx < len(buckets) else buckets[0]
                )
                # The active bucket's rows, kept in the frozen global order
                # (``select_host_buckets`` partitions ``placed``, which is
                # already frozen, so block order is preserved).
                rows = active_bucket.rows
        try:
            widget = host.query_one("#sessions-dashboard", SessionListView)
        except Exception:  # pragma: no cover — not yet mounted
            return
        prev_cursor_key = self.cursor_row_key(widget)
        # Live column-set reconciliation. The USER column (``cross_user``
        # latch) and HOST column (``multi_host``) can appear at runtime;
        # rebuild them on the live widget instead of re-composing the
        # screen, so the focus holder survives and no in-flight key is
        # dropped. ``set_columns`` no-ops when the column tuple is
        # unchanged; ``prev_cursor_key`` was captured above so the cursor
        # re-pins by identity afterwards.
        new_columns = host._compute_active_columns()
        if new_columns != host._active_columns:
            host._active_columns = new_columns
            widget.set_columns(new_columns)
        widget.set_block_meta(self.build_block_meta(rows))
        widget.set_block_starts(compute_block_starts(rows, host.cfg.current_user))
        widget.set_rows(rows)
        host._dashboard_rows = rows
        widget.pin_cursor_to(prev_cursor_key)
        self.refresh_dashboard_note(all_rows)
        # Status lines aggregate over the unfiltered, full row tuple so the
        # bars reflect fleet totals even when a search filter narrows the
        # table.
        host_stats_local = state.main.host_stats if state.main is not None else None
        status_lines = select_host_status_block(all_rows, state, host_stats_local, cfg_view)
        self._update_compact_bar(in_by_host, active_bucket, status_lines)
        self._update_fleet_bar(status_lines)
        self._update_superuser_block()
        _debug(
            "keys",
            at="refresh_dashboard_exit",
            ms=int((_time.monotonic() - _refresh_t0) * 1000),
            rows=len(rows),
            view=host._dashboard_ui.view_mode,
            forced_flat=forced_flat,
        )

    def _update_compact_bar(
        self,
        in_by_host: bool,
        active_bucket: HostBucket | None,
        status_lines: tuple[HostStatusLine, ...],
    ) -> None:
        """Drive the by_host per-tab status line (changed-only — AC11).

        Shows the active host's detail under its tab in by_host; hidden
        otherwise. The ``display`` flag and the line content are each
        written only when they change vs the last tick.
        """
        host = self.host
        try:
            compact_bar = host.query_one("#host-status-compact", HostStatusBar)
        except Exception:
            return
        if in_by_host and active_bucket is not None and status_lines:
            line = next(
                (sl for sl in status_lines if sl.host_name == active_bucket.host_name),
                status_lines[0],
            )
            if self._changed("compact_display", True):
                compact_bar.display = True
            if self._changed("compact_line", line):
                compact_bar.update_lines((line,))
        elif self._changed("compact_display", False):
            compact_bar.display = False

    def _update_fleet_bar(self, status_lines: tuple[HostStatusLine, ...]) -> None:
        """Drive the fleet status bar (changed-only — AC11).

        Present in both views: collapsed = counts + quiet alerts;
        expanded (``h``) = one line per host. The summary/lines/expanded
        triple is written only when it differs from the last tick.
        """
        host = self.host
        try:
            fleet_bar = host.query_one("#fleet-status", FleetStatusBar)
        except Exception:
            return
        summary = select_fleet_summary(status_lines)
        if self._changed("fleet", (summary, status_lines, host._hosts_expanded)):
            fleet_bar.update_fleet(summary, status_lines, expanded=host._hosts_expanded)

    def _update_superuser_block(self) -> None:
        """Drive the superuser block visibility + labels (changed-only).

        The Settings / Kill-ALL-global rows are always mounted; their
        visibility is toggled here, never by re-composing the tree
        (a sudo-reachability flip or a 0↔N session crossing used to force
        a focus-dropping screen swap). Each ``display`` / ``label`` write
        is gated on a value change vs the last tick (AC11).
        """
        host = self.host
        has_super = bool(host.cfg.sudo_caps.reachable_users)
        total_sessions = len(host.cfg.sessions) + len(host.cfg.other_sessions)
        try:
            settings_row = host.query_one("#action-settings", ActionRow)
        except Exception:
            settings_row = None
        if settings_row is not None and self._changed("settings_display", has_super):
            settings_row.display = has_super
        try:
            kill_row = host.query_one("#action-kill-all-global", ActionRow)
        except Exception:
            kill_row = None
        if kill_row is not None:
            label = main_render.kill_all_global_label(total_sessions)
            detail = host._kill_all_global_detail()
            kill_display = has_super and total_sessions > 0
            if self._changed("kill_global_label", label):
                kill_row.label = label
            if self._changed("kill_global_detail", detail):
                kill_row.detail = detail
            if self._changed("kill_global_text", (label, detail)):
                kill_row._render_text()
            if self._changed("kill_global_display", kill_display):
                kill_row.display = kill_display

    def apply_ctx_refresh(self) -> bool:
        """Re-render the action rows + status bars after a ctx swap.

        Returns ``True`` on success, ``False`` if the action-row DOM is
        not ready (the caller then falls back to a full recompose). The
        in-place patch path that ``apply_loaded_ctx`` takes when the
        layout signature is unchanged.
        """
        host = self.host
        try:
            host._refresh_cwd_row()
            open_row = host.query_one("#action-open", ActionRow)
            open_row.detail = host._open_detail()
            open_row.set_enabled(host.cfg.loading or bool(host.cfg.existing_projects))
            host.query_one("#action-new", ActionRow).detail = main_render.new_project_detail(
                host.cfg.new_project_root
            )
            host.query_one("#action-new", ActionRow)._render_text()
            open_row._render_text()
        except Exception:
            return False

        # All sessions (own + other-user + remote) render through the
        # unified dashboard widget; ``refresh_dashboard`` below pulls
        # a consistent ``state.main`` + ``state.remote`` snapshot. It also
        # owns the superuser block's visibility + content (Settings /
        # Kill-ALL-global), so no separate update is needed here.

        # The "Loading sessions…" / "No active sessions." placeholder
        # is owned by ``refresh_dashboard_note`` (called from
        # ``refresh_dashboard`` below), which also toggles its
        # visibility based on the current local-row count.
        self.refresh_dashboard()

        host._update_status_line()
        return True

    def build_block_meta(
        self,
        rows: tuple[SessionRow, ...],
    ) -> dict[str, tuple[str, int]]:
        """Map each row's identity key to (block_color, row_in_block).

        ``block_color`` comes from :func:`assign_block_colors` on the
        cfg's remote hosts; ``row_in_block`` is the row's index inside
        its host block (0, 1, 2, ...) for zebra parity. The key matches
        :attr:`uxon.tui.dashboard.row.SessionRow.key`, which the widget
        looks up at render time to colour NAME / HOST and pick zebra
        parity.
        """
        colors = self.block_colors()
        local_color = colors.get(None, "green")
        out: dict[str, tuple[str, int]] = {}
        counters: dict[str | None, int] = {}
        for row in rows:
            host_key = row.host  # None for locals
            idx = counters.get(host_key, 0)
            counters[host_key] = idx + 1
            out[row.key] = (colors.get(host_key, local_color), idx)
        return out
