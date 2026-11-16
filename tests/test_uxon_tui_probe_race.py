"""Host worker results become canonical only after on-loop message delivery."""

import asyncio
import threading
import unittest
from unittest.mock import patch

from helpers import make_dashboard_context as _mk_ctx

from uxon.domain.agents import DEFAULT_AGENT_CATALOG
from uxon.domain.host_report import BinaryStatus, HostReport
from uxon.infra.agents import AgentAvailability
from uxon.tui.app import UxonApp
from uxon.tui.context import LaunchProfileOption
from uxon.tui.messages import _HostReportUpdated
from uxon.tui.screens.agents_unavailable import AgentsUnavailableScreen
from uxon.tui.state import compute_all_missing, visible_agent_ids


class ProbeWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_auto_mode_keeps_missing_and_broken_results_for_unavailable_diagnostics(self):
        ctx = _mk_ctx(
            enabled_profiles=("claude", "codex"),
            launch_auto_mode=True,
            launch_profiles={
                aid: LaunchProfileOption(aid, aid, aid, "alice") for aid in ("claude", "codex")
            },
        )
        app = UxonApp(ctx, probe_agents=False)
        posted = []
        app.post_message = lambda message: posted.append(message) or True
        report = HostReport(
            BinaryStatus("tmux", "/usr/bin/tmux", ""),
            {
                "claude": BinaryStatus("claude", None, "install claude"),
                "codex": BinaryStatus(
                    "codex",
                    "/bin/codex",
                    "install codex",
                    AgentAvailability("error", error="missing target", returncode=127),
                ),
            },
            "alice",
        )
        with patch("uxon.infra.probes.probe_host", return_value=report):
            app._worker_coord._probe_host_worker()
        availability = posted[0].availability
        self.assertTrue(
            compute_all_missing(enabled_profiles=ctx.enabled_profiles, availability=availability)
        )
        self.assertEqual(
            visible_agent_ids(
                enabled_profiles=(), availability=availability, catalog_ids=ctx.enabled_profiles
            ),
            (),
        )
        screen = AgentsUnavailableScreen(
            enabled_profiles=ctx.enabled_profiles, availability=availability
        )
        self.assertIn("missing target", screen.body_text)

    async def test_completed_worker_cannot_mutate_state_before_buffered_delivery(self):
        started, release = threading.Event(), threading.Event()
        worker_threads = []

        def probe(_cfg, user, catalog, *, check_agents):
            self.assertFalse(check_agents)
            worker_threads.append(threading.get_ident())
            started.set()
            if not release.wait(10):
                raise TimeoutError("host probe release missing")
            return HostReport(
                BinaryStatus("tmux", "/usr/bin/tmux", ""),
                {aid: BinaryStatus(aid, f"/usr/bin/{aid}", "") for aid in catalog},
                user,
            )

        ctx = _mk_ctx(
            agents=dict(DEFAULT_AGENT_CATALOG),
            enabled_profiles=("ops",),
            default_profile="ops",
            launch_profiles={"ops": LaunchProfileOption("ops", "Ops", "claude", "alice")},
        )
        app = UxonApp(ctx, probe_agents=False)
        buffered = []
        original_post = app.post_message

        def buffer(message):
            if isinstance(message, _HostReportUpdated):
                buffered.append(message)
                return True
            return original_post(message)

        with patch("uxon.infra.probes.probe_host", probe):
            async with app.run_test(size=(100, 30)) as pilot:
                loop_thread = threading.get_ident()
                slot = app.state.agent_availability
                original = dict(slot.value)
                context_seed = dict(ctx.agent_availability)
                app.post_message = buffer
                app.probe_agents = True
                await pilot.press("r")
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 10))
                    self.assertIs(app.state.agent_availability, slot)
                    release.set()
                    await asyncio.wait_for(app.workers.wait_for_complete(), 10)
                    self.assertEqual(len(buffered), 1)
                    self.assertEqual(ctx.agent_availability, context_seed)
                    self.assertIs(app.state.agent_availability, slot)
                    self.assertEqual(slot.value, original)
                    self.assertEqual(len(worker_threads), 1)
                    self.assertNotEqual(worker_threads[0], loop_thread)
                    self.assertEqual(
                        buffered[0].availability,
                        {"ops": AgentAvailability("ok", path="/usr/bin/claude")},
                    )
                    app.post_message = original_post
                    self.assertTrue(app.post_message(buffered[0]))
                    await pilot.pause()
                    self.assertIsNot(app.state.agent_availability, slot)
                    self.assertEqual(
                        app.state.agent_availability.value,
                        {"ops": AgentAvailability("ok", path="/usr/bin/claude")},
                    )
                    self.assertIsNotNone(app.state.agent_availability.last_attempt_at)
                    self.assertEqual(ctx.agent_availability, context_seed)
                finally:
                    release.set()
                    app.post_message = original_post

    def test_same_agent_different_users_and_runtime_profiles_have_separate_results(self):
        calls = []

        def probe(_cfg, user, catalog, *, check_agents):
            self.assertFalse(check_agents)
            calls.append((user, frozenset(catalog)))
            return HostReport(
                BinaryStatus("tmux", "/usr/bin/tmux", ""),
                {aid: BinaryStatus(aid, f"/home/{user}/bin/{aid}", "") for aid in catalog},
                user,
            )

        ctx = _mk_ctx(
            agents=dict(DEFAULT_AGENT_CATALOG),
            enabled_profiles=("ops", "dev", "box"),
            default_profile="ops",
            launch_profiles={
                "ops": LaunchProfileOption("ops", "Ops", "claude", "alice"),
                "dev": LaunchProfileOption("dev", "Dev", "claude", "bob"),
                "box": LaunchProfileOption("box", "Box", "claude", "dana", runtime="docker"),
            },
        )
        app = UxonApp(ctx, probe_agents=False)
        posted = []
        app.post_message = lambda message: posted.append(message) or True
        with patch("uxon.infra.probes.probe_host", probe):
            app._worker_coord._probe_host_worker()
        self.assertCountEqual(
            calls,
            [
                ("alice", frozenset({"claude"})),
                ("bob", frozenset({"claude"})),
                ("dana", frozenset()),
            ],
        )
        self.assertEqual(len(posted), 1)
        self.assertFalse(posted[0].error)
        self.assertEqual(
            posted[0].availability,
            {
                "ops": AgentAvailability("ok", path="/home/alice/bin/claude"),
                "dev": AgentAvailability("ok", path="/home/bob/bin/claude"),
                "box": AgentAvailability("ok", path="runtime:docker"),
            },
        )
