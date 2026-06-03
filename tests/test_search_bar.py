from __future__ import annotations

import unittest

from textual.app import App

from uxon.tui.widgets.search_bar import FilterChanged, SearchBar


class _Harness(App):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []
        self.bar = SearchBar(id="search")

    def compose(self):
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

    async def test_search_bar_emits_filter_changed_on_typing(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            app.bar.show()
            await pilot.pause()
            await pilot.press("k", "r", "i", "s")
            await pilot.pause()
            self.assertEqual(app.events[-1], "kris")

    async def test_search_bar_starts_hidden_and_outside_focus_chain(self) -> None:
        """Default state: ``-shown`` class absent and the inner Input is
        flagged ``can_focus=False`` so Tab/Shift+Tab skip the invisible
        bar. Reaching into ``_filter.input`` is acceptable here — the
        test is a unit test of SearchBar's own focus-chain contract."""
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertFalse(app.bar.has_class("-shown"))
            self.assertFalse(app.bar._filter.input.can_focus)

    async def test_search_bar_show_reveals_and_focuses_input(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.bar.show()
            await pilot.pause()
            self.assertTrue(app.bar.has_class("-shown"))
            self.assertIsNotNone(app.focused)
            assert app.focused is not None
            self.assertEqual(app.focused.id, "filter-input")

    async def test_search_bar_esc_clears_then_hides(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            app.bar.show()
            await pilot.pause()
            await pilot.press("a", "b", "c")
            await pilot.pause()
            self.assertEqual(app.bar.value, "abc")
            await pilot.press("escape")  # clears text, bar stays open
            await pilot.pause()
            self.assertEqual(app.bar.value, "")
            self.assertTrue(app.bar.has_class("-shown"))
            self.assertIsNotNone(app.focused)
            assert app.focused is not None
            self.assertEqual(app.focused.id, "filter-input")
            await pilot.press("escape")  # hides bar, blurs input
            await pilot.pause()
            self.assertFalse(app.bar.has_class("-shown"))
            self.assertFalse(app.bar._filter.input.can_focus)
            self.assertTrue(app.focused is None or app.focused.id != "filter-input")
