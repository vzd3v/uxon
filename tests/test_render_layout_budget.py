"""Layout/recompose perf-gate for the render-on-demand dashboard (Phase 3).

This is the hard op-count gate that pins the spec's structural acceptance
criteria over a **real** ``MainScreen`` driven by Pilot. It is the net-new
perf-gate paired with the deleted ``test_dashboard_reconcile.py`` (tests
budget — § Tests). It counts mutating calls **at the call site** via
monkeypatch rather than by observed effect, because Textual self-no-ops
``set_class`` / ``display`` / an identical ``refresh`` — so an effect-based
assertion would pass even if the call was wastefully made.

Covered ACs:

* **AC1** — a steady telemetry tick (cell values change, the live-session
  set is unchanged) issues **0** ``refresh(layout=True)`` and **0**
  ``recompose``.
* **AC8** — the dashboard ``Static``s (note, status bars, captions) issue no
  global relayout on that tick (folded into the AC1 ``layout=True`` counter —
  any ``Static.update(layout=True)`` shows up as a ``refresh(layout=True)``).
* **AC2** — an in-bounds ``cursor_down`` sequence issues **0**
  ``refresh(layout=True)`` and **0** ``Footer.recompose``. (An *edge* cursor
  move legitimately hands focus to a sibling, which recomposes the footer —
  the gate drives strictly in-bounds moves, which is the steady-state cost.)
* **AC7** — after first paint the footer is **non-blank** (the recompose
  gating must not suppress the initial compose) and the ``kill*`` bindings
  are visible.
* **AC11** — an identical-data tick (rows + aggregates unchanged) issues
  **zero** widget-mutating calls: no ``refresh`` / ``move_cursor`` on the
  list view, no ``Static.update``, no ``set_class``.
* **AC5** — a 200-row (5 hosts × 40) reorder tick, including a full-block
  reverse, issues **0** ``refresh(layout=True)`` and a bounded, count-gated
  number of structural ops — the **same op-shape** as a steady tick, not the
  old O(n²) tail rebuild. Wall-clock p50/p95 is **logged** via the
  ``tui-table-perf`` debug channel (registered in ``docs/agents/debugging.md``)
  and **not asserted** (repo no-wall-clock-gate practice).

The behaviour ACs (AC3/AC4/AC6) live in the dashboard/order behaviour tests;
AC9/AC10 are the standard local-checks block.
"""

from __future__ import annotations

import time
import unittest
from dataclasses import replace


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _mk_ctx(**overrides):
    """Minimal :class:`TuiContext` for a real ``MainScreen`` Pilot.

    Mirrors the helper in ``test_main_screen_dashboard_own.py``; refresh
    sources are wired empty so ``kick_refresh`` is a no-op and the tick is
    driven by hand via ``app.screen._refresh_dashboard()``.
    """
    from uxon.tui.context import LaunchRequest, TuiContext

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
        on_launch_cwd=lambda agent_id, mode_id, target_dir=None: LaunchRequest(
            cmd=("/bin/true",), label="cwd"
        ),
        on_launch_new=lambda n, agent_id, mode_id, g: LaunchRequest(
            cmd=("/bin/true",), label="new"
        ),
        on_launch_existing=lambda n, agent_id, mode_id: LaunchRequest(
            cmd=("/bin/true",), label="existing"
        ),
    )
    base.update(overrides)
    ctx = TuiContext(**base)
    ctx.refresh_sources = []
    return ctx


def _own_session(name: str, short: str, *, cpu: str = "1.0"):
    from uxon.tui.context import TuiSession

    return TuiSession(
        name=name,
        short=short,
        attached=False,
        pid="1",
        cpu=cpu,
        ram="1M",
        created="1s",
        last_activity="1s",
        cmd="claude",
        path="/srv/work",
        user="devagent",
    )


def _seed_state_main(app, ctx) -> None:
    from uxon.tui.main_data import MainData

    app.state.main = MainData.from_context(ctx)


class _MutationCounter:
    """Install/remove monkeypatches that tally mutating call-site hits.

    Counts at the **call site** (the spec's AC11 requirement): the wrapped
    method increments before delegating, so a self-no-op inside Textual does
    not hide a wasteful call.
    """

    def __init__(self) -> None:
        self.refresh_layout = 0
        self.refresh_any = 0
        self.recompose = 0
        self.move_cursor = 0
        self.static_update = 0
        self.set_class = 0
        self._orig: dict[str, object] = {}

    def install(self) -> None:
        from textual.dom import DOMNode
        from textual.widget import Widget
        from textual.widgets import Static

        from uxon.tui.widgets.session_list_view import SessionListView

        self._orig = {
            "refresh": Widget.refresh,
            "recompose": Widget.recompose,
            "move_cursor": SessionListView.move_cursor,
            "static_update": Static.update,
            "set_class": DOMNode.set_class,
        }
        counter = self

        def refresh(self, *a, **k):  # type: ignore[no-untyped-def]
            counter.refresh_any += 1
            if k.get("layout"):
                counter.refresh_layout += 1
            return counter._orig["refresh"](self, *a, **k)  # type: ignore[operator]

        def recompose(self):  # type: ignore[no-untyped-def]
            counter.recompose += 1
            return counter._orig["recompose"](self)  # type: ignore[operator]

        def move_cursor(self, **k):  # type: ignore[no-untyped-def]
            counter.move_cursor += 1
            return counter._orig["move_cursor"](self, **k)  # type: ignore[operator]

        def static_update(self, *a, **k):  # type: ignore[no-untyped-def]
            counter.static_update += 1
            return counter._orig["static_update"](self, *a, **k)  # type: ignore[operator]

        def set_class(self, *a, **k):  # type: ignore[no-untyped-def]
            counter.set_class += 1
            return counter._orig["set_class"](self, *a, **k)  # type: ignore[operator]

        Widget.refresh = refresh  # type: ignore[method-assign,assignment]
        Widget.recompose = recompose  # type: ignore[method-assign,assignment]
        SessionListView.move_cursor = move_cursor  # type: ignore[method-assign,assignment]
        Static.update = static_update  # type: ignore[method-assign,assignment]
        DOMNode.set_class = set_class  # type: ignore[method-assign,assignment]

    def remove(self) -> None:
        from textual.dom import DOMNode
        from textual.widget import Widget
        from textual.widgets import Static

        from uxon.tui.widgets.session_list_view import SessionListView

        Widget.refresh = self._orig["refresh"]  # type: ignore[method-assign,assignment]
        Widget.recompose = self._orig["recompose"]  # type: ignore[method-assign,assignment]
        SessionListView.move_cursor = self._orig["move_cursor"]  # type: ignore[method-assign,assignment]
        Static.update = self._orig["static_update"]  # type: ignore[method-assign,assignment]
        DOMNode.set_class = self._orig["set_class"]  # type: ignore[method-assign,assignment]


@unittest.skipUnless(_textual_available(), "textual not installed")
class LayoutBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from uxon.tui.dashboard import model as _m

        _m._LAST_OUTPUT = ()

    def tearDown(self) -> None:
        from uxon.tui.dashboard import model as _m

        _m._LAST_OUTPUT = ()

    async def test_steady_telemetry_tick_no_layout_no_recompose(self) -> None:
        """AC1/AC8: a cell-value tick (same session set) → 0 layout, 0 recompose."""
        from uxon.tui.app import UxonApp

        ctx = _mk_ctx(
            sessions=[
                _own_session("devagent.a", "a", cpu="1.0"),
                _own_session("devagent.b", "b", cpu="2.0"),
            ]
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _seed_state_main(app, ctx)
            app.screen._refresh_dashboard()
            await pilot.pause()
            # A new snapshot: same sessions, one cpu value moved. Forces the
            # fleet/status aggregates to re-render — the steady-tick hot path.
            ctx2 = _mk_ctx(
                sessions=[
                    _own_session("devagent.a", "a", cpu="55.0"),
                    _own_session("devagent.b", "b", cpu="2.0"),
                ]
            )
            _seed_state_main(app, ctx2)
            counter = _MutationCounter()
            counter.install()
            try:
                app.screen._refresh_dashboard()
                await pilot.pause()
            finally:
                counter.remove()
            self.assertEqual(counter.refresh_layout, 0, "AC1: a steady tick must not relayout")
            self.assertEqual(counter.recompose, 0, "AC1: a steady tick must not recompose")

    async def test_cursor_down_no_layout_no_footer_recompose(self) -> None:
        """AC2: an in-bounds cursor_down sequence → 0 layout, 0 Footer.recompose."""
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_list_view import SessionListView

        ctx = _mk_ctx(sessions=[_own_session(f"devagent.s{i}", f"s{i}") for i in range(6)])
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _seed_state_main(app, ctx)
            app.screen._refresh_dashboard()
            await pilot.pause()
            widget = app.screen.query_one("#sessions-dashboard", SessionListView)
            widget.focus()
            widget.move_cursor(row=0)
            await pilot.pause()
            counter = _MutationCounter()
            counter.install()
            try:
                # 0 → 1 → 2 → 3: strictly in-bounds (6 rows), so no edge
                # release / focus handoff that would legitimately recompose.
                for _ in range(3):
                    widget.action_cursor_down()
                    await pilot.pause()
            finally:
                counter.remove()
            self.assertEqual(widget.cursor_row, 3)
            self.assertEqual(counter.refresh_layout, 0, "AC2: cursor move must not relayout")
            self.assertEqual(counter.recompose, 0, "AC2: cursor move must not recompose footer")

    async def test_first_paint_footer_non_blank_kill_visible(self) -> None:
        """AC7: footer is non-blank after first paint; kill* bindings visible."""
        from textual.widgets._footer import FooterKey

        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.gated_footer import GatedFooter

        ctx = _mk_ctx(sessions=[_own_session("devagent.a", "a")])
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            footer = app.screen.query_one(GatedFooter)
            self.assertTrue(footer._bindings_ready, "footer must have composed (not blank)")
            keys = list(footer.query(FooterKey))
            self.assertGreater(len(keys), 0, "AC7: footer must be non-blank after first paint")
            descs = [k.description or "" for k in keys]
            self.assertTrue(
                any(d.lower().startswith("kill") for d in descs),
                f"AC7: kill* bindings must be visible in the footer; saw {descs}",
            )

    async def test_identical_data_tick_zero_mutations(self) -> None:
        """AC11: an identical-data tick mutates zero widgets at the call site."""
        from uxon.tui.app import UxonApp

        ctx = _mk_ctx(
            sessions=[
                _own_session("devagent.a", "a"),
                _own_session("devagent.b", "b"),
            ]
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _seed_state_main(app, ctx)
            app.screen._refresh_dashboard()
            await pilot.pause()
            # Re-run with the SAME state.main snapshot — every value identical.
            counter = _MutationCounter()
            counter.install()
            try:
                app.screen._refresh_dashboard()
                await pilot.pause()
            finally:
                counter.remove()
            self.assertEqual(counter.refresh_any, 0, "AC11: no refresh on an identical tick")
            self.assertEqual(counter.move_cursor, 0, "AC11: no move_cursor on an identical tick")
            self.assertEqual(
                counter.static_update, 0, "AC11: no Static.update on an identical tick"
            )
            self.assertEqual(counter.set_class, 0, "AC11: no set_class on an identical tick")

    async def test_reorder_200_rows_same_op_shape_as_steady(self) -> None:
        """AC5: a 200-row reorder (incl. full-block reverse) matches steady op-shape.

        Same row *count*, so 0 ``refresh(layout=True)`` and exactly one
        ``set_rows``-driven repaint — never the O(n²) tail rebuild the old
        DataTable did on a mid-list reorder. Wall-clock p50/p95 is logged to
        the ``tui-table-perf`` channel, never asserted.
        """
        from textual.app import App, ComposeResult

        from uxon.infra import events as ev
        from uxon.tui.dashboard.row import SessionRow
        from uxon.tui.widgets.session_list_view import SessionListView

        def _build_model(*, hosts: int = 5, per_host: int = 40) -> tuple[SessionRow, ...]:
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

        from uxon.tui.dashboard.columns import REGISTRY

        by_id = {c.id: c for c in REGISTRY}
        cols = (by_id["host"], by_id["name"], by_id["cpu"], by_id["ram"])
        model = _build_model()

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionListView(cols, id="dash")

        app = Host()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows(model)
            await pilot.pause()

            counter = _MutationCounter()

            # Steady op-shape baseline: one cell value moves.
            steady = model[:10] + (replace(model[10], cpu_pct=99.0),) + model[11:]
            counter.install()
            try:
                view.set_rows(steady)
                await pilot.pause()
            finally:
                counter.remove()
            steady_refresh = counter.refresh_any
            self.assertEqual(counter.refresh_layout, 0, "steady tick must not relayout")

            # Reorder: full-block reverse of the first host block (mid-list
            # churn that wrecked the old DataTable), same 200-row count.
            block = list(steady[:40])
            block.reverse()
            reordered = tuple(block) + steady[40:]
            counter = _MutationCounter()
            counter.install()
            try:
                view.set_rows(reordered)
                await pilot.pause()
            finally:
                counter.remove()
            reorder_refresh = counter.refresh_any
            self.assertEqual(
                counter.refresh_layout, 0, "AC5: reorder must issue 0 refresh(layout=True)"
            )
            # Same op-shape: a full-block reverse repaints with **no more**
            # ``refresh`` calls than a steady single-cell tick — a small
            # constant, not a count-scaled (O(n) per-row / O(n^2) tail) rebuild.
            # render-on-demand means ``set_rows`` is one repaint regardless of
            # how many rows moved; the steady baseline bounds it.
            self.assertLessEqual(
                reorder_refresh,
                steady_refresh,
                "AC5: reorder op-shape must be bounded by the steady tick "
                f"(no O(n^2) tail rebuild); steady={steady_refresh} reorder={reorder_refresh}",
            )
            self.assertEqual(view.row_count, 200)

            # Wall-clock perf log — surfaced, not gated (no-wall-clock-gate).
            samples_ms: list[float] = []
            cur = reordered
            for n in range(50):
                blk = list(cur[:40])
                blk.reverse()
                nxt = tuple(blk) + cur[40:]
                t0 = time.perf_counter()
                view.set_rows(nxt)
                samples_ms.append((time.perf_counter() - t0) * 1000)
                cur = nxt
            samples_ms.sort()
            ev.debug(
                "tui-table-perf",
                at="reorder_benchmark",
                samples=len(samples_ms),
                p50_ms=round(samples_ms[len(samples_ms) // 2], 3),
                p95_ms=round(samples_ms[int(len(samples_ms) * 0.95)], 3),
            )
            self.assertEqual(len(samples_ms), 50)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
