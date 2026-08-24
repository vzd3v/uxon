"""Production-path layout/repaint perf-gate for the idle-quiet TUI render.

This is the hard op-count gate that pins the structural acceptance criteria
over a **real** ``MainScreen`` driven by Pilot **through the production
message path** (``app.post_message(_RefreshSourceLanded(...))`` →
``on__refresh_source_landed`` → ``handle_main_ctx_rebuild`` →
``RenderScheduler`` → ``_render_dirty`` → ``apply_loaded_ctx``). The
predecessor gate drove ``_refresh_dashboard()`` directly and so never saw the
layout storm that lived on ``apply_loaded_ctx``; these gates close that gap.

Two-level counting strategy (each level sees what only it can see):

* **Sink counting** (``Screen._refresh_layout`` invoked while
  ``screen._layout_required`` is ``True`` at call time — the flag is set by
  layout dirt and cleared only *after* the call returns, so a wrapper reads
  it unmasked). This is the AC1 predicate. It cannot be bypassed by a new
  call site, but it cannot see a write that Textual self-no-ops. The
  ``scroll`` kwarg is explicitly NOT the predicate — a coincident scroll
  makes a layout-required relayout arrive with ``scroll=True``; only the
  ``_layout_required`` flag is unmaskable. A separate total-invocation
  counter backs the "zero ``_refresh_layout`` at all" cursor assertion.
* **Call-site counting** (``_MutationCounter``: ``Static.update`` / class
  toggle / ``display`` writes, ``move_cursor``, ``refresh``). Textual
  self-no-ops an unchanged ``display`` / ``set_class`` / identical
  ``refresh``, so only a call-site counter can detect a *wasteful* write —
  one that was made but had no effect.

Covered ACs:

* **AC1** — a steady telemetry-only landing (same session set, same
  workspace/caption/action strings) triggers **zero** layout-required
  relayouts on the production path. The spinner's ``update(layout=False)``
  and the region row-repaints are repaints, not layout dirt, and are
  permitted.
* **AC2** — a value-identical landing issues zero call-site widget writes
  except the single permitted spinner ``Static.update(layout=False)`` on
  ``#server-status`` (``refresh_tick`` increments per landing by
  construction, so the spinner glyph differs every tick).
* **AC4** — an in-viewport cursor move repaints only the affected rows
  (``refresh(Region)``; never a no-arg whole-widget ``refresh()``) and
  triggers **zero** ``Screen._refresh_layout`` of any kind (no scroll, no
  layout dirt).
* **AC5** — crossing the viewport edge scrolls with ``animate=False``; uxon
  adds no whole-screen dirt (no ``Screen.refresh()``-class call) and no
  layout-required relayout. Partialness of the compositor update follows by
  proxy from zero whole-screen dirt plus the framework's region-scoped
  ``ScrollView.watch_scroll_y`` (refreshes ``self.size.region`` only) — it
  is asserted by proxy, not by inspecting the compositor diff.
* **AC9** — the fixture version-guards the pinned Textual internals (fails
  loudly on a Textual upgrade rather than passing vacuously); the
  identical-tick gate keeps call-site counting re-pointed at the production
  path; a modal push/pop round-trip asserts the two AC9 counters separately
  (zero uxon-issued writes on resume; zero layout-required relayouts on
  resume).

The footer-non-blank gate (AC7-class) and the 200-row reorder benchmark
survive unchanged from the predecessor. Wall-clock p50/p95 in the reorder
benchmark is **logged** via the ``tui-table-perf`` debug channel and **never
asserted** (repo no-wall-clock-gate practice). Behavioural checks live in their
own focused tests; the manual py-spy gate is not run here.
"""

from __future__ import annotations

import inspect
import time
import unittest
from dataclasses import replace


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


_PIN_DRIFT_MSG = (
    "Textual upgrade changed pinned internals — re-verify spec § Root causes before bumping"
)


def _assert_pinned_internals() -> None:
    """Fail loudly if Textual drifted away from the seams these gates pin (AC9).

    Same loud-on-drift discipline as GatedFooter's ``_bindings_ready`` guard:
    a Textual rename must hit this clear message, not a raw ``AttributeError``
    deep inside the sink wrapper or the settle predicate. Per-screen instance
    flags (``_layout_required`` etc.) are checked inside the first Pilot via
    :meth:`_assert_screen_flags`, since they exist only on a live screen.
    """
    from textual.app import App
    from textual.screen import Screen
    from textual.widgets import Static

    params = set(inspect.signature(Screen._refresh_layout).parameters)
    if not {"size", "scroll"} <= params:
        raise AssertionError(
            f"{_PIN_DRIFT_MSG}: Screen._refresh_layout(size, scroll) signature "
            f"changed (saw {sorted(params)})"
        )
    if not hasattr(Screen, "_on_timer_update"):
        raise AssertionError(f"{_PIN_DRIFT_MSG}: Screen._on_timer_update is gone")
    if "layout" not in inspect.signature(Static.update).parameters:
        raise AssertionError(f"{_PIN_DRIFT_MSG}: Static.update lost its layout parameter")
    if not hasattr(App, "batch_update"):
        raise AssertionError(f"{_PIN_DRIFT_MSG}: App.batch_update is gone")
    if "_batch_count" not in inspect.getsource(App._end_batch):
        raise AssertionError(f"{_PIN_DRIFT_MSG}: App._end_batch no longer references _batch_count")


def _assert_screen_flags(screen) -> None:  # type: ignore[no-untyped-def]
    """Assert the four live-screen flags the harness reads exist (AC9).

    The sink predicate reads ``_layout_required``; the settle predicate reads
    ``_scroll_required`` / ``_repaint_required`` / ``_dirty_widgets``. A
    Textual rename of any must surface the loud guard message here, not a raw
    ``AttributeError`` inside ``_settle``.
    """
    for attr in ("_layout_required", "_scroll_required", "_repaint_required", "_dirty_widgets"):
        if not hasattr(screen, attr):
            raise AssertionError(f"{_PIN_DRIFT_MSG}: Screen lost the {attr!r} flag")


def _mk_ctx(**overrides):
    """Minimal :class:`TuiContext` for a real ``MainScreen`` Pilot.

    A tiny scheduler debounce / max-latency keeps the production-path settle
    fast (R7): the bounded pump in :meth:`_settle` rides out a 10 ms debounce
    instead of the 300 ms default. Refresh sources are wired empty so the only
    landing is the synthetic one the test posts.
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
        current_user="dana_agent",
        tui_render_debounce_ms=10,
        tui_render_max_latency_ms=50,
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
        user="dana_agent",
    )


async def _land_ctx(pilot, app, ctx) -> None:  # type: ignore[no-untyped-def]
    """Post a ``main_ctx_rebuild`` landing through the real message path.

    ``instance_epoch=-1`` is the unstamped sentinel that passes the epoch
    gate for synthetic posts (``app.py`` epoch check). The handler increments
    ``state.refresh_tick``, folds ``state.main``, sets ``_latest_ctx`` and
    requests a ``main_ctx`` render — exactly the production landing path.

    Pumps (bounded) until the posted message has actually been dispatched
    (``refresh_tick`` advances), so a downstream :func:`_settle` does not read
    its quiet predicate before the message pump has even delivered the post.
    """
    from uxon.tui.messages import _RefreshSourceLanded

    before = app.state.refresh_tick
    app.post_message(_RefreshSourceLanded(name="main_ctx_rebuild", value=ctx, instance_epoch=-1))
    for _ in range(25):
        if app.state.refresh_tick > before:
            return
        await pilot.pause(0.02)


async def _settle(pilot, app, screen) -> None:  # type: ignore[no-untyped-def]
    """Bounded pump until the render pipeline is quiet (AC1; no wall-clock).

    Termination must not depend on the spinner write keeping the screen dirty,
    so the quiet predicate covers both the scheduler (no pending debounce
    timer, no dirty kinds) and the screen update pipeline (no layout / scroll
    dirt, no pending dirty/repaint widgets). Capped at 25 pauses — a fixed
    budget, not a wall-clock assertion.
    """
    _assert_screen_flags(screen)
    for _ in range(25):
        scheduler_quiet = app._render._timer is None and not app._render._dirty
        screen_quiet = (
            not screen._layout_required
            and not screen._scroll_required
            and not screen._dirty_widgets
            and not screen._repaint_required
        )
        if scheduler_quiet and screen_quiet:
            return
        await pilot.pause(0.05)


class _SinkCounter:
    """Tally ``Screen._refresh_layout`` invocations (the AC1 sink predicate).

    Increments ``layout_required`` only when ``self._layout_required`` is
    ``True`` at call time (set by layout dirt, cleared after the call returns
    — verified against ``Screen._on_timer_update``). ``total`` counts every
    invocation regardless of the flag, backing the cursor gate's "zero
    ``_refresh_layout`` at all" assertion. Optionally filtered to one screen
    instance so a modal's own composes do not leak into the MainScreen counts.
    """

    def __init__(self, *, only_screen=None) -> None:  # type: ignore[no-untyped-def]
        self.layout_required = 0
        self.total = 0
        self._only = only_screen
        self._orig = None

    def install(self) -> None:
        from textual.screen import Screen

        self._orig = Screen._refresh_layout
        counter = self

        def refresh_layout(self, *a, **k):  # type: ignore[no-untyped-def]
            if counter._only is None or self is counter._only:
                counter.total += 1
                if getattr(self, "_layout_required", False):
                    counter.layout_required += 1
            return counter._orig(self, *a, **k)  # type: ignore[misc]

        Screen._refresh_layout = refresh_layout  # type: ignore[method-assign,assignment]

    def remove(self) -> None:
        from textual.screen import Screen

        Screen._refresh_layout = self._orig  # type: ignore[method-assign,assignment]


class _UxonLayoutDirtSource:
    """Count *uxon-issued* layout dirt at its source (the AC9 resume predicate).

    The sink (``_SinkCounter``) reads ``_layout_required`` when the deferred
    ``_on_timer_update`` finally relayouts — by then the call stack is the
    framework timer's and no longer names who set the dirt. To distinguish a
    *uxon-issued* relayout from a framework-inherent one (which AC9's
    Scope-Out accepts on a modal pop), this counter wraps ``Widget.refresh``
    and counts a call only when (a) it carries ``layout=True``, (b) it lands
    on the MainScreen under test, and (c) a ``uxon`` frame is on the stack.

    In the dev venv (Textual 8.2.4) tearing the modal screen out of the DOM
    makes the framework itself issue one ``App.post_mount`` →
    ``screen.refresh(layout=True)`` — a relayout, not the pure repaint the
    8.2.7 ``_on_screen_resume`` source models (R1 dual versions). That call
    has no uxon frame on its stack, so it is correctly excluded; what AC9
    forbids is a *uxon-issued* relayout on resume, which this isolates.
    """

    def __init__(self, *, only_screen) -> None:  # type: ignore[no-untyped-def]
        self.uxon_layout = 0
        self._only = only_screen
        self._orig = None

    @staticmethod
    def _stack_has_uxon() -> bool:
        for frame in inspect.stack():
            if frame.frame.f_globals.get("__name__", "").startswith("uxon."):
                return True
        return False

    def install(self) -> None:
        from textual.widget import Widget

        self._orig = Widget.refresh
        counter = self

        def refresh(self, *a, **k):  # type: ignore[no-untyped-def]
            if (
                k.get("layout")
                and getattr(self, "screen", None) is counter._only
                and counter._stack_has_uxon()
            ):
                counter.uxon_layout += 1
            return counter._orig(self, *a, **k)  # type: ignore[misc]

        Widget.refresh = refresh  # type: ignore[method-assign,assignment]

    def remove(self) -> None:
        from textual.widget import Widget

        Widget.refresh = self._orig  # type: ignore[method-assign,assignment]


class _MutationCounter:
    """Install/remove monkeypatches that tally mutating call-site hits.

    Counts at the **call site** (AC9's call-site half): the wrapped method
    increments before delegating, so a self-no-op inside Textual does not hide
    a wasteful call. Optionally filtered to the MainScreen under test so a
    modal's internal compose writes do not pollute the round-trip counts.
    ``ScrollBar`` ``display`` writes (Textual's internal scrollbar management)
    are excluded — they live on the same screen, so the screen filter alone
    would not keep them out. Each ``Static.update`` target id is recorded so
    the spinner-exception assertion can verify the one permitted write hit
    ``#server-status``.
    """

    def __init__(self, *, only_screen=None) -> None:  # type: ignore[no-untyped-def]
        self.refresh_any = 0
        self.refresh_layout = 0
        self.recompose = 0
        self.move_cursor = 0
        self.static_update = 0
        self.static_update_ids: list[str | None] = []
        self.set_class = 0
        self.display = 0
        self._only = only_screen
        self._orig: dict[str, object] = {}

    def _on_screen(self, node) -> bool:  # type: ignore[no-untyped-def]
        if self._only is None:
            return True
        try:
            return node.screen is self._only
        except Exception:
            return False

    def install(self) -> None:
        from textual.dom import DOMNode
        from textual.scrollbar import ScrollBar
        from textual.widget import Widget
        from textual.widgets import Static

        from uxon.tui.widgets.session_list_view import SessionListView

        self._orig = {
            "refresh": Widget.refresh,
            "recompose": Widget.recompose,
            "move_cursor": SessionListView.move_cursor,
            "static_update": Static.update,
            "set_class": DOMNode.set_class,
            "display": DOMNode.display,
        }
        counter = self

        def refresh(self, *a, **k):  # type: ignore[no-untyped-def]
            if counter._on_screen(self):
                counter.refresh_any += 1
                if k.get("layout"):
                    counter.refresh_layout += 1
            return counter._orig["refresh"](self, *a, **k)  # type: ignore[operator]

        def recompose(self):  # type: ignore[no-untyped-def]
            if counter._on_screen(self):
                counter.recompose += 1
            return counter._orig["recompose"](self)  # type: ignore[operator]

        def move_cursor(self, **k):  # type: ignore[no-untyped-def]
            if counter._on_screen(self):
                counter.move_cursor += 1
            return counter._orig["move_cursor"](self, **k)  # type: ignore[operator]

        def static_update(self, *a, **k):  # type: ignore[no-untyped-def]
            if counter._on_screen(self):
                counter.static_update += 1
                counter.static_update_ids.append(getattr(self, "id", None))
            return counter._orig["static_update"](self, *a, **k)  # type: ignore[operator]

        def set_class(self, *a, **k):  # type: ignore[no-untyped-def]
            if counter._on_screen(self):
                counter.set_class += 1
            return counter._orig["set_class"](self, *a, **k)  # type: ignore[operator]

        display_prop = counter._orig["display"]

        def display_fset(self, value):  # type: ignore[no-untyped-def]
            if counter._on_screen(self) and not isinstance(self, ScrollBar):
                counter.display += 1
            return display_prop.fset(self, value)  # type: ignore[union-attr]

        Widget.refresh = refresh  # type: ignore[method-assign,assignment]
        Widget.recompose = recompose  # type: ignore[method-assign,assignment]
        SessionListView.move_cursor = move_cursor  # type: ignore[method-assign,assignment]
        Static.update = static_update  # type: ignore[method-assign,assignment]
        DOMNode.set_class = set_class  # type: ignore[method-assign,assignment]
        DOMNode.display = property(display_prop.fget, display_fset)  # type: ignore[union-attr]

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
        DOMNode.display = self._orig["display"]  # type: ignore[method-assign,assignment]


@unittest.skipUnless(_textual_available(), "textual not installed")
class LayoutBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _assert_pinned_internals()
        from uxon.tui.dashboard import model as _m

        _m._LAST_OUTPUT = ()

    def tearDown(self) -> None:
        from uxon.tui.dashboard import model as _m

        _m._LAST_OUTPUT = ()

    async def test_steady_tick_production_path_zero_layout_relayouts(self) -> None:
        """AC1: a telemetry-only landing → 0 layout-required relayouts, 0 recompose.

        Posts the baseline landing and the steady (one cpu value moved, same
        session set / workspace / caption / action strings) landing through
        the real ``_RefreshSourceLanded`` message path; counts relayouts at
        the sink. The spinner's ``update(layout=False)`` and the row repaints
        are repaints, not layout dirt, and are permitted.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.screens.main import MainScreen

        ctx = _mk_ctx(
            sessions=[
                _own_session("dana_agent.a", "a", cpu="1.0"),
                _own_session("dana_agent.b", "b", cpu="2.0"),
            ]
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, MainScreen)
            await _land_ctx(pilot, app, ctx)
            await _settle(pilot, app, screen)
            # Ride out the debounce cool window so the measured landing fires
            # leading-edge (synchronous) with the counters installed.
            await pilot.pause(0.05)

            ctx2 = _mk_ctx(
                sessions=[
                    _own_session("dana_agent.a", "a", cpu="55.0"),
                    _own_session("dana_agent.b", "b", cpu="2.0"),
                ]
            )
            sink = _SinkCounter(only_screen=screen)
            counter = _MutationCounter(only_screen=screen)
            sink.install()
            counter.install()
            try:
                await _land_ctx(pilot, app, ctx2)
                await _settle(pilot, app, screen)
            finally:
                counter.remove()
                sink.remove()
            self.assertEqual(sink.layout_required, 0, "AC1: a steady tick must not layout-relayout")
            self.assertEqual(counter.recompose, 0, "AC1: a steady tick must not recompose")

    async def test_identical_tick_zero_mutations_production_path(self) -> None:
        """AC2/AC9: a value-identical landing mutates zero widgets, bar the spinner.

        Call-site counting (re-pointed at the production message path): the
        only permitted write is the single spinner ``Static.update`` on
        ``#server-status`` (``refresh_tick`` increments per landing, so the
        glyph differs by construction). No class toggle, no ``display``, no
        ``move_cursor``, no ``SessionListView`` refresh, zero layout-required
        relayouts.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.widgets.session_list_view import SessionListView

        ctx = _mk_ctx(
            sessions=[
                _own_session("dana_agent.a", "a"),
                _own_session("dana_agent.b", "b"),
            ]
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, MainScreen)
            view = screen.query_one("#sessions-dashboard", SessionListView)
            await _land_ctx(pilot, app, ctx)
            await _settle(pilot, app, screen)
            await pilot.pause(0.05)

            # A value-identical ctx: every field equal to the prior landing.
            ctx_same = _mk_ctx(
                sessions=[
                    _own_session("dana_agent.a", "a"),
                    _own_session("dana_agent.b", "b"),
                ]
            )
            sink = _SinkCounter(only_screen=screen)
            counter = _MutationCounter(only_screen=screen)

            view_refreshes = [0]
            orig_view_refresh = SessionListView.refresh

            def _wrapped_view_refresh(self, *a, **k):  # type: ignore[no-untyped-def]
                if self is view:
                    view_refreshes[0] += 1
                return orig_view_refresh(self, *a, **k)

            sink.install()
            counter.install()
            SessionListView.refresh = _wrapped_view_refresh  # type: ignore[method-assign,assignment]
            try:
                await _land_ctx(pilot, app, ctx_same)
                await _settle(pilot, app, screen)
            finally:
                SessionListView.refresh = orig_view_refresh  # type: ignore[method-assign,assignment]
                counter.remove()
                sink.remove()

            self.assertEqual(
                counter.static_update, 1, "AC2: only the spinner repaints on an identical tick"
            )
            self.assertEqual(
                counter.static_update_ids,
                ["server-status"],
                "AC2/AC3: the one permitted Static.update must hit #server-status",
            )
            self.assertEqual(counter.set_class, 0, "AC2: no set_class on an identical tick")
            self.assertEqual(counter.display, 0, "AC2: no display write on an identical tick")
            self.assertEqual(counter.move_cursor, 0, "AC2: no move_cursor on an identical tick")
            self.assertEqual(
                view_refreshes[0], 0, "AC2: no SessionListView.refresh on an identical tick"
            )
            self.assertEqual(sink.layout_required, 0, "AC2: no relayout on an identical tick")

    async def test_cursor_move_region_repaint_then_scroll(self) -> None:
        """AC4/AC5/AC9: in-viewport cursor → region repaint; edge cross → partial scroll.

        Drives a session list **longer than the viewport** through the
        production path, focuses the view, then:

        * In-viewport: every ``SessionListView.refresh`` carries ≥ 1 region
          (no no-arg whole-widget refresh), and ``Screen._refresh_layout`` is
          invoked **zero** times of any kind (no scroll occurred, no layout
          dirt); zero recompose.
        * Edge-crossing: drive past the viewport edge — zero layout-required
          relayouts and zero uxon ``Screen.refresh()`` calls. The framework's
          scroll-only reflow (``_layout_required`` False) is permitted and
          expected.

        AC5 "partial compositor update" entailment: partialness follows from
        zero whole-screen dirt (sink == 0, zero ``Screen.refresh()``) plus the
        framework's region-scoped ``ScrollView.watch_scroll_y`` (refreshes
        ``self.size.region`` only — verified 8.2.4); it is asserted by proxy,
        not by inspecting the compositor diff.
        """
        from textual.screen import Screen

        from uxon.tui.app import UxonApp
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.widgets.session_list_view import SessionListView

        ctx = _mk_ctx(
            sessions=[_own_session(f"dana_agent.s{i:02d}", f"s{i:02d}") for i in range(30)]
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, MainScreen)
            await _land_ctx(pilot, app, ctx)
            await _settle(pilot, app, screen)

            view = screen.query_one("#sessions-dashboard", SessionListView)
            view.focus()
            await _settle(pilot, app, screen)
            view.move_cursor(row=0)
            await _settle(pilot, app, screen)

            # ── In-viewport segment ──────────────────────────────────────
            region_counts: list[int] = []
            orig_view_refresh = SessionListView.refresh

            def _rec_view_refresh(self, *regions, **k):  # type: ignore[no-untyped-def]
                if self is view:
                    region_counts.append(len(regions))
                return orig_view_refresh(self, *regions, **k)

            sink = _SinkCounter(only_screen=screen)
            counter = _MutationCounter(only_screen=screen)
            SessionListView.refresh = _rec_view_refresh  # type: ignore[method-assign,assignment]
            sink.install()
            counter.install()
            try:
                # Stay strictly inside the viewport (well under ~20 visible rows).
                for _ in range(3):
                    view.action_cursor_down()
                    await _settle(pilot, app, screen)
            finally:
                counter.remove()
                sink.remove()
                SessionListView.refresh = orig_view_refresh  # type: ignore[method-assign,assignment]

            self.assertEqual(view.cursor_row, 3)
            self.assertTrue(region_counts, "AC4: cursor move must issue a region refresh")
            self.assertTrue(
                all(n >= 1 for n in region_counts),
                f"AC4: every refresh must carry ≥1 region (no whole-widget); saw {region_counts}",
            )
            self.assertEqual(
                sink.total, 0, "AC4: an in-viewport cursor move must not _refresh_layout at all"
            )
            self.assertEqual(counter.recompose, 0, "AC4: cursor move must not recompose")

            # ── Edge-crossing segment ────────────────────────────────────
            screen_refreshes = [0]
            orig_screen_refresh = Screen.refresh

            def _rec_screen_refresh(self, *a, **k):  # type: ignore[no-untyped-def]
                if self is screen:
                    screen_refreshes[0] += 1
                return orig_screen_refresh(self, *a, **k)

            sink2 = _SinkCounter(only_screen=screen)
            Screen.refresh = _rec_screen_refresh  # type: ignore[method-assign,assignment]
            sink2.install()
            try:
                # Drive well past the viewport edge to force scrolling.
                for _ in range(25):
                    view.action_cursor_down()
                    await _settle(pilot, app, screen)
            finally:
                sink2.remove()
                Screen.refresh = orig_screen_refresh  # type: ignore[method-assign,assignment]

            self.assertGreater(view.cursor_row, 3, "AC5: the cursor must have advanced")
            self.assertEqual(sink2.layout_required, 0, "AC5: scrolling must not layout-relayout")
            self.assertEqual(
                screen_refreshes[0], 0, "AC5: uxon must add no whole-screen Screen.refresh()"
            )

    async def test_modal_roundtrip_zero_uxon_writes_and_relayouts(self) -> None:
        """AC9: a modal push/pop round-trip adds zero uxon writes / relayouts on resume.

        Two counters, asserted separately and both filtered to the MainScreen
        (so the modal's own compose writes do not leak in): (1) zero
        uxon-issued ``Static.update`` / class / ``display`` writes on resume,
        (2) zero **uxon-issued** layout-required relayouts on resume.

        The pop's framework cost is accepted per Scope-Out and excluded from
        (2): on Textual 8.2.7 ``_on_screen_resume`` issues a single
        ``Screen.refresh()`` (pure repaint, never trips the sink); on the dev
        venv 8.2.4 tearing the modal out of the DOM additionally makes the
        framework issue one ``App.post_mount`` → ``screen.refresh(layout=True)``
        relayout (R1 dual versions). Both are framework-originated (no uxon
        frame on the stack), so the source-attributed counter
        (:class:`_UxonLayoutDirtSource`) excludes them while still catching any
        relayout uxon code would issue on resume.
        """
        from uxon.tui.app import UxonApp
        from uxon.tui.screens.confirm import ConfirmYesNo
        from uxon.tui.screens.main import MainScreen

        ctx = _mk_ctx(
            sessions=[
                _own_session("dana_agent.a", "a"),
                _own_session("dana_agent.b", "b"),
            ]
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, MainScreen)
            await _land_ctx(pilot, app, ctx)
            await _settle(pilot, app, screen)

            dirt = _UxonLayoutDirtSource(only_screen=screen)
            counter = _MutationCounter(only_screen=screen)
            dirt.install()
            counter.install()
            try:
                app.push_screen(ConfirmYesNo("Confirm?"))
                await _settle(pilot, app, screen)
                app.pop_screen()
                await _settle(pilot, app, screen)
            finally:
                counter.remove()
                dirt.remove()

            self.assertEqual(counter.static_update, 0, "AC9: no uxon Static.update on modal resume")
            self.assertEqual(counter.set_class, 0, "AC9: no uxon set_class on modal resume")
            self.assertEqual(counter.display, 0, "AC9: no uxon display write on modal resume")
            self.assertEqual(
                dirt.uxon_layout, 0, "AC9: no uxon-issued layout-required relayout on modal resume"
            )

    async def test_first_paint_footer_non_blank_kill_visible(self) -> None:
        """AC7: footer is non-blank after first paint; kill* bindings visible."""
        from textual.widgets._footer import FooterKey

        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.gated_footer import GatedFooter

        ctx = _mk_ctx(sessions=[_own_session("dana_agent.a", "a")])
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
