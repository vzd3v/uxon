"""List picker whose arrow navigation wraps at either end."""

from __future__ import annotations

from textual.widgets import ListView


class CyclicListView(ListView):
    """Keep ListView selection events and scrolling, with cyclic arrows."""

    def action_cursor_up(self) -> None:
        self.move_cursor(-1)

    def action_cursor_down(self) -> None:
        self.move_cursor(1)

    def move_cursor(self, direction: int) -> None:
        """Move one row, skipping disabled items and wrapping at the ends."""
        count = len(self.children)
        if not count:
            return
        current = self.index
        if current is None:
            current = -1 if direction > 0 else 0
        for offset in range(1, count + 1):
            index = (current + direction * offset) % count
            if not self.children[index].disabled:
                self.index = index
                return
