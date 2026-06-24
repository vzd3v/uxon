"""Wire schema for ``uxon`` machine-readable output.

Defines the JSON shape that ``uxon list/doctor/version/kill/kill-all
--json`` emits and that the multi-host remote collector consumes. The
schema is the *public contract* between an uxon CLI on one host and
any consumer of its JSON output (a TUI on a different host, a
monitoring scraper, a future REST surface).

Two things live here:

- Constants and TypedDicts that describe the wire format. They are
  the source of truth — both the producer (``--json`` rendering in
  ``app.listing``) and the consumer (the SSH collector in
  ``infra.remote.collector``) import from this module.
- :func:`build_session_records`, a pure function that turns
  :class:`uxon.domain.session.SessionInfo` instances into wire records.
  Pure means: no subprocess, no I/O, no formatting decisions beyond what
  ``SessionInfo`` already carries. Output is JSON-serialisable as-is.

Why a separate module: keeping the *shaping* step pure lets the same
function feed both the human table (``app.listing.print_list``) and the
JSON envelope, and lets a remote consumer parse a JSON snapshot without
importing any of the tmux-poking probe machinery.

Versioning: :data:`WIRE_SCHEMA_VERSION` is bumped on **incompatible**
changes (renamed/removed fields, semantic changes). Adding a new
optional field is backward-compatible — older consumers ignore it. A
remote collector that sees an unknown ``schema_version`` should refuse
to parse rather than guess.

List-data optional fields (forward-compatible additions, no version bump):

- ``data.scope_skipped`` (``list[str]``): users in ``session_users``
  that the producer probed for sudo reachability and could *not* reach
  via ``sudo -niu <U>``. ``data.scope_users`` is the *reachable*
  subset; the union ``scope_users ∪ scope_skipped`` is the set of
  candidates actually probed. Users in ``session_users`` that were
  never asked (e.g. the caller themselves, filtered before probing)
  appear in neither field. Older peers that don't emit the key are
  treated as ``[]`` by the aggregator — no schema bump.

Top-level optional fields (also forward-compatible, no version bump):

- ``host_stats`` (``HostStats``): host-level metrics snapshot
  (CPU/RAM/loadavg/uptime/kernel) sampled by the producer at envelope
  build time. Older peers omit the key; the consumer treats absence as
  ``None`` and renders the host status bar without metrics. Adding new
  keys inside ``host_stats`` is also additive — consumers ``.get(...)``
  defensively.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, Protocol, TypedDict

WIRE_SCHEMA_VERSION = "2"
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
    - ``profile``: launch profile id from the verified launch record, or the
      session-name suffix display fallback for unmanaged sessions.
    - ``agent``: underlying agent id from the verified launch record, or ``""``
      when the session is unmanaged.
    - ``container_profile`` / ``container``: verified launch-record container
      profile id and resolved container name, or ``""`` for host-only and
      unmanaged sessions.
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
    profile: str
    agent: str
    container_profile: str
    container: str
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
    container_down: bool
    legacy: bool


class HostStats(TypedDict, total=False):
    """Snapshot of host-level metrics returned alongside session list.

    Forward-compatible: peers running schema 1 omit this field; the
    parser treats absence as ``None``. Adding a new key here is also
    additive — peers running an older binary omit unknown keys; the
    consumer should ``.get(...)`` defensively.
    """

    cpu_pct: float  # /proc/stat delta over ~50 ms
    mem_used_kib: int  # MemTotal - MemAvailable
    mem_total_kib: int  # MemTotal
    loadavg_1m: float  # /proc/loadavg field 0
    uptime_s: int  # /proc/uptime field 0
    kernel: str  # uname -r


RemoteSessionPayload = dict[str, Any]
"""One peer-emitted session record, as it lands at the local collector.

Shape mirrors :class:`SessionRecord` (the producer-side total TypedDict
written by :func:`build_session_records`), but
:func:`uxon.infra.remote.envelope.parse_envelope` validates the envelope
only and treats individual records as opaque. Forward-compatibility
with peers running a newer or older uxon is the explicit goal: a peer
that adds, renames, or omits a per-session field must not blank a
whole snapshot. Consumers therefore read fields defensively
(``rec.get(...)``) and cannot assume any field is present even after a
successful fetch — hence the deliberately permissive type.
"""


@dataclass(frozen=True)
class RemoteSnapshot:
    """Result of one fetch attempt against one ``RemoteHost``.

    Pure data: the in-memory representation of a peer's session list as
    it lands at the local collector. Lives in the wire-schema module
    because it mirrors the wire DTOs (it is the decoded, consumer-side
    counterpart of an ``uxon list --json`` envelope) and is the
    widest-shared remote type — the collector, the TUI state, and the
    demo loader all traffic in it.

    Attributes:
        host_name: Mirrors ``RemoteHost.name`` — preserved here so
            consumers that store snapshots without the matching host
            object can still attribute them.
        fetched_at_epoch: ``time.time()`` at the moment the fetch
            attempt finished (success or failure). Used by the TUI to
            display "stale 12s ago" indicators.
        from_cache: ``True`` when the sessions list came from the
            on-disk cache rather than a fresh fetch. In a snapshot
            returned by ``fetch_remote_snapshot`` this implies
            :attr:`error` is non-None (the live fetch failed and we
            fell back). A snapshot returned by ``read_cached_snapshot``
            in isolation also sets ``from_cache=True`` but leaves
            ``error=None`` — the cache file alone has no opinion on
            whether the peer is currently reachable.
        error: Short, human-readable error string, or ``None`` on a
            successful fetch. The collector never raises — every
            failure surfaces here.
        sessions: List of peer-emitted session records
            (:data:`RemoteSessionPayload`). Each record's shape mirrors
            :class:`SessionRecord` but is unvalidated at decode time —
            consumers must read fields defensively. Empty when the
            fetch failed AND no cache was available.
        cached_at_epoch: Wall-clock time at which the underlying
            payload was originally collected. For a fresh fetch this
            equals :attr:`fetched_at_epoch`; for a cache fallback it
            is older.
        scope_limited: ``True`` when the peer rejected
            ``list --all-users`` (because its
            ``enable_all_users_list = false``) and the collector fell
            back to own-only ``list --json``. The TUI
            badges the section header with ``(own only)`` so the
            operator knows the per-peer view is partial. Default
            ``False`` — fresh peers serve the all-users view.
        scope_skipped: Users the peer probed for sudo reachability
            and could not reach. Forward-compatible: missing on older
            peers that don't emit the field — the collector treats
            that as ``[]``.
        host_stats: Optional snapshot of host-level metrics
            (CPU/RAM/loadavg/uptime/kernel) emitted by the peer
            alongside the session list. Forward-compatible: missing
            on peers that don't emit the field — the collector treats
            that as ``None`` and the host status bar renders without
            metrics.
    """

    host_name: str
    fetched_at_epoch: float
    from_cache: bool
    error: str | None
    sessions: list[RemoteSessionPayload] = field(default_factory=list)
    cached_at_epoch: float | None = None
    scope_limited: bool = False
    scope_skipped: list[str] = field(default_factory=list)
    host_stats: dict[str, Any] | None = None


class _SessionLike(Protocol):
    """Structural type for what :func:`build_session_records` reads.

    Decoupled from :class:`uxon.domain.session.SessionInfo` to keep this
    module importable without a cycle. ``SessionInfo`` satisfies this
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
    profile: str
    container_profile: str
    container: str
    container_down: bool
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
                profile=s.profile,
                agent=s.agent,
                container_profile=s.container_profile,
                container=s.container,
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
                container_down=s.container_down,
                legacy=s.legacy,
            )
        )
    return records


EnvelopeKind = Literal["list", "doctor", "version", "kill", "kill-all"]


class Envelope(TypedDict):
    """Top-level wrapper for every ``--json`` payload.

    Carries the schema version, the producing uxon's own version, and
    the kind discriminator that tells a consumer which ``data`` shape
    to expect. ``host`` is added by the multi-host RemoteCollector to
    attribute a snapshot to its source host; local ``--json`` output
    omits it. ``host_stats`` is the additive forward-compatible
    per-host metrics block (see module docstring) populated only for
    ``kind == "list"``.
    """

    schema_version: str
    uxon_version: str
    kind: EnvelopeKind
    data: dict[str, Any]
    host: NotRequired[str]
    host_stats: NotRequired[HostStats]


def make_envelope(
    kind: EnvelopeKind,
    data: dict[str, Any],
    *,
    uxon_version: str,
    host: str | None = None,
    host_stats: HostStats | None = None,
) -> Envelope:
    """Construct a versioned envelope for one ``--json`` payload.

    ``kind`` discriminates the data shape; ``data`` is the kind-specific
    body. ``uxon_version`` is supplied by the caller (``cli.read_repo_version``)
    so this module stays free of cli imports. ``host`` is added only
    when given — local invocations leave it absent. ``host_stats`` is
    the additive forward-compatible per-host metrics block populated
    only for ``kind == "list"``; producers that can't read ``/proc``
    pass ``None`` and the field is left absent (consumers ``.get(...)``
    on it).
    """
    env: Envelope = {
        "schema_version": WIRE_SCHEMA_VERSION,
        "uxon_version": uxon_version,
        "kind": kind,
        "data": data,
    }
    if host is not None:
        env["host"] = host
    if host_stats is not None:
        env["host_stats"] = host_stats
    return env
