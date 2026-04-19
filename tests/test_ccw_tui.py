"""Pure-data tests for ``ccw_tui.context``.

Screen / widget / integration tests live in:
  - ``tests/test_ccw_tui_screens.py``        (pilot tests)
  - ``tests/test_ccw_tui_widgets_textual.py`` (pilot tests)
  - ``tests/test_ccw_tui_bindings.py``       (drift guards)
  - ``tests/test_tui_integration.py``        (pty end-to-end)
  - ``tests/test_ccw_tui_logging.py``        (JSONL event log)
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.abspath(os.path.join(_HERE, "..", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import ccw_tui  # noqa: E402
from ccw_tui.context import (  # noqa: E402
    ACTION_COUNT,
    _ACTION_KINDS,
    _digit_hinted_indices,
    _segments,
    _total_items,
    build_items,
)


def _ctx(**overrides) -> "ccw_tui.TuiContext":
    base = dict(
        sessions=[],
        total_cpu="0",
        total_ram="0",
        version="0",
        cwd="/srv/work",
        cwd_short="work",
        new_project_root="/srv/work",
        existing_projects=[],
        cwd_allowed=True,
    )
    base.update(overrides)
    return ccw_tui.TuiContext(**base)


def _session(name: str = "a", user: str = "u") -> "ccw_tui.TuiSession":
    return ccw_tui.TuiSession(
        name=name, short=name, attached=False, pid="1", cpu="0", ram="0",
        created="1s", last_activity="1s", cmd="claude", path="/", user=user,
    )


class TuiContextShapeTests(unittest.TestCase):
    def test_tui_context_has_launch_fields(self) -> None:
        ctx = _ctx()
        self.assertTrue(callable(ctx.on_attach))
        self.assertTrue(callable(ctx.on_kill))
        self.assertTrue(callable(ctx.on_launch_cwd))
        self.assertTrue(callable(ctx.on_launch_new))
        self.assertTrue(callable(ctx.on_launch_existing))
        self.assertTrue(callable(ctx.on_refresh))

    def test_tui_context_defaults_return_noop_launch_request(self) -> None:
        ctx = _ctx()
        req = ctx.on_launch_cwd(False)
        self.assertIsInstance(req, ccw_tui.LaunchRequest)
        self.assertEqual(req.cmd, ("true",))


class SegmentsTests(unittest.TestCase):
    def test_no_superuser_block_without_sudo(self) -> None:
        ctx = _ctx(has_sudo=False)
        own, other, settings_idx, kill_idx, has_super = _segments(ctx)
        self.assertEqual(own, ACTION_COUNT)
        self.assertEqual(other, ACTION_COUNT)
        self.assertEqual(settings_idx, -1)
        self.assertEqual(kill_idx, -1)
        self.assertFalse(has_super)

    def test_sudo_with_no_sessions_shows_only_settings(self) -> None:
        ctx = _ctx(has_sudo=True)
        own, other, settings_idx, kill_idx, has_super = _segments(ctx)
        self.assertTrue(has_super)
        self.assertEqual(settings_idx, ACTION_COUNT)
        self.assertEqual(kill_idx, -1)

    def test_sudo_with_only_own_sessions_has_settings_and_kill(self) -> None:
        ctx = _ctx(has_sudo=True, sessions=[_session("a")])
        own, other, settings_idx, kill_idx, has_super = _segments(ctx)
        self.assertEqual(settings_idx, ACTION_COUNT + 1)
        self.assertEqual(kill_idx, ACTION_COUNT + 2)

    def test_superuser_block_adds_other_sessions_plus_settings_plus_kill(self) -> None:
        ctx = _ctx(
            has_sudo=True,
            sessions=[_session("own1")],
            other_sessions=[_session("other1", "alice")],
        )
        own, other, settings_idx, kill_idx, has_super = _segments(ctx)
        self.assertEqual(other, ACTION_COUNT + 1)
        self.assertEqual(settings_idx, ACTION_COUNT + 2)
        self.assertEqual(kill_idx, ACTION_COUNT + 3)

    def test_total_items_includes_actions(self) -> None:
        ctx = _ctx(sessions=[_session("a"), _session("b")])
        self.assertEqual(_total_items(ctx), ACTION_COUNT + 2)

    def test_total_items_no_sessions(self) -> None:
        self.assertEqual(_total_items(_ctx()), ACTION_COUNT)


class BuildItemsTests(unittest.TestCase):
    def test_actions_always_first_three_items(self) -> None:
        items = build_items(_ctx())
        self.assertEqual([i.kind for i in items[:3]], list(_ACTION_KINDS))

    def test_action_items_have_digit_hints_1_through_3(self) -> None:
        items = build_items(_ctx())
        self.assertEqual([i.digit_hint for i in items[:3]], [1, 2, 3])

    def test_settings_item_has_no_digit_hint(self) -> None:
        ctx = _ctx(has_sudo=True)
        items = build_items(ctx)
        [settings] = [i for i in items if i.kind == "settings"]
        self.assertIsNone(settings.digit_hint)

    def test_kill_all_item_has_no_digit_hint(self) -> None:
        ctx = _ctx(has_sudo=True, sessions=[_session("a")])
        items = build_items(ctx)
        [kill] = [i for i in items if i.kind == "kill-all-global"]
        self.assertIsNone(kill.digit_hint)

    def test_action_open_identity_stable_under_session_change(self) -> None:
        ctx0 = _ctx()
        ctx1 = _ctx(sessions=[_session("a"), _session("b")])
        a0 = next(i for i in build_items(ctx0) if i.kind == "action-open")
        a1 = next(i for i in build_items(ctx1) if i.kind == "action-open")
        self.assertEqual(a0.label, a1.label)

    def test_build_items_length_matches_total_items(self) -> None:
        ctx = _ctx(has_sudo=True, sessions=[_session("a")], other_sessions=[_session("o", "x")])
        self.assertEqual(len(build_items(ctx)), _total_items(ctx))

    def test_action_count_derived_from_action_kinds(self) -> None:
        self.assertEqual(ACTION_COUNT, len(_ACTION_KINDS))


class DigitHintedIndicesTests(unittest.TestCase):
    def test_digit_allowed_excludes_settings_and_kill(self) -> None:
        ctx = _ctx(has_sudo=True)  # fresh superuser; settings at ACTION_COUNT
        allowed = _digit_hinted_indices(ctx)
        self.assertNotIn(ACTION_COUNT, allowed)  # Settings excluded.

    def test_digit_allowed_is_subset_of_valid_items(self) -> None:
        ctx = _ctx(has_sudo=True, sessions=[_session("a")])
        allowed = _digit_hinted_indices(ctx)
        total = _total_items(ctx)
        for i in allowed:
            self.assertLess(i, total)

    def test_digit_allowed_actions_always_included(self) -> None:
        ctx = _ctx()
        allowed = _digit_hinted_indices(ctx)
        for i in range(ACTION_COUNT):
            self.assertIn(i, allowed)

    def test_digit_allowed_includes_session_rows(self) -> None:
        ctx = _ctx(sessions=[_session("a"), _session("b")])
        allowed = _digit_hinted_indices(ctx)
        self.assertIn(ACTION_COUNT, allowed)
        self.assertIn(ACTION_COUNT + 1, allowed)


class LaunchRequestShapeTests(unittest.TestCase):
    def test_launch_request_is_hashable_and_immutable(self) -> None:
        r = ccw_tui.LaunchRequest(cmd=("tmux", "attach"), label="a")
        self.assertEqual(hash(r), hash(r))
        with self.assertRaises(Exception):
            r.cmd = ("other",)  # type: ignore[misc]

    def test_callback_error_is_exception(self) -> None:
        self.assertTrue(issubclass(ccw_tui.CallbackError, Exception))


if __name__ == "__main__":
    unittest.main()
