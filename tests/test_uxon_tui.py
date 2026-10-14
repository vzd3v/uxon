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
from types import SimpleNamespace
from unittest import mock

from uxon import tui as uxon_tui
from uxon.domain.agents import DEFAULT_AGENT_CATALOG as _CATALOG
from uxon.tui.context import (
    LaunchProfileOption,
    ServerStatus,
)
from uxon.tui.state import (
    LaunchCommitDecision,
    LaunchOptionsState,
    LaunchOptionsUpdate,
    MainIntent,
    ProjectBrowserLevel,
    compute_all_missing,
    confirm_phrase_matches,
    filter_existing_projects,
    launch_commit_decision,
    launch_options_state,
    launch_permission_modes,
    launch_profile_users_differ,
    main_action_intent,
    pick_index,
    pick_visible_agent,
    project_name_error,
    project_name_valid,
    resettable_setting_key,
    selected_setting_index,
    server_status_line,
    should_push_agents_unavailable,
    update_launch_options_after_availability,
    visible_agent_ids,
)

_CATALOG_IDS = tuple(_CATALOG)


class WorkspaceContinuationTests(unittest.TestCase):
    def test_out_of_order_probes_keep_current_modal_and_its_repository(self) -> None:
        from helpers import make_launch_profile_options

        from uxon.infra.worktrees import Workspace
        from uxon.tui.screens.launch_flow import LaunchFlow
        from uxon.tui.screens.workspace import WorkspaceScreen

        pushed, workers, created = [], [], []
        app = SimpleNamespace(
            state=None,
            push_screen=lambda screen, callback: pushed.append((screen, callback)),
            run_off_loop=lambda fn, **kw: workers.append((fn, kw)),
            request_launch=mock.Mock(),
            notify=mock.Mock(),
        )
        cfg = _ctx(
            launch_profiles=make_launch_profile_options(),
            on_create_worktree=lambda *args: created.append(args),
        )
        host = SimpleNamespace(
            app=app,
            cfg=cfg,
            state=SimpleNamespace(agent_availability=SimpleNamespace(value=cfg.agent_availability)),
        )
        app.screen = host
        flow = LaunchFlow(host)
        for target in ("/srv/a/subdir", "/srv/b/subdir"):
            flow.begin_launch_in_folder(
                target_dir=target, target_label=target, commit_primary=mock.Mock()
            )
            pushed[-1][1](("claude", "normal"))
        for index, root in ((1, "/srv/b"), (0, "/srv/a")):
            workers[index][1]["on_success"]([Workspace(root, "main", root, True)])
            if index == 1:
                app.screen = pushed[-1][0]
        self.assertIsInstance(app.screen, WorkspaceScreen)
        self.assertEqual(len(pushed), 3)  # two profile choices, only the current workspace
        pushed[-1][1](("new", None))
        pushed[-1][1]("feature/b")
        workers[-1][0]()
        self.assertEqual(created, [("/srv/b", "feature/b", "claude", "normal")])
        app.screen = host
        flow.begin_launch_in_folder(
            target_dir="/srv/c", target_label="c", commit_primary=mock.Mock()
        )
        pushed[-1][1](("claude", "normal"))
        app.screen = SimpleNamespace()  # an unrelated modal opened during the current probe
        count = len(pushed)
        workers[-1][1]["on_success"]([Workspace("c", "main", "/srv/c", True)])
        self.assertEqual(len(pushed), count)


def _ctx(**overrides) -> uxon_tui.TuiContext:
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
    return uxon_tui.TuiContext(**base)


def _availability(**statuses):
    return {key: SimpleNamespace(status=status) for key, status in statuses.items()}


def test_server_status_full_and_partial_snapshots() -> None:
    for status, expected in (
        (ServerStatus(), "server: unavailable"),
        (ServerStatus(cpu="11%"), "server: cpu 11% load -"),
        (ServerStatus(load="0.42"), "server: cpu - load 0.42"),
        (
            ServerStatus(cpu="11%", load="0.42", ram="2G/8G", disk="20G/100G", uptime="3d"),
            "server: cpu 11% load 0.42 · ram 2G/8G · disk 20G/100G · up 3d",
        ),
    ):
        assert server_status_line(status) == expected


def test_all_missing_requires_resolved_enabled_profiles() -> None:
    for enabled, availability, expected in (
        ((), {}, False),
        (("claude", "codex"), _availability(claude="missing"), False),
        (("claude", "codex"), _availability(claude="missing", codex="pending"), False),
        (("claude", "codex"), _availability(claude="ok", codex="missing"), False),
        (("claude", "codex"), _availability(claude="missing", codex="timeout"), True),
        (("claude",), _availability(claude="unknown"), False),
    ):
        assert compute_all_missing(enabled_profiles=enabled, availability=availability) is expected


def test_unavailable_modal_transitions_and_suppressions() -> None:
    for previous, current, modal, launch, expected in (
        (None, True, False, False, True),
        (False, True, False, False, True),
        (True, True, False, False, False),
        (True, False, False, False, False),
        (None, False, False, False, False),
        (False, True, True, False, False),
        (False, True, False, True, False),
    ):
        assert (
            should_push_agents_unavailable(
                last_all_missing=previous,
                current_all_missing=current,
                modal_already_on_stack=modal,
                pending_launch=launch,
            )
            is expected
        )


def test_launch_options_visibility_preference_and_availability_transitions() -> None:
    enabled = ("claude", "codex", "cursor")
    availability = _availability(claude="pending", codex="ok")
    assert (
        visible_agent_ids(
            enabled_profiles=enabled, availability=availability, catalog_ids=_CATALOG_IDS
        )
        == enabled
    )
    assert launch_options_state(
        enabled_profiles=enabled, default_profile="codex", availability=availability
    ) == LaunchOptionsState(enabled, False, "agent", "codex")
    assert launch_options_state(
        enabled_profiles=enabled, default_profile="unknown", availability=availability
    ) == LaunchOptionsState(enabled, False, "agent", "claude")
    for availability, current, default, panel, expected in (
        (
            _availability(claude="pending", cursor="ok"),
            "claude",
            "codex",
            "agent",
            LaunchOptionsUpdate(enabled, False, "agent", "claude", False),
        ),
        (
            _availability(claude="missing", codex="ok", cursor="ok"),
            "claude",
            "codex",
            "agent",
            LaunchOptionsUpdate(("codex", "cursor"), False, "agent", "codex", False),
        ),
        (
            _availability(claude="missing", codex="ok", cursor="ok"),
            "unknown",
            "unknown",
            "workspace",
            LaunchOptionsUpdate(("codex", "cursor"), False, "workspace", "codex", False),
        ),
        (
            _availability(claude="missing", codex="timeout", cursor="ok"),
            "claude",
            "claude",
            "agent",
            LaunchOptionsUpdate(("cursor",), True, "mode", "cursor", False),
        ),
        (
            _availability(claude="missing", codex="timeout", cursor="missing"),
            "claude",
            "claude",
            "agent",
            LaunchOptionsUpdate((), True, "mode", "claude", True),
        ),
        (
            _availability(claude="missing", codex="timeout", cursor="missing"),
            "",
            "codex",
            "agent",
            LaunchOptionsUpdate((), True, "mode", "codex", True),
        ),
    ):
        assert (
            update_launch_options_after_availability(
                enabled_profiles=enabled,
                default_profile=default,
                availability=availability,
                current_agent=current,
                active_panel=panel,
            )
            == expected
        )
    assert launch_options_state(
        enabled_profiles=enabled,
        default_profile="claude",
        availability=_availability(claude="missing", codex="timeout", cursor="ok"),
    ) == LaunchOptionsState(("cursor",), True, "mode", "cursor")
    for visible, index, expected in (
        (("claude", "codex"), -1, "cursor"),
        (("claude", "codex"), 0, "claude"),
        (("claude", "codex"), 1, "codex"),
        (("claude", "codex"), 2, "cursor"),
        ((), 0, "cursor"),
    ):
        assert pick_visible_agent(visible, index, "cursor") == expected


def test_profile_collision_keeps_underlying_modes_and_selected_id() -> None:
    profiles = {"cursor": LaunchProfileOption("cursor", "Claude", "claude", "alice")}
    assert tuple(m.id for m in launch_permission_modes(_CATALOG, "cursor", profiles)) == (
        "normal",
        "auto",
        "yolo",
    )
    for panel, profile, availability, mode, expected in (
        (
            "agent",
            "cursor",
            _availability(cursor="pending"),
            "normal",
            LaunchCommitDecision("ignore"),
        ),
        (
            "agent",
            "cursor",
            _availability(cursor="ok"),
            "normal",
            LaunchCommitDecision("switch-to-mode"),
        ),
        ("agent", "cursor", {}, "normal", LaunchCommitDecision("switch-to-mode")),
        ("mode", "cursor", {}, "auto", LaunchCommitDecision("commit", "auto")),
        ("mode", "cursor", {}, "normal", LaunchCommitDecision("commit", "normal")),
        ("mode", "cursor", {}, "yolo", LaunchCommitDecision("commit", "yolo")),
        ("mode", "cursor", {}, "missing", LaunchCommitDecision("dismiss")),
        ("mode", "cursor", {}, None, LaunchCommitDecision("dismiss")),
        ("mode", "claude", {}, "normal", LaunchCommitDecision("dismiss")),
    ):
        assert (
            launch_commit_decision(
                active_panel=panel,
                current_agent=profile,
                availability=availability,
                selected_mode_id=mode,
                agents=_CATALOG,
                launch_profiles=profiles,
            )
            == expected
        )


def test_profile_labels_use_visible_users_and_distinct_profile_details() -> None:
    from uxon.tui.state import launch_profile_list_label

    profiles = {
        "claude": LaunchProfileOption("claude", "claude", "claude", "alice"),
        "codex": LaunchProfileOption("codex", "codex", "codex", "alice"),
        "work": LaunchProfileOption("work", "Claude work", "claude", "bob", runtime="box"),
    }
    for visible, expected_cross in (
        (("claude", "codex"), False),
        (("claude", "work"), True),
        (("missing",), False),
    ):
        assert launch_profile_users_differ(visible, profiles) is expected_cross
    for index, id_, pending, show_user, expected in (
        (2, "codex", False, True, "2 codex  alice"),
        (2, "codex", False, False, "2 codex"),
        (1, "work", True, True, "1 Claude work  work · claude · bob · runtime:box  (checking…)"),
        (1, "missing", True, True, "1 missing  (checking…)"),
    ):
        availability = SimpleNamespace(status="pending" if pending else "ok")
        assert (
            launch_profile_list_label(
                index, id_, profiles.get(id_), availability, show_launch_user=show_user
            )
            == expected
        )


def test_panel_cycles_only_visible_panels() -> None:
    from uxon.tui.state import next_launch_panel

    for order, current, direction, expected in (
        (("agent", "mode", "workspace"), "agent", 1, "mode"),
        (("agent", "mode", "workspace"), "mode", 1, "workspace"),
        (("agent", "mode", "workspace"), "workspace", 1, "agent"),
        (("agent", "mode", "workspace"), "agent", -1, "workspace"),
        (("agent", "mode", "workspace"), "mode", -1, "agent"),
        (("agent", "mode", "workspace"), "workspace", -1, "mode"),
        (("mode", "workspace"), "mode", 1, "workspace"),
        (("mode", "workspace"), "workspace", 1, "mode"),
        (("mode", "workspace"), "mode", -1, "workspace"),
        (("mode", "workspace"), "workspace", -1, "mode"),
        (("agent", "mode"), "agent", 1, "mode"),
        (("agent", "mode"), "mode", 1, "agent"),
        (("agent", "mode"), "agent", -1, "mode"),
        (("agent", "mode"), "mode", -1, "agent"),
        (("mode", "workspace"), "agent", 1, "mode"),
        ((), "agent", 1, "agent"),
    ):
        assert next_launch_panel(current, direction, order) == expected


def test_branch_validation_accepts_nested_names_and_rejects_whitespace() -> None:
    from uxon.tui.screens.worktree_branch import worktree_branch_valid

    for name, expected in (
        ("feature/auth", True),
        ("bugfix-1", True),
        ("a/b/c", True),
        (" feature/auth ", True),
        ("", False),
        ("   ", False),
        ("-x", False),
        ("a b", False),
        ("a\tb", False),
        ("..", False),
        (".", False),
    ):
        assert worktree_branch_valid(name) is expected, name


def test_picker_bounds_preserve_empty_sentinel() -> None:
    rows = [("", "skip"), ("main", "branch")]
    for index, expected in ((-1, None), (0, ""), (1, "main"), (2, None)):
        assert pick_index(rows, index) == expected, index


def test_browser_selection_preserves_relative_path_and_filtered_rows() -> None:
    rows = (("alpha", "1s"), ("beta", "2s"))
    for directory, needle, index, selected, filtered in (
        ("", "", None, "alpha", list(rows)),
        ("group", "ALP", 0, "group/alpha", [rows[0]]),
        ("group/project", "", 1, "group/project/beta", list(rows)),
        ("group", "missing", 0, None, []),
        ("group", "", -1, None, list(rows)),
        ("group", "", 2, None, list(rows)),
    ):
        level = ProjectBrowserLevel(directory, rows, needle, index)
        assert (level.selected_path, level.filtered) == (selected, filtered), (
            directory,
            needle,
            index,
        )


def test_project_filter_matches_only_names_and_keeps_input_order() -> None:
    rows = [("zeta-Alpha", "1s"), ("beta", "2s"), ("AlphaBeta", "3s"), ("my-secret-tool", "4s")]
    for needle, expected in (
        ("", rows),
        ("   ", rows),
        ("ALP", [rows[0], rows[2]]),
        ("secret", [rows[3]]),
        ("absent", []),
        ("1s", []),
    ):
        assert filter_existing_projects(rows, needle) == expected, needle
    assert rows == [
        ("zeta-Alpha", "1s"),
        ("beta", "2s"),
        ("AlphaBeta", "3s"),
        ("my-secret-tool", "4s"),
    ]


class MainScreenIntentStateTests(unittest.TestCase):
    def test_main_action_intent_for_action_rows(self) -> None:
        self.assertEqual(main_action_intent("action-cwd"), MainIntent("launch-cwd"))
        self.assertEqual(main_action_intent("action-new"), MainIntent("launch-new"))
        self.assertEqual(main_action_intent("action-open"), MainIntent("launch-existing"))
        self.assertEqual(main_action_intent("settings"), MainIntent("open-settings"))
        self.assertEqual(main_action_intent("kill-all-global"), MainIntent("kill-all-global"))
        self.assertIsNone(main_action_intent("unknown"))


class ModalStateTests(unittest.TestCase):
    def test_confirm_phrase_matches_after_strip_only(self) -> None:
        self.assertTrue(confirm_phrase_matches(" kill-all ", "kill-all"))
        self.assertFalse(confirm_phrase_matches("KILL-ALL", "kill-all"))

    def test_project_name_validation_and_errors(self) -> None:
        for name, valid, error in (
            ("", False, "Name cannot be empty"),
            ("   ", False, "Name cannot be empty"),
            (".", False, "Invalid name"),
            ("..", False, "Invalid name"),
            ("a/b", False, "Name cannot contain '/'"),
            ("demo", True, None),
            (" demo ", True, None),
            ("two words", True, None),
        ):
            with self.subTest(name=name):
                self.assertEqual(project_name_valid(name), valid)
                if error is not None:
                    self.assertEqual(project_name_error(name), error)


def test_settings_selection_bounds_and_reset_permissions() -> None:
    entries = [
        SimpleNamespace(spec=SimpleNamespace(key="session_prefix"), editable=True),
        SimpleNamespace(spec=SimpleNamespace(key="immutable"), editable=False),
    ]
    for git, row, selected, key in (
        (True, -1, None, None),
        (True, 0, None, None),
        (True, 1, 0, "session_prefix"),
        (True, 2, 1, None),
        (True, 3, None, None),
        (False, -1, None, None),
        (False, 0, 0, "session_prefix"),
        (False, 1, 1, None),
        (False, 2, None, None),
    ):
        actual = selected_setting_index(row=row, has_git_view=git, entry_count=len(entries))
        assert actual == selected, (git, row)
        assert resettable_setting_key(entries[actual] if actual is not None else None) == key
