"""Pilot tests for textual screens (T8+).

Uses ``App.run_test()`` + ``Pilot`` to drive the TUI in-process.
Covers MainScreen routing, kill flow, CallbackError → toast,
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
    from uxon.domain.agents import DEFAULT_AGENT_CATALOG
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
        current_user="dana_agent",
        agents=DEFAULT_AGENT_CATALOG,
        on_launch_cwd=lambda profile_id, mode_id, target_dir=None: LaunchRequest(
            cmd=("/bin/true",), label="cwd"
        ),
        on_launch_new=lambda n, profile_id, mode_id, g: LaunchRequest(
            cmd=("/bin/true",), label="new"
        ),
        on_launch_existing=lambda n, profile_id, mode_id: LaunchRequest(
            cmd=("/bin/true",), label="existing"
        ),
    )
    base.update(overrides)
    if "launch_profiles" not in overrides:
        from helpers import make_launch_profile_options

        base["launch_profiles"] = make_launch_profile_options(base["agents"])
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

    async def test_enter_on_default_focus_activates_action_cwd(self) -> None:
        from uxon.tui.app import UxonApp

        app = UxonApp(_mk_ctx(), probe_agents=False)
        calls: list[str] = []
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen._launch_cwd = lambda: calls.append("cwd")
            # action-cwd holds default focus; Enter activates it.
            await pilot.press("enter")
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
        result used to be orphaned on a ctx swap, so every subsequent
        ``LaunchOptionsScreen`` saw a fresh ``pending`` dict and rendered
        ``(checking…)`` forever, blocking the agent commit path.

        Post-shim-removal the availability lives on the App-owned
        ``state.agent_availability`` slot (identity-stable across rebuild
        ticks), so the swap can no longer orphan it. Pin both: the slot
        keeps the probe result, and the static snapshot stays shared
        (``app.ctx is app.screen.cfg``).
        """
        from uxon.infra.agents import AgentAvailability
        from uxon.tui.app import UxonApp

        loaded = _mk_ctx()  # loaded ctx with its own fresh availability dict

        def fake_refresh():
            return _mk_ctx(on_refresh=fake_refresh)

        skeleton = _mk_ctx(loading=True, on_refresh=fake_refresh)
        # Pre-seed the skeleton's availability dict with a non-pending
        # entry — emulates the probe completing before the swap. The App
        # seeds ``state.agent_availability`` from this at construction.
        skeleton.agent_availability["claude"] = AgentAvailability(status="ok")

        app = UxonApp(skeleton, probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # Force-trigger the swap (in real life kick_refresh fires in on_mount).
            app.screen.apply_loaded_ctx(loaded)
            await pilot.pause()
            self.assertEqual(
                app.state.agent_availability.value["claude"].status,
                "ok",
                msg="state slot lost the probe result across the ctx swap",
            )
            self.assertIs(
                app.ctx,
                app.screen.cfg,
                msg="app.ctx and screen.cfg must point to the same TuiContext snapshot",
            )

    async def test_main_ui_survives_structural_refresh(self) -> None:
        """Dashboard view, tab index, and focus-restore flag survive
        a layout-signature change.

        Regression for a bug class: ``apply_loaded_ctx`` used to build a
        fresh ``MainScreen`` whenever ``select_layout_signature`` flipped
        (e.g. another user starts a session). Three pieces of operator-set
        UI state died with the old screen — view mode, active host tab,
        pending tab-focus-restore — snapping the operator back to defaults
        mid-session. Two layers now defend this: the state lives on
        ``self.app.main_ui`` (App-owned), AND the structural refresh is
        reconciled in place, so the screen is no longer swapped at all.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.context import TuiSession
        from uxon.tui.dashboard.ui_state import set_view_mode

        # Skeleton has a single-user dashboard (dana_agent's own row);
        # loaded ctx introduces a second user (alice), flipping the
        # cross_user latch False→True and forcing the recompose path.
        # Real :class:`TuiSession` instances are required because
        # ``apply_loaded_ctx`` now syncs ``state.main`` from the ctx
        # so the dashboard model walks the rows downstream.
        own = TuiSession(
            name="dana_agent.foo",
            short="foo",
            attached=False,
            pid="1",
            cpu="0",
            ram="0",
            created="0s",
            last_activity="0s",
            cmd="claude",
            path="/srv",
            user="dana_agent",
        )
        skeleton = _mk_ctx(sessions=[own])
        loaded = _mk_ctx(
            sessions=[own],
            other_sessions=[
                TuiSession(
                    name="alice.proj",
                    short="proj",
                    attached=False,
                    pid="1",
                    cpu="0",
                    ram="0",
                    created="0s",
                    last_activity="0s",
                    cmd="codex",
                    path="/srv",
                    user="alice",
                )
            ],
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
            # Apply a ctx with a different layout signature. This is now
            # reconciled IN PLACE (no ``switch_screen`` swap), so the
            # screen object itself is preserved — and the App-owned
            # ``main_ui`` with it.
            app.screen.apply_loaded_ctx(loaded)
            await pilot.pause()
            self.assertIs(
                app.screen, old_screen, msg="structural refresh must patch in place, not swap"
            )
            self.assertIs(app.main_ui, old_main_ui, msg="main_ui must survive the refresh")
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
            name="dana_agent.foo",
            short="foo",
            attached=False,
            pid="1",
            cpu="1.0",
            ram="1M",
            created="1s",
            last_activity="1s",
            cmd="claude",
            path="/srv/work",
            user="dana_agent",
        )

        def fake_refresh():
            # Commit 10: the dashboard is data-driven from
            # ``state.main``. The on-mount ``kick_refresh`` lands a
            # rebuild before the test presses 'd'; return the same
            # session so the dashboard has a row to focus on.
            refresh_calls.append(1)
            return _mk_ctx(
                sessions=[session],
                current_user="dana_agent",
                on_kill=fake_kill,
                on_refresh=fake_refresh,
            )

        ctx = _mk_ctx(
            sessions=[session],
            current_user="dana_agent",
            on_kill=fake_kill,
            on_refresh=fake_refresh,
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Focus the dashboard and press 'd'. The dashboard widget is
            # ``#sessions-dashboard`` (SessionListView), data-driven from
            # ``state.main`` — inject a ``MainData`` snapshot so the model
            # selector emits the row without waiting for the periodic
            # rebuild source to run.
            from uxon.tui.main_data import MainData
            from uxon.tui.widgets.session_list_view import SessionListView

            app.state.main = MainData.from_context(ctx)
            app.screen._refresh_dashboard()
            t = app.screen.query_one("#sessions-dashboard", SessionListView)
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
        self.assertEqual(kill_calls, [("dana_agent", "dana_agent.foo")])


@unittest.skipUnless(_textual_available(), "textual not installed")
class ContainerPromptSmokeTests(unittest.IsolatedAsyncioTestCase):
    """AC-B4 — the TUI confirm affordance for a stopped/absent container.

    With ``approval = "prompt"`` a needed start/create must show a
    ``ConfirmYesNo`` before the prepare runs; on confirm the prepare fires
    and the launch proceeds.
    """

    async def _settle(self, pilot) -> None:
        for _ in range(12):
            await pilot.pause()

    async def test_prompt_then_confirm_runs_prepare_and_launches(self) -> None:
        from uxon.app.launch import RuntimeGate
        from uxon.infra.agents import AgentAvailability
        from uxon.tui.app import UxonApp
        from uxon.tui.screens.confirm import ConfirmYesNo
        from uxon.tui.screens.launch_options import LaunchOptionsScreen

        prepared: list[bool] = []
        launched: list[str] = []

        gate = RuntimeGate(
            needs_prepare=True,
            needs_prompt=True,
            message="Container 'proj-work' is stopped — start and launch?",
            fail_message="",
            prepare=lambda: prepared.append(True),
        )

        ctx = _mk_ctx(
            on_runtime_gate=lambda *a: gate,
            on_probe_existing_sessions=lambda *a: (),
            enabled_profiles=["claude"],
            agent_availability={"claude": AgentAvailability(status="ok", path="/usr/bin/claude")},
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await self._settle(pilot)
            app.request_launch = lambda req: launched.append(req.label)  # type: ignore[assignment, method-assign]
            app.screen._launch_flow.launch_cwd()
            opts = None
            for _ in range(10):
                await pilot.pause()
                opts = next(
                    (s for s in app.screen_stack if isinstance(s, LaunchOptionsScreen)), None
                )
                if opts is not None:
                    break
            self.assertIsNotNone(opts, msg="LaunchOptionsScreen was not reached")
            opts.dismiss(("claude", "normal"))
            await self._settle(pilot)
            # The container gate must have pushed the confirm BEFORE launching.
            confirm = next((s for s in app.screen_stack if isinstance(s, ConfirmYesNo)), None)
            self.assertIsNotNone(
                confirm, msg="ConfirmYesNo was not shown for the stopped container"
            )
            self.assertEqual(prepared, [], msg="prepare ran before the operator confirmed")
            self.assertEqual(launched, [], msg="launch committed before confirm")
            await pilot.press("y")
            await self._settle(pilot)
        self.assertEqual(prepared, [True], msg="prepare did not run after confirm")
        self.assertEqual(launched, ["cwd"], msg="launch did not proceed after prepare")


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

        from uxon.tui.workers import _worker_active

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
        from uxon.infra.agents import AgentAvailability

        return AgentAvailability(status=status)

    async def test_launch_options_layout_smoke_batch(self) -> None:
        from uxon.tui.context import LaunchProfileOption
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
                        enabled_profiles=("claude",),
                        default_profile="claude",
                        agent_availability={"claude": self._make_avail("pending")},
                    )
                ),
                assert_pending,
                ("claude", "normal"),
            ),
            ScreenScenario(
                "profile-agent-name-collision",
                lambda: LaunchOptionsScreen(
                    _mk_ctx(
                        enabled_profiles=("cursor",),
                        default_profile="cursor",
                        launch_profiles={
                            "cursor": LaunchProfileOption("cursor", "Claude", "claude", "alice")
                        },
                        agent_availability={"cursor": self._make_avail("ok")},
                    )
                ),
                press_keys("down", "enter"),
                ("cursor", "auto"),
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
            enabled_profiles=("claude", "cursor"),
            default_profile="claude",
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
class WorkspaceScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_choice_smoke_batch(self) -> None:
        from uxon.infra.worktrees import Workspace
        from uxon.tui.screens.workspace import WorkspaceScreen

        rows = [
            Workspace(label="main", branch="main", path="/srv/work", is_primary=True),
            Workspace(
                label="feature/auth",
                branch="feature/auth",
                path="/srv/work/.uxon/worktrees/auth",
                is_primary=False,
            ),
        ]
        scenarios = [
            ScreenScenario(
                "primary",
                lambda: WorkspaceScreen(rows, repo_root="/srv/work"),
                press_keys("enter"),
                ("primary", "/srv/work"),
            ),
            ScreenScenario(
                "existing",
                lambda: WorkspaceScreen(rows, repo_root="/srv/work"),
                press_keys("down", "enter"),
                ("worktree", "/srv/work/.uxon/worktrees/auth", "feature/auth"),
            ),
            ScreenScenario(
                "new",
                lambda: WorkspaceScreen(rows, repo_root="/srv/work"),
                press_keys("down", "down", "enter"),
                ("new", None),
            ),
            ScreenScenario(
                "cancel",
                lambda: WorkspaceScreen(rows, repo_root="/srv/work"),
                press_keys("escape"),
                None,
            ),
        ]
        self.assertEqual(await run_screen_scenarios(scenarios), [s.expected for s in scenarios])


@unittest.skipUnless(_textual_available(), "textual not installed")
class LaunchCwdWorktreeWiringTests(unittest.IsolatedAsyncioTestCase):
    def _ctx(self, **overrides):
        from uxon.infra.agents import AgentAvailability
        from uxon.infra.worktrees import Workspace

        self.workspaces = [
            Workspace(label="main", branch="main", path="/srv/work", is_primary=True),
            Workspace(
                label="feature/auth",
                branch="feature/auth",
                path="/srv/work/.uxon/worktrees/auth",
                is_primary=False,
            ),
        ]
        base = dict(
            enabled_profiles=("claude",),
            default_profile="claude",
            agent_availability={"claude": AgentAvailability(status="ok")},
            on_probe_worktrees=lambda cwd, profile, mode: self.workspaces,
            on_probe_existing_worktree_sessions=lambda *a: (),
            on_probe_existing_sessions=lambda *a: (),
        )
        base.update(overrides)
        return _mk_ctx(**base)

    async def test_pinned_profile_discovers_and_launches_worktree_off_loop(self) -> None:
        # App-level worker/continuation behavior needs a separate lifecycle.
        import asyncio

        from uxon.tui.app import UxonApp
        from uxon.tui.context import LaunchProfileOption, LaunchRequest
        from uxon.tui.screens.launch_options import LaunchOptionsScreen
        from uxon.tui.screens.workspace import WorkspaceScreen

        probes, launches = [], []

        def probe(target, profile, mode):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            probes.append((target, profile, mode))
            return self.workspaces

        def launch(*args):
            launches.append(args)
            return LaunchRequest(cmd=("/bin/true",), label="worktree")

        ctx = self._ctx(
            cwd_writable=False,
            launch_profiles={
                "claude": LaunchProfileOption("claude", "Claude", "claude", "alice_agent")
            },
            on_probe_worktrees=probe,
            on_launch_existing_worktree=launch,
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, LaunchOptionsScreen)
            self.assertEqual(probes, [])
            await pilot.press("down", "enter")  # Claude auto
            await pilot.pause()
            await pilot.pause()
            self.assertIsInstance(app.screen, WorkspaceScreen)
            self.assertEqual(probes, [("/srv/work", "claude", "auto")])
            await pilot.press("down", "enter")
            await pilot.pause()
            await pilot.pause()
        self.assertEqual(
            launches,
            [("/srv/work", "feature/auth", "/srv/work/.uxon/worktrees/auth", "claude", "auto")],
        )

    async def test_new_worktree_choice_reaches_branch_and_create(self) -> None:
        # The branch modal follows an asynchronous profile-specific probe.
        from textual.widgets import Input

        from uxon.tui.app import UxonApp
        from uxon.tui.context import LaunchRequest
        from uxon.tui.screens.worktree_branch import WorktreeBranchScreen

        created = []

        def create(*args):
            created.append(args)
            return LaunchRequest(cmd=("/bin/true",), label="new-worktree")

        app = UxonApp(self._ctx(on_create_worktree=create), probe_agents=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("enter", "enter")
            await pilot.pause()
            await pilot.pause()
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, WorktreeBranchScreen)
            app.screen.query_one(Input).value = "feature/new"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
        self.assertEqual(created, [("/srv/work", "feature/new", "claude", "normal")])

    async def test_workspace_probe_error_aborts_launch_with_diagnostic(self) -> None:
        # Failed worker completion must not continue to a launch.
        from uxon.tui.app import UxonApp
        from uxon.tui.screens.main import MainScreen

        def broken(*args):
            raise RuntimeError("corrupt HEAD")

        app = UxonApp(self._ctx(on_probe_worktrees=broken), probe_agents=False)
        notices = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.notify = lambda message, **kwargs: notices.append((message, kwargs))
            await pilot.press("enter", "enter")
            await pilot.pause()
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)
            self.assertIsNone(app.pending_launch)
        self.assertTrue(any("corrupt HEAD" in str(n[0]) for n in notices), notices)


@unittest.skipUnless(_textual_available(), "textual not installed")
class LaunchExistingWorktreeWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_project_discovers_workspaces_after_profile_choice(self) -> None:
        # Project selection and asynchronous workspace continuation share state.
        from uxon.infra.agents import AgentAvailability
        from uxon.infra.worktrees import Workspace
        from uxon.tui.app import UxonApp
        from uxon.tui.screens.launch_options import LaunchOptionsScreen
        from uxon.tui.screens.workspace import WorkspaceScreen

        probes = []
        rows = [Workspace(label="main", branch="main", path="/srv/work/proj", is_primary=True)]
        ctx = _mk_ctx(
            enabled_profiles=("claude",),
            default_profile="claude",
            agent_availability={"claude": AgentAvailability(status="ok")},
            existing_projects=[("proj", "2026-05-01")],
            on_probe_worktrees=lambda *a: probes.append(a) or rows,
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.screen._launch_existing()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, LaunchOptionsScreen)
            self.assertEqual(probes, [])
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertIsInstance(app.screen, WorkspaceScreen)
            self.assertEqual(probes, [("/srv/work/proj", "claude", "normal")])


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

        from uxon.infra.settings import SettingEntry, SettingSpec
        from uxon.tui.screens.settings import SettingsScreen
        from uxon.tui.screens.settings_modals import BoolToggleModal

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
            from uxon.tui.screens.settings_modals import BoolToggleModal

            modal = app.screen_stack[-1]
            self.assertIsInstance(modal, BoolToggleModal)
            btn = modal.query_one("#true")
            btn.press()
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        self.assertEqual(saved, [("git_create_enabled", True)])


@unittest.skipUnless(_textual_available(), "textual not installed")
class ValueInputModalTests(unittest.IsolatedAsyncioTestCase):
    """Behaviour of the single-Input edit modals (string/number/array/table).

    These pin the parse/initial-text/commit contract per kind so the
    template-method base (``_ValueInputModal``) can be refactored without
    silent regressions. Inputs are addressed via ``query_one(Input)``
    (one Input per modal) rather than by id, so the assertions survive
    the id being unified.
    """

    def _entry(self, key, kind, value, choices=None):
        from uxon.infra.settings import SettingEntry, SettingSpec

        spec = SettingSpec(key, kind, "desc", choices=choices)
        return SettingEntry(spec=spec, value=value, source="default", editable=True)

    def _cbs(self):
        from uxon.tui.screens.settings import SettingsCallbacks

        saved: list = []
        return saved, SettingsCallbacks(
            get_entries=lambda: [],
            save_setting=lambda k, v: saved.append((k, v)),
            remove_setting=lambda k: None,
            save_mapping=lambda k, v: saved.append((k, v)),
        )

    async def _drive(self, modal, text):
        """Push ``modal``, type ``text`` into its Input, press Enter.

        Returns ``(result, still_open)`` where ``result`` is the dismiss
        value (or ``_UNSET`` if never dismissed) and ``still_open`` says
        whether the modal is still on the screen stack.
        """
        from textual.app import App
        from textual.widgets import Input

        _UNSET = object()
        captured = {"r": _UNSET}

        class Host(App):
            def on_mount(self):
                self.push_screen(modal, lambda r: captured.__setitem__("r", r))

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            modal.query_one(Input).value = text
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            still_open = app.screen is modal
        return captured["r"], still_open

    async def test_string_saves_raw(self):
        from uxon.tui.screens.settings_modals import StringInputModal

        saved, cbs = self._cbs()
        modal = StringInputModal(self._entry("k", "string", ""), cbs)
        result, _ = await self._drive(modal, "  hello world  ")
        # String kind does not strip — the raw value is persisted verbatim.
        self.assertEqual(saved, [("k", "  hello world  ")])
        self.assertTrue(result)

    async def test_number_saves_float(self):
        from uxon.tui.screens.settings_modals import NumberInputModal

        saved, cbs = self._cbs()
        modal = NumberInputModal(self._entry("k", "number", 0), cbs)
        result, _ = await self._drive(modal, "42")
        self.assertEqual(saved, [("k", 42.0)])
        self.assertTrue(result)

    async def test_number_rejects_non_number_and_stays_open(self):
        from uxon.tui.screens.settings_modals import NumberInputModal

        saved, cbs = self._cbs()
        modal = NumberInputModal(self._entry("k", "number", 0), cbs)
        result, still_open = await self._drive(modal, "abc")
        self.assertEqual(saved, [])
        self.assertTrue(still_open)

    async def test_array_parses_csv(self):
        from uxon.tui.screens.settings_modals import ArrayCsvModal

        saved, cbs = self._cbs()
        modal = ArrayCsvModal(self._entry("k", "array", []), cbs)
        await self._drive(modal, "a, b ,c,")
        # Comma-split, each part stripped, empties dropped.
        self.assertEqual(saved, [("k", ["a", "b", "c"])])

    async def test_array_initial_text_renders_current(self):
        from textual.app import App
        from textual.widgets import Input

        from uxon.tui.screens.settings_modals import ArrayCsvModal

        saved, cbs = self._cbs()
        modal = ArrayCsvModal(self._entry("k", "array", ["p", "q"]), cbs)

        class Host(App):
            def on_mount(self):
                self.push_screen(modal)

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(modal.query_one(Input).value, "p, q")

    async def test_number_prefills_falsy_zero(self):
        # A current value of 0 must render as "0", not a blank field —
        # ``str(value or "")`` would wrongly blank it.
        from textual.app import App
        from textual.widgets import Input

        from uxon.tui.screens.settings_modals import NumberInputModal

        _saved, cbs = self._cbs()
        modal = NumberInputModal(self._entry("k", "number", 0), cbs)

        class Host(App):
            def on_mount(self):
                self.push_screen(modal)

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(modal.query_one(Input).value, "0")

    async def test_table_parses_kv_via_save_mapping(self):
        from uxon.tui.screens.settings_modals import TableMappingModal

        saved, cbs = self._cbs()
        modal = TableMappingModal(self._entry("k", "table", {}), cbs)
        await self._drive(modal, "x=1, y=2 , bad, =skip")
        # ``key=value`` pairs only; malformed/keyless parts dropped.
        self.assertEqual(saved, [("k", {"x": "1", "y": "2"})])


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
