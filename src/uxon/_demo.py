"""Demo-mode hooks for screenshots and articles.

Off by default. Activated by setting ``UXON_DEMO_HOSTS=<dir>``: a
directory of pre-rendered wire envelopes (one ``<host>.json`` per
peer, conforming to :class:`uxon.wire_schema.Envelope` with
``kind="list"``).

When the env var is set, three data sources are intercepted:

- :func:`synthesize_remote_hosts` returns one synthetic
  :class:`uxon.remote_hosts.RemoteHost` per ``<host>.json`` file
  (files starting with ``_`` are reserved and skipped). ``ssh_alias``
  is set to a ``demo:`` sentinel that must never reach a real ``ssh``
  invocation.
- :func:`load_demo_snapshot` reads the per-peer envelope from disk
  and returns a :class:`uxon.remote_collector.RemoteSnapshot`
  directly, bypassing SSH entirely. Called from
  :func:`uxon.remote_collector.fetch_remote_snapshot` before any
  network I/O.
- :func:`load_demo_local_sessions` reads the optional
  ``_local.json`` envelope and yields :class:`uxon.cli.SessionInfo`
  records for the requested user, bypassing tmux entirely. Called
  from :func:`uxon.cli.collect_sessions_for_user` before any
  subprocess invocation. Absent file ⇒ empty list, which is the
  desired default on a multi-tenant box: screenshots show only the
  demo's pre-rendered peers.

Why a separate module: keeps demo plumbing off the public API surface,
keeps the cost on the production path to a single env-var probe per
source, and gives tests a single seam to patch.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import msgspec

from uxon.remote_collector import RemoteSnapshot
from uxon.remote_hosts import RemoteHost
from uxon.wire_schema import WIRE_SCHEMA_VERSION

DEMO_ENV_VAR = "UXON_DEMO_HOSTS"
"""Env var that activates demo mode. Value is a directory of envelopes."""

DEMO_SSH_ALIAS_PREFIX = "__uxon_demo__:"
"""Sentinel prefix on synthetic ``RemoteHost.ssh_alias`` values.

Chosen to be near-impossible to collide with a real ``Host`` pattern
in ``~/.ssh/config`` — operators occasionally use ``Host demo:foo``
style aliases, and a shorter sentinel like ``demo:`` would shadow
them and trip the unset-env guard in ``fetch_remote_snapshot``.

A real fetch path that ever sees this prefix means the demo hook in
``fetch_remote_snapshot`` was bypassed — the collector treats it as a
hard error rather than silently shelling out to the alias.
"""

LOCAL_ENVELOPE_NAME = "_local.json"
"""Name of the optional synthetic-local envelope inside the demo dir.

Scenarios that want a non-empty local section render this file with
the same wire schema as a per-peer envelope. Absent file ⇒ empty
local section, which is what most screenshot scenarios want on a
multi-tenant box where the caller's tmux server is full of unrelated
real sessions.

Leading-underscore name is reserved: :func:`synthesize_remote_hosts`
skips any ``_*.json`` so this never becomes a synthetic peer.
"""


def demo_hosts_dir() -> Path | None:
    """Return the demo-envelopes directory, or ``None`` when inactive.

    Empty / unset env var → demo mode off. Non-existent directory →
    demo mode off (logged would be nice but this module stays
    print-free; the loader call sites surface the issue indirectly via
    an empty host list).
    """
    raw = os.environ.get(DEMO_ENV_VAR, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_dir():
        return None
    return path


def synthesize_remote_hosts(dir_path: Path) -> list[RemoteHost]:
    """Build one :class:`RemoteHost` per ``<dir>/*.json`` envelope file.

    Files whose name starts with ``_`` are reserved (see
    :data:`LOCAL_ENVELOPE_NAME`) and skipped — they describe the
    synthetic local section, not a peer.

    ``ssh_alias`` carries the :data:`DEMO_SSH_ALIAS_PREFIX` sentinel —
    the collector hook intercepts before any ``ssh`` invocation.

    Order: by the optional top-level envelope field ``demo_order``
    (integer, lower first), then by host name as a tie-break. Lets a
    scenario pin presentation order — important when the article-aligned
    headline wants ``dev-common`` (the *exception*) at the bottom even
    though alphabetic sort would float it to the top.

    Optional ``color`` is read from the top-level envelope field
    ``demo_color`` (additive, never reaches a real peer). This lets a
    scenario pin a specific Rich color per host without writing a TOML
    config; falsy / absent / non-string values fall through to TUI
    palette auto-assignment.
    """
    candidates: list[tuple[int, str, Path]] = []
    for env_path in dir_path.glob("*.json"):
        if env_path.name.startswith("_"):
            continue
        order = _read_demo_order(env_path)
        candidates.append((order, env_path.stem, env_path))
    candidates.sort(key=lambda t: (t[0], t[1]))

    hosts: list[RemoteHost] = []
    for _order, name, env_path in candidates:
        color = _read_demo_color(env_path)
        hosts.append(
            RemoteHost(
                name=name,
                ssh_alias=f"{DEMO_SSH_ALIAS_PREFIX}{name}",
                description=f"demo host ({env_path.name})",
                remote_uxon="uxon",
                color=color,
            )
        )
    return hosts


_DEFAULT_DEMO_ORDER = 1_000_000  # absent / malformed → sorts after every explicit value.


def _read_demo_order(env_path: Path) -> int:
    """Extract optional integer ``demo_order``; falsy / absent → sentinel."""
    try:
        blob: Any = msgspec.json.decode(env_path.read_bytes())
    except (OSError, msgspec.DecodeError):
        return _DEFAULT_DEMO_ORDER
    if not isinstance(blob, dict):
        return _DEFAULT_DEMO_ORDER
    raw = blob.get("demo_order")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return _DEFAULT_DEMO_ORDER
    return raw


def _read_demo_color(env_path: Path) -> str | None:
    """Extract the optional ``demo_color`` field. Best-effort, never raises."""
    try:
        blob: Any = msgspec.json.decode(env_path.read_bytes())
    except (OSError, msgspec.DecodeError):
        return None
    if not isinstance(blob, dict):
        return None
    color = blob.get("demo_color")
    return color if isinstance(color, str) and color else None


def load_demo_snapshot(host_name: str, dir_path: Path, fetched_at: float) -> RemoteSnapshot:
    """Read ``<dir>/<host_name>.json`` and return a synthetic snapshot.

    Always returns a :class:`RemoteSnapshot`. A missing / malformed
    envelope yields a snapshot with an ``error`` set — same contract as
    a failed live fetch, so the TUI's existing error rendering kicks in.
    """
    env_path = dir_path / f"{host_name}.json"
    try:
        blob: Any = msgspec.json.decode(env_path.read_bytes())
    except FileNotFoundError:
        return _error_snapshot(host_name, fetched_at, f"demo envelope not found: {env_path}")
    except (OSError, msgspec.DecodeError) as exc:
        return _error_snapshot(host_name, fetched_at, f"demo envelope unreadable: {exc}")

    if not isinstance(blob, dict):
        return _error_snapshot(host_name, fetched_at, "demo envelope is not a JSON object")
    if blob.get("schema_version") != WIRE_SCHEMA_VERSION:
        return _error_snapshot(
            host_name,
            fetched_at,
            f"demo envelope schema_version {blob.get('schema_version')!r} "
            f"!= local {WIRE_SCHEMA_VERSION!r}",
        )
    data = blob.get("data")
    if not isinstance(data, dict):
        return _error_snapshot(host_name, fetched_at, "demo envelope.data is not an object")
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        return _error_snapshot(host_name, fetched_at, "demo envelope.data.sessions is not a list")

    raw_skipped = data.get("scope_skipped", [])
    scope_skipped = (
        [str(u) for u in raw_skipped if isinstance(u, str)] if isinstance(raw_skipped, list) else []
    )
    raw_host_stats = blob.get("host_stats")
    host_stats = raw_host_stats if isinstance(raw_host_stats, dict) else None

    return RemoteSnapshot(
        host_name=host_name,
        fetched_at_epoch=fetched_at,
        from_cache=False,
        error=None,
        sessions=sessions,
        cached_at_epoch=fetched_at,
        scope_limited=False,
        scope_skipped=scope_skipped,
        host_stats=host_stats,
    )


def _error_snapshot(host_name: str, fetched_at: float, error: str) -> RemoteSnapshot:
    return RemoteSnapshot(
        host_name=host_name,
        fetched_at_epoch=fetched_at,
        from_cache=False,
        error=error,
        sessions=[],
        cached_at_epoch=None,
    )


def load_demo_local_scope_users(dir_path: Path) -> list[str]:
    """Return the ``scope_users`` list authored into ``_local.json``.

    Lets the CLI auto-populate ``session_users`` in demo mode without
    asking the operator to set it in TOML — the scenario YAML already
    knows which ``-agent`` accounts have sessions on the local box, and
    that's exactly the list the TUI needs to fan ``collect_sessions``
    out over. Absent file / malformed JSON / missing field → ``[]``,
    matching the fail-soft posture of the other demo loaders.
    """
    env_path = dir_path / LOCAL_ENVELOPE_NAME
    try:
        blob: Any = msgspec.json.decode(env_path.read_bytes())
    except (FileNotFoundError, OSError, msgspec.DecodeError):
        return []
    if not isinstance(blob, dict):
        return []
    data = blob.get("data")
    if not isinstance(data, dict):
        return []
    raw = data.get("scope_users")
    if not isinstance(raw, list):
        return []
    return [str(u) for u in raw if isinstance(u, str) and u]


def load_demo_local_sessions(dir_path: Path, user: str) -> list[Any]:
    """Read ``<dir>/_local.json`` and return ``SessionInfo`` for ``user``.

    Inverse of :func:`uxon.wire_schema.build_session_records` for the
    list-kind envelope, scoped to one OS user. The envelope can carry
    sessions for multiple users (mirroring how a remote peer reports
    its full ``session_users`` scope); this loader filters by
    ``record.user`` so the production contract of ``collect_sessions``
    — "give me what's running for these users" — holds in demo mode
    without forcing every scenario to author per-user files.

    Fail-soft, all the way through: missing file, unreadable bytes,
    malformed JSON, wrong top-level type, schema-version mismatch,
    missing ``data.sessions`` list — all yield ``[]``. Matches the
    production "empty tmux socket" path, so a screenshot run on a
    pristine demo dir mirrors a fresh box.

    Imports :class:`uxon.cli.SessionInfo` lazily because ``uxon.cli``
    imports this module on demand from its own demo short-circuits;
    a load-time import here would close the cycle.
    """
    from uxon.cli import SessionInfo  # noqa: PLC0415

    env_path = dir_path / LOCAL_ENVELOPE_NAME
    try:
        blob: Any = msgspec.json.decode(env_path.read_bytes())
    except (FileNotFoundError, OSError, msgspec.DecodeError):
        return []
    if not isinstance(blob, dict):
        return []
    if blob.get("schema_version") != WIRE_SCHEMA_VERSION:
        return []
    data = blob.get("data")
    if not isinstance(data, dict):
        return []
    records = data.get("sessions")
    if not isinstance(records, list):
        return []

    out: list[SessionInfo] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("user") != user:
            continue
        out.append(_session_info_from_record(rec, SessionInfo))
    return out


def _session_info_from_record(rec: dict[str, Any], cls: type) -> Any:
    """Build one :class:`uxon.cli.SessionInfo` from a wire record.

    Every field is read defensively (``.get(...)``) so an envelope
    authored by a future or past producer can't crash the loader —
    same forward-compat posture as
    :func:`uxon.remote_collector._parse_envelope`. Type-narrowing of
    primitive fields uses ``str()`` / ``int()`` / ``float()`` /
    ``bool()`` rather than ``isinstance`` checks because the
    envelope is JSON (so the input is already one of those types) and
    we'd rather coerce than drop the record for a stray ``None``.

    ``cls`` is passed in by the caller (rather than imported here) to
    keep this helper free of the lazy-import dance — it's only ever
    invoked from :func:`load_demo_local_sessions`, which already
    resolved :class:`uxon.cli.SessionInfo`.
    """
    pane_pids_raw = rec.get("pane_pids") or []
    pane_pids = tuple(p for p in pane_pids_raw if isinstance(p, int))
    active_pid = rec.get("active_pid")
    if not isinstance(active_pid, int):
        active_pid = None
    return cls(
        user=str(rec.get("user", "")),
        name=str(rec.get("name", "")),
        attached="1" if rec.get("attached") else "0",
        windows=str(rec.get("windows", "0")),
        created=str(rec.get("created", "")),
        last_attached=str(rec.get("last_attached", "")),
        pane_pids=pane_pids,
        active_pid=active_pid,
        active_cmd=str(rec.get("active_cmd", "")),
        active_path=str(rec.get("active_path", "")),
        cpu_pct=float(rec.get("cpu_pct", 0.0) or 0.0),
        rss_kib=int(rec.get("rss_kib", 0) or 0),
        agent=str(rec.get("agent", "claude")),
        legacy=bool(rec.get("legacy", False)),
    )
