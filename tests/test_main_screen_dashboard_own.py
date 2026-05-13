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
    def setUp(self) -> None:
        # Reset the module-level ``_LAST_OUTPUT`` cache in
        # ``dashboard.model``: the selector identity-stables its return
        # tuple across calls, and that cache survives between tests in
        # the same process. Without this reset, a follow-up test that
        # builds a fresh ctx with the same row content would see the
        # previous test's tuple object — masking real divergence and
        # making cache-hit assertions ambiguous.
        from uxon.tui.dashboard import model as _dashboard_model

        _dashboard_model._LAST_OUTPUT = ()

    def tearDown(self) -> None:
        from uxon.tui.dashboard import model as _dashboard_model

        _dashboard_model._LAST_OUTPUT = ()

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
        (remote rows). Local own + other-user rows both flow through.
        Sanity-check the count here; the column tuple is verified at
        construction time elsewhere.
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

    async def test_action_kill_dispatches_remote_for_remote_dashboard_row(self) -> None:
        """``action_kill`` dispatches via ``ctx.on_remote_kill`` when the
        focused dashboard row carries a non-``None`` host.

        Pilot-level pin: drop a remote :class:`SessionRow` into the
        dashboard, focus row 0, press ``d``, confirm in the modal and
        assert ``on_remote_kill`` was invoked with ``(host, user, name)``
        — and the local ``on_kill`` was NOT.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.dashboard.row import SessionRow
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        local_kill_calls: list[tuple[str, str]] = []
        remote_kill_calls: list[tuple[str, str, str]] = []

        def fake_kill(user: str, name: str) -> None:
            local_kill_calls.append((user, name))

        def fake_remote_kill(host: str, user: str, name: str) -> None:
            remote_kill_calls.append((host, user, name))

        ctx = _mk_ctx(
            sessions=[_own_session()],
            on_kill=fake_kill,
            on_remote_kill=fake_remote_kill,
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _seed_state_main(app, ctx)
            app.screen._refresh_dashboard()
            # Inject a synthetic remote row.
            synthetic_remote_row = SessionRow(
                host="peer1",
                user="devagent",
                name="devagent.remote",
                short="remote",
                agent="claude",
                attached=False,
                legacy=False,
                pid=42,
                cpu_pct=0.0,
                rss_kib=0,
                created_epoch=None,
                last_attached_epoch=None,
                cmd="claude",
                path="/srv/work",
            )
            app.screen._dashboard_rows = (synthetic_remote_row,)
            widget = app.screen.query_one("#sessions-dashboard", SessionDashboardTable)
            widget.focus()
            widget.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            # ConfirmYesNo modal — confirm with y.
            await pilot.press("y")
            await pilot.pause()
        self.assertEqual(local_kill_calls, [])
        self.assertEqual(remote_kill_calls, [("peer1", "devagent", "devagent.remote")])

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


def _other_session(name: str = "alice.bar", short: str = "bar", user: str = "alice"):
    """Other-user :class:`TuiSession` used by the commit-11 sudo flows."""
    from uxon.tui.context import TuiSession

    return TuiSession(
        name=name,
        short=short,
        attached=False,
        pid="2",
        cpu="2.0",
        ram="2M",
        created="2s",
        last_activity="2s",
        cmd="codex",
        path="/srv/work",
        user=user,
    )


@unittest.skipUnless(_textual_available(), "textual not installed")
class DashboardOtherUserTests(unittest.IsolatedAsyncioTestCase):
    """Pilot tests for commit 11: other-user (sudo) rows folded into the dashboard."""

    def setUp(self) -> None:
        from uxon.tui.dashboard import model as _dashboard_model

        _dashboard_model._LAST_OUTPUT = ()

    def tearDown(self) -> None:
        from uxon.tui.dashboard import model as _dashboard_model

        _dashboard_model._LAST_OUTPUT = ()

    async def test_other_user_row_appears_in_dashboard(self) -> None:
        """An ``other_sessions`` row lands in ``#sessions-dashboard``.

        The legacy ``#sessions-other`` widget is no longer mounted —
        verify the dashboard carries both own and other-user rows.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.context import SudoCapability
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        ctx = _mk_ctx(
            sessions=[_own_session()],
            other_sessions=[_other_session()],
            sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _seed_state_main(app, ctx)
            app.screen._refresh_dashboard()
            widget = app.screen.query_one("#sessions-dashboard", SessionDashboardTable)
            self.assertEqual(widget.row_count, 2)
            users = sorted(r.user for r in app.screen._dashboard_rows)
            self.assertEqual(users, ["alice", "devagent"])
            # The legacy ``#sessions-other`` widget must NOT exist —
            # all rows live in the unified dashboard now.
            self.assertEqual(len(app.screen.query("#sessions-other")), 0)

    async def test_sudo_attach_via_dashboard(self) -> None:
        """Enter on an other-user dashboard row dispatches ``ctx.on_attach`` with the row's user."""
        from uxon.tui.app import UxonApp
        from uxon.tui.context import LaunchRequest, SudoCapability
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        attach_calls: list[tuple[str, str]] = []

        def fake_attach(user: str, name: str) -> LaunchRequest:
            attach_calls.append((user, name))
            return LaunchRequest(cmd=("/bin/true",), label=f"attach {name}")

        ctx = _mk_ctx(
            sessions=[],
            other_sessions=[_other_session()],
            sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
            on_attach=fake_attach,
        )
        app = UxonApp(ctx, probe_agents=False)
        launch_requests: list[LaunchRequest] = []
        app.request_launch = launch_requests.append  # type: ignore[method-assign]
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _seed_state_main(app, ctx)
            app.screen._refresh_dashboard()
            widget = app.screen.query_one("#sessions-dashboard", SessionDashboardTable)
            # Locate the row for "alice.bar" — sort_by may reorder.
            target_idx = next(
                i
                for i, r in enumerate(app.screen._dashboard_rows)
                if r.user == "alice" and r.name == "alice.bar"
            )
            widget.focus()
            widget.move_cursor(row=target_idx)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        self.assertEqual(attach_calls, [("alice", "alice.bar")])
        self.assertEqual(len(launch_requests), 1)

    async def test_sudo_kill_via_dashboard(self) -> None:
        """``d`` + confirm on an other-user dashboard row dispatches ``ctx.on_kill`` with the row's user.

        Also asserts the ``ConfirmYesNo`` modal's prompt mentions the
        OTHER user (the row's user), not the operator's current user
        — so the operator visually confirms they're killing alice's
        session, not their own.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.context import SudoCapability
        from uxon.tui.screens.confirm import ConfirmYesNo
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        kill_calls: list[tuple[str, str]] = []

        def fake_kill(user: str, name: str) -> None:
            kill_calls.append((user, name))

        ctx = _mk_ctx(
            sessions=[],
            other_sessions=[_other_session()],
            sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
            on_kill=fake_kill,
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _seed_state_main(app, ctx)
            app.screen._refresh_dashboard()
            app.screen.action_refresh = lambda: None
            # Spy on push_screen so we can read the modal's prompt.
            modal_prompts: list[str] = []
            orig_push_screen = app.push_screen

            def _capture_push(screen, *args, **kwargs):
                if isinstance(screen, ConfirmYesNo):
                    modal_prompts.append(screen.prompt)
                return orig_push_screen(screen, *args, **kwargs)

            app.push_screen = _capture_push  # type: ignore[method-assign]
            widget = app.screen.query_one("#sessions-dashboard", SessionDashboardTable)
            target_idx = next(
                i
                for i, r in enumerate(app.screen._dashboard_rows)
                if r.user == "alice" and r.name == "alice.bar"
            )
            widget.focus()
            widget.move_cursor(row=target_idx)
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
        self.assertEqual(kill_calls, [("alice", "alice.bar")])
        # Prompt must reference the OTHER user (alice), not the
        # current user (devagent). Pin both halves so a regression
        # that reverses the substitution fails fast.
        self.assertEqual(len(modal_prompts), 1)
        prompt = modal_prompts[0]
        self.assertIn("alice", prompt)
        self.assertIn("alice.bar", prompt)
        self.assertNotIn("devagent", prompt)

    async def test_user_column_present_when_cross_user_true(self) -> None:
        """Construction with ``other_sessions`` non-empty includes the USER column."""
        from uxon.tui.app import UxonApp
        from uxon.tui.context import SudoCapability

        ctx = _mk_ctx(
            sessions=[_own_session()],
            other_sessions=[_other_session()],
            sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            ids = tuple(c.id for c in app.screen._active_columns)
            self.assertIn("user", ids)

    async def test_user_column_absent_when_cross_user_false(self) -> None:
        """Construction with no ``other_sessions`` omits the USER column."""
        from uxon.tui.app import UxonApp

        ctx = _mk_ctx(sessions=[_own_session()])
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            ids = tuple(c.id for c in app.screen._active_columns)
            self.assertNotIn("user", ids)

    async def test_recompose_when_cross_user_flips_true(self) -> None:
        """A refresh that lands an other-user row triggers recompose → USER column appears."""
        from uxon.tui.app import UxonApp
        from uxon.tui.context import SudoCapability
        from uxon.tui.screens.main import MainScreen

        ctx = _mk_ctx(
            sessions=[_own_session()],
            other_sessions=[],
            sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            old_screen = app.screen
            self.assertNotIn("user", tuple(c.id for c in old_screen._active_columns))
            # Synthesise the rebuilt ctx with an other-user session and
            # apply it via the same path the worker uses.
            new_ctx = _mk_ctx(
                sessions=[_own_session()],
                other_sessions=[_other_session()],
                sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
            )
            old_screen.apply_loaded_ctx(new_ctx, focus_key="")
            await pilot.pause()
            # The screen must have been recomposed (signature flipped).
            self.assertIsNot(app.screen, old_screen)
            self.assertIsInstance(app.screen, MainScreen)
            self.assertIn("user", tuple(c.id for c in app.screen._active_columns))

    async def test_cursor_pinned_across_cross_user_recompose(self) -> None:
        """Cursor on an own row survives the recompose triggered by other_sessions arriving.

        Sequence: mount with no other-user rows; place cursor on the
        first own row; apply a fresh ctx that adds an other-user
        session. The signature flip recomposes ``MainScreen``; the
        new screen must restore focus to the same own row by KEY
        (own:<name>), not by index — index 0 in the new model could
        be the alice.bar row depending on sort order.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.context import SudoCapability
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        ctx = _mk_ctx(
            sessions=[_own_session()],
            other_sessions=[],
            sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _seed_state_main(app, ctx)
            app.screen._refresh_dashboard()
            old_screen = app.screen
            old_widget = old_screen.query_one("#sessions-dashboard", SessionDashboardTable)
            old_widget.focus()
            old_widget.move_cursor(row=0)
            await pilot.pause()
            # Sanity: the only own row is devagent.foo.
            self.assertEqual(old_screen._dashboard_rows[0].name, "devagent.foo")
            # Apply a fresh ctx with the other-user session — flips
            # the layout signature → forces recompose.
            new_ctx = _mk_ctx(
                sessions=[_own_session()],
                other_sessions=[_other_session()],
                sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
            )
            old_screen.apply_loaded_ctx(new_ctx)
            await pilot.pause()
            # Recompose happened.
            self.assertIsNot(app.screen, old_screen)
            self.assertIsInstance(app.screen, MainScreen)
            # USER column is now active.
            self.assertIn("user", tuple(c.id for c in app.screen._active_columns))
            # Cursor is pinned to the devagent.foo row by key, not
            # index — even if the sort places alice.bar above it.
            new_widget = app.screen.query_one("#sessions-dashboard", SessionDashboardTable)
            self.assertIs(app.screen.focused, new_widget)
            cursor_idx = new_widget.cursor_row
            assert cursor_idx is not None
            cursor_row = app.screen._dashboard_rows[cursor_idx]
            self.assertEqual(cursor_row.name, "devagent.foo")
            self.assertEqual(cursor_row.user, "devagent")

    async def test_user_column_stays_after_other_sessions_disappear(self) -> None:
        """Cross-user latch is monotonic: once mounted, the USER column
        does not auto-hide when the other-user row that triggered it
        goes away.

        Auto-hiding under the operator while they are using the column
        is the broken behaviour the latch exists to prevent — losing
        the column on every transient remote-snapshot dropout or
        filter narrowing was the very bug this design fixes.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.context import SudoCapability

        ctx = _mk_ctx(
            sessions=[_own_session()],
            other_sessions=[_other_session()],
            sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            old_screen = app.screen
            self.assertIn("user", tuple(c.id for c in old_screen._active_columns))
            new_ctx = _mk_ctx(
                sessions=[_own_session()],
                other_sessions=[],
                sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
            )
            old_screen.apply_loaded_ctx(new_ctx, focus_key="")
            await pilot.pause()
            # The column survives whatever happens to the underlying
            # rows — same screen (no recompose, signature unchanged)
            # or a fresh one, USER must still be there.
            self.assertIn("user", tuple(c.id for c in app.screen._active_columns))

    async def test_user_column_appears_on_remote_snapshot_with_foreign_user(self) -> None:
        """Multi-host scenario: app starts with a single-user local
        dashboard; a remote-peer snapshot lands carrying a row owned
        by a different user → USER column mounts on the very tick of
        the snapshot landing.

        Regression for the headline bug this design fixes: the old
        ``cross_user`` predicate read ``bool(ctx.other_sessions)``
        and ignored remote rows entirely, so a multi-host dashboard
        with sessions from a foreign user on a remote peer rendered
        without the USER column — making the user column ambiguous
        across the rows.
        """
        from uxon.remote_collector import RemoteSnapshot
        from uxon.remote_hosts import RemoteHost
        from uxon.tui.app import UxonApp, _RefreshSourceLanded
        from uxon.tui.screens.main import MainScreen

        ctx = _mk_ctx(
            sessions=[_own_session()],
            remote_hosts=[
                RemoteHost(
                    name="vz-prod1",
                    ssh_alias="vz-prod1",
                    description="",
                    remote_uxon="uxon",
                )
            ],
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            initial_cols = tuple(c.id for c in app.screen._active_columns)
            self.assertNotIn("user", initial_cols)
            # Inject a remote snapshot carrying a row from a foreign
            # user. The handler must fold ``alice`` into the latch
            # accumulator and escalate the render kind so the layout
            # signature is re-evaluated.
            snap = RemoteSnapshot(
                host_name="vz-prod1",
                fetched_at_epoch=0.0,
                from_cache=False,
                error=None,
                sessions=[
                    {
                        "user": "alice",
                        "name": "uxon-bar@claude",
                        "short_id": "bar",
                        "agent": "claude",
                        "attached": False,
                        "windows": "1",
                        "created": "",
                        "last_attached": "",
                        "pane_pids": [],
                        "active_pid": None,
                        "active_cmd": "claude",
                        "active_path": "/srv",
                        "cpu_pct": 0.0,
                        "rss_kib": 0,
                        "legacy": False,
                    }
                ],
            )
            # Manually drive _latest_ctx so the main_ctx escalation has
            # a context to apply (production sets this from the first
            # main_ctx_rebuild tick, which our deterministic fixture
            # doesn't run).
            app._latest_ctx = ctx  # type: ignore[attr-defined]
            handler = app._source_dispatch_prefix[0][1]  # type: ignore[attr-defined]
            handler(_RefreshSourceLanded(name="remote:vz-prod1", value=snap))
            await pilot.pause()
            # Column appeared via the apply_loaded_ctx → recompose path.
            self.assertIsInstance(app.screen, MainScreen)
            self.assertIn("user", tuple(c.id for c in app.screen._active_columns))


if __name__ == "__main__":
    unittest.main()
