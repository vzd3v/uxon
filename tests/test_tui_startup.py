"""Startup, terminal handoff and independent local-data contracts."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from helpers import make_config, make_dashboard_context
from textual.widgets import Label, ListItem, ListView, Static

from uxon.domain.execution import ExecutionBackendSpec, ExecutionConfig
from uxon.domain.launch_request import HandoffResult, LaunchRequest
from uxon.domain.status import ServerStatus
from uxon.tui.app import UxonApp
from uxon.tui.background import BackgroundSources
from uxon.tui.context_builder import build_tui_context
from uxon.tui.main_data import HostData, ProjectsData
from uxon.tui.messages import _RefreshSourceLanded
from uxon.tui.refresh import SourceSpec
from uxon.tui.screens.existing import ExistingProjectScreen
from uxon.tui.widgets.filter_input import FilterInput


def test_prefetch_and_cancelled_subscriber_reuse_work_across_handoff():
    async def exercise():
        started, release = threading.Event(), threading.Event()
        calls = []

        def sessions():
            calls.append("sessions")
            started.set()
            assert release.wait(5), "test did not release its collector"
            return "sessions ready"

        background = BackgroundSources()
        slow = SourceSpec("main_ctx_rebuild", sessions, prefetch=True)
        projects = SourceSpec("local_projects", lambda: "projects ready", prefetch=True)
        discovery = SourceSpec("agent_discovery", lambda: calls.append("discovery"))
        try:
            background.prefetch([slow, projects])
            pending = background.start(slow, reuse=True)
            assert await asyncio.to_thread(started.wait, 2)
            ready = await asyncio.wait_for(
                asyncio.wrap_future(background.start(projects, reuse=True)), 2
            )
            assert ready.value == "projects ready" and not pending.done()

            async def subscriber():
                return await asyncio.shield(asyncio.wrap_future(pending))

            previous_app = asyncio.create_task(subscriber())
            await asyncio.sleep(0)
            previous_app.cancel()
            with pytest.raises(asyncio.CancelledError):
                await previous_app
            background.prefetch([slow])  # Re-entering the dashboard must not double the read.
            assert background.start(slow, reuse=True) is pending
            assert not pending.cancelled()
            release.set()
            assert (await asyncio.wait_for(subscriber(), 2)).value == "sessions ready"
            first = background.start(discovery, reuse=True)
            await asyncio.wrap_future(first)
            assert background.start(discovery, reuse=True) is first
            assert calls == ["sessions", "discovery"]
            changed = replace(projects, key="different-backend", fetch=lambda: "new context")
            assert (
                await asyncio.wrap_future(background.start(changed, reuse=True))
            ).value == "new context"
        finally:
            release.set()
            background.close()

    asyncio.run(exercise())


def test_root_switch_starts_new_read_before_old_read_finishes():
    async def exercise():
        old_started, new_started, release = threading.Event(), threading.Event(), threading.Event()

        def old_read():
            old_started.set()
            assert release.wait(5), "test did not release old project read"
            return ProjectsData("/old", (("oldrepo", "1d"),))

        def new_read():
            new_started.set()
            return ProjectsData("/new", (("newrepo", "1d"),))

        old = SourceSpec("local_projects", old_read, key="old", prefetch=True)
        new = SourceSpec("local_projects", new_read, key="new", prefetch=True)
        ctx = make_dashboard_context(
            new_project_root="/old", projects_loading=True, refresh_sources=[old]
        )
        background = BackgroundSources()
        try:
            background.prefetch([old])
            previous = background.start(old, reuse=True)
            app = UxonApp(ctx, background=background, probe_agents=False)
            async with app.run_test(size=(100, 30)) as pilot:
                assert await asyncio.to_thread(old_started.wait, 2)
                app.on__refresh_source_landed(
                    _RefreshSourceLanded(
                        "main_ctx_rebuild",
                        replace(ctx, new_project_root="/new", refresh_sources=[new]),
                    )
                )
                assert await asyncio.to_thread(new_started.wait, 2)
                await pilot.pause()
                assert app.ctx.existing_projects == [("newrepo", "1d")]
                assert not release.is_set()
                release.set()
                await asyncio.wait_for(asyncio.wrap_future(previous), 2)
                await pilot.pause()
                assert app.ctx.existing_projects == [("newrepo", "1d")]
        finally:
            release.set()
            background.close()

    asyncio.run(exercise())


def test_early_open_keeps_filter_selection_and_unchanged_rows():
    async def exercise():
        app = UxonApp(
            make_dashboard_context(loading=True, projects_loading=True), probe_agents=False
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.click("#action-open")
            browser = app.screen
            assert isinstance(browser, ExistingProjectScreen)
            assert str(browser.query_one("#browser-status", Static).content) == "Loading…"
            await pilot.press("r", "e", "p")
            root = app.ctx.new_project_root
            rows = (("repo_b", "1d"), ("repo_c", "1d"), ("other", "1d"))

            def land(projects):
                app.on__refresh_source_landed(
                    _RefreshSourceLanded("local_projects", ProjectsData(root, projects))
                )

            land(rows)
            await pilot.pause()
            assert browser.query_one(FilterInput).value == "rep"
            assert len(browser.query(ListItem)) == 2
            assert app.ctx.loading, "projects must not claim that sessions have loaded"
            await pilot.press("down")
            selected = browser.query_one(ListView).highlighted_child
            assert "repo_c" in str(selected.query_one(Label).content)
            before = tuple(browser.query(ListItem))
            land(rows)
            await pilot.pause()
            assert tuple(browser.query(ListItem)) == before
            land((("repo_a", "1d"), *rows))
            await pilot.pause()
            selected = browser.query_one(ListView).highlighted_child
            assert "repo_c" in str(selected.query_one(Label).content)
            assert browser.query_one(FilterInput).value == "rep"
            await pilot.press("escape", "escape")
            await pilot.pause()
            assert not isinstance(app.screen, ExistingProjectScreen)
            assert "loading" not in str(app.screen.query_one("#action-open", Static).content)

    asyncio.run(exercise())


def test_root_update_during_navigation_survives_back_without_clearing_pending_read():
    async def exercise():
        started, release = threading.Event(), threading.Event()

        def subfolders(path):
            assert path == "repo"
            started.set()
            assert release.wait(5), "test did not release directory read"
            return [("child", "1d")]

        app = UxonApp(make_dashboard_context(), probe_agents=False)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                browser = ExistingProjectScreen(
                    [("repo", "1d")],
                    "/projects",
                    list_directories=subfolders,
                    error="root read failed",
                )
                app.push_screen(browser)
                await pilot.pause()
                await pilot.press("right")
                assert await asyncio.to_thread(started.wait, 2)
                await browser.update_root([("new", "1d"), ("repo", "1d")])
                assert str(browser.query_one("#browser-status", Static).content) == "Loading…"
                release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert "/projects/repo/" in str(browser.query_one("#project-path", Static).content)
                assert "failed" not in str(browser.query_one("#browser-status", Static).content)
                await pilot.press("left")
                await pilot.pause()
                assert len(browser.query(ListItem)) == 2
                selected = browser.query_one(ListView).highlighted_child
                assert "repo" in str(selected.query_one(Label).content)
        finally:
            release.set()

    asyncio.run(exercise())


@pytest.mark.parametrize("changed", ["root", "backend"])
@pytest.mark.parametrize(
    "name,error",
    [
        ("local_projects", ""),
        ("local_projects", "old permission error"),
        ("local_stats", ""),
        ("local_stats", "old timeout"),
    ],
)
def test_stale_local_success_and_failure_cannot_replace_new_source_context(
    tmp_path, changed, name, error
):
    cfg = make_config(new_project_root=str(tmp_path))
    old = build_tui_context(cfg, "alice", "alice", str(tmp_path), skeleton=True)
    if changed == "root":
        cfg = replace(cfg, new_project_root=str(tmp_path / "other"))
    else:
        backend = ExecutionBackendSpec("other", "command", ("/usr/bin/sudo", "-u", "{user}"))
        cfg = replace(
            cfg,
            execution=ExecutionConfig(
                backend_by_launch_user={"alice": "other"},
                backends={**cfg.execution.backends, "other": backend},
            ),
        )
    current = build_tui_context(cfg, "alice", "alice", str(tmp_path), skeleton=True)
    old_key = next(spec.key for spec in old.refresh_sources if spec.name == name)
    new_key = next(spec.key for spec in current.refresh_sources if spec.name == name)
    assert old_key != new_key
    app = UxonApp(current, probe_agents=False)
    data = (
        ProjectsData(old.new_project_root, (("stale", "1d"),))
        if name == "local_projects"
        else HostData(ServerStatus(load="99"), None)
    )
    with patch.object(app, "notify") as notify:
        app.on__refresh_source_landed(
            _RefreshSourceLanded(name, None if error else data, error=error, source_key=old_key)
        )
    assert app.ctx is current and app.state.main is None
    notify.assert_not_called()


def test_project_and_stats_updates_preserve_focus_and_do_not_rebuild_session_model():
    async def exercise():
        app = UxonApp(make_dashboard_context(), probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            focused = app.screen.query_one("#action-new")
            focused.focus()
            await pilot.pause()
            with patch("uxon.tui.screens.dashboard_render.select_dashboard_model") as model:
                app.on__refresh_source_landed(
                    _RefreshSourceLanded(
                        "local_projects", ProjectsData(app.ctx.new_project_root, (("repo", "1d"),))
                    )
                )
                app.on__refresh_source_landed(
                    _RefreshSourceLanded(
                        "local_stats", HostData(ServerStatus(load="0.25", ram="1G/8G"), None)
                    )
                )
                await pilot.pause()
                model.assert_not_called()
            assert app.focused is focused
            assert "0.25" in str(app.screen.query_one("#server-status", Static).content)

    asyncio.run(exercise())


def test_runner_reopens_from_published_data_without_synchronous_refresh(monkeypatch):
    from uxon.tui import app as app_module
    from uxon.tui import runner

    refresh = Mock(side_effect=AssertionError("dashboard waited for a full rebuild"))
    ctx = make_dashboard_context(on_refresh=refresh)
    instances = []

    class Dashboard:
        def __init__(self, context, pending_status, background):
            self.ctx = context
            self.quit_rc = 0 if instances else None
            self.pending_launch = LaunchRequest(("true",), label="attach:test")
            self.state = SimpleNamespace(agent_availability=SimpleNamespace(value={}))
            self.call_later = self.exit = lambda *args: None
            instances.append(self)

        def run(self):
            if len(instances) == 1:
                self.ctx = replace(self.ctx, existing_projects=[("loaded", "1d")])

    monkeypatch.setattr(app_module, "UxonApp", Dashboard)
    monkeypatch.setattr(runner, "_run_app_with_terminal_lifetime", lambda run, *args: run())
    monkeypatch.setattr(runner, "run_launch_request", lambda req: HandoffResult(0, "done", 0.0))
    monkeypatch.setattr(runner, "pause_on_launch_failure", lambda *args: None)
    monkeypatch.setattr(runner.sys, "stdin", SimpleNamespace(fileno=lambda: 0))
    background = BackgroundSources()
    try:
        assert runner._run(ctx, background) == 0
    finally:
        background.close()
    assert len(instances) == 2
    assert instances[1].ctx.existing_projects == [("loaded", "1d")]
    refresh.assert_not_called()
