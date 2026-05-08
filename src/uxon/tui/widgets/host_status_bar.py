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


def _format_mem(used_kib: int, total_kib: int) -> str:
    """Render ``used / total`` in GiB with one decimal — '6.3/16G'.

    Returns ``—`` when total is missing (e.g., cache from an older
    peer that didn't ship host_stats).
    """
    if total_kib <= 0:
        return "—"
    used_gib = used_kib / 1024 / 1024
    total_gib = total_kib / 1024 / 1024
    return f"{used_gib:.1f}/{total_gib:.0f}G"


def _render(line: HostStatusLine) -> str:
    """One compact host-status line, dot-separated.

    Layout: ``label · N/M · cpu X% · mem U/TG · la X.XX · up Xd[Xh] · state``.
    Sessions are written ``total/attached`` to fold two numbers into
    one column. CPU is the per-host sum with no decoration (the
    aggregate is implicit from context). Empty-data fields collapse
    to ``—`` rather than the whole segment vanishing — column count
    stays stable across hosts.
    """
    parts = [
        line.label,
        f"{line.session_count}/{line.attached_count} sess",
        f"cpu {line.cpu_pct_sum:.0f}%",
        f"mem {_format_mem(line.mem_used_kib, line.mem_total_kib)}",
    ]
    if line.loadavg_1m is not None:
        parts.append(f"la {line.loadavg_1m:.2f}")
    parts.append(f"up {_format_uptime(line.uptime_s)}")
    if line.state:
        parts.append(line.state)
    return " · ".join(parts)


class HostStatusBar(Widget):
    """One- or many-line per-host status renderer."""

    DEFAULT_CSS = """
    HostStatusBar {
        height: auto;
        padding: 0 1;
    }
    HostStatusBar > Vertical {
        height: auto;
    }
    HostStatusBar > Vertical > Static {
        height: auto;
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
