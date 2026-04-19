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
    _digit_hinted_indices,
    _segments,
    _total_items,
)
from ..widgets import ActionRow, SessionTable


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
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit", show=True),
        Binding("escape", "quit", "Quit", show=False),
        Binding("f1", "help", "Help", show=False),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("d", "kill", "Kill", show=True),
        Binding("D", "kill_all_own", "Kill-ALL (mine)", show=True),
        # Digit 1-9 jump — resolver guards Settings / Kill-ALL.
        Binding("1", "digit_jump(1)", "1-9 jump", show=True),
        Binding("2", "digit_jump(2)", "", show=False),
        Binding("3", "digit_jump(3)", "", show=False),
        Binding("4", "digit_jump(4)", "", show=False),
        Binding("5", "digit_jump(5)", "", show=False),
        Binding("6", "digit_jump(6)", "", show=False),
        Binding("7", "digit_jump(7)", "", show=False),
        Binding("8", "digit_jump(8)", "", show=False),
        Binding("9", "digit_jump(9)", "", show=False),
    ]

    def __init__(self, ctx: TuiContext) -> None:
        super().__init__()
        self.ctx = ctx

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="main-scroll"):
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
            if self.ctx.sessions:
                yield Static("── sessions ──", classes="segment-header")
                yield SessionTable(id="sessions-own")
            if self.ctx.has_sudo:
                yield Static("── superuser ──", classes="segment-header")
                if self.ctx.other_sessions:
                    yield SessionTable(show_user=True, id="sessions-other")
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

    # ── ActionRow.Activated dispatcher ───────────────────────────────

    def on_action_row_activated(self, event: ActionRow.Activated) -> None:
        """Route an :class:`ActionRow.Activated` to the right handler.

        Modal chains are stubbed here and wired through in T14.
        ``CallbackError`` from any callback renders as a red toast.
        """
        kind = event.row.kind
        if kind == "action-cwd":
            self._launch_cwd()
        elif kind == "action-new":
            self._launch_new()
        elif kind == "action-open":
            self._launch_existing()
        elif kind == "settings":
            # T15 replaces this with push_screen(SettingsScreen(...)).
            self.app.notify("TODO Settings screen (T15)")
        elif kind == "kill-all-global":
            self._kill_all_global()

    # ── DataTable row activation (Enter on a SessionTable row) ───────

    def on_data_table_row_selected(self, event) -> None:  # type: ignore[no-untyped-def]
        """Enter/click on a session row attaches to that session."""
        table = event.data_table
        if not isinstance(table, SessionTable):
            return
        session = table.session_at(event.cursor_row)
        if session is None:
            return
        user = session.user or self.ctx.current_user
        try:
            req = self.ctx.on_attach(user, session.name)
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
        # Permissions modal arrives in T10/T14. For now pick regular.
        dsp = False
        self.app.notify("TODO permissions modal (T10/T14)")
        try:
            req = self.ctx.on_launch_cwd(dsp)
        except CallbackError as exc:
            self.app.notify(str(exc), severity="error", timeout=6)
            return
        self.app.request_launch(req)  # type: ignore[attr-defined]

    def _launch_new(self) -> None:
        # Name + git + permissions chain arrives in T14.
        self.app.notify("TODO new-project modal chain (T11/T12/T10 → T14)")
        try:
            req = self.ctx.on_launch_new("placeholder-name", False, "")
        except CallbackError as exc:
            self.app.notify(str(exc), severity="error", timeout=6)
            return
        self.app.request_launch(req)  # type: ignore[attr-defined]

    def _launch_existing(self) -> None:
        if not self.ctx.existing_projects:
            self.app.notify(
                f"No projects in {self.ctx.new_project_root}",
                severity="warning",
                timeout=4,
            )
            return
        self.app.notify("TODO existing-project picker modal (T13/T10 → T14)")
        try:
            req = self.ctx.on_launch_existing(self.ctx.existing_projects[0], False)
        except CallbackError as exc:
            self.app.notify(str(exc), severity="error", timeout=6)
            return
        self.app.request_launch(req)  # type: ignore[attr-defined]

    def _kill_all_global(self) -> None:
        total = len(self.ctx.sessions) + len(self.ctx.other_sessions)
        if total == 0:
            return
        self.app.notify(f"TODO confirm kill-all-global ({total}) (T14)")
        try:
            self.ctx.on_kill_all_global()
            self.app.notify(f"Killed all {total} sessions (all users)")
        except CallbackError as exc:
            self.app.notify(
                f"Kill all (global) failed: {exc}", severity="error", timeout=6
            )
            return
        self.action_refresh()

    # ── Core bindings ────────────────────────────────────────────────

    def action_quit(self) -> None:
        self.app.quit_rc = 0  # type: ignore[attr-defined]
        self.app.exit()

    def action_help(self) -> None:
        self.app.notify(
            "Enter/click activates a row.  d kills selected, D kills all own, r refreshes."
        )

    def action_refresh(self) -> None:
        try:
            self.ctx = self.ctx.on_refresh()
        except CallbackError as exc:
            self.app.notify(f"Refresh failed: {exc}", severity="error", timeout=6)
            return
        # Full re-compose: swap the top screen with a fresh MainScreen.
        new_screen = MainScreen(self.ctx)
        self.app.switch_screen(new_screen)

    def action_kill(self) -> None:
        """Kill the session under focus.

        Modal-confirm flow lands in T14. For T7c we stub the confirm
        with a ``notify`` and always proceed — pilot tests mock the
        ``on_kill`` callback to assert plumbing.
        """
        focused = self.focused
        if not isinstance(focused, SessionTable):
            self.app.notify("Select a session first.", severity="warning")
            return
        row = focused.cursor_row
        session = focused.session_at(row)
        if session is None:
            return
        user = session.user or self.ctx.current_user
        self.app.notify(f"TODO confirm kill {session.name} (user={user}) (T14)")
        try:
            self.ctx.on_kill(user, session.name)
            self.app.notify(f"Killed {session.short}")
        except CallbackError as exc:
            self.app.notify(
                f"Kill {session.short} failed: {exc}", severity="error", timeout=6
            )
            return
        self.action_refresh()

    def action_kill_all_own(self) -> None:
        if not self.ctx.sessions:
            return
        self.app.notify(
            f"TODO confirm kill-all-own ({len(self.ctx.sessions)}) (T14)"
        )
        try:
            self.ctx.on_kill_all()
            self.app.notify(f"Killed all {len(self.ctx.sessions)} sessions")
        except CallbackError as exc:
            self.app.notify(f"Kill all failed: {exc}", severity="error", timeout=6)
            return
        self.action_refresh()

    # ── Digit-jump ───────────────────────────────────────────────────

    def action_digit_jump(self, n: int) -> None:
        """Jump to (and activate) the item hinted by digit ``n``.

        Guard (ported verbatim from ``DigitJumpGuardTests``): on an
        empty-superuser state, digit ACTION_COUNT+1 lands on Settings /
        Kill-ALL, which must NOT auto-activate — it's a "move cursor
        only" row, reachable by arrow-down + Enter. Same rule applies
        in the textual flavour via :func:`_digit_hinted_indices`.
        """
        idx = n - 1
        total = _total_items(self.ctx)
        if idx < 0 or idx >= total:
            return
        allowed = _digit_hinted_indices(self.ctx)
        if idx in allowed:
            self._activate_index(idx)
            return
        # Digit pointed at Settings or Kill-ALL — move focus there but
        # don't auto-activate.
        own_start, other_start, settings_idx, kill_idx, has_super = _segments(self.ctx)
        if has_super and idx in (settings_idx, kill_idx):
            self._focus_index(idx)
            self.app.notify(
                "Press Enter to open Settings / Kill-ALL (digit moves cursor only)"
            )

    def _activate_index(self, idx: int) -> None:
        """Resolve index into a concrete item and fire its activation."""
        own_start, other_start, settings_idx, kill_idx, has_super = _segments(self.ctx)
        self._focus_index(idx)
        if idx < own_start:
            if idx == 0:
                self._launch_cwd()
            elif idx == 1:
                self._launch_new()
            elif idx == 2:
                self._launch_existing()
            return
        if idx < other_start:
            session = self.ctx.sessions[idx - own_start]
            try:
                req = self.ctx.on_attach(self.ctx.current_user, session.name)
            except CallbackError as exc:
                self.app.notify(f"Attach failed: {exc}", severity="error", timeout=6)
                return
            self.app.request_launch(req)  # type: ignore[attr-defined]
            return
        if has_super and idx < settings_idx:
            session = self.ctx.other_sessions[idx - other_start]
            try:
                req = self.ctx.on_attach(session.user, session.name)
            except CallbackError as exc:
                self.app.notify(f"Attach failed: {exc}", severity="error", timeout=6)
                return
            self.app.request_launch(req)  # type: ignore[attr-defined]
            return

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

    # ── Convenience ──────────────────────────────────────────────────

    def segments(self) -> tuple[int, int, int, int, bool]:
        """Expose segment indices for tests / digit-jump math."""
        return _segments(self.ctx)

    def action_count(self) -> int:
        return ACTION_COUNT
