"""On-disk cache for the last successful remote snapshot per host.

The cache is the operator's safety net: a brief outage falls back to
the last good payload rather than blanking the TUI table. Read on every
failed fetch; a successful fetch overwrites it atomically (temp-file +
rename) under a mode-0700 directory so other users on a shared host
cannot read another user's host list.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import msgspec
import platformdirs

from uxon.domain.wire_schema import RemoteSnapshot


def state_dir(*, override: Path | None = None) -> Path:
    """Resolve the snapshot-cache directory.

    Honours ``$XDG_STATE_HOME`` per the XDG Base Directory spec, falls
    back to ``~/.local/state``. The ``override`` argument is for
    tests (so they don't have to mutate the user's real state dir).

    The directory is *not* created here — :func:`write_cached_snapshot`
    creates it on demand with mode 700 so a shared host's other
    users cannot read another user's cached host list.
    """
    if override is not None:
        return override
    # platformdirs honours ``$XDG_STATE_HOME`` on Linux and falls back
    # to ``~/.local/state`` — identical semantics to the previous
    # hand-rolled resolver. ``appauthor=False`` suppresses the
    # Windows-only "Author" path component (no-op on Linux but kept
    # explicit so the call site is portable).
    return Path(platformdirs.user_state_dir("uxon", appauthor=False)) / "remote"


def snapshot_cache_path(name: str, *, override_dir: Path | None = None) -> Path:
    """Return the on-disk cache path for the host named ``name``.

    ``name`` is trusted at this point — :func:`load_remote_hosts` in
    ``uxon.infra.remote_hosts`` already validated the charset against a
    conservative ASCII whitelist, so it is safe to use as a filename
    component. We do not double-validate to keep this module's
    surface narrow.
    """
    return state_dir(override=override_dir) / f"{name}.json"


def read_cached_snapshot(name: str, *, override_dir: Path | None = None) -> RemoteSnapshot | None:
    """Load the last successful snapshot from disk.

    Returns ``None`` when no cache exists or the file is unreadable /
    malformed; a corrupt cache is treated as no-cache rather than
    surfaced as an error, because the live-fetch error message is
    almost always more useful to the operator.
    """
    path = snapshot_cache_path(name, override_dir=override_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    try:
        blob: Any = msgspec.json.decode(text)
    except msgspec.DecodeError:
        return None
    if not isinstance(blob, dict):
        return None
    sessions = blob.get("sessions")
    cached_at = blob.get("cached_at_epoch")
    if not isinstance(sessions, list) or not isinstance(cached_at, (int, float)):
        return None
    # Forward-compat: pre-stage-5 caches don't carry scope flags.
    # Treat as defaults rather than discarding the cache file.
    raw_limited = blob.get("scope_limited", False)
    scope_limited = bool(raw_limited) if isinstance(raw_limited, bool) else False
    raw_skipped = blob.get("scope_skipped", [])
    if isinstance(raw_skipped, list):
        scope_skipped = [str(u) for u in raw_skipped if isinstance(u, str)]
    else:
        scope_skipped = []
    raw_host_stats = blob.get("host_stats")
    host_stats = raw_host_stats if isinstance(raw_host_stats, dict) else None
    return RemoteSnapshot(
        host_name=name,
        fetched_at_epoch=float(cached_at),
        from_cache=True,
        error=None,
        sessions=sessions,
        cached_at_epoch=float(cached_at),
        scope_limited=scope_limited,
        scope_skipped=scope_skipped,
        host_stats=host_stats,
    )


def write_cached_snapshot(snapshot: RemoteSnapshot, *, override_dir: Path | None = None) -> None:
    """Write a successful snapshot to disk atomically.

    No-op when ``snapshot.error`` is set or ``snapshot.from_cache``
    is True — a failed fetch must not overwrite the last good
    payload, and a snapshot loaded from cache should not be
    re-written (cached_at_epoch would be clobbered).

    The state directory is created with mode 0o700 if absent. The
    file itself is written via temp-file + rename so a concurrent
    reader never sees a half-written JSON object.
    """
    if snapshot.error is not None or snapshot.from_cache:
        return
    path = snapshot_cache_path(snapshot.host_name, override_dir=override_dir)
    # ``mkdir(mode=0o700)`` does NOT chmod an already-existing
    # directory, but the 0o700 mode is the security property
    # documented above (a shared host's other users must not read
    # another user's cache). Force-apply it after the mkdir so a
    # pre-existing more-permissive directory is brought into line.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        # Read-only filesystem or unwritable parent: caller will see
        # the failure on the actual write below; nothing useful to do
        # here.
        pass
    blob: dict[str, Any] = {
        "host_name": snapshot.host_name,
        "cached_at_epoch": snapshot.fetched_at_epoch,
        "sessions": snapshot.sessions,
        # Stage 5 step 8: persist scope flags. Without them, a peer
        # that flipped enable_all_users_list = false (or a peer that
        # accumulated scope_skipped users) would lose that signal on
        # the next cache-fallback path and the TUI would surface a
        # misleading "full visibility" badge.
        "scope_limited": bool(snapshot.scope_limited),
        "scope_skipped": list(snapshot.scope_skipped),
    }
    # Optional host-level metrics block; older caches predate this
    # field and ``read_cached_snapshot`` tolerates its absence.
    if snapshot.host_stats is not None:
        blob["host_stats"] = snapshot.host_stats
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(blob), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # Atomic-write failed (disk full, EINTR, perms). Best-effort
        # remove the partial ``.tmp`` so a future ``ls`` of the
        # state dir doesn't surface a stale partial file.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
