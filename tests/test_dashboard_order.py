"""Unit tests for :func:`uxon.tui.dashboard.order.place` (pure, no Pilot).

``place`` is the frozen-order placement: existing keys keep their slot, new
keys are inserted by recency-at-arrival, dead keys are dropped (spec D2 /
AC3 / AC4). Pure function — tested directly, no event loop.
"""

from __future__ import annotations

import unittest

from uxon.tui.dashboard.order import place
from uxon.tui.dashboard.row import SessionRow


def _row(name: str, *, host: str | None = None, last: float | None = None) -> SessionRow:
    return SessionRow(
        host=host,
        user="u",
        name=name,
        short=name,
        agent="claude",
        attached=False,
        legacy=False,
        pid=None,
        cpu_pct=0.0,
        rss_kib=0,
        created_epoch=None,
        last_attached_epoch=last,
        cmd="",
        path="",
    )


class PlaceTests(unittest.TestCase):
    def test_existing_rows_keep_their_slot(self) -> None:
        """A telemetry tick that only changes recency does not reorder.

        Even though ``b`` is now more recent than ``a``, the persisted
        order (a, b, c) is preserved — existing rows never swap (AC3).
        """
        a = _row("a", last=1.0)
        b = _row("b", last=100.0)  # would sort first by recency
        c = _row("c", last=2.0)
        order = (a.key, b.key, c.key)
        result = place(order, (a, b, c))
        self.assertEqual([r.key for r in result], [a.key, b.key, c.key])

    def test_new_arrival_placed_by_recency(self) -> None:
        """A new key is inserted among current rows by recency (AC4)."""
        a = _row("a", last=10.0)
        c = _row("c", last=1.0)
        # Persisted order has a then c (a more recent).
        order = (a.key, c.key)
        # New arrival x with recency between a and c.
        x = _row("x", last=5.0)
        result = place(order, (a, x, c))
        # x's recency (5) sits between a (10) and c (1): a, x, c.
        self.assertEqual([r.key for r in result], [a.key, x.key, c.key])

    def test_new_arrival_most_recent_goes_first(self) -> None:
        """The freshest arrival lands ahead of less-recent existing rows."""
        a = _row("a", last=10.0)
        c = _row("c", last=1.0)
        order = (a.key, c.key)
        x = _row("x", last=999.0)  # most recent
        result = place(order, (a, x, c))
        self.assertEqual(result[0].key, x.key)

    def test_departed_key_is_dropped(self) -> None:
        """A persisted key with no current row is removed."""
        a = _row("a")
        b = _row("b")
        c = _row("c")
        order = (a.key, b.key, c.key)
        # b has departed.
        result = place(order, (a, c))
        self.assertEqual([r.key for r in result], [a.key, c.key])

    def test_empty_persisted_order_places_all_by_recency(self) -> None:
        """Cold start (no persisted order): every row is a new arrival."""
        a = _row("a", last=1.0)
        b = _row("b", last=3.0)
        c = _row("c", last=2.0)
        result = place((), (a, b, c))
        # All new → sorted by recency desc: b (3), c (2), a (1).
        self.assertEqual([r.key for r in result], [b.key, c.key, a.key])

    def test_empty_current_rows_returns_empty(self) -> None:
        a = _row("a")
        self.assertEqual(place((a.key,), ()), ())

    def test_stale_and_new_keys_together(self) -> None:
        """Mixed tick: one departs, one stays, one arrives."""
        a = _row("a", last=10.0)
        b = _row("b", last=5.0)
        order = (a.key, b.key)
        # b departs, a stays, new arrival d (recency 7, between a and... only a left).
        d = _row("d", last=7.0)
        result = place(order, (a, d))
        # a kept at slot 0; d (7) < a (10 → rank -10) so d after a.
        self.assertEqual([r.key for r in result], [a.key, d.key])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
