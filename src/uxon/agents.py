"""Agent catalog: declarative data for claude / codex / cursor-agent.

Pure data + small helpers. No textual, no TUI, no cli imports.
``_probe_one`` uses subprocess locally but never at module scope; it is
only called by ``do_doctor`` to fetch the per-agent ``--version`` line
shown for present binaries (the host-wide "is this installed" probe
lives in ``uxon.probes`` since 0.5.x).
"""

from __future__ import annotations

import os
import pwd
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionMode:
    id: str  # "normal" | "auto" | "yolo"
    label: str  # user-facing in TUI
    flags: tuple[str, ...]


@dataclass(frozen=True)
class AgentSpec:
    id: str  # "claude" | "codex" | "cursor"
    binary: str  # executable name on PATH
    session_suffix: str  # "@<id>"
    permission_modes: tuple[PermissionMode, ...]
    install_hint: str  # shown by doctor


CATALOG: dict[str, AgentSpec] = {
    "claude": AgentSpec(
        id="claude",
        binary="claude",
        session_suffix="@claude",
        permission_modes=(
            PermissionMode("normal", "normal", ()),
            PermissionMode("auto", "auto", ("--permission-mode", "auto")),
            PermissionMode("yolo", "yolo (--dsp)", ("--dangerously-skip-permissions",)),
        ),
        install_hint="see https://docs.claude.com/claude-code",
    ),
    "codex": AgentSpec(
        id="codex",
        binary="codex",
        session_suffix="@codex",
        permission_modes=(
            PermissionMode("normal", "normal", ()),
            PermissionMode("auto", "auto (--full-auto)", ("--full-auto",)),
            PermissionMode(
                "yolo",
                "yolo (--dangerously-bypass-approvals-and-sandbox)",
                ("--dangerously-bypass-approvals-and-sandbox",),
            ),
        ),
        install_hint="install: npm i -g @openai/codex",
    ),
    "cursor": AgentSpec(
        id="cursor",
        binary="cursor-agent",
        session_suffix="@cursor",
        permission_modes=(
            PermissionMode("normal", "normal", ()),
            PermissionMode("yolo", "yolo (--yolo)", ("--yolo",)),
        ),
        install_hint="install: curl https://cursor.com/install -fsSL | bash",
    ),
}


def permission_mode_for(agent: AgentSpec, mode_id: str) -> PermissionMode | None:
    for mode in agent.permission_modes:
        if mode.id == mode_id:
            return mode
    return None


# ── Availability probe ───────────────────────────────────────────────


@dataclass(frozen=True)
class AgentAvailability:
    status: str  # "pending" | "ok" | "missing" | "timeout"
    path: str | None = None
    version: str | None = None
    error: str | None = None


PROBE_TIMEOUT_SEC = (
    10.0  # cursor-agent --version is slow (~5-8s); probe is async + parallel, non-blocking
)


def _current_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def _probe_one(
    binary: str,
    launch_user: str | None,
    *,
    timeout_override: float | None = None,
) -> AgentAvailability:
    """Run ``<binary> --version`` (under sudo if launch_user differs from caller)
    and return a status-tagged :class:`AgentAvailability`.

    Used by ``do_doctor`` to render the version line for binaries that
    ``uxon.probes.probe_host`` already confirmed are present. The
    parallel multi-agent driver (``probe_agents``) was removed in 0.5.x
    once the host-wide probe replaced it everywhere except the doctor's
    per-binary version detail.

    ``timeout_override`` lets the doctor caller (which probes in parallel)
    use a tighter 2 s deadline; the TUI host-probe path keeps the 10 s
    default since it is async and off the event loop.
    """
    timeout = PROBE_TIMEOUT_SEC if timeout_override is None else timeout_override
    if launch_user and launch_user != _current_user():
        # Match the login-env semantics that ``command_prefix_for_user``
        # in ``uxon.cli`` uses for the actual launch (``sudo -iu``). The
        # ``-i`` loads the target user's login shell so ``PATH`` picks
        # up npm-global / nvm / ``~/.local/bin`` entries where agents
        # like ``claude`` and ``cursor-agent`` are typically installed.
        # Without ``-i``, sudo's ``secure_path`` hides them and the
        # probe reports "missing" for agents that the launch can
        # actually run.
        cmd = ["sudo", "-niu", launch_user, "--", binary, "--version"]
    else:
        cmd = [binary, "--version"]
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return AgentAvailability(status="missing", error=str(exc))
    except subprocess.TimeoutExpired:
        return AgentAvailability(status="timeout")
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout or "").strip()
        return AgentAvailability(status="missing", error=err or f"exit {cp.returncode}")
    version = (cp.stdout or "").strip().splitlines()[0] if cp.stdout else None
    return AgentAvailability(status="ok", version=version)
