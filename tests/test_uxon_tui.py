"""Pure-data tests for ``uxon_tui.context``.

Screen / widget / integration tests live in:
  - ``tests/test_uxon_tui_screens.py``        (pilot tests)
  - ``tests/test_uxon_tui_widgets_textual.py`` (pilot tests)
  - ``tests/test_uxon_tui_bindings.py``       (drift guards)
  - ``tests/test_tui_integration.py``        (pty end-to-end)
  - ``tests/test_uxon_tui_logging.py``        (JSONL event log)
"""

from __future__ import annotations

import unittest

from uxon import tui as uxon_tui
from uxon.tui.context import (
    _ACTION_KINDS,
    ACTION_COUNT,
    ServerStatus,
    _digit_hinted_indices,
    _segments,
    _total_items,
    build_items,
)
from uxon.tui.state import (
    CallbackFailure,
    LaunchCommitDecision,
    LaunchOptionsState,
    LaunchOptionsUpdate,
    MainIntent,
    activate_main_index,
    agent_is_pending,
    agent_list_label,
    callback_failure_to_toast,
    compute_all_missing,
    confirm_phrase_matches,
    digit_jump_intent,
    filter_existing_projects,
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
    server_status_line,
    session_intent,
    should_push_agents_unavailable,
    should_show_agents_unavailable,
    should_start_agent_probe,
    update_launch_options_after_availability,
    visible_agent_ids,
    visible_detected_agents,
)


def _ctx(**overrides) -> uxon_tui.TuiContext:
    """Build a TuiContext for tests.

    Translates the legacy ``has_sudo=True/False`` kwarg into the new
    per-target ``sudo_caps`` shape: ``has_sudo=True`` is modelled as
    "one synthetic reachable user" so the visibility predicate
    (``bool(sudo_caps.reachable_users)``) flips on. Tests that need
    fine-grained control pass ``sudo_caps=...`` directly and skip
    ``has_sudo``.
    """
    from uxon.tui.context import SudoCapability

    has_sudo = overrides.pop("has_sudo", None)
    base = dict(
        sessions=[],
        total_cpu="0",
        total_ram="0",
        version="0",
        cwd="/srv/work",
        cwd_short="work",
        new_project_root="/srv/work",
        existing_projects=[],
        cwd_writable=True,
    )
    base.update(overrides)
    if has_sudo is not None and "sudo_caps" not in base:
        base["sudo_caps"] = SudoCapability(
            reachable_users=frozenset({"_synthetic_reachable_"} if has_sudo else set()),
            can_root=bool(has_sudo),
        )
    return uxon_tui.TuiContext(**base)


def _session(name: str = "a", user: str = "u") -> uxon_tui.TuiSession:
    return uxon_tui.TuiSession(
        name=name,
        short=name,
        attached=False,
        pid="1",
        cpu="0",
        ram="0",
        created="1s",
        last_activity="1s",
        cmd="claude",
        path="/",
        user=user,
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
        self.assertIsInstance(req, uxon_tui.LaunchRequest)
        self.assertEqual(req.cmd, ("true",))

    def test_server_status_defaults_to_unavailable(self) -> None:
        ctx = _ctx()
        self.assertEqual(server_status_line(ctx.server_status), "server: unavailable")

    def test_server_status_line_formats_compact_snapshot(self) -> None:
        status = ServerStatus(
            load="0.42",
            cpu="11%",
            ram="2.0G/8.0G 25%",
            disk="20G/100G 20%",
            uptime="3d 2h",
        )
        self.assertEqual(
            server_status_line(status),
            "server: cpu 11% load 0.42 | ram 2.0G/8.0G 25% | disk 20G/100G 20% | up 3d 2h",
        )


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


class ComputeAllMissingTests(unittest.TestCase):
    def test_false_without_enabled(self) -> None:
        self.assertFalse(compute_all_missing(enabled_agents=(), availability={}))

    def test_false_when_pending(self) -> None:
        self.assertFalse(
            compute_all_missing(
                enabled_agents=("claude",),
                availability={"claude": type("A", (), {"status": "pending"})()},
            )
        )

    def test_false_when_any_ok(self) -> None:
        self.assertFalse(
            compute_all_missing(
                enabled_agents=("claude", "codex"),
                availability={
                    "claude": type("A", (), {"status": "ok"})(),
                    "codex": type("A", (), {"status": "missing"})(),
                },
            )
        )

    def test_true_when_all_missing(self) -> None:
        self.assertTrue(
            compute_all_missing(
                enabled_agents=("claude", "codex"),
                availability={
                    "claude": type("A", (), {"status": "missing"})(),
                    "codex": type("A", (), {"status": "timeout"})(),
                },
            )
        )


class ShouldPushAgentsUnavailableTests(unittest.TestCase):
    """Transition-based push gate."""

    def test_no_push_when_state_is_ok(self) -> None:
        self.assertFalse(
            should_push_agents_unavailable(
                last_all_missing=True,
                current_all_missing=False,
                modal_already_on_stack=False,
                pending_launch=False,
            )
        )

    def test_no_push_when_modal_already_up(self) -> None:
        self.assertFalse(
            should_push_agents_unavailable(
                last_all_missing=False,
                current_all_missing=True,
                modal_already_on_stack=True,
                pending_launch=False,
            )
        )

    def test_no_push_when_pending_launch(self) -> None:
        self.assertFalse(
            should_push_agents_unavailable(
                last_all_missing=False,
                current_all_missing=True,
                modal_already_on_stack=False,
                pending_launch=True,
            )
        )

    def test_push_on_first_observation_all_missing(self) -> None:
        self.assertTrue(
            should_push_agents_unavailable(
                last_all_missing=None,
                current_all_missing=True,
                modal_already_on_stack=False,
                pending_launch=False,
            )
        )

    def test_push_on_transition_false_to_true(self) -> None:
        self.assertTrue(
            should_push_agents_unavailable(
                last_all_missing=False,
                current_all_missing=True,
                modal_already_on_stack=False,
                pending_launch=False,
            )
        )

    def test_no_push_when_state_steady_true(self) -> None:
        # Don't spam: same all-missing state on consecutive ticks should
        # not re-push the modal once the user dismissed it.
        self.assertFalse(
            should_push_agents_unavailable(
                last_all_missing=True,
                current_all_missing=True,
                modal_already_on_stack=False,
                pending_launch=False,
            )
        )


class VisibleDetectedAgentsTests(unittest.TestCase):
    def test_empty_when_nothing_detected(self) -> None:
        self.assertEqual(
            visible_detected_agents(detected={}, enabled_agents=("claude",), dismissed=[]),
            [],
        )

    def test_filters_already_enabled(self) -> None:
        self.assertEqual(
            visible_detected_agents(
                detected={"claude": object(), "codex": object()},
                enabled_agents=("claude",),
                dismissed=[],
            ),
            ["codex"],
        )

    def test_filters_dismissed(self) -> None:
        self.assertEqual(
            visible_detected_agents(
                detected={"codex": object(), "cursor": object()},
                enabled_agents=("claude",),
                dismissed=["codex"],
            ),
            ["cursor"],
        )

    def test_keeps_order_of_detected_iter(self) -> None:
        from collections import OrderedDict

        det = OrderedDict()
        det["cursor"] = object()
        det["codex"] = object()
        self.assertEqual(
            visible_detected_agents(
                detected=det,
                enabled_agents=("claude",),
                dismissed=[],
            ),
            ["cursor", "codex"],
        )


class DetectedBannerRenderTests(unittest.TestCase):
    def test_empty_when_no_detected(self) -> None:
        from uxon.tui.widgets.detected_banner import render_banner_text

        self.assertEqual(render_banner_text([], repo_config_writable=True), "")

    def test_single_agent_writable(self) -> None:
        from uxon.tui.widgets.detected_banner import render_banner_text

        text = render_banner_text(["codex"], repo_config_writable=True)
        self.assertIn("codex is installed", text)
        self.assertIn("[a]", text)
        self.assertIn("[x] dismiss", text)

    def test_multi_agent_readonly(self) -> None:
        from uxon.tui.widgets.detected_banner import render_banner_text

        text = render_banner_text(["codex", "cursor"], repo_config_writable=False)
        self.assertIn("codex", text)
        self.assertIn("cursor", text)
        self.assertIn("read-only", text)


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
        self.assertEqual(
            agent_list_label(2, "codex", self._avail("pending")), "2 codex  (checking…)"
        )
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


class FilterExistingProjectsTests(unittest.TestCase):
    def test_empty_needle_returns_full_list_in_order(self) -> None:
        rows = [("zebra", "1s"), ("alpha", "2s"), ("Mango", "3s")]
        self.assertEqual(filter_existing_projects(rows, ""), rows)

    def test_whitespace_only_needle_returns_full_list(self) -> None:
        rows = [("alpha", "1s"), ("beta", "2s")]
        self.assertEqual(filter_existing_projects(rows, "   "), rows)

    def test_substring_match_is_case_insensitive(self) -> None:
        rows = [("Alpha", "1s"), ("beta", "2s"), ("AlphaBeta", "3s")]
        self.assertEqual(
            filter_existing_projects(rows, "ALP"),
            [("Alpha", "1s"), ("AlphaBeta", "3s")],
        )

    def test_no_match_returns_empty_list(self) -> None:
        rows = [("alpha", "1s"), ("beta", "2s")]
        self.assertEqual(filter_existing_projects(rows, "zzz"), [])

    def test_filter_preserves_input_order(self) -> None:
        # mtime-desc order from the loader must survive the filter —
        # we never re-rank by match position.
        rows = [("zeta-foo", "1s"), ("foo-bar", "2s"), ("alpha-foo", "3s")]
        self.assertEqual(
            filter_existing_projects(rows, "foo"),
            rows,
        )

    def test_match_inside_name_not_only_prefix(self) -> None:
        rows = [("my-secret-tool", "1s")]
        self.assertEqual(filter_existing_projects(rows, "secret"), rows)

    def test_does_not_match_against_mtime_column(self) -> None:
        # Filter is name-only — the mtime suffix shouldn't sneak into
        # the haystack and surface false matches like "1s".
        rows = [("alpha", "1s"), ("beta", "2s")]
        self.assertEqual(filter_existing_projects(rows, "1s"), [])


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
        failure = callback_failure_to_toast("Refresh failed", uxon_tui.CallbackError("nope"))
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
        self.assertEqual(
            session_intent(s, "dev"), MainIntent("attach", user="dev", session_name="dev.foo")
        )


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
        import dataclasses

        r = uxon_tui.LaunchRequest(cmd=("tmux", "attach"), label="a")
        self.assertEqual(hash(r), hash(r))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            r.cmd = ("other",)  # type: ignore[misc]

    def test_callback_error_is_exception(self) -> None:
        self.assertTrue(issubclass(uxon_tui.CallbackError, Exception))


class HostHealthBadgeTests(unittest.TestCase):
    """``host_health_badge`` derives a per-host badge from a
    ``RemoteSnapshot`` (or ``None``). Stage 6 reads the snapshot's
    ``error`` / ``from_cache`` / ``cached_at_epoch`` directly — the
    SlotState[T] surface lands at stage 8."""

    def _snap(self, **overrides):
        from uxon.remote_collector import RemoteSnapshot

        defaults: dict[str, object] = dict(
            host_name="prod",
            fetched_at_epoch=1000.0,
            from_cache=False,
            error=None,
            sessions=[],
            cached_at_epoch=1000.0,
        )
        defaults.update(overrides)
        return RemoteSnapshot(**defaults)  # type: ignore[arg-type]

    def test_none_snapshot_is_loading(self) -> None:
        from uxon.tui.state import host_health_badge

        b = host_health_badge(None)
        self.assertEqual(b.status, "loading")
        self.assertEqual(b.text, "loading")

    def test_fresh_success_is_ok(self) -> None:
        from uxon.tui.state import host_health_badge

        b = host_health_badge(self._snap())
        self.assertEqual(b.status, "ok")
        self.assertEqual(b.text, "ok")

    def test_from_cache_no_error_is_stale_with_age(self) -> None:
        from uxon.tui.state import host_health_badge

        b = host_health_badge(self._snap(from_cache=True), now=1027.0)
        self.assertEqual(b.status, "stale")
        self.assertEqual(b.text, "cache 27s")

    def test_from_cache_minutes_age(self) -> None:
        from uxon.tui.state import host_health_badge

        b = host_health_badge(self._snap(from_cache=True), now=1000.0 + 90)
        self.assertEqual(b.text, "cache 1m")

    def test_from_cache_hours_age(self) -> None:
        from uxon.tui.state import host_health_badge

        b = host_health_badge(self._snap(from_cache=True), now=1000.0 + 7200)
        self.assertEqual(b.text, "cache 2h")

    def test_from_cache_with_live_error_appends_err(self) -> None:
        from uxon.tui.state import host_health_badge

        b = host_health_badge(
            self._snap(from_cache=True, error="ssh timeout after 5s"),
            now=1010.0,
        )
        self.assertEqual(b.status, "stale")
        self.assertIn("cache 10s", b.text)
        self.assertIn("err", b.text)

    def test_error_no_cache_is_down_with_short_message(self) -> None:
        from uxon.tui.state import host_health_badge

        b = host_health_badge(
            self._snap(from_cache=False, error="ssh timeout after 5s", cached_at_epoch=None)
        )
        self.assertEqual(b.status, "down")
        self.assertTrue(b.text.startswith("err: "))
        self.assertIn("ssh timeout", b.text)

    def test_error_message_first_line_only(self) -> None:
        from uxon.tui.state import host_health_badge

        b = host_health_badge(
            self._snap(from_cache=False, error="line one\nline two", cached_at_epoch=None)
        )
        self.assertNotIn("\n", b.text)
        self.assertNotIn("line two", b.text)

    def test_error_message_truncated_when_long(self) -> None:
        from uxon.tui.state import host_health_badge

        long_err = "x" * 200
        b = host_health_badge(self._snap(from_cache=False, error=long_err, cached_at_epoch=None))
        # Bounded badge length keeps the section header / table cell
        # from wrapping under pathological peer error strings.
        self.assertLessEqual(len(b.text), 60)


if __name__ == "__main__":
    unittest.main()
