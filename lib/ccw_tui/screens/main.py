"""MainScreen — the top-level menu rendered by :class:`CcwApp`.

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
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from ..context import (
    ACTION_COUNT,
    CallbackError,
    TuiContext,
    _segments,
)
from ..state import (
    MainIntent,
    activate_main_index,
    digit_jump_intent,
    main_status_line,
    main_action_intent,
    session_intent,
)
from ..widgets import ActionRow, SessionTable
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
    #main-scroll {
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
        with VerticalScroll(id="main-scroll"):
            line = main_status_line(
                self.ctx.server_status,
                self.ctx.link_health_status,
                self.ctx.refresh_tick,
            )
            yield Static(line.text, id="server-status", classes="-alert" if line.alert else "")
            # Action rows
            yield ActionRow(
                kind="action-cwd",
                label="New session in current folder",
                detail=self._cwd_detail(),
                digit=1,
                enabled=self.ctx.cwd_allowed,
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
                detail=f"({self.ctx.new_project_root}/…)",
                digit=3,
                enabled=bool(self.ctx.existing_projects),
                id="action-open",
            )
            show_agent = len(self.ctx.enabled_agents) > 1
            if self.ctx.sessions:
                yield Static("── sessions ──", classes="segment-header")
                yield SessionTable(show_agent_column=show_agent, id="sessions-own")
            if self.ctx.has_sudo:
                yield Static("── superuser ──", classes="segment-header")
                if self.ctx.other_sessions:
                    yield SessionTable(show_user=True, show_agent_column=show_agent, id="sessions-other")
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
                    yield ActionRow(
                        kind="kill-all-global",
                        label=f"⚡ Kill ALL ccw sessions (all users, {total_sessions} total)",
                        detail="",
                        digit=None,
                        enabled=True,
                        id="action-kill-all-global",
                    )
            if (
                not self.ctx.sessions
                and not (self.ctx.has_sudo and self.ctx.other_sessions)
            ):
                yield Static("No active sessions.", classes="empty-note")
        yield Footer()

    def _cwd_detail(self) -> str:
        if self.ctx.cwd_allowed:
            return f"({self.ctx.cwd_short})"
        return f"({self.ctx.cwd_short} — not under allowed_roots)"

    def on_mount(self) -> None:
        # VerticalScroll is focusable by default and swallows arrow keys
        # for its own scroll. We want arrow keys to flow through to screen
        # bindings (focus_next/focus_previous), so disable its focusability.
        try:
            scroll = self.query_one("#main-scroll")
            scroll.can_focus = False
        except Exception:  # pragma: no cover — defensive
            pass
        if self.ctx.sessions:
            try:
                self.query_one("#sessions-own", SessionTable).populate(self.ctx.sessions)
            except Exception:  # pragma: no cover — defensive
                pass
        if self.ctx.has_sudo and self.ctx.other_sessions:
            try:
                self.query_one("#sessions-other", SessionTable).populate(
                    self.ctx.other_sessions
                )
            except Exception:  # pragma: no cover — defensive
                pass
        interval = self.ctx.tui_refresh_interval_seconds
        if interval > 0:
            self.set_interval(interval, self._auto_refresh)
        self.call_after_refresh(self._update_status_line)
        for delay in (0.05, 0.2, 0.5):
            self.set_timer(delay, self._prime_initial_frame)
        if self._restore_focus_key and self._focus_key(self._restore_focus_key):
            return
        self.call_later(self._focus_default_action)

    def on_show(self) -> None:
        if not self._restore_focus_key:
            self.call_later(self._focus_default_action)

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
        if not self.ctx.cwd_allowed:
            self.app.notify(
                f"cwd {self.ctx.cwd_short} is not under allowed_roots",
                severity="warning",
                timeout=6,
            )
            return

        def after_opts(result: "tuple[str, str] | None") -> None:
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
            def _on_opts(result: "tuple[str, str] | None") -> None:
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

            def after_opts(result: "tuple[str, str] | None") -> None:
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

        def after_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            try:
                self.ctx.on_kill_all_global()
                self.app.notify(f"Killed all {total} sessions (all users)")
            except CallbackError as exc:
                self.app.notify(
                    f"Kill all (global) failed: {exc}",
                    severity="error",
                    timeout=6,
                )
                return
            self.action_refresh()

        self.app.push_screen(
            ConfirmPhrase(
                f"Kill ALL {total} sessions across ALL users?",
                "kill-all-global",
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
        self._refresh_main()

    def _auto_refresh(self) -> None:
        if self.app.screen is not self:
            return
        self._refresh_main()

    def _layout_signature(self, ctx: TuiContext) -> tuple[bool, bool, bool, bool]:
        return (
            bool(ctx.sessions),
            ctx.has_sudo,
            bool(ctx.other_sessions),
            ctx.has_sudo and (len(ctx.sessions) + len(ctx.other_sessions) > 0),
        )

    def _apply_ctx_refresh(self) -> bool:
        try:
            self.query_one("#action-cwd", ActionRow).detail = self._cwd_detail()
            self.query_one("#action-cwd", ActionRow)._render_text()
            self.query_one("#action-cwd", ActionRow).set_enabled(self.ctx.cwd_allowed)
            open_row = self.query_one("#action-open", ActionRow)
            open_row.detail = f"({self.ctx.new_project_root}/…)"
            open_row.set_enabled(bool(self.ctx.existing_projects))
            self.query_one("#action-new", ActionRow).detail = f"({self.ctx.new_project_root}/…)"
            self.query_one("#action-new", ActionRow)._render_text()
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

        if self.ctx.has_sudo:
            try:
                kill_row = self.query_one("#action-kill-all-global", ActionRow)
            except Exception:
                kill_row = None
            if kill_row is not None:
                total_sessions = len(self.ctx.sessions) + len(self.ctx.other_sessions)
                kill_row.label = f"⚡ Kill ALL ccw sessions (all users, {total_sessions} total)"
                kill_row._render_text()

        self._update_status_line()
        return True

    def _refresh_main(self) -> None:
        focus_key = self._current_focus_key()
        old_signature = self._layout_signature(self.ctx)
        old_link_health = self.ctx.link_health_status
        try:
            new_ctx = self.ctx.on_refresh()
        except CallbackError as exc:
            self.app.notify(f"Refresh failed: {exc}", severity="error", timeout=6)
            return
        new_ctx.link_health_status = old_link_health
        new_ctx.refresh_tick = self.ctx.refresh_tick + 1
        self.ctx = new_ctx
        if self._layout_signature(self.ctx) == old_signature and self._apply_ctx_refresh():
            if focus_key and self._focus_key(focus_key):
                return
            self.call_later(self._focus_default_action)
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

        def after_confirm(ok: bool) -> None:
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

        def after_confirm(ok: bool) -> None:
            if not ok:
                return
            try:
                self.ctx.on_kill_all()
                self.app.notify(f"Killed all {n} sessions")
            except CallbackError as exc:
                self.app.notify(
                    f"Kill all failed: {exc}", severity="error", timeout=6
                )
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
        )
        status = self.query_one("#server-status", Static)
        status.update(line.text)
        status.set_class(line.alert, "-alert")

    def _focus_default_action(self) -> None:
        try:
            self.query_one("#action-cwd", ActionRow).focus()
        except Exception:  # pragma: no cover
            pass

    def _prime_initial_frame(self) -> None:
        try:
            self.query_one("#server-status", Static).refresh()
            self.query_one("#action-new", ActionRow).refresh()
        except Exception:  # pragma: no cover
            pass

    # ── Convenience ──────────────────────────────────────────────────

    def segments(self) -> tuple[int, int, int, int, bool]:
        """Expose segment indices for tests / digit-jump math."""
        return _segments(self.ctx)

    def action_count(self) -> int:
        return ACTION_COUNT
