"""Agent catalog: declarative data for claude / codex / cursor-agent.

Pure data + small helpers. No textual, no TUI, no bin/ccw imports.
`probe_agents` uses subprocess locally but never at module scope.
"""
from __future__ import annotations

import os
import pwd
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionMode:
    id: str            # "normal" | "auto" | "yolo"
    label: str         # user-facing in TUI
    flags: tuple[str, ...]


@dataclass(frozen=True)
class AgentSpec:
    id: str                      # "claude" | "codex" | "cursor"
    binary: str                  # executable name on PATH
    session_suffix: str          # "@<id>"
    permission_modes: tuple[PermissionMode, ...]
    install_hint: str            # shown by doctor


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
    status: str                  # "pending" | "ok" | "missing" | "timeout"
    path: str | None = None
    version: str | None = None
    error: str | None = None


PROBE_TIMEOUT_SEC = 10.0  # cursor-agent --version is slow (~5-8s); probe is async + parallel, non-blocking


def _current_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def _build_probe_cmd(binary: str, launch_user: str | None) -> list[str]:
    if launch_user and launch_user != _current_user():
        # Match the login-env semantics that ``command_prefix_for_user``
        # in ``bin/ccw`` uses for the actual launch (``sudo -iu``). The
        # ``-i`` loads the target user's login shell so ``PATH`` picks
        # up npm-global / nvm / ``~/.local/bin`` entries where agents
        # like ``claude`` and ``cursor-agent`` are typically installed.
        # Without ``-i``, sudo's ``secure_path`` hides them and the
        # probe reports "missing" for agents that the launch can
        # actually run.
        return ["sudo", "-niu", launch_user, "--", binary, "--version"]
    return [binary, "--version"]


def _probe_one(binary: str, launch_user: str | None) -> AgentAvailability:
    cmd = _build_probe_cmd(binary, launch_user)
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SEC,
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


def probe_agents(
    agent_ids: list[str],
    launch_user: str | None,
) -> dict[str, AgentAvailability]:
    """Probe `<binary> --version` for each known agent id, in parallel.

    Unknown ids are silently dropped. Runs under ``launch_user`` when
    different from the caller, via ``sudo -n -u <user>``.
    """
    valid = [aid for aid in agent_ids if aid in CATALOG]
    if not valid:
        return {}
    results: dict[str, AgentAvailability] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(valid))) as ex:
        futures = {
            ex.submit(_probe_one, CATALOG[aid].binary, launch_user): aid
            for aid in valid
        }
        for fut in futures:
            aid = futures[fut]
            try:
                results[aid] = fut.result()
            except Exception as exc:  # pragma: no cover — defensive
                results[aid] = AgentAvailability(status="missing", error=str(exc))
    return results
