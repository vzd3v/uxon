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
        app.bar.input.value = "kris"
        await pilot.pause()
        assert app.events[-1] == "kris"


@pytest.mark.asyncio
async def test_search_bar_starts_hidden_with_input_unfocusable():
    """Default state: ``-shown`` class absent and Input.can_focus=False
    so Tab/Shift+Tab skip the invisible bar."""
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.bar.has_class("-shown")
        assert app.bar.input.can_focus is False


@pytest.mark.asyncio
async def test_search_bar_show_reveals_and_focuses_input():
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.bar.show()
        await pilot.pause()
        assert app.bar.has_class("-shown")
        assert app.bar.input.can_focus is True
        assert app.focused is app.bar.input


@pytest.mark.asyncio
async def test_search_bar_esc_clears_then_hides():
    app = _Harness()
    async with app.run_test() as pilot:
        app.bar.show()
        await pilot.pause()
        app.bar.input.value = "abc"
        await pilot.pause()
        await pilot.press("escape")  # clears text, bar stays open
        await pilot.pause()
        assert app.bar.input.value == ""
        assert app.bar.has_class("-shown")
        assert app.focused is app.bar.input
        await pilot.press("escape")  # hides bar, blurs input
        await pilot.pause()
        assert not app.bar.has_class("-shown")
        assert app.bar.input.can_focus is False
        assert app.focused is not app.bar.input
