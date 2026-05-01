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
        from uxon_tui.widgets import ActionRow
        from textual.app import App, ComposeResult

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

    def test_disabled_row_tracks_enabled_state(self) -> None:
        from uxon_tui.widgets.action_row import action_row_can_activate

        self.assertFalse(action_row_can_activate(False))
        self.assertTrue(action_row_can_activate(True))


@unittest.skipUnless(_textual_available(), "textual not installed")
class SessionTableTests(unittest.IsolatedAsyncioTestCase):
    async def test_populate_adds_rows_and_preserves_cursor(self) -> None:
        from uxon_tui.context import TuiSession
        from uxon_tui.widgets import SessionTable
        from textual.app import App, ComposeResult

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

    def test_session_table_shows_stem_not_full_name(self) -> None:
        """Name cell renders session.stem, not the full tmux session name."""
        from uxon_tui.context import TuiSession
        from uxon_tui.widgets import SessionTable

        session = TuiSession(
            name="ccw-myproject@codex",
            short="myproject@codex",
            attached=False,
            pid="1",
            cpu="0",
            ram="1M",
            created="1s",
            last_activity="1s",
            cmd="codex",
            path="/srv/work",
            user="devagent",
            stem="myproject",
            agent="codex",
            legacy=False,
        )

        self.assertEqual(SessionTable._display_name(session), "myproject")

    def test_session_table_show_user_column_labels(self) -> None:
        from uxon_tui.widgets import SessionTable

        labels = SessionTable.column_labels(show_user=True, show_agent_column=False)
        self.assertEqual(labels[0], "USER")

    def test_session_table_agent_column_when_multi(self) -> None:
        """show_agent_column=True adds AGENT header and per-row agent labels."""
        from uxon_tui.context import TuiSession
        from uxon_tui.widgets import SessionTable

        def _s(name, stem, agent, legacy=False):
            return TuiSession(
                name=name,
                short=stem,
                attached=False,
                pid="1",
                cpu="0",
                ram="1M",
                created="1s",
                last_activity="1s",
                cmd=agent,
                path="/srv/work",
                user="dev",
                stem=stem,
                agent=agent,
                legacy=legacy,
            )

        sessions = [_s("ccw-foo@claude", "foo", "claude"), _s("ccw-foo@codex", "foo", "codex")]

        self.assertIn(
            "AGENT",
            SessionTable.column_labels(show_user=False, show_agent_column=True),
        )
        self.assertEqual(SessionTable._agent_label(sessions[0]), "claude")
        self.assertEqual(SessionTable._agent_label(sessions[1]), "codex")

    async def test_session_table_legacy_label(self) -> None:
        """Legacy cc-<stem> session shows 'claude (legacy)' in the agent cell."""
        from uxon_tui.context import TuiSession
        from uxon_tui.widgets import SessionTable

        s = TuiSession(
            name="cc-oldproject",
            short="oldproject",
            attached=False,
            pid="1",
            cpu="0",
            ram="1M",
            created="1s",
            last_activity="1s",
            cmd="claude",
            path="/srv/work",
            user="dev",
            stem="oldproject",
            agent="claude",
            legacy=True,
        )
        label = SessionTable._agent_label(s)
        self.assertEqual(label, "claude (legacy)")


if __name__ == "__main__":
    unittest.main()
