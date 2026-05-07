"""SessionRow — unified row type for local and remote sessions.

Every later layer (column formatters, selector, reconciler, widget)
types against :class:`SessionRow`. The two adapters
:func:`from_tui_session` and :func:`from_wire_record` are the *only*
boundaries where source-specific shapes meet the dashboard pipeline:

* ``from_tui_session(s)`` — local sessions. Always sets ``host=None``.
* ``from_wire_record(host, rec)`` — peer-emitted records. Always sets
  ``host=host`` (never ``None``). Reads fields defensively because
  peers may run an older wire schema.

``host is None`` is therefore the local-vs-remote routing invariant
that downstream code (action dispatch in particular) keys off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..context import TuiSession


@dataclass(frozen=True, slots=True)
class SessionRow:
    """Single row in the unified dashboard model.

    All fields are sortable / formattable without further parsing. ISO
    timestamps are pre-parsed to epoch seconds so per-tick sort_keys do
    not re-parse strings.
    """

    host: str | None
    user: str
    name: str
    short: str
    agent: str
    attached: bool
    legacy: bool
    pid: int | None
    cpu_pct: float
    rss_kib: int
    created_epoch: float | None
    last_attached_epoch: float | None
    cmd: str
    path: str


def _parse_pid(raw: str) -> int | None:
    """Best-effort int parse. Empty / ``"-"`` / non-numeric → ``None``."""
    if not raw or raw == "-":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_cpu(raw: str) -> float:
    """Parse formatted CPU back to float. Empty / ``"-"`` → ``0.0``."""
    if not raw or raw == "-":
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


# Recognised RAM units in the formatter at ``cli.format_rss_kib``: the
# string is one of ``"-"`` (zero), ``"{n}K"``, ``"{n}M"``, ``"{n:.1f}G"``.
# Older builds may emit ``"MiB"``/``"GiB"`` so we accept both.
_RAM_UNIT_KIB = {
    "K": 1,
    "KIB": 1,
    "M": 1024,
    "MIB": 1024,
    "G": 1024 * 1024,
    "GIB": 1024 * 1024,
    "T": 1024 * 1024 * 1024,
    "TIB": 1024 * 1024 * 1024,
}


def _parse_ram_to_kib(raw: str) -> int:
    """Parse RAM display string back to KiB. Empty / ``"-"`` → ``0``.

    Accepts both ``"456M"``/``"1.2G"`` (current local format) and
    ``"456 MiB"``/``"1.2 GiB"`` (older display format).
    """
    if not raw or raw == "-":
        return 0
    s = raw.strip()
    # Split into number + unit, ignoring whitespace between.
    i = 0
    while i < len(s) and (s[i].isdigit() or s[i] in ".-"):
        i += 1
    num_part = s[:i].strip()
    unit_part = s[i:].strip().upper()
    if not num_part:
        return 0
    try:
        value = float(num_part)
    except ValueError:
        return 0
    factor = _RAM_UNIT_KIB.get(unit_part, 0)
    if factor == 0:
        return 0
    return int(value * factor)


def _parse_iso_to_epoch(raw: str) -> float | None:
    """Parse ISO 8601 string to epoch seconds. Empty / unparseable → ``None``."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (ValueError, TypeError):
        return None


def from_tui_session(s: TuiSession) -> SessionRow:
    """Adapt a local :class:`TuiSession` to a :class:`SessionRow`.

    ``host`` is always ``None`` — this is the local-row marker that
    later action routing branches on. ``created_epoch`` /
    ``last_attached_epoch`` are parsed from the raw ISO strings the
    CLI threads through alongside the pre-formatted display strings,
    so local rows rank correctly when the dashboard sorts by ``new``
    or ``last``. Empty ISO strings (the wire-schema "missing"
    sentinel) collapse to ``None``.
    """
    return SessionRow(
        host=None,
        user=s.user,
        name=s.name,
        short=s.short,
        agent=s.agent,
        attached=s.attached,
        legacy=s.legacy,
        pid=_parse_pid(s.pid),
        cpu_pct=_parse_cpu(s.cpu),
        rss_kib=_parse_ram_to_kib(s.ram),
        created_epoch=_parse_iso_to_epoch(s.created_iso) if s.created_iso else None,
        last_attached_epoch=(
            _parse_iso_to_epoch(s.last_attached_iso) if s.last_attached_iso else None
        ),
        cmd=s.cmd,
        path=s.path,
    )


def from_wire_record(host: str, rec: dict[str, Any]) -> SessionRow:
    """Adapt a peer-emitted wire record to a :class:`SessionRow`.

    ``host`` is always set to the supplied peer name — never ``None`` —
    so action routing can dispatch the row to its origin. Fields are
    read defensively (``rec.get(...)``) because the peer may run an
    older wire schema and a missing field must not raise.
    """
    name = str(rec.get("name", "") or "")
    short = str(rec.get("short_id", "") or "") or name
    return SessionRow(
        host=host,
        user=str(rec.get("user", "") or ""),
        name=name,
        short=short,
        agent=str(rec.get("agent", "") or ""),
        attached=bool(rec.get("attached", False)),
        legacy=bool(rec.get("legacy", False)),
        pid=rec.get("active_pid") if isinstance(rec.get("active_pid"), int) else None,
        cpu_pct=float(rec.get("cpu_pct", 0.0) or 0.0),
        rss_kib=int(rec.get("rss_kib", 0) or 0),
        created_epoch=_parse_iso_to_epoch(str(rec.get("created", "") or "")),
        last_attached_epoch=_parse_iso_to_epoch(str(rec.get("last_attached", "") or "")),
        cmd=str(rec.get("active_cmd", "") or ""),
        path=str(rec.get("active_path", "") or ""),
    )
