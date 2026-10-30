from __future__ import annotations

import unittest

from textual.app import App
from textual.widgets import Button

from uxon.tui.widgets.search_bar import FilterChanged, SearchBar


class _Harness(App):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []
        self.bar = SearchBar(id="search")

    def compose(self):
        yield Button("Default caller", id="action-open")
        yield Button("Other caller", id="other")
        yield self.bar

    def on_filter_changed(self, event: FilterChanged) -> None:
        self.events.append(event.text)


class SearchBarTests(unittest.IsolatedAsyncioTestCase):
    """SearchBar focus-chain and filter-emit contract.

    Uses ``IsolatedAsyncioTestCase`` (a fresh, self-closing event loop
    per test) rather than ``@pytest.mark.asyncio`` — the latter leaves a
    package-scoped loop unclosed, whose late GC surfaces as a
    ``PytestUnraisableExceptionWarning`` under ``-n auto`` contention and
    fails an unrelated sibling test (``filterwarnings = ["error"]``).
    All other TUI Pilot tests already use this base class.
    """

    async def test_search_focus_typing_clear_hide_and_reopen(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertFalse(app.bar.display)
            app.query_one("#action-open", Button).focus()
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(app.focused.id, "other")
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-open")
            app.bar.show(return_focus_id="other")
            await pilot.pause()
            self.assertTrue(app.bar.display)
            self.assertEqual(app.focused.id, "filter-input")
            await pilot.press("1", "2", "3")
            await pilot.pause()
            self.assertEqual(app.bar.value, "123")
            self.assertEqual(app.events[-1], "123")
            await pilot.press("escape")  # clears text, bar stays open
            await pilot.pause()
            self.assertEqual(app.bar.value, "")
            self.assertEqual(app.events[-1], "")
            self.assertTrue(app.bar.display)
            self.assertEqual(app.focused.id, "filter-input")
            await pilot.press("escape")  # hides bar, blurs input
            await pilot.pause()
            self.assertFalse(app.bar.display)
            self.assertEqual(app.focused.id, "other")
            app.bar.show()  # id-less reopening resets the previous caller
            await pilot.pause()
            self.assertTrue(app.bar.display)
            self.assertEqual(app.focused.id, "filter-input")
            self.assertEqual(app.bar.value, "")
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.bar.display)
            self.assertEqual(app.focused.id, "action-open")
