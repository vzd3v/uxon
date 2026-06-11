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
  with it. The dashboard table forwards ← / → via a
  :class:`HostNavigate` event so the same gesture cycles hosts
  even when the cursor lives on a row.
* Click on any tab activates it and pulls focus.
"""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
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

    Navigation goes through ``BINDINGS`` (no ``on_key`` — AGENTS.md hard
    rule, enforced by ``tests/test_uxon_tui_bindings.py``).
    """

    # Class default: not focusable. The owning strip flips this to
    # ``True`` on the active tab in :meth:`HostTabStrip._sync_focus`.
    can_focus = False

    BINDINGS = [
        # ← / → cycle within the strip. ↑ / ↓ leave it via the focus
        # chain (only the active tab is a stop, so one press lands on the
        # action row above / dashboard below). Tab / Shift+Tab fall
        # through to the default handler.
        Binding("left", "cycle(-1)", "", show=False),
        Binding("right", "cycle(1)", "", show=False),
        Binding("up", "leave_up", "", show=False),
        Binding("down", "leave_down", "", show=False),
    ]

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

    def action_leave_down(self) -> None:
        self.screen.focus_next()

    def action_leave_up(self) -> None:
        # The action rows above are a vertical stack, so the previous
        # focus stop is the row directly above the strip.
        self.screen.focus_previous()

    def action_cycle(self, delta: int) -> None:
        self._cycle(delta)

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
    /* Active without focus — soft tint just so the operator knows
       which host's rows the table is showing. */
    HostTabStrip _TabButton.-active {
        text-style: bold;
        background: $accent 20%;
    }
    /* Active and focused (= keyboard cursor is on it) — stronger
       fill plus an underline acts as the cursor marker, separate
       from the 'this is the visible host' marker above. */
    HostTabStrip _TabButton:focus {
        text-style: bold underline;
        background: $accent 60%;
    }
    """

    active_index: reactive[int] = reactive(0)

    def __init__(self, buckets: list[HostBucket], *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._buckets = buckets
        self._colors: dict[str | None, str] = {}
        # ``(label, color, active)`` per tab as last written — the change
        # gate for :meth:`set_buckets`. A per-tick rebuild whose tabs match
        # the last applied state issues zero ``update`` / ``set_class`` /
        # ``can_focus`` writes (RC3). Kept in sync by ``watch_active_index``
        # for the tabs it rewrites on a tab switch.
        self._last_tabs: list[tuple[str, str, bool]] = []

    def compose(self) -> ComposeResult:
        with Horizontal():
            for i, bucket in enumerate(self._buckets):
                yield self._build_tab(i, bucket)

    def _build_tab(self, i: int, bucket: HostBucket) -> _TabButton:
        btn = _TabButton(index=i, id=f"tab-{i}")
        color = self._colors.get(bucket.host_name, "white")
        # ``layout=False``: tab content is a fixed-size cell; a label/colour
        # swap never resizes it, so skip the screen-global relayout (AC8).
        btn.update(_render_label(bucket.label, color), layout=False)
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
            w.update(_render_label(bucket.label, color), layout=False)
            # Keep the change-gate in sync with the tabs we just rewrote so
            # a following ``set_buckets`` doesn't skip a needed write.
            if i < len(self._last_tabs):
                self._last_tabs[i] = (bucket.label, color, active)
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
        # Clamp the active index when the bucket list shrinks (e.g. a
        # remote host disappears mid-session). Without this, the
        # highlighted tab and the active row group can drift apart
        # until the operator presses ``[`` / ``]``.
        if buckets and self.active_index >= len(buckets):
            self.active_index = len(buckets) - 1
        try:
            container = self.query_one(Horizontal)
        except Exception:
            return
        existing = list(container.children)
        new_last_tabs: list[tuple[str, str, bool]] = []
        # Update / append.
        for i, bucket in enumerate(buckets):
            active = i == self.active_index
            color = self._colors.get(bucket.host_name, "white")
            triple = (bucket.label, color, active)
            new_last_tabs.append(triple)
            if i < len(existing):
                w = existing[i]
                if isinstance(w, _TabButton):
                    # Skip the writes when this tab is unchanged vs the last
                    # applied state (RC3) — but only for tabs that actually
                    # existed last pass (``i < len(self._last_tabs)``).
                    if i < len(self._last_tabs) and self._last_tabs[i] == triple:
                        continue
                    w.update(_render_label(bucket.label, color), layout=False)
                    w.set_class(active, "-active")
                    w.can_focus = active
            else:
                btn = self._build_tab(i, bucket)
                container.mount(btn)
        # Drop excess (async removal — IDs free up next frame).
        for w in existing[len(buckets) :]:
            w.remove()
        self._last_tabs = new_last_tabs
