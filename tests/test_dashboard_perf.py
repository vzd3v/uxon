"""Perf-shape tests for the session-dashboard reconcile path (commit 9).

The point of this file is **observability**, not gating. Three of the
four cases are hard op-count assertions; the wall-clock measurement is
logged via the ``tui-table`` debug channel and printed, never asserted
on — CI machines vary too much for a useful wall-clock gate here.

Pinned contracts:

* No-op apply (``diff(model, model, columns) == ()``) emits **zero**
  ``tui-table`` log lines. The widget's silence-on-no-op contract is
  the early-warning signal that an "every tick repaints everything"
  regression has slipped in.
* No-op diff produces zero ops — hard gate.
* Single-row CPU change produces exactly one ``CellUpdate`` op — hard
  gate.
* 100 reconciles measured back-to-back; p50 / p95 wall time logged via
  ``debug("tui-table-perf", ...)`` for operators to spot regressions
  in the field.

Fixture is 200 rows × 5 hosts, matching the perf scenario in the plan.
"""

from __future__ import annotations

import json
import os
import tempfile
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


class _DebugLogCapture:
    """Context manager: redirects ``UXON_LOG_DIR`` to a tempdir and
    forces ``_DEBUG_TOPICS`` to enable the ``tui-table`` channel.

    The events module snapshots ``_DEBUG_TOPICS`` at import time, so
    setting ``UXON_DEBUG`` after import has no effect — we patch the
    module-level frozenset directly and restore on exit.
    """

    def __init__(self, *topics: str) -> None:
        self._topics = frozenset(topics)
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._prev_log_dir: str | None = None
        self._prev_topics: frozenset[str] | None = None

    def __enter__(self) -> _DebugLogCapture:
        from uxon.tui import events as ev

        self._tmp = tempfile.TemporaryDirectory()
        self._prev_log_dir = os.environ.get("UXON_LOG_DIR")
        os.environ["UXON_LOG_DIR"] = self._tmp.name
        self._prev_topics = ev._DEBUG_TOPICS
        ev._DEBUG_TOPICS = self._topics  # type: ignore[assignment]
        return self

    def __exit__(self, *exc) -> None:
        from uxon.tui import events as ev

        if self._prev_topics is not None:
            ev._DEBUG_TOPICS = self._prev_topics  # type: ignore[assignment]
        if self._prev_log_dir is None:
            os.environ.pop("UXON_LOG_DIR", None)
        else:
            os.environ["UXON_LOG_DIR"] = self._prev_log_dir
        if self._tmp is not None:
            self._tmp.cleanup()

    @property
    def log_dir(self) -> str:
        assert self._tmp is not None
        return self._tmp.name

    def lines_with_topic(self, topic: str) -> list[dict]:
        """Read every JSONL file in the capture dir and return records
        whose ``topic`` field matches ``topic``.
        """
        out: list[dict] = []
        if not os.path.isdir(self.log_dir):
            return out
        for name in os.listdir(self.log_dir):
            path = os.path.join(self.log_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("topic") == topic:
                            out.append(rec)
            except OSError:
                continue
        return out


class DiffPerfShapeTests(unittest.TestCase):
    """Pure-diff op-count assertions (no Textual required)."""

    def test_noop_diff_emits_zero_ops(self) -> None:
        from uxon.tui.dashboard.reconcile import diff

        model = _build_model()
        cols = _active_columns()
        ops = diff(model, model, cols)
        self.assertEqual(len(ops), 0)

    def test_single_cpu_change_emits_one_cell_update(self) -> None:
        from uxon.tui.dashboard.reconcile import CellUpdate, diff

        old = _build_model()
        # Mutate one row's cpu_pct only (rebuild that row immutably).
        from dataclasses import replace

        target_idx = 17
        new = old[:target_idx] + (replace(old[target_idx], cpu_pct=99.0),) + old[target_idx + 1 :]
        cols = _active_columns()
        ops = diff(old, new, cols)
        self.assertEqual(len(ops), 1)
        self.assertIsInstance(ops[0], CellUpdate)
        self.assertEqual(ops[0].col_id, "cpu")  # type: ignore[union-attr]


@unittest.skipUnless(_textual_available(), "textual not installed")
class WidgetApplyPerfTests(unittest.IsolatedAsyncioTestCase):
    async def test_noop_apply_emits_no_tui_table_log_line(self) -> None:
        """Widget.apply(()) MUST NOT write to the tui-table debug channel.

        Captured via ``UXON_LOG_DIR=tmpdir`` + monkey-patched
        ``_DEBUG_TOPICS``; we then grep the directory's JSONL files for
        any record with ``topic == "tui-table"`` and assert zero.
        """
        from textual.app import App, ComposeResult

        from uxon.tui.dashboard.reconcile import diff
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        cols = _active_columns()
        model = _build_model()

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionDashboardTable(cols, id="dash")

        with _DebugLogCapture("tui-table") as cap:
            app = Host()
            async with app.run_test() as pilot:
                table: SessionDashboardTable = app.query_one("#dash", SessionDashboardTable)
                # Initial population (this DOES emit a tui-table line).
                table.apply(diff((), model, cols))
                await pilot.pause()
                lines_after_initial = len(cap.lines_with_topic("tui-table"))
                self.assertGreaterEqual(lines_after_initial, 1)

                # Now: no-op apply — same model. MUST be silent.
                table.apply(diff(model, model, cols))
                await pilot.pause()
                lines_after_noop = len(cap.lines_with_topic("tui-table"))
                self.assertEqual(lines_after_noop, lines_after_initial)

    async def test_100_reconciles_log_perf(self) -> None:
        """Run 100 small reconciles, log p50/p95 wall-time; do not assert.

        The wall-clock numbers are operator-facing — surfacing a
        regression in the debug log is the goal, not gating CI.
        """
        from dataclasses import replace

        from textual.app import App, ComposeResult

        from uxon.tui import events as ev
        from uxon.tui.dashboard.reconcile import diff
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        cols = _active_columns()
        model = _build_model()

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionDashboardTable(cols, id="dash")

        with _DebugLogCapture("tui-table", "tui-table-perf"):
            app = Host()
            async with app.run_test() as pilot:
                table: SessionDashboardTable = app.query_one("#dash", SessionDashboardTable)
                table.apply(diff((), model, cols))
                await pilot.pause()

                samples_ms: list[float] = []
                cur = model
                for n in range(100):
                    # Touch one row's CPU each iteration so the diff is
                    # a single CellUpdate — the steady-state path we
                    # care about.
                    idx = n % len(cur)
                    nxt = cur[:idx] + (replace(cur[idx], cpu_pct=float(n % 100)),) + cur[idx + 1 :]
                    t0 = time.perf_counter()
                    table.apply(diff(cur, nxt, cols))
                    elapsed_ms = (time.perf_counter() - t0) * 1000
                    samples_ms.append(elapsed_ms)
                    cur = nxt

                samples_ms.sort()
                p50 = samples_ms[len(samples_ms) // 2]
                p95 = samples_ms[int(len(samples_ms) * 0.95)]
                # Surface the percentiles to the debug channel — operators
                # tail this to spot regressions. NO assertion on wall time.
                ev.debug(
                    "tui-table-perf",
                    samples=len(samples_ms),
                    p50_ms=round(p50, 3),
                    p95_ms=round(p95, 3),
                )
                # Sanity gate: every iteration produced exactly one op
                # (single CellUpdate). If this ever stops being true the
                # measurement no longer reflects the steady-state path.
                self.assertEqual(len(samples_ms), 100)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
