# SPDX-License-Identifier: MIT
"""``uxon doctor`` use-case: diagnose tmux/agent/config/git-profile health.

Impure: probes the host (subprocess), resolves command paths, reads config
layers, optionally SSHes to remote peers under ``--remote``.

HARD RULE (AGENTS.md): default ``uxon doctor`` performs zero remote SSH I/O;
remote probes run only under ``--remote`` (:func:`_doctor_remote_rows`).
:func:`detect_root_nopasswd` keeps its tight 0.5s timeout — do not add probes
that can exceed it.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

import uxon.app.listing as listing_app
import uxon.app.repeat as repeat_app
from uxon.domain.config import Config
from uxon.domain.constants import VALID_AGENT_IDS
from uxon.domain.session import SessionInfo
from uxon.infra import config_loader, identity, sessions_probe, tmux, version_probe


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
    from uxon.domain.authz import is_under_allowed_roots

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
        listing_app._emit_json("doctor", data)
        return 0

    print("uxon doctor")
    print(f"version={version_probe.format_version()}")
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
