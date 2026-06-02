"""KillFlow — the kill-chain controller for :class:`MainScreen`.

Holds the bodies of the confirm-modal kill chains (``action_kill``,
``action_kill_all_own``, ``_kill_all_global``). The Screen keeps the
``action_kill*`` methods as thin delegators with their ``show=True``
destructive bindings intact (AGENTS.md hard rule) — this controller
carries the body only.

Mechanical lift: every ``self.cfg``/``self.app``/``self.action_refresh``
reference is re-routed through ``self.host``; the confirm-modal flow and
``action_refresh`` ordering are preserved exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..context import CallbackError
from ..widgets.session_dashboard_table import SessionDashboardTable
from .confirm import ConfirmPhrase, ConfirmYesNo

if TYPE_CHECKING:
    from .main import MainScreen


class KillFlow:
    """Kill-chain controller bound to one :class:`MainScreen`."""

    def __init__(self, host: MainScreen) -> None:
        self.host = host

    def kill(self) -> None:
        """Confirm then kill the session under focus.

        Resolves via the cursor index into ``host._dashboard_rows``:

        * Local rows (``row.host is None``) dispatch to ``ctx.on_kill``;
          ``row.user`` carries the target user so other-user rows take
          the existing sudo path inside the kill callback.
        * Remote rows (``row.host`` set) dispatch to
          ``ctx.on_remote_kill(host, user, name)`` — the local CLI runs
          ``uxon kill --force --host <h> --user <u> <name>`` over SSH.

        Any other focus target falls through to the "select a session"
        notify.
        """
        host = self.host
        focused = host.focused
        if not isinstance(focused, SessionDashboardTable):
            host.app.notify("Select a session first.", severity="warning")
            return
        idx = focused.cursor_row
        if idx is None or idx < 0 or idx >= len(host._dashboard_rows):
            return
        row = host._dashboard_rows[idx]
        session_user = row.user or host.cfg.current_user
        session_name = row.name
        session_short = row.short or row.name

        if row.host is not None:
            # Remote: dispatch via SSH through ctx.on_remote_kill.
            host_name = row.host

            def after_confirm_remote(ok: bool | None) -> None:
                if not ok:
                    return
                try:
                    host.cfg.on_remote_kill(host_name, session_user, session_name)
                    host.app.notify(
                        f"Killed {session_short} on {host_name}; "
                        "remote table will update on next poll"
                    )
                except CallbackError as exc:
                    host.app.notify(f"Remote kill failed: {exc}", severity="error", timeout=6)
                    return
                host.action_refresh()

            host.app.push_screen(
                ConfirmYesNo(f"Kill {session_name} on {host_name} (user={session_user})?"),
                after_confirm_remote,
            )
            return

        def after_confirm(ok: bool | None) -> None:
            if not ok:
                return
            try:
                host.cfg.on_kill(session_user, session_name)
                host.app.notify(f"Killed {session_short}")
            except CallbackError as exc:
                host.app.notify(
                    f"Kill {session_short} failed: {exc}",
                    severity="error",
                    timeout=6,
                )
                return
            host.action_refresh()

        host.app.push_screen(
            ConfirmYesNo(f"Kill {session_name} (user={session_user})?"),
            after_confirm,
        )

    def kill_all_own(self) -> None:
        host = self.host
        if not host.cfg.sessions:
            return
        n = len(host.cfg.sessions)

        def after_confirm(ok: bool | None) -> None:
            if not ok:
                return
            try:
                host.cfg.on_kill_all()
                host.app.notify(f"Killed all {n} sessions")
            except CallbackError as exc:
                host.app.notify(f"Kill all failed: {exc}", severity="error", timeout=6)
                return
            host.action_refresh()

        host.app.push_screen(
            ConfirmPhrase(f"Kill ALL {n} sessions?", "kill-all"),
            after_confirm,
        )

    def kill_all_global(self) -> None:
        host = self.host
        total = len(host.cfg.sessions) + len(host.cfg.other_sessions)
        if total == 0:
            return
        reachable = sorted(host.cfg.sudo_caps.reachable_users)
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
                host.cfg.on_kill_all_global()
                host.app.notify(f"Killed all {total} sessions across {n_users} reachable users")
            except CallbackError as exc:
                host.app.notify(
                    f"Kill all (reachable) failed: {exc}",
                    severity="error",
                    timeout=6,
                )
                return
            host.action_refresh()

        host.app.push_screen(
            ConfirmPhrase(
                (f"Kill ALL {total} sessions for {n_users} reachable users? [{scope_summary}]"),
                "kill-all-reachable",
            ),
            after_confirm,
        )
