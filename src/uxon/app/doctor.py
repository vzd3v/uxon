# SPDX-License-Identifier: MIT
"""``uxon doctor`` use-case: diagnose tmux/agent/config/git-profile health.

Impure: probes the host (subprocess), resolves command paths, reads config
sources, optionally SSHes to remote peers under ``--remote``.

Default ``uxon doctor`` performs zero remote SSH I/O; remote probes run only
under ``--remote`` (:func:`_doctor_remote_rows`). :func:`detect_root_nopasswd`
keeps its tight 0.5s timeout — do not add probes that can exceed it.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import uxon.app.listing as listing_app
import uxon.app.repeat as repeat_app
from uxon.domain.config import Config
from uxon.domain.session import SessionInfo
from uxon.infra import config_loader, identity, sessions_probe, tmux, version_probe
from uxon.infra.run import run_query


def user_can_write_dir(path: str, target_user: str) -> bool:
    cp = run_query(
        identity.command_prefix_for_user(target_user)
        + [
            "python3",
            "-c",
            "import os, sys; raise SystemExit(0 if os.access(sys.argv[1], os.W_OK | os.X_OK) else 1)",
            path,
        ],
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
    host_required_agents = tuple(
        dict.fromkeys(
            cfg.launch.profiles[pid].agent
            for pid in cfg.launch.effective_enabled_profiles
            if pid in cfg.launch.profiles
            and not cfg.launch.profiles[pid].container_profile
            and (cfg.launch.profiles[pid].launch_user or launch_user) == launch_user
        )
    )
    if host_required_agents:
        for aid in host_required_agents:
            if agent_paths.get(aid) is None:
                issues.append(f"'{aid}' agent binary is not resolvable for {launch_user}")
    elif cfg.launch.auto_mode and all(path is None for path in agent_paths.values()):
        issues.append(
            f"no agent binary is resolvable for {launch_user} (auto-mode); "
            f"install one of {tuple(cfg.agents)}"
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
    report = uxon_probes.probe_host(launch_user, cfg.agents)
    tmux_path = report.tmux.path
    # Doctor shows every catalogued agent regardless of strict/auto mode —
    # the operator wants to see the full landscape ("is X installed?")
    # not just the configured whitelist.
    doctor_agent_ids: tuple[str, ...] = tuple(cfg.agents)
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
        spec = cfg.agents[aid]
        if not spec.version_args:
            # ``version_args = []`` → operator opted out of the version line.
            return aid, uxon_agents.AgentAvailability(status="ok", path=agent_paths.get(aid))
        return aid, uxon_agents._probe_one(
            spec.binary,
            launch_user,
            version_args=spec.version_args,
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
    launch_profile_rows = _doctor_launch_profile_rows(cfg, caller_user, launch_user, agent_paths)
    container_profile_rows = _doctor_container_profile_rows(cfg, cwd, caller_user)

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
            "launch_profiles": launch_profile_rows,
            "current_socket_sessions": build_session_records(
                current_sessions, session_prefix=cfg.session_prefix
            ),
            "legacy_default_socket_sessions": build_session_records(
                legacy_sessions, session_prefix=cfg.session_prefix
            ),
            "git_create_enabled": cfg.git_create_enabled,
            "git_remote_profiles": _doctor_git_profile_rows(cfg, launch_user)
            if cfg.git_remote_profiles
            else [],
            "container_profiles": container_profile_rows,
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
        spec = cfg.agents[aid]
        path = agent_paths.get(aid) or "-"
        avail = availability.get(aid)
        if avail and avail.status == "ok":
            print(f"{aid}:  {path}  ok ({avail.version or '?'})")
        elif avail and avail.status == "timeout":
            print(f"{aid}:  {path}  TIMEOUT (>2.0s)")
        else:
            print(f"{aid}:  -  MISSING  ({spec.install_hint})")
    print(f"launch_profiles={len(launch_profile_rows)}:")
    for row in launch_profile_rows:
        print(
            f"- {row['id']}  agent={row['agent']}  launch_user={row['launch_user']}  "
            f"container_profile={row['container_profile'] or '-'}  "
            f"host_agent={'not-required' if row['containerized'] else row['host_agent_path'] or 'missing'}"
        )
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
    if cfg.git_remote_profiles:
        print(f"git_remote_profiles={len(cfg.git_remote_profiles)}:")
        for row in _doctor_git_profile_rows(cfg, launch_user):
            print(f"- {row}")
    else:
        print("git_remote_profiles=0")
    if container_profile_rows:
        print(f"container_profiles={len(container_profile_rows)}:")
        for row in container_profile_rows:
            print(
                f"- launch_profile={row['launch_profile']}  "
                f"container_profile={row['container_profile']}  "
                f"resolved_name={row['resolved_name'] or '-'}  "
                f"is_running={row['is_running']}  exists={row['exists']}"
            )
            print(
                "- host agent binaries absent from PATH are EXPECTED (provisioned in the container)"
            )
            for warning in row["warnings"]:
                print(f"- warn: {warning}")
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
    # Remote-host probes run only when the operator explicitly opts in via
    # ``--remote``. Default ``uxon doctor`` stays local-only.
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


def _doctor_launch_profile_rows(
    cfg: Config,
    caller_user: str,
    default_launch_user: str,
    agent_paths: dict[str, str | None],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pid in cfg.launch.effective_enabled_profiles:
        profile = cfg.launch.profiles.get(pid)
        if profile is None:
            continue
        selected_user = profile.launch_user or identity.resolve_launch_user(cfg, caller_user)
        container_profile = profile.container_profile
        rows.append(
            {
                "id": pid,
                "agent": profile.agent,
                "launch_user": selected_user,
                "container_profile": container_profile or None,
                "containerized": bool(container_profile),
                "host_agent_path": (
                    agent_paths.get(profile.agent) if selected_user == default_launch_user else None
                ),
            }
        )
    return rows


def _doctor_container_profile_rows(
    cfg: Config,
    cwd: str,
    caller_user: str,
) -> list[dict[str, Any]]:
    """Build non-raising diagnostics for enabled containerized launch profiles."""

    from uxon.domain.container import (
        apply_path_map,
        path_map_under_prefix,
        resolve_profile_container_name,
    )
    from uxon.domain.session import slugify
    from uxon.infra import container as container_infra

    rows: list[dict[str, Any]] = []
    project_slug = slugify(Path(cwd).name)
    for launch_profile_id in cfg.launch.effective_enabled_profiles:
        launch_profile = cfg.launch.profiles.get(launch_profile_id)
        if launch_profile is None or not launch_profile.container_profile:
            continue
        profile = cfg.container_profiles.get(launch_profile.container_profile)
        launch_user = launch_profile.launch_user or identity.resolve_launch_user(cfg, caller_user)
        warnings: list[str] = []
        name = ""
        name_error = ""
        is_running, exists = ("?", "?")
        if profile is None:
            warnings.append(
                f"container profile {launch_profile.container_profile!r} is not configured"
            )
        else:
            try:
                # Validate the mapped cwd: a non-absolute or ``..`` result
                # raises SystemExit (caught below → name_error).
                apply_path_map(cwd, profile.path_map)
                name = resolve_profile_container_name(
                    profile,
                    user=launch_user,
                    launch_profile=launch_profile.id,
                    agent=launch_profile.agent,
                    project_slug=project_slug,
                )
            except SystemExit as exc:
                name_error = str(getattr(exc, "uxon_msg", exc))
            if name:
                is_running, exists = container_infra.probe_container_state_for_profile(
                    profile,
                    name,
                    launch_user,
                    launch_profile=launch_profile.id,
                    agent=launch_profile.agent,
                    project_slug=project_slug,
                )
            if not profile.stop_template:
                warnings.append(
                    "stop_template is unset: the in-container agent is not reaped on kill "
                    "(it orphans); set stop_template on the container profile"
                )
            if profile.path_map and not path_map_under_prefix(cwd, profile.path_map):
                warnings.append(
                    f"path_map is set but the current directory {cwd} is under none of its "
                    "host prefixes — launches from here may hit an unmapped path inside the container"
                )
            definition_under_mount = ""
            if profile.path_map:
                for token in profile.create_template:
                    candidate = (
                        token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
                    )
                    if candidate.startswith("/") and path_map_under_prefix(
                        candidate, profile.path_map
                    ):
                        definition_under_mount = candidate
                        break
            if definition_under_mount:
                warnings.append(
                    f"create_template references {definition_under_mount}, which is under a "
                    "path_map host prefix (inside the agent-writable bind mount). Move the "
                    "container definition to an operator-owned path outside the mount"
                )
        rows.append(
            {
                "launch_profile": launch_profile_id,
                "agent": launch_profile.agent,
                "launch_user": launch_user,
                "container_profile": launch_profile.container_profile,
                "resolved_name": name,
                "name_error": name_error or None,
                "is_running": is_running,
                "exists": exists,
                "host_agent_absence_expected": True,
                "warnings": warnings,
            }
        )
    return rows


def _doctor_remote_rows(cfg: Config) -> list[dict[str, Any]]:
    """Probe each ``[[remote_hosts]]`` peer once for ``uxon doctor --remote``.

    Default ``uxon doctor`` does not probe ``[[remote_hosts]]``. This helper
    runs only when the operator passes ``--remote`` — the explicit gesture for
    fleet health diagnosis. The default invocation still has zero SSH I/O.

    Each peer gets one ``ssh ... uxon list --json`` round-trip with the
    fleet-global SSH multiplex setting; per-host overrides on
    ``host.connect_timeout`` / ``host.total_timeout`` are honoured by
    ``fetch_remote_snapshot``. Errors are surfaced (no fail-soft cache
    fallback masking — the operator wants the truth).

    Returns one dict per peer: ``name``, ``ok`` (bool),
    ``latency_ms`` (int), ``error`` (str | None), ``from_cache`` (bool),
    ``sessions`` (int).
    """
    from uxon.infra.remote.collector import fetch_remote_snapshot

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
        probe = run_query(
            ["sudo", "-n", "-u", creds_user, "--", "true"],
            timeout=0.5,
        )
        if probe.returncode != 0:
            return f"warn:passwordless sudo to {creds_user} unavailable"

    prefix = (
        ["sudo", "-n", "-u", creds_user, "--"] if creds_user and creds_user != current_user else []
    )
    if profile.auth == "gh":
        which = run_query(
            prefix + ["sh", "-c", "command -v gh"],
            timeout=2,
        )
        if which.returncode != 0 or not which.stdout.strip():
            return f"warn:gh not found under {creds_user}"
        status = run_query(
            prefix + ["gh", "auth", "status", "--hostname", profile.host],
            timeout=5,
        )
        if status.returncode != 0:
            return f"warn:gh not logged in to {profile.host}"
        return "ok"
    if profile.auth == "token":
        res = run_query(
            prefix + ["test", "-r", profile.token_file],
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
        cp = run_query(
            ["sudo", "-n", "true"],
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
