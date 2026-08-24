"""Perf-shape tests for the render-on-demand session dashboard.

The point of this file is **observability**, not gating. The wall-clock
measurement is logged via the ``tui-table-perf`` debug channel and never
asserted on — CI machines vary too much for a useful wall-clock gate here
(repo no-wall-clock-gate practice).

The render-on-demand contract these tests pin (post-reconciler refactor):

* A steady tick that swaps cell values at the same row *count* does not move
  ``virtual_size`` — only a real arrival/departure (count change) does. That
  is the structural signal that "every tick relayouts everything" has not
  crept back in.
* ``set_rows`` lands the rows in the given order at scale; ``render_line``
  builds only the visible viewport regardless of row count.
* 100 steady ticks at 200 rows: p50 / p95 wall time logged via
  ``debug("tui-table-perf", ...)``, not asserted.

The hard layout/recompose op-count gate (``refresh(layout=True)`` /
``Footer.recompose`` counters) lives in ``tests/test_render_layout_budget.py``.
The fixture here is 200 rows × 5 hosts.
"""

from __future__ import annotations

import time
import unittest


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _build_model(*, hosts: int = 5, per_host: int = 40):
    """Return a tuple of SessionRows: ``hosts × per_host`` rows total."""
    from uxon.tui.dashboard.row import SessionRow

    rows: list[SessionRow] = []
    for h in range(hosts):
        host_name = f"host-{h:02d}"
        for i in range(per_host):
            rows.append(
                SessionRow(
                    host=host_name,
                    user="u",
                    name=f"s-{h:02d}-{i:03d}",
                    short=f"s-{h:02d}-{i:03d}",
                    agent="claude",
                    attached=False,
                    legacy=False,
                    pid=1000 + i,
                    cpu_pct=float(i % 30),
                    rss_kib=1024 * (i + 1),
                    created_epoch=None,
                    last_attached_epoch=None,
                    cmd="cmd",
                    path="/tmp",
                )
            )
    return tuple(rows)


def _active_columns():
    from uxon.tui.dashboard.columns import REGISTRY

    by_id = {c.id: c for c in REGISTRY}
    return (by_id["host"], by_id["name"], by_id["cpu"], by_id["ram"])


@unittest.skipUnless(_textual_available(), "textual not installed")
class SessionListViewPerfShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_steady_tick_keeps_virtual_size_height(self) -> None:
        """A same-count cell-value tick must not move ``virtual_size``.

        ``virtual_size`` height changes only on a row *count* change — the
        one legitimate relayout. A steady tick (same 200 rows, one cpu
        value changed) repaints visible lines without resizing.
        """
        from dataclasses import replace

        from textual.app import App, ComposeResult

        from uxon.tui.widgets.session_list_view import SessionListView

        cols = _active_columns()
        model = _build_model()

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionListView(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows(model)
            await pilot.pause()
            height_before = view.virtual_size.height
            nxt = model[:17] + (replace(model[17], cpu_pct=99.0),) + model[18:]
            view.set_rows(nxt)
            await pilot.pause()
            self.assertEqual(view.virtual_size.height, height_before)
            self.assertEqual(view.row_count, len(model))

    async def test_render_line_viewport_only_at_scale(self) -> None:
        """``render_line`` builds the header + in-range rows only at 200 rows."""
        from textual.app import App, ComposeResult

        from uxon.tui.widgets.session_list_view import SessionListView

        cols = _active_columns()
        model = _build_model()

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionListView(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows(model)
            await pilot.pause()
            # Line 0 = header; line 1 = first data row; a line past 200
            # rows is blank (never built).
            self.assertIn("HOST", view.render_line(0).text)
            self.assertIn(model[0].name, view.render_line(1).text)
            self.assertEqual(view.render_line(len(model) + 5).text.strip(), "")

    async def test_100_steady_ticks_log_perf(self) -> None:
        """Run 100 steady ticks, log p50/p95 wall-time; do not assert.

        The wall-clock numbers are operator-facing — surfacing a regression
        in the debug log is the goal, not gating CI.
        """
        from dataclasses import replace

        from textual.app import App, ComposeResult

        from uxon.infra import events as ev
        from uxon.tui.widgets.session_list_view import SessionListView

        cols = _active_columns()
        model = _build_model()

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionListView(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows(model)
            await pilot.pause()

            samples_ms: list[float] = []
            cur = model
            for n in range(100):
                idx = n % len(cur)
                nxt = cur[:idx] + (replace(cur[idx], cpu_pct=float(n % 100)),) + cur[idx + 1 :]
                t0 = time.perf_counter()
                view.set_rows(nxt)
                samples_ms.append((time.perf_counter() - t0) * 1000)
                cur = nxt

            samples_ms.sort()
            p50 = samples_ms[len(samples_ms) // 2]
            p95 = samples_ms[int(len(samples_ms) * 0.95)]
            # Surface the percentiles to the debug channel — operators tail
            # this to spot regressions. NO assertion on wall time.
            ev.debug(
                "tui-table-perf",
                samples=len(samples_ms),
                p50_ms=round(p50, 3),
                p95_ms=round(p95, 3),
            )
            self.assertEqual(len(samples_ms), 100)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
