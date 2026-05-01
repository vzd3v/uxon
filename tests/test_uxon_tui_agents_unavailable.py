"""Pilot tests for AgentsUnavailableScreen and the app-level gate."""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.abspath(os.path.join(_HERE, "..", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _mk_ctx(**overrides):
    from uxon_tui.context import LaunchRequest, TuiContext

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
        from uxon_tui.screens.agents_unavailable import AgentsUnavailableScreen

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
        from uxon_tui.screens.agents_unavailable import AgentsUnavailableScreen

        bindings = {binding.key: binding.action for binding in AgentsUnavailableScreen.BINDINGS}
        self.assertEqual(bindings.get("escape"), "dismiss_screen")


@unittest.skipUnless(_textual_available(), "textual not installed")
class AppLevelGateTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end: CcwApp pushes AgentsUnavailableScreen iff all probes miss."""

    async def test_pushes_when_all_agents_missing(self) -> None:
        import uxon_agents
        from uxon_tui.app import CcwApp, _AgentAvailabilityUpdated
        from uxon_tui.screens.agents_unavailable import AgentsUnavailableScreen

        ctx = _mk_ctx(
            enabled_agents=("claude", "codex"),
            default_agent="claude",
        )

        app = CcwApp(ctx, probe_agents=False)
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


if __name__ == "__main__":
    unittest.main()
