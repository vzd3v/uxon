"""Multi-host: SSH-driven remote-snapshot collector.

The collector is the single point where the local uxon process talks
to a peer machine. It SSH-runs ``uxon list --json`` on the peer,
parses the wire-schema envelope, and returns an in-memory
:class:`RemoteSnapshot`. On any failure (network, command timeout,
malformed JSON, schema-version mismatch, missing remote uxon) it
returns a snapshot with :attr:`RemoteSnapshot.error` populated and
:attr:`RemoteSnapshot.sessions` either empty or filled from the
last successful fetch cached on disk.

Design constraints (from the multi-host spec):

- **Fail-soft.** A bad host must never raise into the TUI event loop
  or block another host's poll. Every error path returns a snapshot
  object instead of raising; the only exceptions that propagate are
  ``KeyboardInterrupt`` / ``SystemExit`` for Ctrl-C.
- **Cached on disk.** The last successful payload is written to
  ``${XDG_STATE_HOME:-~/.local/state}/uxon/remote/<name>.json`` so a
  brief outage doesn't blank the TUI table. The cache is read on
  every failed fetch; a successful fetch overwrites it atomically.
- **SSH config is the source of truth.** The collector passes the
  configured ``ssh_alias`` to ``ssh`` verbatim — port, user,
  identity, ProxyCommand all come from the operator's
  ``~/.ssh/config``. We never construct user@host:port strings.
- **No prompts.** ``BatchMode=yes`` forbids password prompts,
  keyboard-interactive, and host-key TOFU prompts. If the operator's
  agent isn't loaded or the host key isn't already trusted, the
  fetch fails fast rather than blocking.

This commit is the collector + cache only. The TUI integration (a
``SourceSpec`` in the refresh registry, the new "Remote sessions"
table) is the next commit in the sequence.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from uxon.remote_hosts import RemoteHost
from uxon.wire_schema import WIRE_SCHEMA_VERSION, SessionRecord

# Reasonable defaults: a peer that doesn't answer in 5 s is treated as
# down. ssh's TCP-level ConnectTimeout caps the connect phase only;
# the wall-clock budget for the whole fetch is enforced by
# ``subprocess.run(timeout=...)``.
DEFAULT_CONNECT_TIMEOUT_SEC = 5
DEFAULT_TOTAL_TIMEOUT_SEC = 10


@dataclass(frozen=True)
class RemoteSnapshot:
    """Result of one fetch attempt against one :class:`RemoteHost`.

    Attributes:
        host_name: Mirrors :attr:`RemoteHost.name` — preserved here so
            consumers that store snapshots without the matching host
            object can still attribute them.
        fetched_at_epoch: ``time.time()`` at the moment the fetch
            attempt finished (success or failure). Used by the TUI to
            display "stale 12s ago" indicators.
        from_cache: ``True`` when the sessions list came from the
            on-disk cache rather than a fresh fetch. In a snapshot
            returned by :func:`fetch_remote_snapshot` this implies
            :attr:`error` is non-None (the live fetch failed and we
            fell back). A snapshot returned by :func:`read_cached_snapshot`
            in isolation also sets ``from_cache=True`` but leaves
            ``error=None`` — the cache file alone has no opinion on
            whether the peer is currently reachable.
        error: Short, human-readable error string, or ``None`` on a
            successful fetch. The collector never raises — every
            failure surfaces here.
        sessions: List of wire-schema :class:`SessionRecord` dicts.
            Empty when the fetch failed AND no cache was available.
        cached_at_epoch: Wall-clock time at which the underlying
            payload was originally collected. For a fresh fetch this
            equals :attr:`fetched_at_epoch`; for a cache fallback it
            is older.
        scope_limited: ``True`` when the peer rejected
            ``list --all-users`` (because its
            ``enable_all_users_list = false``) and the collector fell
            back to the legacy own-only ``list --json``. The TUI
            badges the section header with ``(own only)`` so the
            operator knows the per-peer view is partial. Default
            ``False`` — fresh peers serve the all-users view.
        scope_skipped: Users the peer probed for sudo reachability
            and could not reach. Forward-compatible: missing on older
            peers that don't emit the field — the collector treats
            that as ``[]``.
    """

    host_name: str
    fetched_at_epoch: float
    from_cache: bool
    error: str | None
    sessions: list[SessionRecord] = field(default_factory=list)
    cached_at_epoch: float | None = None
    scope_limited: bool = False
    scope_skipped: list[str] = field(default_factory=list)


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
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "uxon" / "remote"


def snapshot_cache_path(name: str, *, override_dir: Path | None = None) -> Path:
    """Return the on-disk cache path for the host named ``name``.

    ``name`` is trusted at this point — :func:`load_remote_hosts` in
    ``uxon.remote_hosts`` already validated the charset against a
    conservative ASCII whitelist, so it is safe to use as a filename
    component. We do not double-validate to keep this module's
    surface narrow.
    """
    return state_dir(override=override_dir) / f"{name}.json"


# ── Argv template (stage 5 step 3, additive — _build_ssh_argv stays alive
# until step 4 migrates the call site) ───────────────────────────────────

PLACEHOLDER_CLOSED_SET: frozenset[str] = frozenset(
    {
        "{ssh_alias}",
        "{remote_uxon}",
        "{connect_timeout}",
        "{xdg_cache}",
        "{remote_command}",
    }
)


def _xdg_cache_home() -> str:
    """Return the operator's XDG cache root.

    Honours ``$XDG_CACHE_HOME``; falls back to ``~/.cache``. Used as the
    parent of the SSH ``ControlPath`` socket so multiplexed connections
    survive across uxon invocations.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return base
    return str(Path.home() / ".cache")


def _default_template() -> list[str]:
    """The default SSH argv template.

    Tokens in ``{...}`` are placeholders resolved by :func:`_render_argv`.
    The template includes ``ControlMaster=auto`` so the second-and-later
    fetches against the same peer reuse a multiplexed session — first
    tick costs 200-500 ms (TCP+auth), warm ticks 5-20 ms.
    """
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ConnectTimeout={connect_timeout}",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPath={xdg_cache}/uxon/ssh-%C",
        "-o",
        "ControlPersist=60s",
        "{ssh_alias}",
        "{remote_command}",
    ]


def validate_command_template(template: list[str]) -> None:
    """Raise :class:`ValueError` if ``template`` contains unknown
    placeholders or violates the ``{remote_command}`` / ``{remote_uxon}``
    mutual-exclusion rule.

    Mutual exclusion: ``{remote_command}`` is the rendered
    ``"<remote_uxon> list ..."`` string; using both in the same template
    would produce two competing remote-shell invocations.
    """
    if not template:
        raise ValueError("command_template must be non-empty")
    seen: set[str] = set()
    for token in template:
        if not isinstance(token, str) or not token:
            raise ValueError(f"command_template tokens must be non-empty strings, got {token!r}")
        # Scan for placeholders in this token. We deliberately don't
        # support nested or repeated placeholders within a single token
        # — that's a code smell in argv construction.
        i = 0
        while i < len(token):
            start = token.find("{", i)
            if start == -1:
                break
            end = token.find("}", start)
            if end == -1:
                # Lone "{" without "}" — treat as literal, skip.
                break
            placeholder = token[start : end + 1]
            seen.add(placeholder)
            if placeholder not in PLACEHOLDER_CLOSED_SET:
                raise ValueError(
                    f"command_template contains unknown placeholder {placeholder!r}; "
                    f"valid placeholders are {sorted(PLACEHOLDER_CLOSED_SET)}"
                )
            i = end + 1
    if "{remote_command}" in seen and "{remote_uxon}" in seen:
        raise ValueError(
            "command_template uses both {remote_command} and {remote_uxon} — "
            "they are mutually exclusive ({remote_command} already includes "
            "the rendered remote uxon invocation)"
        )


def _render_argv(
    template: list[str],
    *,
    ssh_alias: str,
    remote_uxon: str,
    connect_timeout: int,
    xdg_cache: str,
    remote_command: str,
) -> list[str]:
    """Substitute placeholders in ``template``. Empty tokens after
    substitution are dropped (e.g. an empty extra-options list).
    """
    mapping = {
        "{ssh_alias}": ssh_alias,
        "{remote_uxon}": remote_uxon,
        "{connect_timeout}": str(connect_timeout),
        "{xdg_cache}": xdg_cache,
        "{remote_command}": remote_command,
    }
    rendered: list[str] = []
    for token in template:
        out = token
        for placeholder, value in mapping.items():
            if placeholder in out:
                out = out.replace(placeholder, value)
        if out:
            rendered.append(out)
    return rendered


def _build_ssh_argv(host: RemoteHost, *, connect_timeout: int, all_users: bool = True) -> list[str]:
    """Assemble the ``ssh`` argv for one fetch.

    ``BatchMode=yes`` turns off every interactive prompt (password,
    keyboard-interactive, host-key TOFU). ``StrictHostKeyChecking``
    is left at the user's configured default — typically ``ask`` in
    interactive sessions and ``accept-new`` if the operator opted in
    via ssh_config. The collector deliberately does not override
    that policy.

    The remote command is built from ``host.remote_uxon`` (validated
    non-empty) followed by ``list --all-users --json`` (or just
    ``list --json`` when ``all_users=False`` — used by the
    ``ALL_USERS_DISABLED_MARKER`` fallback path so peers with
    ``enable_all_users_list = false`` still report their own
    sessions). ``shlex.quote`` is applied to ``remote_uxon`` even
    though the validator forbids obvious metacharacters, because ssh
    joins remote args with spaces and runs them through the remote
    shell.
    """
    remote_cmd = (
        f"{shlex.quote(host.remote_uxon)} list --all-users --json"
        if all_users
        else f"{shlex.quote(host.remote_uxon)} list --json"
    )
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        "ServerAliveInterval=5",
        host.ssh_alias,
        remote_cmd,
    ]


# Stable substring emitted by a peer whose ``enable_all_users_list =
# false`` rejects ``list --all-users``. The collector greps stderr for
# this marker to decide whether to retry with the legacy
# ``list --json`` (own-only) command. Producer side: see
# ``cli.py``'s ``--all-users`` failure paths.
ALL_USERS_DISABLED_MARKER = "uxon-error: all-users-disabled"


def _parse_envelope(
    payload: str,
) -> tuple[list[SessionRecord] | None, list[str], str | None]:
    """Validate and unpack an ``uxon list --json`` envelope.

    Returns ``(sessions, scope_skipped, None)`` on success, or
    ``(None, [], error)`` when the payload is malformed. Failure
    modes:

    - JSON parse error.
    - Top-level shape is not a dict.
    - ``schema_version`` is missing or differs from the local
      :data:`WIRE_SCHEMA_VERSION`. Cross-version peers are rejected
      explicitly so a future schema bump fails loud rather than
      silently dropping fields.
    - ``kind`` is not ``"list"`` (the collector only ever runs
      ``list``; anything else is a remote bug or a wrong binary).
    - ``data.sessions`` is missing or not a list.

    ``scope_skipped`` is the optional per-target-sudo skipped-users
    list emitted by peers that ran the per-target probe. Older peers
    omit the field — we treat that as ``[]`` (forward-compatible
    addition to the schema, no version bump).

    No deep validation of individual session records — they're
    treated as opaque dicts. If a peer renames a session field, the
    TUI will surface the absence; we don't want to fail the whole
    snapshot for one bad session.
    """
    try:
        env: Any = json.loads(payload)
    except json.JSONDecodeError as exc:
        return None, [], f"invalid JSON: {exc.msg}"
    if not isinstance(env, dict):
        return None, [], "envelope is not a JSON object"
    schema_version = env.get("schema_version")
    if schema_version != WIRE_SCHEMA_VERSION:
        return (
            None,
            [],
            (
                f"schema_version mismatch: peer reports {schema_version!r}, "
                f"local expects {WIRE_SCHEMA_VERSION!r}"
            ),
        )
    if env.get("kind") != "list":
        return None, [], f"unexpected envelope kind {env.get('kind')!r}"
    data = env.get("data")
    if not isinstance(data, dict):
        return None, [], "envelope.data is not an object"
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        return None, [], "envelope.data.sessions is not a list"
    raw_skipped = data.get("scope_skipped", [])
    if isinstance(raw_skipped, list):
        scope_skipped = [str(u) for u in raw_skipped if isinstance(u, str)]
    else:
        scope_skipped = []
    return sessions, scope_skipped, None


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
        blob: Any = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(blob, dict):
        return None
    sessions = blob.get("sessions")
    cached_at = blob.get("cached_at_epoch")
    if not isinstance(sessions, list) or not isinstance(cached_at, (int, float)):
        return None
    return RemoteSnapshot(
        host_name=name,
        fetched_at_epoch=float(cached_at),
        from_cache=True,
        error=None,
        sessions=sessions,
        cached_at_epoch=float(cached_at),
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
    blob = {
        "host_name": snapshot.host_name,
        "cached_at_epoch": snapshot.fetched_at_epoch,
        "sessions": snapshot.sessions,
    }
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


def fetch_remote_snapshot(
    host: RemoteHost,
    *,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SEC,
    total_timeout: int = DEFAULT_TOTAL_TIMEOUT_SEC,
    override_state_dir: Path | None = None,
    _runner: Any = subprocess.run,
) -> RemoteSnapshot:
    """Fetch one host's session list. Always returns a snapshot.

    Fail-soft contract:
      - SSH process error (non-zero exit, timeout, file-not-found):
        captured into ``error``; cached snapshot loaded if present.
      - Malformed payload from a peer: same — the cache is the
        operator's safety net.
      - Successful fetch: cache rewritten, fresh snapshot returned
        with ``from_cache=False``.

    ``_runner`` exists for tests — production callers leave it at
    its default. ``override_state_dir`` is also a test seam.
    """
    fetched_at = time.time()

    def _run_one(all_users: bool) -> tuple[str | None, str | None, str]:
        """Run one ssh attempt. Returns ``(error, payload, stderr)``."""
        argv = _build_ssh_argv(host, connect_timeout=connect_timeout, all_users=all_users)
        try:
            cp = _runner(argv, capture_output=True, text=True, timeout=total_timeout)
        except subprocess.TimeoutExpired:
            return f"ssh timeout after {total_timeout}s", None, ""
        except FileNotFoundError:
            return "ssh not installed on local host", None, ""
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # pragma: no cover — defensive only
            return f"{exc.__class__.__name__}: {exc}", None, ""
        if cp.returncode != 0:
            stderr = (cp.stderr or "").strip()
            err = (
                f"ssh exited {cp.returncode}: {stderr.splitlines()[0]}"
                if stderr
                else f"ssh exited {cp.returncode}"
            )
            return err, None, stderr
        return None, cp.stdout, ""

    error, payload, stderr = _run_one(all_users=True)
    scope_limited = False
    # Fallback: peer rejected ``--all-users`` (its
    # ``enable_all_users_list = false``). Retry with the legacy
    # own-only command so the TUI still sees something for that peer.
    # The marker is the stable substring documented in
    # ``ALL_USERS_DISABLED_MARKER``; anything else is a hard error.
    if error is not None and ALL_USERS_DISABLED_MARKER in stderr:
        scope_limited = True
        error, payload, _ = _run_one(all_users=False)

    if error is None and payload is not None:
        sessions, scope_skipped, parse_err = _parse_envelope(payload)
        if parse_err is not None:
            error = parse_err
        else:
            assert sessions is not None
            snap = RemoteSnapshot(
                host_name=host.name,
                fetched_at_epoch=fetched_at,
                from_cache=False,
                error=None,
                sessions=sessions,
                cached_at_epoch=fetched_at,
                scope_limited=scope_limited,
                scope_skipped=scope_skipped,
            )
            # A cache-write failure (disk full, perms) must not taint
            # a fresh in-memory snapshot — we still have valid data.
            # Swallow the OSError; the next successful fetch retries.
            try:
                write_cached_snapshot(snap, override_dir=override_state_dir)
            except OSError:
                pass
            return snap

    # Failure path: try to fall back to the on-disk cache.
    cached = read_cached_snapshot(host.name, override_dir=override_state_dir)
    if cached is not None:
        return RemoteSnapshot(
            host_name=host.name,
            fetched_at_epoch=fetched_at,
            from_cache=True,
            error=error,
            sessions=cached.sessions,
            cached_at_epoch=cached.cached_at_epoch,
            scope_limited=scope_limited,
        )
    return RemoteSnapshot(
        host_name=host.name,
        fetched_at_epoch=fetched_at,
        from_cache=False,
        error=error,
        sessions=[],
        cached_at_epoch=None,
        scope_limited=scope_limited,
    )
