# SPDX-License-Identifier: MIT
"""Shared test helpers.

Test fixtures used by more than one ``test_*.py`` module live here so
sibling test files don't import from each other. ``tests/`` is on
``sys.path`` via pytest rootdir collection (no ``__init__.py``),
matching the existing ``harness`` package convention.
"""

from __future__ import annotations

import uxon.cli as uxon


def make_config(**overrides: object) -> uxon.Config:
    base: dict[str, object] = {
        "runtime_user": "",
        "default_launch_mode": "caller",
        "enable_all_users_list": False,
        "launch_user_by_caller": {},
        "session_users": [],
        "allowed_roots": ["/srv/repos"],
        "session_prefix": "uxon-",
        "legacy_session_prefixes": (),
        "enabled_agents": ("claude",),
        "default_agent": "claude",
        "agent_default_args": {"claude": (), "codex": (), "cursor": ()},
        "new_project_root": "/srv/repos",
        "repeat_noninteractive_mode": "fail",
        "tmux_socket_template": "/tmp/uxon-{user}.sock",
        "tui_refresh_interval_seconds": 2.0,
        "git_create_enabled": False,
        "default_git_remote_profile": "",
        "git_remote_profiles": [],
    }
    base.update(overrides)
    return uxon.Config(**base)  # type: ignore[arg-type]


def make_session(name: str = "uxon-demo@claude", *, user: str = "u-vz") -> uxon.SessionInfo:
    return uxon.SessionInfo(
        user=user,
        name=name,
        attached="0",
        windows="1",
        created="2026-05-03T12:00:00+00:00",
        last_attached="2026-05-03T12:30:00+00:00",
        pane_pids=(111,),
        active_pid=111,
        active_cmd="claude",
        active_path="/srv/repos/demo",
    )
