"""Unit tests for :func:`uxon.tui.dashboard.order.place` (pure, no Pilot).

``place`` is the frozen-order placement: existing keys keep their slot, new
keys land at their position in ``current_rows`` (the model's host-grouped
order), dead keys are dropped (spec D2 / AC3 / AC4 / AC6). Pure function —
tested directly, no event loop.

``current_rows`` is whatever the model selector emits — already host-grouped
(locals → cfg-order remote blocks, recency within each block). ``place`` must
preserve that grouping: cold start reproduces it verbatim, and a new arrival
lands inside its own block, never flattened above another host by global
recency.
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

    def test_new_arrival_lands_at_its_model_order_slot(self) -> None:
        """A new key lands at its position in ``current_rows`` (AC4).

        ``current_rows`` is model-ordered (recency within block), so the
        arrival's slot between its neighbours is the model's recency-at-
        arrival slot — without re-ranking the frozen rows.
        """
        a = _row("a", last=10.0)
        c = _row("c", last=1.0)
        # Persisted order has a then c (a more recent).
        order = (a.key, c.key)
        # New arrival x, model-ordered between a and c (recency 5).
        x = _row("x", last=5.0)
        result = place(order, (a, x, c))
        self.assertEqual([r.key for r in result], [a.key, x.key, c.key])

    def test_new_arrival_most_recent_goes_first(self) -> None:
        """A freshest arrival the model ranks first lands at the front."""
        a = _row("a", last=10.0)
        c = _row("c", last=1.0)
        order = (a.key, c.key)
        x = _row("x", last=999.0)  # most recent → model puts it first
        # current_rows is model-ordered: x, a, c.
        result = place(order, (x, a, c))
        self.assertEqual(result[0].key, x.key)
        # a and c keep their relative slots behind the arrival.
        self.assertEqual([r.key for r in result], [x.key, a.key, c.key])

    def test_departed_key_is_dropped(self) -> None:
        """A persisted key with no current row is removed."""
        a = _row("a")
        b = _row("b")
        c = _row("c")
        order = (a.key, b.key, c.key)
        # b has departed.
        result = place(order, (a, c))
        self.assertEqual([r.key for r in result], [a.key, c.key])

    def test_cold_start_reproduces_model_order(self) -> None:
        """Cold start (no persisted order) yields ``current_rows`` verbatim.

        Every row is a new arrival, so the result must equal the model's
        already-ordered input — placement never re-sorts a cold start.
        """
        # Model-ordered input (recency desc within the single block).
        b = _row("b", last=3.0)
        c = _row("c", last=2.0)
        a = _row("a", last=1.0)
        result = place((), (b, c, a))
        self.assertEqual([r.key for r in result], [b.key, c.key, a.key])

    def test_cold_start_preserves_host_blocks(self) -> None:
        """Cold start keeps the model's host grouping intact (AC6).

        Global-recency placement would flatten the blocks (a fresh remote
        row above the local block). Model-order placement must not — the
        result equals the host-grouped input exactly.
        """
        # Model order: local block first, then remote ``r`` block — even
        # though the remote rows are more recently attached than the locals.
        loc1 = _row("loc1", host=None, last=1.0)
        loc2 = _row("loc2", host=None, last=2.0)
        rem1 = _row("rem1", host="r", last=100.0)
        rem2 = _row("rem2", host="r", last=99.0)
        # Model emits locals (recency desc) then the remote block.
        current = (loc2, loc1, rem1, rem2)
        result = place((), current)
        self.assertEqual([r.key for r in result], [r.key for r in current])

    def test_multi_host_arrival_lands_inside_its_block(self) -> None:
        """A new remote row lands in its host block, not above the locals.

        Global recency would sort the fresh remote arrival to the front
        (above the locals). Model-order placement keeps it inside the
        remote block where the model put it (AC4 + AC6).
        """
        loc = _row("loc", host=None, last=10.0)
        rem_old = _row("rem_old", host="r", last=5.0)
        order = (loc.key, rem_old.key)
        # New remote row, very recent — the model places it at the head of
        # the remote block (recency within block), still after the local.
        rem_new = _row("rem_new", host="r", last=999.0)
        current = (loc, rem_new, rem_old)  # model order
        result = place(order, current)
        # loc kept at slot 0; the arrival lands inside the remote block.
        self.assertEqual([r.key for r in result], [loc.key, rem_new.key, rem_old.key])

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
