"""Pilot tests for ``RemoteSessionTable.update_host_rows`` — the
per-host repaint path introduced in commit 4 of the TuiState split.

Pinned contracts:

* ``populate`` keeps working as the full-replace path (initial mount,
  layout-change re-mount); per-host landings go through
  ``update_host_rows``.
* ``update_host_rows(host, rows)`` rewrites *only* the rows for that
  host; other peers' rows are untouched.
* The DataTable's row-keyed API is used (``add_row(*cells, key=…)``,
  ``remove_row(key)``) — no ``self.clear()`` inside ``update_host_rows``.
* Spy on ``add_row`` / ``remove_row`` confirms exactly the rows for
  the changed host are touched on a single-host landing.
"""

from __future__ import annotations

import unittest


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_textual_available(), "textual not installed")
class UpdateHostRowsTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_changed_host_touched(self) -> None:
        """Two peers populated initially. Calling ``update_host_rows``
        for one host must remove that host's old rows and add its new
        rows — and not touch any row of the other peer.
        """
        from textual.app import App, ComposeResult

        from uxon.tui.widgets import RemoteSessionTable

        class Host(App):
            def compose(self) -> ComposeResult:
                yield RemoteSessionTable(show_host=True, id="rt")

        app = Host()
        async with app.run_test() as pilot:
            table: RemoteSessionTable = app.query_one("#rt", RemoteSessionTable)
            # Initial population: 2 hosts, 2 rows each.
            initial_rows = [
                ("host-a", {"user": "u1", "name": "a1", "short_id": "a1"}),
                ("host-a", {"user": "u1", "name": "a2", "short_id": "a2"}),
                ("host-b", {"user": "u2", "name": "b1", "short_id": "b1"}),
                ("host-b", {"user": "u2", "name": "b2", "short_id": "b2"}),
            ]
            table.populate(initial_rows)
            await pilot.pause()
            self.assertEqual(table.row_count, 4)

            # Spy on the structural mutators.
            add_calls: list[tuple] = []
            remove_calls: list[str] = []
            real_add = table.add_row
            real_remove = table.remove_row

            def spy_add(*cells, **kw):
                add_calls.append((cells, kw))
                return real_add(*cells, **kw)

            def spy_remove(key):
                remove_calls.append(key)
                return real_remove(key)

            table.add_row = spy_add  # type: ignore[method-assign]
            table.remove_row = spy_remove  # type: ignore[method-assign]

            # Update host-a: drop a2, keep a1, add a3.
            table.update_host_rows(
                "host-a",
                [
                    ("host-a", {"user": "u1", "name": "a1", "short_id": "a1"}),
                    ("host-a", {"user": "u1", "name": "a3", "short_id": "a3"}),
                ],
            )
            await pilot.pause()

            # Removed both old host-a rows (a1, a2). Added the new
            # host-a rows (a1, a3). host-b never appears in either spy.
            self.assertEqual(len(remove_calls), 2)
            self.assertTrue(all(k.startswith("host-a/") for k in remove_calls))
            self.assertEqual(len(add_calls), 2)
            for cells, _kw in add_calls:
                # First cell is the HOST column (Text(host_name, …)).
                # Use ``str(...)`` to extract the rendered text since
                # ``rich.text.Text`` is not a plain str.
                first_cell = cells[0]
                rendered = first_cell.plain if hasattr(first_cell, "plain") else str(first_cell)
                self.assertEqual(rendered, "host-a")

            # End-state: host-a now has [a1, a3]; host-b unchanged.
            self.assertEqual(table.row_count, 4)
            host_names = [h for (h, _r) in table._row_index]
            # host-b's rows were never touched.
            self.assertEqual(host_names.count("host-b"), 2)

    async def test_multi_host_decorated_display_name_round_trip(self) -> None:
        """Regression: ``apply_remote_snapshot`` passes the bare
        host name to ``update_host_rows``, but the rows' display
        name carries a ``(own only) [badge]`` decoration in
        multi-host mode. The DataTable row key uses the *display
        name* (because that's what ``add_row(key=...)`` used during
        insertion); building the drop key with the bare host name
        would silently fail (``remove_row`` raises on miss, caught
        by the defensive bare-except), leaking old rows alongside
        the new ones.

        Pin it: insert a host with a decorated display name, call
        ``update_host_rows`` with the bare name, assert the row
        count stays bounded by the new rows for that host.
        """
        from textual.app import App, ComposeResult

        from uxon.tui.widgets import RemoteSessionTable

        class Host(App):
            def compose(self) -> ComposeResult:
                yield RemoteSessionTable(show_host=True, id="rt")

        app = Host()
        async with app.run_test() as pilot:
            table: RemoteSessionTable = app.query_one("#rt", RemoteSessionTable)
            decorated = "host-a (own only) [ok]"
            table.populate(
                [
                    (decorated, {"user": "u1", "name": "a1", "short_id": "a1"}),
                    (decorated, {"user": "u1", "name": "a2", "short_id": "a2"}),
                    ("host-b [ok]", {"user": "u2", "name": "b1", "short_id": "b1"}),
                ]
            )
            await pilot.pause()
            self.assertEqual(table.row_count, 3)

            # Now update host-a with one row replacing two — the
            # caller passes the bare canonical name as the first arg
            # but the row tuples carry the (possibly different)
            # decorated display name.
            new_decorated = "host-a (own only) [stale]"
            table.update_host_rows(
                "host-a",
                [(new_decorated, {"user": "u1", "name": "a3", "short_id": "a3"})],
            )
            await pilot.pause()
            # host-a now has 1 row, host-b still has 1 row → 2 total.
            # Pre-fix this was 4 (the two old host-a rows leaked).
            self.assertEqual(table.row_count, 2)

    async def test_empty_rows_drops_host(self) -> None:
        """A host that lost all its sessions should disappear from the
        table when ``update_host_rows(host, [])`` lands.
        """
        from textual.app import App, ComposeResult

        from uxon.tui.widgets import RemoteSessionTable

        class Host(App):
            def compose(self) -> ComposeResult:
                yield RemoteSessionTable(show_host=True, id="rt")

        app = Host()
        async with app.run_test() as pilot:
            table: RemoteSessionTable = app.query_one("#rt", RemoteSessionTable)
            table.populate(
                [
                    ("host-a", {"user": "u1", "name": "a1", "short_id": "a1"}),
                    ("host-b", {"user": "u2", "name": "b1", "short_id": "b1"}),
                ]
            )
            await pilot.pause()
            table.update_host_rows("host-a", [])
            await pilot.pause()
            self.assertEqual(table.row_count, 1)
            self.assertEqual(table._row_index[0][0], "host-b")


if __name__ == "__main__":
    unittest.main()
