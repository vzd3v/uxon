"""Unit tests for :class:`uxon.tui.tui_state.TuiState` and the
:class:`TuiContext` ↔ :class:`TuiState` plumbing introduced in
commit 3 of the TuiContext-split plan.

Pinned contracts:

* ``TuiState()`` constructs with every slot in its zero state and
  ``main is None`` (the "never loaded" sentinel).
* ``TuiContext.refresh_tick`` is a write-through proxy onto
  ``ctx._state.refresh_tick`` when a state is linked. Tests cover
  both sides — read after write goes through state, and reads when
  no state is linked fall back to a private legacy slot.
* ``UxonApp`` constructs a fresh :class:`TuiState`, links the live
  ``ctx._state``, and the App's ``state`` is identity-stable across
  ctx replacement (``app.ctx`` may change; ``app.state`` does not).
* ``MainScreen.loading`` is declared as a writable reactive and has
  no ``compute_loading`` method (a compute would make the reactive
  read-only — see plan §commit 3 / §commit 11 verification).
"""

from __future__ import annotations

import unittest

from uxon.tui.context import TuiContext
from uxon.tui.slot_state import SlotState
from uxon.tui.tui_state import TuiState


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


class TuiStateZeroStateTests(unittest.TestCase):
    def test_main_is_none_initially(self) -> None:
        s = TuiState()
        self.assertIsNone(s.main)

    def test_refresh_tick_starts_at_zero(self) -> None:
        self.assertEqual(TuiState().refresh_tick, 0)

    def test_every_slot_is_zero_state(self) -> None:
        s = TuiState()
        for slot_name in (
            "agent_availability",
            "detected_agents",
            "link_health",
            "cwd_writable",
        ):
            slot = getattr(s, slot_name)
            self.assertIsInstance(slot, SlotState)
            self.assertIsNone(slot.value)
            self.assertIsNone(slot.last_attempt_at)
            self.assertIsNone(slot.last_success_at)
            self.assertEqual(slot.consecutive_failures, 0)
            self.assertFalse(slot.from_cache)
            self.assertEqual(slot.elapsed_ms_recent, ())

    def test_remote_slot_starts_empty(self) -> None:
        s = TuiState()
        self.assertEqual(s.remote, {})

    def test_field_level_mutability(self) -> None:
        # Plan §"Note on container style": TuiState stays mutable
        # (field-level), MainData and SlotState are frozen.
        s = TuiState()
        s.refresh_tick = 7
        self.assertEqual(s.refresh_tick, 7)


class RefreshTickProxyTests(unittest.TestCase):
    def test_proxy_read_after_write_goes_through_state(self) -> None:
        state = TuiState()
        ctx = _bare_ctx()
        ctx._state = state
        ctx.refresh_tick = 5
        self.assertEqual(state.refresh_tick, 5)
        self.assertEqual(ctx.refresh_tick, 5)

    def test_proxy_reflects_external_state_writes(self) -> None:
        state = TuiState()
        ctx = _bare_ctx()
        ctx._state = state
        state.refresh_tick = 11
        self.assertEqual(ctx.refresh_tick, 11)

    def test_legacy_fallback_when_no_state_is_linked(self) -> None:
        ctx = _bare_ctx()
        # No state linked — assignments still round-trip via a
        # private legacy slot. Pinned so test fixtures that build a
        # bare ``TuiContext`` for unit-testing pure helpers don't
        # break in the migration window.
        self.assertEqual(ctx.refresh_tick, 0)
        ctx.refresh_tick = 9
        self.assertEqual(ctx.refresh_tick, 9)
        self.assertIsNone(ctx._state)


class AppStateIntegrationTests(unittest.TestCase):
    def test_app_creates_state_and_links_ctx(self) -> None:
        try:
            import textual  # noqa: F401
        except ImportError:
            self.skipTest("textual not available")
        from uxon.tui.app import UxonApp

        ctx = _bare_ctx(loading=True)
        app = UxonApp(ctx, probe_agents=False)
        self.assertIsInstance(app.state, TuiState)
        self.assertIs(app.ctx._state, app.state)
        # Round-trip through the proxy.
        ctx.refresh_tick = 3
        self.assertEqual(app.state.refresh_tick, 3)

    def test_state_is_identity_stable_across_ctx_replacement(self) -> None:
        try:
            import textual  # noqa: F401
        except ImportError:
            self.skipTest("textual not available")
        from uxon.tui.app import UxonApp

        ctx = _bare_ctx(loading=True)
        app = UxonApp(ctx, probe_agents=False)
        first_state = app.state
        # Plain attribute reassignment — apply_loaded_ctx does this
        # inside the running app; here we exercise the same shape.
        app.ctx = _bare_ctx(loading=False)
        self.assertIs(app.state, first_state)


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


if __name__ == "__main__":
    unittest.main()
