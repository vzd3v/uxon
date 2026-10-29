"""Confirmation and outcome rendering for destructive session actions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from uxon.domain.session import BulkKillResult, KillResult, SessionIdentity

from ..dashboard.model import remote_session_available
from ..widgets.session_list_view import SessionListView
from .confirm import ConfirmPhrase, ConfirmYesNo

if TYPE_CHECKING:
    from .main import MainScreen


class KillFlow:
    """Capture identities before displaying any confirmation."""

    def __init__(self, host: MainScreen) -> None:
        self.host = host

    def _result(self, result: KillResult | BulkKillResult) -> None:
        host = self.host
        if isinstance(result, BulkKillResult):
            host.app.notify(
                result.summary, severity="information" if result.ok else "error", timeout=6
            )
            for row in result.results:
                if row.error:
                    host.app.notify(
                        f"{row.identity.name} ({row.identity.session_id}): {row.error}",
                        severity="error",
                        timeout=8,
                    )
        else:
            label = f"{result.identity.name} ({result.identity.session_id})"
            host.app.notify(
                f"{result.action}: {label}" + (f": {result.error}" if result.error else ""),
                severity="information" if result.ok else "error",
                timeout=6,
            )
        host.app.kick_refresh()  # type: ignore[attr-defined]

    def _error(self, exc: Exception) -> None:
        self.host.app.notify(f"Kill failed: {exc}", severity="error", timeout=8)
        self.host.app.kick_refresh()  # type: ignore[attr-defined]

    def kill(self) -> None:
        host = self.host
        focused = host.focused
        if not isinstance(focused, SessionListView):
            host.app.notify("Select a session first.", severity="warning")
            return
        idx = focused.cursor_row
        if idx is None or not 0 <= idx < len(host._dashboard_rows):
            return
        row = host._dashboard_rows[idx]
        expected = row.identity
        if expected is None or not expected.launch_nonce:
            host.app.notify(
                "Cannot safely remove this session: launch identity unavailable. Drain older sessions or remove them explicitly with tmux.",
                severity="error",
                timeout=8,
            )
            return
        remote_host = row.host
        label = f"{expected.name} ({expected.session_id}, user={expected.user})"
        if remote_host:
            label += f" on {remote_host}"

        def confirmed(ok: bool | None) -> None:
            if not ok:
                return
            if remote_host is not None:
                if not remote_session_available(
                    host.state, remote_host, expected.user, expected.name, expected
                ):
                    host.app.notify("Remote session is no longer available.", severity="warning")
                    return
                remote_name = remote_host

                def callback():
                    return host.cfg.on_remote_kill(remote_name, expected)
            else:

                def callback():
                    return host.cfg.on_kill(expected)

            host.app.run_off_loop(  # type: ignore[attr-defined]
                callback,
                on_success=self._result,
                on_error=self._error,
                label="kill",
            )

        host.app.push_screen(ConfirmYesNo(f"Kill {label}?"), confirmed)

    def _bulk(self, *, reachable: bool) -> None:
        host = self.host
        rows = tuple(host.cfg.sessions) + (tuple(host.cfg.other_sessions) if reachable else ())
        if not rows:
            return
        if any(row.identity is None for row in rows):
            host.app.notify(
                "Cannot safely remove all sessions: some launch identities are unavailable. Drain older sessions or remove them explicitly with tmux.",
                severity="error",
                timeout=8,
            )
            return
        targets: tuple[SessionIdentity, ...] = tuple(
            row.identity for row in rows if row.identity is not None
        )
        users = ", ".join(sorted({target.user for target in targets}))

        def confirmed(ok: bool | None) -> None:
            if not ok:
                return
            fn = host.cfg.on_kill_all_reachable if reachable else host.cfg.on_kill_all
            host.app.run_off_loop(  # type: ignore[attr-defined]
                lambda: fn(targets),
                on_success=self._result,
                on_error=self._error,
                label="kill_all",
            )

        host.app.push_screen(
            ConfirmPhrase(
                f"Kill ALL {len(targets)} selected sessions? [users: {users}]",
                "kill-all-reachable" if reachable else "kill-all",
            ),
            confirmed,
        )

    def kill_all_own(self) -> None:
        self._bulk(reachable=False)

    def kill_all_global(self) -> None:
        self._bulk(reachable=True)
