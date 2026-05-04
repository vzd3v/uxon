"""Tests for the pluggable refresh-source registry.

PR1 introduces ``uxon.tui.refresh`` as the seam future asynchronous
data streams (e.g. multi-host SSH collectors) plug into. The contract
this file pins down:

- ``run_source`` is fail-soft: any ``Exception`` raised by a fetcher
  is captured into ``SourceResult.error`` instead of escaping.
- ``KeyboardInterrupt`` and ``SystemExit`` propagate so a user Ctrl-C
  or a process termination is never silently swallowed.
- ``SourceResult`` carries the source name, the value (or None), the
  error (or None), and elapsed wall time in milliseconds.
- A failing source produces a result with ``value is None`` — so a
  per-source dispatch handler that checks ``error`` first and ignores
  ``None`` values cannot be misled into clobbering known-good state
  with empty data.

These tests are pure: no Textual, no event loop. Behaviour wiring
through the app lives in ``test_uxon_tui_screens.WorkerGateTests``.
"""

from __future__ import annotations

import unittest

from uxon.tui.refresh import SourceResult, SourceSpec, run_source


class RunSourceTests(unittest.TestCase):
    def test_returns_value_on_success(self) -> None:
        spec = SourceSpec(name="ok", fetch=lambda: {"hello": 1})
        result = run_source(spec)
        self.assertEqual(result.name, "ok")
        self.assertEqual(result.value, {"hello": 1})
        self.assertIsNone(result.error)
        self.assertGreaterEqual(result.elapsed_ms, 0)

    def test_captures_exception_into_error(self) -> None:
        def boom() -> object:
            raise RuntimeError("disk on fire")

        spec = SourceSpec(name="hot", fetch=boom)
        result = run_source(spec)
        self.assertEqual(result.name, "hot")
        self.assertIsNone(result.value)
        self.assertIsNotNone(result.error)
        assert result.error is not None  # for the type checker
        self.assertIn("disk on fire", result.error)

    def test_captures_typeerror_with_class_name_when_message_empty(self) -> None:
        def boom() -> object:
            raise ValueError()

        result = run_source(SourceSpec(name="bare", fetch=boom))
        # Empty exception message → falls back to class name so the
        # debug log row never carries an empty error= field.
        self.assertEqual(result.error, "ValueError")

    def test_keyboard_interrupt_propagates(self) -> None:
        def boom() -> object:
            raise KeyboardInterrupt

        spec = SourceSpec(name="ctrl_c", fetch=boom)
        with self.assertRaises(KeyboardInterrupt):
            run_source(spec)

    def test_system_exit_propagates(self) -> None:
        def boom() -> object:
            raise SystemExit(2)

        with self.assertRaises(SystemExit) as cm:
            run_source(SourceSpec(name="sysexit", fetch=boom))
        self.assertEqual(cm.exception.code, 2)

    def test_one_sources_failure_does_not_pollute_anothers_result(self) -> None:
        """The headline isolation property the registry exists to guarantee.

        Running a failing source and a healthy source independently
        must produce results that do not influence each other — a
        slow/broken source can never corrupt a sibling's snapshot.
        This is the unit-level proof of the property the multi-host
        feature relies on.
        """

        def boom() -> object:
            raise OSError("ssh: connect timeout")

        slow_failing = SourceSpec(name="remote:gpu-vps", fetch=boom)
        healthy = SourceSpec(name="local_sessions", fetch=lambda: ["one", "two"])

        r_failing = run_source(slow_failing)
        r_healthy = run_source(healthy)

        self.assertIsNone(r_failing.value)
        self.assertEqual(r_healthy.value, ["one", "two"])
        self.assertIsNotNone(r_failing.error)
        self.assertIsNone(r_healthy.error)


class SourceResultTests(unittest.TestCase):
    def test_dataclass_fields(self) -> None:
        # Pin the field set: app.py builds ``_RefreshSourceLanded``
        # from these names; renaming any of them is a cross-file
        # contract change that should fail this test loudly.
        r = SourceResult(name="x", value=None, error=None, elapsed_ms=0)
        self.assertEqual(r.name, "x")
        self.assertIsNone(r.value)
        self.assertIsNone(r.error)
        self.assertEqual(r.elapsed_ms, 0)


class SourceSpecTests(unittest.TestCase):
    def test_default_cadence_attr_matches_legacy_refresh(self) -> None:
        # Sources default to the legacy ``tui_refresh_interval_seconds``
        # cadence so existing callers register without touching the
        # field. Multi-host sources will override to
        # ``remote_refresh_interval_seconds`` (PR3).
        spec = SourceSpec(name="legacy", fetch=lambda: None)
        self.assertEqual(spec.cadence_seconds_attr, "tui_refresh_interval_seconds")
        self.assertTrue(spec.kick_on_mount)

    def test_one_shot_source_via_none_cadence(self) -> None:
        # A source with cadence_seconds_attr=None is run once on mount
        # and never on a periodic timer — pattern reserved for things
        # like the cwd-writable probe (when migrated).
        spec = SourceSpec(name="oneshot", fetch=lambda: None, cadence_seconds_attr=None)
        self.assertIsNone(spec.cadence_seconds_attr)


class DispatchRegistryTests(unittest.TestCase):
    """Stage 8: ``UxonApp`` builds an id → handler dispatch registry
    inspected by :meth:`on__refresh_source_landed`. The legacy
    ``main_ctx_rebuild`` and ``remote:<host>`` paths must keep working
    bit-identical, but they now live in the registry rather than an
    ``if/elif`` ladder. These tests poke at the registry directly so a
    future regression that swaps the lookup back into bespoke control
    flow is caught without a Pilot.
    """

    def _make_app(self) -> object:
        # Lazy import: importing ``uxon.tui.app`` requires textual.
        try:
            from uxon.tui.app import UxonApp
        except ImportError:
            self.skipTest("textual not available")
        from uxon.tui.context import LaunchRequest, TuiContext

        # Build a minimal ``TuiContext`` mirroring the screens-test
        # factory. We don't need a Pilot — only the app constructor,
        # which builds the dispatch registry in ``__init__``.
        ctx = TuiContext(
            sessions=[],
            total_cpu="0",
            total_ram="0",
            version="0.0",
            cwd="/tmp",
            cwd_short="tmp",
            new_project_root="/tmp",
            existing_projects=[],
            cwd_writable=True,
            current_user="u",
            on_launch_cwd=lambda agent_id, mode_id: LaunchRequest(cmd=("/bin/true",), label="cwd"),
            on_launch_new=lambda n, agent_id, mode_id, g: LaunchRequest(
                cmd=("/bin/true",), label="new"
            ),
            on_launch_existing=lambda n, agent_id, mode_id: LaunchRequest(
                cmd=("/bin/true",), label="existing"
            ),
        )
        return UxonApp(ctx, probe_agents=False)

    def test_main_ctx_rebuild_in_exact_registry(self) -> None:
        app = self._make_app()
        self.assertIn("main_ctx_rebuild", app._source_dispatch_exact)  # type: ignore[attr-defined]

    def test_remote_prefix_in_prefix_registry(self) -> None:
        app = self._make_app()
        prefixes = [p for p, _ in app._source_dispatch_prefix]  # type: ignore[attr-defined]
        self.assertIn("remote:", prefixes)

    def test_main_ctx_handler_posts_main_ctx_loaded(self) -> None:
        from uxon.tui.app import _MainCtxLoaded, _RefreshSourceLanded

        app = self._make_app()
        ctx = app.ctx  # type: ignore[attr-defined]
        # Capture posted messages instead of letting them route.
        posted: list[object] = []
        app.post_message = posted.append  # type: ignore[method-assign]

        handler = app._source_dispatch_exact["main_ctx_rebuild"]  # type: ignore[attr-defined]
        handler(_RefreshSourceLanded(name="main_ctx_rebuild", value=ctx))

        self.assertEqual(len(posted), 1)
        self.assertIsInstance(posted[0], _MainCtxLoaded)
        self.assertIs(posted[0].ctx, ctx)  # type: ignore[union-attr]

    def test_unknown_name_falls_through_to_drop(self) -> None:
        from uxon.tui.app import _RefreshSourceLanded

        app = self._make_app()
        # No-op post_message; an unknown source name must NOT post
        # anything (the legacy ladder logged and dropped).
        posted: list[object] = []
        app.post_message = posted.append  # type: ignore[method-assign]
        # Drive the dispatcher directly; it should not raise and not
        # post any message.
        app.on__refresh_source_landed(  # type: ignore[attr-defined]
            _RefreshSourceLanded(name="totally_unknown", value=None)
        )
        self.assertEqual(posted, [])


if __name__ == "__main__":
    unittest.main()
