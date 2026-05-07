"""HostTabStrip — one tab per HostBucket.

Reactive ``active_index``. Posts :class:`HostTabActivated` on change.
The label is :attr:`HostBucket.label`; coloring is done by the screen
via the same ``assign_block_colors`` map shared with the dashboard.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ..dashboard.buckets import HostBucket


class HostTabActivated(Message):
    """Posted whenever ``active_index`` changes."""

    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index


class HostTabStrip(Widget):
    DEFAULT_CSS = """
    HostTabStrip {
        height: 1;
        padding: 0 1;
    }
    HostTabStrip > Horizontal {
        height: 1;
    }
    HostTabStrip Static {
        margin-right: 2;
        text-style: dim;
    }
    HostTabStrip Static.-active {
        text-style: bold;
        background: $accent 30%;
    }
    """

    active_index: reactive[int] = reactive(0)

    def __init__(self, buckets: list[HostBucket], *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._buckets = buckets

    def compose(self) -> ComposeResult:
        with Horizontal():
            for i, bucket in enumerate(self._buckets):
                cls = "-active" if i == self.active_index else ""
                yield Static(bucket.label, classes=cls, id=f"tab-{i}")

    def watch_active_index(self, old: int, new: int) -> None:
        if old == new:
            return
        for i, _ in enumerate(self._buckets):
            try:
                w = self.query_one(f"#tab-{i}", Static)
            except Exception:
                continue
            w.set_class(i == new, "-active")
        self.post_message(HostTabActivated(new))

    def set_buckets(self, buckets: list[HostBucket]) -> None:
        """Replace the bucket list, reusing existing Static children
        where possible. Textual's ``child.remove()`` is asynchronous, so
        rebuilding from scratch and re-mounting same-id widgets in one
        pass collides on the still-alive old IDs.
        """
        self._buckets = buckets
        try:
            container = self.query_one(Horizontal)
        except Exception:
            return
        existing = list(container.children)
        # Update / append.
        for i, bucket in enumerate(buckets):
            cls_active = i == self.active_index
            if i < len(existing):
                w = existing[i]
                if isinstance(w, Static):
                    w.update(bucket.label)
                    w.set_class(cls_active, "-active")
            else:
                container.mount(
                    Static(
                        bucket.label,
                        classes="-active" if cls_active else "",
                        id=f"tab-{i}",
                    )
                )
        # Drop excess (async removal — IDs free up next frame).
        for w in existing[len(buckets) :]:
            w.remove()
