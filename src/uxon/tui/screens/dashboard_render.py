"""DashboardRender — the refresh/render cluster for :class:`MainScreen`.

Holds the bodies of the dashboard per-tick lifecycle (model compute,
diff, widget apply, status bars, the empty-note toggle, and the block
colour/meta helpers). The Screen keeps ``_refresh_dashboard`` as a thin
delegator so existing call sites (and the unit tests that drive it
directly) stay valid; the controller takes the owning Screen as ``host``
and mutates ``host._dashboard_rows`` exactly as before.

Mechanical lift — no behavior change: every ``self.cfg``/``self.app``/
``self._dashboard_ui`` reference is re-routed through ``self.host``.
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
from ..dashboard.reconcile import diff
from ..widgets import ActionRow
from ..widgets.fleet_status_bar import FleetStatusBar
from ..widgets.host_status_bar import HostStatusBar
from ..widgets.host_tab_strip import HostTabStrip
from ..widgets.session_dashboard_table import SessionDashboardTable
from . import main_render

if TYPE_CHECKING:
    from ..dashboard.row import SessionRow
    from .main import MainScreen


class DashboardRender:
    """Dashboard refresh/render controller bound to one :class:`MainScreen`."""

    def __init__(self, host: MainScreen) -> None:
        self.host = host

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

    def cursor_row_key(self, widget: SessionDashboardTable) -> str | None:
        """Read the dashboard cursor's row-key for pin-after-apply.

        Returns ``None`` for an empty table or out-of-range cursor — the
        widget's :meth:`pin_cursor_to` accepts ``None`` as "leave alone".
        """
        try:
            idx = widget.cursor_row
            if idx is None or idx < 0 or idx >= len(widget.ordered_rows):
                return None
            key_obj = widget.ordered_rows[idx].key
            value = getattr(key_obj, "value", None)
            return value if isinstance(value, str) else None
        except Exception:  # pragma: no cover — defensive (widget not ready)
            return None

    def refresh_dashboard_note(self, all_rows: tuple[SessionRow, ...]) -> None:
        """Toggle the ``#sessions-note`` placeholder above the dashboard.

        Visible when no rows are present (Loading… on cold start,
        "No active sessions." once the rebuild has landed). The class
        toggle keeps the layout signature stable across the
        empty/non-empty transition — the Static is mounted
        unconditionally.
        """
        host = self.host
        try:
            note = host.query_one("#sessions-note", Static)
        except Exception:  # pragma: no cover — note not yet mounted
            return
        if all_rows:
            note.set_class(True, "-hidden")
        else:
            note.set_class(False, "-hidden")
            note.update("Loading sessions…" if host.cfg.loading else "No active sessions.")

    def refresh_dashboard(self) -> None:
        """Compute new model, diff against the previous, apply to the widget.

        Owns the dashboard's per-tick lifecycle: pull state, build the
        row tuple via :func:`select_dashboard_model` (full model — local
        own + local other-user + remote per-host rows), diff against the
        previous tuple, apply the ops to the widget, then pin the cursor
        by row-key so a no-op tick leaves it where it was.

        ``cross_user`` is *not* recomputed here — the active column
        tuple is fixed at ``__init__`` time, and a flip in
        ``bool(ctx.other_sessions)`` forces a recompose via the
        layout signature.
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
        rows = select_dashboard_model(state, cfg_view, host._dashboard_ui)  # type: ignore[arg-type]
        # Full model — local (host=None) + remote (host=peer).
        all_rows = rows
        # A non-empty filter forces flat render; the tab strip is
        # hidden (no buckets) so the operator sees every match across
        # hosts in one list.
        needle = host._dashboard_ui.filter_text.strip()
        forced_flat = bool(needle)
        in_by_host = host._dashboard_ui.view_mode == "by_host" and not forced_flat
        # Tab strip visible only when in_by_host.
        try:
            tab_strip_widget = host.query_one("#host-tabs", HostTabStrip)
            tab_strip_widget.display = in_by_host
        except Exception:
            pass
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
            try:
                tab_strip = host.query_one("#host-tabs", HostTabStrip)
            except Exception:
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
                rows = active_bucket.rows
        try:
            widget = host.query_one("#sessions-dashboard", SessionDashboardTable)
        except Exception:  # pragma: no cover — not yet mounted
            return
        prev_cursor_key = self.cursor_row_key(widget)
        # Live column-set reconciliation. The USER column (``cross_user``
        # latch) and HOST column (``multi_host``) can appear at runtime;
        # rebuild them on the live widget instead of re-composing the
        # screen, so the focus holder survives and no in-flight key is
        # dropped. ``set_columns`` clears rows, so reset the row cache —
        # the diff below then re-adds every row against the new columns.
        # ``prev_cursor_key`` was captured above, so the cursor still
        # re-pins by identity after the rebuild.
        new_columns = host._compute_active_columns()
        if new_columns != host._active_columns:
            host._active_columns = new_columns
            widget.set_columns(new_columns)
            host._dashboard_rows = ()
        plan = diff(host._dashboard_rows, rows, host._active_columns)
        widget.set_block_meta(self.build_block_meta(rows))
        widget.set_block_starts(compute_block_starts(rows, host.cfg.current_user))
        widget.apply(plan)
        host._dashboard_rows = rows
        widget.pin_cursor_to(prev_cursor_key)
        self.refresh_dashboard_note(all_rows)
        # Status lines aggregate over the unfiltered, full row tuple so the
        # bars reflect fleet totals even when a search filter narrows the
        # table.
        host_stats_local = state.main.host_stats if state.main is not None else None
        status_lines = select_host_status_block(all_rows, state, host_stats_local, cfg_view)
        # Compact per-tab line: by_host only, shows the active host's detail
        # under its tab.
        try:
            compact_bar = host.query_one("#host-status-compact", HostStatusBar)
        except Exception:
            compact_bar = None
        if compact_bar is not None:
            if in_by_host and active_bucket is not None and status_lines:
                line = next(
                    (sl for sl in status_lines if sl.host_name == active_bucket.host_name),
                    status_lines[0],
                )
                compact_bar.display = True
                compact_bar.update_lines((line,))
            else:
                compact_bar.display = False
        # Fleet bar below the table: present in both views. Collapsed =
        # counts + quiet alerts; expanded (``h``) = one line per host.
        try:
            fleet_bar = host.query_one("#fleet-status", FleetStatusBar)
        except Exception:
            fleet_bar = None
        if fleet_bar is not None:
            fleet_bar.update_fleet(
                select_fleet_summary(status_lines),
                status_lines,
                expanded=host._hosts_expanded,
            )
        # Superuser block (header / Settings / Kill-ALL-global) is always
        # mounted; its visibility is toggled here, never by re-composing
        # the tree. A sudo-reachability flip (has_super) or a 0↔N session
        # crossing (kill-global) used to change the widget tree, forcing a
        # focus-dropping screen swap; the ``display`` toggle keeps the
        # tree stable so refresh can't drop keys. Mirrors the tab-strip /
        # status-bar idiom above.
        has_super = bool(host.cfg.sudo_caps.reachable_users)
        total_sessions = len(host.cfg.sessions) + len(host.cfg.other_sessions)
        try:
            super_header = host.query_one("#superuser-header", Static)
        except Exception:
            super_header = None
        if super_header is not None:
            super_header.update(host._superuser_header())
            super_header.display = has_super
        try:
            settings_row = host.query_one("#action-settings", ActionRow)
        except Exception:
            settings_row = None
        if settings_row is not None:
            settings_row.display = has_super
        try:
            kill_row = host.query_one("#action-kill-all-global", ActionRow)
        except Exception:
            kill_row = None
        if kill_row is not None:
            kill_row.label = main_render.kill_all_global_label(total_sessions)
            reachable = sorted(host.cfg.sudo_caps.reachable_users)
            kill_row.detail = f"({', '.join(reachable)} + self)" if reachable else ""
            kill_row._render_text()
            kill_row.display = has_super and total_sessions > 0
        _debug(
            "keys",
            at="refresh_dashboard_exit",
            ms=int((_time.monotonic() - _refresh_t0) * 1000),
            rows=len(rows),
            view=host._dashboard_ui.view_mode,
            forced_flat=forced_flat,
        )

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
            try:
                host.query_one("#top-actions-caption", Static).update(host._top_actions_caption())
            except Exception:  # pragma: no cover — caption mounted in compose
                pass
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
        """Map each row's reconciler key to (block_color, row_in_block).

        ``block_color`` comes from :func:`assign_block_colors` on the
        cfg's remote hosts; ``row_in_block`` is the row's index inside
        its host block (0, 1, 2, ...) for zebra parity.
        """
        colors = self.block_colors()
        local_color = colors.get(None, "green")
        out: dict[str, tuple[str, int]] = {}
        counters: dict[str | None, int] = {}
        for row in rows:
            host_key = row.host  # None for locals
            idx = counters.get(host_key, 0)
            counters[host_key] = idx + 1
            key = f"{row.host or 'local'}/{row.user}/{row.name}"
            out[key] = (colors.get(host_key, local_color), idx)
        return out
