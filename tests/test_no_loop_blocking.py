"""Blocking dashboard and launch callbacks keep input responsive off the loop."""

import asyncio
import getpass
import subprocess
import threading
import unittest

from helpers import land_dashboard_context as _land
from helpers import make_config, make_launch_profile_options
from helpers import make_dashboard_context as _mk_ctx
from helpers import make_dashboard_session as _session
from helpers import make_dashboard_snapshot as _snapshot
from textual.widgets import Input

from uxon.domain.agents import DEFAULT_AGENT_CATALOG
from uxon.domain.execution import ExecutionBackendSpec, ExecutionConfig
from uxon.domain.session import BulkKillResult, KillResult, SessionIdentity
from uxon.infra import execution
from uxon.infra.agents import AgentAvailability
from uxon.infra.loop_guard import (
    EventLoopBlockedError,
    assert_off_event_loop,
    install_subprocess_guard,
)
from uxon.infra.remote_hosts import RemoteHost
from uxon.infra.run import run_query
from uxon.tui.app import UxonApp
from uxon.tui.context import LaunchRequest
from uxon.tui.messages import _RefreshSourceLanded
from uxon.tui.screens.launch_options import LaunchOptionsScreen
from uxon.tui.widgets.session_list_view import SessionListView


def _on_loop():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class GuardUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_loop_refuses_before_spawn_worker_command_succeeds(self):
        install_subprocess_guard()
        with self.assertRaises(EventLoopBlockedError):
            assert_off_event_loop("blocking call")
        with self.assertRaises(EventLoopBlockedError):
            subprocess.run(["true"], capture_output=True)
        backend = ExecutionBackendSpec(
            "boundary", "command", ("/usr/local/libexec/test-boundary", "{user}", "--")
        )
        cfg = make_config(
            execution=ExecutionConfig(
                default_backend="boundary",
                backends={"local": ExecutionConfig().backends["local"], "boundary": backend},
            )
        )
        with self.assertRaises(EventLoopBlockedError):
            execution.probe(cfg, getpass.getuser())

        def worker():
            assert_off_event_loop("worker command")
            return run_query(["true"], timeout=5).returncode

        self.assertEqual(await asyncio.wait_for(asyncio.to_thread(worker), 10), 0)


class InteractiveActionsRunOffLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_targets_and_continuations_with_input_during_blocked_kill(self):
        started, release, completed = threading.Event(), threading.Event(), threading.Event()
        calls, launches = [], []
        loop_thread = threading.get_ident()

        def kill(expected):
            user, name = expected.user, expected.name
            calls.append(("kill", user, name, threading.get_ident(), _on_loop()))
            started.set()
            try:
                if not release.wait(10):
                    raise TimeoutError("kill release missing")
            finally:
                completed.set()
            return KillResult(expected, "killed", "ok")

        def attach(*target):
            if isinstance(target[-1], SessionIdentity):
                target = target[:-1]
            calls.append(("attach", *target, threading.get_ident(), _on_loop()))
            return LaunchRequest(("true",), label="/".join(target))

        def kill_all(targets):
            calls.append(("all", threading.get_ident(), _on_loop()))
            return BulkKillResult(tuple(KillResult(target, "killed", "ok") for target in targets))

        ctx = _mk_ctx(
            sessions=[_session("uxon-a@claude"), _session("uxon-b@claude")],
            remote_hosts=[RemoteHost("peer", "peer", "", "uxon")],
            on_kill=kill,
            on_attach=attach,
            on_remote_attach=attach,
            on_kill_all=kill_all,
        )
        app = UxonApp(ctx, probe_agents=False)
        app.request_launch = lambda request: launches.append(
            (request, threading.get_ident(), _on_loop())
        )
        async with app.run_test(size=(120, 30)) as pilot:
            _land(app, ctx)
            app.on__refresh_source_landed(_RefreshSourceLanded("remote:peer", _snapshot()))
            await pilot.pause()
            table = app.screen.query_one("#sessions-dashboard", SessionListView)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("d", "y")
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 10))
                self.assertFalse(completed.is_set())
                selected = table.selected_row_key
                await pilot.press("down")
                self.assertNotEqual(table.selected_row_key, selected)
                self.assertIn("uxon-b", table.render_line(2).text)
                self.assertFalse(completed.is_set(), "input must be handled before release")
                self.assertEqual(calls[0][:-2], ("kill", "alice", "uxon-a@claude"))
                self.assertFalse(calls[0][-1])
            finally:
                release.set()
            self.assertTrue(await asyncio.to_thread(completed.wait, 10))
            await app.workers.wait_for_complete()
            await pilot.pause()
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(calls[-1][:-2], ("attach", "alice", "uxon-a@claude"))
            table.move_cursor(
                row=next(i for i, r in enumerate(app.screen._dashboard_rows) if r.host)
            )
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(calls[-1][:-2], ("attach", "peer", "bob", "uxon-remote@claude"))
            await pilot.press("D")
            app.screen.query_one(Input).value = "kill-all"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(calls[-1][:-2], ("all",))
            self.assertTrue(
                all(thread != loop_thread and not on_loop for *_, thread, on_loop in calls)
            )
            self.assertEqual(
                [r.label for r, _, _ in launches],
                ["alice/uxon-a@claude", "peer/bob/uxon-remote@claude"],
            )
            self.assertTrue(
                all(thread == loop_thread and on_loop for _, thread, on_loop in launches)
            )

    async def test_launch_workspace_session_runtime_and_commit_boundaries(self):
        calls, launches = [], []
        loop_thread = threading.get_ident()
        started, release, completed = threading.Event(), threading.Event(), threading.Event()

        def record(label, *args, result=None):
            calls.append((label, args, threading.get_ident(), _on_loop()))
            return result

        def workspaces(*args):
            record("workspaces", *args)
            started.set()
            try:
                if not release.wait(10):
                    raise TimeoutError("workspace probe release missing")
                return []
            finally:
                completed.set()

        def commit(profile, mode, target_dir=None):
            record("commit", profile, mode, target_dir)
            self.assertEqual(run_query(["true"], timeout=5).returncode, 0)
            return LaunchRequest(("true",), label="launch")

        ctx = _mk_ctx(
            agents=dict(DEFAULT_AGENT_CATALOG),
            enabled_profiles=("claude",),
            default_profile="claude",
            launch_profiles=make_launch_profile_options(),
            agent_availability={"claude": AgentAvailability("ok")},
            on_probe_worktrees=workspaces,
            on_probe_existing_sessions=lambda *args: record("sessions", *args, result=()),
            on_runtime_gate=lambda *args: record("runtime", *args),
            on_launch_cwd=commit,
        )
        app = UxonApp(ctx, probe_agents=False)
        app.request_launch = lambda request: launches.append(
            (request, threading.get_ident(), _on_loop())
        )
        async with app.run_test(size=(120, 30)) as pilot:
            app.screen.query_one("#action-cwd").focus()
            await pilot.press("enter")
            for _ in range(20):
                if isinstance(app.screen, LaunchOptionsScreen):
                    break
                await pilot.pause()
            self.assertIsInstance(app.screen, LaunchOptionsScreen)
            await pilot.press("enter")
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 10))
                self.assertFalse(completed.is_set())
                await pilot.press("s", "1")
                search = app.screen.query_one("#search-bar Input", Input)
                self.assertEqual(search.value, "1")
                self.assertIs(app.focused, search)
                self.assertIn("1", search.render_line(0).text)
                self.assertTrue(app.screen.query_one("#search-bar").has_class("-shown"))
                self.assertFalse(completed.is_set(), "search input must be handled before release")
                self.assertEqual([label for label, _, _, _ in calls], ["workspaces"])
                self.assertEqual(launches, [])
            finally:
                release.set()
            self.assertTrue(await asyncio.to_thread(completed.wait, 10))
            await app.workers.wait_for_complete()
            await pilot.pause()
            for _ in range(20):
                if launches:
                    break
                await pilot.pause()
            self.assertEqual(
                [label for label, _, _, _ in calls], ["workspaces", "sessions", "runtime", "commit"]
            )
            self.assertEqual(calls[0][1], ("/srv/work", "claude", "normal"))
            self.assertTrue(
                all(thread != loop_thread and not on_loop for _, _, thread, on_loop in calls)
            )
            self.assertEqual(len(launches), 1)
            self.assertEqual(launches[0][0].label, "launch")
            self.assertEqual(launches[0][1:], (loop_thread, True))
