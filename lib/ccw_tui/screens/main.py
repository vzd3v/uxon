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
        """Delegate kill to T7c wiring — stub for now."""
        self.app.notify("TODO modal kill (T7c)")

    def action_kill_all_own(self) -> None:
        self.app.notify("TODO modal kill-all-own (T7c)")

    # ── Convenience ──────────────────────────────────────────────────

    def segments(self) -> tuple[int, int, int, int, bool]:
        """Expose segment indices for tests / digit-jump math."""
        return _segments(self.ctx)

    def action_count(self) -> int:
        return ACTION_COUNT
