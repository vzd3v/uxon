"""FleetStatusBar — compact, collapsible per-host status below the table.

Two states, default collapsed:

* **Collapsed** — one line: ``N hosts · M sess`` + capped alert tokens
  + a right-aligned ``h · hosts`` expand affordance. Goes to a warning
  colour only when there is a real alert (``unreachable`` or memory
  pressure) — never for ``pending…`` / ``(cached)`` (see
  :func:`uxon.tui.dashboard.buckets.select_fleet_summary`).
* **Expanded** — one compact line per host (full detail; the only place
  ``up`` lives), reusing :func:`host_status_bar._render`.

State (collapsed vs expanded) is owned by the screen
(``app.main_ui.hosts_expanded``) so it survives the ``apply_loaded_ctx``
recompose; the widget is told which state to render via
:meth:`update_fleet`. Toggling is via the screen's ``h`` binding or a
click on the bar (which posts :class:`FleetStatusBar.Toggled`). The bar
is deliberately **not focusable**: keeping it out of the focus chain
preserves "↑ from the top action buttons wraps to the session list"
(the table, not a status bar, is the star) and keeps server data out of
the way — the whole point of the redesign.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from ..dashboard.buckets import FleetSummary, HostStatusLine
from .host_status_bar import _render

# Max alert tokens rendered inline before collapsing the rest to "+N more".
_MAX_ALERTS = 2


def format_collapsed(summary: FleetSummary, *, max_alerts: int = _MAX_ALERTS) -> str:
    """One-line collapsed text: counts + up to ``max_alerts`` ⚠ tokens.

    Pure (no widget contact) so it is unit-testable. Excess alerts beyond
    the cap collapse to ``+N more`` rather than wrapping the line.
    """
    base = f"{summary.host_count} hosts · {summary.session_count} sess"
    if not summary.alerts:
        return base
    shown = summary.alerts[:max_alerts]
    tokens = " · ".join(f"⚠ {a}" for a in shown)
    extra = len(summary.alerts) - len(shown)
    if extra > 0:
        tokens += f" · +{extra} more"
    return f"{base} · {tokens}"


class FleetStatusBar(Widget):
    """Collapsible fleet status bar mounted below the session table."""

    DEFAULT_CSS = """
    FleetStatusBar {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    FleetStatusBar #fleet-collapsed {
        height: 1;
    }
    FleetStatusBar #fleet-counts {
        width: 1fr;
    }
    FleetStatusBar #fleet-counts.-alert {
        color: $warning;
        text-style: bold;
    }
    FleetStatusBar #fleet-affordance {
        width: auto;
        color: $text-muted;
    }
    FleetStatusBar #fleet-expanded {
        height: auto;
    }
    FleetStatusBar #fleet-expanded > Static {
        height: auto;
        color: $text-muted;
    }
    """

    class Toggled(Message):
        """Posted when the operator clicks the bar to toggle it."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._summary: FleetSummary | None = None
        self._lines: tuple[HostStatusLine, ...] = ()
        self._expanded = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="fleet-collapsed"):
            yield Static("", id="fleet-counts")
            yield Static("h · hosts", id="fleet-affordance")
        yield Vertical(id="fleet-expanded")

    def on_click(self) -> None:
        # Clicking the bar (incl. the "h · hosts" affordance) toggles it.
        # Keyboard toggling is the screen's ``h`` binding.
        self.post_message(self.Toggled())

    def update_fleet(
        self,
        summary: FleetSummary,
        lines: tuple[HostStatusLine, ...],
        *,
        expanded: bool,
    ) -> None:
        """Re-render for the given summary / per-host lines / state."""
        self._summary = summary
        self._lines = lines
        self._expanded = expanded
        try:
            collapsed_row = self.query_one("#fleet-collapsed")
            expanded_box = self.query_one("#fleet-expanded", Vertical)
            counts = self.query_one("#fleet-counts", Static)
        except Exception:  # pragma: no cover — not mounted yet
            return
        collapsed_row.display = not expanded
        expanded_box.display = expanded
        if expanded:
            self._render_expanded(expanded_box)
        else:
            counts.update(format_collapsed(summary))
            counts.set_class(bool(summary.alerts), "-alert")

    def _render_expanded(self, box: Vertical) -> None:
        existing = list(box.children)
        for w in existing[len(self._lines) :]:
            w.remove()
        for i, line in enumerate(self._lines):
            text = _render(line)
            if i < len(existing):
                existing[i].update(text)  # type: ignore[attr-defined]
            else:
                box.mount(Static(text, id=f"fleet-line-{i}"))
