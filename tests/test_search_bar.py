from __future__ import annotations

import pytest
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


@pytest.mark.asyncio
async def test_search_bar_emits_filter_changed_on_typing():
    app = _Harness()
    async with app.run_test() as pilot:
        app.bar.show()
        await pilot.pause()
        await pilot.press("k", "r", "i", "s")
        await pilot.pause()
        assert app.events[-1] == "kris"


@pytest.mark.asyncio
async def test_search_bar_starts_hidden_and_outside_focus_chain():
    """Default state: ``-shown`` class absent and the inner Input is
    flagged ``can_focus=False`` so Tab/Shift+Tab skip the invisible
    bar. Reaching into ``_filter.input`` is acceptable here — the
    test is a unit test of SearchBar's own focus-chain contract."""
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.bar.has_class("-shown")
        assert app.bar._filter.input.can_focus is False


@pytest.mark.asyncio
async def test_search_bar_show_reveals_and_focuses_input():
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.bar.show()
        await pilot.pause()
        assert app.bar.has_class("-shown")
        assert app.focused is not None and app.focused.id == "filter-input"


@pytest.mark.asyncio
async def test_search_bar_esc_clears_then_hides():
    app = _Harness()
    async with app.run_test() as pilot:
        app.bar.show()
        await pilot.pause()
        await pilot.press("a", "b", "c")
        await pilot.pause()
        assert app.bar.value == "abc"
        await pilot.press("escape")  # clears text, bar stays open
        await pilot.pause()
        assert app.bar.value == ""
        assert app.bar.has_class("-shown")
        assert app.focused is not None and app.focused.id == "filter-input"
        await pilot.press("escape")  # hides bar, blurs input
        await pilot.pause()
        assert not app.bar.has_class("-shown")
        assert app.bar._filter.input.can_focus is False
        assert app.focused is None or app.focused.id != "filter-input"
