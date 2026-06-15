# SPDX-License-Identifier: MIT
"""Agent-selection use-case.

:func:`resolve_agent_id` picks an agent and verifies its binary is on
PATH for the launch user. It is impure: when ``report is None`` it probes
the host via :mod:`uxon.infra.probes` (subprocess). The ``probes`` and
``agents`` imports are kept lazy (in-function) so that importing this
module does not pull them into the eager graph of ``uxon.cli`` — the
CLI-startup latency invariant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from uxon.domain.config import Config
from uxon.errors import fail

if TYPE_CHECKING:
    from uxon.domain.host_report import HostReport


def resolve_agent_id(
    cfg: Config,
    launch_user: str,
    requested: str | None,
    *,
    report: HostReport | None = None,
) -> str:
    """Pick an agent to launch and verify the binary is on PATH.

    Policy precedence:

    1. ``--agent <id>`` if given (must be valid + in whitelist).
    2. ``cfg.default_agent`` if set.
    3. ``cfg.enabled_agents[0]`` (strict mode).
    4. Auto-mode (empty whitelist, no default): the first installed
       catalogued agent (``cfg.agents`` order).

    Whatever the policy picks, this function probes the host once
    (or reuses ``report``) and verifies the binary is actually
    installed for ``launch_user``. Missing binaries fail with a
    uxon-level message rather than punting to a tmux ``execvp``
    failure. ``report`` is the optional escape-hatch for callers
    that already probed (TUI, doctor) — pass it to avoid the
    double round-trip.

    When ``cfg.container.enabled``, the agent runs inside the operator's
    container, so host presence is irrelevant: agent **resolution** still
    runs by the normal precedence (the launch request needs the resolved
    ``spec.binary``), but the host-presence **gate** is suppressed — a
    host-absent binary no longer fails the launch. The host probe still
    *runs* (so doctor/version surfaces see the real host landscape). In that
    mode auto-pick has no host signal to choose from, so auto-mode (no
    ``--agent``, empty ``agents.enabled``, no ``agents.default``) requires an
    explicit agent rather than silently misbehaving.
    """
    if requested and requested not in cfg.agents:
        fail(f"--agent must be one of {tuple(cfg.agents)}, got {requested!r}")
    if requested and cfg.enabled_agents and requested not in cfg.enabled_agents:
        fail(f"agent {requested!r} is not in agents.enabled={list(cfg.enabled_agents)}")

    if requested:
        candidate, source = requested, "--agent"
    elif cfg.default_agent:
        candidate, source = cfg.default_agent, "agents.default"
    elif cfg.enabled_agents:
        candidate, source = cfg.enabled_agents[0], "agents.enabled"
    else:
        candidate, source = None, "auto"

    container_enabled = cfg.container.enabled

    # Container mode + auto: the suppressed host gate (below) leaves the
    # auto-fallback loop no host signal to pick from, so require an explicit
    # agent BEFORE the probe/fallback. Off-path (no container) is unchanged.
    if container_enabled and candidate is None:
        fail(
            "container mode is enabled but no agent is selected; the agent runs "
            "inside the container so uxon cannot auto-detect one from the host. "
            "Pass --agent <id> or set agents.default / agents.enabled in the repo config.",
            1,
        )

    from uxon.infra import probes as uxon_probes

    if report is None:
        report = uxon_probes.probe_host(launch_user, cfg.agents)

    if candidate is not None:
        # Under container mode the agent is provisioned in the container, not
        # on the host — keep RESOLUTION but suppress the host-presence GATE
        # (the probe above still ran, for doctor/version visibility).
        if container_enabled:
            return candidate
        status = report.agents.get(candidate)
        if status is None or status.path is None:
            hint = status.install_hint if status is not None else ""
            fail(
                f"agent {candidate!r} (from {source}) is not installed for "
                f"{launch_user!r}." + (f"\n{hint}" if hint else ""),
                1,
            )
        return candidate

    for aid in cfg.agents:
        status = report.agents.get(aid)
        if status is not None and status.path is not None:
            return aid
    fail(
        f"no agent binary found on PATH for {launch_user!r}. "
        f"Install one of {tuple(cfg.agents)} or set agents.enabled / "
        "agents.default in the repo config.",
        1,
    )
    raise AssertionError("unreachable")  # fail() never returns
