"""Advisory workspace reads must not delay or redirect a launch gesture."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest
from harness.textual_ready import wait_until
from helpers import make_dashboard_context, make_launch_profile_options

from uxon.domain.agents import DEFAULT_AGENT_CATALOG
from uxon.domain.execution import ExecutionBackendSpec, ExecutionConfig
from uxon.infra.agents import AgentAvailability
from uxon.infra.worktrees import GitDiscovery, Workspace
from uxon.tui.app import UxonApp
from uxon.tui.context import LaunchProfileOption, LaunchRequest
from uxon.tui.messages import _RefreshSourceLanded
from uxon.tui.screens.launch_options import LaunchOptionsScreen
from uxon.tui.screens.workspace import WorkspaceScreen
from uxon.tui.worktree_discovery import WorktreeDiscovery


def context(**overrides):
    values = dict(
        enabled_profiles=("claude",),
        default_profile="claude",
        launch_profiles=make_launch_profile_options(),
        agent_availability={"claude": AgentAvailability("ok")},
        agents=DEFAULT_AGENT_CATALOG,
    )
    values.update(overrides)
    return make_dashboard_context(**values)


def test_prefetch_deduplicates_profiles_bounds_obsolete_queue_and_keys_backend():
    condition = threading.Condition()
    release = threading.Event()
    calls = []

    def changed():
        with condition:
            condition.notify_all()

    def discover(path, user):
        with condition:
            calls.append((path, user))
            condition.notify_all()
        if path == "/initial":
            assert release.wait(5), "test failed to release initial reads"
        return None

    profiles = {
        f"profile{i}": LaunchProfileOption(
            f"profile{i}", str(i), "claude", "alice" if i % 2 else "bob"
        )
        for i in range(10)
    }
    ctx = context(
        enabled_profiles=tuple(profiles), launch_profiles=profiles, on_discover_worktrees=discover
    )
    cache = WorktreeDiscovery(changed)
    try:
        cache.prefetch(ctx, "/initial")
        with condition:
            assert condition.wait_for(lambda: len(calls) == 2, 5)
        cache.prefetch(ctx, "/initial")
        assert len(calls) == 2
        for i in range(60):
            cache.prefetch(ctx, f"/selection-{i}")
        assert len(calls) == 2
        assert cache.peek(ctx, "/selection-59", "alice").status == "pending"
        release.set()
        with condition:
            assert condition.wait_for(
                lambda: (
                    cache.peek(ctx, "/selection-59", "alice").status == "nongit"
                    and cache.peek(ctx, "/selection-59", "bob").status == "nongit"
                ),
                5,
            )
        assert set(calls) == {
            (path, user) for path in ("/initial", "/selection-59") for user in ("alice", "bob")
        }
        cache.prefetch(ctx, "/selection-59")
        assert len(calls) == 4
        changed_backend = ExecutionConfig(
            default_backend="other",
            backends={"other": ExecutionBackendSpec("other", "command", ("synthetic", "{user}"))},
        )
        fresh = replace(ctx, execution=changed_backend)
        cache.prefetch(fresh, "/selection-59")
        with condition:
            assert condition.wait_for(
                lambda: (
                    cache.peek(fresh, "/selection-59", "alice").status == "nongit"
                    and cache.peek(fresh, "/selection-59", "bob").status == "nongit"
                ),
                5,
            )
        assert len(calls) == 6
    finally:
        release.set()
        cache.close()


@pytest.mark.parametrize(
    "outcomes,expected",
    [
        (("nongit", "nongit"), ""),
        (("ready", "ready"), "alice, bob: Git · main"),
        (("ready", "nongit"), "alice: Git · main"),
        (("error", "nongit"), "alice: Git unavailable: corrupt HEAD"),
        (("ready", "error"), "alice: Git · main\nbob: Git unavailable: corrupt HEAD"),
    ],
)
def test_git_status_hides_pending_and_nongit_and_groups_matching_details(outcomes, expected):
    condition = threading.Condition()
    release = threading.Event()

    def changed():
        with condition:
            condition.notify_all()

    def discover(path, user):
        assert release.wait(5), "test failed to release discovery"
        outcome = outcomes[("alice", "bob").index(user)]
        if outcome == "error":
            raise RuntimeError("corrupt HEAD")
        if outcome == "nongit":
            return None
        return GitDiscovery(
            path, path, path, path, path + "/.git", (Workspace("main", "main", path, True),)
        )

    profiles = {user: LaunchProfileOption(user, user, "claude", user) for user in ("alice", "bob")}
    ctx = context(
        enabled_profiles=tuple(profiles), launch_profiles=profiles, on_discover_worktrees=discover
    )
    cache = WorktreeDiscovery(changed)
    try:
        cache.prefetch(ctx, "/srv/work/project")
        assert cache.status(ctx, "/srv/work/project") == ""
        release.set()
        with condition:
            assert condition.wait_for(
                lambda: all(
                    cache.peek(ctx, "/srv/work/project", user).status != "pending"
                    for user in profiles
                ),
                5,
            )
        assert cache.status(ctx, "/srv/work/project") == expected
        if outcomes[0] == "ready":
            assert cache.status(ctx, "/srv/work/project", "alice") == "Git · main"
    finally:
        release.set()
        cache.close()


@pytest.mark.parametrize(
    "choice,expected",
    [(0, "/srv/work/linked/nested"), (1, "/srv/work/main"), (3, "/srv/work/other")],
)
def test_selected_workspace_is_identical_for_session_runtime_and_launch(choice, expected):
    async def exercise():
        target = "/srv/work/linked/nested"
        rows = (
            Workspace("main", "main", "/srv/work/main", True),
            Workspace("feature", "feature", "/srv/work/linked", False),
            Workspace("other", "other", "/srv/work/other", False),
        )
        discovery = GitDiscovery(
            target,
            "/srv/work/linked",
            "/srv/work/main",
            "/srv/work/main",
            "/srv/work/main/.git",
            rows,
        )
        calls, launched = [], []

        def record(kind, path):
            calls.append((kind, path))
            return (
                LaunchRequest(("true",), label=path)
                if kind == "launch"
                else ()
                if kind == "sessions"
                else None
            )

        ctx = context(
            existing_projects=[("linked/nested", "")],
            on_discover_worktrees=lambda path, user: discovery,
            on_probe_existing_sessions=lambda path, *args: record("sessions", path),
            on_probe_existing_worktree_sessions=lambda path, *args: record("sessions", path),
            on_runtime_gate=lambda path, *args: record("runtime", path),
            on_launch_existing=lambda name, profile, mode, path=None: record(
                "launch", path or f"/srv/work/{name}"
            ),
            on_launch_existing_worktree=lambda repo, branch, path, *args: record("launch", path),
        )
        app = UxonApp(ctx, probe_agents=False)
        app.request_launch = launched.append
        async with app.run_test(size=(120, 40)) as pilot:
            app.screen._launch_existing()
            await wait_until(
                pilot,
                lambda: app.worktree_discovery.peek(ctx, target, "alice").status == "ready",
                "project discovery",
            )
            await pilot.press("enter")
            await wait_until(
                pilot, lambda: isinstance(app.screen, LaunchOptionsScreen), "profile choice"
            )
            await pilot.press("enter")
            await wait_until(
                pilot, lambda: isinstance(app.screen, WorkspaceScreen), "ready workspace choices"
            )
            await pilot.press(*(["down"] * choice), "enter")
            await wait_until(pilot, lambda: bool(launched), "chosen workspace launch")
            assert calls == [(kind, expected) for kind in ("sessions", "runtime", "launch")]

    asyncio.run(exercise())


def test_context_refresh_during_project_picker_cannot_mix_launch_accounts():
    async def exercise():
        calls, launched = [], []

        def snapshot(label, user):
            def record(kind, *args):
                calls.append((label, kind, args))
                return (
                    LaunchRequest(("true",), label=label)
                    if kind == "launch"
                    else ()
                    if kind == "sessions"
                    else None
                )

            return context(
                existing_projects=[("project", "")],
                launch_profiles={"claude": LaunchProfileOption("claude", "Claude", "claude", user)},
                on_discover_worktrees=lambda path, target_user: record("git", path, target_user),
                on_probe_existing_sessions=lambda *args: record("sessions", *args),
                on_runtime_gate=lambda *args: record("runtime", *args),
                on_launch_existing=lambda *args: record("launch", *args),
            )

        original, fresh = snapshot("original", "alice"), snapshot("fresh", "bob")
        app = UxonApp(original, probe_agents=False)
        app.request_launch = launched.append
        async with app.run_test(size=(120, 40)) as pilot:
            app.screen._launch_existing()
            await wait_until(
                pilot, lambda: any(kind == "git" for _, kind, _ in calls), "original discovery"
            )
            app.post_message(_RefreshSourceLanded("main_ctx_rebuild", fresh))
            await wait_until(pilot, lambda: app.ctx is fresh, "background context refresh")
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: isinstance(app.screen, LaunchOptionsScreen),
                "original profile picker",
            )
            await pilot.press("enter")
            await wait_until(pilot, lambda: bool(launched), "snapshot-consistent launch")
            assert {label for label, _, _ in calls} == {"original"}
            assert [kind for _, kind, _ in calls] == ["git", "sessions", "runtime", "launch"]

    asyncio.run(exercise())
