# SPDX-License-Identifier: MIT
"""uxon: readable wrapper for terminal AI coding agent sessions."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uxon.app.agent_select as agent_select
import uxon.app.launch as launch_app
import uxon.app.repeat as repeat_app
from uxon.domain.args import SUBCOMMANDS, USAGE, ParsedArgs
from uxon.domain.authz import canonical, is_under_allowed_roots
from uxon.domain.config import (
    DEFAULT_CONFIG,
    Config,
)
from uxon.domain.constants import VALID_AGENT_IDS
from uxon.domain.format import (
    compact_time,
    fmt_epoch,
    format_cpu_pct,
    format_rss_kib,
)
from uxon.domain.session import (
    SessionInfo,
    allocate_session_name,
    choose_attach_session,
    compatible_indexed_sessions,
    parse_session_name,
    session_stem_for_path,
    session_stem_for_worktree,
)
from uxon.domain.version import format_version as _format_version_str
from uxon.errors import eprint, fail
from uxon.infra import (
    config_loader,
    git,
    identity,
    process,
    sessions_probe,
    tmux,
    version_probe,
)
from uxon.infra.worktrees import compute_worktree_path

if TYPE_CHECKING:
    from uxon.domain.sudo import SudoCapability

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

# TUI-context types are imported lazily inside the four functions that
# construct them at runtime (`sessions_probe._read_server_status`, `sessions_probe._read_ssh_link_health_status`,
# `_to_tui_session`, `_build_tui_context`). Module-load of `uxon.cli` no longer
# pulls `uxon.tui.context` (~90 ms saved on `uxon version` / `uxon list`).
if TYPE_CHECKING:
    from uxon.domain.session import TuiSession
    from uxon.tui.context import TuiContext


def _sanitize_callback_stderr(raw: str) -> str:
    """Strip boilerplate (``uxon:`` prefix, trailing blank lines) from
    captured stderr so it reads cleanly on a TUI status line.

    Keeps multi-line lists intact (e.g. allowed-roots bullets) with their
    indentation normalised to two spaces. Called by
    :func:`_wrap_tui_callback`.
    """
    out: list[str] = []
    for line in raw.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("uxon:   - "):
            out.append("  - " + line[len("uxon:   - ") :])
        elif line.startswith("uxon: "):
            out.append(line[len("uxon: ") :])
        else:
            out.append(line)
    return "\n".join(out)


def _wrap_tui_callback(fn: Any, callback_error_cls: type[Exception]) -> Any:
    """Wrap a callback so exceptions surface on the TUI status line.

    Captures anything the callback writes to ``stderr`` (e.g. the message
    :func:`fail` prints before ``raise SystemExit``), and on exception
    raises ``callback_error_cls`` with the captured text as its payload.
    A plain return is passed through untouched.

    This is the single place that converts uxon's ``fail() → SystemExit``
    style into a structured error the TUI can render in red without the
    blessed fullscreen context swallowing the message.
    """
    import contextlib
    import io as _io

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        buf = _io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                return fn(*args, **kwargs)
        except SystemExit as exc:
            msg = _sanitize_callback_stderr(buf.getvalue())
            if not msg:
                code = exc.code if exc.code is not None else "?"
                msg = f"command exited with code {code}"
            raise callback_error_cls(msg) from exc
        except callback_error_cls:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            detail = _sanitize_callback_stderr(buf.getvalue())
            head = str(exc) or exc.__class__.__name__
            msg = f"{head}\n{detail}" if detail else head
            raise callback_error_cls(msg) from exc

    wrapper.__name__ = getattr(fn, "__name__", "wrapped_callback")
    return wrapper


def is_new_project_target_allowed(cfg: Config, launch_user: str, project_dir: str) -> bool:
    """Return True if ``project_dir`` may be created by ``uxon new``.

    Variant of :func:`is_launch_target_allowed` for the create-new
    flow: the target itself does not exist yet, so we check the
    parent's write access (typically ``cfg.new_project_root``) plus
    the same whitelist policy. With empty ``cfg.allowed_roots`` the
    whitelist is bypassed and a writable parent suffices.
    """
    parent = os.path.dirname(project_dir) or "/"
    if not identity.probe_cwd_writable(launch_user, parent):
        return False
    return is_under_allowed_roots(cfg, project_dir)


def ensure_new_project_target_allowed(cfg: Config, launch_user: str, project_dir: str) -> None:
    """Raise variant of :func:`is_new_project_target_allowed`.

    Splits the failure reasons so the user sees whether the parent is
    unwritable or whether the path is outside ``allowed_roots``.
    """
    parent = os.path.dirname(project_dir) or "/"
    if not identity.probe_cwd_writable(launch_user, parent):
        fail(f"no write access to {parent} for {launch_user}")
    if not is_under_allowed_roots(cfg, project_dir):
        eprint("uxon: new project directory must be under one of:")
        for base in cfg.allowed_roots:
            eprint(f"uxon:   - {base}")
        fail(f"got: {project_dir}")


def _resolve_all_users_scope(cfg: Config, launch_user: str) -> tuple[list[str], list[str]]:
    """Probe per-target sudo and split ``session_users`` into reachable / skipped.

    Returns ``(scope_users, scope_skipped)``:

    - ``scope_users`` = ``launch_user`` plus every user from
      ``identity.resolve_all_session_users(cfg, launch_user)`` that the caller
      can reach via ``sudo -niu <U>``. The list is deterministically
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
    caps = probe_sudo_capability(candidates)
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
    optional shape documented in :class:`uxon.wire_schema.Envelope`.

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
                agent=str(r.get("agent", "claude")),
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
    from uxon.infra.remote_collector import fetch_remote_snapshot
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
    from uxon.infra.remote_collector import fetch_remote_snapshot

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
        cpu_s = format_cpu_pct(s.cpu_pct)
        ram_s = format_rss_kib(s.rss_kib)
        start_s = compact_time(s.created)
        last_s = compact_time(s.last_attached)
        cmd_s = s.active_cmd or "-"
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


def parse_list_args(argv: list[str]) -> ParsedArgs:
    from uxon.infra.audit import extract_correlation_id, set_correlation_id

    corr_id, argv = extract_correlation_id(argv)
    if corr_id:
        set_correlation_id(corr_id)
    all_users = False
    json_out = False
    all_hosts = False
    host: str | None = None
    i = 0
    extras: list[str] = []
    while i < len(argv):
        token = argv[i]
        if token == "--all-users":
            all_users = True
        elif token == "--json":
            json_out = True
        elif token == "--all-hosts":
            all_hosts = True
        elif token == "--host":
            i += 1
            if i >= len(argv):
                fail("--host requires a host name")
            host = argv[i]
        else:
            extras.append(token)
        i += 1
    if extras:
        fail(f"unknown args for list: {' '.join(extras)}")
    if host is not None and all_hosts:
        fail("--host and --all-hosts are mutually exclusive")
    return ParsedArgs(
        action="list",
        all_users=all_users,
        json_output=json_out,
        host=host,
        all_hosts=all_hosts,
        audit_correlation_id=corr_id,
    )


def parse_run_like(argv: list[str], action: str, target_id: str | None = None) -> ParsedArgs:
    parsed = ParsedArgs(action=action, target_id=target_id)
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in ("-w", "--worktree"):
            if parsed.git_remote:
                fail("cannot combine -w with --git-remote")
            i += 1
            if i >= len(argv):
                fail(f"{token} requires a branch value")
            parsed.worktree_branch = argv[i]
        elif token == "--dry-run":
            parsed.dry_run = True
        elif token == "--attach-existing":
            if action != "new":
                fail(f"{token} is only supported with 'new' / '-n'")
            if parsed.repeat_mode == "new":
                fail("cannot combine --attach-existing with --new-session")
            parsed.repeat_mode = "attach"
        elif token == "--new-session":
            if action != "new":
                fail(f"{token} is only supported with 'new' / '-n'")
            if parsed.repeat_mode == "attach":
                fail("cannot combine --new-session with --attach-existing")
            parsed.repeat_mode = "new"
        elif token in ("--dsp", "--dangerously-skip-permissions", "--dap", "-dap", "-dsp"):
            # --dsp is the canonical short form; --dap, -dap, -dsp are legacy synonyms
            if parsed.permission_mode == "auto":
                fail("--dsp and --auto are mutually exclusive")
            parsed.permission_mode = "yolo"
        elif token == "--auto":
            if parsed.permission_mode == "yolo":
                fail("--dsp and --auto are mutually exclusive")
            parsed.permission_mode = "auto"
        elif token == "--agent":
            i += 1
            if i >= len(argv):
                fail("--agent requires an id (claude|codex|cursor)")
            value = argv[i]
            if value not in VALID_AGENT_IDS:
                fail(f"--agent must be one of {VALID_AGENT_IDS}, got {value!r}")
            parsed.agent = value
        elif token == "--git-remote":
            if action != "new":
                fail(f"{token} is only supported with 'new' / '-n'")
            if parsed.no_git:
                fail("cannot combine --git-remote with --no-git")
            if parsed.worktree_branch:
                fail("cannot combine --git-remote with -w")
            i += 1
            if i >= len(argv):
                fail(f"{token} requires a profile name (or 'default')")
            parsed.git_remote = argv[i]
        elif token == "--no-git":
            if action != "new":
                fail(f"{token} is only supported with 'new' / '-n'")
            if parsed.git_remote:
                fail("cannot combine --no-git with --git-remote")
            parsed.no_git = True
        elif token == "--git-visibility":
            if action != "new":
                fail(f"{token} is only supported with 'new' / '-n'")
            i += 1
            if i >= len(argv):
                fail(f"{token} requires 'private' or 'public'")
            value = argv[i]
            if value not in ("private", "public"):
                fail(f"{token} must be 'private' or 'public', got {value!r}")
            parsed.git_visibility = value
        else:
            parsed.agent_args.append(token)
        i += 1
    return parsed


def _parse_kill_extras(rest: list[str], target_id: str) -> ParsedArgs:
    """Parse the arg tail of ``uxon kill <id> [...]``.

    Shared between the subcommand form and the ``-k`` / ``--kill`` short
    form so both surfaces accept exactly the same flag set.

    Recognised flags:
        --dry-run        : print the would-be argv (or SSH command),
                           do not execute.
        --force          : skip the interactive confirmation prompt.
        --json           : emit a wire-schema envelope on stdout.
        --user <name>    : kill a session belonging to a different
                           launch user (per-target NOPASSWD required).
        --host <alias>   : route the kill to a configured remote peer
                           over SSH.

    Unknown flags fail loudly. Returns a fully populated
    :class:`ParsedArgs` with ``action="kill"``.
    """
    from uxon.infra.audit import extract_correlation_id, set_correlation_id

    corr_id, rest = extract_correlation_id(rest)
    if corr_id:
        set_correlation_id(corr_id)
    dry = False
    force = False
    json_out = False
    user: str | None = None
    host: str | None = None
    extras: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--dry-run":
            dry = True
        elif token == "--force":
            force = True
        elif token == "--json":
            json_out = True
        elif token == "--user":
            i += 1
            if i >= len(rest):
                fail("--user requires a name")
            user = rest[i]
        elif token == "--host":
            i += 1
            if i >= len(rest):
                fail("--host requires a host name")
            host = rest[i]
        else:
            extras.append(token)
        i += 1
    if extras:
        fail(f"unknown args for kill: {' '.join(extras)}")
    return ParsedArgs(
        action="kill",
        target_id=target_id,
        dry_run=dry,
        force=force,
        json_output=json_out,
        user=user,
        host=host,
        audit_correlation_id=corr_id,
    )


def _parse_attach_extras(rest: list[str], target_id: str) -> ParsedArgs:
    """Parse the arg tail of ``uxon attach <id> [...]``.

    Symmetric to :func:`_parse_kill_extras` but without
    ``--all-users``, ``--json``, ``--force``. ``--host`` requires
    ``--user`` — implicit peer-login-user defaults invite
    "where did this attach actually go?" surprises.
    """
    from uxon.infra.audit import extract_correlation_id, set_correlation_id

    corr_id, rest = extract_correlation_id(rest)
    if corr_id:
        set_correlation_id(corr_id)
    dry = False
    user: str | None = None
    host: str | None = None
    extras: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--dry-run":
            dry = True
        elif token == "--user":
            i += 1
            if i >= len(rest):
                fail("--user requires a name")
            user = rest[i]
        elif token == "--host":
            i += 1
            if i >= len(rest):
                fail("--host requires a host name")
            host = rest[i]
        else:
            extras.append(token)
        i += 1
    if extras:
        fail(f"unknown args for attach: {' '.join(extras)}")
    if host is not None and user is None:
        fail(
            "attach --host requires --user (peer owns authorisation; "
            "pass the target user explicitly)"
        )
    return ParsedArgs(
        action="attach",
        target_id=target_id,
        dry_run=dry,
        user=user,
        host=host,
        audit_correlation_id=corr_id,
    )


def parse_subcommand(argv: list[str]) -> ParsedArgs:
    cmd = argv[0]
    if cmd == "version":
        json_out = "--json" in argv[1:]
        extras = [a for a in argv[1:] if a != "--json"]
        if extras:
            fail(f"unknown args for version: {' '.join(extras)}")
        return ParsedArgs(action="version", json_output=json_out)
    if cmd == "doctor":
        json_out = "--json" in argv[1:]
        # Stage 10c — opt-in ``--remote`` flag walks back the
        # AGENTS.md "doctor doesn't probe remote_hosts" rule under
        # explicit operator gesture. See ``do_doctor`` for the
        # rationale + the AGENTS.md addendum.
        all_remote = "--remote" in argv[1:]
        extras = [a for a in argv[1:] if a not in {"--json", "--remote"}]
        if extras:
            fail(f"unknown args for doctor: {' '.join(extras)}")
        # Reuse ``all_hosts`` as the bool carrier — adding a separate
        # field for one flag isn't worth widening ``ParsedArgs``.
        return ParsedArgs(action="doctor", json_output=json_out, all_hosts=all_remote)
    if cmd == "run":
        return parse_run_like(argv[1:], "run")
    if cmd == "list":
        return parse_list_args(argv[1:])
    if cmd == "kill-all":
        dry = "--dry-run" in argv[1:]
        force = "--force" in argv[1:]
        json_out = "--json" in argv[1:]
        extras = [a for a in argv[1:] if a not in {"--dry-run", "--force", "--json"}]
        if extras:
            fail(f"unknown args for kill-all: {' '.join(extras)}")
        return ParsedArgs(action="kill-all", dry_run=dry, force=force, json_output=json_out)
    if cmd in ("attach", "kill"):
        if len(argv) < 2:
            fail(f"{cmd} requires an identifier")
        target = argv[1]
        if cmd == "kill":
            return _parse_kill_extras(argv[2:], target)
        return _parse_attach_extras(argv[2:], target)
    if cmd == "new":
        if len(argv) < 2:
            fail("new requires a name")
        name = argv[1]
        return parse_run_like(argv[2:], "new", target_id=name)
    fail(f"unknown subcommand: {cmd}")
    raise AssertionError("unreachable")


def parse_args(argv: list[str]) -> ParsedArgs:
    if not argv:
        if identity.is_interactive_tty():
            return ParsedArgs(action="interactive")
        print(USAGE)
        raise SystemExit(0)
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        raise SystemExit(0)
    if argv[0] in ("-V", "--version"):
        json_out = "--json" in argv[1:]
        extras = [a for a in argv[1:] if a != "--json"]
        if extras:
            fail(f"unknown args for version: {' '.join(extras)}")
        return ParsedArgs(action="version", json_output=json_out)
    if argv[0] in ("-l", "--list"):
        return parse_list_args(argv[1:])
    if argv[0] in ("-a", "--attach"):
        if len(argv) < 2:
            fail("attach requires an identifier")
        return _parse_attach_extras(argv[2:], argv[1])
    if argv[0] in ("-k", "--kill"):
        if len(argv) < 2:
            fail("kill requires an identifier")
        return _parse_kill_extras(argv[2:], argv[1])
    if argv[0] in ("--killall",):
        dry = "--dry-run" in argv[1:]
        force = "--force" in argv[1:]
        json_out = "--json" in argv[1:]
        extras = [a for a in argv[1:] if a not in {"--dry-run", "--force", "--json"}]
        if extras:
            fail(f"unknown args for kill-all: {' '.join(extras)}")
        return ParsedArgs(action="kill-all", dry_run=dry, force=force, json_output=json_out)
    if argv[0] in ("-n", "--new"):
        if len(argv) < 2:
            fail("new requires a name")
        return parse_run_like(argv[2:], "new", target_id=argv[1])
    if argv[0] in SUBCOMMANDS:
        return parse_subcommand(argv)
    if not argv[0].startswith("-"):
        fail(f"unknown command: {argv[0]}\n{USAGE}")
    # Convenience: support `uxon --model sonnet` as run passthrough.
    return parse_run_like(argv, "run")


def _do_attach_remote(args: ParsedArgs, cfg: Config) -> int:
    """Handle ``uxon attach <id> --host <alias> --user <u>``.

    Looks up the configured peer, builds an interactive ssh argv via
    :func:`build_peer_ssh_argv`, and execvp's it. Peer's own
    ``uxon attach --user`` runs the per-target sudo probe, so the
    local side does not need to know the peer's user table.

    The wire command always passes ``--user`` (even when it equals
    the ssh-login-user on the peer): peer is the sole authority on
    'who can attach to what', and we route that decision through
    its own gating. ``--user`` was made required at parse time
    (:func:`_parse_attach_extras`).
    """
    from uxon.infra.remote_collector import (
        DEFAULT_CONNECT_TIMEOUT_SEC,
        build_peer_ssh_argv,
    )
    from uxon.infra.remote_hosts import find_host

    peer = find_host(cfg.remote_hosts, args.host or "")
    if peer is None:
        names = ", ".join(h.name for h in cfg.remote_hosts) or "(none)"
        fail(f"unknown --host {args.host!r}; configured: {names}")
    assert args.user is not None  # parser-enforced
    import uuid as _uuid

    from uxon.infra import audit as _audit

    corr_id = str(_uuid.uuid4())
    _audit.set_correlation_id(corr_id)
    # ``target_id`` MUST come first after the verb: peer-side
    # ``parse_subcommand`` reads ``argv[1]`` as the target, with flags
    # tail-parsed afterwards.  Putting flags first makes the peer parse
    # the flag name as the target and reject the rest.
    remote_cmd = (
        f"{shlex.quote(peer.remote_uxon)} attach {shlex.quote(args.target_id or '')} "
        f"--user {shlex.quote(args.user)} "
        f"--audit-correlation-id {shlex.quote(corr_id)}"
    )
    ssh_argv = build_peer_ssh_argv(
        peer,
        remote_command=remote_cmd,
        allocate_tty=True,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_SEC,
        # Interactive attach is a one-shot connection: the multiplex
        # savings (200-500 ms vs 5-20 ms) are negligible against a
        # human-paced session, while sharing the poller's
        # ControlMaster means a wedged master can hang the user's
        # terminal at ``unix_wait_for_peer``. Force a fresh connection.
        ssh_multiplex="off",
        ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
    )
    # Audit must fire *before* ``os.execvp`` (Bug 7) — once the process
    # image is replaced the cached socket is gone.  ``audit()`` is a
    # non-blocking ``socket.send``, so the kernel buffers the datagram
    # and the data is handed off before we exec.
    _audit.audit(
        "attach.remote.out",
        peer_name=peer.name,
        ssh_alias=peer.ssh_alias,
        target_user=args.user,
        target_session=args.target_id,
        correlation_id=corr_id,
    )
    if args.dry_run:
        print(shlex.join(ssh_argv))
        return 0
    try:
        os.execvp(ssh_argv[0], ssh_argv)
    except Exception as exc:
        _audit.audit(
            "attach.remote.out",
            outcome="error",
            peer_name=peer.name,
            ssh_alias=peer.ssh_alias,
            target_user=args.user,
            target_session=args.target_id,
            correlation_id=corr_id,
            error=str(exc)[:256],
        )
        raise
    return 0  # unreachable


def do_attach(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    if not args.target_id:
        fail("attach requires an identifier")

    from uxon.infra import audit as _audit

    # Remote dispatch: --host routes to a configured peer over SSH.
    # Per-target sudo gating happens on the peer (peer's own
    # 'uxon attach' runs the probe), so the local side does not need
    # to know the peer's user table. Mirrors do_kill --host.
    #
    # Checked *before* the SSH_CONNECTION peer-inbound branch: a
    # caller invoking ``ssh peer1 "uxon attach --host peer2 …"`` is the
    # caller-side leg dispatching onward, not a peer-inbound terminus,
    # and must not emit ``attach.remote.in``.
    if args.host is not None:
        return _do_attach_remote(args, cfg)

    # Bug 6 — peer-inbound branch.  When invoked over SSH the only
    # signal that this is the peer side of an ``attach.remote.out`` is
    # ``SSH_CONNECTION`` in the env (sudo strips it on the next leg, so
    # we have to capture it before the sudo execvp below).  Spec line
    # 299: ``attach.remote.in`` *replaces* ``session.attach`` on the
    # peer side — both names describe the same physical event from
    # caller-vs-peer POV.
    #
    # The spec also requires (line 207-209) that state-changing events
    # emit on **both** the success and failure paths.  We honour that
    # for the peer side too: instead of a single ``outcome=ok`` emit at
    # the top, every ``session.attach`` emission point below switches
    # event name (``attach.remote.in``) and identifier-field name
    # (``target_session`` instead of ``session``) when ``peer_inbound``.
    # An auditor querying ``EVENT=attach.remote.in OUTCOME=denied``
    # then actually finds the failure.
    peer_inbound = bool(os.environ.get("SSH_CONNECTION"))
    _attach_event: str = "attach.remote.in" if peer_inbound else "session.attach"
    _session_field: str = "target_session" if peer_inbound else "session"

    target_user = args.user or launch_user
    if target_user != launch_user:
        from uxon.infra.sudo_probe import probe_sudo_capability

        caps = probe_sudo_capability([target_user])
        if target_user not in caps.reachable_users:
            _audit.audit(
                _attach_event,
                outcome="denied",
                **{_session_field: args.target_id or ""},
                target_user=target_user,
            )
            eprint(
                f"uxon-error: not-reachable (cannot sudo -niu {target_user}; "
                "check /etc/sudoers.d for a NOPASSWD rule for this target)"
            )
            return 1
        sessions = sessions_probe.collect_sessions([target_user], cfg)
        target = sessions_probe._resolve_or_audit_not_found(
            args.target_id,
            sessions,
            cfg,
            audit_event=_attach_event,
            target_user=target_user,
            session_field=_session_field,
        )
        base = tmux.configured_tmux_base(cfg, target_user) + ["attach-session", "-t", target.name]
        full = ["sudo", "-niu", target_user, "--", *base]
        if args.dry_run:
            print(f"attach_user={shlex.quote(target_user)}")
            print(f"socket={shlex.quote(tmux.tmux_socket_path(cfg, target_user))}")
            print(f"session={shlex.quote(target.name)}")
            print(f"exec {shlex.join(full)}")
            return 0
        # Audit before ``os.execvp`` (Bug 7) — once the image is
        # replaced our cached socket is gone.
        _audit.audit(
            _attach_event,
            **{_session_field: target.name},
            target_user=target_user,
        )
        try:
            os.execvp(full[0], full)
        except Exception as exc:
            _audit.audit(
                _attach_event,
                outcome="error",
                **{_session_field: target.name},
                target_user=target_user,
                error=str(exc)[:256],
            )
            raise
        return 0

    # Same-user path.
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    if not sessions:
        legacy = sessions_probe.collect_sessions_for_user(
            launch_user,
            cfg.session_prefix,
            socket_path=None,
            legacy_prefixes=cfg.legacy_session_prefixes,
        )
        if legacy:
            fail(
                f"no sessions found on dedicated socket {tmux.tmux_socket_path(cfg, launch_user)}, "
                f"but legacy default-socket sessions still exist. Use 'uxon doctor' for details."
            )
    target = sessions_probe._resolve_or_audit_not_found(
        args.target_id,
        sessions,
        cfg,
        audit_event=_attach_event,
        target_user=launch_user,
        session_field=_session_field,
    )
    # Same-user audit fires once before ``attach_session``'s execvp.
    # Emitting from ``do_attach`` (not ``attach_session``) keeps the
    # call exactly once per CLI invocation; the helper is also used by
    # the TUI's ``attach_session_blocking`` and we don't want to double
    # up there.
    _audit.audit(
        _attach_event,
        **{_session_field: target.name},
        target_user=launch_user,
    )
    try:
        return attach_session(target, cfg, launch_user, args.dry_run)
    except Exception as exc:
        _audit.audit(
            _attach_event,
            outcome="error",
            **{_session_field: target.name},
            target_user=launch_user,
            error=str(exc)[:256],
        )
        raise


def _tui_launch_request_cls() -> type:
    """Lazy-load ``LaunchRequest`` from ``uxon.domain.launch_request`` (pure
    data; no textual import). Kept as a function so the module-top import
    surface of cli.py stays small."""
    from uxon.domain.launch_request import LaunchRequest

    return LaunchRequest


def _session_name_from_launch_label(label: str) -> str:
    """Thin wrapper so cli.py call sites keep their local symbol.

    Helper lives next to LaunchRequest (``uxon.domain.launch_request``) so
    the TUI run-loop can reuse it for ``session.ended`` without a circular
    dep on cli.py.
    """
    from uxon.domain.launch_request import session_name_from_launch_label

    return session_name_from_launch_label(label)


def attach_session(
    target: SessionInfo, cfg: Config, launch_user: str, dry_run: bool = False
) -> int:
    req = tmux._build_tmux_attach_request(target, cfg, launch_user)
    if dry_run:
        print(f"attach_user={shlex.quote(launch_user)}")
        print(f"socket={shlex.quote(tmux.tmux_socket_path(cfg, launch_user))}")
        print(f"session={shlex.quote(target.name)}")
        print(f"exec {shlex.join(req.cmd)}")
        return 0
    os.execvp(req.cmd[0], list(req.cmd))
    return 0


def attach_session_blocking(target: SessionInfo, cfg: Config, launch_user: str) -> int:
    """Fork-and-wait variant of :func:`attach_session` for the TUI path."""
    req = tmux._build_tmux_attach_request(target, cfg, launch_user)
    for pre in req.prelaunch:
        rc = subprocess.call(list(pre))
        if rc != 0:
            return rc
    return subprocess.call(list(req.cmd))


def _confirm_kill_or_fail(prompt: str, args: ParsedArgs) -> None:
    """Common confirmation gate for cross-user / cross-host kills.

    ``--json`` is non-interactive — refuse unless ``--force`` or
    ``--dry-run`` was passed (mirrors the ``kill-all`` precedent).
    On a TTY without ``--force``, prompt for the literal phrase
    ``kill``. Non-TTY without ``--force`` fails fast with a hint.
    """
    if args.force or args.dry_run:
        return
    if args.json_output:
        fail("kill --json requires --force or --dry-run")
    if not identity.is_interactive_tty():
        fail(
            "kill is destructive; rerun with --force, or omit --user/--host for the local self path"
        )
    response = input(f"{prompt} Type 'kill' to confirm: ")
    if response.strip() != "kill":
        fail("cancelled", 130)


def _do_kill_remote(args: ParsedArgs, cfg: Config) -> int:
    """Handle ``uxon kill <id> --host <alias>`` (optionally with ``--user``).

    Looks up the configured peer, optionally confirms with the user
    locally, then dispatches the kill to the peer over SSH. The
    peer's own ``uxon kill`` does the per-target sudo gating, so
    the local side does not need to know the peer's user table —
    this matches the design constraint that bulk destructive ops
    stay local while per-session kill may cross hosts.

    Confirmation shape mirrors :func:`do_kill` for the local case:
    ``--json`` requires ``--force`` or ``--dry-run``; an interactive
    TTY without ``--force`` prompts for the literal phrase ``kill``.

    On the wire we always pass ``--force`` to the peer — local
    confirmation is a UI gesture, not a wire concern; the peer
    must not re-prompt.
    """
    from uxon.infra.remote_collector import (
        DEFAULT_CONNECT_TIMEOUT_SEC,
        DEFAULT_TOTAL_TIMEOUT_SEC,
        _recover_wedged_master,
        build_peer_ssh_argv,
    )
    from uxon.infra.remote_hosts import find_host

    if not cfg.remote_hosts:
        fail("no [[remote_hosts]] configured; --host requires at least one peer")
    target_host = find_host(cfg.remote_hosts, args.host or "")
    if target_host is None:
        names = ", ".join(h.name for h in cfg.remote_hosts) or "<none>"
        fail(f"unknown --host {args.host!r}; configured: {names}")

    target_user_part = f" (user={args.user})" if args.user else ""
    prompt = f"Kill {args.target_id}@{target_host.name}{target_user_part}?"
    _confirm_kill_or_fail(prompt, args)

    # ``target_id`` MUST come first after the verb: peer-side
    # ``parse_subcommand`` reads ``argv[1]`` as the target, flags are
    # tail-parsed afterwards.  Mirrors ``_do_attach_remote`` ordering.
    remote_cmd_parts = [
        shlex.quote(target_host.remote_uxon),
        "kill",
        shlex.quote(str(args.target_id)),
        "--force",
    ]
    if args.user:
        remote_cmd_parts.extend(["--user", shlex.quote(args.user)])
    if args.json_output:
        remote_cmd_parts.append("--json")
    # Correlation-id append must precede the join.  ``_do_kill_remote``
    # uses ``subprocess.run`` (not ``os.execvp``), so there is no Bug 7
    # process-replacement concern here — the audit emit is correct
    # anywhere before the run.
    import uuid as _uuid

    from uxon.infra import audit as _audit

    corr_id = str(_uuid.uuid4())
    _audit.set_correlation_id(corr_id)
    remote_cmd_parts.extend(["--audit-correlation-id", shlex.quote(corr_id)])
    remote_cmd = " ".join(remote_cmd_parts)
    _audit.audit(
        "kill.remote.out",
        peer_name=target_host.name,
        ssh_alias=target_host.ssh_alias,
        target_user=args.user,
        target_session=args.target_id,
        force=args.force,
        dry_run=args.dry_run,
        correlation_id=corr_id,
    )
    ssh_argv = build_peer_ssh_argv(
        target_host,
        remote_command=remote_cmd,
        allocate_tty=False,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_SEC,
        ssh_multiplex=cfg.ssh_multiplex,
        ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
    )

    if args.dry_run:
        if args.json_output:
            _emit_json_with_host(
                "kill",
                {
                    "target": args.target_id,
                    "target_user": args.user,
                    "action": "would-kill",
                    "dry_run": True,
                    "ssh_argv": ssh_argv,
                },
                host=target_host.name,
            )
        else:
            print(f"dry-run: {shlex.join(ssh_argv)}")
        return 0

    def _emit_kill_remote_error(error: str, rc: int) -> None:
        _audit.audit(
            "kill.remote.out",
            outcome="error",
            peer_name=target_host.name,
            ssh_alias=target_host.ssh_alias,
            target_user=args.user,
            target_session=args.target_id,
            force=args.force,
            dry_run=args.dry_run,
            correlation_id=corr_id,
            rc=rc,
            error=error[:256],
        )

    try:
        cp = subprocess.run(
            ssh_argv,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TOTAL_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        # Same wedge-recovery as the polling path — without it a CLI
        # ``uxon kill --host`` invoked when no TUI is running has no
        # other consumer to drive recovery, and every retry will hang
        # identically until the master is killed by hand. See
        # ``fetch_remote_snapshot._run_one`` for the rationale.
        if cfg.ssh_multiplex != "off":
            _recover_wedged_master(target_host)
        _emit_kill_remote_error("ssh timeout", 124)
        eprint(f"uxon: --host {target_host.name}: ssh timeout after {DEFAULT_TOTAL_TIMEOUT_SEC}s")
        return 1
    except FileNotFoundError:
        _emit_kill_remote_error("ssh binary missing", 127)
        eprint("uxon: ssh not installed on local host")
        return 1

    if cp.stdout:
        sys.stdout.write(cp.stdout)
    if cp.stderr:
        sys.stderr.write(cp.stderr)
    if cp.returncode != 0:
        _emit_kill_remote_error("non-zero ssh rc", cp.returncode)
        return 1
    return 0


def do_kill(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    if not args.target_id:
        fail("kill requires an identifier")

    from uxon.infra import audit as _audit

    # Remote dispatch: --host routes to a configured peer over SSH.
    # Per-target sudo gating happens on the peer (its own ``uxon kill``
    # runs the probe), so the local side does not need to know the
    # peer's user table. Bulk kill stays strictly local.
    #
    # Checked *before* the SSH_CONNECTION peer-inbound branch: a chained
    # ``ssh peer1 "uxon kill --host peer2 …"`` invocation is the
    # caller-side dispatch leg, not a peer-inbound terminus.
    if args.host is not None:
        return _do_kill_remote(args, cfg)

    # Bug 6 — peer-inbound branch.  Same shape as ``do_attach`` above.
    # ``correlation_id`` is auto-injected by ``audit()`` from module
    # state (the parser layer set it via ``set_correlation_id`` after
    # popping ``--audit-correlation-id`` from argv).  Spec line 302:
    # ``kill.remote.in`` *replaces* ``session.kill`` for the peer-side
    # branch.  Spec line 207-209: state-changing events emit on **both**
    # success and failure paths; we honour that on the peer side too by
    # switching the event name at every emit point (rather than the old
    # single ``outcome=ok`` emit at the top, which lost the failure
    # signal for sudo-denied / not-found / process.run_cmd-error paths).  Per
    # spec line 225, ``kill.remote.in`` shares the ``session`` key with
    # ``session.kill`` — only the event name differs, no field rename.
    peer_inbound = bool(os.environ.get("SSH_CONNECTION"))
    _kill_event: str = "kill.remote.in" if peer_inbound else "session.kill"

    # Local cross-user kill: --user X where X != launch_user requires
    # per-target NOPASSWD. Probe once for the single target (the same
    # probe machinery the TUI uses on startup, but a single-target
    # subset). Matches the TUI's per-target sudo gating.
    target_user = args.user or launch_user
    if target_user != launch_user:
        from uxon.infra.sudo_probe import probe_sudo_capability

        caps = probe_sudo_capability([target_user])
        reachable = target_user in caps.reachable_users
        if not reachable:
            _audit.audit(
                _kill_event,
                outcome="denied",
                session=args.target_id or "",
                target_user=target_user,
                force=args.force,
                dry_run=args.dry_run,
            )
            # Stable error tag — mirrors the ``all-users-disabled``
            # precedent. Callers (and the SSH peer-aggregator) parse
            # this exact substring. Surface the verdict on dry-run too:
            # without sudo we cannot resolve the session name, so the
            # honest answer is "this would fail" rather than a faked
            # would-kill envelope.
            eprint(
                f"uxon-error: not-reachable (cannot sudo -niu {target_user}; "
                "check /etc/sudoers.d for a NOPASSWD rule for this target)"
            )
            return 1

        prompt = f"Kill {args.target_id} (user={target_user})?"
        _confirm_kill_or_fail(prompt, args)

        sessions = sessions_probe.collect_sessions([target_user], cfg)
        target = sessions_probe._resolve_or_audit_not_found(
            args.target_id,
            sessions,
            cfg,
            audit_event=_kill_event,
            target_user=target_user,
            extra={"force": args.force, "dry_run": args.dry_run},
        )
        # Non-interactive sudo: there's no TTY in the kill path even
        # for the CLI; if NOPASSWD is missing we want a fast failure
        # rather than a blocked password prompt.
        full = tmux.configured_tmux_base(cfg, target_user, nonint=True) + [
            "kill-session",
            "-t",
            target.name,
        ]
        if args.dry_run:
            _audit.audit(
                _kill_event,
                session=target.name,
                target_user=target_user,
                force=args.force,
                dry_run=True,
            )
            if args.json_output:
                _emit_json(
                    "kill",
                    {
                        "target": target.name,
                        "user": launch_user,
                        "target_user": target_user,
                        "reachable": reachable,
                        "socket": tmux.tmux_socket_path(cfg, target_user),
                        "action": "would-kill",
                        "dry_run": True,
                    },
                )
            else:
                print(f"dry-run: {shlex.join(full)}")
            return 0
        try:
            process.run_cmd(full, check=True)
        except subprocess.CalledProcessError as exc:
            _audit.audit(
                _kill_event,
                outcome="error",
                session=target.name,
                target_user=target_user,
                force=args.force,
                dry_run=args.dry_run,
                rc=exc.returncode,
            )
            raise
        _audit.audit(
            _kill_event,
            session=target.name,
            target_user=target_user,
            force=args.force,
            dry_run=args.dry_run,
        )
        if args.json_output:
            _emit_json(
                "kill",
                {
                    "target": target.name,
                    "user": launch_user,
                    "target_user": target_user,
                    "reachable": True,
                    "socket": tmux.tmux_socket_path(cfg, target_user),
                    "action": "killed",
                    "dry_run": False,
                },
            )
        else:
            print(f"killed: {target.name}")
        return 0

    # Self-only path: unchanged from the pre-3.4.0 behaviour.
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    target = sessions_probe._resolve_or_audit_not_found(
        args.target_id,
        sessions,
        cfg,
        audit_event=_kill_event,
        target_user=launch_user,
        extra={"force": args.force, "dry_run": args.dry_run},
    )
    full = tmux.configured_tmux_base(cfg, launch_user) + ["kill-session", "-t", target.name]
    if args.dry_run:
        _audit.audit(
            _kill_event,
            session=target.name,
            target_user=launch_user,
            force=args.force,
            dry_run=True,
        )
        if args.json_output:
            _emit_json(
                "kill",
                {
                    "target": target.name,
                    "user": launch_user,
                    "socket": tmux.tmux_socket_path(cfg, launch_user),
                    "action": "would-kill",
                    "dry_run": True,
                },
            )
        else:
            print(f"dry-run: {shlex.join(full)}")
        return 0
    try:
        process.run_cmd(full, check=True)
    except subprocess.CalledProcessError as exc:
        _audit.audit(
            _kill_event,
            outcome="error",
            session=target.name,
            target_user=launch_user,
            force=args.force,
            dry_run=args.dry_run,
            rc=exc.returncode,
        )
        raise
    _audit.audit(
        _kill_event,
        session=target.name,
        target_user=launch_user,
        force=args.force,
        dry_run=args.dry_run,
    )
    if args.json_output:
        _emit_json(
            "kill",
            {
                "target": target.name,
                "user": launch_user,
                "socket": tmux.tmux_socket_path(cfg, launch_user),
                "action": "killed",
                "dry_run": False,
            },
        )
    else:
        print(f"killed: {target.name}")
    return 0


def do_kill_all(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    if not sessions:
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.kill_all",
            target_users=[launch_user],
            killed_count=0,
            dry_run=args.dry_run,
        )
        if args.json_output:
            _emit_json(
                "kill-all",
                {
                    "user": launch_user,
                    "socket": tmux.tmux_socket_path(cfg, launch_user),
                    "dry_run": args.dry_run,
                    "sessions": [],
                },
            )
        else:
            print(f"uxon: no {cfg.session_prefix}* sessions for {launch_user}")
        return 0
    if not args.dry_run and not args.force:
        if args.json_output:
            # --json is a non-interactive surface; we never prompt with
            # JSON enabled. Force the caller to be explicit.
            fail("kill-all --json requires --force or --dry-run")
        if not identity.is_interactive_tty():
            fail(
                "kill-all is destructive; rerun with --force, or use 'uxon list' / 'uxon doctor' first"
            )
        names = ", ".join(s.name for s in sessions)
        response = input(
            f"Kill all {len(sessions)} session(s) on {tmux.tmux_socket_path(cfg, launch_user)}: {names}\nType 'kill-all' to confirm: "
        )
        if response.strip() != "kill-all":
            fail("cancelled", 130)
    results: list[dict[str, Any]] = []
    for s in sessions:
        full = tmux.configured_tmux_base(cfg, launch_user) + ["kill-session", "-t", s.name]
        if args.dry_run:
            if not args.json_output:
                print(f"dry-run: {shlex.join(full)}")
            results.append({"name": s.name, "action": "would-kill"})
            continue
        cp = process.run_cmd(full, check=False)
        ok = cp.returncode == 0
        if not args.json_output:
            print(f"killed: {s.name}" if ok else f"failed: {s.name}")
        results.append({"name": s.name, "action": "killed" if ok else "failed"})
    if args.json_output:
        _emit_json(
            "kill-all",
            {
                "user": launch_user,
                "socket": tmux.tmux_socket_path(cfg, launch_user),
                "dry_run": args.dry_run,
                "sessions": results,
            },
        )
    from uxon.infra import audit as _audit

    killed = sum(1 for r in results if r["action"] == "killed")
    attempted = sum(1 for r in results if r["action"] in ("killed", "failed"))
    _audit.audit(
        "session.kill_all",
        outcome="ok" if killed == attempted else "error",
        target_users=[launch_user],
        killed_count=killed,
        dry_run=args.dry_run,
    )
    return 0


def do_new(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    name = args.target_id
    if not name:
        fail("new requires a name")
    if "/" in name or name in (".", ".."):
        fail(f"invalid name: {name}")
    project_dir = canonical(os.path.join(cfg.new_project_root, name))
    ensure_new_project_target_allowed(cfg, launch_user, project_dir)
    branch = args.worktree_branch
    if branch:
        if not os.path.isdir(project_dir):
            fail(
                "new -w requires an existing project directory: "
                f"{project_dir} (create it first with 'uxon -n {name}')"
            )
        repo_root = git.git_repo_root_as_user(project_dir, launch_user)
        if not repo_root:
            fail(
                "new -w requires a git repository (checked as launch user "
                f"{launch_user}) in {project_dir}"
            )
        # Normalise worktree-from-worktree to the primary repo (§8).
        primary = git.git_common_dir_root_as_user(project_dir, launch_user)
        if primary:
            repo_root = primary
        launch_app.ensure_launch_target_allowed(cfg, launch_user, repo_root)
        _agent = agent_select.resolve_agent_id(
            cfg, launch_user, args.agent, report=args.host_report
        )
        args.agent = _agent
        # uxon-managed worktree sessions live AT the worktree path (§2.5),
        # so both the stem and the compatibility root are derived from the
        # worktree, not the repo root.
        session_stem = session_stem_for_worktree(repo_root, branch)
        compatibility_root = compute_worktree_path(
            repo_root=repo_root, branch=branch, worktree_root=cfg.worktree_root
        )
        target_desc = f"{repo_root} (worktree {branch})"
        sessions = sessions_probe.collect_sessions([launch_user], cfg)
        existing = compatible_indexed_sessions(
            session_stem,
            _agent,
            compatibility_root,
            sessions,
            prefix=cfg.session_prefix,
            legacy_prefixes=cfg.legacy_session_prefixes,
        )
        if existing:
            attach_target = choose_attach_session(
                existing,
                session_stem,
                _agent,
                prefix=cfg.session_prefix,
                legacy_prefixes=cfg.legacy_session_prefixes,
            )
            decision = repeat_app.resolve_repeat_decision(
                args.repeat_mode, cfg, target_desc, attach_target, existing
            )
            if decision == "attach":
                from uxon.infra import audit as _audit

                _audit.audit(
                    "session.attach",
                    session=attach_target.name,
                    target_user=launch_user,
                )
                return attach_session(attach_target, cfg, launch_user, args.dry_run)
        # No existing session, or decision == "new": create + launch via the
        # single worktree planner (gates the path, runs git worktree add,
        # copies includes, emits worktree.create + session.new, Task 11).
        req = launch_app.plan_worktree_launch(
            cfg,
            launch_user,
            repo_root,
            branch,
            _agent,
            args.permission_mode,
            agent_args=args.agent_args,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            print(f"launch_user={shlex.quote(launch_user)}")
            print(f"exec {shlex.join(req.cmd)}")
            return 0
        for pre in req.prelaunch:
            process.run_cmd(list(pre))
        os.execvp(req.cmd[0], list(req.cmd))
        return 0

    target_dir = project_dir
    if args.dry_run:
        mkdir_cmd = identity.command_prefix_for_user(launch_user) + ["mkdir", "-p", target_dir]
        print(f"mkdir= {shlex.join(mkdir_cmd)}")
    else:
        process.run_cmd(identity.command_prefix_for_user(launch_user) + ["mkdir", "-p", target_dir])
    session_stem = session_stem_for_path(target_dir)
    compatibility_root = target_dir
    target_desc = target_dir
    if args.git_remote:
        _do_create_git_remote(args, cfg, launch_user, project_dir, name, branch)

    _agent = agent_select.resolve_agent_id(cfg, launch_user, args.agent, report=args.host_report)
    # See ``do_run``: pin resolved id back to args so the downstream
    # assembler does not re-derive it from cfg.default_agent.
    args.agent = _agent
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    existing = compatible_indexed_sessions(
        session_stem,
        _agent,
        compatibility_root,
        sessions,
        prefix=cfg.session_prefix,
        legacy_prefixes=cfg.legacy_session_prefixes,
    )
    if existing:
        attach_target = choose_attach_session(
            existing,
            session_stem,
            _agent,
            prefix=cfg.session_prefix,
            legacy_prefixes=cfg.legacy_session_prefixes,
        )
        decision = repeat_app.resolve_repeat_decision(
            args.repeat_mode, cfg, target_desc, attach_target, existing
        )
        if decision == "attach":
            # Same physical operation as ``do_attach`` for an existing
            # session — emit the same event before ``attach_session``'s
            # execvp (Bug 7 — audit fires before the image is replaced).
            from uxon.infra import audit as _audit

            _audit.audit(
                "session.attach",
                session=attach_target.name,
                target_user=launch_user,
            )
            try:
                return attach_session(attach_target, cfg, launch_user, args.dry_run)
            except Exception as exc:
                _audit.audit(
                    "session.attach",
                    outcome="error",
                    session=attach_target.name,
                    target_user=launch_user,
                    error=str(exc)[:256],
                )
                raise
    else:
        sessions_probe.repeat_guardrail_for_legacy_socket(
            cfg, launch_user, session_stem, compatibility_root
        )
    session = allocate_session_name(
        session_stem, _agent, compatibility_root, sessions, prefix=cfg.session_prefix
    )
    from uxon.infra import audit as _audit

    _audit.audit(
        "session.new",
        agent=_agent,
        project=target_dir,
        branch=branch or "",
        session=session,
        dry_run=args.dry_run,
    )
    try:
        return tmux.launch_in_tmux(
            target_dir, session, args, cfg, branch, launch_user, server_running=bool(sessions)
        )
    except Exception as exc:
        _audit.audit(
            "session.new",
            outcome="error",
            agent=_agent,
            project=target_dir,
            branch=branch or "",
            session=session,
            dry_run=args.dry_run,
            error=str(exc)[:256],
        )
        raise


def _do_create_git_remote(
    args: ParsedArgs,
    cfg: Config,
    launch_user: str,
    project_dir: str,
    repo_name: str,
    branch: str | None,
) -> None:
    """Resolve the selected profile and drive the creation orchestrator.

    Fails (via :func:`fail`) on invalid combinations — the CLI is
    strictly non-interactive, so mismatches are surfaced as errors
    rather than prompts.
    """
    # Callers gate on ``if args.git_remote:`` before dispatching here.
    assert args.git_remote is not None, "_do_create_git_remote called without --git-remote"
    git_remote_selector = args.git_remote
    if branch:
        fail("--git-remote is not supported together with -w <branch>")
    if not cfg.git_create_enabled:
        fail(
            "git_create_enabled=false in config; either flip it on in "
            "config/config.toml or drop --git-remote"
        )
    if not cfg.git_remote_profiles:
        fail(
            "no git_remote_profiles configured; add at least one "
            "[[git_remote_profiles]] entry to config/config.toml"
        )

    from uxon.gitremote import create as uxon_git_create
    from uxon.gitremote import profiles as uxon_git_profiles

    try:
        profile = uxon_git_profiles.resolve_profile_selector(
            cfg.git_remote_profiles,
            git_remote_selector,
            cfg.default_git_remote_profile,
        )
    except uxon_git_profiles.ProfileError as exc:
        fail(str(exc))

    if args.git_visibility:
        profile = uxon_git_profiles.GitRemoteProfile(
            name=profile.name,
            host=profile.host,
            owner=profile.owner,
            auth=profile.auth,
            creds_user=profile.creds_user,
            token_file=profile.token_file,
            visibility=args.git_visibility,
        )

    current_user = identity.process_user()
    from uxon.infra import audit as _audit

    _git_ok = False
    try:
        result = uxon_git_create.create_project_remote(
            profile,
            repo_name,
            project_dir,
            launch_user=launch_user,
            current_user=current_user,
            dry_run=args.dry_run,
        )
        _git_ok = True
    except uxon_git_create.CreationError as exc:
        # Audit before ``fail()`` re-raises ``SystemExit`` — the operator
        # cares more about the failure than the success.
        _audit.audit(
            "git.remote.create",
            outcome="error",
            profile=profile.name,
            repo=repo_name,
            creds_user=profile.creds_user or launch_user,
            rc=1,
        )
        fail(f"git remote creation failed at stage {exc.stage!r}: {exc}")
    if _git_ok:
        _audit.audit(
            "git.remote.create",
            outcome="ok",
            profile=profile.name,
            repo=repo_name,
            creds_user=profile.creds_user or launch_user,
            rc=0,
        )

    if args.dry_run:
        for cmd in result.commands:
            print(f"git-remote dry-run: {cmd}")
        print(f"git-remote ssh_url={result.ssh_url}")
    else:
        print(f"git remote created: {result.ssh_url}")


def do_run(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    cwd = canonical(os.getcwd())
    launch_app.ensure_launch_target_allowed(cfg, launch_user, cwd)
    branch = args.worktree_branch
    if branch:
        repo_root = git.git_repo_root_nonint_as_user(cwd, launch_user)
        if not repo_root:
            fail(f"run -w must be run inside a git repository readable by {launch_user}")
        # Normalise to the PRIMARY working tree so a worktree-from-worktree
        # anchors to the main repo, not a nested one (§8).
        primary = git.git_common_dir_root_as_user(cwd, launch_user)
        if primary:
            repo_root = primary
        launch_app.ensure_launch_target_allowed(cfg, launch_user, repo_root)
        _agent = agent_select.resolve_agent_id(
            cfg, launch_user, args.agent, report=args.host_report
        )
        args.agent = _agent
        # plan_worktree_launch gates the worktree path, runs git worktree
        # add, copies includes, emits worktree.create + session.new, and
        # returns the launch request. In dry-run it prints the git plan and
        # does no side effects (Task 11).
        req = launch_app.plan_worktree_launch(
            cfg,
            launch_user,
            repo_root,
            branch,
            _agent,
            args.permission_mode,
            agent_args=args.agent_args,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            print(f"launch_user={shlex.quote(launch_user)}")
            print(f"exec {shlex.join(req.cmd)}")
            return 0
        for pre in req.prelaunch:
            process.run_cmd(list(pre))
        os.execvp(req.cmd[0], list(req.cmd))
        return 0
    target_dir = cwd
    session_stem = session_stem_for_path(target_dir)
    compatibility_root = target_dir
    _agent = agent_select.resolve_agent_id(cfg, launch_user, args.agent, report=args.host_report)
    # Pin the resolved id back to ``args.agent`` so the downstream
    # ``tmux._build_tmux_launch_request`` does not re-derive it from
    # ``cfg.default_agent`` (which can disagree with auto-mode pick).
    args.agent = _agent
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    session = allocate_session_name(
        session_stem, _agent, compatibility_root, sessions, prefix=cfg.session_prefix
    )
    from uxon.infra import audit as _audit

    _audit.audit(
        "session.new",
        agent=_agent,
        project=target_dir,
        branch=branch or "",
        session=session,
        dry_run=args.dry_run,
    )
    try:
        return tmux.launch_in_tmux(
            target_dir, session, args, cfg, branch, launch_user, server_running=bool(sessions)
        )
    except Exception as exc:
        _audit.audit(
            "session.new",
            outcome="error",
            agent=_agent,
            project=target_dir,
            branch=branch or "",
            session=session,
            dry_run=args.dry_run,
            error=str(exc)[:256],
        )
        raise


def format_version() -> str:
    """Compose the impure version readers with the pure string builder.

    The display-string construction is owned by
    :func:`uxon.domain.version.format_version`; this composition root
    gathers ``version`` / ``commit`` / ``dirty`` from the (still-impure)
    git/FS readers. Phase 2 relocates the readers to ``infra``.
    """
    version = version_probe.read_repo_version()
    commit = version_probe.read_git_commit_short()
    dirty = version_probe.repo_is_dirty() if commit else False
    return _format_version_str(version, commit, dirty)


def command_path_for_user(command: str, target_user: str) -> str | None:
    cp = subprocess.run(
        identity.command_prefix_for_user(target_user)
        + ["sh", "-lc", f"command -v {shlex.quote(command)}"],
        text=True,
        capture_output=True,
    )
    if cp.returncode != 0:
        return None
    resolved = (cp.stdout or "").strip().splitlines()
    if not resolved:
        return None
    return resolved[0]


def user_can_write_dir(path: str, target_user: str) -> bool:
    cp = subprocess.run(
        identity.command_prefix_for_user(target_user)
        + [
            "python3",
            "-c",
            "import os, sys; raise SystemExit(0 if os.access(sys.argv[1], os.W_OK | os.X_OK) else 1)",
            path,
        ],
        text=True,
        capture_output=True,
    )
    return cp.returncode == 0


def doctor_issues(
    cfg: Config,
    caller_user: str,
    launch_user: str,
    tmux_path: str | None,
    agent_paths: dict[str, str | None],
    socket_path: str,
    current_sessions: list[SessionInfo],
    legacy_sessions: list[SessionInfo],
) -> list[str]:
    issues: list[str] = []
    if cfg.default_launch_mode == "fixed" and not cfg.runtime_user:
        issues.append("default_launch_mode is 'fixed' but runtime_user is empty")
    if not is_under_allowed_roots(cfg, cfg.new_project_root):
        issues.append(f"new_project_root {cfg.new_project_root} is outside allowed_roots")
    socket_parent = str(Path(socket_path).parent)
    if not os.path.isdir(socket_parent):
        issues.append(f"tmux socket parent does not exist yet: {socket_parent}")
    elif not user_can_write_dir(socket_parent, launch_user):
        issues.append(f"launch user {launch_user} cannot write tmux socket parent: {socket_parent}")
    if tmux_path is None:
        issues.append(f"'tmux' is not resolvable for {launch_user}")
    # Strict-whitelist: every enabled agent must resolve. Auto-mode:
    # missing agents are not issues — they're just outside what the
    # user can launch, and the doctor's per-agent table already shows
    # the full installed/missing landscape.
    if cfg.enabled_agents:
        for aid in cfg.enabled_agents:
            if agent_paths.get(aid) is None:
                issues.append(f"'{aid}' agent binary is not resolvable for {launch_user}")
    elif all(path is None for path in agent_paths.values()):
        issues.append(
            f"no agent binary is resolvable for {launch_user} (auto-mode); "
            f"install one of {VALID_AGENT_IDS}"
        )
    if legacy_sessions and not current_sessions:
        issues.append(
            "legacy default-socket uxon sessions exist while the dedicated uxon socket has none"
        )
    if (
        caller_user != launch_user
        and launch_user not in cfg.session_users
        and not cfg.enable_all_users_list
    ):
        issues.append(
            f"launch user {launch_user} is not present in session_users; list --all-users may omit it"
        )
    return issues


def do_doctor(
    cfg: Config,
    caller_user: str,
    launch_user: str,
    cwd: str,
    *,
    json_output: bool = False,
    probe_remote: bool = False,
) -> int:
    from uxon.domain.wire_schema import build_session_records
    from uxon.infra import agents as uxon_agents
    from uxon.infra import probes as uxon_probes

    _, config_sources = config_loader.resolve_config_layers(cwd)
    socket_path = tmux.tmux_socket_path(cfg, launch_user)
    # Single-round-trip probe for tmux + every catalogued agent.
    report = uxon_probes.probe_host(launch_user)
    tmux_path = report.tmux.path
    # Doctor shows every CATALOG agent regardless of strict/auto mode —
    # the operator wants to see the full landscape ("is X installed?")
    # not just the configured whitelist.
    doctor_agent_ids: tuple[str, ...] = tuple(uxon_agents.CATALOG)
    agent_paths: dict[str, str | None] = {
        aid: report.agents[aid].path for aid in doctor_agent_ids if aid in report.agents
    }
    # Per-present-binary version detail. Probes run in parallel with a
    # 2 s per-probe deadline — slow agents (e.g. cold ``cursor-agent``)
    # surface as TIMEOUT instead of inflating doctor's wall time. The
    # host probe above already established presence; ``--version`` is
    # informational.
    import concurrent.futures  # noqa: PLC0415

    def _probe(aid: str) -> tuple[str, uxon_agents.AgentAvailability]:
        if not agent_paths.get(aid):
            return aid, uxon_agents.AgentAvailability(status="missing", error="not on PATH")
        return aid, uxon_agents._probe_one(
            uxon_agents.CATALOG[aid].binary,
            launch_user,
            timeout_override=2.0,
        )

    availability: dict[str, uxon_agents.AgentAvailability] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for aid, result in pool.map(_probe, doctor_agent_ids):
            availability[aid] = result
    current_sessions = sessions_probe.collect_sessions([launch_user], cfg)
    legacy_sessions = sessions_probe.collect_sessions_for_user(
        launch_user,
        cfg.session_prefix,
        socket_path=None,
        legacy_prefixes=cfg.legacy_session_prefixes,
    )
    config_paths = [str(path) for path in config_sources]
    env_repeat_mode = repeat_app.get_env_repeat_noninteractive_mode()
    issues = doctor_issues(
        cfg,
        caller_user,
        launch_user,
        tmux_path,
        agent_paths,
        socket_path,
        current_sessions,
        legacy_sessions,
    )

    if json_output:
        agents_block: dict[str, dict[str, Any]] = {}
        for aid in doctor_agent_ids:
            avail = availability.get(aid)
            agents_block[aid] = {
                "path": agent_paths.get(aid),
                "status": (avail.status if avail else "missing"),
                "version": (avail.version if avail else None),
                "error": (avail.error if avail else None),
            }
        socket_parent = str(Path(socket_path).parent)
        data: dict[str, Any] = {
            "cwd": cwd,
            "caller_user": caller_user,
            "launch_user": launch_user,
            "config_paths": config_paths,
            "allowed_roots": list(cfg.allowed_roots),
            "new_project_root": cfg.new_project_root,
            "repeat_noninteractive_mode": cfg.repeat_noninteractive_mode,
            "repeat_noninteractive_env": env_repeat_mode or None,
            "tmux": {
                "path": tmux_path,
                "socket": socket_path,
                "socket_parent": socket_parent,
                "socket_parent_exists": Path(socket_parent).is_dir(),
                "socket_parent_writable": user_can_write_dir(socket_parent, launch_user),
            },
            "agents": agents_block,
            "current_socket_sessions": build_session_records(
                current_sessions, session_prefix=cfg.session_prefix
            ),
            "legacy_default_socket_sessions": build_session_records(
                legacy_sessions, session_prefix=cfg.session_prefix
            ),
            "git_create_enabled": cfg.git_create_enabled,
            "default_git_remote_profile": cfg.default_git_remote_profile or None,
            "git_remote_profiles": _doctor_git_profile_rows(cfg, launch_user)
            if cfg.git_remote_profiles
            else [],
            "issues": list(issues),
        }
        if probe_remote:
            # Forward-compat addition: ``data.remote_hosts`` only
            # appears under ``--remote``. Default doctor JSON output
            # is unchanged so existing operator scripts that read the
            # envelope keep working.
            data["remote_hosts"] = _doctor_remote_rows(cfg)
        # Audit-channel report (Bug 2).  Operators run ``uxon doctor``
        # to validate the deploy; we surface the resolved sink so
        # "audit isn't reaching journald" is one command away.  Force
        # sink detection by reading ``audit.sink`` after a synthetic
        # touch (so the doctor invocation itself initialises the channel
        # if the operator has not invoked ``cli.start`` first).
        from uxon.infra import audit as _audit

        if not _audit._initialized and _audit.enabled:
            _audit._lazy_init()
        data["audit"] = {"enabled": _audit.enabled, "sink": _audit.sink or "none"}
        _emit_json("doctor", data)
        return 0

    print("uxon doctor")
    print(f"version={format_version()}")
    print(f"cwd={cwd}")
    print(f"caller_user={caller_user}")
    print(f"launch_user={launch_user}")
    print(f"config_paths={', '.join(config_paths) if config_paths else '-'}")
    print(f"allowed_roots={', '.join(cfg.allowed_roots) if cfg.allowed_roots else '-'}")
    print(f"new_project_root={cfg.new_project_root}")
    print(f"repeat_noninteractive_mode={cfg.repeat_noninteractive_mode}")
    print(f"repeat_noninteractive_env={env_repeat_mode or '-'}")
    print(f"tmux_path={tmux_path or '-'}")
    print(f"tmux_socket={socket_path}")
    print(f"tmux_socket_parent={Path(socket_path).parent}")
    print(f"tmux_socket_parent_exists={'yes' if Path(socket_path).parent.is_dir() else 'no'}")
    print(
        f"tmux_socket_parent_writable={'yes' if user_can_write_dir(str(Path(socket_path).parent), launch_user) else 'no'}"
    )
    # Per-agent status block.
    for aid in doctor_agent_ids:
        spec = uxon_agents.CATALOG[aid]
        path = agent_paths.get(aid) or "-"
        avail = availability.get(aid)
        if avail and avail.status == "ok":
            print(f"{aid}:  {path}  ok ({avail.version or '?'})")
        elif avail and avail.status == "timeout":
            print(f"{aid}:  {path}  TIMEOUT (>2.0s)")
        else:
            print(f"{aid}:  -  MISSING  ({spec.install_hint})")
    print(f"current_socket_sessions={len(current_sessions)}")
    if current_sessions:
        print(
            "current_socket_session_names="
            + ", ".join(session.name for session in current_sessions)
        )
    print(f"legacy_default_socket_sessions={len(legacy_sessions)}")
    if legacy_sessions:
        print(
            "legacy_default_socket_session_names="
            + ", ".join(session.name for session in legacy_sessions)
        )
    print(f"git_create_enabled={'yes' if cfg.git_create_enabled else 'no'}")
    print(f"default_git_remote_profile={cfg.default_git_remote_profile or '-'}")
    if cfg.git_remote_profiles:
        print(f"git_remote_profiles={len(cfg.git_remote_profiles)}:")
        for row in _doctor_git_profile_rows(cfg, launch_user):
            print(f"- {row}")
    else:
        print("git_remote_profiles=0")
    # Audit-channel report (Bug 2) — operator-visible verification of
    # the platform-log path.  Force sink detection if it hasn't run yet
    # (``cli.start`` already triggered it for non-doctor invocations,
    # but a stand-alone ``uxon doctor`` may be the first audit-aware
    # call in this process).
    from uxon.infra import audit as _audit

    if not _audit._initialized and _audit.enabled:
        _audit._lazy_init()
    _sink_label = {"journal": "journald-native", "syslog": "syslog", "none": "no-sink"}.get(
        _audit.sink, "no-sink"
    )
    print(f"audit:    {'enabled' if _audit.enabled else 'disabled'}, sink={_sink_label}")
    if issues:
        print("issues:")
        for issue in issues:
            print(f"- {issue}")
    else:
        print("issues: none")
    # Remote-host probes: only when the operator explicitly opts in via
    # ``--remote``. Default ``uxon doctor`` stays local-only per the
    # AGENTS.md contract; the rule has been amended to add "except
    # under --remote" in the same change.
    if probe_remote:
        if not cfg.remote_hosts:
            print("remote_hosts: no remote hosts configured")
        else:
            rows = _doctor_remote_rows(cfg)
            print(f"remote_hosts={len(rows)}:")
            for row in rows:
                if row["ok"]:
                    print(
                        f"- {row['name']}  ok  latency={row['latency_ms']}ms  "
                        f"sessions={row['sessions']}"
                    )
                else:
                    err = (row["error"] or "").splitlines()[0] if row["error"] else "error"
                    print(f"- {row['name']}  err  latency={row['latency_ms']}ms  {err}")
    return 0


def _doctor_remote_rows(cfg: Config) -> list[dict[str, Any]]:
    """Probe each ``[[remote_hosts]]`` peer once for ``uxon doctor --remote``.

    **Deliberate AGENTS.md walk-back**: the project rule "uxon doctor
    does not probe ``[[remote_hosts]]``" stays in force for default
    ``uxon doctor``. This helper runs only when the operator passes
    ``--remote`` — the explicit gesture for fleet health diagnosis.
    The default invocation still has zero SSH I/O.

    Each peer gets one ``ssh ... uxon list --json`` round-trip with the
    fleet-global SSH multiplex setting; per-host overrides on
    ``host.connect_timeout`` / ``host.total_timeout`` are honoured by
    ``fetch_remote_snapshot``. Errors are surfaced (no fail-soft cache
    fallback masking — the operator wants the truth).

    Returns one dict per peer: ``name``, ``ok`` (bool),
    ``latency_ms`` (int), ``error`` (str | None), ``from_cache`` (bool),
    ``sessions`` (int).
    """
    from uxon.infra.remote_collector import fetch_remote_snapshot

    rows: list[dict[str, Any]] = []
    for host in cfg.remote_hosts:
        t0 = time.monotonic()
        snap = fetch_remote_snapshot(
            host,
            ssh_multiplex=cfg.ssh_multiplex,
            ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        rows.append(
            {
                "name": host.name,
                "ok": snap.error is None,
                "latency_ms": latency_ms,
                "error": snap.error,
                "from_cache": bool(snap.from_cache),
                "sessions": len(snap.sessions),
            }
        )
    return rows


def _doctor_git_profile_rows(cfg: Config, launch_user: str) -> list[str]:
    """One status line per profile for ``uxon doctor``. Probes are
    read-only (no repo creation). ``[ok]`` / ``[warn:<reason>]``.
    """
    rows: list[str] = []
    current_user = identity.process_user()
    for p in cfg.git_remote_profiles:
        creds_user = p.creds_user or launch_user
        status = _probe_git_profile(p, creds_user, current_user)
        token_bit = f" token_file={p.token_file}" if p.auth == "token" else ""
        rows.append(
            f"{p.name}  host={p.host}  owner={p.owner}  auth={p.auth}  "
            f"creds_user={creds_user}{token_bit}  status={status}"
        )
    return rows


def _probe_git_profile(profile, creds_user: str, current_user: str) -> str:
    """Non-destructive probe for ``uxon doctor``. Doesn't touch GitHub."""
    # sudo reachability under creds_user
    if creds_user and creds_user != current_user:
        probe = subprocess.run(
            ["sudo", "-n", "-u", creds_user, "--", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=0.5,
        )
        if probe.returncode != 0:
            return f"warn:passwordless sudo to {creds_user} unavailable"

    prefix = (
        ["sudo", "-n", "-u", creds_user, "--"] if creds_user and creds_user != current_user else []
    )
    if profile.auth == "gh":
        which = subprocess.run(
            prefix + ["sh", "-c", "command -v gh"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if which.returncode != 0 or not which.stdout.strip():
            return f"warn:gh not found under {creds_user}"
        status = subprocess.run(
            prefix + ["gh", "auth", "status", "--hostname", profile.host],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if status.returncode != 0:
            return f"warn:gh not logged in to {profile.host}"
        return "ok"
    if profile.auth == "token":
        res = subprocess.run(
            prefix + ["test", "-r", profile.token_file],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        if res.returncode != 0:
            return f"warn:token_file unreadable under {creds_user}"
        return "ok"
    return "warn:unknown auth"


def detect_root_nopasswd() -> bool:
    """Fast non-interactive check for *root* NOPASSWD.

    Returns True if:
      - the process is already root (euid==0), or
      - `sudo -n true` succeeds within a short timeout (NOPASSWD or cached credential).

    We probe with `sudo -n true` rather than `sudo -n -v`: `-v` validates the
    user's credential cache and, in non-interactive mode, fails with "a
    password is required" when the cache is empty — even for users who have
    `NOPASSWD: ALL` in sudoers. Running a trivial command under `-n` honors
    NOPASSWD correctly.

    Timeout is intentionally tight (0.5s) so the TUI never blocks on startup.
    False on timeout / OSError / non-zero exit.

    Used for the Settings-screen writability gate (``sudo tee`` of a
    root-owned config file). The "see other users' sessions" gate is
    now per-target — see :func:`uxon.infra.sudo_probe.probe_sudo_capability`.
    """
    if os.geteuid() == 0:
        return True
    try:
        cp = subprocess.run(
            ["sudo", "-n", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=0.5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return cp.returncode == 0


# Backwards-compatible alias for any out-of-tree caller. The renamed
# :func:`detect_root_nopasswd` is the canonical name; the old name is
# preserved so a stale import doesn't crash ``uxon``. New code must
# use the canonical name (or :func:`uxon.infra.sudo_probe.probe_sudo_capability`
# for the per-target gate).
detect_passwordless_sudo = detect_root_nopasswd


def _list_existing_projects(root: str) -> list[tuple[str, str]]:
    """List ``(name, compact_mtime)`` under ``new_project_root``, sorted by name.

    ``compact_mtime`` uses :func:`compact_time`: ``HH:MM`` if the
    directory was last modified today, ``MM-DD`` otherwise. ``"-"``
    when the stat call fails.
    """
    try:
        entries = [
            (e.name, str(e))
            for e in Path(root).iterdir()
            if e.is_dir() and not e.name.startswith(".")
        ]
    except OSError:
        return []
    entries.sort()
    result: list[tuple[str, str]] = []
    for name, path in entries:
        try:
            mtime = int(os.stat(path).st_mtime)
            mtime_str = compact_time(fmt_epoch(str(mtime)))
        except OSError:
            mtime_str = "-"
        result.append((name, mtime_str))
    return result


def _to_tui_session(
    s: SessionInfo, prefix: str, legacy_prefixes: tuple[str, ...] = ()
) -> TuiSession:
    from uxon.domain.session import TuiSession  # noqa: PLC0415

    short = s.name[len(prefix) :] if s.name.startswith(prefix) else s.name
    for lp in legacy_prefixes:
        if s.name.startswith(lp):
            short = s.name[len(lp) :]
            break
    parsed = parse_session_name(s.name, prefix=prefix, legacy_prefixes=legacy_prefixes)
    if parsed is not None:
        stem, agent, _idx, legacy = parsed
    else:
        stem, agent, legacy = s.name, "unknown", False
    return TuiSession(
        name=s.name,
        short=short,
        attached=s.attached == "1",
        pid=str(s.active_pid) if s.active_pid is not None else "-",
        cpu=format_cpu_pct(s.cpu_pct),
        ram=format_rss_kib(s.rss_kib),
        created=compact_time(s.created),
        last_activity=compact_time(s.last_attached),
        cmd=s.active_cmd or "-",
        path=s.active_path or "-",
        user=s.user,
        stem=stem,
        agent=agent,
        legacy=legacy,
        created_iso=s.created,
        last_attached_iso=s.last_attached,
    )


def _load_settings_sources(cwd: str) -> tuple[dict, dict, Path | None]:
    """Load raw repo + project config data (unmerged) plus the project path.

    Used by the TUI settings screen so it can show each value's origin and
    write back only to the repo-level file.
    """
    repo_cfg = config_loader.repo_config_path()
    repo_data = config_loader.load_toml(repo_cfg)
    seed_allowed = [
        canonical(p) for p in repo_data.get("allowed_roots", DEFAULT_CONFIG["allowed_roots"])
    ]
    proj_cfg = config_loader.find_project_config(cwd, seed_allowed)
    proj_data = config_loader.load_toml(proj_cfg) if proj_cfg else {}
    return repo_data, proj_data, proj_cfg


def _plan_tui_run_agent(
    cfg: Config,
    launch_user: str,
    cwd: str,
    agent_id: str,
    mode_id: str,
    worktree: tuple[str, str] | None = None,
):
    """Build a LaunchRequest for the TUI "New session in current folder" action.

    Mirrors :func:`do_run` minus the terminal handoff: gates via
    :func:`ensure_launch_target_allowed` (writable + ``allowed_roots``
    whitelist when configured), allocates a session name, returns a
    LaunchRequest. The agent and permission mode are picked by the TUI
    callback before this is called — no probe needed here.

    When ``worktree`` is ``(repo_root, branch)`` the session stem is the
    repo-qualified :func:`session_stem_for_worktree` (§2.5) — identical to
    the stem the worktree-aware probe derives — instead of the
    basename-only :func:`session_stem_for_path`. ``cwd`` is the worktree
    path in that case; for a plain (primary / non-git) target ``worktree``
    is ``None`` and the basename stem is used unchanged.
    """
    launch_app.ensure_launch_target_allowed(cfg, launch_user, cwd)
    target_dir = cwd
    if worktree is not None:
        repo_root, branch = worktree
        session_stem = session_stem_for_worktree(repo_root, branch)
    else:
        session_stem = session_stem_for_path(target_dir)
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    session = allocate_session_name(
        session_stem, agent_id, target_dir, sessions, prefix=cfg.session_prefix
    )
    args = ParsedArgs(action="run", agent=agent_id, permission_mode=mode_id)
    return tmux._build_tmux_launch_request(
        target_dir, session, args, cfg, None, launch_user, server_running=bool(sessions)
    )


def _plan_tui_create_new_agent(
    cfg: Config,
    launch_user: str,
    name: str,
    agent_id: str,
    mode_id: str,
    git_profile: str,
):
    """Build a LaunchRequest for the TUI "Create new project" flow.

    Creates the project directory (if missing), optionally creates the
    git remote, and — when a compatible session already exists — forces
    ``attach`` semantics (the TUI cannot safely prompt via stdin inside
    a blessed context). ``git_profile`` is the (possibly empty) name of
    a ``[[git_remote_profiles]]`` entry; when set this calls
    :func:`_do_create_git_remote`. The "Open existing project" flow must
    never call this — see :func:`_plan_tui_open_existing_agent`.
    """
    project_dir = _resolve_tui_project_dir(cfg, launch_user, name)
    args = ParsedArgs(
        action="new",
        target_id=name,
        agent=agent_id,
        permission_mode=mode_id,
        git_remote=git_profile or None,
        repeat_mode="attach",
    )
    if args.git_remote:
        _do_create_git_remote(args, cfg, launch_user, project_dir, name, None)
    return _plan_tui_existing_session_or_launch(cfg, launch_user, project_dir, name, args)


def _plan_tui_open_existing_agent(
    cfg: Config,
    launch_user: str,
    name: str,
    agent_id: str,
    mode_id: str,
):
    """Build a LaunchRequest for the TUI "Open existing project" flow.

    By construction this function has **no** ``git_profile`` parameter
    and never calls :func:`_do_create_git_remote`: opening an existing
    project must not have any git side effect, regardless of
    ``git_create_enabled`` or profile configuration.
    """
    project_dir = _resolve_tui_project_dir(cfg, launch_user, name)
    args = ParsedArgs(
        action="new",
        target_id=name,
        agent=agent_id,
        permission_mode=mode_id,
        git_remote=None,
        repeat_mode="attach",
    )
    return _plan_tui_existing_session_or_launch(cfg, launch_user, project_dir, name, args)


def _resolve_tui_project_dir(cfg: Config, launch_user: str, name: str) -> str:
    """Shared validation + directory creation for both TUI project flows.

    Returns the canonical absolute path; raises via ``fail()`` if ``name``
    is malformed, the parent is not writable, or the path violates a
    non-empty ``allowed_roots`` whitelist.
    """
    if "/" in name or name in (".", ".."):
        fail(f"invalid name: {name}")
    project_dir = canonical(os.path.join(cfg.new_project_root, name))
    ensure_new_project_target_allowed(cfg, launch_user, project_dir)
    process.run_cmd(identity.command_prefix_for_user(launch_user) + ["mkdir", "-p", project_dir])
    return project_dir


def _plan_tui_existing_session_or_launch(
    cfg: Config,
    launch_user: str,
    project_dir: str,
    name: str,
    args: ParsedArgs,
):
    """Allocate + launch a fresh session under ``project_dir``.

    Shared tail of both TUI project flows. The TUI is the sole owner of
    the attach-vs-launch decision now: it probes via
    :func:`sessions_probe.probe_tui_compatible_sessions` after the operator picks
    agent+mode, surfaces the choice in a modal, and routes "attach" to
    :func:`on_attach` directly. By the time this planner runs we already
    know the operator wants a new (parallel) session — the only thing
    left to do is allocate the next available index and emit the launch
    request. :func:`compatible_indexed_sessions` is still called for its
    path-safety side effect (it ``fail()``-s if a same-named session
    points outside ``project_dir``).
    """
    session_stem = session_stem_for_path(project_dir)
    compatibility_root = project_dir
    _agent = agent_select.resolve_agent_id(
        cfg, launch_user, args.agent or None, report=args.host_report
    )
    args.agent = _agent
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    # Path-safety side effect — raises via fail() on a path mismatch.
    compatible_indexed_sessions(
        session_stem,
        _agent,
        compatibility_root,
        sessions,
        prefix=cfg.session_prefix,
        legacy_prefixes=cfg.legacy_session_prefixes,
    )
    sessions_probe.repeat_guardrail_for_legacy_socket(
        cfg, launch_user, session_stem, compatibility_root
    )
    session = allocate_session_name(
        session_stem, _agent, compatibility_root, sessions, prefix=cfg.session_prefix
    )
    return tmux._build_tmux_launch_request(
        project_dir, session, args, cfg, None, launch_user, server_running=bool(sessions)
    )


def _build_on_remote_attach_callback(cfg: Config):
    """Return the TUI on_remote_attach callback for the given cfg.

    Pulled out as a module-level factory so tests can construct it
    with a synthetic Config without spinning up the full
    _build_tui_context closure.
    """
    from uxon.domain.launch_request import LaunchRequest
    from uxon.infra.remote_collector import (
        DEFAULT_CONNECT_TIMEOUT_SEC,
        build_peer_ssh_argv,
    )
    from uxon.infra.remote_hosts import find_host
    from uxon.tui.context import CallbackError

    def on_remote_attach(host_name: str, user: str, name: str) -> LaunchRequest:
        import uuid as _uuid

        from uxon.infra import audit as _audit

        peer = find_host(cfg.remote_hosts, host_name)
        if peer is None:
            raise CallbackError(f"unknown remote host: {host_name}")
        # Pass correlation_id explicitly via kwargs rather than seeding
        # ``_audit._correlation_id``: the TUI process is long-lived, and a
        # left-behind global would leak into subsequent local audit events
        # (next session.attach / session.kill picked up the stale UUID).
        corr_id = str(_uuid.uuid4())
        _audit.audit(
            "attach.remote.out",
            peer_name=peer.name,
            ssh_alias=peer.ssh_alias,
            target_user=user,
            target_session=name,
            correlation_id=corr_id,
        )
        # target first (see _do_attach_remote for rationale).
        remote_cmd = (
            f"{shlex.quote(peer.remote_uxon)} attach {shlex.quote(name)} "
            f"--user {shlex.quote(user)} "
            f"--audit-correlation-id {shlex.quote(corr_id)}"
        )
        argv = build_peer_ssh_argv(
            peer,
            remote_command=remote_cmd,
            allocate_tty=True,
            connect_timeout=DEFAULT_CONNECT_TIMEOUT_SEC,
            # See _do_attach_remote: interactive attach must not share
            # the poller's ControlMaster — a wedged master would hang
            # the TUI handoff at ``unix_wait_for_peer``.
            ssh_multiplex="off",
            ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
        )
        return LaunchRequest(cmd=tuple(argv), label=f"attach {name}@{host_name}")

    return on_remote_attach


def _build_tui_context(
    cfg: Config,
    launch_user: str,
    cwd: str,
    *,
    skeleton: bool = False,
    sudo_caps_override: SudoCapability | None = None,
) -> TuiContext:
    """Build a TuiContext from live session data.

    When ``skeleton=True`` we skip every blocking I/O call (tmux, sudo
    probes, project directory scans) and return a minimal context with
    ``loading=True``. The TUI mounts immediately and a background worker
    calls this function again with ``skeleton=False`` to fill in the
    real data — see :class:`uxon.tui.app.UxonApp._initial_load_worker`.

    ``sudo_caps_override`` lets the caller (typically ``on_refresh``)
    reuse a previously-probed :class:`SudoCapability` instead of
    re-running the probe. Probing is one-shot at startup — the spec
    forbids per-refresh re-probing because new sudo grants are picked
    up by restarting ``uxon``, not by polling. When ``None`` and
    ``skeleton=False``, the function probes once.
    """
    from uxon.domain.status import ServerStatus
    from uxon.domain.sudo import SudoCapability
    from uxon.infra import settings as uxon_settings
    from uxon.infra.sudo_probe import probe_sudo_capability
    from uxon.tui.context import (  # noqa: PLC0415
        CallbackError,
        TuiContext,
    )

    if skeleton:
        # Skeleton ctx skips the per-target probe — it's the fast first
        # frame, and the real probe runs below when the worker calls
        # back with skeleton=False.
        sudo_caps = SudoCapability(reachable_users=frozenset(), can_root=False)
        own: list[SessionInfo] = []
        other: list[SessionInfo] = []
        skipped_users: tuple[str, ...] = ()
    else:
        from uxon import _demo as _uxon_demo_ctx  # noqa: PLC0415

        _demo_dir = _uxon_demo_ctx.demo_hosts_dir()
        if _demo_dir is not None:
            # Single demo seam for local sessions: pull the agent-user
            # scope and per-user records straight from _local.json.
            # Bypasses sudo probe + tmux.tmux_socket_path (the production
            # collectors reject synthetic users that don't exist as
            # OS accounts) and keeps every demo-only branch inside this
            # one block.
            scope_users = _uxon_demo_ctx.load_demo_local_scope_users(_demo_dir)
            own = _uxon_demo_ctx.load_demo_local_sessions(_demo_dir, launch_user)
            other = []
            for _u in scope_users:
                if _u == launch_user:
                    continue
                other.extend(_uxon_demo_ctx.load_demo_local_sessions(_demo_dir, _u))
            sudo_caps = SudoCapability(
                reachable_users=frozenset(u for u in scope_users if u != launch_user),
                can_root=False,
            )
            skipped_users = ()
        else:
            # One-shot probe: the candidate set is ``session_users \ {self}``.
            # Self is filtered before probing because ``sudo -niu <self>``
            # trivially succeeds and would inflate ``reachable_users``
            # with a meaningless entry.
            candidates = [
                u for u in identity.resolve_all_session_users(cfg, launch_user) if u != launch_user
            ]
            if sudo_caps_override is not None:
                sudo_caps = sudo_caps_override
            else:
                sudo_caps = probe_sudo_capability(candidates)
            own = sessions_probe.collect_sessions([launch_user], cfg)

            # Other-user sessions are scoped to the *reachable* subset.
            # Unreachable candidates are surfaced separately so the TUI
            # can show the "(2/4 users reachable)" hint.
            if sudo_caps.reachable_users:
                other = sessions_probe.collect_sessions(sorted(sudo_caps.reachable_users), cfg)
            else:
                other = []
            skipped_users = tuple(
                sorted(u for u in candidates if u not in sudo_caps.reachable_users)
            )
        # Spec line 223: ``list.peek`` fires when the TUI actually
        # enumerates cross-user sessions (gated by ``enable_all_users_list``
        # and ``reachable_users`` being non-empty).  CLI ``uxon list
        # --all-users`` emits its own ``list.peek`` from the list block;
        # the TUI refresh path is the second documented site and was
        # previously silent.
        if cfg.enable_all_users_list and sudo_caps.reachable_users:
            from uxon.infra import audit as _audit

            _audit.audit(
                "list.peek",
                scope_users=sorted({launch_user, *sudo_caps.reachable_users}),
                scope_skipped=list(skipped_users),
            )

        own.sort(key=lambda s: s.name)
        other.sort(key=lambda s: (s.user, s.name))

    tui_own = [_to_tui_session(s, cfg.session_prefix, cfg.legacy_session_prefixes) for s in own]
    tui_other = [_to_tui_session(s, cfg.session_prefix, cfg.legacy_session_prefixes) for s in other]

    total_cpu = format_cpu_pct(sum(s.cpu_pct for s in own) + sum(s.cpu_pct for s in other))
    total_ram = format_rss_kib(sum(s.rss_kib for s in own) + sum(s.rss_kib for s in other))

    home = os.path.expanduser("~")
    cwd_short = cwd.replace(home, "~") if cwd.startswith(home) else cwd

    def on_attach(user: str, name: str):
        # TUI Enter on a local row dispatches a direct
        # ``tmux attach-session`` (no ``uxon`` wrapper) — emit
        # ``session.attach`` here so the operation is auditable.
        # ``do_attach``'s emit only covers the CLI-side ``uxon attach``
        # invocation; the TUI request bypasses that path entirely.
        from uxon.infra import audit as _audit

        fresh = sessions_probe.collect_sessions([user], cfg)
        target = sessions_probe._resolve_or_audit_not_found(
            name,
            fresh,
            cfg,
            audit_event="session.attach",
            target_user=user,
        )
        _audit.audit("session.attach", session=target.name, target_user=user)
        return tmux._build_tmux_attach_request(target, cfg, user)

    def on_kill(user: str, name: str) -> None:
        # TUI 'k' on a local row runs ``tmux kill-session`` directly
        # via ``process.run_cmd`` — emit ``session.kill`` after success so the
        # operation is auditable (mirrors do_kill same-user pattern).
        from uxon.infra import audit as _audit

        fresh = sessions_probe.collect_sessions([user], cfg)
        target = sessions_probe._resolve_or_audit_not_found(
            name,
            fresh,
            cfg,
            audit_event="session.kill",
            target_user=user,
            extra={"force": True, "dry_run": False},
        )
        # TUI-driven kill: no TTY available, use non-interactive sudo.
        full = tmux.configured_tmux_base(cfg, user, nonint=True) + [
            "kill-session",
            "-t",
            target.name,
        ]
        try:
            process.run_cmd(full, check=True)
        except subprocess.CalledProcessError as exc:
            _audit.audit(
                "session.kill",
                outcome="error",
                session=target.name,
                target_user=user,
                force=True,
                dry_run=False,
                rc=exc.returncode,
            )
            raise
        _audit.audit(
            "session.kill",
            session=target.name,
            target_user=user,
            force=True,
            dry_run=False,
        )

    def on_kill_all() -> None:
        # TUI 'D' / kill-all-mine. Mirrors ``on_kill_all_reachable``'s
        # audit shape (``target_users``, ``killed_count``, ``dry_run``)
        # for the single-user case.
        from uxon.infra import audit as _audit

        fresh = sessions_probe.collect_sessions([launch_user], cfg)
        killed_count = 0
        for s in fresh:
            full = tmux.configured_tmux_base(cfg, launch_user, nonint=True) + [
                "kill-session",
                "-t",
                s.name,
            ]
            cp = process.run_cmd(full, check=False)
            if cp.returncode == 0:
                killed_count += 1
        _audit.audit(
            "session.kill_all",
            outcome="ok" if killed_count == len(fresh) else "error",
            target_users=[launch_user],
            killed_count=killed_count,
            dry_run=False,
        )

    def on_remote_kill(host_name: str, user: str, name: str) -> None:
        """TUI dispatch: kill ``name`` belonging to ``user`` on peer ``host_name``.

        Reuses the same SSH gesture as the CLI's ``uxon kill --host
        <alias> --user <user> --force <id>``: the peer's own ``uxon
        kill`` runs the per-target sudo probe, so the local side does
        not need to know the peer's user table. ``--force`` is passed
        on the wire because confirmation is a local-UI concern (the TUI
        already prompted before this callback fires).

        Failures surface as :class:`CallbackError` via the
        ``_wrap_tui_callback`` shim — :meth:`MainScreen.action_kill`
        renders them as a red toast (the dashboard's ``d`` binding
        dispatches here when the cursor sits on a remote row).
        """
        import uuid as _uuid

        from uxon.infra import audit as _audit
        from uxon.infra.remote_collector import (
            DEFAULT_CONNECT_TIMEOUT_SEC,
            DEFAULT_TOTAL_TIMEOUT_SEC,
            _recover_wedged_master,
            build_peer_ssh_argv,
        )
        from uxon.infra.remote_hosts import find_host

        peer = find_host(cfg.remote_hosts, host_name)
        if peer is None:
            fail(f"unknown remote host: {host_name}", 1)
        # See on_remote_attach: TUI process outlives the dispatch, so we
        # avoid the module-level correlation_id global to keep state from
        # bleeding into the next local emit.
        corr_id = str(_uuid.uuid4())
        _audit.audit(
            "kill.remote.out",
            peer_name=peer.name,
            ssh_alias=peer.ssh_alias,
            target_user=user,
            target_session=name,
            force=True,
            dry_run=False,
            correlation_id=corr_id,
        )
        # target first (see _do_attach_remote for rationale).
        remote_cmd = (
            f"{shlex.quote(peer.remote_uxon)} kill {shlex.quote(name)} --force "
            f"--user {shlex.quote(user)} "
            f"--audit-correlation-id {shlex.quote(corr_id)}"
        )
        ssh_argv = build_peer_ssh_argv(
            peer,
            remote_command=remote_cmd,
            allocate_tty=False,
            connect_timeout=DEFAULT_CONNECT_TIMEOUT_SEC,
            ssh_multiplex=cfg.ssh_multiplex,
            ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
        )

        # Mirrors ``_do_kill_remote::_emit_kill_remote_error`` (CLI
        # path) so the TUI and CLI failure trails are symmetric: an
        # operator querying ``EVENT=kill.remote.out OUTCOME=error``
        # finds TUI-originated ssh failures alongside CLI ones.
        def _emit_kill_remote_error(error: str, rc: int) -> None:
            _audit.audit(
                "kill.remote.out",
                outcome="error",
                peer_name=peer.name,
                ssh_alias=peer.ssh_alias,
                target_user=user,
                target_session=name,
                force=True,
                dry_run=False,
                correlation_id=corr_id,
                rc=rc,
                error=error[:256],
            )

        try:
            cp = subprocess.run(
                ssh_argv,
                capture_output=True,
                text=True,
                timeout=DEFAULT_TOTAL_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            if cfg.ssh_multiplex != "off":
                _recover_wedged_master(peer)
            _emit_kill_remote_error("ssh timeout", 124)
            fail(f"ssh timeout after {DEFAULT_TOTAL_TIMEOUT_SEC}s talking to {host_name}", 1)
        except FileNotFoundError:
            _emit_kill_remote_error("ssh binary missing", 127)
            fail("ssh not installed on local host", 1)
        if cp.returncode != 0:
            stderr = (cp.stderr or "").strip().splitlines()
            tail = stderr[-1] if stderr else f"ssh exited {cp.returncode}"
            _emit_kill_remote_error(f"non-zero ssh rc: {tail}", cp.returncode)
            fail(f"remote kill on {host_name} failed: {tail}", 1)

    def on_kill_all_reachable() -> None:
        # Iterate the launch user plus every reachable peer user. An
        # empty ``reachable_users`` collapses to "kill all my own
        # sessions", which is the same behaviour the legacy
        # ``kill-all-global`` had when sudo was unavailable.
        users = sorted({launch_user, *sudo_caps.reachable_users})
        killed_count = 0
        attempted = 0
        for u in users:
            fresh = sessions_probe.collect_sessions([u], cfg)
            for s in fresh:
                full = tmux.configured_tmux_base(cfg, u, nonint=True) + [
                    "kill-session",
                    "-t",
                    s.name,
                ]
                cp = process.run_cmd(full, check=False)
                attempted += 1
                if cp.returncode == 0:
                    killed_count += 1
        # Operationally the most-significant kill_all path: cross-user
        # bulk kill from the TUI.  Audit emit covers the whole sweep,
        # not per-session — matches the spec's `target_users` /
        # `killed_count` shape.
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.kill_all",
            outcome="ok" if killed_count == attempted else "error",
            target_users=users,
            killed_count=killed_count,
            dry_run=False,
        )

    # Legacy alias kept for any out-of-tree caller. The TUI dispatches
    # via ``on_kill_all_global`` (the field name on TuiContext); the
    # implementation now scopes to the reachable set.
    on_kill_all_global = on_kill_all_reachable

    # Capture the caps probed for *this* ctx so subsequent ``on_refresh``
    # calls reuse them. Probing is one-shot at startup (spec § Non-goals
    # "Per-refresh re-probing"); new sudo grants are picked up by
    # restarting uxon, not by polling.
    #
    # Subtlety: a *skeleton* ctx has empty placeholder caps, not real
    # ones. If we captured those, the first real load would reuse the
    # empty placeholder and never probe. So skeleton's on_refresh
    # passes None, which forces the probe on the first non-skeleton
    # load. Every refresh after that reuses the captured real caps.
    captured_sudo_caps: SudoCapability | None = None if skeleton else sudo_caps

    def on_refresh() -> TuiContext:
        # Re-read config so settings edits take effect immediately.
        # Always returns a fully loaded ctx (skeleton=False) — even when
        # the calling ctx was a skeleton, the caller wants real data.
        # We pass the captured caps (or None on the very first load)
        # so the probe runs at most once per process.
        fresh_cfg = config_loader.load_config(cwd)
        return _build_tui_context(
            fresh_cfg, launch_user, cwd, sudo_caps_override=captured_sudo_caps
        )

    def on_probe_link_health() -> object | None:
        return sessions_probe._read_ssh_link_health_status()

    # ── Settings bindings (superuser-only; safe to wire unconditionally) ──
    def get_settings_entries() -> list:
        repo_data, proj_data, proj_cfg = _load_settings_sources(cwd)
        return uxon_settings.resolve_setting_entries(repo_data, proj_data, proj_cfg, DEFAULT_CONFIG)

    def on_setting_save(key: str, value: object) -> None:
        uxon_settings.persist_repo_config_updates(config_loader.repo_config_path(), {key: value})

    def on_setting_remove(key: str) -> None:
        uxon_settings.remove_repo_key(config_loader.repo_config_path(), key)

    def on_setting_save_mapping(key: str, mapping: dict) -> None:
        uxon_settings.persist_repo_config_updates(config_loader.repo_config_path(), {key: mapping})

    def get_git_remote_profile_rows() -> list:
        return [
            (
                p.name,
                p.host,
                p.owner,
                p.auth,
                p.creds_user or launch_user,
                p.visibility,
                p.token_file or "-",
            )
            for p in cfg.git_remote_profiles
        ]

    def on_launch_cwd(agent_id: str, mode_id: str):
        req = _plan_tui_run_agent(cfg, launch_user, cwd, agent_id, mode_id)
        # ``_plan_tui_run_agent`` only ever yields a launch (never an
        # attach), so this path is unconditional ``session.new``.
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.new",
            agent=agent_id,
            project=cwd,
            branch="",
            session=_session_name_from_launch_label(req.label),
            dry_run=False,
        )
        return req

    def on_launch_new(name: str, agent_id: str, mode_id: str, git_profile: str):
        req = _plan_tui_create_new_agent(cfg, launch_user, name, agent_id, mode_id, git_profile)
        # The TUI planner no longer auto-attaches — every launch request
        # routed here is a fresh ``session.new``. The attach path is
        # owned by ``on_attach`` (which emits its own ``session.attach``
        # event when the operator picks "attach" in SessionChoiceScreen).
        project = canonical(os.path.join(cfg.new_project_root, name))
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.new",
            agent=agent_id,
            project=project,
            branch="",
            session=_session_name_from_launch_label(req.label),
            dry_run=False,
        )
        return req

    def on_launch_existing(name: str, agent_id: str, mode_id: str):
        req = _plan_tui_open_existing_agent(cfg, launch_user, name, agent_id, mode_id)
        # Same as ``on_launch_new``: TUI owns attach decisions; this path
        # always emits ``session.new``.
        project = canonical(os.path.join(cfg.new_project_root, name))
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.new",
            agent=agent_id,
            project=project,
            branch="",
            session=_session_name_from_launch_label(req.label),
            dry_run=False,
        )
        return req

    def on_probe_existing_sessions(target_dir: str, agent_id: str) -> tuple[tuple[str, bool], ...]:
        """TUI probe: return (name, attached) pairs for launch_user's
        compatible sessions under ``target_dir`` + ``agent_id``.

        Called by the TUI between LaunchOptionsScreen (agent + mode pick)
        and the actual ``on_launch_*`` commit. An empty tuple means the
        TUI proceeds straight to launch; otherwise it pushes the
        SessionChoiceScreen modal to let the operator pick attach vs
        new-alongside.
        """
        matches = sessions_probe.probe_tui_compatible_sessions(
            cfg, launch_user, target_dir, agent_id
        )
        return tuple((s.name, s.attached == "1") for s in matches)

    def on_probe_worktrees(cwd_arg: str) -> list:
        """Workspaces for ``cwd_arg``'s repo (folders only). Non-git → [].

        Resolves ``cwd`` → primary repo root with the NON-interactive
        resolvers (Task 5) so the fullscreen TUI never blocks on a hidden
        ``sudo`` prompt, then lists worktrees under the same
        ``identity.nonint_command_prefix_for_user`` and parses with Task 2.
        """
        from uxon.infra.worktrees import parse_worktree_porcelain

        repo_root = git.git_repo_root_nonint_as_user(cwd_arg, launch_user)
        if not repo_root:
            return []
        primary = git.git_common_dir_root_as_user(cwd_arg, launch_user)
        if primary:
            repo_root = primary
        cp = subprocess.run(
            identity.nonint_command_prefix_for_user(launch_user)
            + ["git", "-C", repo_root, "worktree", "list", "--porcelain"],
            text=True,
            capture_output=True,
        )
        if cp.returncode != 0:
            return []
        return parse_worktree_porcelain(cp.stdout or "", repo_root=repo_root)

    def on_create_worktree(repo_root: str, branch: str, agent_id: str, mode_id: str):
        # plan_worktree_launch emits its own worktree.create + session.new
        # audit events. The TUI has no agent passthrough args (agent_args
        # defaults to None).
        return launch_app.plan_worktree_launch(
            cfg, launch_user, repo_root, branch, agent_id, mode_id
        )

    def on_launch_existing_worktree(
        repo_root: str, branch: str, worktree_path: str, agent_id: str, mode_id: str
    ):
        # Launch into an EXISTING worktree with the worktree-aware stem
        # (§2.5) — never re-creates the worktree.
        req = _plan_tui_run_agent(
            cfg,
            launch_user,
            worktree_path,
            agent_id,
            mode_id,
            worktree=(repo_root, branch),
        )
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.new",
            agent=agent_id,
            project=worktree_path,
            branch=branch,
            session=_session_name_from_launch_label(req.label),
            dry_run=False,
        )
        return req

    def on_probe_existing_worktree_sessions(
        worktree_path: str, repo_root: str, branch: str, agent_id: str
    ) -> tuple[tuple[str, bool], ...]:
        matches = sessions_probe.probe_tui_compatible_sessions(
            cfg,
            launch_user,
            worktree_path,
            agent_id,
            stem=session_stem_for_worktree(repo_root, branch),
            compatibility_root=worktree_path,
        )
        return tuple((s.name, s.attached == "1") for s in matches)

    git_profile_options = [
        (
            p.name,
            f"{p.host}/{p.owner}  via {p.creds_user or launch_user} [{p.auth}]",
        )
        for p in cfg.git_remote_profiles
    ]

    # Reflects whether the "new session in current folder" row should be
    # enabled — same predicate the click handler will apply, so the row
    # state never lies. Same-user fast path runs synchronously (os.access
    # under the hood); cross-user case leaves the value None so the TUI
    # ships the first frame fast and an app worker probes via sudo
    # without blocking the event loop.
    if identity.process_user() == launch_user:
        cwd_writable: bool | None = launch_app.is_launch_target_allowed(cfg, launch_user, cwd)
    else:
        cwd_writable = None

    def on_probe_cwd_writable() -> bool:
        return launch_app.is_launch_target_allowed(cfg, launch_user, cwd)

    def on_probe_dir_launchable(target_dir: str) -> bool:
        # Same predicate as on_probe_cwd_writable, parameterised by target —
        # gates the "Open existing project" launch (no pre-probed slot).
        return launch_app.is_launch_target_allowed(cfg, launch_user, target_dir)

    # Wrap all callbacks so failures surface on the TUI status line instead of
    # killing uxon silently (blessed's fullscreen context hides stderr + tracebacks).
    _CbErr = CallbackError
    on_attach = _wrap_tui_callback(on_attach, _CbErr)
    on_kill = _wrap_tui_callback(on_kill, _CbErr)
    on_kill_all = _wrap_tui_callback(on_kill_all, _CbErr)
    on_kill_all_global = _wrap_tui_callback(on_kill_all_global, _CbErr)
    on_remote_kill = _wrap_tui_callback(on_remote_kill, _CbErr)
    # on_remote_attach already raises CallbackError directly (see
    # _build_on_remote_attach_callback) — no _wrap_tui_callback shim
    # needed.
    on_remote_attach = _build_on_remote_attach_callback(cfg)
    on_refresh = _wrap_tui_callback(on_refresh, _CbErr)
    on_probe_link_health = _wrap_tui_callback(on_probe_link_health, _CbErr)
    on_probe_cwd_writable = _wrap_tui_callback(on_probe_cwd_writable, _CbErr)
    on_probe_dir_launchable = _wrap_tui_callback(on_probe_dir_launchable, _CbErr)
    on_launch_cwd = _wrap_tui_callback(on_launch_cwd, _CbErr)
    on_launch_new = _wrap_tui_callback(on_launch_new, _CbErr)
    on_launch_existing = _wrap_tui_callback(on_launch_existing, _CbErr)
    on_probe_existing_sessions = _wrap_tui_callback(on_probe_existing_sessions, _CbErr)
    on_probe_worktrees = _wrap_tui_callback(on_probe_worktrees, _CbErr)
    on_create_worktree = _wrap_tui_callback(on_create_worktree, _CbErr)
    on_launch_existing_worktree = _wrap_tui_callback(on_launch_existing_worktree, _CbErr)
    on_probe_existing_worktree_sessions = _wrap_tui_callback(
        on_probe_existing_worktree_sessions, _CbErr
    )
    get_settings_entries = _wrap_tui_callback(get_settings_entries, _CbErr)
    on_setting_save = _wrap_tui_callback(on_setting_save, _CbErr)
    on_setting_remove = _wrap_tui_callback(on_setting_remove, _CbErr)
    on_setting_save_mapping = _wrap_tui_callback(on_setting_save_mapping, _CbErr)
    get_git_remote_profile_rows = _wrap_tui_callback(get_git_remote_profile_rows, _CbErr)

    from uxon.infra import agents as _uxon_agents

    agent_availability = {
        aid: _uxon_agents.AgentAvailability(status="pending") for aid in cfg.enabled_agents
    }

    if skeleton:
        existing_projects: list[tuple[str, str]] = []
        server_status = ServerStatus()
    else:
        existing_projects = _list_existing_projects(cfg.new_project_root)
        server_status = sessions_probe._read_server_status(cfg.new_project_root)

    # Pluggable refresh sources. PR1 ships a single source that wraps
    # ``on_refresh()`` so the existing kick-refresh path runs through the
    # registry — same wall behaviour, but now extensible. PR3 adds one
    # source per configured remote host alongside this one.
    #
    # The skeleton ctx still gets the full source list. SourceSpec
    # construction is pure (just stores names + lambdas), no I/O, so
    # there is no cost to wiring it on the fast-path. The ctx is what
    # ``MainScreen.on_mount`` reads to fan out the initial refresh —
    # an empty list there means the "Loading sessions…" placeholder
    # never gets replaced.
    from uxon.infra.host_breaker import BreakerSpec, HostBreaker
    from uxon.infra.remote_collector import (
        RemoteSnapshot,
        fetch_remote_snapshot,
        read_cached_snapshot,
    )
    from uxon.tui.refresh import SourceSpec

    # ``main_ctx_rebuild`` returns a fresh ``TuiContext``. The app's
    # source-result handler routes this into ``apply_loaded_ctx``,
    # which is the same swap-or-recompose path the legacy
    # ``_MainCtxLoaded`` message used.
    # The lambda captures ``on_refresh`` by name; by the time the
    # registry runs the fetch on a worker thread, ``on_refresh`` has
    # already been replaced (a few lines above) by its
    # ``_wrap_tui_callback`` shim. So a SystemExit / ``fail()`` from
    # inside the rebuild surfaces as ``CallbackError``, which
    # ``run_source`` captures into ``SourceResult.error`` for
    # fail-soft delivery.
    refresh_sources: list = [
        SourceSpec(
            name="main_ctx_rebuild",
            fetch=lambda: on_refresh(),
            cadence_seconds_attr="tui_refresh_interval_seconds",
            kick_on_mount=True,
        ),
    ]
    # One source per configured remote host. Each runs in its own
    # worker group (``refresh:remote:<name>``) so a slow / dead
    # peer can never stall the local-sessions stream or another
    # peer's poll. Cadence is the dedicated SSH interval — peers
    # are polled less aggressively than the local tmux stream.
    # Fleet-wide fetch-concurrency cap. Without this a 50-host peer
    # set recovering from an outage launches 50 concurrent
    # ``subprocess.Popen`` calls (each holding ≥3 pipe FDs), which
    # saturates the default 1024-FD ulimit before scheduling becomes
    # the bottleneck. Scope is per-``TuiContext`` instance — matches
    # the spec's "no worker survives App teardown" contract.
    import threading as _threading

    _fetch_sem = _threading.Semaphore(cfg.fetch_concurrency)

    # Per-host circuit breaker. One :class:`HostBreaker` per peer,
    # keyed by host name and captured by ``_make_remote_fetch``. The
    # breaker decides whether an SSH attempt fires; when open, the
    # fetcher short-circuits to a cache-only snapshot so the UI keeps
    # rendering the last good payload without the cost of yet another
    # doomed connect. ``BreakerSpec`` defaults are intentional — no
    # new config knobs in this commit. Wiring a per-host override is a
    # follow-up.
    _host_breakers: dict[str, HostBreaker] = {}

    def _make_remote_fetch(h, sem, multiplex, persist_seconds, breaker):
        def _fetch():
            # Breaker is the first gate: if it says "do not attempt",
            # skip the SSH layer entirely. We still produce a
            # ``RemoteSnapshot`` so the UI sees something — load the
            # last good cache if we have one; otherwise an empty
            # error snapshot. Either way the cadence-driven retry
            # path will get its next chance the moment the breaker
            # half-opens.
            if not breaker.should_attempt():
                cached = read_cached_snapshot(h.name)
                if cached is not None:
                    return cached
                return RemoteSnapshot(
                    host_name=h.name,
                    fetched_at_epoch=0.0,
                    from_cache=False,
                    error="circuit breaker open",
                    sessions=[],
                    cached_at_epoch=None,
                )
            breaker.mark_inflight()
            try:
                sem.acquire()
                try:
                    snap = fetch_remote_snapshot(
                        h,
                        ssh_multiplex=multiplex,
                        ssh_control_persist_seconds=persist_seconds,
                    )
                finally:
                    sem.release()
                # Translate the snapshot's success/failure into the
                # breaker's outcome reporting. A cache-fallback
                # snapshot (``from_cache=True``, ``error=<live>``)
                # means the live fetch did not succeed — count as
                # failure. ``error is None and not from_cache`` is a
                # real live success.
                if snap.error is None and not snap.from_cache:
                    breaker.on_success()
                else:
                    breaker.on_failure()
                return snap
            finally:
                # Defence in depth: if ``fetch_remote_snapshot`` ever
                # propagates (KeyboardInterrupt mid-tick, a test mock
                # that raises), the breaker must NOT stay in_flight=True
                # forever — that would permanently block the host's
                # next probe. ``on_success`` / ``on_failure`` already
                # clear the gate as a safety net, but the contract is
                # this finally.
                breaker.clear_inflight()

        return _fetch

    for host in cfg.remote_hosts:
        _host_breakers[host.name] = HostBreaker(BreakerSpec())
        # Per-host cadence: ``host.interval`` (if set) wins over the
        # fleet-global ``tui_ssh_refresh_interval_seconds``. We pass
        # cadence_seconds_attr=None so the timer reads the explicit
        # value and does not fall through to the legacy attribute path.
        host_cadence = (
            float(host.interval)
            if host.interval is not None
            else float(cfg.tui_ssh_refresh_interval_seconds)
        )
        refresh_sources.append(
            SourceSpec(
                name=f"remote:{host.name}",
                fetch=_make_remote_fetch(
                    host,
                    _fetch_sem,
                    cfg.ssh_multiplex,
                    cfg.ssh_control_persist_seconds,
                    _host_breakers[host.name],
                ),
                cadence_seconds_attr=None,
                cadence_seconds=host_cadence,
                kick_on_mount=True,
            )
        )

    # Local /proc snapshot for the HostStatusBar's locals bucket. Skip
    # on the skeleton tick (first frame must paint immediately); on the
    # real refresh tick treat probe failure as "pending…" — the
    # selector renders the absence rather than the error.
    host_stats: Any = None
    if not skeleton:
        try:
            from uxon.infra.probes import read_host_stats

            host_stats = read_host_stats()
        except Exception:  # pragma: no cover — defensive
            host_stats = None

    return TuiContext(
        sessions=tui_own,
        total_cpu=total_cpu,
        total_ram=total_ram,
        version=format_version(),
        cwd=cwd,
        cwd_short=cwd_short,
        new_project_root=cfg.new_project_root,
        existing_projects=existing_projects,
        server_status=server_status,
        loading=skeleton,
        host_stats=host_stats,
        tui_refresh_interval_seconds=cfg.tui_refresh_interval_seconds,
        tui_ssh_refresh_interval_seconds=cfg.tui_ssh_refresh_interval_seconds,
        ssh_multiplex=cfg.ssh_multiplex,
        ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
        fetch_concurrency=cfg.fetch_concurrency,
        cwd_writable=cwd_writable,
        current_user=launch_user,
        sudo_caps=sudo_caps,
        scope_skipped_users=skipped_users,
        other_sessions=tui_other,
        enabled_agents=cfg.enabled_agents,
        default_agent=cfg.default_agent,
        launch_user=launch_user,
        agent_availability=agent_availability,
        on_attach=on_attach,
        on_kill=on_kill,
        on_kill_all=on_kill_all,
        on_kill_all_global=on_kill_all_global,
        on_remote_kill=on_remote_kill,
        on_remote_attach=on_remote_attach,
        on_refresh=on_refresh,
        on_probe_link_health=on_probe_link_health,
        on_probe_cwd_writable=on_probe_cwd_writable,
        on_probe_dir_launchable=on_probe_dir_launchable,
        on_launch_cwd=on_launch_cwd,
        on_launch_new=on_launch_new,
        on_launch_existing=on_launch_existing,
        on_probe_existing_sessions=on_probe_existing_sessions,
        on_probe_worktrees=on_probe_worktrees,
        on_create_worktree=on_create_worktree,
        on_launch_existing_worktree=on_launch_existing_worktree,
        on_probe_existing_worktree_sessions=on_probe_existing_worktree_sessions,
        get_settings_entries=get_settings_entries,
        on_setting_save=on_setting_save,
        on_setting_remove=on_setting_remove,
        on_setting_save_mapping=on_setting_save_mapping,
        get_git_remote_profile_rows=get_git_remote_profile_rows,
        git_create_enabled=cfg.git_create_enabled,
        default_git_remote_profile=cfg.default_git_remote_profile,
        git_remote_profile_options=git_profile_options,
        refresh_sources=refresh_sources,
        remote_hosts=list(cfg.remote_hosts),
        tui_table_columns=cfg.tui_table_columns,
        tui_table_default_view=cfg.tui_table_default_view,
        tui_search_fields=cfg.tui_search_fields,
        tui_color_palette=cfg.tui_color_palette,
        local_host_color=cfg.local_host_color,
    )


def do_interactive(cfg: Config, launch_user: str) -> int:
    try:
        from uxon import tui as uxon_tui
    except ImportError:
        try:
            from uxon.tui.hints import TEXTUAL_MISSING_HINT

            eprint(TEXTUAL_MISSING_HINT)
        except ImportError:
            eprint(
                "uxon: interactive mode requires the 'textual' package "
                "(pip install --user textual)."
            )
        return 1
    cwd = canonical(os.getcwd())
    # Hand the TUI a skeleton ctx so the first frame paints immediately;
    # the real ctx is loaded by a worker once the app is mounted.
    ctx = _build_tui_context(cfg, launch_user, cwd, skeleton=True)
    return uxon_tui.run(ctx)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = parse_args(argv)
    except SystemExit as ex:
        # argparse always raises SystemExit with an int (0 for --help,
        # 2 for parse errors); guard the typed-as-``str | int | None`` shape.
        return int(ex.code) if isinstance(ex.code, int) else (0 if ex.code is None else 2)
    from uxon.infra import audit as _audit

    try:
        cfg = config_loader.load_config(os.getcwd())
    except SystemExit as ex:
        # Bug 5 part 2 — convert config-load failure into an audit event.
        # The audit module's compile-time defaults (``enabled=True``,
        # ``syslog_facility="user"``) are what fires here; ``configure()``
        # has not run yet because ``config_loader.load_config`` is what feeds it.
        # Spec says ``error`` carries the first 256 chars of the error
        # text; ``fail()`` stashes the human-readable message on
        # ``ex.uxon_msg`` so we don't end up logging just the int exit
        # code (``str(SystemExit(1)) == "1"``).
        err_msg = getattr(ex, "uxon_msg", None) or str(ex.code)
        _audit.audit(
            "config.error",
            outcome="error",
            path=str(config_loader.repo_config_path()),
            error=err_msg[:256],
        )
        raise
    _audit.configure(
        enabled=cfg.audit_enabled,
        syslog_facility=cfg.audit_syslog_facility,
        subcmd=args.action,
    )
    caller_user = identity.resolve_caller_user()
    launch_user = identity.resolve_launch_user(cfg, caller_user)

    # CLI preflight: probe for tmux and required agents on actions that
    # actually shell out to tmux. ``interactive`` is excluded so the TUI
    # mount stays fast — the TUI runs its own async probe in the
    # background and surfaces the same hints in line.
    if args.action in {"run", "new", "attach", "list", "kill", "kill-all"}:
        from uxon.infra import probes as uxon_probes

        report = uxon_probes.probe_host(launch_user)
        if report.tmux.path is None:
            fail(f"tmux is not installed.\n{report.tmux.install_hint}", 1)
        # Stash the report on ``args`` so downstream ``resolve_agent_id``
        # reuses it instead of paying a second sudo round-trip. Agent
        # install-gating is owned by ``resolve_agent_id`` — it now
        # validates the picked candidate (including ``--agent`` and
        # ``agents.default``) against this same report.
        args.host_report = report

    if args.action == "interactive":
        return do_interactive(cfg, launch_user)
    if args.action == "version":
        if args.json_output:
            _emit_json("version", version_probe._version_data())
            return 0
        print(format_version())
        return 0
    # Emit ``cli.start`` *after* the ``version`` early-return (the version
    # subcommand is a no-op probe; we don't litter the audit trail with it)
    # but *before* the ``doctor`` early-return — ``uxon doctor`` is a
    # substantive operator gesture that belongs in the audit history.
    _audit.audit(
        "cli.start",
        flags=_audit._sanitize_flags(list(argv or [])),
        agents_enabled=list(cfg.enabled_agents),
        enable_all_users_list=cfg.enable_all_users_list,
        audit_enabled=True,
        allowed_roots_count=len(cfg.allowed_roots),
        remote_hosts_count=len(cfg.remote_hosts),
    )
    if args.action == "doctor":
        return do_doctor(
            cfg,
            caller_user,
            launch_user,
            canonical(os.getcwd()),
            json_output=args.json_output,
            probe_remote=args.all_hosts,
        )
    if args.action == "run":
        return do_run(args, cfg, launch_user)
    if args.action == "list":
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
    if args.action == "attach":
        return do_attach(args, cfg, launch_user)
    if args.action == "kill":
        return do_kill(args, cfg, launch_user)
    if args.action == "kill-all":
        return do_kill_all(args, cfg, launch_user)
    if args.action == "new":
        return do_new(args, cfg, launch_user)
    fail(f"unsupported action: {args.action}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
