"""Pilot tests for ActionRow and SessionTable widgets (T6)."""

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


@unittest.skipUnless(_textual_available(), "textual not installed")
class ActionRowTests(unittest.IsolatedAsyncioTestCase):
    async def test_enter_activates_and_posts_message(self) -> None:
        from textual.app import App, ComposeResult
        from ccw_tui.widgets import ActionRow

        captured: list[str] = []

        class Host(App):
            def compose(self) -> ComposeResult:
                yield ActionRow(
                    kind="action-cwd",
                    label="New session",
                    detail="(cwd)",
                    digit=1,
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

    async def test_disabled_row_ignores_activation(self) -> None:
        from textual.app import App, ComposeResult
        from ccw_tui.widgets import ActionRow

        captured: list[str] = []

        class Host(App):
            def compose(self) -> ComposeResult:
                yield ActionRow(
                    kind="action-cwd", label="X", enabled=False, id="row",
                )

            def on_action_row_activated(self, message: ActionRow.Activated) -> None:
                captured.append(message.row.kind)

        app = Host()
        async with app.run_test() as pilot:
            app.query_one("#row", ActionRow).focus()
            await pilot.press("enter")
            await pilot.pause()
        self.assertEqual(captured, [])


@unittest.skipUnless(_textual_available(), "textual not installed")
class SessionTableTests(unittest.IsolatedAsyncioTestCase):
    async def test_populate_adds_rows_and_preserves_cursor(self) -> None:
        from textual.app import App, ComposeResult
        from ccw_tui.context import TuiSession
        from ccw_tui.widgets import SessionTable

        sessions = [
            TuiSession(
                name="devagent.foo",
                short="foo",
                attached=True,
                pid="1234",
                cpu="12.5",
                ram="50M",
                created="1m",
                last_activity="2s",
                cmd="claude",
                path="/srv/work",
                user="devagent",
            ),
            TuiSession(
                name="devagent.bar",
                short="bar",
                attached=False,
                pid="5678",
                cpu="85.0",
                ram="100M",
                created="5m",
                last_activity="30s",
                cmd="claude",
                path="/srv/work",
                user="devagent",
            ),
        ]

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionTable(id="sessions")

        app = Host()
        async with app.run_test() as pilot:
            t = app.query_one(SessionTable)
            t.populate(sessions)
            await pilot.pause()
            self.assertEqual(t.row_count, 2)
            self.assertIsNotNone(t.session_at(0))
            self.assertEqual(t.session_at(0).short, "foo")

    async def test_show_user_column(self) -> None:
        from textual.app import App, ComposeResult
        from ccw_tui.context import TuiSession
        from ccw_tui.widgets import SessionTable

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionTable(show_user=True, id="sessions")

        app = Host()
        async with app.run_test() as pilot:
            t = app.query_one(SessionTable)
            t.populate([
                TuiSession(
                    name="x.y", short="y", attached=False,
                    pid="1", cpu="0", ram="1M", created="1s",
                    last_activity="1s", cmd="claude", path="/", user="alice",
                ),
            ])
            await pilot.pause()
            # USER column exists first.
            cols = list(t.columns)
            self.assertEqual(t.columns[cols[0]].label.plain, "USER")


if __name__ == "__main__":
    unittest.main()
