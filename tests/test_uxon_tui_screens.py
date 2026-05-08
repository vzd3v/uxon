"""Pilot tests for textual screens (T8+).

Uses ``App.run_test()`` + ``Pilot`` to drive the TUI in-process. Covers
MainScreen routing, digit-jump guard, kill flow, CallbackError → toast,
refresh re-calls ``on_refresh``.

See ``tests/harness/pty_tui.py`` for end-to-end pty tests.
"""

from __future__ import annotations

import unittest

from harness.textual_scenarios import ScreenScenario, press_keys, run_screen_scenarios


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _mk_ctx(**overrides):
    from uxon.tui.context import LaunchRequest, TuiContext
    from uxon.tui.refresh import SourceSpec

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
    # Default source mirrors the production wiring: one
    # ``main_ctx_rebuild`` source whose fetcher delegates to
    # ``ctx.on_refresh()``. The lambda closes over the ``ctx`` already
    # built from ``base`` (which includes any caller-supplied
    # ``on_refresh=`` from ``overrides``), so the registry path
    # invokes the test-supplied fake when 'r' is pressed. Tests that
    # need to suppress refresh spawning can override with
    # ``refresh_sources=[]``.
    if "refresh_sources" not in overrides:
        ctx.refresh_sources = [
            SourceSpec(
                name="main_ctx_rebuild",
                fetch=lambda ctx=ctx: ctx.on_refresh(),
                cadence_seconds_attr="tui_refresh_interval_seconds",
                kick_on_mount=True,
            )
        ]
    return ctx


@unittest.skipUnless(_textual_available(), "textual not installed")
class MainScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_q_quits(self) -> None:
        from uxon.tui.app import UxonApp

        app = UxonApp(_mk_ctx(), probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # SearchBar has default focus; Esc blurs it so ``q``
            # reaches the screen-level binding instead of being
            # consumed as Input text.
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        self.assertEqual(app.quit_rc, 0)

    async def test_digit_1_activates_action_cwd(self) -> None:
        from uxon.tui.app import UxonApp

        app = UxonApp(_mk_ctx(), probe_agents=False)
        calls: list[str] = []
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen._launch_cwd = lambda: calls.append("cwd")
            # Blur the SearchBar (default focus) so digit-jump fires.
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("1")
            await pilot.pause()
        self.assertEqual(calls, ["cwd"])

    async def test_refresh_preserves_action_focus(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets import ActionRow

        def fake_refresh():
            return _mk_ctx(on_refresh=fake_refresh)

        app = UxonApp(_mk_ctx(on_refresh=fake_refresh), probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.screen.query_one("#action-open", ActionRow).focus()
            await pilot.press("r")
            await pilot.pause()
            self.assertEqual(app.screen.focused.id, "action-open")

    async def test_skeleton_swap_preserves_agent_availability(self) -> None:
        """Probe results survive the skeleton→loaded ctx swap.

        Regression for a bug where ``apply_loaded_ctx`` carried over
        ``link_health_status`` but not ``agent_availability``: the probe
        worker writes to ``app.ctx.agent_availability`` and after the
        first refresh tick that dict was orphaned — every subsequent
        ``LaunchOptionsScreen`` saw a fresh ``pending`` dict and rendered
        ``(checking…)`` forever, blocking the agent commit path.
        """
        from uxon.agents import AgentAvailability
        from uxon.tui.app import UxonApp

        loaded = _mk_ctx()  # loaded ctx with its own fresh availability dict

        def fake_refresh():
            return _mk_ctx(on_refresh=fake_refresh)

        skeleton = _mk_ctx(loading=True, on_refresh=fake_refresh)
        # Pre-seed the skeleton's availability dict with a non-pending
        # entry — emulates the probe completing before the swap.
        skeleton.agent_availability["claude"] = AgentAvailability(status="ok")

        app = UxonApp(skeleton, probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # Force-trigger the swap (in real life kick_refresh fires in on_mount).
            app.screen.apply_loaded_ctx(loaded)
            await pilot.pause()
            self.assertEqual(
                app.screen.ctx.agent_availability["claude"].status,
                "ok",
                msg="screen.ctx lost the probe result",
            )
            self.assertIs(
                app.ctx,
                app.screen.ctx,
                msg="app.ctx and screen.ctx must point to the same TuiContext",
            )

    async def test_skeleton_swap_preserves_detected_agents(self) -> None:
        """``detected_agents`` survives ``apply_loaded_ctx``.

        Regression for a bug where the periodic refresh tick wiped the
        suggestion banner one tick after it appeared: the probe worker
        writes detected agents to ``app.ctx.detected_agents`` and the
        next ctx swap clobbered that dict with a fresh empty one.
        """
        from uxon.probes import BinaryStatus
        from uxon.tui.app import UxonApp

        loaded = _mk_ctx()  # loaded ctx with its own fresh detected dict

        def fake_refresh():
            return _mk_ctx(on_refresh=fake_refresh)

        skeleton = _mk_ctx(loading=True, on_refresh=fake_refresh)
        # Pre-seed the skeleton's detected dict — emulates the probe
        # finding codex installed but not yet in [agents].enabled.
        skeleton.detected_agents["codex"] = BinaryStatus(
            name="codex",
            path="/usr/bin/codex",
            install_hint="npm install -g @openai/codex",
        )

        app = UxonApp(skeleton, probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.screen.apply_loaded_ctx(loaded)
            await pilot.pause()
            self.assertIn(
                "codex",
                app.screen.ctx.detected_agents,
                msg="detected_agents was dropped on ctx swap",
            )

    async def test_main_ui_survives_recompose(self) -> None:
        """Dashboard view, tab index, and focus-restore flag survive
        a layout-signature recompose.

        Regression for a bug class: ``apply_loaded_ctx`` builds a
        fresh ``MainScreen`` whenever ``select_layout_signature``
        flips (e.g. another user starts a session). Three pieces of
        operator-set UI state used to die with the old screen — view
        mode, active host tab, pending tab-focus-restore — silently
        snapping the operator back to defaults mid-session. The fix
        moved them to ``self.app.main_ui`` (a
        :class:`MainScreenUiState`), which the App keeps stable
        across screen swaps.
        """
        from types import SimpleNamespace

        from uxon.tui.app import UxonApp
        from uxon.tui.dashboard.ui_state import set_view_mode

        skeleton = _mk_ctx()
        # Loaded ctx flips ``has_other_sessions`` → forces recompose.
        loaded = _mk_ctx(
            other_sessions=[SimpleNamespace(user="alice", name="proj@claude", status="active")]
        )

        app = UxonApp(skeleton, probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            old_screen = app.screen
            old_main_ui = app.main_ui
            # Mutate every field the contract claims to preserve.
            app.main_ui.ui = set_view_mode(app.main_ui.ui, "flat")
            app.main_ui.active_tab_index = 2
            app.main_ui.pending_tab_focus_restore = True
            # Swap to a ctx with a different layout signature → triggers
            # the ``MainScreen(self.ctx)`` rebuild + ``switch_screen`` path.
            app.screen.apply_loaded_ctx(loaded)
            await pilot.pause()
            self.assertIsNot(
                app.screen, old_screen, msg="layout flip should have produced a fresh MainScreen"
            )
            self.assertIs(app.main_ui, old_main_ui, msg="main_ui must survive the screen swap")
            self.assertEqual(app.main_ui.ui.view_mode, "flat")
            self.assertEqual(app.main_ui.active_tab_index, 2)
            self.assertTrue(app.main_ui.pending_tab_focus_restore)

    async def test_refresh_keypress_kicks_host_probe(self) -> None:
        """Pressing ``r`` re-runs the host probe.

        Regression: without this, the periodic timer only kicked
        ``kick_refresh`` (which rebuilds the ctx) but the host probe
        ran exactly once on mount, so the missing-agents modal never
        recovered after the user installed an agent.
        """
        from uxon.tui.app import UxonApp

        kicks: list[None] = []
        app = UxonApp(_mk_ctx(), probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._kick_host_probe = lambda: kicks.append(None)  # type: ignore[method-assign]
            # Blur the SearchBar so ``r`` hits action_refresh rather
            # than being consumed by the Input.
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
        self.assertEqual(len(kicks), 1, msg="action_refresh did not kick the host probe")

    async def test_kill_calls_on_kill_callback(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.context import TuiSession

        kill_calls: list[tuple[str, str]] = []
        refresh_calls = []

        def fake_kill(user: str, name: str) -> None:
            kill_calls.append((user, name))

        session = TuiSession(
            name="devagent.foo",
            short="foo",
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

        def fake_refresh():
            # Commit 10: the dashboard is data-driven from
            # ``state.main``. The on-mount ``kick_refresh`` lands a
            # rebuild before the test presses 'd'; return the same
            # session so the dashboard has a row to focus on.
            refresh_calls.append(1)
            return _mk_ctx(
                sessions=[session],
                current_user="devagent",
                on_kill=fake_kill,
                on_refresh=fake_refresh,
            )

        ctx = _mk_ctx(
            sessions=[session],
            current_user="devagent",
            on_kill=fake_kill,
            on_refresh=fake_refresh,
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Focus the dashboard table and press 'd'. Commit 10
            # replaced ``#sessions-own`` (the legacy local table) with
            # ``#sessions-dashboard`` (SessionDashboardTable). The
            # dashboard is data-driven from ``state.main`` — inject a
            # ``MainData`` snapshot so the model selector emits the row
            # without waiting for the periodic rebuild source to run.
            from uxon.tui.main_data import MainData
            from uxon.tui.widgets.session_dashboard_table import (
                SessionDashboardTable,
            )

            app.state.main = MainData.from_context(ctx)
            app.screen._refresh_dashboard()
            t = app.screen.query_one("#sessions-dashboard", SessionDashboardTable)
            app.screen.action_refresh = lambda: None
            t.focus()
            t.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            # ConfirmYesNo modal is active — answer y.
            await pilot.press("y")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        self.assertEqual(kill_calls, [("devagent", "devagent.foo")])


@unittest.skipUnless(_textual_available(), "textual not installed")
class LazyWidgetsTests(unittest.IsolatedAsyncioTestCase):
    """Lazy-mounted MainScreen children resolve post first paint.

    ``DetectedAgentsBanner`` is wrapped in ``textual.lazy.Lazy`` so it
    does not block first paint. Verify (a) it IS present after Pilot's
    ``pause()`` ticks, and (b) focus did not jump to the deferred-
    mounted widget.
    """

    async def test_lazy_banner_mounts_after_pause_and_keeps_focus(self) -> None:
        from uxon.remote_hosts import RemoteHost
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets import DetectedAgentsBanner

        ctx = _mk_ctx(
            remote_hosts=(
                RemoteHost(name="peer", ssh_alias="peer", description="", remote_uxon="uxon"),
            )
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()  # second tick: Lazy wrappers swap in their children
            screen = app.screen
            # Inner widget resolves by its original id (Lazy wrapper
            # removes itself after mounting the child).
            screen.query_one("#detected-banner", DetectedAgentsBanner)
            # Focus stayed on a non-banner row.
            focused_id = screen.focused.id if screen.focused else None
            self.assertNotEqual(focused_id, "detected-banner")


@unittest.skipUnless(_textual_available(), "textual not installed")
class WorkerGateTests(unittest.TestCase):
    """Regression coverage for the worker-handle in-flight gate.

    The previous bool-latch implementation wedged when a refresh worker
    was cancelled before it ran (an ``exclusive=True`` host probe in the
    same default group did exactly this), because ``_refresh_in_flight``
    stayed True forever. The handle-based gate must self-heal: once a
    worker leaves PENDING/RUNNING (cancelled, errored, or succeeded),
    the next kick spawns a fresh one.
    """

    def test_worker_active_helper(self) -> None:
        from textual.worker import WorkerState

        from uxon.tui.app import _worker_active

        class _FakeWorker:
            def __init__(self, state: WorkerState) -> None:
                self.state = state

        self.assertFalse(_worker_active(None))
        self.assertTrue(_worker_active(_FakeWorker(WorkerState.PENDING)))
        self.assertTrue(_worker_active(_FakeWorker(WorkerState.RUNNING)))
        for done in (WorkerState.CANCELLED, WorkerState.ERROR, WorkerState.SUCCESS):
            self.assertFalse(_worker_active(_FakeWorker(done)))

    def test_kick_refresh_heals_after_worker_cancellation(self) -> None:
        """Cancelled worker must not wedge the refresh stream."""
        from textual.worker import WorkerState

        from uxon.tui.app import UxonApp

        class _FakeWorker:
            def __init__(self) -> None:
                self.state = WorkerState.RUNNING

        spawned: list[_FakeWorker] = []

        def fake_run_worker(*_args, **_kwargs):
            w = _FakeWorker()
            spawned.append(w)
            return w

        app = UxonApp(_mk_ctx(), probe_agents=False)
        app.run_worker = fake_run_worker  # type: ignore[method-assign]

        app.kick_refresh()
        self.assertEqual(len(spawned), 1)
        app.kick_refresh()  # still RUNNING — must skip
        self.assertEqual(len(spawned), 1)

        spawned[0].state = WorkerState.CANCELLED  # simulate exclusive-cancel
        app.kick_refresh()  # must self-heal and spawn
        self.assertEqual(len(spawned), 2)

    def test_mount_skips_kick_for_sources_opting_out(self) -> None:
        """``SourceSpec.kick_on_mount=False`` is honoured at mount time.

        Regression guard for the ``kick_on_mount`` flag: it is a
        load-bearing knob future one-shot or lazy interval-only
        sources rely on (e.g. a remote-host probe that only fires on
        the first periodic tick, not at startup). The mount-time
        kick path must filter sources by this flag.
        """
        from textual.worker import WorkerState

        from uxon.tui.app import UxonApp
        from uxon.tui.refresh import SourceSpec

        class _FakeWorker:
            def __init__(self) -> None:
                self.state = WorkerState.RUNNING

        captured: list[str] = []

        def fake_run_worker(*_args, **kwargs):
            captured.append(kwargs.get("group", ""))
            return _FakeWorker()

        ctx = _mk_ctx(loading=True)
        ctx.refresh_sources = [
            SourceSpec(name="eager", fetch=lambda: None, kick_on_mount=True),
            SourceSpec(name="lazy", fetch=lambda: None, kick_on_mount=False),
        ]
        app = UxonApp(ctx, probe_agents=False)
        app.run_worker = fake_run_worker  # type: ignore[method-assign]

        # Exercise the mount-time kick path directly — calling
        # ``on_mount`` would also touch ``push_screen`` and other DOM
        # state that requires a running Textual loop, which this pure
        # gate test deliberately avoids.
        app._kick_initial_sources()
        self.assertEqual([g for g in captured if g.startswith("refresh:")], ["refresh:eager"])

    def test_kick_helpers_use_distinct_groups(self) -> None:
        """Each periodic stream pins its worker to its own group.

        Without distinct groups, ``run_worker(exclusive=True)`` from one
        stream cancels workers from any other stream that happens to
        share the default group.
        """
        from uxon.tui.app import UxonApp

        captured: list[dict] = []

        class _FakeWorker:
            def __init__(self) -> None:
                from textual.worker import WorkerState

                self.state = WorkerState.RUNNING

        def fake_run_worker(*_args, **kwargs):
            captured.append(kwargs)
            return _FakeWorker()

        app = UxonApp(_mk_ctx(), probe_agents=True)
        app.run_worker = fake_run_worker  # type: ignore[method-assign]

        app.kick_refresh()
        app._kick_host_probe()
        app._kick_link_health_probe()

        groups = [k.get("group") for k in captured]
        # Registry sources carry a ``refresh:<name>`` group prefix so
        # ``exclusive=True`` from one source can never cancel another's
        # worker. Bespoke streams (host_probe, link_health) keep their
        # legacy group names.
        self.assertEqual(
            sorted(groups),
            sorted({"refresh:main_ctx_rebuild", "host_probe", "link_health"}),
        )


@unittest.skipUnless(_textual_available(), "textual not installed")
class ConfirmModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_modal_smoke_batch(self) -> None:
        from textual.widgets import Input

        from uxon.tui.screens.confirm import ConfirmPhrase, ConfirmYesNo

        async def phrase(app, pilot):
            app.screen.query_one("#confirm-input", Input).focus()
            await pilot.press(*"kill-all")
            await pilot.press("enter")

        scenarios = [
            ScreenScenario("yesno-y", lambda: ConfirmYesNo("Kill foo?"), press_keys("y"), True),
            ScreenScenario("yesno-n", lambda: ConfirmYesNo("Kill foo?"), press_keys("n"), False),
            ScreenScenario(
                "phrase-match", lambda: ConfirmPhrase("Danger!", "kill-all"), phrase, True
            ),
        ]
        results = await run_screen_scenarios(scenarios, size=(80, 24))
        self.assertEqual(results, [s.expected for s in scenarios])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_textual_available(), "textual not installed")
class LaunchOptionsScreenTests(unittest.IsolatedAsyncioTestCase):
    """Pilot tests for the two-panel agent × permission-mode modal."""

    def _make_avail(self, status: str):
        from uxon.agents import AgentAvailability

        return AgentAvailability(status=status)

    async def test_launch_options_layout_smoke_batch(self) -> None:
        from uxon.tui.screens.launch_options import LaunchOptionsScreen

        async def assert_pending(app, pilot):
            screen = app.screen
            self.assertIn("claude", screen._visible_agents)
            agent_list = screen.query_one("#agent-list")
            labels = [str(item.query_one("Static").content) for item in agent_list.children]
            self.assertTrue(
                any("checking" in label for label in labels), f"no checking in {labels}"
            )
            await pilot.press("enter")

        scenarios = [
            ScreenScenario(
                "pending-label",
                lambda: LaunchOptionsScreen(
                    _mk_ctx(
                        enabled_agents=("claude",),
                        default_agent="claude",
                        agent_availability={"claude": self._make_avail("pending")},
                    )
                ),
                assert_pending,
                ("claude", "normal"),
            ),
        ]

        results = await run_screen_scenarios(scenarios)
        self.assertEqual(results, [s.expected for s in scenarios])

    async def test_arrow_to_cursor_rebuilds_modes(self) -> None:
        """Regression: arrow-down on the agent list must update _current_agent
        and rebuild the mode list for that agent.

        Previous bug: the screen-level up/down bindings were shadowed by
        ListView's built-in cursor_up/cursor_down, so _maybe_rebuild_mode
        never ran. Arrowing down to cursor kept _current_agent=claude and
        kept claude's three modes (normal/auto/yolo) in the right panel,
        so cursor's mode set was not shown.
        """
        from textual.app import App
        from textual.widgets import ListView

        from uxon.tui.screens.launch_options import LaunchOptionsScreen

        ctx = _mk_ctx(
            enabled_agents=("claude", "cursor"),
            default_agent="claude",
            agent_availability={
                "claude": self._make_avail("ok"),
                "cursor": self._make_avail("ok"),
            },
        )

        class Host(App):
            result = "unset"

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()

                self.push_screen(LaunchOptionsScreen(ctx), done)

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            # Move highlight to cursor (2nd entry).
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(screen._current_agent, "cursor")
            mode_list = screen.query_one("#mode-list", ListView)
            # cursor has exactly two modes in the catalog: normal, yolo.
            self.assertEqual(len(mode_list.children), 2)
            mode_ids = [item.id for item in mode_list.children]
            self.assertEqual(mode_ids, ["mode-normal", "mode-yolo"])


@unittest.skipUnless(_textual_available(), "textual not installed")
class NewProjectScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_project_smoke_batch(self) -> None:
        from textual.widgets import Input

        from uxon.tui.screens.new_project import NewProjectScreen

        async def submit_foo(app, pilot):
            app.screen.query_one("#name-input", Input).focus()
            await pilot.press(*"foo")
            await pilot.press("enter")

        async def cancel_bar(app, pilot):
            app.screen.query_one("#name-input", Input).focus()
            await pilot.press(*"bar")
            await pilot.press("escape")

        scenarios = [
            ScreenScenario("valid-name", lambda: NewProjectScreen("/srv/work"), submit_foo, "foo"),
            ScreenScenario(
                "escape-cancel", lambda: NewProjectScreen("/srv/work"), cancel_bar, None
            ),
        ]
        results = await run_screen_scenarios(scenarios, size=(80, 24))
        self.assertEqual(results, [s.expected for s in scenarios])


@unittest.skipUnless(_textual_available(), "textual not installed")
class GitProfileScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_git_profile_smoke_batch(self) -> None:
        from uxon.tui.screens.git_profile import GitProfileScreen

        scenarios = [
            ScreenScenario(
                "escape-cancel",
                lambda: GitProfileScreen([("profA", "A")]),
                press_keys("escape"),
                None,
            ),
            ScreenScenario(
                "default-profile-enter",
                lambda: GitProfileScreen([("profA", "A"), ("profB", "B")], default_profile="profB"),
                press_keys("enter"),
                "profB",
            ),
        ]
        results = await run_screen_scenarios(scenarios)
        self.assertEqual(results, [s.expected for s in scenarios])


@unittest.skipUnless(_textual_available(), "textual not installed")
class ExistingProjectScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_project_smoke_batch(self) -> None:
        from uxon.tui.screens.existing import ExistingProjectScreen

        scenarios = [
            ScreenScenario(
                "enter-picks-cursor",
                lambda: ExistingProjectScreen([("alpha", "")], "/srv/work"),
                press_keys("enter"),
                "alpha",
            ),
            ScreenScenario(
                "escape-cancel",
                lambda: ExistingProjectScreen([("alpha", "")], "/srv/work"),
                press_keys("escape"),
                None,
            ),
            ScreenScenario(
                "up-wraps-to-last",
                lambda: ExistingProjectScreen([("alpha", ""), ("beta", "")], "/srv/work"),
                press_keys("up", "enter"),
                "beta",
            ),
            ScreenScenario(
                # 'p' narrows [alpha,beta,gamma] → [alpha]; cursor lands on 0;
                # Enter picks the only match.
                "type-narrows-and-picks",
                lambda: ExistingProjectScreen(
                    [("alpha", ""), ("beta", ""), ("gamma", "")], "/srv/work"
                ),
                press_keys("p", "enter"),
                "alpha",
            ),
            ScreenScenario(
                # 'z' narrows to []; Enter is a no-op so no dismiss fires
                # and the harness's "unset" sentinel survives.
                "type-no-match-enter-noop",
                lambda: ExistingProjectScreen([("alpha", ""), ("beta", "")], "/srv/work"),
                press_keys("z", "enter"),
                "unset",
            ),
            ScreenScenario(
                # First Esc clears the (non-empty) filter; second Esc
                # dismisses with None because the input is empty.
                "esc-clears-then-cancels",
                lambda: ExistingProjectScreen([("alpha", ""), ("beta", "")], "/srv/work"),
                press_keys("a", "escape", "escape"),
                None,
            ),
        ]
        results = await run_screen_scenarios(scenarios)
        self.assertEqual(results, [s.expected for s in scenarios])


@unittest.skipUnless(_textual_available(), "textual not installed")
class ExistingProjectSearchTests(unittest.IsolatedAsyncioTestCase):
    """Standalone pilot tests for live-search wiring: focus-on-mount and
    the match counter — assertions that need direct widget queries
    rather than the dismiss-value harness."""

    async def test_filter_input_focused_on_mount(self) -> None:
        from textual.app import App

        from uxon.tui.screens.existing import ExistingProjectScreen
        from uxon.tui.widgets.filter_input import FilterInput

        class Host(App):
            def __init__(self) -> None:
                super().__init__()
                self.scr = ExistingProjectScreen([("alpha", ""), ("beta", "")], "/srv/work")

            def on_mount(self) -> None:
                self.push_screen(self.scr)

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            fi = app.scr.query_one(FilterInput)
            self.assertIs(app.focused, fi.input)

    async def test_match_counter_updates_with_typing(self) -> None:
        from textual.app import App
        from textual.widgets import Static

        from uxon.tui.screens.existing import ExistingProjectScreen
        from uxon.tui.widgets.filter_input import FilterInput

        class Host(App):
            def __init__(self) -> None:
                super().__init__()
                self.scr = ExistingProjectScreen(
                    [("alpha", ""), ("beta", ""), ("gamma", "")], "/srv/work"
                )

            def on_mount(self) -> None:
                self.push_screen(self.scr)

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            fi = app.scr.query_one(FilterInput)
            counter = fi.query_one("#match-count", Static)
            # Empty filter → counter blank.
            self.assertEqual(str(counter.content), "")
            await pilot.press("a")  # 'a' matches alpha + gamma + beta — wait, beta?
            await pilot.pause()
            # 'a' is in alpha, gamma, beta — three matches.
            self.assertEqual(str(counter.content), "3 matches")
            await pilot.press("l")  # filter is now "al" → only alpha
            await pilot.pause()
            self.assertEqual(str(counter.content), "1 match")
            await pilot.press("z")  # "alz" → no matches
            await pilot.pause()
            self.assertEqual(str(counter.content), "0 matches")


@unittest.skipUnless(_textual_available(), "textual not installed")
class SettingsScreenTests(unittest.IsolatedAsyncioTestCase):
    async def _mk_cbs(self, entries_factory):
        from uxon.tui.screens.settings import SettingsCallbacks

        saved: list = []
        removed: list = []

        def save(k, v):
            saved.append((k, v))

        def remove(k):
            removed.append(k)

        def save_mapping(k, v):
            saved.append((k, v))

        return (
            saved,
            removed,
            SettingsCallbacks(
                get_entries=entries_factory,
                save_setting=save,
                remove_setting=remove,
                save_mapping=save_mapping,
            ),
        )

    async def test_bool_toggle_saves_value(self):
        from textual.app import App

        from uxon.settings import SettingEntry, SettingSpec
        from uxon.tui.screens.settings import BoolToggleModal, SettingsScreen

        spec = SettingSpec("git_create_enabled", "bool", "desc")
        entries = [SettingEntry(spec=spec, value=False, source="default", editable=True)]

        saved, removed, cbs = await self._mk_cbs(lambda: entries)

        class Host(App):
            def on_mount(self):
                self.push_screen(SettingsScreen(cbs))

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # SettingsScreen is active; its DataTable cursor is on row 0
            # (the bool entry). Press Enter → BoolToggleModal pushed.
            await pilot.press("enter")
            await pilot.pause()
            # Click True button.
            from uxon.tui.screens.settings import BoolToggleModal

            modal = app.screen_stack[-1]
            self.assertIsInstance(modal, BoolToggleModal)
            btn = modal.query_one("#true")
            btn.press()
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        self.assertEqual(saved, [("git_create_enabled", True)])


@unittest.skipUnless(_textual_available(), "textual not installed")
class GitRemotesScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_populates_and_esc_dismisses(self):
        from textual.app import App

        from uxon.tui.screens.git_remotes import GitRemotesScreen

        rows = [
            ("foo", "github.com", "alice", "gh", "alice", "private", ""),
            ("bar", "gitlab.com", "bob", "token", "bob", "public", "~/.tok"),
        ]

        class Host(App):
            dismissed = False

            def on_mount(self):
                def done(_r):
                    self.dismissed = True
                    self.exit()

                self.push_screen(GitRemotesScreen(rows), done)

        app = Host()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        self.assertTrue(app.dismissed)
