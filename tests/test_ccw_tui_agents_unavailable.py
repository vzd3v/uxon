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
    from ccw_tui.context import LaunchRequest, TuiContext
    base = dict(
        sessions=[],
        total_cpu="0",
        total_ram="0",
        version="0.0.0",
        cwd="/srv/work",
        cwd_short="work",
        new_project_root="/srv/work",
        existing_projects=[],
        cwd_allowed=True,
        current_user="devagent",
        on_launch_cwd=lambda a, m: LaunchRequest(cmd=("/bin/true",), label="cwd"),
        on_launch_new=lambda n, a, m, g: LaunchRequest(cmd=("/bin/true",), label="new"),
        on_launch_existing=lambda n, a, m: LaunchRequest(cmd=("/bin/true",), label="existing"),
    )
    base.update(overrides)
    return TuiContext(**base)


@unittest.skipUnless(_textual_available(), "textual not installed")
class AgentsUnavailableScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_each_enabled_agent_with_install_hint(self) -> None:
        from textual.app import App
        from ccw_tui.screens.agents_unavailable import AgentsUnavailableScreen

        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            screen = AgentsUnavailableScreen(
                enabled_agents=("claude", "codex", "cursor"),
            )
            app.push_screen(screen)
            await pilot.pause()
            text = screen.body_text
            # Each agent id appears
            self.assertIn("claude", text)
            self.assertIn("codex", text)
            self.assertIn("cursor", text)
            # Each install hint from CATALOG appears (substring is enough)
            self.assertIn("docs.claude.com", text)
            self.assertIn("npm i -g @openai/codex", text)
            self.assertIn("cursor.com/install", text)
            # And the widget is actually mounted with that id.
            self.assertIsNotNone(screen.query_one("#agents-unavailable-body"))

    async def test_escape_dismisses_with_none(self) -> None:
        from textual.app import App
        from ccw_tui.screens.agents_unavailable import AgentsUnavailableScreen

        class Host(App):
            result = "unset"

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(
                    AgentsUnavailableScreen(enabled_agents=("claude",)),
                    done,
                )

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        self.assertIsNone(app.result)


if __name__ == "__main__":
    unittest.main()
