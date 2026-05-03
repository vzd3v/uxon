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

    def __init__(self, ctx: TuiContext) -> None:
        super().__init__()
        self.ctx = ctx
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
            yield DetectedAgentsBanner("", id="detected-banner", classes="-hidden")
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
                yield RemoteSessionTable(show_host=show_host, id="sessions-remote")
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
        # rejected --all-users (e.g. enable_all_users_list = false on
        # the peer's config). Multi-host case: badges go onto the
        # per-row HOST column in ``_flatten_remote_rows`` instead.
        if len(self.ctx.remote_hosts) == 1:
            host = self.ctx.remote_hosts[0]
            snap = self.ctx.remote_snapshots.get(host.name)
            badge = (
                " (own only)" if snap is not None and getattr(snap, "scope_limited", False) else ""
            )
            return f"── remote sessions ── {host.name}{badge}"
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
            return
        self.call_later(self._focus_default_action)

    def on_show(self) -> None:
        if not self._restore_focus_key:
            self.call_later(self._focus_default_action)

    # ── Remote sessions block (multi-host) ────────────────────────────

    def _flatten_remote_rows(self) -> list[tuple[str, dict]]:
        """Flatten ``ctx.remote_snapshots`` into a list the table can
        render.

        Iteration follows ``ctx.remote_hosts`` order so the displayed
        order is config-defined, not snapshot-arrival-defined. Within
        a host the session order is whatever the peer reported (the
        wire schema preserves it). Peers whose snapshot reports
        ``scope_limited=True`` (the peer fell back to "own only" because
        ``enable_all_users_list`` is disabled there) get a ``(own only)``
        badge appended to the displayed host name. Single-host case
        puts the badge in the section header instead — see
        :meth:`_remote_header`.
        """
        rows: list[tuple[str, dict]] = []
        multi_host = len(self.ctx.remote_hosts) > 1
        for host in self.ctx.remote_hosts:
            snap = self.ctx.remote_snapshots.get(host.name)
            if snap is None:
                continue
            limited = bool(getattr(snap, "scope_limited", False))
            display_name = f"{host.name} (own only)" if multi_host and limited else host.name
            for rec in snap.sessions:
                rows.append((display_name, rec))
        return rows

    def _populate_remote_table(self) -> None:
        table = self.query_one("#sessions-remote", RemoteSessionTable)
        table.populate(self._flatten_remote_rows())

    def apply_remote_snapshot(self, host_name: str, snapshot) -> None:
        """Hook called by the app dispatch when a per-host SourceSpec
        result lands.

        Updates ``ctx.remote_snapshots`` in place and re-populates the
        table. Called from ``UxonApp.on__refresh_source_landed`` for
        ``remote:*`` events. Also re-renders the section header so a
        single-host "(own only)" badge appears as soon as the peer's
        ``scope_limited`` flag arrives.
        """
        self.ctx.remote_snapshots[host_name] = snapshot
        if self.ctx.remote_hosts:
            try:
                self._populate_remote_table()
            except Exception:  # pragma: no cover — table not yet mounted
                pass
            # Single-host header carries the (own only) badge — refresh
            # it whenever a snapshot lands. Multi-host header is fixed
            # ("N hosts") and doesn't change.
            if len(self.ctx.remote_hosts) == 1:
                try:
                    from textual.widgets import Static

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
        if self.ctx.cwd_writable is None:
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
        has_super = bool(ctx.sudo_caps.reachable_users)
        return (
            bool(ctx.sessions),
            has_super,
            bool(ctx.other_sessions),
            has_super and (len(ctx.sessions) + len(ctx.other_sessions) > 0),
        )

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

        if self.ctx.remote_hosts:
            try:
                self._populate_remote_table()
            except Exception:  # pragma: no cover — DOM not mounted yet
                pass

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
        new_ctx.link_health_status = self.ctx.link_health_status
        new_ctx.agent_availability = self.ctx.agent_availability
        # detected_agents is mutated in place by the same probe worker;
        # without this carry-over the periodic ctx refresh would clear
        # the suggestion banner one tick after it appeared.
        new_ctx.detected_agents = self.ctx.detected_agents
        new_ctx.refresh_tick = self.ctx.refresh_tick + 1
        # Carry the cwd-writable result across refreshes too: same-user
        # builds set it synchronously, cross-user builds leave None and
        # the probe runs once on mount. Without this, every refresh in
        # the cross-user case would drop back to None until the next
        # probe, flicker-disabling the row.
        if new_ctx.cwd_writable is None and new_ctx.cwd == self.ctx.cwd:
            new_ctx.cwd_writable = self.ctx.cwd_writable
        self.ctx = new_ctx
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
