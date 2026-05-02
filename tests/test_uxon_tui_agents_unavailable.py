"""Pilot tests for AgentsUnavailableScreen and the app-level gate."""

from __future__ import annotations

import unittest


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _mk_ctx(**overrides):
    from uxon.tui.context import LaunchRequest, TuiContext

    base = dict(
        sessions=[],
        total_cpu="0",
        total_ram="0",
        version="0.0.0",
        cwd="/srv/work",
        cwd_short="work",
        new_project_root="/srv/work",
        existing_projects=[],
        cwd_writable=True,
        current_user="devagent",
        on_launch_cwd=lambda a, m: LaunchRequest(cmd=("/bin/true",), label="cwd"),
        on_launch_new=lambda n, a, m, g: LaunchRequest(cmd=("/bin/true",), label="new"),
        on_launch_existing=lambda n, a, m: LaunchRequest(cmd=("/bin/true",), label="existing"),
    )
    base.update(overrides)
    return TuiContext(**base)


@unittest.skipUnless(_textual_available(), "textual not installed")
class AgentsUnavailableScreenTests(unittest.TestCase):
    def test_lists_each_enabled_agent_with_install_hint(self) -> None:
        from uxon.tui.screens.agents_unavailable import AgentsUnavailableScreen

        screen = AgentsUnavailableScreen(
            enabled_agents=("claude", "codex", "cursor"),
        )
        text = screen.body_text
        self.assertIn("claude", text)
        self.assertIn("codex", text)
        self.assertIn("cursor", text)
        self.assertIn("docs.claude.com", text)
        self.assertIn("npm i -g @openai/codex", text)
        self.assertIn("cursor.com/install", text)

    def test_escape_binding_is_declared(self) -> None:
        from uxon.tui.screens.agents_unavailable import AgentsUnavailableScreen

        bindings = {binding.key: binding.action for binding in AgentsUnavailableScreen.BINDINGS}
        self.assertEqual(bindings.get("escape"), "dismiss_screen")


@unittest.skipUnless(_textual_available(), "textual not installed")
class AppLevelGateTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end: UxonApp pushes AgentsUnavailableScreen iff all probes miss."""

    async def test_pushes_when_all_agents_missing(self) -> None:
        from uxon import agents as uxon_agents
        from uxon.tui.app import UxonApp, _AgentAvailabilityUpdated
        from uxon.tui.screens.agents_unavailable import AgentsUnavailableScreen

        ctx = _mk_ctx(
            enabled_agents=("claude", "codex"),
            default_agent="claude",
        )

        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            ctx.agent_availability.clear()
            ctx.agent_availability.update(
                {
                    aid: uxon_agents.AgentAvailability(status="missing", error="not found")
                    for aid in ctx.enabled_agents
                }
            )
            app.post_message(_AgentAvailabilityUpdated())
            await pilot.pause()
            self.assertTrue(
                any(isinstance(s, AgentsUnavailableScreen) for s in app.screen_stack),
                f"popup not pushed; stack={app.screen_stack!r}",
            )
            await pilot.press("q")
            await pilot.pause()

    async def test_rearm_after_dismiss_when_state_degrades_again(self) -> None:
        """Transition gate: after the user dismisses the modal and state
        recovers, a later transition back to all-missing must push again.
        """
        from uxon import agents as uxon_agents
        from uxon.tui.app import UxonApp, _HostReportUpdated
        from uxon.tui.screens.agents_unavailable import AgentsUnavailableScreen

        ctx = _mk_ctx(enabled_agents=("claude",), default_agent="claude")

        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()

            # First tick: all-missing → pushes modal.
            ctx.agent_availability["claude"] = uxon_agents.AgentAvailability(
                status="missing", error="not found"
            )
            app.post_message(_HostReportUpdated())
            await pilot.pause()
            self.assertTrue(any(isinstance(s, AgentsUnavailableScreen) for s in app.screen_stack))

            # User dismisses (Esc on AgentsUnavailableScreen).
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(any(isinstance(s, AgentsUnavailableScreen) for s in app.screen_stack))

            # State recovers: agent becomes available; do NOT auto-pop
            # (no modal to pop, but no re-push either).
            ctx.agent_availability["claude"] = uxon_agents.AgentAvailability(status="ok")
            app.post_message(_HostReportUpdated())
            await pilot.pause()
            self.assertFalse(any(isinstance(s, AgentsUnavailableScreen) for s in app.screen_stack))

            # State degrades again → re-pushes.
            ctx.agent_availability["claude"] = uxon_agents.AgentAvailability(
                status="missing", error="gone again"
            )
            app.post_message(_HostReportUpdated())
            await pilot.pause()
            self.assertTrue(
                any(isinstance(s, AgentsUnavailableScreen) for s in app.screen_stack),
                "re-arm after recovery → degradation must push again",
            )

            await pilot.press("q")
            await pilot.pause()


if __name__ == "__main__":
    unittest.main()
