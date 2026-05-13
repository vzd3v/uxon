"""HostTabStrip pilot test: ``active_index`` change posts ``HostTabActivated``."""

from __future__ import annotations

import unittest


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _bucket(name):
    from uxon.tui.dashboard.buckets import HostBucket

    return HostBucket(host_name=name, label=name or "local", rows=())


@unittest.skipUnless(_textual_available(), "textual not installed")
class TabStripTests(unittest.IsolatedAsyncioTestCase):
    async def test_tab_strip_emits_activated_on_index_change(self) -> None:
        from textual.app import App

        from uxon.tui.widgets.host_tab_strip import HostTabActivated, HostTabStrip

        events: list[int] = []

        class _App(App):
            def compose(self):
                yield HostTabStrip([_bucket(None), _bucket("kris"), _bucket("ada")], id="strip")

            def on_host_tab_activated(self, event: HostTabActivated) -> None:
                events.append(event.index)

        app = _App()
        async with app.run_test() as pilot:
            await pilot.pause()
            strip = app.query_one("#strip", HostTabStrip)
            strip.active_index = 1
            await pilot.pause()
            assert events == [1], events


if __name__ == "__main__":
    unittest.main()
