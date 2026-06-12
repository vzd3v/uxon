"""MainScreen — the top-level menu rendered by :class:`UxonApp`.

Layout (one flat visual language — every action is a single-line
ActionRow; the session table is the dominant element):
    ┌ Header ──────────────────────────────────────────┐
    │ server-status line                               │
    │ ActionRow action-cwd   (detail: cwd)             │
    │ ActionRow action-new   (detail: project root)    │
    │ ActionRow action-open  (detail: project root)    │
    │ SessionListView (own + other-user + remote rows; │
    │   USER column iff cross_user, HOST column iff    │
    │   multi_host; height hugs content)               │
    │ FleetStatusBar                                   │
    │ ActionRow settings          (superuser only)     │
    │ ActionRow kill-all-global   (superuser only)     │
    └ Footer ──────────────────────────────────────────┘

T7a shipped layout + core bindings (q/f1/d/D/r); 3.4 dropped
``escape → quit`` and added layout-invariant JCUKEN twins. Digit-jump
arrives in T7b; activation wiring in T7c. The screen holds a
reference to the current :class:`TuiContext` and refreshes it on ``r``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Header, Static

from uxon.infra.events import debug as _debug

from ..context import (
    ACTION_COUNT,
    TuiContext,
    _segments,
)
from ..dashboard.layout import LayoutFlags, build_active_columns
from ..dashboard.seen_users import collect_user_set, cross_user_latched
from ..dashboard.ui_state import (
    DashboardUiState,
    MainScreenUiState,
    next_block_start,
    set_filter,
    set_view_mode,
    step_tab_index,
    toggle_view_mode,
)
from ..keymap import bindings_with_aliases
from ..state import (
    MainIntent,
    activate_main_index,
    focus_key_for_row,
    main_action_intent,
    main_status_line,
    parse_focus_key,
)
from ..widgets import ActionRow
from ..widgets.fleet_status_bar import FleetStatusBar
from ..widgets.gated_footer import GatedFooter
from ..widgets.host_status_bar import HostStatusBar
from ..widgets.host_tab_strip import HostTabActivated, HostTabStrip
from ..widgets.search_bar import FilterChanged, SearchBar
from ..widgets.session_list_view import SessionListView
from . import main_render
from .dashboard_render import DashboardRender
from .kill_flow import KillFlow
from .launch_flow import LaunchFlow

if TYPE_CHECKING:
    from uxon.domain.status import LinkHealthStatus

    from ..dashboard.columns import ColumnSpec
    from ..dashboard.row import SessionRow
    from ..tui_state import TuiState


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
        height: 1;
        text-wrap: nowrap;
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
        Binding("h", "toggle_hosts", "Hosts", show=True),
        Binding("s", "focus_search", "Search", show=True),
        Binding("/", "focus_search", "", show=False),
        # Arrow navigation across focusable widgets. DataTable consumes
        # up/down internally for row navigation, so these only fire when
        # focus sits on an ActionRow — crossing widget boundaries.
        Binding("up", "app.focus_previous", "", show=False),
        Binding("down", "app.focus_next", "", show=False),
    )

    # Writable cold-start flag the rebuild dispatcher flips when the
    # first ``MainData`` lands (``screen.loading = state.main is None``).
    # Plain assignment only — a ``compute_loading`` method would make the
    # reactive read-only (textual/reactive.py:330-333).
    #
    # NOTE: this name shadows Textual's built-in ``Widget.loading``,
    # whose watcher would otherwise cover the WHOLE screen with a
    # ``LoadingIndicator`` (a blue full-screen dots overlay). We don't
    # want that — the cold-start view is the composed skeleton (server
    # status line + "Loading sessions…" note + action rows), not a blank
    # overlay. ``set_loading`` below is overridden to a no-op so the flag
    # stays a plain reactive and never engages Textual's overlay. (Before
    # that override, the in-place refresh path could leave the cover
    # stuck up: the cold-start cover is mounted via a mount-time
    # ``call_later`` that could land *after* the data-arrival
    # ``loading = False``, re-covering a screen nothing would uncover —
    # the whole UI froze on the loader. The old ``switch_screen`` refresh
    # masked it by always building a fresh, uncovered screen.)
    loading: reactive[bool] = reactive(True)

    def set_loading(self, loading: bool) -> None:  # noqa: ARG002 — overrides Textual hook
        """No-op override of Textual's loading-overlay hook.

        See the ``loading`` reactive note above: MainScreen renders its
        own cold-start skeleton, so it must never be replaced by the
        built-in full-screen ``LoadingIndicator``. Severing the cover
        hook keeps the ``loading`` flag a plain writable reactive while
        making Textual's overlay machinery inert for this screen.
        """
        return

    def __init__(self, cfg: TuiContext, state: TuiState) -> None:
        super().__init__()
        # ``cfg`` is the static rebuild snapshot (sessions, cwd,
        # sudo_caps, callbacks, …) — swapped wholesale on each rebuild
        # tick by :meth:`apply_loaded_ctx`. ``state`` is the App-owned
        # live :class:`TuiState`; its identity is stable across rebuild
        # ticks. The three async-slot fields the screen renders
        # (``refresh_tick``, ``link_health``, ``cwd_writable``) are read
        # from ``state`` directly — there is no read-through proxy on
        # ``cfg`` any more.
        self.cfg = cfg
        self.state = state
        # Controllers hold the heavy bodies of the launch/kill/refresh
        # flows; the screen's ``action_*``/``on_*`` handlers stay thin
        # delegators (AGENTS.md). Each reaches back into this screen via
        # ``host=self`` so per-build state has one source of truth.
        self._launch_flow = LaunchFlow(self)
        self._kill_flow = KillFlow(self)
        self._dashboard = DashboardRender(self)
        self.loading = bool(cfg.loading)
        self._restore_focus_key = ""
        # Primary repo root resolved by the launch-flow workspace probe
        # (§4.2). Threaded into LaunchOptionsScreen + the workspace-choice
        # dispatch so neither re-resolves the repo root on the event loop.
        self._workspace_repo_root = ""
        # Dashboard rows are an in-flight repaint cache, not UI state —
        # they're rebuilt from ``state`` on every tick, so dying with
        # the screen on recompose is harmless. Filter/view/tab state
        # is different: it lives on ``self.app.main_ui`` (see
        # :class:`MainScreenUiState`) so a layout-signature flip
        # doesn't silently snap the operator back to defaults.
        self._dashboard_rows: tuple[SessionRow, ...] = ()
        # Active dashboard columns, recomputed every refresh and rebuilt
        # on the live table when they change (USER column on the
        # ``cross_user`` latch, HOST column on ``multi_host``) — see
        # :meth:`_compute_active_columns` and
        # :meth:`DashboardRender.refresh_dashboard`. The latch never
        # resets, so the column doesn't flicker when a foreign user's
        # sessions die or filtering narrows the row set.
        self._active_columns = self._compute_active_columns()
        # ``(text, alert)`` of the last status-line write — the change gate
        # for :meth:`_update_status_line`. ``None`` until the first write.
        self._status_last: tuple[str, bool] | None = None

    def _main_ui_or_empty(self) -> MainScreenUiState:
        """Return ``app.main_ui`` if reachable, otherwise a fresh empty
        :class:`MainScreenUiState`.

        ``MainScreen.__init__`` runs before Textual attaches the
        screen to the App in some unit tests (``MainScreen.__new__``
        construction) and before App readiness on the very first
        mount. Falling back to an empty UI state in those windows is
        equivalent to "the latch has not been observed yet" — the
        feeder fills it on the next dispatch and the next recompose
        picks up the real value.
        """
        try:
            ui = self.app.main_ui  # type: ignore[attr-defined]
        except Exception:
            return MainScreenUiState()
        if isinstance(ui, MainScreenUiState):
            return ui
        return MainScreenUiState()

    def _compute_active_columns(self) -> tuple[ColumnSpec, ...]:
        """Active dashboard column tuple for the current cfg + latch state.

        Recomputed on every refresh (not just at ``__init__``) so the
        USER column (``cross_user`` latch) and HOST column
        (``multi_host``) can be mounted on the *live* table in place —
        see :meth:`DashboardRender.refresh_dashboard`. Pure; cheap to
        recompute and equality-compared by the caller.
        """
        flags = LayoutFlags(
            multi_host=bool(self.cfg.remote_hosts),
            cross_user=cross_user_latched(self._main_ui_or_empty()),
        )
        return build_active_columns(cfg_columns=self.cfg.tui_table_columns, flags=flags)

    # ── Live async-slot reads ─────────────────────────────────────────
    #
    # These read the App-owned :class:`TuiState` slots directly. They
    # replicate exactly what the deleted ``TuiContext`` read-through
    # properties did: prefer the live slot once a probe has landed,
    # otherwise fall back to the static seed carried on ``cfg``.

    def _link_health_now(self) -> LinkHealthStatus:
        """Current SSH-link health: live slot, else the ``cfg`` seed."""
        value = self.state.link_health.value
        if value is not None:
            return value
        return self.cfg.link_health_status

    def _cwd_writable_now(self) -> bool | None:
        """Current cwd-writable flag with three-valued semantics.

        Returns the live slot value once any probe has landed
        (``last_attempt_at is not None``); otherwise the static seed on
        ``cfg`` (``None`` when the probe is still in flight / never ran).
        """
        if self.state.cwd_writable.last_attempt_at is not None:
            return self.state.cwd_writable.value
        return self.cfg.cwd_writable

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

    @property
    def _hosts_expanded(self) -> bool:
        return self.app.main_ui.hosts_expanded  # type: ignore[attr-defined]

    @_hosts_expanded.setter
    def _hosts_expanded(self, value: bool) -> None:
        self.app.main_ui.hosts_expanded = value  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="main-body"):
            line = main_status_line(
                self.cfg.server_status,
                self._link_health_now(),
                self.state.refresh_tick,
                loading=self.cfg.loading,
            )
            yield Static(line.text, id="server-status", classes="-alert" if line.alert else "")
            # Launch actions — a vertical stack of flat single-line
            # rows (one visual language with the superuser rows below).
            # ↑/↓ walk the stack via the standard focus chain; per-row
            # detail (cwd / project root) renders inline as a dim
            # suffix after the label.
            yield ActionRow(
                kind="action-cwd",
                label="New session in current folder",
                detail=self._cwd_detail(),
                enabled=self._cwd_writable_now() is not False,
                id="action-cwd",
            )
            yield ActionRow(
                kind="action-new",
                label="Create new project",
                detail=main_render.new_project_detail(self.cfg.new_project_root),
                enabled=True,
                id="action-new",
            )
            yield ActionRow(
                kind="action-open",
                label="Open existing project",
                detail=self._open_detail(),
                # While loading we don't yet know whether projects exist.
                # Keep the row enabled so it isn't dimmed; activation falls
                # through to the existing "no projects" notify path.
                enabled=self.cfg.loading or bool(self.cfg.existing_projects),
                id="action-open",
            )
            # Unified dashboard. The table's own column header labels
            # the block — no section header. The dashboard is mounted
            # unconditionally; empty-state copy is rendered by the
            # ``#sessions-note`` Static above it (toggled by class in
            # ``_refresh_dashboard_note``). Local own + local
            # other-user + remote per-host rows all flow through this
            # one widget; the USER column appears when ``cross_user``
            # is set, the HOST column when ``multi_host`` is set.
            yield SearchBar(id="search-bar")
            note = "Loading sessions…" if self.cfg.loading else "No active sessions."
            note_classes = "empty-note"
            if self.cfg.sessions or self.cfg.other_sessions:
                # Dashboard will populate momentarily — start hidden so
                # the layout doesn't flicker an empty-note at first paint.
                note_classes = "empty-note -hidden"
            yield Static(note, id="sessions-note", classes=note_classes)
            # Tab strip + compact status bar are always mounted regardless
            # of the initial view mode; ``_refresh_dashboard`` toggles
            # ``display`` so the widget tree stays stable across ``v`` flips.
            # Mounting only when ``view_mode == "by_host"`` would silently
            # break ``v`` for operators who configure ``default_view =
            # "flat"`` (the toggle would have no widgets to show). The
            # compact bar shows the active host's detail under its tab in
            # by_host; the fleet bar below the table carries the
            # collapsed/expanded fleet summary in both views.
            yield HostTabStrip([], id="host-tabs")
            yield HostStatusBar(mode="compact", id="host-status-compact")
            yield SessionListView(columns=self._active_columns, id="sessions-dashboard")
            # Fleet status: below the table, before the superuser block, so
            # arrowing down off the table lands on it first. Collapsed by
            # default; ``h`` (or a click on the bar) expands it.
            yield FleetStatusBar(id="fleet-status")
            # Superuser block (Settings + Kill-ALL-global) is mounted
            # UNCONDITIONALLY and shown/hidden via ``display`` in
            # ``refresh_dashboard`` — the same stable-tree idiom the tab
            # strip / status bars use above. Mounting it conditionally on
            # ``has_super`` meant a sudo-reachability flip changed the
            # widget tree, which forced ``apply_loaded_ctx`` down the
            # ``switch_screen`` path — a full screen swap that destroys
            # the focused widget and drops any in-flight key events. The
            # initial ``display`` is set here so non-super sessions never
            # flash the block before the first refresh tick. No section
            # header: the rows' own colours (settings amber, kill red)
            # mark the privileged block; the partial-reachability hint
            # rides on the kill row's detail.
            has_super = bool(self.cfg.sudo_caps.reachable_users)
            total_sessions = len(self.cfg.sessions) + len(self.cfg.other_sessions)
            settings_row = ActionRow(
                kind="settings",
                label="⚙ Settings",
                detail="(repo-level config.toml)",
                enabled=True,
                id="action-settings",
            )
            settings_row.display = has_super
            yield settings_row
            # Visible whenever has_super (dimmed at 0 sessions) so the
            # partial-reachability hint in its detail never disappears —
            # 0 visible sessions is exactly when "a colleague may be
            # unreachable" matters most.
            kill_global_row = ActionRow(
                kind="kill-all-global",
                label=main_render.kill_all_global_label(total_sessions),
                detail=self._kill_all_global_detail(),
                enabled=total_sessions > 0,
                id="action-kill-all-global",
            )
            kill_global_row.display = has_super
            yield kill_global_row
            # Multi-host: remote rows fold into the unified dashboard
            # above. The HOST column is auto-prepended when the
            # ``multi_host`` LayoutFlag is True (data-driven via
            # ``cfg.remote_hosts`` in ``__init__``). No separate widget
            # / section header is needed — per-host attribution lives on
            # each row's HOST cell + the dashboard's host colour glyph.
        yield GatedFooter()

    # Thin Textual-side shells over the pure builders in
    # :mod:`uxon.tui.screens.main_render` — the "what text to show" logic
    # is fast-tested there; these adapt the live ``cfg``/slot reads.

    def _kill_all_global_detail(self) -> str:
        return main_render.kill_all_global_detail(
            self.cfg.sudo_caps.reachable_users,
            self.cfg.scope_skipped_users,
        )

    def _cwd_detail(self) -> str:
        return main_render.cwd_detail(
            cwd_writable=self._cwd_writable_now(),
            cwd_short=self.cfg.cwd_short,
            launch_user=self.cfg.launch_user,
            current_user=self.cfg.current_user,
        )

    def _open_detail(self) -> str:
        return main_render.open_detail(
            loading=self.cfg.loading,
            new_project_root=self.cfg.new_project_root,
        )

    def on_mount(self) -> None:
        # Initial dashboard apply. ``state.main`` may still be ``None``
        # at this point (cold-start skeleton ctx) — ``_refresh_dashboard``
        # treats that as a zero-row tick and toggles the empty-note in
        # response. ``#sessions-dashboard`` (the unified
        # :class:`SessionListView`) owns row display end-to-end.
        self._refresh_dashboard()
        self.call_after_refresh(self._update_status_line)
        if self._restore_focus_key and self._focus_key(self._restore_focus_key):
            # Closest proxy to "first frame painted" without a renderer
            # hook: by the end of ``on_mount`` widgets are mounted and
            # the next event-loop tick paints them.
            import time as _time  # noqa: PLC0415

            _debug("startup", at="first_paint", ts=_time.monotonic())
            return
        self.call_later(self._focus_default_action)
        import time as _time  # noqa: PLC0415

        _debug("startup", at="first_paint", ts=_time.monotonic())

    def on_show(self) -> None:
        if not self._restore_focus_key:
            self.call_later(self._focus_default_action)

    # ── Dashboard ────────────────────────────────────────────────────

    def _refresh_dashboard(self) -> None:
        self._dashboard.refresh_dashboard()

    # ── ActionRow.Activated dispatcher ───────────────────────────────

    def on_action_row_activated(self, event: ActionRow.Activated) -> None:
        """Route an :class:`ActionRow.Activated` to the right handler.

        ``CallbackError`` from any callback renders as a red toast.
        """
        self._run_intent(main_action_intent(event.row.kind))

    # ── DataTable row activation (Enter on a session row) ───────────

    def on_session_list_view_row_selected(self, event: SessionListView.RowSelected) -> None:
        """Enter/click on a session row attaches to that session.

        SessionListView rows dispatch the right callback based on
        ``row.host``: local rows (``host is None``) go through
        ``ctx.on_attach``; remote rows go through ``ctx.on_remote_attach``
        (SSH). The message carries ``cursor_row``; the row list is owned
        by ``self._dashboard_rows`` (kept in lockstep by the refresh).
        """
        idx = event.cursor_row
        if idx is None or idx < 0 or idx >= len(self._dashboard_rows):
            return
        self._launch_flow.attach_row(self._dashboard_rows[idx])

    def _run_intent(self, intent: MainIntent | None) -> None:
        self._launch_flow.run_intent(intent)

    def _attach_session(self, user: str, session_name: str) -> None:
        self._launch_flow.attach_session(user, session_name)

    def _maybe_show_session_choice(
        self,
        *,
        target_dir: str,
        target_label: str,
        agent_id: str,
        on_new,
        probe=None,
    ) -> None:
        self._launch_flow.maybe_show_session_choice(
            target_dir=target_dir,
            target_label=target_label,
            agent_id=agent_id,
            on_new=on_new,
            probe=probe,
        )

    def _begin_launch_in_folder(
        self,
        *,
        target_dir: str,
        target_label: str,
        commit_primary,
        launchable: bool | None = None,
        on_probed=None,
    ) -> None:
        self._launch_flow.begin_launch_in_folder(
            target_dir=target_dir,
            target_label=target_label,
            commit_primary=commit_primary,
            launchable=launchable,
            on_probed=on_probed,
        )

    def _launch_cwd(self) -> None:
        self._launch_flow.launch_cwd()

    def _launch_new(self) -> None:
        self._launch_flow.launch_new()

    def _launch_existing(self) -> None:
        self._launch_flow.launch_existing()

    def _open_settings(self) -> None:
        self._launch_flow.open_settings()

    def _kill_all_global(self) -> None:
        self._kill_flow.kill_all_global()

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
        new_mode = toggle_view_mode(self._dashboard_ui.view_mode)
        # The tab strip is hidden in flat mode. If focus is currently
        # inside the strip, ``_refresh_dashboard`` is about to strand
        # it on a ``display: none`` widget — move it to the dashboard
        # table now and remember the position so we can return focus
        # to the active tab when the strip reappears.
        was_on_strip = self._focus_in_tab_strip()
        if new_mode == "flat" and was_on_strip:
            try:
                self.query_one("#sessions-dashboard", SessionListView).focus()
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

    def action_toggle_hosts(self) -> None:
        self._hosts_expanded = not self._hosts_expanded
        self._refresh_dashboard()

    def on_fleet_status_bar_toggled(self, event: FleetStatusBar.Toggled) -> None:
        # A click on the bar routes through the same shared state as the
        # ``h`` binding so the two can never diverge.
        self.action_toggle_hosts()

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
        new_idx = step_tab_index(strip.active_index, len(strip._buckets), -1)
        if new_idx is not None:
            strip.active_index = new_idx

    def action_next_tab(self) -> None:
        try:
            strip = self.query_one("#host-tabs", HostTabStrip)
        except Exception:
            return
        new_idx = step_tab_index(strip.active_index, len(strip._buckets), +1)
        if new_idx is not None:
            strip.active_index = new_idx

    def on_session_list_view_host_navigate(
        self,
        event: SessionListView.HostNavigate,
    ) -> None:
        """Route ←/→ on the dashboard.

        by_host (no active search): cycle the active host tab cyclically.
        flat — or by_host forced flat by an active search filter: jump
        the cursor to the first row of the next/previous block (own /
        other-user / per-host remote). The search-active branch is
        critical: a non-empty filter renders flat *while* ``view_mode``
        stays ``by_host`` (see ``_refresh_dashboard``); without this
        guard ←/→ would silently cycle the hidden tab strip and
        clearing the search would land on an unexpected tab.
        """
        forced_flat = bool(self._dashboard_ui.filter_text.strip())
        if self._dashboard_ui.view_mode == "by_host" and not forced_flat:
            if event.direction < 0:
                self.action_prev_tab()
            else:
                self.action_next_tab()
            return
        try:
            table = self.query_one("#sessions-dashboard", SessionListView)
        except Exception:
            return
        target = next_block_start(table.block_starts, table.cursor_row, event.direction)
        if target is not None:
            table.move_cursor(row=target)

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

    def _refresh_cwd_row(self) -> None:
        """Re-render the cwd action row from the current cwd-writable flag."""
        try:
            row = self.query_one("#action-cwd", ActionRow)
        except Exception:  # pragma: no cover — DOM not mounted yet
            return
        row.detail = self._cwd_detail()
        row.set_enabled(self._cwd_writable_now() is not False)
        row._render_text()

    def _apply_ctx_refresh(self) -> bool:
        return self._dashboard.apply_ctx_refresh()

    def apply_loaded_ctx(self, new_cfg: TuiContext, *, focus_key: str | None = None) -> None:
        """Swap the screen's static snapshot in. Patches in place if the
        layout signature matches, otherwise switches to a freshly composed
        MainScreen.

        ``new_cfg`` is the fresh rebuild snapshot (the value
        ``on_refresh()`` produced); the live :class:`TuiState` is
        unchanged and shared with the App.

        ``focus_key=None`` (default) captures the currently focused widget
        before the swap so it can be restored after. Pass ``""`` to skip
        focus restoration entirely (used by initial mount).
        """
        _debug(
            "refresh",
            at="apply_loaded_ctx",
            action="enter",
            sessions=len(new_cfg.sessions),
            other=len(new_cfg.other_sessions),
            tick=self.state.refresh_tick,
        )
        if focus_key is None:
            focus_key = self._current_focus_key()
        # cwd-change invalidation: reset ``state.cwd_writable`` to its
        # zero state so the row paints "checking…" until the next
        # probe lands. Worker-side gating
        # (``_CwdWritableUpdated.cwd_at_start``) drops in-flight
        # probes that started against the old cwd. Operates on the
        # App-owned state container so the reset is observed by the
        # probe handlers; some unit-test FakeApps lack ``state``.
        app_state = getattr(self.app, "state", None)
        if app_state is not None and new_cfg.cwd != self.cfg.cwd:
            from uxon.tui.slot_state import SlotState as _SlotState

            app_state.cwd_writable = _SlotState[bool | None]()
        self.cfg = new_cfg
        # The reactive descriptor needs Textual node setup (``_id``);
        # test stubs that bypass ``__init__`` (``MainScreen.__new__``)
        # would hit ReactiveError on assignment, so guard with
        # ``hasattr`` rather than try/except.
        if hasattr(self, "_id"):
            self.loading = bool(new_cfg.loading)
        # Keep app.ctx in lockstep so the probe worker's reads target the
        # same static snapshot that screens read from (``app._probe_cwd_*``
        # gates against ``self.app.ctx.cwd``).
        self.app.ctx = new_cfg  # type: ignore[attr-defined]
        # Defensive feed: walk the current state (with ``state.main``
        # synced from ``new_ctx``) and fold any new users into the
        # cross-user latch accumulator. Production already feeds via
        # :meth:`UxonApp._handle_remote_snapshot` for remote ticks, but
        # this site closes two extra paths:
        #
        # 1. Direct callers that bypass the dispatcher (some unit
        #    tests call ``apply_loaded_ctx`` with a fresh ``new_ctx``
        #    without ever running a refresh source).
        # 2. The ``main_ctx_rebuild`` tick itself — the local-rebuild
        #    handler only wires ``state.main``; this is where the
        #    accumulator picks up the new local own / other-user
        #    rows it produced.
        #
        # ``state.main = MainData.from_context(new_cfg)`` is idempotent
        # with the dispatcher's earlier write in production; the sync
        # keeps the dashboard model aligned with ``self.cfg`` for the
        # in-place patch path below regardless of who called us.
        main_ui = getattr(self.app, "main_ui", None)
        if isinstance(main_ui, MainScreenUiState) and app_state is not None:
            from uxon.tui.main_data import MainData as _MainData

            app_state.main = _MainData.from_context(new_cfg)
            main_ui.seen_users |= collect_user_set(app_state)
        # In-place patch is the ONLY refresh path. Every structural delta
        # the layout signature used to gate is now reconciled in place by
        # ``_apply_ctx_refresh`` → ``refresh_dashboard``:
        #   * superuser block (header / Settings / Kill-ALL-global) →
        #     ``display`` toggle on the always-mounted widgets;
        #   * USER / HOST columns (cross_user latch / multi_host) →
        #     live ``SessionListView.set_columns`` rebuild.
        # So a background refresh never destroys the focused widget, and
        # keys in Textual's input queue always have a live target. This
        # is the fix for keys vanishing mid-refresh: the old
        # ``switch_screen`` swap tore down the DOM and dropped any
        # in-flight key events aimed at it.
        if self._apply_ctx_refresh():
            _debug("keys", at="apply_loaded_ctx", path="patch", focus_key=focus_key)
            if focus_key and self._focus_key(focus_key):
                return
            # Don't yank focus when the in-place patch leaves the focused
            # widget intact.
            return
        # Fallback only: the action-row DOM genuinely isn't ready (not a
        # routine refresh — e.g. a half-mounted screen). The full swap is
        # the safety net; it can drop in-flight keys, but it fires only
        # when the in-place path cannot run at all, never on a normal
        # signature change.
        _debug(
            "keys",
            at="apply_loaded_ctx",
            path="switch_screen_fallback",
            focus_key=focus_key,
        )
        new_screen = MainScreen(self.cfg, self.state)
        new_screen._restore_focus_key = focus_key
        self.app.switch_screen(new_screen)

    def action_kill(self) -> None:
        """Confirm then kill the session under focus.

        Only fires on :class:`SessionListView`
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
        self._kill_flow.kill()

    def action_kill_all_own(self) -> None:
        self._kill_flow.kill_all_own()

    def _activate_index(self, idx: int) -> None:
        """Resolve index into a concrete item and fire its activation."""
        self._run_intent(activate_main_index(self.cfg, idx))

    def _focus_index(self, idx: int) -> None:
        """Move focus to the widget backing ``idx`` on the current screen."""
        own_start, _other_start, settings_idx, kill_idx, has_super = _segments(self.cfg)
        # Own + other-user share the dashboard, so the local segment
        # ends at ``settings_idx`` (when sudo) or
        # ``own_start + len(ctx.sessions)`` (no sudo — no other-user
        # segment at all).
        local_end = settings_idx if has_super else own_start + len(self.cfg.sessions)
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
                t = self.query_one("#sessions-dashboard", SessionListView)
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
        # Pure encode lives in :func:`focus_key_for_row`; the cursor-row
        # read off the live widget stays here (Textual half).
        focused = self.focused
        if isinstance(focused, ActionRow):
            return f"action:{focused.id or ''}"
        if isinstance(focused, SessionListView):
            idx = focused.cursor_row
            if idx is None or idx < 0 or idx >= len(self._dashboard_rows):
                return ""
            row = self._dashboard_rows[idx]
            return focus_key_for_row(
                host=row.host,
                user=row.user,
                name=row.name,
                current_user=self.cfg.current_user,
            )
        return ""

    def _focus_key(self, key: str) -> bool:
        # Pure decode lives in :func:`parse_focus_key`; the
        # ``query``/``focus``/``move_cursor`` half stays here.
        parsed = parse_focus_key(key)
        if parsed.kind == "action":
            if not parsed.selector:
                return False
            try:
                row = self.query_one(f"#{parsed.selector}", ActionRow)
            except Exception:
                return False
            # The superuser rows (#action-settings / #action-kill-all-global)
            # are always mounted and toggled via ``display``; a hidden row
            # is not a valid focus target. Report failure so the caller
            # falls back instead of parking focus on an invisible widget.
            # (Before these rows were always-mounted, this was a query_one
            # miss → False; preserve that contract rather than leaning on
            # Textual's hide-reflow to bounce focus back off.)
            if not row.display:
                return False
            row.focus()
            return True
        if parsed.kind == "own":
            return self._focus_dashboard_own(parsed.name)
        if parsed.kind == "other":
            return self._focus_dashboard_other(parsed.user, parsed.name)
        if parsed.kind == "remote":
            return self._focus_remote_key(parsed.host, parsed.user, parsed.name)
        return False

    def _focus_remote_key(self, host: str, user: str, name: str) -> bool:
        """Restore focus on a dashboard remote row keyed by ``host/user/name``.

        Falls back gracefully when the dashboard is not mounted, the row
        no longer exists (peer dropped the session between the focus
        capture and the restore), or the suffix is malformed.
        """
        if not (host and name):
            return False
        try:
            table = self.query_one("#sessions-dashboard", SessionListView)
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
        is empty.
        """
        try:
            table = self.query_one("#sessions-dashboard", SessionListView)
        except Exception:
            return False
        current_user = self.cfg.current_user
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

        Mirrors :meth:`_focus_dashboard_own` for other-user rows.
        ``user`` must be non-empty — an own-user focus key never
        serialises through this path.
        """
        if not (user and session_name):
            return False
        try:
            table = self.query_one("#sessions-dashboard", SessionListView)
        except Exception:
            return False
        for idx, row in enumerate(self._dashboard_rows):
            if row.host is None and row.user == user and row.name == session_name:
                table.focus()
                table.move_cursor(row=idx)
                return True
        return False

    def _update_status_line(self) -> None:
        """Repaint the ``#server-status`` line, gated on a content change.

        Identical ``(text, alert)`` since the last write → zero widget
        writes (AC8: a no-change link-health landing mutates nothing). A
        glyph-only delta (the spinner advances every tick by construction)
        or a content delta → exactly one ``update(layout=False)`` — the
        fixed ``height: 1`` + ``text-wrap: nowrap`` CSS means the spinner
        repaint never relayouts the screen (AC2/AC3). ``set_class`` fires
        only on an alert flip. The ``batch_update`` covers the link-health
        path, which does not pass through ``_render_dirty`` (AC6).
        """
        line = main_status_line(
            self.cfg.server_status,
            self._link_health_now(),
            self.state.refresh_tick,
            loading=self.cfg.loading,
        )
        status = self.query_one("#server-status", Static)
        prev = self._status_last
        if prev is not None and prev == (line.text, line.alert):
            return
        with self.app.batch_update():
            status.update(line.text, layout=False)
            if prev is None or prev[1] != line.alert:
                status.set_class(line.alert, "-alert")
        self._status_last = (line.text, line.alert)

    def _focus_default_action(self) -> None:
        try:
            self.query_one("#action-cwd", ActionRow).focus()
        except Exception:  # pragma: no cover
            pass

    # ── Convenience ──────────────────────────────────────────────────

    def segments(self) -> tuple[int, int, int, int, bool]:
        """Expose segment indices for tests and intent-routing math."""
        return _segments(self.cfg)

    def action_count(self) -> int:
        return ACTION_COUNT
