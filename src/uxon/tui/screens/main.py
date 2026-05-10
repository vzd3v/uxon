"""MainScreen — the top-level menu rendered by :class:`UxonApp`.

Layout:
    ┌ Header ──────────────────────────────────────────┐
    │ ActionRow action-cwd                             │
    │ ActionRow action-new                             │
    │ ActionRow action-open                            │
    │ ── sessions ──                                   │
    │ SessionDashboardTable (own + other-user + remote │
    │   rows; USER column iff cross_user, HOST column  │
    │   iff multi_host)                                │
    │ ── superuser ── (when reachable_users is set)    │
    │ ActionRow settings                               │
    │ ActionRow kill-all-global                        │
    └ Footer ──────────────────────────────────────────┘

T7a shipped layout + core bindings (q/f1/d/D/r); 3.4 dropped
``escape → quit`` and added layout-invariant JCUKEN twins. Digit-jump
arrives in T7b; activation wiring in T7c. The screen holds a
reference to the current :class:`TuiContext` and refreshes it on ``r``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.lazy import Lazy
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from ..context import (
    ACTION_COUNT,
    CallbackError,
    TuiContext,
    _segments,
)
from ..dashboard.buckets import select_host_buckets, select_host_status_block
from ..dashboard.layout import LayoutFlags, build_active_columns
from ..dashboard.model import select_dashboard_model
from ..dashboard.reconcile import diff
from ..dashboard.row import SessionRow
from ..dashboard.ui_state import DashboardUiState, set_filter, set_view_mode
from ..events import debug as _debug
from ..keymap import bindings_with_aliases
from ..state import (
    MainIntent,
    activate_main_index,
    digit_jump_intent,
    main_action_intent,
    main_status_line,
    select_layout_signature,
    visible_detected_agents,
)
from ..widgets import ActionRow, DetectedAgentsBanner
from ..widgets.host_status_bar import HostStatusBar
from ..widgets.host_tab_strip import HostTabActivated, HostTabStrip
from ..widgets.search_bar import FilterChanged, SearchBar
from ..widgets.session_dashboard_table import SessionDashboardTable
from .confirm import ConfirmPhrase, ConfirmYesNo
from .existing import ExistingProjectScreen
from .git_profile import GitProfileScreen
from .launch_options import LaunchOptionsScreen
from .new_project import NewProjectScreen


class MainScreen(Screen):
    """Top-level screen: actions + session tables + Kill-ALL / Settings."""

    DEFAULT_CSS = """
    MainScreen {
        layout: vertical;
    }
    #main-body {
        width: 1fr;
        height: 1fr;
    }
    .segment-header {
        color: $text-muted;
        padding: 0 1;
    }
    .empty-note {
        color: $text-muted;
        padding: 1 2;
    }
    .empty-note.-hidden {
        display: none;
    }
    #server-status {
        color: $text-muted;
        padding: 0 1;
        margin-bottom: 1;
    }
    #server-status.-alert {
        color: $error;
        text-style: bold;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("q", "quit", "Quit", show=True),
        Binding("f1", "help", "Help", show=False),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("d", "kill", "Kill", show=True),
        Binding("D", "kill_all_own", "Kill-ALL (mine)", show=True),
        Binding("v", "toggle_view", "View", show=True),
        Binding("[", "prev_tab", "Prev host", show=True),
        Binding("]", "next_tab", "Next host", show=True),
        Binding("s", "focus_search", "Search", show=True),
        Binding("/", "focus_search", "", show=False),
        # Detected-agents banner: only does something when the banner is
        # visible (``visible_detected_agents(...)`` is non-empty). When the
        # banner is hidden these bindings are no-ops; the footer hides
        # them via ``show=False`` to avoid clutter.
        Binding("a", "enable_detected", "Enable detected", show=False),
        Binding("x", "dismiss_detected", "Dismiss detected", show=False),
        # Arrow navigation across focusable widgets. DataTable consumes
        # up/down internally for row navigation, so these only fire when
        # focus sits on an ActionRow — crossing widget boundaries.
        Binding("up", "app.focus_previous", "", show=False),
        Binding("down", "app.focus_next", "", show=False),
        # Digit 1-9 jump — resolver guards Settings / Kill-ALL.
        Binding("1", "digit_jump(1)", "1-9 jump", show=True, priority=True),
        Binding("2", "digit_jump(2)", "", show=False, priority=True),
        Binding("3", "digit_jump(3)", "", show=False, priority=True),
        Binding("4", "digit_jump(4)", "", show=False, priority=True),
        Binding("5", "digit_jump(5)", "", show=False, priority=True),
        Binding("6", "digit_jump(6)", "", show=False, priority=True),
        Binding("7", "digit_jump(7)", "", show=False, priority=True),
        Binding("8", "digit_jump(8)", "", show=False, priority=True),
        Binding("9", "digit_jump(9)", "", show=False, priority=True),
    )

    # Stage 8 commit 3: writable reactive driving the
    # ``#sessions-note`` re-render when the first ``MainData`` lands.
    # Plain assignment only — **no** ``compute_loading`` method:
    # Textual's ``Reactive._set`` (textual/reactive.py:330-333) marks
    # the descriptor read-only when ``hasattr(obj, compute_name)``
    # holds, and ``mutate_reactive`` raises immediately. The
    # rebuild-source dispatcher (commit 7) writes
    # ``screen.loading = (state.main is None)`` directly. Until then
    # ``apply_loaded_ctx`` mirrors ``ctx.loading`` into this reactive
    # so existing renderers see a consistent state.
    loading: reactive[bool] = reactive(True)

    def __init__(self, ctx: TuiContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.loading = bool(ctx.loading)
        self._restore_focus_key = ""
        # Dashboard rows are an in-flight repaint cache, not UI state —
        # they're rebuilt from ``state`` on every tick, so dying with
        # the screen on recompose is harmless. Filter/view/tab state
        # is different: it lives on ``self.app.main_ui`` (see
        # :class:`MainScreenUiState`) so a layout-signature flip
        # doesn't silently snap the operator back to defaults.
        self._dashboard_rows: tuple[SessionRow, ...] = ()
        # Compute the active dashboard columns once and reuse from
        # ``compose`` and ``_refresh_dashboard``. Two independent calls
        # would be fragile when the flags widen — easy to drift one
        # path. ``MainScreen`` is reconstructed via
        # ``switch_screen(MainScreen)`` on the recompose path, so a
        # ``cross_user`` flip picks up the new columns naturally
        # because ``__init__`` runs again. The layout signature's
        # ``has_other_sessions`` bool (``select_layout_signature``)
        # tracks ``bool(ctx.other_sessions)`` — the same predicate
        # used here — so the flip and the column-set rebuild are
        # wired through the same source.
        flags = LayoutFlags(
            multi_host=bool(ctx.remote_hosts),
            cross_user=bool(ctx.other_sessions),
        )
        self._active_columns = build_active_columns(
            cfg_columns=ctx.tui_table_columns,
            flags=flags,
        )

    # ── Recompose-safe UI state proxies ──────────────────────────────
    #
    # These three properties tunnel through to ``self.app.main_ui``
    # (the :class:`MainScreenUiState` the App owns). They keep the
    # call sites in this screen unchanged while making the underlying
    # storage stable across the ``apply_loaded_ctx`` recompose path.

    @property
    def _dashboard_ui(self) -> DashboardUiState:
        return self.app.main_ui.ui  # type: ignore[attr-defined]

    @_dashboard_ui.setter
    def _dashboard_ui(self, value: DashboardUiState) -> None:
        self.app.main_ui.ui = value  # type: ignore[attr-defined]

    @property
    def _tab_focus_pending_restore(self) -> bool:
        return self.app.main_ui.pending_tab_focus_restore  # type: ignore[attr-defined]

    @_tab_focus_pending_restore.setter
    def _tab_focus_pending_restore(self, value: bool) -> None:
        self.app.main_ui.pending_tab_focus_restore = value  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="main-body"):
            line = main_status_line(
                self.ctx.server_status,
                self.ctx.link_health_status,
                self.ctx.refresh_tick,
                loading=self.ctx.loading,
            )
            yield Static(line.text, id="server-status", classes="-alert" if line.alert else "")
            # Detected-agents banner. Hidden by default; the host probe
            # worker populates ctx.detected_agents and triggers
            # ``_refresh_detected_banner``.
            # Wrapped in Lazy so it does not contend with first paint;
            # the banner is hidden at mount and only becomes visible after
            # the host probe lands and ``_refresh_detected_banner`` runs.
            yield Lazy(DetectedAgentsBanner("", id="detected-banner", classes="-hidden"))
            # Action rows
            yield ActionRow(
                kind="action-cwd",
                label="New session in current folder",
                detail=self._cwd_detail(),
                digit=1,
                enabled=self.ctx.cwd_writable is not False,
                id="action-cwd",
            )
            yield ActionRow(
                kind="action-new",
                label="Create new project",
                detail=f"({self.ctx.new_project_root}/…)",
                digit=2,
                enabled=True,
                id="action-new",
            )
            yield ActionRow(
                kind="action-open",
                label="Open existing project",
                detail=self._open_detail(),
                digit=3,
                # While loading we don't yet know whether projects exist.
                # Keep the row enabled so it isn't dimmed; activation falls
                # through to the existing "no projects" notify path.
                enabled=self.ctx.loading or bool(self.ctx.existing_projects),
                id="action-open",
            )
            # Sessions header + unified dashboard. The dashboard is
            # mounted unconditionally — empty-state copy is rendered by
            # the ``#sessions-note`` Static above it (toggled by class
            # in ``_refresh_dashboard_note``). Local own + local
            # other-user + remote per-host rows all flow through this
            # one widget; the USER column appears when ``cross_user``
            # is set, the HOST column when ``multi_host`` is set.
            yield SearchBar(id="search-bar")
            yield Static("── sessions ──", classes="segment-header")
            note = "Loading sessions…" if self.ctx.loading else "No active sessions."
            note_classes = "empty-note"
            if self.ctx.sessions or self.ctx.other_sessions:
                # Dashboard will populate momentarily — start hidden so
                # the layout doesn't flicker an empty-note at first paint.
                note_classes = "empty-note -hidden"
            yield Static(note, id="sessions-note", classes=note_classes)
            # Tab strip + status bars are always mounted regardless of the
            # initial view mode; ``_refresh_dashboard`` toggles ``display``
            # so the widget tree stays stable across ``v`` flips. Mounting
            # only when ``view_mode == "by_host"`` would silently break
            # ``v`` for operators who configure ``default_view = "flat"``
            # (the toggle would have no widgets to show).
            yield HostTabStrip([], id="host-tabs")
            yield HostStatusBar(mode="compact", id="host-status-compact")
            yield HostStatusBar(mode="expanded", id="host-status-expanded")
            yield SessionDashboardTable(columns=self._active_columns, id="sessions-dashboard")
            if bool(self.ctx.sudo_caps.reachable_users):
                yield Static(self._superuser_header(), classes="segment-header")
                yield ActionRow(
                    kind="settings",
                    label="⚙ Settings",
                    detail="(repo-level config.toml)",
                    digit=None,
                    enabled=True,
                    id="action-settings",
                )
                total_sessions = len(self.ctx.sessions) + len(self.ctx.other_sessions)
                if total_sessions > 0:
                    reachable_users = sorted(self.ctx.sudo_caps.reachable_users)
                    yield ActionRow(
                        kind="kill-all-global",
                        label=(
                            f"⚡ Kill ALL uxon sessions (reachable users, {total_sessions} total)"
                        ),
                        detail=f"({', '.join(reachable_users)} + self)",
                        digit=None,
                        enabled=True,
                        id="action-kill-all-global",
                    )
            # Multi-host: remote rows fold into the unified dashboard
            # above. The HOST column is auto-prepended when the
            # ``multi_host`` LayoutFlag is True (data-driven via
            # ``cfg.remote_hosts`` in ``__init__``). No separate widget
            # / section header is needed — per-host attribution lives on
            # each row's HOST cell + the dashboard's host colour glyph.
        yield Footer()

    def _superuser_header(self) -> str:
        """Header for the "Other users' sessions" / superuser block.

        When the per-target probe filtered any candidates (caller's
        sudoers rule covers some users in ``session_users`` but not
        all), append a ``(N/M users reachable)`` hint so the operator
        notices a colleague is missing rather than silently absent.
        """
        reachable = sorted(self.ctx.sudo_caps.reachable_users)
        skipped = list(self.ctx.scope_skipped_users)
        total = len(reachable) + len(skipped)
        if skipped and total:
            return f"── superuser ── ({len(reachable)}/{total} users reachable)"
        return "── superuser ──"

    def _cwd_detail(self) -> str:
        if self.ctx.cwd_writable is False:
            user = self.ctx.launch_user or self.ctx.current_user or "launch user"
            return f"({self.ctx.cwd_short} — not launchable for {user})"
        return f"({self.ctx.cwd_short})"

    def _open_detail(self) -> str:
        if self.ctx.loading:
            return f"({self.ctx.new_project_root}/… — loading)"
        return f"({self.ctx.new_project_root}/…)"

    def on_mount(self) -> None:
        # Initial dashboard apply. ``state.main`` may still be ``None``
        # at this point (cold-start skeleton ctx) — ``_refresh_dashboard``
        # treats that as a zero-row tick and toggles the empty-note in
        # response. ``#sessions-dashboard`` (the unified
        # :class:`SessionDashboardTable`) owns row display end-to-end.
        self._refresh_dashboard()
        self.call_after_refresh(self._update_status_line)
        if self._restore_focus_key and self._focus_key(self._restore_focus_key):
            # Stage 10a — ``UXON_DEBUG=startup``: closest proxy to "first
            # frame painted" Textual exposes without a renderer hook. By
            # the end of ``MainScreen.on_mount`` the widgets are mounted
            # and the next event-loop tick paints them; this is the
            # signal we emit for the ``mount_started → first_paint``
            # budget.
            import time as _time  # noqa: PLC0415

            _debug("startup", at="first_paint", ts=_time.monotonic())
            return
        self.call_later(self._focus_default_action)
        import time as _time  # noqa: PLC0415

        _debug("startup", at="first_paint", ts=_time.monotonic())

    def on_show(self) -> None:
        if not self._restore_focus_key:
            self.call_later(self._focus_default_action)

    # ── Dashboard (commit 10 bridge: own-only) ───────────────────────

    def _block_colors(self) -> dict[str | None, str]:
        """Map ``host_name → block colour``, shared by tab strip + table glyphs.

        Single source for the palette/local-host pair so the strip
        and the dashboard rows can never disagree on hue. Local
        import keeps the module graph tidy.
        """
        from ..dashboard.columns import assign_block_colors

        palette = tuple(getattr(self.ctx, "tui_color_palette", ("cyan", "blue")))
        local_color = getattr(self.ctx, "local_host_color", "green")
        return assign_block_colors(
            tuple(self.ctx.remote_hosts),
            local_color=local_color,
            palette=palette,
        )

    def _build_dashboard_cfg_view(self) -> SimpleNamespace:
        """Minimal cfg view consumed by :func:`select_dashboard_model`.

        The selector reads only ``cfg.remote_hosts`` today; the namespace
        includes ``current_user`` for symmetry / future widening. This
        avoids importing :class:`uxon.tui.config.TuiConfig` here (its
        constructor demands the full callback bundle, which the bridge
        does not need).
        """
        return SimpleNamespace(
            remote_hosts=self.ctx.remote_hosts,
            current_user=self.ctx.current_user,
        )

    def _cursor_row_key(self, widget: SessionDashboardTable) -> str | None:
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

    def _refresh_dashboard_note(self, all_rows: tuple[SessionRow, ...]) -> None:
        """Toggle the ``#sessions-note`` placeholder above the dashboard.

        Visible when no rows are present (Loading… on cold start,
        "No active sessions." once the rebuild has landed). The class
        toggle keeps the layout signature stable across the
        empty/non-empty transition — the Static is mounted
        unconditionally.
        """
        try:
            note = self.query_one("#sessions-note", Static)
        except Exception:  # pragma: no cover — note not yet mounted
            return
        if all_rows:
            note.set_class(True, "-hidden")
        else:
            note.set_class(False, "-hidden")
            note.update("Loading sessions…" if self.ctx.loading else "No active sessions.")

    def _refresh_dashboard(self) -> None:
        """Compute new model, diff against the previous, apply to the widget.

        Owns the dashboard's per-tick lifecycle: pull state, build the
        row tuple via :func:`select_dashboard_model` (full model — local
        own + local other-user + remote per-host rows), diff against the
        previous tuple, apply the ops to the widget, then pin the cursor
        by row-key so a no-op tick leaves it where it was.

        ``cross_user`` is *not* recomputed here. The active column
        tuple is fixed at ``__init__`` time; a flip in
        ``bool(ctx.other_sessions)`` changes the layout signature
        (``select_layout_signature``) and forces the outer
        ``apply_loaded_ctx`` recompose path, which constructs a new
        :class:`MainScreen` whose ``__init__`` rebuilds
        ``_active_columns`` with the new flag. The patch path
        (this method) only applies row-level ops.

        Per-host repaint optimisation is preserved structurally: the
        model selector's identity-stable contract returns the same
        tuple object when no slot changed, and a single-host slot
        replacement yields a tuple where only that host's rows differ;
        the reconciler emits ops only for those rows.
        """
        state = getattr(self.app, "state", None)
        if state is None:
            return
        cfg_view = self._build_dashboard_cfg_view()
        rows = select_dashboard_model(state, cfg_view, self._dashboard_ui)  # type: ignore[arg-type]
        # Commit 12: full model — local (host=None) + remote (host=peer).
        all_rows = rows
        # Task 9: a non-empty filter forces flat render; the tab strip is
        # hidden (no buckets) so the operator sees every match across
        # hosts in one list.
        needle = self._dashboard_ui.filter_text.strip()
        forced_flat = bool(needle)
        in_by_host = self._dashboard_ui.view_mode == "by_host" and not forced_flat
        # Tab strip visible only when in_by_host.
        try:
            tab_strip_widget = self.query_one("#host-tabs", HostTabStrip)
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
            saved_idx = self.app.main_ui.active_tab_index  # type: ignore[attr-defined]
            if buckets and saved_idx >= len(buckets):
                saved_idx = max(0, len(buckets) - 1)
                self.app.main_ui.active_tab_index = saved_idx  # type: ignore[attr-defined]
            try:
                tab_strip = self.query_one("#host-tabs", HostTabStrip)
            except Exception:
                active_idx = saved_idx if buckets else 0
            else:
                if tab_strip.active_index != saved_idx:
                    tab_strip.active_index = saved_idx
                tab_strip.set_buckets(list(buckets), colors=self._block_colors())
                active_idx = tab_strip.active_index
            if buckets:
                active_bucket = (
                    buckets[active_idx] if 0 <= active_idx < len(buckets) else buckets[0]
                )
                rows = active_bucket.rows
        try:
            widget = self.query_one("#sessions-dashboard", SessionDashboardTable)
        except Exception:  # pragma: no cover — not yet mounted
            return
        prev_cursor_key = self._cursor_row_key(widget)
        plan = diff(self._dashboard_rows, rows, self._active_columns)
        widget.set_block_meta(self._build_block_meta(rows))
        widget.apply(plan)
        self._dashboard_rows = rows
        widget.pin_cursor_to(prev_cursor_key)
        self._refresh_dashboard_note(all_rows)
        # Task 11: feed the HostStatusBar(s). Status lines aggregate over
        # the unfiltered, full row tuple so the bar reflects fleet
        # totals even when a search filter narrows the table.
        host_stats_local = state.main.host_stats if state.main is not None else None
        status_lines = select_host_status_block(all_rows, state, host_stats_local, cfg_view)
        try:
            compact_bar = self.query_one("#host-status-compact", HostStatusBar)
        except Exception:
            compact_bar = None
        try:
            expanded_bar = self.query_one("#host-status-expanded", HostStatusBar)
        except Exception:
            expanded_bar = None
        if in_by_host and active_bucket is not None and status_lines:
            line = next(
                (sl for sl in status_lines if sl.host_name == active_bucket.host_name),
                status_lines[0],
            )
            if compact_bar is not None:
                compact_bar.display = True
                compact_bar.update_lines((line,))
            if expanded_bar is not None:
                expanded_bar.display = False
        else:
            if compact_bar is not None:
                compact_bar.display = False
            if expanded_bar is not None:
                expanded_bar.display = True
                expanded_bar.update_lines(status_lines)

    def _build_block_meta(
        self,
        rows: tuple[SessionRow, ...],
    ) -> dict[str, tuple[str, int]]:
        """Map each row's reconciler key to (block_color, row_in_block).

        ``block_color`` comes from :func:`assign_block_colors` on the
        cfg's remote hosts; ``row_in_block`` is the row's index inside
        its host block (0, 1, 2, ...) for zebra parity.
        """
        colors = self._block_colors()
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

    # ── ActionRow.Activated dispatcher ───────────────────────────────

    def on_action_row_activated(self, event: ActionRow.Activated) -> None:
        """Route an :class:`ActionRow.Activated` to the right handler.

        Modal chains are stubbed here and wired through in T14.
        ``CallbackError`` from any callback renders as a red toast.
        """
        self._run_intent(main_action_intent(event.row.kind))

    # ── DataTable row activation (Enter on a session row) ───────────

    def on_data_table_row_selected(self, event) -> None:  # type: ignore[no-untyped-def]
        """Enter/click on a session row attaches to that session.

        SessionDashboardTable rows dispatch the right callback based
        on ``row.host``: local rows (``host is None``) go through
        ``ctx.on_attach``; remote rows go through
        ``ctx.on_remote_attach`` (SSH).
        """
        table = event.data_table
        if isinstance(table, SessionDashboardTable):
            idx = event.cursor_row
            if idx is None or idx < 0 or idx >= len(self._dashboard_rows):
                return
            row = self._dashboard_rows[idx]
            if row.host is not None:
                # Remote: dispatch via ctx.on_remote_attach over SSH.
                user = row.user or self.ctx.current_user
                try:
                    req = self.ctx.on_remote_attach(row.host, user, row.name)
                except CallbackError as exc:
                    self.app.notify(f"Remote attach failed: {exc}", severity="error", timeout=6)
                    return
                self.app.request_launch(req)  # type: ignore[attr-defined]
                return
            session_user = row.user or self.ctx.current_user
            self._attach_session(session_user, row.name)
            return

    def _run_intent(self, intent: MainIntent | None) -> None:
        if intent is None:
            return
        if intent.index is not None:
            self._focus_index(intent.index)
        if intent.kind == "launch-cwd":
            self._launch_cwd()
        elif intent.kind == "launch-new":
            self._launch_new()
        elif intent.kind == "launch-existing":
            self._launch_existing()
        elif intent.kind == "open-settings":
            self._open_settings()
        elif intent.kind == "kill-all-global":
            self._kill_all_global()
        elif intent.kind == "attach":
            self._attach_session(intent.user, intent.session_name)
        elif intent.kind == "focus-only":
            self.app.notify("Press Enter to open Settings / Kill-ALL (digit moves cursor only)")

    def _attach_session(self, user: str, session_name: str) -> None:
        try:
            req = self.ctx.on_attach(user, session_name)
        except CallbackError as exc:
            self.app.notify(f"Attach failed: {exc}", severity="error", timeout=6)
            return
        self.app.request_launch(req)  # type: ignore[attr-defined]

    # ── Activation handlers (modals stubbed — T14 replaces stubs) ────

    def _launch_cwd(self) -> None:
        # Async probe may not have landed yet (cross-user / sudo path).
        # Run the fallback probe synchronously here so the user never
        # gets to launch-time with an unknown answer.
        # Stage 8 commit 6: "loading" is now structural — the slot
        # has not been written yet (``last_attempt_at is None``). A
        # legitimate ``value=None`` from a returned probe (rare; the
        # callback rarely returns None) does not trigger the
        # synchronous fallback because the slot's
        # ``last_attempt_at`` is set.
        state = getattr(self.app, "state", None)
        never_loaded = (
            state is None or state.cwd_writable.last_attempt_at is None
        ) and self.ctx.cwd_writable is None
        if never_loaded:
            try:
                self.ctx.cwd_writable = bool(self.ctx.on_probe_cwd_writable())
            except CallbackError as exc:
                self.app.notify(str(exc), severity="error", timeout=6)
                return
            self._refresh_cwd_row()
        if self.ctx.cwd_writable is False:
            user = self.ctx.launch_user or self.ctx.current_user or "launch user"
            self.app.notify(
                f"Cannot launch in {self.ctx.cwd_short} as {user} "
                "(no write access, or outside allowed_roots)",
                severity="warning",
                timeout=6,
            )
            return

        def after_opts(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            agent_id, mode_id = result
            try:
                req = self.ctx.on_launch_cwd(agent_id, mode_id)
            except CallbackError as exc:
                self.app.notify(str(exc), severity="error", timeout=6)
                return
            self.app.request_launch(req)  # type: ignore[attr-defined]

        self.app.push_screen(LaunchOptionsScreen(self.ctx), after_opts)

    def _launch_new(self) -> None:
        def after_opts(name: str, git_profile: str):
            def _on_opts(result: tuple[str, str] | None) -> None:
                if result is None:
                    return
                agent_id, mode_id = result
                try:
                    req = self.ctx.on_launch_new(name, agent_id, mode_id, git_profile)
                except CallbackError as exc:
                    self.app.notify(str(exc), severity="error", timeout=6)
                    return
                self.app.request_launch(req)  # type: ignore[attr-defined]

            return _on_opts

        def after_git(name: str):
            def _on_git(git_profile: str | None) -> None:
                if git_profile is None:
                    return  # user cancelled the whole chain
                self.app.push_screen(LaunchOptionsScreen(self.ctx), after_opts(name, git_profile))

            return _on_git

        def after_name(name: str | None) -> None:
            if not name:
                return
            if self.ctx.git_create_enabled and self.ctx.git_remote_profile_options:
                self.app.push_screen(
                    GitProfileScreen(
                        self.ctx.git_remote_profile_options,
                        default_profile=self.ctx.default_git_remote_profile,
                    ),
                    after_git(name),
                )
            else:
                self.app.push_screen(LaunchOptionsScreen(self.ctx), after_opts(name, ""))

        self.app.push_screen(NewProjectScreen(self.ctx.new_project_root), after_name)

    def _launch_existing(self) -> None:
        if not self.ctx.existing_projects:
            self.app.notify(
                f"No projects in {self.ctx.new_project_root}",
                severity="warning",
                timeout=4,
            )
            return

        def after_name(name: str | None) -> None:
            if not name:
                return

            def after_opts(result: tuple[str, str] | None) -> None:
                if result is None:
                    return
                agent_id, mode_id = result
                try:
                    req = self.ctx.on_launch_existing(name, agent_id, mode_id)
                except CallbackError as exc:
                    self.app.notify(str(exc), severity="error", timeout=6)
                    return
                self.app.request_launch(req)  # type: ignore[attr-defined]

            self.app.push_screen(LaunchOptionsScreen(self.ctx), after_opts)

        self.app.push_screen(
            ExistingProjectScreen(self.ctx.existing_projects, self.ctx.new_project_root),
            after_name,
        )

    def _open_settings(self) -> None:
        """Push SettingsScreen with the context's callback bundle."""
        from .settings import SettingsCallbacks, SettingsScreen

        cbs = SettingsCallbacks(
            get_entries=self.ctx.get_settings_entries,
            save_setting=self.ctx.on_setting_save,
            remove_setting=self.ctx.on_setting_remove,
            save_mapping=self.ctx.on_setting_save_mapping,
            get_git_remote_profile_rows=self.ctx.get_git_remote_profile_rows,
        )
        self.app.push_screen(SettingsScreen(cbs))

    def _kill_all_global(self) -> None:
        total = len(self.ctx.sessions) + len(self.ctx.other_sessions)
        if total == 0:
            return
        reachable = sorted(self.ctx.sudo_caps.reachable_users)
        # User-visible scope summary: name the reachable users
        # explicitly so the operator can't confuse "all reachable"
        # with "all users on the host" — those diverge under the
        # per-target sudo model.
        scope_summary = f"{', '.join(reachable)} (+ self)" if reachable else "self only"
        n_users = len(reachable) + 1  # + launch_user

        def after_confirm(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                self.ctx.on_kill_all_global()
                self.app.notify(f"Killed all {total} sessions across {n_users} reachable users")
            except CallbackError as exc:
                self.app.notify(
                    f"Kill all (reachable) failed: {exc}",
                    severity="error",
                    timeout=6,
                )
                return
            self.action_refresh()

        self.app.push_screen(
            ConfirmPhrase(
                (f"Kill ALL {total} sessions for {n_users} reachable users? [{scope_summary}]"),
                "kill-all-reachable",
            ),
            after_confirm,
        )

    # ── Core bindings ────────────────────────────────────────────────

    def action_quit(self) -> None:
        self.app.quit_rc = 0  # type: ignore[attr-defined]
        self.app.exit()

    def action_help(self) -> None:
        self.app.notify(
            "Enter/click activates a row.  d kills selected, D kills all own, r refreshes."
        )

    def action_refresh(self) -> None:
        # Dispatch through the app so the refresh runs in a worker
        # thread and the event loop stays responsive. Also re-run the
        # host probe so newly-installed tmux/agents show up after a
        # manual refresh — without this, ``r`` would only re-fetch
        # session state and the missing-agents modal would never recover.
        self.app.kick_refresh()  # type: ignore[attr-defined]
        kick = getattr(self.app, "_kick_host_probe", None)
        if callable(kick):
            kick()

    def action_toggle_view(self) -> None:
        new_mode = "flat" if self._dashboard_ui.view_mode == "by_host" else "by_host"
        # The tab strip is hidden in flat mode. If focus is currently
        # inside the strip, ``_refresh_dashboard`` is about to strand
        # it on a ``display: none`` widget — move it to the dashboard
        # table now and remember the position so we can return focus
        # to the active tab when the strip reappears.
        was_on_strip = self._focus_in_tab_strip()
        if new_mode == "flat" and was_on_strip:
            try:
                self.query_one("#sessions-dashboard", SessionDashboardTable).focus()
            except Exception:
                pass
        self._dashboard_ui = set_view_mode(self._dashboard_ui, new_mode)
        self._refresh_dashboard()
        if new_mode == "by_host" and self._tab_focus_pending_restore:
            self._restore_focus_to_active_tab()
        # Pending-restore flag flips on flat→by_host but only when we
        # left the strip on the previous toggle. Set after the restore
        # check so the same toggle doesn't fire it twice.
        self._tab_focus_pending_restore = was_on_strip and new_mode == "flat"
        self.app.notify(f"View: {new_mode.replace('_', ' ')}")

    def _focus_in_tab_strip(self) -> bool:
        """True iff the currently-focused widget is inside ``#host-tabs``."""
        node = self.focused
        while node is not None:
            if isinstance(node, HostTabStrip):
                return True
            node = node.parent
        return False

    def _restore_focus_to_active_tab(self) -> None:
        """Focus the active ``_TabButton`` after a flat→by_host flip."""
        try:
            strip = self.query_one("#host-tabs", HostTabStrip)
        except Exception:
            return
        idx = strip.active_index
        try:
            tab = strip.query_one(f"#tab-{idx}")
        except Exception:
            return
        tab.focus()

    def action_focus_search(self) -> None:
        # Remember which widget summoned the bar so Esc can return
        # focus to it instead of always falling back to action-cwd.
        focused = self.focused
        return_id = focused.id if focused is not None else None
        try:
            bar = self.query_one("#search-bar", SearchBar)
        except Exception:
            return
        bar.show(return_focus_id=return_id)

    def on_filter_changed(self, event: FilterChanged) -> None:
        self._dashboard_ui = set_filter(self._dashboard_ui, event.text)
        self._refresh_dashboard()
        # Update match counter.
        try:
            bar = self.query_one("#search-bar", SearchBar)
            bar.set_match_count(len(self._dashboard_rows))
        except Exception:
            pass

    def action_prev_tab(self) -> None:
        try:
            strip = self.query_one("#host-tabs", HostTabStrip)
        except Exception:
            return
        n = len(strip._buckets)
        if n <= 1:
            return
        strip.active_index = (strip.active_index - 1) % n

    def action_next_tab(self) -> None:
        try:
            strip = self.query_one("#host-tabs", HostTabStrip)
        except Exception:
            return
        n = len(strip._buckets)
        if n <= 1:
            return
        strip.active_index = (strip.active_index + 1) % n

    def on_host_tab_activated(self, event: HostTabActivated) -> None:
        # Persist the new index on the App so a recompose mid-session
        # restores the same tab. ``_refresh_dashboard`` re-reads from
        # ``self.app.main_ui.active_tab_index`` and feeds it back into
        # the strip, keeping the two in lockstep.
        # Short-circuit when the message just echoes the App's
        # current state — `_refresh_dashboard` itself sets
        # ``tab_strip.active_index`` to sync with `main_ui`, which
        # posts this message back; without the guard each refresh
        # would trigger a second redundant refresh per tick.
        if self.app.main_ui.active_tab_index == event.index:  # type: ignore[attr-defined]
            return
        self.app.main_ui.active_tab_index = event.index  # type: ignore[attr-defined]
        self._refresh_dashboard()

    def action_enable_detected(self) -> None:
        """Banner action: add the first detected agent to ``[agents].enabled``."""
        ids = self._visible_detected()
        if not ids:
            return
        if not self.ctx.repo_config_writable:
            self.app.notify(
                "Repo config is read-only; ask your operator to enable in [agents].enabled.",
                severity="warning",
                timeout=6,
            )
            return
        agent_id = ids[0]
        try:
            self.ctx.on_enable_detected_agent(agent_id)
        except CallbackError as exc:
            self.app.notify(f"Enable failed: {exc}", severity="error", timeout=6)
            return
        self.app.notify(f"Enabled '{agent_id}' in [agents].enabled.")
        # Trigger a full refresh so cfg.enabled_agents picks up the new
        # agent and the banner re-evaluates.
        self.app.kick_refresh()  # type: ignore[attr-defined]

    def action_dismiss_detected(self) -> None:
        """Banner action: dismiss the first detected agent (per-user state file)."""
        ids = self._visible_detected()
        if not ids:
            return
        agent_id = ids[0]
        try:
            self.ctx.on_dismiss_detected_agent(agent_id)
        except CallbackError as exc:
            self.app.notify(f"Dismiss failed: {exc}", severity="error", timeout=6)
            return
        self._refresh_detected_banner()

    def _visible_detected(self) -> list[str]:
        try:
            dismissed = self.ctx.get_dismissed_detected_agents()
        except CallbackError:
            dismissed = []
        return visible_detected_agents(
            detected=self.ctx.detected_agents,
            enabled_agents=tuple(self.ctx.enabled_agents),
            dismissed=dismissed,
        )

    def _refresh_detected_banner(self) -> None:
        """Recompute and apply the banner text. No-op if banner not mounted."""
        try:
            banner = self.query_one("#detected-banner", DetectedAgentsBanner)
        except Exception:  # pragma: no cover — DOM not mounted yet
            return
        banner.update_from(
            self._visible_detected(),
            repo_config_writable=self.ctx.repo_config_writable,
        )

    def _layout_signature(self, ctx: TuiContext) -> tuple[bool, bool, bool, bool]:
        """Thin shim over :func:`select_layout_signature`.

        Kept as an instance method so the existing test surface (some
        tests synthesise a screen and call this directly) stays
        unchanged.
        """
        return select_layout_signature(ctx)

    def _refresh_cwd_row(self) -> None:
        """Re-render the cwd action row from the current ctx.cwd_writable."""
        try:
            row = self.query_one("#action-cwd", ActionRow)
        except Exception:  # pragma: no cover — DOM not mounted yet
            return
        row.detail = self._cwd_detail()
        row.set_enabled(self.ctx.cwd_writable is not False)
        row._render_text()

    def _apply_ctx_refresh(self) -> bool:
        try:
            self._refresh_cwd_row()
            open_row = self.query_one("#action-open", ActionRow)
            open_row.detail = self._open_detail()
            open_row.set_enabled(self.ctx.loading or bool(self.ctx.existing_projects))
            self.query_one("#action-new", ActionRow).detail = f"({self.ctx.new_project_root}/…)"
            self.query_one("#action-new", ActionRow)._render_text()
            open_row._render_text()
        except Exception:
            return False

        # All sessions (own + other-user + remote) render through the
        # unified dashboard widget. The dashboard is repopulated
        # below so the model selector sees a consistent ``state.main``
        # + ``state.remote`` snapshot. Per-host repaint optimisation
        # is preserved structurally by the model's identity-stable
        # contract: an unchanged-host slot does not produce new
        # ops in ``diff``.

        if bool(self.ctx.sudo_caps.reachable_users):
            try:
                kill_row = self.query_one("#action-kill-all-global", ActionRow)
            except Exception:
                kill_row = None
            if kill_row is not None:
                total_sessions = len(self.ctx.sessions) + len(self.ctx.other_sessions)
                kill_row.label = (
                    f"⚡ Kill ALL uxon sessions (reachable users, {total_sessions} total)"
                )
                kill_row._render_text()

        # The "Loading sessions…" / "No active sessions." placeholder
        # is owned by ``_refresh_dashboard_note`` (called from
        # ``_refresh_dashboard`` below), which also toggles its
        # visibility based on the current local-row count.

        # Dashboard repopulate: pulls a fresh model from ``state.main``
        # and applies the diff. Owns own + other-user local rows after
        # commit 11.
        self._refresh_dashboard()

        self._update_status_line()
        return True

    def apply_loaded_ctx(self, new_ctx: TuiContext, *, focus_key: str | None = None) -> None:
        """Swap the screen's ctx in. Patches in place if the layout signature
        matches, otherwise switches to a freshly composed MainScreen.

        ``focus_key=None`` (default) captures the currently focused widget
        before the swap so it can be restored after. Pass ``""`` to skip
        focus restoration entirely (used by initial mount).
        """
        _debug(
            "refresh",
            at="apply_loaded_ctx",
            action="enter",
            sessions=len(new_ctx.sessions),
            other=len(new_ctx.other_sessions),
            tick=new_ctx.refresh_tick,
        )
        if focus_key is None:
            focus_key = self._current_focus_key()
        old_signature = self._layout_signature(self.ctx)
        # Carry over state that lives outside the on_refresh result: the
        # link-health status comes from a separate worker, the agent
        # availability dict is mutated in place by the probe worker (which
        # writes to the *app's* ctx — see UxonApp._probe_agents_worker),
        # and refresh_tick is a TUI-internal counter. Without this the
        # probe results are lost after the first ctx swap and every
        # LaunchOptionsScreen would render "(checking…)" forever.
        # Stage 8 commit 6: ``link_health_status`` no longer needs a
        # carry — ``app.state.link_health`` is canonical and shared
        # across ctx rebuild ticks. The shim's getter returns
        # ``state.link_health.value`` so any consumer of the legacy
        # attribute sees the currently-applied status regardless of
        # which ctx it goes through.
        # Stage 8 commit 5b: ``agent_availability`` and
        # ``detected_agents`` no longer need carries — the canonical
        # store is ``app.state.agent_availability`` /
        # ``app.state.detected_agents``, shared across ctx rebuild
        # ticks. The shim's getter returns ``state.<slot>.value`` on
        # read so any consumer of the legacy attribute sees the
        # currently-applied dict regardless of which ctx it goes
        # through.
        # Stage 8 commit 4: ``remote_snapshots`` no longer needs a
        # carry — the canonical store is ``app.state.remote``,
        # shared across rebuild ticks. The shim's getter flattens
        # state.remote on read; in-place mutations of legacy slots
        # are no longer driven by the dispatcher (which writes
        # through ``slot_state.apply`` directly).
        # Stage 8 commit 3: link the new ctx to the App's TuiState
        # before writing through the ``refresh_tick`` proxy. Without
        # this link the assignment would land on the new ctx's own
        # default-factory state (transient and unobserved by anyone)
        # instead of the App-owned state container. Some unit tests
        # build a FakeApp without ``state``; fall through to the
        # legacy slot in that case (covered by ``getattr``).
        app_state = getattr(self.app, "state", None)
        if app_state is not None:
            new_ctx._state = app_state
        else:
            # Carry the legacy ctx's _state across so the proxy keeps
            # round-tripping the counter when no App is in the picture.
            new_ctx._state = self.ctx._state
        # Stage 8 commit 6b: ``state.refresh_tick`` is canonical and
        # advanced by ``UxonApp._handle_main_ctx_rebuild`` *before*
        # this method runs. ``apply_loaded_ctx`` no longer touches
        # the counter — the previous ``new_ctx.refresh_tick =
        # self.ctx.refresh_tick + 1`` line is gone.
        # Stage 8 commit 6: cwd-change invalidation. The carry-list
        # used to enforce this implicitly ("only carry when cwd
        # matches"); now ``state.cwd_writable`` is canonical and a
        # cwd transition resets the slot to its zero state so the
        # row paints "checking…" until the next probe lands.
        # ``state.main`` is not yet canonical (commit 7 flips it),
        # so we compare ``new_ctx.cwd`` against the previous
        # ``self.ctx.cwd``. Worker-side gating
        # (``_CwdWritableUpdated.cwd_at_start``) drops in-flight
        # probes that started against the old cwd.
        if app_state is not None and new_ctx.cwd != self.ctx.cwd:
            from uxon.tui.slot_state import SlotState as _SlotState

            app_state.cwd_writable = _SlotState[bool | None]()
        self.ctx = new_ctx
        # Mirror ``ctx.loading`` into the reactive so watchers fire
        # when the first non-skeleton ctx lands. Commit 7 flips this
        # to ``state.main is None``; for now both sides agree. The
        # reactive descriptor needs Textual node setup (``_id``); test
        # stubs that bypass ``__init__`` (``MainScreen.__new__``) hit
        # ReactiveError on assignment, so guard with a defensive
        # ``hasattr`` rather than try/except (cheaper hot-path).
        if hasattr(self, "_id"):
            self.loading = bool(new_ctx.loading)
        # Keep app.ctx in lockstep so the probe worker's writes target the
        # same dict that screens read from.
        self.app.ctx = new_ctx  # type: ignore[attr-defined]
        if self._layout_signature(self.ctx) == old_signature and self._apply_ctx_refresh():
            if focus_key and self._focus_key(focus_key):
                return
            # Don't yank focus when the in-place patch leaves the DOM
            # untouched; the focused widget still exists and is fine.
            return
        # Full re-compose when section structure changed.
        new_screen = MainScreen(self.ctx)
        new_screen._restore_focus_key = focus_key
        self.app.switch_screen(new_screen)

    def action_kill(self) -> None:
        """Confirm then kill the session under focus.

        Only fires on :class:`SessionDashboardTable`
        (``#sessions-dashboard``). Resolves via the cursor index into
        ``self._dashboard_rows``:

        * Local rows (``row.host is None``) dispatch to ``ctx.on_kill``;
          ``row.user`` carries the target user so other-user rows take
          the existing sudo path inside the kill callback.
        * Remote rows (``row.host`` set) dispatch to
          ``ctx.on_remote_kill(host, user, name)`` — the local CLI runs
          ``uxon kill --force --host <h> --user <u> <name>`` over SSH.

        Any other focus target falls through to the "select a session"
        notify.
        """
        focused = self.focused
        if not isinstance(focused, SessionDashboardTable):
            self.app.notify("Select a session first.", severity="warning")
            return
        idx = focused.cursor_row
        if idx is None or idx < 0 or idx >= len(self._dashboard_rows):
            return
        row = self._dashboard_rows[idx]
        session_user = row.user or self.ctx.current_user
        session_name = row.name
        session_short = row.short or row.name

        if row.host is not None:
            # Remote: dispatch via SSH through ctx.on_remote_kill.
            host = row.host

            def after_confirm_remote(ok: bool | None) -> None:
                if not ok:
                    return
                try:
                    self.ctx.on_remote_kill(host, session_user, session_name)
                    self.app.notify(
                        f"Killed {session_short} on {host}; remote table will update on next poll"
                    )
                except CallbackError as exc:
                    self.app.notify(f"Remote kill failed: {exc}", severity="error", timeout=6)
                    return
                self.action_refresh()

            self.app.push_screen(
                ConfirmYesNo(f"Kill {session_name} on {host} (user={session_user})?"),
                after_confirm_remote,
            )
            return

        def after_confirm(ok: bool | None) -> None:
            if not ok:
                return
            try:
                self.ctx.on_kill(session_user, session_name)
                self.app.notify(f"Killed {session_short}")
            except CallbackError as exc:
                self.app.notify(
                    f"Kill {session_short} failed: {exc}",
                    severity="error",
                    timeout=6,
                )
                return
            self.action_refresh()

        self.app.push_screen(
            ConfirmYesNo(f"Kill {session_name} (user={session_user})?"),
            after_confirm,
        )

    def action_kill_all_own(self) -> None:
        if not self.ctx.sessions:
            return
        n = len(self.ctx.sessions)

        def after_confirm(ok: bool | None) -> None:
            if not ok:
                return
            try:
                self.ctx.on_kill_all()
                self.app.notify(f"Killed all {n} sessions")
            except CallbackError as exc:
                self.app.notify(f"Kill all failed: {exc}", severity="error", timeout=6)
                return
            self.action_refresh()

        self.app.push_screen(
            ConfirmPhrase(f"Kill ALL {n} sessions?", "kill-all"),
            after_confirm,
        )

    # ── Digit-jump ───────────────────────────────────────────────────

    def action_digit_jump(self, n: int) -> None:
        """Jump to (and activate) the item hinted by digit ``n``.

        Guard (ported verbatim from ``DigitJumpGuardTests``): on an
        empty-superuser state, digit ACTION_COUNT+1 lands on Settings /
        Kill-ALL, which must NOT auto-activate — it's a "move cursor
        only" row, reachable by arrow-down + Enter. Same rule applies
        in the textual flavour via :func:`_digit_hinted_indices`.
        """
        self._run_intent(digit_jump_intent(self.ctx, n))

    def _activate_index(self, idx: int) -> None:
        """Resolve index into a concrete item and fire its activation."""
        self._run_intent(activate_main_index(self.ctx, idx))

    def _focus_index(self, idx: int) -> None:
        """Move focus to the widget backing ``idx`` on the current screen."""
        own_start, _other_start, settings_idx, kill_idx, has_super = _segments(self.ctx)
        # Local-rows segment in the dashboard. Own + other-user are
        # both mounted in ``#sessions-dashboard`` (commit 11), so the
        # visual segment ends at ``settings_idx`` (when sudo) or
        # ``own_start + len(ctx.sessions)`` (no sudo — there's no
        # other-user segment at all).
        local_end = settings_idx if has_super else own_start + len(self.ctx.sessions)
        try:
            if idx < own_start:
                action_ids = ("action-cwd", "action-new", "action-open")
                self.query_one(f"#{action_ids[idx]}", ActionRow).focus()
                return
            if idx < local_end:
                # Single dashboard widget for own + other-user rows.
                # The visual cursor row offset is ``idx - own_start``;
                # this is best-effort focus (the dashboard applies the
                # hard sort contract and a substring filter, so the
                # visual order may not match
                # ``ctx.sessions + ctx.other_sessions``). Out-of-range
                # cursor moves are silently no-op.
                t = self.query_one("#sessions-dashboard", SessionDashboardTable)
                t.move_cursor(row=idx - own_start)
                t.focus()
                return
            if has_super and idx == settings_idx:
                self.query_one("#action-settings", ActionRow).focus()
                return
            if has_super and idx == kill_idx:
                self.query_one("#action-kill-all-global", ActionRow).focus()
                return
        except Exception:  # pragma: no cover — focus best-effort
            pass

    def _current_focus_key(self) -> str:
        focused = self.focused
        if isinstance(focused, ActionRow):
            return f"action:{focused.id or ''}"
        if isinstance(focused, SessionDashboardTable):
            idx = focused.cursor_row
            if idx is None or idx < 0 or idx >= len(self._dashboard_rows):
                return ""
            row = self._dashboard_rows[idx]
            # Remote rows carry ``host=peer``; serialise as
            # ``remote:host/user/name`` so a recompose can pin the
            # cursor back onto the right peer's session.
            if row.host is not None:
                return f"remote:{row.host}/{row.user}/{row.name}"
            # Local rows (own + other-user) carry ``host=None``.
            # Other-user rows are tagged with the row's user so a
            # focus restore after recompose lands on the right row
            # rather than colliding with an own-session of the same
            # name.
            if row.user and row.user != self.ctx.current_user:
                return f"other:{row.user}/{row.name}"
            return f"own:{row.name}"
        return ""

    def _focus_key(self, key: str) -> bool:
        if key.startswith("action:"):
            selector = key.removeprefix("action:")
            if not selector:
                return False
            try:
                self.query_one(f"#{selector}", ActionRow).focus()
                return True
            except Exception:
                return False
        if key.startswith("own:"):
            return self._focus_dashboard_own(key.removeprefix("own:"))
        if key.startswith("other:"):
            user, _, session_name = key.removeprefix("other:").partition("/")
            return self._focus_dashboard_other(user, session_name)
        if key.startswith("remote:"):
            return self._focus_remote_key(key.removeprefix("remote:"))
        return False

    def _focus_remote_key(self, suffix: str) -> bool:
        """Restore focus on a dashboard remote row keyed by ``host/user/name``.

        Suffix shape mirrors :meth:`_current_focus_key`: ``host/user/name``.
        Falls back gracefully when the dashboard is not mounted, the row
        no longer exists (peer dropped the session between the focus
        capture and the restore), or the suffix is malformed.
        """
        host, _, rest = suffix.partition("/")
        user, _, name = rest.partition("/")
        if not (host and name):
            return False
        try:
            table = self.query_one("#sessions-dashboard", SessionDashboardTable)
        except Exception:
            return False
        for idx, row in enumerate(self._dashboard_rows):
            if row.host != host or row.name != name:
                continue
            if user and row.user and row.user != user:
                continue
            table.focus()
            table.move_cursor(row=idx)
            return True
        return False

    def _focus_dashboard_own(self, session_name: str) -> bool:
        """Restore focus on the dashboard row matching ``session_name``.

        The dashboard model is owned by ``self._dashboard_rows``; we
        index that tuple instead of the widget's row keys to avoid a
        round-trip through the private DataTable index. Matches own
        rows only — ``row.user`` either equals the current user or
        is empty (legacy adapter fall-through).
        """
        try:
            table = self.query_one("#sessions-dashboard", SessionDashboardTable)
        except Exception:
            return False
        current_user = self.ctx.current_user
        for idx, row in enumerate(self._dashboard_rows):
            if row.host is not None or row.name != session_name:
                continue
            if row.user and row.user != current_user:
                continue
            table.focus()
            table.move_cursor(row=idx)
            return True
        return False

    def _focus_dashboard_other(self, user: str, session_name: str) -> bool:
        """Restore focus on the dashboard row matching ``user/session_name``.

        Mirrors :meth:`_focus_dashboard_own` for other-user rows folded
        into the dashboard in commit 11. ``user`` must be non-empty —
        an own-user focus key never serialises through this path.
        """
        if not (user and session_name):
            return False
        try:
            table = self.query_one("#sessions-dashboard", SessionDashboardTable)
        except Exception:
            return False
        for idx, row in enumerate(self._dashboard_rows):
            if row.host is None and row.user == user and row.name == session_name:
                table.focus()
                table.move_cursor(row=idx)
                return True
        return False

    def _update_status_line(self) -> None:
        line = main_status_line(
            self.ctx.server_status,
            self.ctx.link_health_status,
            self.ctx.refresh_tick,
            loading=self.ctx.loading,
        )
        status = self.query_one("#server-status", Static)
        status.update(line.text)
        status.set_class(line.alert, "-alert")

    def _focus_default_action(self) -> None:
        try:
            self.query_one("#action-cwd", ActionRow).focus()
        except Exception:  # pragma: no cover
            pass

    # ── Convenience ──────────────────────────────────────────────────

    def segments(self) -> tuple[int, int, int, int, bool]:
        """Expose segment indices for tests / digit-jump math."""
        return _segments(self.ctx)

    def action_count(self) -> int:
        return ACTION_COUNT
