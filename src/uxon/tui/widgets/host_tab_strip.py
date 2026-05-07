"""HostTabStrip — one tab per HostBucket.

Reactive ``active_index``. Posts :class:`HostTabActivated` on change.
The label is :attr:`HostBucket.label`; per-host colour comes from the
same ``assign_block_colors`` map shared with the dashboard glyph
column, passed through ``set_buckets(..., colors=...)`` so the strip
and the rows agree on the hue without re-deriving it here.

Focus contract
--------------

The strip is a horizontal switcher, not a focus group. **Only the
active tab is focusable** (per-instance ``can_focus`` toggle). That
makes the whole strip a single stop in the surrounding focus chain:

* Tab / Shift+Tab / ↑ / ↓ behave normally — the chain enters the
  strip on the active tab and leaves it on the next focusable
  widget (action row above, dashboard table below).
* ← / → on the active tab cycle ``active_index`` and move focus
  with it.
* Click on any tab activates it and pulls focus.
* ``[`` / ``]`` (screen-level bindings) cycle regardless of focus.
"""

from __future__ import annotations

from rich.text import Text
from textual import events
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


def _render_label(label: str, color: str) -> Text:
    """Rich :class:`Text` for a tab label.

    The leading ``●`` glyph carries the host's block colour so the
    tab matches its dashboard rows. Bold for the active tab is left
    to the CSS class (``HostTabStrip _TabButton.-active``) so we
    don't paint style twice. :class:`Text` over markup avoids a
    parse round-trip if a ``label`` ever contains ``[`` / ``]``.
    """
    text = Text()
    text.append("● ", style=color)
    text.append(label)
    return text


class _TabButton(Static):
    """Single tab. Per-instance ``can_focus`` — only the active tab
    is in the focus chain so Tab / ↑ / ↓ enter and leave the strip
    in one step instead of stopping on every tab.
    """

    # Class default: not focusable. The owning strip flips this to
    # ``True`` on the active tab in :meth:`HostTabStrip._sync_focus`.
    can_focus = False

    def __init__(self, *, index: int, id: str) -> None:
        super().__init__("", id=id)
        self.index = index

    def _strip(self) -> HostTabStrip | None:
        node = self.parent
        while node is not None and not isinstance(node, HostTabStrip):
            node = node.parent
        return node

    def on_click(self, event: events.Click) -> None:
        event.stop()
        strip = self._strip()
        if strip is None:
            return
        strip.active_index = self.index
        # ``watch_active_index`` flipped ``can_focus`` for us; safe to focus.
        self.focus()

    def on_key(self, event: events.Key) -> None:
        # ← / → cycle within the strip. ↑ / ↓ leave the strip via the
        # focus chain (where only the active tab is a stop, so they
        # land on the action row above / dashboard table below in one
        # step). Tab / Shift+Tab fall through to the default handler.
        if event.key == "left":
            event.stop()
            self._cycle(-1)
        elif event.key == "right":
            event.stop()
            self._cycle(1)
        elif event.key == "down":
            event.stop()
            self.screen.focus_next()
        elif event.key == "up":
            event.stop()
            self.screen.focus_previous()

    def _cycle(self, delta: int) -> None:
        strip = self._strip()
        if strip is None:
            return
        n = len(strip._buckets)
        if n <= 1:
            return
        new_idx = (self.index + delta) % n
        strip.active_index = new_idx
        try:
            strip.query_one(f"#tab-{new_idx}", _TabButton).focus()
        except Exception:
            pass


class HostTabStrip(Widget):
    DEFAULT_CSS = """
    HostTabStrip {
        height: 1;
        padding: 0 1;
    }
    HostTabStrip > Horizontal {
        height: 1;
    }
    HostTabStrip _TabButton {
        width: auto;
        margin-right: 2;
        text-style: dim;
    }
    HostTabStrip _TabButton.-active {
        text-style: bold;
        background: $accent 30%;
    }
    HostTabStrip _TabButton:focus {
        text-style: bold;
    }
    """

    active_index: reactive[int] = reactive(0)

    def __init__(self, buckets: list[HostBucket], *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._buckets = buckets
        self._colors: dict[str | None, str] = {}

    def compose(self) -> ComposeResult:
        with Horizontal():
            for i, bucket in enumerate(self._buckets):
                yield self._build_tab(i, bucket)

    def _build_tab(self, i: int, bucket: HostBucket) -> _TabButton:
        btn = _TabButton(index=i, id=f"tab-{i}")
        color = self._colors.get(bucket.host_name, "white")
        btn.update(_render_label(bucket.label, color))
        active = i == self.active_index
        if active:
            btn.add_class("-active")
        btn.can_focus = active
        return btn

    def watch_active_index(self, old: int, new: int) -> None:
        if old == new:
            return
        for i, bucket in enumerate(self._buckets):
            try:
                w = self.query_one(f"#tab-{i}", _TabButton)
            except Exception:
                continue
            active = i == new
            w.set_class(active, "-active")
            w.can_focus = active
            color = self._colors.get(bucket.host_name, "white")
            w.update(_render_label(bucket.label, color))
        self.post_message(HostTabActivated(new))

    def set_buckets(
        self,
        buckets: list[HostBucket],
        *,
        colors: dict[str | None, str] | None = None,
    ) -> None:
        """Replace the bucket list (and optional host→colour map).

        Reuses existing :class:`_TabButton` children where possible —
        Textual's ``child.remove()`` is asynchronous, so rebuilding
        from scratch and re-mounting same-id widgets in one pass
        collides on the still-alive old ids.
        """
        self._buckets = buckets
        if colors is not None:
            self._colors = colors
        try:
            container = self.query_one(Horizontal)
        except Exception:
            return
        existing = list(container.children)
        # Update / append.
        for i, bucket in enumerate(buckets):
            active = i == self.active_index
            color = self._colors.get(bucket.host_name, "white")
            label_text = _render_label(bucket.label, color)
            if i < len(existing):
                w = existing[i]
                if isinstance(w, _TabButton):
                    w.update(label_text)
                    w.set_class(active, "-active")
                    w.can_focus = active
            else:
                btn = self._build_tab(i, bucket)
                container.mount(btn)
        # Drop excess (async removal — IDs free up next frame).
        for w in existing[len(buckets) :]:
            w.remove()
