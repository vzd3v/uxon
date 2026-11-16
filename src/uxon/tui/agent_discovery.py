"""Presence-only discovery of launch profiles; never executes an agent."""

from __future__ import annotations

from uxon.infra import probes
from uxon.infra.agents import AgentAvailability

from .config import TuiConfig


def discover_agents(cfg: TuiConfig) -> dict[str, AgentAvailability]:
    profiles = cfg.launch_profiles
    if not profiles:
        raise ValueError("TUI requires at least one validated launch profile")
    by_user: dict[str, set[str]] = {}
    for profile in profiles.values():
        agents = by_user.setdefault(profile.launch_user, set())
        if profile.runtime == "direct":
            agents.add(profile.agent)
    reports = {
        user: probes.probe_host(
            cfg,
            user,
            {aid: cfg.agents[aid] for aid in sorted(agent_ids) if aid in cfg.agents},
            check_agents=False,
        )
        for user, agent_ids in by_user.items()
    }
    availability = {}
    for pid in cfg.enabled_profiles:
        profile = profiles.get(pid)
        if profile is None:
            availability[pid] = AgentAvailability(status="missing", error="unknown launch profile")
            continue
        report = reports.get(profile.launch_user)
        if report is None or report.tmux.path is None:
            availability[pid] = AgentAvailability(status="missing", error="tmux not found on PATH")
        elif profile.runtime != "direct":
            availability[pid] = AgentAvailability(status="ok", path=f"runtime:{profile.runtime}")
        else:
            status = report.agents.get(profile.agent)
            if status is not None and status.path is not None:
                availability[pid] = status.availability or AgentAvailability(
                    status="ok", path=status.path
                )
            else:
                spec = cfg.agents.get(profile.agent)
                binary = spec.binary if spec is not None else profile.agent
                availability[pid] = AgentAvailability(
                    status="missing", error=f"{binary} not found on PATH"
                )
    return availability
