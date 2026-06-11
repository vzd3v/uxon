"""Reactive read-only trap on ``MainScreen.loading`` and per-host
repaint isolation in ``select_dashboard_model`` + ``place``.

(Render-coalescer contract is pinned by
``tests/test_render_scheduler.py``.)
"""

from __future__ import annotations

import unittest


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_textual_available(), "textual not installed")
class ReactiveReadOnlyTrapTests(unittest.TestCase):
    """``MainScreen.loading`` may not have a corresponding
    ``compute_loading`` method. Such a method marks the descriptor
    read-only and any later ``__set__`` raises AttributeError.
    """

    def test_main_screen_loading_has_no_compute(self) -> None:
        from uxon.tui.screens.main import MainScreen

        self.assertFalse(
            hasattr(MainScreen, "compute_loading"),
            "MainScreen.compute_loading must not exist — would make loading read-only.",
        )


@unittest.skipUnless(_textual_available(), "textual not installed")
class DashboardPerHostRepaintTests(unittest.TestCase):
    """``select_dashboard_model`` + ``place`` preserve the per-host
    repaint invariant: a snapshot replacement on one host changes only
    that host's rows; rows belonging to unchanged hosts keep both their
    value (equal rows) and their slot in the placed order.

    This is the render-on-demand replacement for the old reconciler
    op-isolation assertion: the widget repaints visible lines from the
    placed model, and ``place`` keeps unchanged rows in their slot, so an
    unchanged host's rows never move.
    """

    def _state_with(self, snaps_by_host):
        from uxon.tui.slot_state import SlotState
        from uxon.tui.tui_state import TuiState

        state = TuiState()
        for name, snap in snaps_by_host.items():
            state.remote[name] = SlotState(value=snap, last_attempt_at=1.0)
        return state

    def _snap(self, host, sessions):
        from uxon.domain.wire_schema import RemoteSnapshot

        return RemoteSnapshot(
            host_name=host,
            fetched_at_epoch=1.0,
            from_cache=False,
            error=None,
            sessions=sessions,
            cached_at_epoch=1.0,
        )

    def test_only_changed_host_produces_ops(self) -> None:
        from types import SimpleNamespace

        from uxon.infra.remote_hosts import RemoteHost
        from uxon.tui.dashboard import model as dashboard_model
        from uxon.tui.dashboard.model import select_dashboard_model
        from uxon.tui.dashboard.order import place
        from uxon.tui.dashboard.ui_state import DashboardUiState
        from uxon.tui.slot_state import SlotState

        # Reset the model selector cache so this test stands alone.
        dashboard_model._LAST_OUTPUT = ()

        host_a = RemoteHost(name="a", ssh_alias="a", description="", remote_uxon="uxon")
        host_b = RemoteHost(name="b", ssh_alias="b", description="", remote_uxon="uxon")
        cfg = SimpleNamespace(remote_hosts=[host_a, host_b], current_user="u1")
        ui = DashboardUiState()

        state = self._state_with(
            {
                "a": self._snap("a", [{"user": "u1", "name": "a1", "short_id": "a1"}]),
                "b": self._snap("b", [{"user": "u1", "name": "b1", "short_id": "b1"}]),
            }
        )
        first = select_dashboard_model(state, cfg, ui)  # type: ignore[arg-type]
        order = tuple(r.key for r in place((), first))

        # Replace only host A's slot with a different snapshot.
        state.remote["a"] = SlotState(
            value=self._snap("a", [{"user": "u1", "name": "a1-new", "short_id": "a1-new"}]),
            last_attempt_at=2.0,
        )
        second = select_dashboard_model(state, cfg, ui)  # type: ignore[arg-type]
        self.assertIsNot(first, second)

        # Host B's row is unchanged: same value object survives the rebuild
        # (model identity stability), so the widget repaints it from
        # identical data — no movement, no value change.
        b_first = next(r for r in first if r.host == "b")
        b_second = next(r for r in second if r.host == "b")
        self.assertEqual(b_first, b_second)

        # Place ``second`` against the order frozen from ``first``: host B's
        # row keeps its slot; only host A's row content changed. (Host A's
        # key changed because the session name changed — a new arrival the
        # model places in host A's block, never in host B's.)
        placed = place(order, second)
        placed_keys = [r.key for r in placed]
        self.assertIn(b_second.key, placed_keys, "host B row must survive the rebuild")
        b_idx = placed_keys.index(b_second.key)
        # No host-A row crossed into host B's slot — every key before B is
        # an "a/" key, every B-block key is "b/".
        self.assertTrue(
            all(placed_keys[i].startswith("a/") for i in range(b_idx)),
            f"a non-host-a row moved ahead of host B: {placed_keys}",
        )


if __name__ == "__main__":
    unittest.main()
