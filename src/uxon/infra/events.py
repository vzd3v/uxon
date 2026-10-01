"""Opt-in debug and metrics JSONL logs with private, best-effort writes."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING, Any

import platformdirs

if TYPE_CHECKING:
    import structlog

# Lazily-built structlog renderer for the off-by-default debug channel.
# ``import structlog`` is expensive (~0.6 s — it eagerly pulls
# ``structlog.dev`` → rich → pygments) and ``events`` sits on the hot
# startup path (``cli.main`` → ``config_loader`` → ``events``, and the
# TUI first-frame path). The renderer is only ever needed when
# ``UXON_DEBUG`` is set, so we defer the import to first use inside
# :func:`_debug_renderer` (called from :func:`debug`) and cache the
# renderer here — keeping every normal
# ``uxon`` invocation (and the TUI first frame) ~0.6 s faster. See
# ``tests/test_uxon_imports.py`` for the regression guard.
#
# We bypass structlog's logger configuration on purpose: the debug log
# is a per-day, per-user file whose path is recomputed per call (so a
# long-running TUI rolls cleanly across midnight, and so tests can
# patch ``_log_dir``). The renderer turns a record dict into one JSON
# line; we write it ourselves. This keeps the on-disk shape
# byte-identical to the prior ``json.dumps(...)`` output and to the
# format consumers (tests, ``jq`` pipelines) rely on.
_DEBUG_RENDERER: structlog.processors.JSONRenderer | None = None


def _debug_renderer() -> structlog.processors.JSONRenderer:
    """Return the cached debug JSON renderer, importing structlog on first use.

    ``debug()`` runs on Textual worker threads, so first-use entry here can
    race. Left lock-free deliberately: the build is idempotent (constant
    args, no per-call state) and the ref assignment is atomic under CPython,
    so a racing double-build just has the last writer win an identical object.
    """
    global _DEBUG_RENDERER
    if _DEBUG_RENDERER is None:
        import structlog

        _DEBUG_RENDERER = structlog.processors.JSONRenderer(
            sort_keys=False,
            ensure_ascii=False,
        )
    return _DEBUG_RENDERER


def _default_log_dir() -> str:
    """Return the XDG-derived default log directory.

    Honours ``XDG_STATE_HOME``; falls back to ``~/.local/state``.
    Resolution is delegated to :mod:`platformdirs`, which honours the
    same env var on Linux. ``UXON_LOG_DIR`` overrides this in
    :func:`_log_dir`.
    """
    return platformdirs.user_state_dir("uxon", appauthor=False)


def _log_dir() -> str:
    """Return the log directory, honouring ``UXON_LOG_DIR``."""
    return os.environ.get("UXON_LOG_DIR") or _default_log_dir()


def _append_private_log(path: str, line: str, *, rotate_bytes: int | None = None) -> None:
    """Validate one private directory handle before rotating or appending."""
    directory, name = os.path.split(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory_info = os.fstat(directory_fd)
        if directory_info.st_uid != os.geteuid() or stat.S_IMODE(directory_info.st_mode) & 0o077:
            raise PermissionError("diagnostic directory must be owned by this user and private")
        if rotate_bytes is not None:
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None and info.st_size >= rotate_bytes:
                _rotate_metrics(directory_fd, name)
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise PermissionError("diagnostic file is not an owned regular file")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8", closefd=False) as handle:
                handle.write(line + "\n")
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


# ── Debug logging (off by default; enable via UXON_DEBUG env) ────────
#
# Internal-only diagnostic channel. Off in production; instrumentation
# call sites stay in place and cost a single ``frozenset`` truthiness
# check when disabled. Goes to ``tui-debug-{user}-{date}.log``.


def _parse_debug_topics() -> frozenset[str]:
    """Resolve ``UXON_DEBUG`` once at import. Empty → channel off."""
    raw = os.environ.get("UXON_DEBUG", "").strip().lower()
    if not raw:
        return frozenset()
    if raw in {"1", "true", "all", "*", "yes", "on"}:
        return frozenset({"*"})
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


_DEBUG_TOPICS: frozenset[str] = _parse_debug_topics()


def is_enabled(topic: str) -> bool:
    """Return whether the debug ``topic`` channel is active.

    Cheap (one ``frozenset`` membership check, no I/O). For call sites
    that must decide whether to *install* instrumentation at all — e.g.
    arming a recurring event-loop watchdog — rather than merely whether
    to emit a single record (which :func:`debug` already gates itself).
    """
    if not _DEBUG_TOPICS:
        return False
    return "*" in _DEBUG_TOPICS or topic in _DEBUG_TOPICS


def debug(topic: str, **fields: Any) -> None:
    """Append one JSON line to the debug log iff ``UXON_DEBUG`` enables ``topic``.

    No-op when ``UXON_DEBUG`` is unset (one ``frozenset`` truthiness
    check), so call sites can be left in place after a bug is fixed —
    they cost nothing in production. Never raises.

    Output: ``${XDG_STATE_HOME:-~/.local/state}/uxon/tui-debug-{user}-{YYYYMMDD}.log``
    (honours ``UXON_LOG_DIR``).

    Topic is required; arbitrary keyword fields merge into the JSON
    record.
    """
    if not _DEBUG_TOPICS:
        return
    if "*" not in _DEBUG_TOPICS and topic not in _DEBUG_TOPICS:
        return
    try:
        import datetime

        now = datetime.datetime.now(datetime.UTC)
        record: dict[str, Any] = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "topic": topic,
        }
        record.update(fields)

        log_dir = _log_dir()

        user = os.environ.get("SUDO_USER") or os.environ.get("USER", "unknown")
        date_str = now.strftime("%Y%m%d")
        path = os.path.join(log_dir, f"tui-debug-{user}-{date_str}.log")
        # Render via structlog's JSONRenderer for shape parity with the
        # rest of the structured-logging surface; the on-disk format
        # remains one ``{"ts": ..., "topic": ..., ...}`` JSON object per
        # line. The first positional argument (logger) is unused by
        # the JSON processor.
        # JSONRenderer is statically typed ``str | bytes``; with our
        # config (no bytes serializer) it always returns ``str`` here.
        line = _debug_renderer()(None, "debug", record)
        assert isinstance(line, str)
        _append_private_log(path, line)
    except Exception:
        # Telemetry, not a correctness path — never crash the TUI.
        return


# ── Metrics (off by default; enable via UXON_METRICS=1) ──────────────
#
# Opt-in JSONL of source-attempt records, rotated at 1 MiB into ``.1``
# and ``.2`` (cap 3 files total). Telemetry, not a correctness path:
# failures are swallowed, never raised. The path lives next to the
# debug log under platformdirs' ``user_state_dir("uxon")``.

# Test seam: rotation threshold in bytes. Production default is 1 MiB
# (per spec). Tests override to a small value to exercise rotation
# without writing megabytes.
_METRICS_ROTATE_BYTES: int = 1024 * 1024


def _metrics_enabled() -> bool:
    """True iff ``UXON_METRICS`` is set to a truthy value.

    Resolved per-call (not snapshotted at import) so tests can flip
    the env var and the production process picks up an operator
    runtime change without restart.
    """
    raw = os.environ.get("UXON_METRICS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _metrics_path() -> str:
    """Return the metrics-log path. Honours ``UXON_LOG_DIR``."""
    return os.path.join(_log_dir(), "metrics.jsonl")


def _rotate_metrics(directory_fd: int, name: str) -> None:
    """Rotate only owned private regular files inside the validated directory."""
    names = (name, name + ".1", name + ".2")
    present: set[str] = set()
    for candidate in names:
        try:
            info = os.stat(candidate, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise PermissionError("metrics rotation requires owned private regular files")
        present.add(candidate)
    if names[2] in present:
        os.unlink(names[2], dir_fd=directory_fd)
    for source, destination in ((names[1], names[2]), (names[0], names[1])):
        if source in present:
            os.rename(source, destination, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)


def metrics_record(
    source_id: str,
    *,
    elapsed_ms: int,
    error: str | None,
    from_cache: bool = False,
    attempted_at: float | None = None,
) -> None:
    """Append one JSON line to ``metrics.jsonl`` if ``UXON_METRICS=1``.

    Rotates at ``_METRICS_ROTATE_BYTES`` (1 MiB by default) into ``.1``
    and ``.2`` files; cap is 3 files total. Telemetry path — never
    raises, never crashes the TUI.

    Fields:
      ts            ISO-8601 UTC timestamp (seconds precision)
      source_id     ``"main_ctx_rebuild"`` or ``"remote:<host>"``
      elapsed_ms    wall-time of the fetch attempt
      error         first-line error string, or ``null`` on success
      from_cache    True iff the result was served from on-disk cache
      attempted_at  optional epoch seconds (caller-supplied)
    """
    if not _metrics_enabled():
        return
    try:
        import datetime
        import json

        now = datetime.datetime.now(datetime.UTC)
        record: dict[str, Any] = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_id": source_id,
            "elapsed_ms": int(elapsed_ms),
            "error": error,
            "from_cache": bool(from_cache),
        }
        if attempted_at is not None:
            record["attempted_at"] = float(attempted_at)

        line = json.dumps(record, ensure_ascii=False)
        _append_private_log(_metrics_path(), line, rotate_bytes=_METRICS_ROTATE_BYTES)
    except Exception:
        # Telemetry — never crash the TUI.
        return
