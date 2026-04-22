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
from ccw_tui.state import (  # noqa: E402
    CallbackFailure,
    LaunchCommitDecision,
    LaunchOptionsState,
    LaunchOptionsUpdate,
    MainIntent,
    activate_main_index,
    agent_is_pending,
    agent_list_label,
    callback_failure_to_toast,
    confirm_phrase_matches,
    digit_jump_intent,
    launch_commit_decision,
    launch_mode_id,
    launch_options_state,
    main_action_intent,
    mode_item_ids,
    pick_index,
    pick_visible_agent,
    project_name_error,
    project_name_valid,
    resettable_setting_key,
    selected_setting_index,
    session_intent,
    should_start_agent_probe,
    should_show_agents_unavailable,
    update_launch_options_after_availability,
    visible_agent_ids,
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
        req = ctx.on_launch_cwd("claude", "normal")
        self.assertIsInstance(req, ccw_tui.LaunchRequest)
        self.assertEqual(req.cmd, ("true",))


class AgentsUnavailableGateStateTests(unittest.TestCase):
    def test_probe_starts_only_when_enabled(self) -> None:
        self.assertTrue(should_start_agent_probe(probe_agents=True, enabled_agents=("claude",)))
        self.assertFalse(should_start_agent_probe(probe_agents=False, enabled_agents=("claude",)))
        self.assertFalse(should_start_agent_probe(probe_agents=True, enabled_agents=()))

    def test_gate_false_when_already_shown(self) -> None:
        result = should_show_agents_unavailable(
            enabled_agents=("claude",),
            availability={"claude": type("Avail", (), {"status": "missing"})()},
            already_shown=True,
        )
        self.assertFalse(result)

    def test_gate_false_without_enabled_agents(self) -> None:
        self.assertFalse(
            should_show_agents_unavailable(
                enabled_agents=(),
                availability={},
                already_shown=False,
            )
        )

    def test_gate_false_until_every_agent_resolved(self) -> None:
        result = should_show_agents_unavailable(
            enabled_agents=("claude", "codex"),
            availability={"claude": type("Avail", (), {"status": "missing"})()},
            already_shown=False,
        )
        self.assertFalse(result)

    def test_gate_false_when_any_agent_is_ok(self) -> None:
        result = should_show_agents_unavailable(
            enabled_agents=("claude", "codex"),
            availability={
                "claude": type("Avail", (), {"status": "ok"})(),
                "codex": type("Avail", (), {"status": "missing"})(),
            },
            already_shown=False,
        )
        self.assertFalse(result)

    def test_gate_true_when_all_agents_missing_or_timeout(self) -> None:
        result = should_show_agents_unavailable(
            enabled_agents=("claude", "codex"),
            availability={
                "claude": type("Avail", (), {"status": "missing"})(),
                "codex": type("Avail", (), {"status": "timeout"})(),
            },
            already_shown=False,
        )
        self.assertTrue(result)


class LaunchOptionsStateTests(unittest.TestCase):
    def _avail(self, status: str):
        return type("Avail", (), {"status": status})()

    def test_visible_agents_include_pending_ok_and_unknown(self) -> None:
        visible = visible_agent_ids(
            enabled_agents=("claude", "codex", "cursor"),
            availability={
                "claude": self._avail("ok"),
                "codex": self._avail("pending"),
            },
        )
        self.assertEqual(visible, ("claude", "codex", "cursor"))

    def test_visible_agents_exclude_missing_and_timeout(self) -> None:
        visible = visible_agent_ids(
            enabled_agents=("claude", "codex", "cursor"),
            availability={
                "claude": self._avail("missing"),
                "codex": self._avail("timeout"),
                "cursor": self._avail("ok"),
            },
        )
        self.assertEqual(visible, ("cursor",))

    def test_initial_agent_prefers_default_when_visible(self) -> None:
        state = launch_options_state(
            enabled_agents=("claude", "codex"),
            default_agent="codex",
            availability={},
        )
        self.assertEqual(
            state,
            LaunchOptionsState(
                visible_agents=("claude", "codex"),
                single_agent=False,
                active_panel="agent",
                current_agent="codex",
            ),
        )

    def test_initial_agent_falls_back_to_first_visible(self) -> None:
        state = launch_options_state(
            enabled_agents=("claude", "codex"),
            default_agent="claude",
            availability={"claude": self._avail("missing")},
        )
        self.assertEqual(state.current_agent, "codex")
        self.assertTrue(state.single_agent)
        self.assertEqual(state.active_panel, "mode")

    def test_pick_visible_agent_ignores_out_of_range_index(self) -> None:
        self.assertEqual(pick_visible_agent(("claude",), 4, "claude"), "claude")

    def test_agent_is_pending_only_for_pending_status(self) -> None:
        self.assertTrue(agent_is_pending("claude", {"claude": self._avail("pending")}))
        self.assertFalse(agent_is_pending("claude", {"claude": self._avail("ok")}))
        self.assertFalse(agent_is_pending("claude", {}))

    def test_agent_list_label_marks_pending_agents(self) -> None:
        self.assertEqual(agent_list_label(2, "codex", self._avail("pending")), "2 codex  (checking…)")
        self.assertEqual(agent_list_label(1, "claude", self._avail("ok")), "1 claude")
        self.assertEqual(agent_list_label(1, "claude", None), "1 claude")

    def test_mode_item_ids_match_catalog_order(self) -> None:
        self.assertEqual(
            mode_item_ids("cursor"),
            ("mode-normal", "mode-yolo"),
        )

    def test_launch_mode_id_uses_selected_mode_or_normal_fallback(self) -> None:
        self.assertEqual(launch_mode_id("cursor", 1), "yolo")
        self.assertEqual(launch_mode_id("cursor", 99), "normal")
        self.assertEqual(launch_mode_id("nosuch", 0), None)

    def test_launch_update_all_missing_dismisses(self) -> None:
        update = update_launch_options_after_availability(
            enabled_agents=("claude", "codex"),
            default_agent="claude",
            availability={
                "claude": self._avail("missing"),
                "codex": self._avail("timeout"),
            },
            current_agent="claude",
            active_panel="agent",
        )
        self.assertEqual(
            update,
            LaunchOptionsUpdate(
                visible_agents=(),
                single_agent=True,
                active_panel="mode",
                current_agent="claude",
                dismiss=True,
            ),
        )

    def test_launch_update_two_to_one_switches_to_mode_panel(self) -> None:
        update = update_launch_options_after_availability(
            enabled_agents=("claude", "cursor"),
            default_agent="claude",
            availability={
                "claude": self._avail("missing"),
                "cursor": self._avail("ok"),
            },
            current_agent="claude",
            active_panel="agent",
        )
        self.assertEqual(update.visible_agents, ("cursor",))
        self.assertTrue(update.single_agent)
        self.assertEqual(update.active_panel, "mode")
        self.assertEqual(update.current_agent, "cursor")
        self.assertFalse(update.dismiss)

    def test_launch_update_pending_agent_stays_visible(self) -> None:
        update = update_launch_options_after_availability(
            enabled_agents=("claude", "cursor"),
            default_agent="claude",
            availability={
                "claude": self._avail("pending"),
                "cursor": self._avail("ok"),
            },
            current_agent="claude",
            active_panel="agent",
        )
        self.assertEqual(update.visible_agents, ("claude", "cursor"))
        self.assertEqual(update.current_agent, "claude")
        self.assertEqual(update.active_panel, "agent")

    def test_launch_commit_ignores_pending_agent_panel(self) -> None:
        decision = launch_commit_decision(
            active_panel="agent",
            current_agent="claude",
            availability={"claude": self._avail("pending")},
            mode_index=0,
        )
        self.assertEqual(decision, LaunchCommitDecision("ignore"))

    def test_launch_commit_switches_panel_or_commits_mode(self) -> None:
        self.assertEqual(
            launch_commit_decision(
                active_panel="agent",
                current_agent="claude",
                availability={"claude": self._avail("ok")},
                mode_index=0,
            ),
            LaunchCommitDecision("switch-to-mode"),
        )
        self.assertEqual(
            launch_commit_decision(
                active_panel="mode",
                current_agent="cursor",
                availability={},
                mode_index=1,
            ),
            LaunchCommitDecision("commit", "yolo"),
        )


class PickIndexStateTests(unittest.TestCase):
    def test_pick_index_returns_name_for_valid_index(self) -> None:
        self.assertEqual(pick_index([("alpha", "1s"), ("beta", "2s")], 1), "beta")

    def test_pick_index_returns_none_for_negative_or_too_large_index(self) -> None:
        rows = [("alpha", "1s")]
        self.assertIsNone(pick_index(rows, -1))
        self.assertIsNone(pick_index(rows, 9))

    def test_pick_index_preserves_empty_string_sentinel(self) -> None:
        self.assertEqual(pick_index([("", "skip"), ("main", "desc")], 0), "")


class MainScreenIntentStateTests(unittest.TestCase):
    def test_main_action_intent_for_action_rows(self) -> None:
        self.assertEqual(main_action_intent("action-cwd"), MainIntent("launch-cwd"))
        self.assertEqual(main_action_intent("action-new"), MainIntent("launch-new"))
        self.assertEqual(main_action_intent("action-open"), MainIntent("launch-existing"))
        self.assertEqual(main_action_intent("settings"), MainIntent("open-settings"))
        self.assertEqual(main_action_intent("kill-all-global"), MainIntent("kill-all-global"))
        self.assertIsNone(main_action_intent("unknown"))

    def test_digit_jump_activates_hinted_action(self) -> None:
        ctx = _ctx()
        self.assertEqual(digit_jump_intent(ctx, 1), MainIntent("launch-cwd", index=0))

    def test_refresh_is_screen_wiring_only(self) -> None:
        self.assertEqual(main_action_intent("action-cwd"), MainIntent("launch-cwd"))

    def test_callback_failure_to_toast(self) -> None:
        failure = callback_failure_to_toast("Refresh failed", ccw_tui.CallbackError("nope"))
        self.assertEqual(failure, CallbackFailure("Refresh failed: nope", "error"))

    def test_digit_jump_focuses_settings_without_activation(self) -> None:
        ctx = _ctx(has_sudo=True)
        self.assertEqual(digit_jump_intent(ctx, 4), MainIntent("focus-only", index=3))

    def test_digit_jump_out_of_range_is_none(self) -> None:
        self.assertIsNone(digit_jump_intent(_ctx(), 9))

    def test_activate_main_index_attaches_own_session(self) -> None:
        ctx = _ctx(sessions=[_session("dev.foo", "stored-owner")], current_user="dev")
        self.assertEqual(
            activate_main_index(ctx, ACTION_COUNT),
            MainIntent("attach", index=ACTION_COUNT, user="dev", session_name="dev.foo"),
        )

    def test_activate_main_index_attaches_other_session(self) -> None:
        ctx = _ctx(
            has_sudo=True,
            sessions=[],
            other_sessions=[_session("alice.foo", "alice")],
            current_user="dev",
        )
        self.assertEqual(
            activate_main_index(ctx, ACTION_COUNT),
            MainIntent("attach", index=ACTION_COUNT, user="alice", session_name="alice.foo"),
        )

    def test_session_intent_defaults_blank_user_to_current_user(self) -> None:
        s = _session("dev.foo", "")
        self.assertEqual(session_intent(s, "dev"), MainIntent("attach", user="dev", session_name="dev.foo"))


class ModalStateTests(unittest.TestCase):
    def test_confirm_phrase_matches_after_strip_only(self) -> None:
        self.assertTrue(confirm_phrase_matches(" kill-all ", "kill-all"))
        self.assertFalse(confirm_phrase_matches("KILL-ALL", "kill-all"))

    def test_project_name_validation_and_errors(self) -> None:
        self.assertTrue(project_name_valid("demo"))
        self.assertFalse(project_name_valid(""))
        self.assertFalse(project_name_valid("a/b"))
        self.assertFalse(project_name_valid("."))
        self.assertEqual(project_name_error(""), "Name cannot be empty")
        self.assertEqual(project_name_error("a/b"), "Name cannot contain '/'")
        self.assertEqual(project_name_error(".."), "Invalid name")


class SettingsStateTests(unittest.TestCase):
    def test_selected_setting_index_accounts_for_git_view(self) -> None:
        self.assertIsNone(selected_setting_index(row=0, has_git_view=True, entry_count=2))
        self.assertEqual(selected_setting_index(row=1, has_git_view=True, entry_count=2), 0)
        self.assertEqual(selected_setting_index(row=0, has_git_view=False, entry_count=2), 0)
        self.assertIsNone(selected_setting_index(row=3, has_git_view=True, entry_count=2))

    def test_resettable_setting_key_requires_editable_entry(self) -> None:
        spec = type("Spec", (), {"key": "session_prefix"})()
        editable = type("Entry", (), {"spec": spec, "editable": True})()
        readonly = type("Entry", (), {"spec": spec, "editable": False})()
        self.assertEqual(resettable_setting_key(editable), "session_prefix")
        self.assertIsNone(resettable_setting_key(readonly))
        self.assertIsNone(resettable_setting_key(None))


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
