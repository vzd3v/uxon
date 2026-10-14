"""Availability seeding and loading/rebuild behavior at the App boundary."""

from __future__ import annotations

import unittest

from uxon.tui.context import TuiContext


def _bare_ctx(**overrides) -> TuiContext:
    base = dict(
        sessions=[],
        total_cpu="",
        total_ram="",
        version="",
        cwd="",
        cwd_short="",
        new_project_root="",
        existing_projects=[],
    )
    base.update(overrides)
    return TuiContext(**base)


class MainScreenLoadingReactiveTests(unittest.TestCase):
    def test_loading_is_a_writable_reactive_no_compute(self) -> None:
        try:
            import textual  # noqa: F401
        except ImportError:
            self.skipTest("textual not available")
        from uxon.tui.screens.main import MainScreen

        # Class-level introspection — no Pilot needed. A
        # ``compute_loading`` method would mark the reactive
        # read-only at the descriptor level (textual reactive.py:
        # 330-333), and any subsequent assignment / mutate_reactive
        # would raise. The plan forbids this for the rebuild-source
        # dispatcher's plain-assignment path.
        self.assertFalse(
            hasattr(MainScreen, "compute_loading"),
            "MainScreen.compute_loading must not exist — would make MainScreen.loading read-only.",
        )
        # Sanity: the reactive descriptor is still on the class.
        self.assertTrue(hasattr(MainScreen, "loading"))


class MainScreenLoadingOverlayTests(unittest.IsolatedAsyncioTestCase):
    """``MainScreen.loading`` must NOT engage Textual's full-screen
    ``LoadingIndicator`` overlay.

    The reactive shadows Textual's built-in ``Widget.loading``, whose
    watcher covers the whole widget with a blue ``LoadingIndicator``.
    MainScreen renders its own cold-start skeleton (status line +
    "Loading sessions…" note + action rows), so it overrides
    ``set_loading`` to a no-op. Regression for the frozen-on-loader bug:
    once the in-place refresh path replaced ``switch_screen``, a stuck
    cold-start cover (mounted via a mount-time ``call_later`` that landed
    after ``loading = False``) hid the entire UI — only the dashboard
    table punched through on navigation. Pin it: even with
    ``loading = True`` set explicitly, no cover widget is mounted and the
    composed content stays live.
    """

    async def test_loading_true_does_not_cover_the_screen(self) -> None:
        try:
            import textual  # noqa: F401
        except ImportError:
            self.skipTest("textual not available")
        from textual.widgets import LoadingIndicator

        from uxon.tui.app import UxonApp
        from uxon.tui.widgets import ActionRow

        app = UxonApp(_bare_ctx(loading=True), probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            # Force the overlay path the way the dispatcher would.
            screen.loading = True
            await pilot.pause()
            await pilot.pause()
            # No cover widget, no LoadingIndicator anywhere on the screen.
            self.assertIsNone(getattr(screen, "_cover_widget", None))
            self.assertEqual(len(list(screen.query(LoadingIndicator))), 0)
            # The composed skeleton is still live underneath.
            self.assertTrue(screen.query_one("#action-cwd", ActionRow).display)


def test_availability_seed_copies_nonempty_mapping_in_both_directions() -> None:
    from uxon.tui.app import UxonApp

    seed = {"claude": "pending"}
    ctx = _bare_ctx(loading=True, agent_availability=seed)
    app = UxonApp(ctx, probe_agents=False)
    assert app.state.main is None
    assert app.state.agent_availability.value == {"claude": "pending"}
    seed["claude"] = "missing"
    seed["cursor"] = "ok"
    assert app.state.agent_availability.value == {"claude": "pending"}
    app.state.agent_availability.value["codex"] = "ok"
    assert seed == {"claude": "missing", "cursor": "ok"}


class StateMainCanonicalTests(unittest.TestCase):
    """Stage 8 commit 7: ``state.main`` is canonical, written by
    ``source_dispatch.handle_main_ctx_rebuild``. Pin the contract:

    * On the first rebuild landing the slot flips from None to a
      fresh :class:`MainData`.
    * Subsequent landings replace the slot with a fresh value.
    * ``MainScreen.loading`` follows ``state.main is None`` —
      starts at True (skeleton), flips to False once the first
      rebuild lands.
    """

    def test_first_rebuild_populates_state_main(self) -> None:
        try:
            import textual  # noqa: F401
        except ImportError:
            self.skipTest("textual not available")
        from uxon.tui.app import UxonApp
        from uxon.tui.context import TuiContext
        from uxon.tui.messages import _RefreshSourceLanded
        from uxon.tui.source_dispatch import handle_main_ctx_rebuild

        ctx = _bare_ctx(loading=True)
        app = UxonApp(ctx, probe_agents=False)
        self.assertIsNone(app.state.main)

        # Synthesise a rebuild landing without a Pilot.
        loaded_ctx = TuiContext(
            sessions=[],
            total_cpu="13",
            total_ram="800M",
            version="x",
            cwd="/srv/work",
            cwd_short="work",
            new_project_root="/srv/work",
            existing_projects=[("p", "1m")],
        )
        handle_main_ctx_rebuild(
            app,
            _RefreshSourceLanded(name="main_ctx_rebuild", value=loaded_ctx),
        )
        self.assertIsNotNone(app.state.main)
        self.assertEqual(app.state.main.cwd, "/srv/work")  # type: ignore[union-attr]
        self.assertEqual(app.state.main.total_cpu, "13")  # type: ignore[union-attr]
