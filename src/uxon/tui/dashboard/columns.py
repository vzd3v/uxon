"""Dashboard column registry: ColumnSpec + REGISTRY + formatters.

Each column ships its own ``format`` (row → cell value) and
``sort_key`` (row → comparable). Formatters are pure functions that
preserve the visual semantics of the legacy ``SessionTable`` /
``RemoteSessionTable``: bold-green for attached, red/yellow CPU
thresholds at >50 / >10, yellow user marker, deterministic per-host
colour glyph on the NAME column so per-row attribution survives sort
even with the HOST column hidden.

These callables are invoked many times per tick by the reconciler;
they MUST stay closure-free over mutable state.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from rich.text import Text

from .row import SessionRow


@dataclass(frozen=True)
class ColumnSpec:
    """One column entry in the dashboard registry.

    ``align`` and ``default_visible`` are layout hints consumed by the
    widget and the layout selector respectively. ``show_when`` gates a
    column on the runtime layout flags (``multi_host`` /
    ``cross_user``); see :mod:`uxon.tui.dashboard.layout`.
    """

    id: str
    label: str
    format: Callable[[SessionRow], Any]
    sort_key: Callable[[SessionRow], Any]
    align: Literal["left", "right"] = "left"
    default_visible: bool = True
    show_when: Literal["always", "multi_host", "cross_user"] = "always"


# Stable palette of Rich style specs the host glyph cycles through.
# Order is deterministic and chosen for visual distinguishability on
# both light and dark terminal themes.
_HOST_PALETTE: tuple[str, ...] = (
    "cyan",
    "magenta",
    "yellow",
    "green",
    "blue",
    "red",
    "bright_cyan",
    "bright_magenta",
    "bright_yellow",
    "bright_green",
)


def host_colour(host_name: str) -> str:
    """Return a deterministic Rich style spec for ``host_name``.

    Stable across calls and across processes — uses md5 of the host
    name modulo the palette size. Two distinct names usually map to
    distinct entries; with the current 10-entry palette, collisions
    are inevitable past ~10 hosts but survive sort and remain
    distinguishable per-row via the HOST column itself.
    """
    digest = hashlib.md5(host_name.encode("utf-8")).hexdigest()
    return _HOST_PALETTE[int(digest, 16) % len(_HOST_PALETTE)]


def format_cpu(row: SessionRow) -> Text:
    """Format CPU% with the existing >50/>10 colour thresholds.

    Legacy ``SessionTable._cpu_cell`` rendered ``"0.0"`` for an idle
    session — only a missing input string blanked the cell. The unified
    pipeline has already collapsed the missing/zero distinction at the
    adapter boundary (``from_tui_session``), so we always render the
    numeric value; an idle row shows as ``"0.0"`` to match legacy.
    """
    raw = f"{row.cpu_pct:.1f}" if row.cpu_pct < 100 else f"{row.cpu_pct:.0f}"
    if row.cpu_pct > 50:
        return Text(raw, style="bold red")
    if row.cpu_pct > 10:
        return Text(raw, style="yellow")
    return Text(raw)


def format_ram(row: SessionRow) -> str:
    """Format ``rss_kib`` to the same compact unit shape ``cli`` uses."""
    rss_kib = row.rss_kib
    if rss_kib <= 0:
        return "-"
    if rss_kib < 1024:
        return f"{rss_kib}K"
    mib = rss_kib / 1024
    if mib < 1024:
        return f"{mib:.0f}M"
    gib = mib / 1024
    return f"{gib:.1f}G"


def format_relative_time(epoch: float | None, now: float | None = None) -> str:
    """Format an epoch-seconds value as a compact relative string.

    ``None`` → ``"-"``. Otherwise: ``<60s → "{n}s"``,
    ``<3600s → "{n}m"``, ``<86400s → "{n}h"``, else ``"{n}d"``.
    Tests pass an explicit ``now`` for determinism; production
    callers pass ``None`` and the helper reads ``time.time()``.
    """
    if epoch is None:
        return "-"
    if now is None:
        now = time.time()
    delta = max(0.0, now - epoch)
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


def _format_host(row: SessionRow) -> Text:
    if row.host is None:
        return Text("local", style="dim")
    return Text(row.host, style=host_colour(row.host))


def _format_user(row: SessionRow) -> Text:
    # Render plain: in cross_user mode the column header itself flags
    # multi-user state; per-row colour would also paint the operator's
    # own user yellow which diverges from the legacy intent (yellow
    # was a non-self marker on the legacy split table; in the unified
    # table the column header itself is the multi-user signal).
    return Text(row.user or "-")


def _format_name(row: SessionRow) -> Text:
    """Render the NAME cell with a left-edge host-coloured glyph.

    The glyph survives sort even when the HOST column is hidden, so
    operators retain per-row host attribution in single-list view.
    """
    glyph_style = host_colour(row.host) if row.host is not None else "dim"
    text = Text("● ", style=glyph_style)
    display = row.short or row.name or "-"
    if row.attached:
        text.append(display, style="bold green")
        text.append(" ●", style="green")
    else:
        text.append(display)
    return text


def _format_agent(row: SessionRow) -> str:
    if row.legacy and row.agent == "claude":
        return "claude (legacy)"
    return row.agent or "-"


def _format_pid(row: SessionRow) -> str:
    return str(row.pid) if row.pid is not None else "-"


def _format_cmd(row: SessionRow) -> str:
    return row.cmd or "-"


def _format_path(row: SessionRow) -> str:
    return row.path or "-"


def _format_new(row: SessionRow) -> str:
    return format_relative_time(row.created_epoch)


def _format_last(row: SessionRow) -> str:
    return format_relative_time(row.last_attached_epoch)


# WINS placeholder: the wire schema carries ``windows`` but
# :class:`SessionRow` does not yet expose it (would require widening
# the row type and both adapters). Ship the column entry for
# forward-compat — operators can opt into it via TOML — but render
# ``"-"`` until the row gains the field in a follow-up plan.
def _format_wins(_row: SessionRow) -> str:
    return "-"


def _sort_host(row: SessionRow) -> tuple[int, str]:
    # Local rows (host=None) sort before any remote host.
    return (0, "") if row.host is None else (1, row.host)


def _sort_user(row: SessionRow) -> str:
    return row.user


def _sort_name(row: SessionRow) -> str:
    return row.short or row.name


def _sort_agent(row: SessionRow) -> str:
    return row.agent


def _sort_cpu(row: SessionRow) -> float:
    return row.cpu_pct


def _sort_ram(row: SessionRow) -> int:
    return row.rss_kib


def _sort_pid(row: SessionRow) -> int:
    return row.pid if row.pid is not None else -1


def _sort_cmd(row: SessionRow) -> str:
    return row.cmd


def _sort_path(row: SessionRow) -> str:
    return row.path


def _sort_new(row: SessionRow) -> float:
    # Newer sessions have a *larger* created_epoch. Sort desc puts
    # newest first, asc puts oldest first.
    return row.created_epoch if row.created_epoch is not None else float("-inf")


def _sort_last(row: SessionRow) -> float:
    return row.last_attached_epoch if row.last_attached_epoch is not None else float("-inf")


def _sort_wins(_row: SessionRow) -> int:
    return 0


REGISTRY: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        id="host",
        label="HOST",
        format=_format_host,
        sort_key=_sort_host,
        default_visible=False,
        show_when="multi_host",
    ),
    ColumnSpec(
        id="user",
        label="USER",
        format=_format_user,
        sort_key=_sort_user,
        default_visible=False,
        show_when="cross_user",
    ),
    ColumnSpec(id="name", label="NAME", format=_format_name, sort_key=_sort_name),
    ColumnSpec(id="agent", label="AGENT", format=_format_agent, sort_key=_sort_agent),
    ColumnSpec(id="cpu", label="CPU", format=format_cpu, sort_key=_sort_cpu, align="right"),
    ColumnSpec(id="ram", label="RAM", format=format_ram, sort_key=_sort_ram, align="right"),
    ColumnSpec(id="new", label="NEW", format=_format_new, sort_key=_sort_new, align="right"),
    ColumnSpec(id="last", label="LAST", format=_format_last, sort_key=_sort_last, align="right"),
    ColumnSpec(id="cmd", label="CMD", format=_format_cmd, sort_key=_sort_cmd),
    ColumnSpec(id="path", label="PATH", format=_format_path, sort_key=_sort_path),
    ColumnSpec(
        id="pid",
        label="PID",
        format=_format_pid,
        sort_key=_sort_pid,
        align="right",
        default_visible=False,
    ),
    ColumnSpec(
        id="wins",
        label="WINS",
        format=_format_wins,
        sort_key=_sort_wins,
        align="right",
        default_visible=False,
    ),
)
