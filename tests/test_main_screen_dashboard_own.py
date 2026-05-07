"""Pilot tests for the unified session-dashboard widget on ``MainScreen``.

Standalone Pilot — these scenarios verify that ``MainScreen`` mounts
the new :class:`SessionDashboardTable` (commit 10), populates own rows
through the dashboard model, and dispatches kill/attach correctly when
the dashboard is focused. The structural mutators here
(``_refresh_dashboard``, ``action_kill`` widening, attach dispatch) are
exercised in a way the batched smoke harness in
``test_uxon_tui_screens.py`` does not, and isolation is part of the
assertion: each scenario starts with a fresh app + state so the
dashboard's identity-stable model cache is reset.
"""

from __future__ import annotations

import unittest


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _mk_ctx(**overrides):
    """Build a minimal :class:`TuiContext` for dashboard Pilot tests.

    Mirrors the helper in ``test_uxon_tui_screens.py`` — the shape of
    fields the dashboard / model selector reads is the only thing that
    matters here. Refresh sources are wired so ``kick_refresh`` is a
    no-op (no fake worker fires), keeping the test deterministic; we
    drive ``state.main`` by hand.
    """
    from uxon.tui.context import LaunchRequest, TuiContext

    base = dict(
        sessions=[],
        total_cpu="0",
        total_ram="0",
        version="0.12.0",
        cwd="/srv/work",
        cwd_short="work",
        new_project_root="/srv/work",
        existing_projects=[],
        cwd_writable=True,
        current_user="devagent",
        on_launch_cwd=lambda agent_id, mode_id: LaunchRequest(cmd=("/bin/true",), label="cwd"),
        on_launch_new=lambda n, agent_id, mode_id, g: LaunchRequest(
            cmd=("/bin/true",), label="new"
        ),
        on_launch_existing=lambda n, agent_id, mode_id: LaunchRequest(
            cmd=("/bin/true",), label="existing"
        ),
    )
    base.update(overrides)
    ctx = TuiContext(**base)
    ctx.refresh_sources = []
    return ctx


def _own_session(name: str = "devagent.foo", short: str = "foo"):
    from uxon.tui.context import TuiSession

    return TuiSession(
        name=name,
        short=short,
        attached=False,
        pid="1",
        cpu="1.0",
        ram="1M",
        created="1s",
        last_activity="1s",
        cmd="claude",
        path="/srv/work",
        user="devagent",
    )


def _seed_state_main(app, ctx) -> None:
    """Inject a :class:`MainData` snapshot from ``ctx`` into ``app.state``.

    The dashboard model selector reads from ``state.main``; the App's
    rebuild dispatcher writes it on every ``main_ctx_rebuild`` landing.
    Tests that don't run the worker (deterministic Pilot path) seed it
    by hand.
    """
    from uxon.tui.main_data import MainData

    app.state.main = MainData.from_context(ctx)


@unittest.skipUnless(_textual_available(), "textual not installed")
class DashboardOwnTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_widget_mounts_with_dashboard_id(self) -> None:
        """``#sessions-dashboard`` is mounted unconditionally.

        Cold-start: ``state.main`` is ``None``; the dashboard mounts
        empty and ``#sessions-note`` shows the loading copy.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        ctx = _mk_ctx(loading=True)
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            widget = app.screen.query_one("#sessions-dashboard", SessionDashboardTable)
            self.assertEqual(widget.row_count, 0)
            # Empty-note visible (no own rows yet, ctx.loading=True).
            from textual.widgets import Static

            note = app.screen.query_one("#sessions-note", Static)
            self.assertNotIn("-hidden", note.classes)

    async def test_refresh_dashboard_populates_own_rows(self) -> None:
        """``state.main`` lands → dashboard widget shows the row.

        The bridge filter discards anything where ``host is not None``
        or ``user != current_user``. Sanity-check the count here; the
        column tuple is verified at construction time elsewhere.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        ctx = _mk_ctx(sessions=[_own_session()])
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _seed_state_main(app, ctx)
            app.screen._refresh_dashboard()
            widget = app.screen.query_one("#sessions-dashboard", SessionDashboardTable)
            self.assertEqual(widget.row_count, 1)
            self.assertEqual(len(app.screen._dashboard_rows), 1)
            self.assertEqual(app.screen._dashboard_rows[0].name, "devagent.foo")
            # Empty-note hidden once a row lands.
            from textual.widgets import Static

            note = app.screen.query_one("#sessions-note", Static)
            self.assertIn("-hidden", note.classes)

    async def test_action_kill_dispatches_on_kill(self) -> None:
        """``d`` on a focused dashboard row calls ``ctx.on_kill(user, name)``.

        Confirm-modal answered with ``y``. Verifies that the cursor →
        ``_dashboard_rows[idx]`` mapping resolves to the correct
        ``(user, name)`` pair and the local kill callback is wired.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        kill_calls: list[tuple[str, str]] = []

        def fake_kill(user: str, name: str) -> None:
            kill_calls.append((user, name))

        ctx = _mk_ctx(sessions=[_own_session()], on_kill=fake_kill)
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _seed_state_main(app, ctx)
            app.screen._refresh_dashboard()
            # Suppress action_refresh — it would re-call kick_refresh
            # and noisily push a second worker; we only care about
            # the kill dispatch here.
            app.screen.action_refresh = lambda: None
            widget = app.screen.query_one("#sessions-dashboard", SessionDashboardTable)
            widget.focus()
            widget.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
        self.assertEqual(kill_calls, [("devagent", "devagent.foo")])

    async def test_enter_on_dashboard_dispatches_on_attach(self) -> None:
        """Enter on a focused dashboard row calls ``ctx.on_attach(user, name)``.

        The bridge dispatch goes through ``_attach_session`` which
        invokes ``ctx.on_attach`` and hands the request to
        ``app.request_launch``. We swap ``request_launch`` for a
        recorder so the test does not actually exit the App.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.context import LaunchRequest
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        attach_calls: list[tuple[str, str]] = []

        def fake_attach(user: str, name: str) -> LaunchRequest:
            attach_calls.append((user, name))
            return LaunchRequest(cmd=("/bin/true",), label=f"attach {name}")

        ctx = _mk_ctx(sessions=[_own_session()], on_attach=fake_attach)
        app = UxonApp(ctx, probe_agents=False)
        launch_requests: list[LaunchRequest] = []
        app.request_launch = launch_requests.append  # type: ignore[method-assign]
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _seed_state_main(app, ctx)
            app.screen._refresh_dashboard()
            widget = app.screen.query_one("#sessions-dashboard", SessionDashboardTable)
            widget.focus()
            widget.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        self.assertEqual(attach_calls, [("devagent", "devagent.foo")])
        self.assertEqual(len(launch_requests), 1)
        self.assertEqual(launch_requests[0].label, "attach devagent.foo")

    async def test_cursor_pinned_across_no_op_refresh(self) -> None:
        """A no-op refresh tick leaves the cursor on the same row.

        Scenario: dashboard has two rows; the user moves the cursor to
        row 1; ``_refresh_dashboard`` runs again with the same model.
        Cursor must stay at row 1, not snap back to 0.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        sessions = [_own_session(name="devagent.a", short="a")]
        sessions.append(_own_session(name="devagent.b", short="b"))
        ctx = _mk_ctx(sessions=sessions)
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _seed_state_main(app, ctx)
            app.screen._refresh_dashboard()
            widget = app.screen.query_one("#sessions-dashboard", SessionDashboardTable)
            widget.focus()
            widget.move_cursor(row=1)
            await pilot.pause()
            self.assertEqual(widget.cursor_row, 1)
            # Second refresh — same model — cursor stays.
            app.screen._refresh_dashboard()
            await pilot.pause()
            self.assertEqual(widget.cursor_row, 1)


if __name__ == "__main__":
    unittest.main()
