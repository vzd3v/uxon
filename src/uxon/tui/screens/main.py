"""MainScreen — the top-level menu rendered by :class:`UxonApp`.

Layout:
    ┌ Header ──────────────────────────────────────────┐
    │ ActionRow action-cwd                             │
    │ ActionRow action-new                             │
    │ ActionRow action-open                            │
    │ ── sessions ──                                   │
    │ SessionTable (own sessions)                      │
    │ ── superuser ──                                  │
    │ SessionTable (other sessions, show_user=True)    │
    │ ActionRow settings                               │
    │ ActionRow kill-all-global                        │
    └ Footer ──────────────────────────────────────────┘

T7a ships layout + core bindings (q/escape/f1/d/D/r). Digit-jump
arrives in T7b; activation wiring in T7c. The screen holds a
reference to the current :class:`TuiContext` and refreshes it on ``r``.
"""

from __future__ import annotations

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
from ..events import debug as _debug
from ..state import (
    MainIntent,
    activate_main_index,
    digit_jump_intent,
    main_action_intent,
    main_status_line,
    select_layout_signature,
    select_remote_health_badge,
    select_remote_rows,
    session_intent,
    visible_detected_agents,
)
from ..widgets import ActionRow, DetectedAgentsBanner, RemoteSessionTable, SessionTable
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
        margin-top: 1;
    }
    .empty-note {
        color: $text-muted;
        padding: 1 2;
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

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit", show=True),
        Binding("escape", "quit", "Quit", show=False),
        Binding("f1", "help", "Help", show=False),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("d", "kill", "Kill", show=True),
        Binding("D", "kill_all_own", "Kill-ALL (mine)", show=True),
        Binding("k", "kill_remote", "Kill remote", show=True),
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
    ]

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
            show_agent = len(self.ctx.enabled_agents) > 1
            if self.ctx.sessions:
                yield Static("── sessions ──", classes="segment-header")
                yield SessionTable(show_agent_column=show_agent, id="sessions-own")
            if bool(self.ctx.sudo_caps.reachable_users):
                yield Static(self._superuser_header(), classes="segment-header")
                if self.ctx.other_sessions:
                    yield SessionTable(
                        show_user=True, show_agent_column=show_agent, id="sessions-other"
                    )
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
            if not self.ctx.sessions and not (
                bool(self.ctx.sudo_caps.reachable_users) and self.ctx.other_sessions
            ):
                note = "Loading sessions…" if self.ctx.loading else "No active sessions."
                yield Static(note, id="sessions-note", classes="empty-note")
            # Multi-host: a separate Remote-sessions block. Rendered
            # only when at least one peer is configured (an empty
            # ctx.remote_hosts skips the section entirely so a
            # single-host operator sees no extra UI). The HOST column
            # is added when more than one peer is configured — the
            # one-host case prints just the table; the host name is
            # implicit from the section header.
            if self.ctx.remote_hosts:
                show_host = len(self.ctx.remote_hosts) > 1
                yield Static(
                    self._remote_header(),
                    classes="segment-header",
                    id="remote-section-header",
                )
                # Lazy: the table is empty at first paint anyway (the
                # first SSH tick has not landed yet); deferring its mount
                # frees the first frame for the local-sessions content.
                yield Lazy(RemoteSessionTable(show_host=show_host, id="sessions-remote"))
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

    def _remote_header(self) -> str:
        # Single-host case: peer name + "(own only)" badge if the peer
        # rejected --all-users (enable_all_users_list = false there) +
        # health badge ([ok] / [cache 12s] / [err: …] / [loading]).
        # Reads through the slot store: ``state.remote[name].value`` is
        # the live :class:`RemoteSnapshot` (or ``None`` until the first
        # landing). Multi-host case puts scope/health badges on the
        # per-row HOST column in ``_flatten_remote_rows`` instead.
        if len(self.ctx.remote_hosts) == 1:
            host = self.ctx.remote_hosts[0]
            state = getattr(self.app, "state", None)
            slot = state.remote.get(host.name) if state is not None else None
            snap = slot.value if slot is not None else None
            scope = (
                " (own only)" if snap is not None and getattr(snap, "scope_limited", False) else ""
            )
            health = select_remote_health_badge(host.name, snap)
            return f"── remote sessions ── {host.name}{scope} [{health.text}]"
        return f"── remote sessions ── {len(self.ctx.remote_hosts)} hosts"

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
        if self.ctx.sessions:
            try:
                self.query_one("#sessions-own", SessionTable).populate(self.ctx.sessions)
            except Exception:  # pragma: no cover — defensive
                pass
        if bool(self.ctx.sudo_caps.reachable_users) and self.ctx.other_sessions:
            try:
                self.query_one("#sessions-other", SessionTable).populate(self.ctx.other_sessions)
            except Exception:  # pragma: no cover — defensive
                pass
        if self.ctx.remote_hosts:
            try:
                self._populate_remote_table()
            except Exception:  # pragma: no cover — defensive
                pass
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

    # ── Remote sessions block (multi-host) ────────────────────────────

    def _flatten_remote_rows(self) -> list[tuple[str, dict]]:
        """Flatten the per-host slot store into a list the table can render.

        Thin shim over :func:`select_remote_rows` — the pure selector
        keys on ``id(slot.value)``, so an unchanged-value tick returns
        the same tuple object and downstream Textual code can
        ``is``-compare to skip a re-render. We return a fresh ``list``
        each call (the table's ``populate`` mutates it).

        Stub-app safety: tests that build a ``_FakeApp`` without a
        ``state`` attribute fall through to an empty result —
        equivalent to "no slots have landed yet", which is a valid
        zero state.
        """
        state = getattr(self.app, "state", None)
        if state is None:
            return []
        return list(select_remote_rows(state, self.ctx.remote_hosts))

    def _populate_remote_table(self) -> None:
        table = self.query_one("#sessions-remote", RemoteSessionTable)
        table.populate(self._flatten_remote_rows())

    def _refresh_remote_section_header(self, host_name: str) -> None:
        """Update the single-host remote-section header in place.

        Stage 8 commit 11: split out from the (deprecated)
        ``apply_remote_snapshot`` path. Used by the App-level
        dispatcher to refresh only the header text after a single
        peer's slot landing — the ``(own only)`` badge depends on
        the newly-landed scope flag, which isn't visible to the
        coalesced row dispatch.
        """
        if not self.ctx.remote_hosts or len(self.ctx.remote_hosts) != 1:
            return
        try:
            self.query_one("#remote-section-header", Static).update(self._remote_header())
        except Exception:  # pragma: no cover — header not yet mounted
            pass

    def _dispatch_remote_rows(self, old_rows: tuple, new_rows: tuple) -> None:
        """Apply a coalesced row-tuple change to the remote table.

        Stage 8 commit 11. ``old_rows`` and ``new_rows`` come from
        ``select_remote_rows`` invocations across two refresh cycles.
        We diff by host (the bare host name is the first
        space-delimited token of the first row tuple element) and
        dispatch ``update_host_rows`` only for hosts whose row list
        actually changed. Unchanged hosts produce zero
        ``add_row`` / ``remove_row`` calls — pinned by the
        ``test_only_changed_host_touched`` regression test.
        """
        if not self.ctx.remote_hosts:
            return
        try:
            table = self.query_one("#sessions-remote", RemoteSessionTable)
        except Exception:  # pragma: no cover — table not yet mounted
            return

        def _group_by_host(rows: tuple) -> dict[str, list[tuple]]:
            grouped: dict[str, list[tuple]] = {}
            for display_name, rec in rows:
                host = display_name.split(" ", 1)[0]
                grouped.setdefault(host, []).append((display_name, rec))
            return grouped

        old_by_host = _group_by_host(old_rows)
        new_by_host = _group_by_host(new_rows)
        # Hosts that disappeared from the new tuple → drop their rows.
        for host in old_by_host:
            if host not in new_by_host:
                table.update_host_rows(host, [])
        # Hosts whose rows changed (or are new).
        for host, rows in new_by_host.items():
            if old_by_host.get(host) != rows:
                table.update_host_rows(host, rows)

    def apply_remote_snapshot(self, host_name: str, snapshot) -> None:
        """Hook called by the app dispatch when a per-host SourceSpec
        result lands. Re-renders the rows for one peer.

        Stage 8 commit 4: the canonical store is ``state.remote`` —
        the dispatcher (``UxonApp._handle_remote_snapshot``) writes
        through ``slot_state.apply`` *before* calling this method,
        so the screen only triggers a repaint here. The repaint is
        per-host: we update only the rows for ``host_name`` via
        :meth:`RemoteSessionTable.update_host_rows`, leaving every
        other peer's rows untouched. The single-host section header
        still re-renders because its ``(own only)`` badge depends on
        the freshly-landed scope flag.
        """
        if not self.ctx.remote_hosts:
            return
        try:
            table = self.query_one("#sessions-remote", RemoteSessionTable)
        except Exception:  # pragma: no cover — table not yet mounted
            return
        rows: list[tuple[str, dict]] = []
        if snapshot is not None:
            multi_host = len(self.ctx.remote_hosts) > 1
            display_name = host_name
            if multi_host:
                if bool(getattr(snapshot, "scope_limited", False)):
                    display_name = f"{display_name} (own only)"
                badge = select_remote_health_badge(host_name, snapshot)
                display_name = f"{display_name} [{badge.text}]"
            for rec in snapshot.sessions:
                rows.append((display_name, rec))
        table.update_host_rows(host_name, rows)
        if len(self.ctx.remote_hosts) == 1:
            try:
                self.query_one("#remote-section-header", Static).update(self._remote_header())
            except Exception:  # pragma: no cover — header not yet mounted
                pass

    # ── ActionRow.Activated dispatcher ───────────────────────────────

    def on_action_row_activated(self, event: ActionRow.Activated) -> None:
        """Route an :class:`ActionRow.Activated` to the right handler.

        Modal chains are stubbed here and wired through in T14.
        ``CallbackError`` from any callback renders as a red toast.
        """
        self._run_intent(main_action_intent(event.row.kind))

    # ── DataTable row activation (Enter on a SessionTable row) ───────

    def on_data_table_row_selected(self, event) -> None:  # type: ignore[no-untyped-def]
        """Enter/click on a session row attaches to that session."""
        table = event.data_table
        if not isinstance(table, SessionTable):
            return
        session = table.session_at(event.cursor_row)
        if session is None:
            return
        self._run_intent(session_intent(session, self.ctx.current_user))

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

        try:
            own_table = self.query_one("#sessions-own", SessionTable)
        except Exception:
            own_table = None
        if own_table is not None:
            own_table.populate(self.ctx.sessions)

        try:
            other_table = self.query_one("#sessions-other", SessionTable)
        except Exception:
            other_table = None
        if other_table is not None:
            other_table.populate(self.ctx.other_sessions)

        # Remote-sessions table is owned by the per-host SSH workers via
        # ``apply_remote_snapshot``. The local ctx-rebuild path does not
        # contribute rows to it (the snapshots dict is carried across
        # rebuilds, see ``apply_loaded_ctx``), so re-populating here on
        # every local tick would clear+re-add identical rows and produce
        # a visible flicker.

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

        # Update the "Loading sessions…" / "No active sessions." placeholder
        # in place when present (only rendered when both lists are empty).
        try:
            note = self.query_one("#sessions-note", Static)
        except Exception:
            note = None
        if note is not None:
            note.update("Loading sessions…" if self.ctx.loading else "No active sessions.")

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
        """Confirm then kill the session under focus."""
        focused = self.focused
        if not isinstance(focused, SessionTable):
            self.app.notify("Select a session first.", severity="warning")
            return
        row = focused.cursor_row
        session = focused.session_at(row)
        if session is None:
            return
        user = session.user or self.ctx.current_user

        def after_confirm(ok: bool | None) -> None:
            if not ok:
                return
            try:
                self.ctx.on_kill(user, session.name)
                self.app.notify(f"Killed {session.short}")
            except CallbackError as exc:
                self.app.notify(
                    f"Kill {session.short} failed: {exc}",
                    severity="error",
                    timeout=6,
                )
                return
            self.action_refresh()

        self.app.push_screen(
            ConfirmYesNo(f"Kill {session.name} (user={user})?"),
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

    def action_kill_remote(self) -> None:
        """Confirm then kill the remote session under focus.

        Only fires when focus sits on the :class:`RemoteSessionTable`.
        Resolves the row to ``(host_name, record)`` via
        :meth:`RemoteSessionTable.row_at` and dispatches via
        ``ctx.on_remote_kill(host, user, name)`` — the local CLI runs
        ``uxon kill --force --host <h> --user <u> <name>`` over SSH on
        the peer. The peer's own ``uxon kill`` does the per-target
        sudo gating; the local TUI never needs the peer's user table.

        After a successful kill we kick the existing refresh — the
        per-host poller will repull on its next cadence tick. There
        is no force-single-host repoll path today; the cadence is
        seconds, not minutes, so the lag is short.
        """
        focused = self.focused
        if not isinstance(focused, RemoteSessionTable):
            self.app.notify("Select a remote session first.", severity="warning")
            return
        row = focused.cursor_row
        entry = focused.row_at(row)
        if entry is None:
            return
        host_name, record = entry
        # Strip any trailing ``" (own only)"`` badge from the displayed
        # host name so the dispatcher receives the bare ``RemoteHost.name``
        # — the badge is a TUI display detail.
        clean_host = host_name.split(" ", 1)[0]
        user = str(record.get("user") or "").strip()
        name = str(record.get("name") or "").strip()
        if not user or not name:
            self.app.notify(
                "Remote row is missing user/name; cannot dispatch kill.",
                severity="error",
                timeout=6,
            )
            return

        def after_confirm(ok: bool | None) -> None:
            if not ok:
                return
            try:
                self.ctx.on_remote_kill(clean_host, user, name)
                self.app.notify(
                    f"Killed {name} on {clean_host}; remote table will update on next poll"
                )
            except CallbackError as exc:
                self.app.notify(
                    f"Remote kill failed: {exc}",
                    severity="error",
                    timeout=6,
                )
                return
            self.action_refresh()

        self.app.push_screen(
            ConfirmYesNo(f"Kill {name} on {clean_host} (user={user})?"),
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
        own_start, other_start, settings_idx, kill_idx, has_super = _segments(self.ctx)
        try:
            if idx < own_start:
                action_ids = ("action-cwd", "action-new", "action-open")
                self.query_one(f"#{action_ids[idx]}", ActionRow).focus()
                return
            if idx < other_start:
                t = self.query_one("#sessions-own", SessionTable)
                t.move_cursor(row=idx - own_start)
                t.focus()
                return
            if has_super and idx < settings_idx:
                t = self.query_one("#sessions-other", SessionTable)
                t.move_cursor(row=idx - other_start)
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
        if isinstance(focused, SessionTable):
            session = focused.session_at(focused.cursor_row)
            if session is None:
                return ""
            if focused.id == "sessions-other":
                return f"other:{session.user}/{session.name}"
            return f"own:{session.name}"
        if isinstance(focused, RemoteSessionTable):
            entry = focused.row_at(focused.cursor_row)
            if entry is None:
                return ""
            host_name, rec = entry
            # Strip any trailing ``" (own only)"`` badge — focus identity
            # is the bare host name, mirroring ``action_kill_remote``.
            clean_host = host_name.split(" ", 1)[0]
            user = str(rec.get("user") or "")
            name = str(rec.get("name") or "")
            return f"remote:{clean_host}/{user}/{name}"
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
            return self._focus_session_key("#sessions-own", key.removeprefix("own:"))
        if key.startswith("other:"):
            _, _, session_name = key.removeprefix("other:").partition("/")
            return self._focus_session_key("#sessions-other", session_name)
        if key.startswith("remote:"):
            return self._focus_remote_key(key.removeprefix("remote:"))
        return False

    def _focus_remote_key(self, suffix: str) -> bool:
        """Restore focus on a ``RemoteSessionTable`` row keyed by ``host/user/name``.

        Suffix shape mirrors :meth:`_current_focus_key`: ``host/user/name``.
        Falls back gracefully when the table is not mounted, the row no
        longer exists (peer dropped the session between the focus
        capture and the restore), or the suffix is malformed.
        """
        host, _, rest = suffix.partition("/")
        user, _, name = rest.partition("/")
        if not (host and name):
            return False
        try:
            table = self.query_one("#sessions-remote", RemoteSessionTable)
        except Exception:
            return False
        for idx, (row_host, rec) in enumerate(table._row_index):
            clean_host = row_host.split(" ", 1)[0]
            if clean_host != host:
                continue
            if name and str(rec.get("name") or "") != name:
                continue
            if user and str(rec.get("user") or "") != user:
                continue
            table.focus()
            table.move_cursor(row=idx)
            return True
        return False

    def _focus_session_key(self, selector: str, session_name: str) -> bool:
        try:
            table = self.query_one(selector, SessionTable)
        except Exception:
            return False
        for idx, session in enumerate(table._session_index):
            if session.name == session_name:
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
