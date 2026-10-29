# SPDX-License-Identifier: MIT
"""``uxon kill`` / ``kill-all`` use-cases: local, cross-user, and remote kill."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import TypeAlias

import uxon.app.listing as listing_app
from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.session import BulkKillResult, KillResult, SessionIdentity, SessionInfo
from uxon.errors import eprint, fail
from uxon.infra import identity, sessions_probe, tmux
from uxon.infra.run import run_query


@dataclass(frozen=True)
class RuntimeTeardown:
    """A prepared runtime session stop (the kill-path mirror of launch).

    Rendered exclusively from the verified controller-side launch record.
    The saved resource identity is compared with the live runtime before the
    stop adapter uses the nonce-scoped workload record.
    """

    stop_cmd: list[str]
    runtime: str
    resource: str
    runtime_id: str
    runtime_fingerprint: str
    runtime_dir: str
    project_slug: str
    launch_profile: str
    agent: str
    launch_user: str
    launch_epoch: str = ""
    session_id: str = ""
    launch_nonce: str = ""
    execution_backend: str = ""


@dataclass(frozen=True)
class CleanupResult:
    ok: bool
    error: str = ""

    def __bool__(self) -> bool:
        return self.ok


RuntimeTeardownPreparation: TypeAlias = RuntimeTeardown | CleanupResult | None


def _teardown_skip(
    session_name: str,
    reason: str,
    *,
    reason_code: str | None = None,
    runtime: str = "",
    resource: str = "",
    target_user: str = "",
    session_id: str = "",
    launch_nonce: str = "",
    execution_backend: str = "",
) -> CleanupResult:
    eprint(f"uxon: runtime session stop for {session_name} skipped: {reason}")
    if reason_code is not None:
        from uxon.infra import audit as _audit

        _audit.audit(
            "runtime.session_stop",
            outcome="skipped",
            reason=reason_code,
            runtime=runtime,
            runtime_resource=resource,
            action="skip",
            session=session_name,
            session_id=session_id,
            launch_nonce=launch_nonce,
            execution_backend=execution_backend,
            target_user=target_user,
        )

    return CleanupResult(False, reason)


def cleanup_launch_record(cfg: Config, target: SessionInfo) -> None:
    """Remove the authoritative record after the exact tmux session is gone."""
    if not target.launch_record_verified:
        return
    from pathlib import Path

    from uxon.infra.launch_records import TmuxSessionMetadata, delete_verified_record

    removed = delete_verified_record(
        tmux.tmux_socket_path(cfg, target.user),
        TmuxSessionMetadata(
            session_id=target.tmux_session_id,
            created=target.tmux_session_created,
            name=target.name,
            launch_nonce=target.launch_nonce,
        ),
        override_dir=Path(cfg.launch_record_dir) if cfg.launch_record_dir else None,
        shared=bool(cfg.launch_record_dir),
        launch_user=target.user,
    )
    if not removed:
        fail(f"unable to verify launch record cleanup for {target.name!r}")


def prepare_runtime_teardown(cfg: Config, target: SessionInfo) -> RuntimeTeardownPreparation:
    """Prepare the workload stop, or report that it is absent or unavailable.

    Capture the verified launch record while the exact tmux session is alive.
    The caller runs the result via :func:`run_runtime_teardown` AFTER
    ``kill-session`` — killing the agent closes its pane, so reaping first
    would race tmux's own session teardown and leave ``kill-session`` with no
    server to talk to. Kill the session first, then reap the orphan the
    severed exec leaves behind.

    ``None`` means no workload stop is configured. A failed ``CleanupResult`` means teardown
    could not be prepared: the tmux kill remains available, but post-kill
    cleanup must report failure and retain the launch record. A runtime-managed
    session without its authoritative launch record fails closed before tmux
    mutation: Uxon cannot safely reconstruct that workload's teardown identity.
    """
    if target.launch_record_verified:
        if target.runtime == "direct":
            return None
    elif not target.runtime_resource and not target.runtime_marker:
        return None
    if not target.launch_record_verified:
        fail(
            f"refusing to kill workload session {target.name!r}: "
            "no authoritative launch record is available"
        )
    if not target.runtime:
        return _teardown_skip(
            target.name,
            "missing workload runtime",
            reason_code="missing_runtime",
            resource=target.runtime_resource,
            target_user=target.user,
            session_id=target.tmux_session_id,
            launch_nonce=target.launch_nonce,
            execution_backend=target.execution_backend,
        )
    profile = cfg.runtimes.get(target.runtime)
    if profile is None:
        return _teardown_skip(
            target.name,
            f"workload runtime {target.runtime!r} is unavailable",
            reason_code="missing_profile",
            runtime=target.runtime,
            resource=target.runtime_resource,
            target_user=target.user,
            session_id=target.tmux_session_id,
            launch_nonce=target.launch_nonce,
            execution_backend=target.execution_backend,
        )
    if profile.fingerprint != target.runtime_fingerprint:
        return _teardown_skip(
            target.name,
            "workload runtime changed since launch",
            reason_code="fingerprint_mismatch",
            runtime=target.runtime,
            resource=target.runtime_resource,
            target_user=target.user,
            session_id=target.tmux_session_id,
            launch_nonce=target.launch_nonce,
            execution_backend=target.execution_backend,
        )
    if not profile.stop_command:
        return None
    if not target.runtime_id or not target.runtime_epoch:
        return _teardown_skip(
            target.name,
            "missing runtime resource identity",
            reason_code="identity_unresolved",
            runtime=target.runtime,
            resource=target.runtime_resource,
            target_user=target.user,
            session_id=target.tmux_session_id,
            launch_nonce=target.launch_nonce,
            execution_backend=target.execution_backend,
        )
    try:
        from uxon.domain.runtime import (
            is_valid_runtime_resource,
            render_profile_template,
            runtime_pidfile,
        )

        name = target.runtime_resource
        if not name or not is_valid_runtime_resource(name):
            return _teardown_skip(
                target.name,
                "missing runtime resource",
                reason_code="missing_resource",
                runtime=target.runtime,
                resource=target.runtime_resource,
                target_user=target.user,
                session_id=target.tmux_session_id,
                launch_nonce=target.launch_nonce,
                execution_backend=target.execution_backend,
            )
        pidfile = runtime_pidfile(target.launch_nonce)
        stop_cmd = render_profile_template(
            profile.stop_command,
            profile=profile,
            what="stop_command",
            resource=name,
            runtime_dir=target.runtime_dir,
            user=target.launch_user,
            launch_profile=target.profile,
            agent=target.agent,
            project_slug=target.project_slug,
            pidfile=pidfile,
        )
        return RuntimeTeardown(
            stop_cmd=stop_cmd,
            runtime=target.runtime,
            resource=name,
            runtime_id=target.runtime_id,
            runtime_fingerprint=target.runtime_fingerprint,
            session_id=target.tmux_session_id,
            launch_nonce=target.launch_nonce,
            execution_backend=target.execution_backend,
            runtime_dir=target.runtime_dir,
            project_slug=target.project_slug,
            launch_profile=target.profile,
            agent=target.agent,
            launch_user=target.launch_user,
            launch_epoch=target.runtime_epoch,
        )
    except SystemExit as exc:  # template render failure must never abort the kill
        return _teardown_skip(
            target.name,
            f"invalid runtime stop configuration: {getattr(exc, 'uxon_msg', str(exc))}",
            reason_code="invalid_stop_config",
            runtime=target.runtime,
            resource=target.runtime_resource,
            target_user=target.user,
            session_id=target.tmux_session_id,
            launch_nonce=target.launch_nonce,
            execution_backend=target.execution_backend,
        )
    except (OSError, ValueError) as exc:  # noqa: BLE001 — teardown must never block kill-session
        return _teardown_skip(
            target.name,
            f"runtime stop preparation failed: {exc}",
            reason_code="prepare_failed",
            runtime=target.runtime,
            resource=target.runtime_resource,
            target_user=target.user,
            session_id=target.tmux_session_id,
            launch_nonce=target.launch_nonce,
            execution_backend=target.execution_backend,
        )


def run_runtime_teardown(
    cfg: Config, teardown: RuntimeTeardown, target_user: str, session_name: str
) -> CleanupResult:
    """Run a prepared teardown (best-effort) — after ``kill-session``.

    The single shared teardown-call site: every kill path (CLI self/cross-user,
    kill-all, and the three TUI bridge paths) routes through here so the
    ``runtime.session_stop`` audit event carries the captured session identity
    and execution backend with its ``ok`` / ``error`` / ``skipped`` outcome.

    **PID-recycle guard:** when the launch stashed a start-epoch, the
    live epoch is re-resolved *here* — immediately before the kill, after
    ``kill-session`` — and a confirmed mismatch means the resource restarted
    since launch (the recorded runtime PID now names an unrelated
    process). The stop command is then **not** run and the event records
    ``outcome=skipped`` with a typed reason. An unresolved identity also skips
    the stop command. A residual window remains between this re-resolve
    and the kill itself — fully closing it would need an epoch-conditional
    ``stop_command`` (an operator-template concern), but re-resolving here
    rather than at capture time shrinks it to a single resolve→kill round-trip.
    Expected stop failures return a concrete cleanup result. Probe exceptions
    propagate to the caller, which reports incomplete cleanup and retains the
    protected launch record after the tmux session has been removed.
    """
    from uxon.infra import audit as _audit
    from uxon.infra import runtime as runtime_infra

    profile = cfg.runtimes.get(teardown.runtime)
    if profile is None:
        _teardown_skip(session_name, f"workload runtime {teardown.runtime!r} is unavailable")
        _audit.audit(
            "runtime.session_stop",
            outcome="skipped",
            reason="missing_profile",
            runtime=teardown.runtime,
            runtime_resource=teardown.resource,
            action="skip",
            session=session_name,
            session_id=teardown.session_id,
            launch_nonce=teardown.launch_nonce,
            execution_backend=teardown.execution_backend,
            target_user=target_user,
        )
        return CleanupResult(False, f"workload runtime {teardown.runtime!r} is unavailable")
    if profile.fingerprint != teardown.runtime_fingerprint:
        _teardown_skip(session_name, "workload runtime changed since launch")
        _audit.audit(
            "runtime.session_stop",
            outcome="skipped",
            reason="fingerprint_mismatch",
            runtime=teardown.runtime,
            runtime_resource=teardown.resource,
            action="skip",
            session=session_name,
            session_id=teardown.session_id,
            launch_nonce=teardown.launch_nonce,
            execution_backend=teardown.execution_backend,
            target_user=target_user,
        )
        return CleanupResult(False, "workload runtime changed since launch")
    live = runtime_infra.current_runtime_identity_for_profile(
        cfg,
        profile,
        teardown.resource,
        teardown.launch_user,
        runtime_dir=teardown.runtime_dir,
        launch_profile=teardown.launch_profile,
        agent=teardown.agent,
        project_slug=teardown.project_slug,
    )
    if live is None:
        _teardown_skip(session_name, "live runtime resource identity could not be resolved")
        _audit.audit(
            "runtime.session_stop",
            outcome="skipped",
            reason="identity_unresolved",
            runtime=teardown.runtime,
            runtime_resource=teardown.resource,
            action="skip",
            session=session_name,
            session_id=teardown.session_id,
            launch_nonce=teardown.launch_nonce,
            execution_backend=teardown.execution_backend,
            target_user=target_user,
        )
        return CleanupResult(False, "live runtime resource identity could not be resolved")
    if live.id != teardown.runtime_id or live.epoch != teardown.launch_epoch:
        _teardown_skip(
            session_name,
            "the runtime resource identity changed since launch",
        )
        _audit.audit(
            "runtime.session_stop",
            outcome="skipped",
            reason="stale_identity",
            runtime=teardown.runtime,
            runtime_resource=teardown.resource,
            action="skip",
            session=session_name,
            session_id=teardown.session_id,
            launch_nonce=teardown.launch_nonce,
            execution_backend=teardown.execution_backend,
            target_user=target_user,
        )
        return CleanupResult(False, "the runtime resource identity changed since launch")

    ok, detail = runtime_infra.run_teardown(
        cfg, teardown.stop_cmd, target_user, profile.stop_timeout_seconds
    )
    if not ok:
        eprint(f"uxon: runtime session stop for {session_name} did not complete: {detail}")
    _audit.audit(
        "runtime.session_stop",
        outcome="ok" if ok else "error",
        runtime=teardown.runtime,
        runtime_resource=teardown.resource,
        action="stop",
        session=session_name,
        session_id=teardown.session_id,
        launch_nonce=teardown.launch_nonce,
        execution_backend=teardown.execution_backend,
        target_user=target_user,
        **({"error": detail[:256]} if not ok and detail else {}),
    )
    return CleanupResult(ok, detail)


def finish_killed_session(
    cfg: Config,
    target: SessionInfo,
    teardown: RuntimeTeardownPreparation,
    *,
    target_user: str,
) -> CleanupResult:
    """Complete runtime/record cleanup after a proven tmux kill.

    Return concrete expected failures; user interruption still propagates.
    """
    try:
        if tmux.exact_identity_exists(cfg, target.identity):
            eprint(
                f"uxon: session {target.name!r} ({target.tmux_session_id}) still exists; cleanup refused"
            )
            return CleanupResult(False, "exact selected session still exists; cleanup refused")
    except (OSError, subprocess.SubprocessError, SystemExit) as exc:
        eprint(f"uxon: cleanup refused: {getattr(exc, 'uxon_msg', str(exc))}")
        return CleanupResult(
            False, f"cannot prove terminal removal: {getattr(exc, 'uxon_msg', str(exc))}"
        )
    if target.launch_record_verified:
        from pathlib import Path

        from uxon.infra import launch_records

        metadata = launch_records.TmuxSessionMetadata(
            target.tmux_session_id,
            target.tmux_session_created,
            target.name,
            target.launch_nonce,
        )
        record_dir = Path(cfg.launch_record_dir) if cfg.launch_record_dir else None
        record = launch_records.read_verified_record(
            target.socket_path,
            metadata,
            override_dir=record_dir,
            shared=bool(cfg.launch_record_dir),
            launch_user=target.user,
        )
        if (
            record is None
            and teardown is None
            and target.runtime == "direct"
            and target.runtime_kind == "direct"
            and launch_records.record_is_absent(
                target.socket_path,
                metadata,
                override_dir=record_dir,
                shared=bool(cfg.launch_record_dir),
                launch_user=target.user,
            )
        ):
            # Another client completed direct cleanup after both captured authority.
            return CleanupResult(True)
        fields = {
            "launch_user": target.launch_user,
            "execution_backend": target.execution_backend,
            "execution_fingerprint": target.execution_fingerprint,
            "runtime": target.runtime,
            "runtime_kind": target.runtime_kind,
            "runtime_fingerprint": target.runtime_fingerprint,
            "runtime_resource": target.runtime_resource,
            "runtime_dir": target.runtime_dir,
            "project_slug": target.project_slug,
            "runtime_id": target.runtime_id,
            "runtime_epoch": target.runtime_epoch,
            "profile": target.profile,
            "agent": target.agent,
        }
        if record is None or any(record.get(key, "") != value for key, value in fields.items()):
            eprint(
                f"uxon: protected launch record changed for {target.name!r} ({target.tmux_session_id}); cleanup refused"
            )
            return CleanupResult(
                False, "protected launch record changed or cannot be verified; cleanup refused"
            )
    if isinstance(teardown, CleanupResult):
        return teardown
    try:
        if teardown is not None:
            result = run_runtime_teardown(cfg, teardown, target_user, target.name)
            if not result:
                return result
        cleanup_launch_record(cfg, target)
    except (OSError, subprocess.SubprocessError, SystemExit) as exc:
        return CleanupResult(False, getattr(exc, "uxon_msg", str(exc)))
    return CleanupResult(True)


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


def remote_kill_command(
    executable: str,
    name: str,
    user: str,
    expected: SessionIdentity | None,
    correlation_id: str,
    *,
    json_output: bool = True,
) -> str:
    parts = [executable, "kill", name, "--force", "--user", user]
    if expected is not None:
        parts += ["--expect-session", expected.to_json()]
    if json_output:
        parts.append("--json")
    parts += ["--audit-correlation-id", correlation_id]
    return shlex.join(parts)


def _remote_kill_argv(cfg: Config, peer, expected: SessionIdentity, corr_id: str) -> list[str]:
    from uxon.infra.remote.collector import DEFAULT_CONNECT_TIMEOUT_SEC
    from uxon.infra.remote.ssh_argv import build_peer_ssh_argv

    return build_peer_ssh_argv(
        command_template=peer.command_template,
        extra_ssh_options=peer.extra_ssh_options,
        ssh_alias=peer.ssh_alias,
        remote_uxon=peer.remote_uxon,
        remote_command=remote_kill_command(
            peer.remote_uxon, expected.name, expected.user, expected, corr_id
        ),
        allocate_tty=False,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_SEC,
        ssh_multiplex=cfg.ssh_multiplex,
        ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
    )


def remote_kill_expected(
    cfg: Config,
    host: str,
    expected: SessionIdentity,
    *,
    dry_run: bool = False,
    correlation_id: str | None = None,
) -> KillResult:
    """Share conditional dispatch, response validation and initiator audit."""
    import json
    import uuid
    from dataclasses import replace

    from uxon.domain.wire_schema import WIRE_SCHEMA_VERSION
    from uxon.infra import audit as _audit
    from uxon.infra.remote.collector import DEFAULT_TOTAL_TIMEOUT_SEC
    from uxon.infra.remote.master_recovery import recover_wedged_master
    from uxon.infra.remote_hosts import find_host

    expected.validate()
    peer = find_host(cfg.remote_hosts, host)
    if peer is None:
        fail(f"unknown remote host: {host}", 1)
    corr_id = correlation_id or str(uuid.uuid4())
    result = KillResult(expected, "failed", error="remote operation did not complete")
    rc = 0
    try:
        if dry_run:
            result = KillResult(expected, "would-kill")
        else:
            cp = run_query(
                _remote_kill_argv(cfg, peer, expected, corr_id), timeout=DEFAULT_TOTAL_TIMEOUT_SEC
            )
            rc = cp.returncode
            try:
                env = json.loads(cp.stdout)
                if not isinstance(env, dict):
                    raise ValueError("response is not an object")
                payload = env["data"]
                if (
                    env.get("schema_version") != WIRE_SCHEMA_VERSION
                    or env.get("kind") != "kill"
                    or not isinstance(payload, dict)
                    or payload.get("session_id") != expected.session_id
                    or payload.get("launch_nonce") != expected.launch_nonce
                    or payload.get("target_user") != expected.user
                    or payload.get("action") not in {"killed", "already_absent", "failed"}
                    or payload.get("cleanup_outcome") not in {"ok", "error", "not_run"}
                    or not isinstance(payload.get("error", ""), str)
                    or not isinstance(payload.get("name"), str)
                    or not payload.get("name")
                    or (
                        payload.get("action") in {"killed", "already_absent"}
                        and payload.get("cleanup_outcome") == "not_run"
                    )
                    or (payload.get("cleanup_outcome") == "error" and not payload.get("error"))
                    or (payload.get("action") == "failed" and not payload.get("error"))
                    or (
                        payload.get("action") != "failed"
                        and payload.get("cleanup_outcome") == "ok"
                        and payload.get("error")
                    )
                ):
                    raise ValueError("unexpected response identity or outcome")
                result = KillResult(
                    replace(expected, name=payload["name"]),
                    payload["action"],
                    payload["cleanup_outcome"],
                    payload.get("error", ""),
                )
                if cp.returncode != 0 and result.ok:
                    result = KillResult(expected, "failed", error=f"ssh exited {cp.returncode}")
            except (TypeError, KeyError, ValueError) as exc:
                detail = (cp.stderr or "").strip()
                result = KillResult(
                    expected, "failed", error=f"invalid conditional-kill response: {detail or exc}"
                )
    except subprocess.TimeoutExpired:
        rc = 124
        if cfg.ssh_multiplex != "off":
            recover_wedged_master(peer)
        result = KillResult(
            expected,
            "failed",
            error=f"ssh timeout after {DEFAULT_TOTAL_TIMEOUT_SEC}s talking to {host}",
        )
    except OSError as exc:
        rc = 127
        result = KillResult(expected, "failed", error=f"ssh unavailable: {exc}")
    _audit.audit(
        "kill.remote.out",
        outcome="ok" if result.ok else "error",
        peer_name=peer.name,
        ssh_alias=peer.ssh_alias,
        target_user=expected.user,
        target_session=result.identity.name,
        session_id=expected.session_id,
        launch_nonce=expected.launch_nonce,
        execution_backend=expected.execution_backend,
        force=True,
        dry_run=dry_run,
        correlation_id=corr_id,
        rc=rc,
        action=result.action,
        cleanup_outcome=result.cleanup_outcome,
        **({"error": result.error} if result.error else {}),
    )
    return result


def resolve_remote_identity(cfg: Config, peer, selector: str, user: str | None) -> SessionIdentity:
    """Resolve a friendly selector once, before confirmation or handoff."""
    from uxon.infra.remote.collector import fetch_remote_snapshot

    snapshot = fetch_remote_snapshot(
        peer,
        all_users=user is not None,
        ssh_multiplex=cfg.ssh_multiplex,
        ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
    )
    if snapshot.error:
        fail(f"cannot resolve session on {peer.name}: {snapshot.error}", 1)
    candidates = []
    for record in snapshot.sessions:
        if user is not None and record.get("user") != user:
            continue
        identity_data = record.get("identity")
        candidate = SessionInfo(
            user=record.get("user", ""),
            name=record.get("name", ""),
            attached="0",
            windows="0",
            created="",
            last_attached="",
            pane_pids=(),
            active_pid=record.get("active_pid"),
            active_cmd="",
            active_path="",
        )
        ident = SessionIdentity.from_wire(identity_data)
        if ident is not None:
            candidate.tmux_session_id, candidate.tmux_session_created = (
                ident.session_id,
                ident.created,
            )
            candidate.launch_nonce, candidate.socket_path = ident.launch_nonce, ident.socket_path
            candidate.execution_backend, candidate.execution_fingerprint = (
                ident.execution_backend,
                ident.execution_fingerprint,
            )
        candidates.append(candidate)
    return sessions_probe.resolve_session(
        selector or "",
        candidates,
        cfg.session_prefix,
        legacy_prefixes=cfg.legacy_session_prefixes,
    ).identity


def _do_kill_remote(args: ParsedArgs, cfg: Config) -> int:
    import uuid

    from uxon.infra import audit as _audit
    from uxon.infra.remote_hosts import find_host

    peer = find_host(cfg.remote_hosts, args.host or "")
    if peer is None:
        _audit.audit(
            "kill.remote.out",
            outcome="not_found",
            peer_name=args.host or "",
            target_user=args.user,
            target_session=args.target_id,
            session_id="",
        )
        if not cfg.remote_hosts:
            fail("no [[remote_hosts]] configured; --host requires at least one peer")
        fail(
            f"unknown --host {args.host!r}; configured: {', '.join(host.name for host in cfg.remote_hosts)}"
        )
    if args.json_output and not args.force and not args.dry_run:
        fail("kill --json requires --force or --dry-run")
    expected = args.expected_session
    if expected is None:
        expected = resolve_remote_identity(cfg, peer, args.target_id or "", args.user)
    expected.validate()
    if args.user is not None and expected.user != args.user:
        fail("--expect-session user differs from --user", 1)
    _confirm_kill_or_fail(
        f"Kill {expected.name} ({expected.session_id}, user={expected.user}) on {peer.name}?", args
    )
    corr_id = str(uuid.uuid4())
    result = remote_kill_expected(
        cfg, peer.name, expected, dry_run=args.dry_run, correlation_id=corr_id
    )
    if args.json_output:
        listing_app._emit_json_with_host(
            "kill",
            {
                **result.to_dict(),
                "target": result.identity.name,
                "target_user": expected.user,
                "dry_run": args.dry_run,
                **(
                    {"ssh_argv": _remote_kill_argv(cfg, peer, expected, corr_id)}
                    if args.dry_run
                    else {}
                ),
            },
            host=peer.name,
        )
    elif result.ok and args.dry_run:
        print(f"dry-run: {shlex.join(_remote_kill_argv(cfg, peer, expected, corr_id))}")
    elif result.ok:
        print(
            f"{result.action}: {result.identity.name} ({result.identity.session_id}) on {peer.name}"
        )
    if result.error:
        eprint(f"uxon: {expected.name} ({expected.session_id}) on {peer.name}: {result.error}")
    return int(not result.ok)


def _target_for_identity(
    cfg: Config, expected: SessionIdentity, *, require_nonce: bool = True
) -> SessionInfo:
    """Refresh descriptive state without ever replacing the captured target."""
    from pathlib import Path

    from uxon.infra import launch_records

    tmux.validate_session_context(cfg, expected, require_nonce=require_nonce)
    snapshot = sessions_probe.collect_session_snapshot_for_user(
        cfg,
        expected.user,
        cfg.session_prefix,
        expected.socket_path,
        legacy_prefixes=cfg.legacy_session_prefixes,
        session_id=expected.session_id,
        for_dismissal=True,
    )
    target = next((row for row in snapshot.sessions if expected.matches(row.identity)), None)
    if target is None:
        target = SessionInfo(
            user=expected.user,
            name=expected.name,
            attached="0",
            windows="0",
            created="",
            last_attached="",
            pane_pids=(),
            active_pid=None,
            active_cmd="",
            active_path="",
            tmux_session_id=expected.session_id,
            tmux_session_created=expected.created,
            launch_nonce=expected.launch_nonce,
            socket_path=expected.socket_path,
            execution_backend=expected.execution_backend,
            execution_fingerprint=expected.execution_fingerprint,
        )
    record = launch_records.read_verified_record(
        expected.socket_path,
        launch_records.TmuxSessionMetadata(
            expected.session_id, expected.created, expected.name, expected.launch_nonce
        ),
        override_dir=Path(cfg.launch_record_dir) if cfg.launch_record_dir else None,
        shared=bool(cfg.launch_record_dir),
        launch_user=expected.user,
    )
    if require_nonce and record is None:
        fail(
            f"cannot safely remove {target.name!r} ({expected.session_id}): "
            "the authoritative launch record is unavailable; drain older sessions "
            "before upgrading or remove them explicitly with tmux",
            1,
        )
    if record is not None:
        if (
            record.get("launch_user") != expected.user
            or record.get("execution_backend") != expected.execution_backend
            or record.get("execution_fingerprint") != expected.execution_fingerprint
        ):
            fail("protected launch record has a different execution identity", 1)
        target.launch_record_verified = True
        for attr, key in (
            ("agent", "agent"),
            ("profile", "profile"),
            ("launch_user", "launch_user"),
            ("runtime", "runtime"),
            ("runtime_kind", "runtime_kind"),
            ("runtime_fingerprint", "runtime_fingerprint"),
            ("runtime_resource", "runtime_resource"),
            ("runtime_dir", "runtime_dir"),
            ("project_slug", "project_slug"),
            ("runtime_cgroup", "runtime_cgroup"),
            ("runtime_id", "runtime_id"),
            ("runtime_epoch", "runtime_epoch"),
        ):
            setattr(target, attr, str(record.get(key, "")))
    return target


def kill_expected(
    cfg: Config,
    expected: SessionIdentity,
    *,
    force: bool = True,
    dry_run: bool = False,
    allowed_users: frozenset[str] | None = None,
) -> KillResult:
    """One conditional destructive operation shared by CLI and TUI."""
    from uxon.infra import audit as _audit

    target = None
    removed = False
    result = KillResult(expected, "failed", error="operation did not complete")
    try:
        if allowed_users is not None and expected.user not in allowed_users:
            fail(f"target user {expected.user!r} is outside the permitted action scope", 1)
        target = _target_for_identity(cfg, expected)
        current = target.identity
        if dry_run:
            result = KillResult(current, "would-kill")
        else:
            teardown = prepare_runtime_teardown(cfg, target)
            if tmux.exact_identity_exists(cfg, current):
                matched, error = tmux.conditional_kill(cfg, current)
                # An acknowledgement can be lost. Exact absence, rather than
                # a transport exit status, is the prerequisite for cleanup.
                if tmux.exact_identity_exists(cfg, current):
                    fail(error or "the exact selected session is still alive", 1)
                removed = matched
            action = "killed" if removed else "already_absent"
            cleanup = finish_killed_session(cfg, target, teardown, target_user=current.user)
            result = KillResult(
                current,
                action,
                "ok" if cleanup else "error",
                "" if cleanup else f"{cleanup.error}; launch record retained",
            )
    except (SystemExit, OSError, subprocess.SubprocessError) as exc:
        result = KillResult(
            target.identity if target is not None else expected,
            "failed",
            error=getattr(exc, "uxon_msg", str(exc)) or type(exc).__name__,
        )
    _audit.audit(
        "session.kill",
        outcome="ok" if result.ok else "error",
        session=result.identity.name,
        session_id=result.identity.session_id,
        launch_nonce=result.identity.launch_nonce,
        target_user=result.identity.user,
        execution_backend=result.identity.execution_backend,
        profile=target.profile if target else "",
        agent=target.agent if target else "",
        action=result.action,
        cleanup_outcome=result.cleanup_outcome,
        force=force,
        dry_run=dry_run,
        **({"error": result.error} if result.error else {}),
    )
    return result


def kill_snapshot(
    cfg: Config,
    targets: tuple[SessionIdentity, ...],
    *,
    dry_run: bool = False,
    allowed_users: frozenset[str] | None = None,
) -> BulkKillResult:
    """Attempt precisely the frozen target set and retain individual errors."""
    from uxon.infra import audit as _audit

    result = BulkKillResult(
        tuple(
            kill_expected(cfg, target, dry_run=dry_run, allowed_users=allowed_users)
            for target in targets
        )
    )
    _audit.audit(
        "session.kill_all",
        outcome="ok" if result.ok else "error",
        target_users=sorted({target.user for target in targets}),
        attempted_count=0 if dry_run else len(targets),
        killed_count=result.killed_count,
        already_absent_count=sum(row.action == "already_absent" for row in result.results),
        failed_count=sum(row.action == "failed" for row in result.results),
        cleanup_failed_count=sum(row.cleanup_outcome == "error" for row in result.results),
        dry_run=dry_run,
        sessions=[row.to_dict() for row in result.results],
    )
    return result


def do_kill(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    if not args.target_id:
        fail("kill requires an identifier")
    if args.host is not None:
        return _do_kill_remote(args, cfg)
    from uxon.infra import audit as _audit

    target_user = args.user or launch_user
    if target_user != launch_user:
        from uxon.infra.sudo_probe import probe_sudo_capability

        caps = probe_sudo_capability(cfg, [target_user])
        if target_user not in caps.reachable_users:
            _audit.audit(
                "session.kill",
                outcome="denied",
                session=args.target_id,
                session_id=args.expected_session.session_id if args.expected_session else "",
                target_user=target_user,
                force=args.force,
                dry_run=args.dry_run,
            )
            eprint(
                f"uxon-error: not-reachable (execution backend cannot reach {target_user}; check the selected backend helper and target policy)"
            )
            return 1

    expected = args.expected_session
    if expected is None:
        sessions = sessions_probe.collect_sessions([target_user], cfg)
        target = sessions_probe._resolve_or_audit_not_found(
            args.target_id,
            sessions,
            cfg,
            audit_event="session.kill",
            target_user=target_user,
            extra={"force": args.force, "dry_run": args.dry_run, "profile": "", "agent": ""},
        )
        expected = target.identity
    elif expected.user != target_user:
        fail("--expect-session user differs from --user", 1)
    # Capture before prompting, for self and cross-user paths alike.
    if target_user != launch_user:
        _confirm_kill_or_fail(
            f"Kill {expected.name} ({expected.session_id}, user={target_user})?", args
        )
    result = kill_expected(cfg, expected, force=args.force, dry_run=args.dry_run)
    if args.json_output:
        listing_app._emit_json(
            "kill",
            {
                **result.to_dict(),
                "target": result.identity.name,
                "user": target_user,
                "target_user": target_user,
                "reachable": True,
                "socket": expected.socket_path,
                "dry_run": args.dry_run,
            },
        )
    elif result.ok:
        if args.dry_run:
            print(f"dry-run: {shlex.join(tmux.conditional_kill_argv(cfg, expected))}")
        else:
            print(f"{result.action}: {result.identity.name} ({result.identity.session_id})")
    if result.error:
        eprint(f"uxon: {result.identity.name} ({result.identity.session_id}): {result.error}")
    return int(not result.ok)


def do_kill_all(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    targets = tuple(row.identity for row in sessions_probe.collect_sessions([launch_user], cfg))
    if targets and not args.dry_run and not args.force:
        if args.json_output:
            fail("kill-all --json requires --force or --dry-run")
        if not identity.is_interactive_tty():
            fail(
                "kill-all is destructive; rerun with --force, or use 'uxon list' / 'uxon doctor' first"
            )
        labels = ", ".join(f"{target.name} ({target.session_id})" for target in targets)
        if (
            input(
                f"Kill all {len(targets)} sessions: {labels}\nType 'kill-all' to confirm: "
            ).strip()
            != "kill-all"
        ):
            fail("cancelled", 130)
    result = kill_snapshot(cfg, targets, dry_run=args.dry_run)
    if args.json_output:
        listing_app._emit_json(
            "kill-all",
            {
                "user": launch_user,
                "socket": tmux.tmux_socket_path(cfg, launch_user),
                "dry_run": args.dry_run,
                "sessions": [row.to_dict() for row in result.results],
                "killed_count": result.killed_count,
                "failed_count": result.failed_count,
            },
        )
    else:
        for row in result.results:
            if args.dry_run:
                print(f"dry-run: {shlex.join(tmux.conditional_kill_argv(cfg, row.identity))}")
                continue
            print(
                f"{row.action}: {row.identity.name} ({row.identity.session_id})"
                + (f": {row.error}" if row.error else "")
            )
        print(result.summary)
    return int(not result.ok)
