# SPDX-License-Identifier: MIT
"""Shared test helpers.

Test fixtures used by more than one ``test_*.py`` module live here so
sibling test files don't import from each other. ``tests/`` is on
``sys.path`` via pytest rootdir collection (no ``__init__.py``),
matching the existing ``harness`` package convention.
"""

from __future__ import annotations

from typing import Literal

from uxon.domain.agents import default_agent_catalog
from uxon.domain.config import Config
from uxon.domain.launch_profiles import LaunchConfig, builtin_launch_profiles
from uxon.domain.session import SessionInfo
from uxon.infra.sessions_probe import UserSessionSnapshot


def make_config(**overrides: object) -> Config:
    agents = default_agent_catalog()
    base: dict[str, object] = {
        "default_launch_user": "",
        "default_launch_mode": "caller",
        "enable_all_users_list": False,
        "launch_user_by_caller": {},
        "session_users": [],
        "allowed_roots": ["/srv/repos"],
        "session_prefix": "uxon-",
        "legacy_session_prefixes": (),
        "new_project_root": "/srv/repos",
        "repeat_noninteractive_mode": "fail",
        "tmux_socket_template": "/tmp/uxon-{user}.sock",
        "launch_record_dir": "",
        "tui_refresh_interval_seconds": 2.0,
        "git_create_enabled": False,
        "git_remote_profiles": [],
        "agents": agents,
        "launch": LaunchConfig(default_profile="claude", profiles=builtin_launch_profiles(agents)),
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def make_launch_profile_options(agents=None, *, user: str = "alice"):
    """The authoritative built-in profile catalog for synthetic TUI contexts."""
    from uxon.tui.context import LaunchProfileOption

    catalog = agents or default_agent_catalog()
    return {
        profile.id: LaunchProfileOption(profile.id, profile.id, profile.agent, user)
        for profile in builtin_launch_profiles(catalog).values()
    }


def make_session_snapshot(
    sessions: list[SessionInfo] | tuple[SessionInfo, ...] = (),
    *,
    user: str = "dana_agent",
    server_state: Literal["absent", "running"] | None = None,
) -> UserSessionSnapshot:
    rows = tuple(sessions)
    state = server_state or ("running" if rows else "absent")
    return UserSessionSnapshot(user=user, server_state=state, sessions=rows)


def run_off_loop_sync(fn, *, on_success=None, on_error=None, label="action"):
    """Synchronous stand-in for ``UxonApp.run_off_loop`` in non-async tests.

    Production runs ``fn`` (a blocking tmux/ssh/sudo callback) on a worker
    thread and invokes the continuation back on the loop. Tests that drive
    a controller through a fake App have no event loop, so this runs ``fn``
    inline and calls the matching continuation — same end-to-end behaviour,
    same error routing (any exception → ``on_error``).
    """
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 — mirror run_off_loop's capture
        if on_error is not None:
            on_error(exc)
        return
    if on_success is not None:
        on_success(value)


def make_session(name: str = "uxon-demo@claude", *, user: str = "alice") -> SessionInfo:
    return SessionInfo(
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


def make_dashboard_context(**overrides):
    from uxon.tui.context import LaunchRequest, TuiContext

    base = dict(
        sessions=[],
        total_cpu="0",
        total_ram="0",
        version="4.0.0",
        cwd="/srv/work",
        cwd_short="work",
        new_project_root="/srv/work",
        existing_projects=[],
        cwd_writable=True,
        current_user="alice",
        refresh_sources=[],
        tui_render_debounce_ms=0,
        on_launch_cwd=lambda p, m, target_dir=None: LaunchRequest(("true",), label="cwd"),
        on_launch_new=lambda n, p, m, g: LaunchRequest(("true",), label="new"),
        on_launch_existing=lambda n, p, m: LaunchRequest(("true",), label="existing"),
    )
    base.update(overrides)
    return TuiContext(**base)


def make_dashboard_session(name="uxon-local@claude", *, user="alice", cpu="1.0"):
    from uxon.tui.context import TuiSession

    return TuiSession(name, name, False, "42", cpu, "1M", "1s", "1s", "claude", "/srv/work", user)


def make_dashboard_snapshot(*, user="bob", name="uxon-remote@claude", error=None):
    from uxon.domain.wire_schema import RemoteSnapshot

    return RemoteSnapshot(
        "peer",
        1.0,
        error,
        sessions=[
            {
                "user": user,
                "name": name,
                "short_id": name,
                "agent": "claude",
                "attached": False,
                "active_pid": 43,
                "cpu_pct": 3.0,
                "rss_kib": 1024,
                "active_cmd": "claude",
                "active_path": "/srv/remote",
            }
        ],
    )


def land_dashboard_context(app, ctx):
    from uxon.tui.messages import _RefreshSourceLanded

    app.on__refresh_source_landed(
        _RefreshSourceLanded("main_ctx_rebuild", ctx, instance_epoch=app._instance_epoch)
    )


def visible_dashboard_text(widget):
    return " ".join(widget.render_line(i).text for i in range(widget.size.height))
