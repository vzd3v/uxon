"""The remote-snapshot collector orchestrator.

Composes the leaf concerns (argv build, envelope parse, on-disk cache,
wedged-master recovery) into :func:`fetch_remote_snapshot`, the single
point where the local uxon process talks to a peer machine. Always
returns a :class:`uxon.domain.wire_schema.RemoteSnapshot`; never raises
(except ``KeyboardInterrupt`` / ``SystemExit``).
"""

from __future__ import annotations

import shlex
import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path

from uxon.domain.wire_schema import RemoteSnapshot
from uxon.infra.remote.cache import read_cached_snapshot, write_cached_snapshot
from uxon.infra.remote.envelope import ALL_USERS_DISABLED_MARKER, parse_envelope
from uxon.infra.remote.master_recovery import recover_wedged_master
from uxon.infra.remote.ssh_argv import build_peer_ssh_argv
from uxon.infra.remote_hosts import RemoteHost
from uxon.infra.run import run_query

# Reasonable defaults: a peer that doesn't answer in 5 s is treated as
# down. ssh's TCP-level ConnectTimeout caps the connect phase only;
# the wall-clock budget for the whole fetch is enforced by the
# ``run_query(timeout=...)`` spawn that runs the ssh slave.
DEFAULT_CONNECT_TIMEOUT_SEC = 5
DEFAULT_TOTAL_TIMEOUT_SEC = 10


def _build_fetch_argv(
    host: RemoteHost,
    *,
    connect_timeout: int,
    all_users: bool,
    correlation_id: str,
    ssh_multiplex: str,
    ssh_control_persist_seconds: int = 300,
) -> list[str]:
    """Assemble the fetch argv for one host.

    Thin wrapper over :func:`build_peer_ssh_argv` with
    ``allocate_tty=False``. The remote command is the standard
    ``<remote_uxon> list [--all-users] --json`` invocation; the
    ``ALL_USERS_DISABLED_MARKER`` fallback path still works because
    ``all_users=False`` rebuilds the argv without ``--all-users``.
    """
    command = (
        f"{shlex.quote(host.remote_uxon)} list --all-users --json"
        if all_users
        else f"{shlex.quote(host.remote_uxon)} list --json"
    )
    remote_command = f"{command} --audit-correlation-id {shlex.quote(correlation_id)}"
    return build_peer_ssh_argv(
        command_template=host.command_template,
        extra_ssh_options=host.extra_ssh_options,
        ssh_alias=host.ssh_alias,
        remote_uxon=host.remote_uxon,
        remote_command=remote_command,
        allocate_tty=False,
        connect_timeout=connect_timeout,
        ssh_multiplex=ssh_multiplex,
        ssh_control_persist_seconds=ssh_control_persist_seconds,
    )


def fetch_remote_snapshot(
    host: RemoteHost,
    *,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SEC,
    total_timeout: int = DEFAULT_TOTAL_TIMEOUT_SEC,
    ssh_multiplex: str = "auto",
    ssh_control_persist_seconds: int = 300,
    override_state_dir: Path | None = None,
) -> RemoteSnapshot:
    """Fetch one host's session list. Always returns a snapshot.

    Fail-soft contract:
      - SSH process error (non-zero exit, timeout, file-not-found):
        captured into ``error``; cached snapshot loaded if present.
      - Malformed payload from a peer: same — the cache is the
        operator's safety net.
      - Successful fetch: cache rewritten, fresh snapshot returned
        with ``from_cache=False``.

    The ssh spawn goes through :func:`uxon.infra.run.run_query`; tests
    stub it by patching that function. ``override_state_dir`` is a test
    seam.
    """
    fetched_at = time.time()

    # Demo-mode short-circuit: when UXON_DEMO_HOSTS is set, every
    # ``fetch_remote_snapshot`` call resolves from a pre-rendered
    # envelope on disk instead of spawning ssh. The synthetic host's
    # ``ssh_alias`` carries the demo sentinel; if we ever see that
    # sentinel without the env var set (or vice versa) something has
    # gone sideways and we surface it as an error rather than letting
    # ``ssh demo:foo`` go out on the wire.
    from uxon.infra import demo as _uxon_demo  # noqa: PLC0415

    _demo_dir = _uxon_demo.demo_hosts_dir()
    _is_demo_alias = host.ssh_alias.startswith(_uxon_demo.DEMO_SSH_ALIAS_PREFIX)
    if _demo_dir is not None and _is_demo_alias:
        return _uxon_demo.load_demo_snapshot(host.name, _demo_dir, fetched_at)
    if _is_demo_alias:
        return RemoteSnapshot(
            host_name=host.name,
            fetched_at_epoch=fetched_at,
            from_cache=False,
            error=f"demo alias {host.ssh_alias!r} but {_uxon_demo.DEMO_ENV_VAR} is unset",
            sessions=[],
            cached_at_epoch=None,
        )

    correlation_id = str(uuid.uuid4())

    def _finalize(snap: RemoteSnapshot) -> RemoteSnapshot:
        from uxon.infra import audit as _audit

        _audit.audit(
            "list.remote.out",
            outcome="error" if snap.error else "ok",
            peer_name=host.name,
            ssh_alias=host.ssh_alias,
            scope="own" if snap.scope_limited else "all-users",
            from_cache=snap.from_cache,
            correlation_id=correlation_id,
            **({"error": snap.error[:256]} if snap.error else {}),
        )
        return snap

    # Per-host override precedence: ``host.connect_timeout`` /
    # ``host.total_timeout`` (if set) win over the keyword-supplied
    # fleet-global defaults. Sub-second durations are ceil-rounded to
    # 1 because ssh's ``ConnectTimeout`` accepts whole seconds only and
    # treats 0 as "system default" (which can be minutes); ``int(0.5)``
    # would silently disable the operator's intended cap. We ceil the
    # subprocess ``timeout=`` too (could be float) for symmetry.
    import math as _math  # noqa: PLC0415

    eff_connect = (
        max(1, _math.ceil(host.connect_timeout))
        if host.connect_timeout is not None
        else connect_timeout
    )
    eff_total = (
        max(1, _math.ceil(host.total_timeout)) if host.total_timeout is not None else total_timeout
    )

    def _run_one(all_users: bool) -> tuple[str | None, str | None, str]:
        """Run one ssh attempt. Returns ``(error, payload, stderr)``."""
        argv = _build_fetch_argv(
            host,
            connect_timeout=eff_connect,
            all_users=all_users,
            correlation_id=correlation_id,
            ssh_multiplex=ssh_multiplex,
            ssh_control_persist_seconds=ssh_control_persist_seconds,
        )
        try:
            cp = run_query(argv, timeout=eff_total)
        except subprocess.TimeoutExpired:
            # Slave hung — almost always wedged ControlMaster (the
            # subprocess timeout already killed our slave; siblings
            # share the same %C socket and would loop on the same
            # wedge). Cleanup is a no-op for non-multiplexed paths
            # and operator-supplied command_template hosts.
            if ssh_multiplex != "off":
                recover_wedged_master(host)
            return f"ssh timeout after {eff_total}s", None, ""
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
    # ``enable_all_users_list = false``). Retry with own-only so
    # the TUI still sees something for that peer.
    # The marker is the stable substring documented in
    # ``ALL_USERS_DISABLED_MARKER``; anything else is a hard error.
    if error is not None and ALL_USERS_DISABLED_MARKER in stderr:
        scope_limited = True
        error, payload, _ = _run_one(all_users=False)

    if error is None and payload is not None:
        sessions, scope_skipped, host_stats, parse_err = parse_envelope(payload)
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
                host_stats=host_stats,
            )
            # A cache-write failure (disk full, perms) must not taint
            # a fresh in-memory snapshot — we still have valid data.
            # Swallow the OSError; the next successful fetch retries.
            try:
                write_cached_snapshot(snap, override_dir=override_state_dir)
            except OSError:
                pass
            return _finalize(snap)

    # Failure path: try to fall back to the on-disk cache. The cache
    # carries the previously-observed scope flags so a peer that went
    # offline after flipping enable_all_users_list (or after building
    # up scope_skipped users) still surfaces the right badge in the
    # TUI — falling back to the live-fetch's intermediate flags would
    # invent a "full visibility" view that the cached payload does
    # not actually represent.
    cached = read_cached_snapshot(host.name, override_dir=override_state_dir)
    if cached is not None:
        # ``replace`` over a field-by-field copy: every other field
        # round-trips automatically, so adding a new attribute to
        # :class:`RemoteSnapshot` (e.g. ``host_stats`` in 3.4) doesn't
        # silently get dropped on the failure path the way it did
        # before this refactor.
        return _finalize(
            replace(
                cached,
                fetched_at_epoch=fetched_at,
                from_cache=True,
                error=error,
            )
        )
    return _finalize(
        RemoteSnapshot(
            host_name=host.name,
            fetched_at_epoch=fetched_at,
            from_cache=False,
            error=error,
            sessions=[],
            cached_at_epoch=None,
            scope_limited=scope_limited,
            scope_skipped=[],
        )
    )
