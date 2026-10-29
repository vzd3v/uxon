"""Pilot tests for Textual screens.

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
class CyclicModalListTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_single_disabled_and_unselected_lists(self) -> None:
        from harness.textual_ready import painted, send_key
        from textual.app import App
        from textual.widgets import ListItem, Static

        from uxon.tui.widgets.cyclic_list_view import CyclicListView

        cases = [
            ((), None, None, None),
            ((False,), 0, 0, 0),
            ((True, False, True, False, True), 1, 3, 3),
            ((True, False, True, False, True), 3, 1, 1),
            ((True, True), None, None, None),
            ((True, False, True, False, True), None, 3, 1),
        ]

        class Host(App):
            def compose(self):
                for disabled, *_ in cases:
                    yield CyclicListView(
                        *(ListItem(Static(str(i)), disabled=d) for i, d in enumerate(disabled))
                    )

        app = Host()
        async with app.run_test() as pilot:
            await painted(pilot)
            for widget, (_, initial, previous, following) in zip(
                app.query(CyclicListView), cases, strict=True
            ):
                with self.subTest(initial=initial, count=len(widget.children)):
                    widget.focus()
                    widget.index = initial
                    await send_key(pilot, "up")
                    self.assertEqual(widget.index, previous)
                    widget.index = initial
                    await send_key(pilot, "down")
                    self.assertEqual(widget.index, following)

    async def test_settings_choice_wraps_and_saves(self) -> None:
        from types import SimpleNamespace

        from uxon.tui.screens.settings_modals import EnumCycleModal

        saved = []
        entry = SimpleNamespace(
            spec=SimpleNamespace(key="choice", description="Choice", choices=("first", "last")),
            value="first",
        )
        scenario = ScreenScenario(
            "enum-wrap",
            lambda: EnumCycleModal(
                entry, SimpleNamespace(save_setting=lambda *args: saved.append(args))
            ),
            press_keys("up", "down", "up", "enter"),
            True,
        )
        self.assertEqual(await run_screen_scenarios([scenario]), [True])
        self.assertEqual(saved, [("choice", "last")])


@unittest.skipUnless(_textual_available(), "textual not installed")
class ContainerPromptSmokeTests(unittest.IsolatedAsyncioTestCase):
    """The TUI confirm affordance for a stopped/absent container.

    With ``approval = "prompt"`` a needed start/create must show a
    ``ConfirmYesNo`` before the prepare runs; on confirm the prepare fires
    and the launch proceeds.
    """

    async def _wait_for(self, pilot, predicate, description) -> None:
        for _ in range(20):
            await pilot.pause()
            if predicate():
                return
        self.fail(f"Timed out waiting for {description}; screen={type(pilot.app.screen).__name__}")

    async def test_prompt_then_confirm_runs_prepare_and_launches(self) -> None:
        from uxon.app.launch import RuntimeGate
        from uxon.infra.agents import AgentAvailability
        from uxon.tui.app import UxonApp
        from uxon.tui.screens.confirm import ConfirmYesNo
        from uxon.tui.screens.launch_options import LaunchOptionsScreen

        prepared: list[bool] = []
        launched: list[str] = []
        order: list[str] = []

        def prepare():
            order.append("prepare")
            prepared.append(True)

        def launch(profile, mode, target_dir=None):
            from uxon.tui.context import LaunchRequest

            order.append(f"launch:{profile}:{mode}")
            return LaunchRequest(cmd=("/bin/true",), label="cwd")

        gate = RuntimeGate(
            needs_prepare=True,
            needs_prompt=True,
            message="Container 'proj-work' is stopped — start and launch?",
            fail_message="",
            prepare=prepare,
        )

        ctx = _mk_ctx(
            on_runtime_gate=lambda *a: gate,
            on_probe_existing_sessions=lambda *a: (),
            on_launch_cwd=launch,
            enabled_profiles=["claude"],
            agent_availability={"claude": AgentAvailability(status="ok", path="/usr/bin/claude")},
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.request_launch = lambda req: launched.append(req.label)  # type: ignore[assignment, method-assign]
            await pilot.press("enter")
            await self._wait_for(
                pilot, lambda: isinstance(app.screen, LaunchOptionsScreen), "launch options"
            )
            await pilot.press("enter")
            await self._wait_for(
                pilot, lambda: isinstance(app.screen, ConfirmYesNo), "runtime confirmation"
            )
            # The container gate must have pushed the confirm BEFORE launching.
            confirm = next((s for s in app.screen_stack if isinstance(s, ConfirmYesNo)), None)
            self.assertIsNotNone(
                confirm, msg="ConfirmYesNo was not shown for the stopped container"
            )
            self.assertEqual(prepared, [], msg="prepare ran before the operator confirmed")
            self.assertEqual(launched, [], msg="launch committed before confirm")
            self.assertEqual(order, [])
            await pilot.press("y")
            await self._wait_for(pilot, lambda: bool(launched), "prepared launch")
        self.assertEqual(prepared, [True], msg="prepare did not run after confirm")
        self.assertEqual(launched, ["cwd"], msg="launch did not proceed after prepare")
        self.assertEqual(order, ["prepare", "launch:claude:normal"])


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
        for state in (WorkerState.PENDING, WorkerState.RUNNING):
            spawned[-1].state = state
            app.kick_refresh()
            self.assertEqual(len(spawned), 1, f"{state} must keep the active source gate")
        for state in (WorkerState.CANCELLED, WorkerState.ERROR, WorkerState.SUCCESS):
            before = len(spawned)
            spawned[-1].state = state
            app.kick_refresh()
            self.assertEqual(len(spawned), before + 1, f"{state} must allow a fresh worker")
            app.kick_refresh()
            self.assertEqual(
                len(spawned), before + 1, "the replacement worker must itself hold the gate"
            )

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

        async def wrong_phrase(app, pilot):
            app.screen.query_one("#confirm-input", Input).value = "kill-al"
            await pilot.press("enter")

        scenarios = [
            ScreenScenario("yesno-y", lambda: ConfirmYesNo("Kill foo?"), press_keys("y"), True),
            ScreenScenario("yesno-n", lambda: ConfirmYesNo("Kill foo?"), press_keys("n"), False),
            ScreenScenario(
                "yesno-cancel", lambda: ConfirmYesNo("Kill foo?"), press_keys("escape"), False
            ),
            ScreenScenario(
                "phrase-mismatch", lambda: ConfirmPhrase("Danger!", "kill-all"), wrong_phrase, False
            ),
            ScreenScenario(
                "phrase-empty",
                lambda: ConfirmPhrase("Danger!", "kill-all"),
                press_keys("enter"),
                False,
            ),
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
            # Wrap from the first profile to cursor (last entry).
            await pilot.press("up")
            await pilot.pause()
            mode_list = screen.query_one("#mode-list", ListView)
            # cursor has exactly two modes in the catalog: normal, yolo.
            self.assertEqual(len(mode_list.children), 2)
            mode_ids = [item.id for item in mode_list.children]
            self.assertEqual(mode_ids, ["mode-normal", "mode-yolo"])
            await pilot.press("right", "up", "down", "up", "enter")
            await pilot.pause()
            self.assertEqual(app.result, ("cursor", "yolo"))


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
                press_keys("up", "down", "enter"),
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
                press_keys("up", "enter"),
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
                press_keys("down", "up", "enter"),
                "profB",
            ),
        ]
        results = await run_screen_scenarios(scenarios)
        self.assertEqual(results, [s.expected for s in scenarios])


@unittest.skipUnless(_textual_available(), "textual not installed")
class ExistingProjectSearchTests(unittest.IsolatedAsyncioTestCase):
    """Standalone pilot tests for live-search wiring: focus-on-mount and
    the match counter — assertions that need direct widget queries
    rather than the dismiss-value harness."""

    async def test_nested_navigation_search_and_launch_keep_key_scopes(self) -> None:
        # Navigation and the launch continuation share worker and focus state.
        import asyncio
        import threading

        from harness.textual_ready import painted, send_key, wait_until
        from textual.widgets import ListView, Static

        from uxon.infra.agents import AgentAvailability
        from uxon.tui.app import UxonApp
        from uxon.tui.context import LaunchRequest
        from uxon.tui.screens.existing import ExistingProjectScreen
        from uxon.tui.screens.launch_options import LaunchOptionsScreen
        from uxon.tui.widgets.filter_input import FilterInput

        calls, probes, launches = [], [], []
        loop_thread = threading.get_ident()
        tree = {
            "beta_group": [("empty", ""), ("project space", "")],
            "beta_group/project space": [("[leaf]", "")],
        }

        def list_directories(path):
            calls.append((path, threading.get_ident()))
            return tree[path]

        def launch(path, profile, mode):
            launches.append((path, profile, mode))
            return LaunchRequest(cmd=("true",), label=path)

        ctx = _mk_ctx(
            existing_projects=[("alpha_group", ""), ("beta_group", "")],
            enabled_profiles=("claude",),
            default_profile="claude",
            agent_availability={"claude": AgentAvailability(status="ok")},
            on_list_project_directories=list_directories,
            on_probe_worktrees=lambda *args: probes.append(args) or [],
            on_launch_existing=launch,
            refresh_sources=[],
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await painted(pilot)
            app.screen._launch_existing()
            await wait_until(
                pilot, lambda: isinstance(app.screen, ExistingProjectScreen), "project picker"
            )
            screen = app.screen
            self.assertIsInstance(screen, ExistingProjectScreen)
            fi = screen.query_one(FilterInput)
            self.assertIs(app.focused, fi.input)
            counter = fi.query_one("#match-count", Static)
            self.assertEqual(str(counter.content), "")
            await send_key(pilot, "up")
            self.assertEqual(screen.query_one(ListView).index, 1)
            await send_key(pilot, "down")
            self.assertEqual(screen.query_one(ListView).index, 0)
            fi.input.value = "no-match"
            await wait_until(pilot, lambda: str(counter.content) == "0 matches", "empty search")
            self.assertEqual(str(counter.content), "0 matches")
            await send_key(pilot, "enter")
            self.assertIs(app.screen, screen)
            self.assertEqual(launches, [])
            self.assertEqual(probes, [])
            await send_key(pilot, "escape")
            self.assertEqual(fi.value, "")
            await send_key(pilot, "escape")
            self.assertIsNot(app.screen, screen)
            app.screen._launch_existing()
            await wait_until(
                pilot, lambda: isinstance(app.screen, ExistingProjectScreen), "reopened picker"
            )
            screen = app.screen
            self.assertIsInstance(screen, ExistingProjectScreen)
            fi = screen.query_one(FilterInput)
            fi.input.value = "grou"
            await wait_until(pilot, lambda: fi.value == "grou", "parent filter")
            await send_key(pilot, "p", "p")
            self.assertEqual(fi.value, "group")
            self.assertEqual(str(fi.query_one("#match-count", Static).content), "2 matches")
            await send_key(pilot, "down")
            self.assertEqual(screen.query_one(ListView).index, 1)
            await send_key(pilot, "right")
            await wait_until(
                pilot,
                lambda: (
                    str(screen.query_one("#project-path", Static).content)
                    == "  /srv/work/beta_group/"
                ),
                "first directory read",
            )
            self.assertEqual(fi.value, "")
            self.assertEqual(
                str(screen.query_one("#project-path", Static).content), "  /srv/work/beta_group/"
            )
            self.assertIs(app.focused, fi.input)
            await send_key(pilot, "down")
            self.assertEqual(screen.query_one(ListView).index, 1)
            await send_key(pilot, "right")
            await wait_until(
                pilot,
                lambda: (
                    str(screen.query_one("#project-path", Static).content)
                    == "  /srv/work/beta_group/project space/"
                ),
                "nested directory read",
            )
            self.assertEqual(
                str(screen.query_one("#project-path", Static).content),
                "  /srv/work/beta_group/project space/",
            )
            await send_key(pilot, "left")
            self.assertEqual(screen.query_one(ListView).index, 1)
            await send_key(pilot, "left")
            self.assertEqual(fi.value, "group")
            self.assertEqual(screen.query_one(ListView).index, 1)
            cursor = fi.input.cursor_position
            await send_key(pilot, "left")
            self.assertEqual(fi.input.cursor_position, cursor)
            self.assertEqual(fi.value, "group")
            await send_key(pilot, "right")
            await wait_until(pilot, lambda: fi.value == "", "child directory resets parent filter")
            fi.input.value = "project"
            await wait_until(
                pilot,
                lambda: str(fi.query_one("#match-count", Static).content) == "1 match",
                "child search scope",
            )
            fi.input.cursor_position = len(fi.input.value)
            await send_key(pilot, "space", " ")
            self.assertEqual(fi.value, "project ")
            await send_key(pilot, "x", "x")
            self.assertEqual(fi.value, "project x")
            await wait_until(
                pilot,
                lambda: str(fi.query_one("#match-count", Static).content) == "0 matches",
                "typed nonmatching child search",
            )
            await send_key(pilot, "backspace")
            await wait_until(
                pilot,
                lambda: str(fi.query_one("#match-count", Static).content) == "1 match",
                "backspace restores child match",
            )
            self.assertEqual(fi.value, "project ")
            self.assertEqual(str(fi.query_one("#match-count", Static).content), "1 match")
            await send_key(pilot, "right")
            await wait_until(
                pilot,
                lambda: "[leaf]" in str(screen.query_one("#existing-list Label").content),
                "filtered child directory read",
            )
            label = screen.query_one("#existing-list Label")
            self.assertIn("[leaf]", str(label.content))
            await send_key(pilot, "enter")
            self.assertIsInstance(app.screen, LaunchOptionsScreen)
            self.assertEqual(probes, [])
            # The launch exits this App; a live-screen paint ACK is inapplicable.
            await pilot.press("enter")
            await asyncio.wait_for(app.workers.wait_for_complete(), 5)
            self.assertEqual(
                probes, [("/srv/work/beta_group/project space/[leaf]", "claude", "normal")]
            )
            self.assertEqual(launches, [("beta_group/project space/[leaf]", "claude", "normal")])
            self.assertEqual(
                [path for path, _ in calls], ["beta_group", "beta_group/project space"] * 2
            )
            self.assertTrue(all(thread != loop_thread for _, thread in calls))

    async def test_failed_and_cancelled_reads_cannot_change_directory(self) -> None:
        # Deferred worker delivery and dismissal require their own app lifecycle.
        import asyncio
        import threading

        from harness.textual_ready import painted, send_key, wait_until
        from textual.widgets import ListView, Static

        from uxon.tui.app import UxonApp
        from uxon.tui.screens.main import MainScreen

        started, release = threading.Event(), threading.Event()
        calls = []

        def list_directories(path):
            calls.append(path)
            if path == "alpha":
                raise PermissionError("directory access denied")
            started.set()
            if not release.wait(5):
                raise TimeoutError("directory read timed out")
            return []

        app = UxonApp(
            _mk_ctx(
                existing_projects=[("alpha", ""), ("beta", "")],
                on_list_project_directories=list_directories,
                refresh_sources=[],
            ),
            probe_agents=False,
        )
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                await painted(pilot)
                app.screen._launch_existing()
                await painted(pilot)
                screen = app.screen
                await send_key(pilot, "right")
                await wait_until(
                    pilot,
                    lambda: (
                        "access denied" in str(screen.query_one("#browser-status", Static).content)
                    ),
                    "directory read failure",
                )
                self.assertEqual(
                    str(screen.query_one("#project-path", Static).content), "  /srv/work/"
                )
                self.assertIn(
                    "access denied", str(screen.query_one("#browser-status", Static).content)
                )
                await send_key(pilot, "down")
                await send_key(pilot, "right")
                await wait_until(pilot, started.is_set, "blocked directory read started")
                await send_key(pilot, "right")
                await send_key(pilot, "enter")
                self.assertIs(app.screen, screen)
                await send_key(pilot, "left")
                release.set()
                await asyncio.wait_for(app.workers.wait_for_complete(), 5)
                await painted(pilot)
                self.assertEqual(calls, ["alpha", "beta"])
                self.assertEqual(
                    str(screen.query_one("#project-path", Static).content), "  /srv/work/"
                )
                self.assertEqual(screen.query_one(ListView).index, 1)
                await send_key(pilot, "right")
                await wait_until(
                    pilot,
                    lambda: (
                        str(screen.query_one("#browser-status", Static).content) == "No subfolders"
                    ),
                    "empty directory read",
                )
                self.assertEqual(
                    str(screen.query_one("#browser-status", Static).content), "No subfolders"
                )
                await send_key(pilot, "enter")
                self.assertIs(app.screen, screen)
                await send_key(pilot, "left")
                started.clear()
                release.clear()
                await send_key(pilot, "right")
                await wait_until(pilot, started.is_set, "dismissed directory read started")
                await send_key(pilot, "escape")
                self.assertIsInstance(app.screen, MainScreen)
                release.set()
                await asyncio.wait_for(app.workers.wait_for_complete(), 5)
                await painted(pilot)
                self.assertIsInstance(app.screen, MainScreen)
        finally:
            release.set()


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

    async def test_bool_editor_save_return_and_git_remote_details(self):
        from textual.app import App
        from textual.geometry import Region
        from textual.widgets import DataTable

        from uxon.infra.settings import SettingEntry, SettingSpec
        from uxon.tui.screens.git_remotes import GitRemotesScreen
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
            modal = app.screen_stack[-1]
            self.assertIsInstance(modal, BoolToggleModal)
            self.assertTrue(await pilot.click("#true"))
            await pilot.pause()
            self.assertIsInstance(app.screen, SettingsScreen)
            self.assertEqual(saved, [("git_create_enabled", True)])
            self.assertEqual(removed, [])
            rows = [
                ("foo", "github.com", "alice", "gh", "alice", "private", ""),
                ("bar", "gitlab.com", "bob", "token", "bob", "public", "~/.tok"),
            ]
            dismissed = []
            app.push_screen(GitRemotesScreen(rows), dismissed.append)
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            self.assertEqual(table.row_count, 2)
            self.assertEqual([tuple(table.get_row_at(i)) for i in range(2)], rows)
            rendered = "".join(
                line.text
                for line in table.render_lines(Region(0, 0, table.size.width, table.size.height))
            )
            for value in ("foo", "github.com", "alice", "bar", "gitlab.com", "bob", "~/.tok"):
                self.assertIn(value, rendered)
            await pilot.press("up")
            self.assertEqual(table.cursor_row, 1)
            await pilot.press("down")
            self.assertEqual(table.cursor_row, 0)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(dismissed, [None])
            self.assertIsInstance(app.screen, SettingsScreen)
            await pilot.press("escape")
            await pilot.pause()
        self.assertEqual(saved, [("git_create_enabled", True)])


@unittest.skipUnless(_textual_available(), "textual not installed")
class ValueInputModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_editors_prefill_validate_and_save_exact_values(self):
        from textual.widgets import Input

        from uxon.infra.settings import SettingEntry, SettingSpec
        from uxon.tui.screens.settings import SettingsCallbacks
        from uxon.tui.screens.settings_modals import (
            ArrayCsvModal,
            NumberInputModal,
            StringInputModal,
            TableMappingModal,
        )

        logs = []

        def editor(modal_type, key, kind, initial):
            saved, mappings = [], []
            logs.append((saved, mappings))
            cbs = SettingsCallbacks(
                get_entries=lambda: [],
                save_setting=lambda k, v: saved.append((k, v)),
                remove_setting=lambda k: self.fail("editor removed a setting"),
                save_mapping=lambda k, v: mappings.append((k, v)),
            )
            return modal_type(
                SettingEntry(SettingSpec(key, kind, "desc"), initial, "default", True), cbs
            )

        async def number(app, pilot):
            modal = app.screen
            field = modal.query_one(Input)
            self.assertEqual(field.value, "0")
            field.value = ""
            await pilot.pause()
            await pilot.press("a", "b", "c", "enter")
            await pilot.pause()
            self.assertIs(app.screen, modal, "invalid number dismissed the editor")
            self.assertEqual(logs[-1], ([], []))
            self.assertEqual(field.value, "abc")
            field.value = "42"
            await pilot.press("enter")

        async def array(app, pilot):
            self.assertEqual(app.screen.query_one(Input).value, "p, q")
            app.screen.query_one(Input).value = "a, b ,c,"
            await pilot.press("enter")

        async def string(app, pilot):
            app.screen.query_one(Input).value = "  hello world  "
            await pilot.press("enter")

        async def mapping(app, pilot):
            app.screen.query_one(Input).value = "x=1, y=2 , bad, =skip"
            await pilot.click("#save")

        scenarios = [
            ScreenScenario(
                "number-invalid-valid",
                lambda: editor(NumberInputModal, "n", "number", 0),
                number,
                True,
            ),
            ScreenScenario(
                "array-prefill-save",
                lambda: editor(ArrayCsvModal, "a", "array", ["p", "q"]),
                array,
                True,
            ),
            ScreenScenario(
                "raw-string", lambda: editor(StringInputModal, "s", "string", ""), string, True
            ),
            ScreenScenario(
                "mapping-sink", lambda: editor(TableMappingModal, "m", "table", {}), mapping, True
            ),
            ScreenScenario(
                "cancel-unsaved",
                lambda: editor(StringInputModal, "cancel", "string", ""),
                press_keys("x", "escape"),
                False,
            ),
        ]
        self.assertEqual(await run_screen_scenarios(scenarios), [True, True, True, True, False])
        self.assertEqual(
            logs,
            [
                ([("n", 42.0)], []),
                ([("a", ["a", "b", "c"])], []),
                ([("s", "  hello world  ")], []),
                ([], [("m", {"x": "1", "y": "2"})]),
                ([], []),
            ],
        )
