"""HostStatusBar — renders one or many HostStatusLine entries.

Modes:
- ``compact``: single-line render of one bucket (used under the
  active tab in by_host view).
- ``expanded``: one line per bucket, vertical layout (used above
  the table in flat view).

The widget is presentational — it does no aggregation. Owners pass
in a freshly-computed tuple of ``HostStatusLine`` and call
:meth:`update_lines`.
"""

from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from ..dashboard.buckets import HostStatusLine


def _format_uptime(seconds: int | None) -> str:
    if seconds is None or seconds <= 0:
        return "—"
    days = seconds // 86_400
    hours = (seconds % 86_400) // 3600
    if days:
        return f"{days}d{hours}h"
    minutes = (seconds % 3600) // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _format_mem(used: int, total: int) -> str:
    if total <= 0:
        return "—/—"
    return f"{used // 1024} MiB / {total // 1024} MiB"


def _render(line: HostStatusLine) -> str:
    state = f" · {line.state}" if line.state else ""
    la = f" · la {line.loadavg_1m:.2f}" if line.loadavg_1m is not None else ""
    return (
        f"{line.label}  {line.session_count} sess · {line.attached_count} attached · "
        f"cpu Σ{line.cpu_pct_sum:.0f}% · mem {_format_mem(line.mem_used_kib, line.mem_total_kib)}"
        f"{la} · up {_format_uptime(line.uptime_s)}{state}"
    )


class HostStatusBar(Widget):
    """One- or many-line per-host status renderer."""

    DEFAULT_CSS = """
    HostStatusBar {
        height: auto;
        padding: 0 1;
    }
    HostStatusBar > Static {
        color: $text-muted;
    }
    """

    def __init__(self, *, mode: Literal["compact", "expanded"], id: str | None = None) -> None:
        super().__init__(id=id)
        self._mode = mode
        self._lines: tuple[HostStatusLine, ...] = ()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("", id=f"{self.id or 'hsb'}-line-0")

    def update_lines(self, lines: tuple[HostStatusLine, ...]) -> None:
        self._lines = lines
        try:
            container = self.query_one(Vertical)
        except Exception:
            return
        # Mount/dismount Static rows to match line count. Compact
        # mode shows just lines[0]; expanded shows all.
        target = lines[:1] if self._mode == "compact" else lines
        existing = list(container.children)
        # Drop excess.
        for w in existing[len(target) :]:
            w.remove()
        # Update / add.
        for i, line in enumerate(target):
            text = _render(line)
            if i < len(existing):
                existing[i].update(text)  # type: ignore[attr-defined]
            else:
                container.mount(Static(text, id=f"{self.id or 'hsb'}-line-{i}"))
