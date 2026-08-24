# SPDX-License-Identifier: MIT
"""``uxon list`` use-case: local + remote session listing and JSON emit."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Any

from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.format import compact_time, format_cpu_pct, format_rss_kib
from uxon.domain.session import SessionInfo
from uxon.errors import eprint, fail
from uxon.infra import config_loader, identity, sessions_probe, version_probe


def _resolve_all_users_scope(cfg: Config, launch_user: str) -> tuple[list[str], list[str]]:
    """Probe per-target sudo and split ``session_users`` into reachable / skipped.

    Returns ``(scope_users, scope_skipped)``:

    - ``scope_users`` = ``launch_user`` plus every user from
      ``identity.resolve_all_session_users(cfg, launch_user)`` that the caller
      can reach via ``sudo -n -H -u <U>``. The list is deterministically
      ordered (stable, sorted by user where it matters).
    - ``scope_skipped`` = the rest of ``session_users`` (excluding
      self) — users in config that the caller cannot reach. Surfaced
      separately so ``--json`` callers and human stderr both see what
      was filtered.

    The launch user itself is always in ``scope_users`` and never in
    ``scope_skipped``: there's no sudo step for "see my own
    sessions".
    """
    from uxon.infra.sudo_probe import probe_sudo_capability

    all_users = identity.resolve_all_session_users(cfg, launch_user)
    candidates = [u for u in all_users if u != launch_user]
    caps = probe_sudo_capability(cfg, candidates)
    reachable = [u for u in candidates if u in caps.reachable_users]
    skipped = [u for u in candidates if u not in caps.reachable_users]
    scope_users = config_loader.normalize_user_list([launch_user, *reachable])
    return scope_users, skipped


def _emit_scope_skipped_hint(scope_skipped: list[str] | None) -> None:
    """Print a single-line stderr hint when ``--all-users`` filtered users.

    Format mirrors the spec:
    ``# 2 users skipped (no sudo): carol_agent, dave_agent``.
    No-op when the skipped list is empty / None — stdout stays
    parseable and human output stays uncluttered.
    """
    if not scope_skipped:
        return
    eprint(f"# {len(scope_skipped)} users skipped (no sudo): {', '.join(scope_skipped)}")


def _list_data(
    cfg: Config,
    sessions: list[SessionInfo],
    scope_users: list[str],
    *,
    all_users: bool,
    scope_skipped: list[str] | None = None,
) -> dict[str, Any]:
    """Build the ``data`` body for ``uxon list --json``.

    Wraps :func:`build_session_records` and exposes the inputs a
    remote consumer needs to label the snapshot: which OS users were
    scoped, whether ``--all-users`` was on, and the session prefix
    that ``short_id`` was stripped against.

    ``scope_skipped`` (optional) is the per-target-sudo "users in
    ``session_users`` we probed but couldn't reach" list. It is
    omitted from the envelope when ``None`` so single-user listings
    stay byte-identical to their previous shape; callers that
    performed an ``--all-users`` probe pass the (possibly empty)
    list to surface it in the envelope.
    """
    from uxon.domain.wire_schema import build_session_records

    body: dict[str, Any] = {
        "all_users": all_users,
        "scope_users": list(scope_users),
        "session_prefix": cfg.session_prefix,
        "sessions": build_session_records(sessions, session_prefix=cfg.session_prefix),
    }
    if scope_skipped is not None:
        body["scope_skipped"] = list(scope_skipped)
    return body


def _emit_json_with_host(
    kind: str, data: dict[str, Any], *, host: str, compact: bool = False
) -> None:
    """Emit a JSON envelope with the optional ``host`` field set.

    Used by ``list --host <name>``: the local CLI is not running on
    the peer, so the envelope is *attributed* to the named host
    rather than implying a local origin. The field follows the
    optional shape documented in :class:`uxon.domain.wire_schema.Envelope`.

    ``compact=True`` emits the envelope on a single line (no
    indentation) so a sequence of calls produces a valid JSON
    Lines stream — used by ``--all-hosts --json`` so a consumer
    can split on ``\\n`` and parse each record independently.
    """
    from uxon.domain.wire_schema import make_envelope

    env = make_envelope(
        kind,  # type: ignore[arg-type]
        data,
        uxon_version=version_probe.read_repo_version(),
        host=host,
    )
    if compact:
        print(json.dumps(env, sort_keys=False))
    else:
        print(json.dumps(env, indent=2, sort_keys=False))


def _list_data_from_records(
    sessions: list[Any],
    scope_users: list[str],
    *,
    session_prefix: str,
    all_users: bool,
    scope_skipped: list[str] | None = None,
) -> dict[str, Any]:
    """Build the ``list`` envelope ``data`` from already-prepared
    wire-schema records (i.e. data fetched from a peer rather than
    collected locally).

    Used by the ``--host`` path so the local CLI's JSON output for a
    remote-host listing has the same shape as a local one — the
    only delta is the envelope-level ``host`` field set by the
    caller.

    ``scope_skipped`` (optional) propagates the per-target-sudo
    skipped-users list through; omitted when ``None`` to keep the
    envelope shape stable for callers that don't pass it.
    """
    body: dict[str, Any] = {
        "all_users": all_users,
        "scope_users": list(scope_users),
        "session_prefix": session_prefix,
        "sessions": list(sessions),
    }
    if scope_skipped is not None:
        body["scope_skipped"] = list(scope_skipped)
    return body


def _print_remote_table(
    cfg: Config,
    host_name: str,
    sessions: Sequence[dict[str, Any]] | Sequence[Any],
    *,
    cached: bool,
) -> None:
    """Render a remote host's ``list --json`` payload as a human
    table.

    The wire-schema dicts carry the same fields :func:`print_list`
    needs, so we synthesise enough of a ``SessionInfo`` to reuse the
    existing renderer. Only ``user``, ``name``, ``attached``,
    ``windows``, ``created``, ``last_attached``, ``active_pid``,
    ``active_cmd``, ``active_path``, ``cpu_pct``, ``rss_kib``,
    ``agent``, ``legacy`` are read; ``pane_pids`` is informational
    on local rows and not rendered, so we leave it empty.
    """
    synth = []
    for r in sessions:
        synth.append(
            SessionInfo(
                user=str(r.get("user", "")),
                name=str(r.get("name", "")),
                attached="1" if r.get("attached") else "0",
                windows=str(r.get("windows", "")),
                created=str(r.get("created", "")),
                last_attached=str(r.get("last_attached", "")),
                pane_pids=(),
                active_pid=r.get("active_pid"),
                active_cmd=str(r.get("active_cmd", "")),
                active_path=str(r.get("active_path", "")),
                cpu_pct=float(r.get("cpu_pct", 0.0) or 0.0),
                rss_kib=int(r.get("rss_kib", 0) or 0),
                agent=str(r.get("agent", "")),
                profile=str(r.get("profile", "")),
                runtime=str(r.get("runtime", "")),
                runtime_resource=str(r.get("runtime_resource", "")),
                runtime_down=bool(r.get("runtime_down", False)),
                legacy=bool(r.get("legacy", False)),
            )
        )
    cache_marker = "  (CACHED — peer unreachable)" if cached else ""
    print(f"── remote: {host_name}{cache_marker} ──")
    users_in_payload = sorted({s.user for s in synth}) or ["?"]
    show_user = len(users_in_payload) > 1
    print_list(cfg, synth, users_in_payload, show_user=show_user)


def _do_list_host(args: ParsedArgs, cfg: Config) -> int:
    """Handle ``uxon list --host <name>``.

    Looks up the configured peer, runs the SSH-driven collector,
    and prints either the JSON envelope (with the ``host`` field
    set) or a human table. When the live fetch fails but the disk
    cache is populated, the result is rendered with a "(CACHED)"
    marker; no fallback exits with a non-zero code so the caller
    knows to investigate.
    """
    from uxon.infra.remote.collector import fetch_remote_snapshot
    from uxon.infra.remote_hosts import find_host

    if not cfg.remote_hosts:
        fail("no [[remote_hosts]] configured; --host requires at least one peer")
    target = find_host(cfg.remote_hosts, args.host or "")
    if target is None:
        names = ", ".join(h.name for h in cfg.remote_hosts) or "<none>"
        fail(f"unknown --host {args.host!r}; configured: {names}")
    snap = fetch_remote_snapshot(
        target,
        ssh_multiplex=cfg.ssh_multiplex,
        ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
    )
    if args.json_output:
        _emit_json_with_host(
            "list",
            _list_data_from_records(
                snap.sessions,
                # The peer's payload carried scope_users on its own
                # envelope; we lost that during collector parsing
                # because the wire schema there only kept ``sessions``.
                # Surface what we can derive.
                scope_users=sorted({s.get("user", "") for s in snap.sessions if s.get("user")}),
                session_prefix=cfg.session_prefix,
                all_users=False,
            ),
            host=target.name,
        )
        if snap.error and not snap.from_cache:
            eprint(f"uxon: --host {target.name}: {snap.error}")
            return 1
        return 0
    _print_remote_table(cfg, target.name, snap.sessions, cached=snap.from_cache)
    if snap.error and not snap.from_cache:
        eprint(f"uxon: --host {target.name}: {snap.error}")
        return 1
    return 0


def _do_list_all_hosts(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    """Handle ``uxon list --all-hosts``.

    Prints the local listing first, then one block per configured
    peer. With ``--json`` emits a JSON Lines stream — one envelope
    per source (local + each peer) — so a consumer can split by
    newline and parse each independently. Exits non-zero iff any
    peer failed AND its cache was empty; partial results are still
    rendered.
    """
    from uxon.infra.remote.collector import fetch_remote_snapshot

    rc = 0
    scope_skipped: list[str] | None
    if args.all_users:
        if not cfg.enable_all_users_list:
            fail("uxon-error: all-users-disabled (enable_all_users_list = false in config)")
        scope_users, scope_skipped = _resolve_all_users_scope(cfg, launch_user)
    else:
        scope_users = [launch_user]
        scope_skipped = None
    local_sessions = sessions_probe.collect_sessions(scope_users, cfg)

    if args.json_output:
        # JSON Lines: one envelope per line. A consumer splits on
        # ``\n`` and parses each line independently.
        _emit_json(
            "list",
            _list_data(
                cfg,
                local_sessions,
                scope_users,
                all_users=args.all_users,
                scope_skipped=scope_skipped,
            ),
            compact=True,
        )
        for host in cfg.remote_hosts:
            snap = fetch_remote_snapshot(
                host,
                ssh_multiplex=cfg.ssh_multiplex,
                ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
            )
            _emit_json_with_host(
                "list",
                _list_data_from_records(
                    snap.sessions,
                    scope_users=sorted({s.get("user", "") for s in snap.sessions if s.get("user")}),
                    session_prefix=cfg.session_prefix,
                    all_users=False,
                ),
                host=host.name,
                compact=True,
            )
            if snap.error and not snap.from_cache:
                eprint(f"uxon: --host {host.name}: {snap.error}")
                rc = 1
        return rc

    # Human-readable: local block first, then peers.
    print_list(cfg, local_sessions, scope_users, show_user=args.all_users)
    if scope_skipped:
        _emit_scope_skipped_hint(scope_skipped)
    for host in cfg.remote_hosts:
        snap = fetch_remote_snapshot(
            host,
            ssh_multiplex=cfg.ssh_multiplex,
            ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
        )
        print()
        _print_remote_table(cfg, host.name, snap.sessions, cached=snap.from_cache)
        if snap.error and not snap.from_cache:
            eprint(f"uxon: --host {host.name}: {snap.error}")
            rc = 1
    return rc


def do_list(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    """Handle the full ``uxon list`` use-case (local + remote + audit/scope).

    Dispatches the ``--host`` / ``--all-hosts`` variants to their
    dedicated handlers, then runs the local-listing audit/scope logic
    (peer-inbound ``list.remote.in`` vs caller-side ``list.peek``, the
    ``--all-users`` enable gate, and the JSON-vs-human emit). Lifted
    verbatim from the old ``cli.main`` inline branch so behavior is
    identical; ``dispatch`` calls this as a thin router target.
    """
    from uxon.infra import audit as _audit

    if args.host is not None:
        return _do_list_host(args, cfg)
    if args.all_hosts:
        return _do_list_all_hosts(args, cfg, launch_user)
    # Peer-inbound branch: a peer-collector invocation arrives with
    # ``SSH_CONNECTION`` set and neither ``--host`` nor ``--all-hosts``.
    # Fires *after* those early-returns so a caller-side
    # ``uxon list --host`` does not double-emit on its own host.
    # Spec line 306: when peer-inbound, ``list.remote.in`` replaces
    # ``list.peek`` ("instead of"), so we suppress the latter on
    # this code path.
    #
    # Spec line 207-209: state-changing events emit on **both**
    # success and failure paths.  ``list.remote.in`` is no
    # exception: the previous shape (single ``outcome=ok`` emit at
    # the top, before the all-users-disabled gate) lost the denied
    # outcome — a peer that refused ``--all-users`` recorded a
    # stale ``ok``.  Emit at outcome boundaries instead: once on
    # the all-users-disabled denial, once on success after the
    # gate passes (or for the own-only branch).
    peer_inbound = bool(os.environ.get("SSH_CONNECTION"))
    # ``correlation_id`` for ``list.remote.in`` is auto-injected by
    # ``audit()`` from module state when the parser popped
    # ``--audit-correlation-id`` off argv.  See spec §"Correlation
    # across hosts" ("omitted rather than synthesized").
    list_scope = "all-users" if args.all_users else "own"
    if args.all_users:
        if not cfg.enable_all_users_list:
            # Stable error tag. The remote-host aggregator's
            # fallback detector greps for this exact substring to
            # decide whether to retry with the legacy ``list
            # --json`` (own-only) command.
            if peer_inbound:
                _audit.audit(
                    "list.remote.in",
                    outcome="denied",
                    scope=list_scope,
                )
            fail("uxon-error: all-users-disabled (enable_all_users_list = false in config)")
        scope_users, scope_skipped = _resolve_all_users_scope(cfg, launch_user)
        # ``list.peek`` / ``list.remote.in`` fires only after the
        # gate passes — placement ensures we never log a successful
        # peek for a denied invocation.
        if peer_inbound:
            _audit.audit(
                "list.remote.in",
                scope=list_scope,
            )
        else:
            _audit.audit(
                "list.peek",
                scope_users=scope_users,
                scope_skipped=list(scope_skipped),
            )
        sessions = sessions_probe.collect_sessions(scope_users, cfg)
        if args.json_output:
            _emit_json(
                "list",
                _list_data(
                    cfg,
                    sessions,
                    scope_users,
                    all_users=True,
                    scope_skipped=scope_skipped,
                ),
            )
            return 0
        rc = print_list(cfg, sessions, scope_users, show_user=True)
        _emit_scope_skipped_hint(scope_skipped)
        return rc
    # Own-only branch: no gate, single success emit on the peer
    # side (the local-side ``list`` does not produce a ``list.peek``
    # for its own user — that's by spec, only ``--all-users``
    # local enumeration triggers ``list.peek``).
    if peer_inbound:
        _audit.audit(
            "list.remote.in",
            scope=list_scope,
        )
    scope_users = [launch_user]
    sessions = sessions_probe.collect_sessions(scope_users, cfg)
    if args.json_output:
        _emit_json("list", _list_data(cfg, sessions, scope_users, all_users=False))
        return 0
    return print_list(cfg, sessions, scope_users, show_user=False)


def _emit_json(kind: str, data: dict[str, Any], *, compact: bool = False) -> None:
    """Print one wire-schema envelope to stdout as JSON.

    Centralises envelope construction so every ``--json`` exit path
    uses the same shape (``schema_version``, ``uxon_version``,
    ``kind``, ``data``). ``kind`` is the action name; the runtime
    accepts any string but only the documented set
    (``list``/``doctor``/``version``/``kill``/``kill-all``) is part
    of the contract.

    ``compact=True`` emits a single-line record (used by the
    ``--all-hosts --json`` JSON Lines stream). Default is the
    pretty-printed form so a human-piped ``uxon list --json`` is
    readable.
    """
    from uxon.domain.wire_schema import make_envelope

    # Additive optional ``host_stats`` block for ``list`` envelopes —
    # producer must never abort the list output if /proc is partially
    # unavailable; absence is the documented forward-compatible signal.
    host_stats: dict[str, Any] | None = None
    if kind == "list":
        try:
            from uxon.infra.probes import read_host_stats

            hs = read_host_stats()
            host_stats = {
                "cpu_pct": hs.cpu_pct,
                "mem_used_kib": hs.mem_used_kib,
                "mem_total_kib": hs.mem_total_kib,
                "loadavg_1m": hs.loadavg_1m,
                "uptime_s": hs.uptime_s,
                "kernel": hs.kernel,
            }
        except Exception as exc:  # pragma: no cover — defensive
            from uxon.infra.events import debug

            debug("probes", err=type(exc).__name__, msg=str(exc))
    env = make_envelope(
        kind,  # type: ignore[arg-type]
        data,
        uxon_version=version_probe.read_repo_version(),
        host_stats=host_stats,  # type: ignore[arg-type]
    )
    if compact:
        print(json.dumps(env, sort_keys=False))
    else:
        print(json.dumps(env, indent=2, sort_keys=False))


def print_list(
    cfg: Config, sessions: list[SessionInfo], scope_users: list[str], show_user: bool = False
) -> int:
    if not sessions:
        if show_user:
            print(f"uxon: no {cfg.session_prefix}* sessions for users: {', '.join(scope_users)}")
        else:
            print(f"uxon: no {cfg.session_prefix}* sessions for {scope_users[0]}")
        return 0

    rows: list[dict[str, str]] = []
    for s in sessions:
        short = (
            s.name[len(cfg.session_prefix) :] if s.name.startswith(cfg.session_prefix) else s.name
        )
        marker = "*" if s.attached == "1" else " "
        pid_s = str(s.active_pid) if s.active_pid is not None else "-"
        # A session whose workload resource is down shows a distinct
        # "down" marker in cpu/ram rather than a silent idle 0/— (AC-P1.8).
        if s.runtime_down:
            cpu_s = "down"
            ram_s = "down"
        else:
            cpu_s = format_cpu_pct(s.cpu_pct)
            ram_s = format_rss_kib(s.rss_kib)
        start_s = compact_time(s.created)
        last_s = compact_time(s.last_attached)
        # For a command-runtime session the active pane command is the runtime
        # client (``docker``/``sh``), not the agent — show the resolved agent
        # id (AC-P1.4). Gated on the runtime-resource marker; direct-runtime
        # sessions unchanged.
        cmd_s = (s.agent if s.runtime_resource else s.active_cmd) or "-"
        path_s = s.active_path or "-"
        rows.append(
            {
                "user": s.user,
                "id": f"{marker}{short}",
                "pid": pid_s,
                "cpu": cpu_s,
                "ram": ram_s,
                "new": start_s,
                "last": last_s,
                "cmd": cmd_s,
                "path": path_s,
            }
        )

    user_w = max(4, max(len(r["user"]) for r in rows)) if show_user else 0
    id_w = max(2, max(len(r["id"]) for r in rows))
    pid_w = max(3, max(len(r["pid"]) for r in rows))
    cpu_w = max(3, max(len(r["cpu"]) for r in rows))
    ram_w = max(3, max(len(r["ram"]) for r in rows))
    cmd_w = max(3, max(len(r["cmd"]) for r in rows))
    attached_count = sum(1 for s in sessions if s.attached == "1")
    total_cpu_pct = sum(s.cpu_pct for s in sessions)
    total_ram_kib = sum(s.rss_kib for s in sessions)
    if show_user:
        scope = f" users={','.join(scope_users)}"
    else:
        scope = f" user={scope_users[0]}"
    print(
        "uxon:"
        f"{scope}"
        f" sessions={len(rows)}"
        f" attached={attached_count}"
        f" cpu={format_cpu_pct(total_cpu_pct)}"
        f" ram={format_rss_kib(total_ram_kib)}"
    )
    if show_user:
        print(
            f"{'USER':<{user_w}}  {'ID':<{id_w}}  {'PID':<{pid_w}}  {'CPU':>{cpu_w}}  "
            f"{'RAM':>{ram_w}}  {'NEW':<5}  {'LAST':<5}  {'CMD':<{cmd_w}}  PATH"
        )
        for row in rows:
            print(
                f"{row['user']:<{user_w}}  {row['id']:<{id_w}}  {row['pid']:<{pid_w}}  "
                f"{row['cpu']:>{cpu_w}}  {row['ram']:>{ram_w}}  {row['new']:<5}  "
                f"{row['last']:<5}  {row['cmd']:<{cmd_w}}  {row['path']}"
            )
    else:
        print(
            f"{'ID':<{id_w}}  {'PID':<{pid_w}}  {'CPU':>{cpu_w}}  {'RAM':>{ram_w}}  {'NEW':<5}  {'LAST':<5}  {'CMD':<{cmd_w}}  PATH"
        )
        for row in rows:
            print(
                f"{row['id']:<{id_w}}  {row['pid']:<{pid_w}}  {row['cpu']:>{cpu_w}}  "
                f"{row['ram']:>{ram_w}}  {row['new']:<5}  {row['last']:<5}  "
                f"{row['cmd']:<{cmd_w}}  {row['path']}"
            )

    print()
    print("(*) attached in tmux now")
    print("attach: uxon attach <id|pid>")
    print("kill:   uxon kill <id|pid> [--dry-run]")
    return 0
