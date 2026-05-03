"""Wire schema for ``uxon`` machine-readable output.

Defines the JSON shape that ``uxon list/doctor/version/kill/kill-all
--json`` emits and that the multi-host remote collector consumes. The
schema is the *public contract* between an uxon CLI on one host and
any consumer of its JSON output (a TUI on a different host, a
monitoring scraper, a future REST surface).

Two things live here:

- Constants and TypedDicts that describe the wire format. They are
  the source of truth — both the producer (``--json`` rendering in
  ``cli.py``, added in the next commit) and the consumer (the SSH
  ``RemoteCollector``, added later) import from this module.
- :func:`build_session_records`, a pure function that turns
  :class:`uxon.cli.SessionInfo` instances into wire records. Pure
  means: no subprocess, no I/O, no formatting decisions beyond what
  ``SessionInfo`` already carries. Output is JSON-serialisable as-is.

Why a separate module: ``cli.py`` already does data collection, data
shaping and presentation in one pass (see ``print_list``). Splitting
the *shaping* step out lets the same function feed both the human
table and the JSON envelope, and lets a remote consumer parse a JSON
snapshot without importing any of cli.py's tmux-poking machinery.

Versioning: :data:`WIRE_SCHEMA_VERSION` is bumped on **incompatible**
changes (renamed/removed fields, semantic changes). Adding a new
optional field is backward-compatible — older consumers ignore it. A
remote collector that sees an unknown ``schema_version`` should refuse
to parse rather than guess.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol, TypedDict

WIRE_SCHEMA_VERSION = "1"
"""Current wire-schema version. Bump on incompatible changes only."""


class SessionRecord(TypedDict):
    """One tmux session, in wire format.

    All fields are JSON-friendly: no tuples, no ``None`` where a
    sensible empty value exists, no datetime objects.

    Field semantics:

    - ``user``: OS user that owns the tmux socket the session lives
      on.
    - ``name``: full tmux session name, including the configured
      ``session_prefix`` (or a legacy prefix for ``legacy=True``
      records).
    - ``short_id``: ``name`` with the *current* ``session_prefix``
      stripped, mirroring the human table. For ``legacy=True``
      records the current prefix does not match, so ``short_id``
      equals ``name``.
    - ``agent``: ``"claude"|"codex"|"cursor"|"unknown"`` — what the
      session-name parser recognised the agent slot as.
    - ``attached``: ``True`` iff a tmux client is currently attached.
    - ``windows``: tmux window count, as reported by
      ``#{session_windows}``. Kept as a string because tmux emits it
      as one and the consumer rarely treats it numerically.
    - ``created`` / ``last_attached``: ISO 8601 UTC strings (e.g.
      ``"2026-05-03T12:34:56+00:00"``) or ``""`` if unparseable /
      never-attached. Already in this form on ``SessionInfo``; the
      wire schema preserves it.
    - ``pane_pids``: list of pane PIDs, in tmux order.
    - ``active_pid``: PID of the active pane's process, or ``None``
      when the active pane has no resolvable PID.
    - ``active_cmd`` / ``active_path``: command name and CWD of the
      active pane, ``""`` when unknown.
    - ``cpu_pct`` / ``rss_kib``: aggregated across the session's
      pane PIDs (see ``enrich_session_usage``). ``0.0`` / ``0`` when
      no usable PID was found.
    - ``legacy``: ``True`` iff ``name`` matched a legacy prefix
      rather than the current ``session_prefix``.
    """

    user: str
    name: str
    short_id: str
    agent: str
    attached: bool
    windows: str
    created: str
    last_attached: str
    pane_pids: list[int]
    active_pid: int | None
    active_cmd: str
    active_path: str
    cpu_pct: float
    rss_kib: int
    legacy: bool


class _SessionLike(Protocol):
    """Structural type for what :func:`build_session_records` reads.

    Decoupled from :class:`uxon.cli.SessionInfo` to keep this module
    importable without a cycle. ``cli.SessionInfo`` satisfies this
    protocol by attribute name and type.
    """

    user: str
    name: str
    attached: str
    windows: str
    created: str
    last_attached: str
    pane_pids: tuple[int, ...]
    active_pid: int | None
    active_cmd: str
    active_path: str
    cpu_pct: float
    rss_kib: int
    agent: str
    legacy: bool


def build_session_records(
    sessions: Sequence[_SessionLike],
    *,
    session_prefix: str,
) -> list[SessionRecord]:
    """Translate a list of ``SessionInfo``-like objects into wire
    records.

    Pure: no I/O, no exception paths beyond what attribute access
    triggers. Field-for-field; the only transformations are:

    - ``attached``: ``"1"`` → ``True``, anything else → ``False``
      (tmux emits ``"1"``/``"0"`` strings).
    - ``short_id``: prefix stripped if (and only if) ``name`` starts
      with the *current* ``session_prefix``. Legacy-prefix sessions
      keep the full name; that mirrors how the human ``print_list``
      table identifies them.
    - ``pane_pids``: tuple → list, so the result is JSON-serialisable.

    Order is preserved (input list order = output list order). No
    deduping; if the input contains duplicates, the output does too.
    """
    records: list[SessionRecord] = []
    for s in sessions:
        if s.name.startswith(session_prefix):
            short_id = s.name[len(session_prefix) :]
        else:
            short_id = s.name
        records.append(
            SessionRecord(
                user=s.user,
                name=s.name,
                short_id=short_id,
                agent=s.agent,
                attached=(s.attached == "1"),
                windows=s.windows,
                created=s.created,
                last_attached=s.last_attached,
                pane_pids=list(s.pane_pids),
                active_pid=s.active_pid,
                active_cmd=s.active_cmd,
                active_path=s.active_path,
                cpu_pct=s.cpu_pct,
                rss_kib=s.rss_kib,
                legacy=s.legacy,
            )
        )
    return records


EnvelopeKind = Literal["list", "doctor", "version", "kill", "kill-all"]


class Envelope(TypedDict):
    """Top-level wrapper for every ``--json`` payload.

    Carries the schema version, the producing uxon's own version, and
    the kind discriminator that tells a consumer which ``data`` shape
    to expect. ``host`` is an optional top-level field added later by
    the multi-host RemoteCollector to attribute a snapshot to its
    source host; local ``--json`` output omits it.
    """

    schema_version: str
    uxon_version: str
    kind: EnvelopeKind
    data: dict[str, Any]


def make_envelope(
    kind: EnvelopeKind,
    data: dict[str, Any],
    *,
    uxon_version: str,
    host: str | None = None,
) -> Envelope:
    """Construct a versioned envelope for one ``--json`` payload.

    ``kind`` discriminates the data shape; ``data`` is the kind-specific
    body. ``uxon_version`` is supplied by the caller (``cli.read_repo_version``)
    so this module stays free of cli imports. ``host`` is added only
    when given — local invocations leave it absent.
    """
    env: Envelope = {
        "schema_version": WIRE_SCHEMA_VERSION,
        "uxon_version": uxon_version,
        "kind": kind,
        "data": data,
    }
    if host is not None:
        env["host"] = host  # type: ignore[typeddict-unknown-key]
    return env
