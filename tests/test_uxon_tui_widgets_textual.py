"""Pilot tests for ActionRow widget."""

from __future__ import annotations

import unittest


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_textual_available(), "textual not installed")
class ActionRowTests(unittest.IsolatedAsyncioTestCase):
    async def test_enter_activates_and_posts_message(self) -> None:
        from textual.app import App, ComposeResult

        from uxon.tui.widgets import ActionRow

        captured: list[str] = []

        class Host(App):
            def compose(self) -> ComposeResult:
                yield ActionRow(
                    kind="action-cwd",
                    label="New session",
                    detail="(cwd)",
                    id="row",
                )

            def on_action_row_activated(self, message: ActionRow.Activated) -> None:
                captured.append(message.row.kind)

        app = Host()
        async with app.run_test() as pilot:
            app.query_one("#row", ActionRow).focus()
            await pilot.press("enter")
            await pilot.pause()
        self.assertEqual(captured, ["action-cwd"])

    def test_disabled_row_tracks_enabled_state(self) -> None:
        from uxon.tui.widgets.action_row import action_row_can_activate

        self.assertFalse(action_row_can_activate(False))
        self.assertTrue(action_row_can_activate(True))


if __name__ == "__main__":
    unittest.main()
