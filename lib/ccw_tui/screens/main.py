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
        """Resolve index into a concrete item and fire its activation.

        Wiring to real callbacks lands in T7c. For T7b, this is a thin
        focus+notify router so pilot tests can verify the guard logic
        without the full modal chain.
        """
        own_start, other_start, settings_idx, kill_idx, has_super = _segments(self.ctx)
        self._focus_index(idx)
        if idx < own_start:
            kinds = ("action-cwd", "action-new", "action-open")
            self.app.notify(f"digit → {kinds[idx]}")
            return
        if idx < other_start:
            session = self.ctx.sessions[idx - own_start]
            self.app.notify(f"digit → own-session:{session.short}")
            return
        if has_super and idx < settings_idx:
            session = self.ctx.other_sessions[idx - other_start]
            self.app.notify(f"digit → other-session:{session.user}/{session.short}")
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
