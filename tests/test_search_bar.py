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
        app.bar.input.value = "kris"
        await pilot.pause()
        assert app.events[-1] == "kris"


@pytest.mark.asyncio
async def test_search_bar_esc_clears_then_blurs():
    app = _Harness()
    async with app.run_test() as pilot:
        app.bar.input.focus()
        await pilot.pause()
        app.bar.input.value = "abc"
        await pilot.pause()
        await pilot.press("escape")  # clears
        await pilot.pause()
        assert app.bar.input.value == ""
        assert app.focused is app.bar.input  # still focused
        await pilot.press("escape")  # blurs
        await pilot.pause()
        assert app.focused is not app.bar.input
