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
    async def test_enabled_and_disabled_keyboard_and_mouse_activation(self) -> None:
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
            row = app.query_one("#row", ActionRow)
            row.focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(captured, ["action-cwd"])
            self.assertTrue(await pilot.click("#row"))
            await pilot.pause()
            self.assertEqual(captured, ["action-cwd", "action-cwd"])
            row.set_enabled(False)
            await pilot.press("enter")
            self.assertTrue(await pilot.click("#row"))
            await pilot.pause()
            self.assertEqual(captured, ["action-cwd", "action-cwd"])
            row.set_enabled(True)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(captured, ["action-cwd"] * 3)


if __name__ == "__main__":
    unittest.main()
