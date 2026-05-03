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


if __name__ == "__main__":
    unittest.main()
