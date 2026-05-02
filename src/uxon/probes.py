"""Binary availability probes for tmux and coding agents on a host.

Pure-data dataclasses + a single batched probe. No textual, no TUI imports.
Uses only stdlib: subprocess, shlex, dataclasses, pwd.
"""

from __future__ import annotations

import os
import pwd
import shlex
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class BinaryStatus:
    """Status of a single binary on the host."""

    name: str  # "tmux" | "claude" | "codex" | "cursor-agent"
    path: str | None  # resolved absolute path or None
    install_hint: str  # ready-to-paste shell command(s)


@dataclass(frozen=True)
class HostReport:
    """Complete host availability snapshot."""

    tmux: BinaryStatus
    enabled: dict[str, BinaryStatus]  # keys = cfg.enabled_agents (agent ids)
    detected: dict[str, BinaryStatus]  # keys = CATALOG ids NOT in enabled, but installed
    launch_user: str


# ── Probe implementation ─────────────────────────────────────────────

PROBE_TIMEOUT_SEC = 2.0  # `command -v` is fast; 2s is plenty for hung shells


def _current_user() -> str:
    """Return the effective user of the running process."""
    return pwd.getpwuid(os.getuid()).pw_name


def _resolve_paths_local(names: list[str]) -> dict[str, str | None]:
    """Resolve paths for binaries using `sh -lc 'command -v X'` (same user).

    Returns a dict mapping binary name to absolute path (or None if not found).
    """
    if not names:
        return {}

    # Build the sh script: for each name, output "name\tpath" or "name\t(empty)" on not found.
    lines = ["for c in " + " ".join(shlex.quote(n) for n in names) + "; do"]
    lines.append('    printf "%s\\t%s\\n" "$c" "$(command -v "$c" 2>/dev/null)"')
    lines.append("done")
    script = "\n".join(lines)

    try:
        cp = subprocess.run(
            ["sh", "-lc", script],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SEC,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # sh not found or timeout: treat all as missing.
        return {name: None for name in names}

    result: dict[str, str | None] = {}
    if cp.returncode == 0:
        for line in (cp.stdout or "").splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                name, path = parts
                result[name] = path if path else None
    # Fill in any missing results as None.
    for name in names:
        if name not in result:
            result[name] = None
    return result


def _resolve_paths_remote(
    names: list[str],
    launch_user: str,
) -> dict[str, str | None]:
    """Resolve paths for binaries on a different user via sudo.

    Uses a single batched `sudo -niu USER -- sh -lc ...` call with
    `command -v` for each binary name. Timeout is PROBE_TIMEOUT_SEC (2.0 s).

    The timeout applies to the entire sudo + sh subprocess, not to
    detect_passwordless_sudo. If sudo needs a password (no NOPASSWD),
    `sudo -n` fails in ~10 ms (with non-zero exit code), so the 2s budget
    is only ever consumed by an actual hung shell command.
    """
    if not names:
        return {}

    # Build the sh script (same as local, but run as launch_user).
    lines = ["for c in " + " ".join(shlex.quote(n) for n in names) + "; do"]
    lines.append('    printf "%s\\t%s\\n" "$c" "$(command -v "$c" 2>/dev/null)"')
    lines.append("done")
    script = "\n".join(lines)

    try:
        cp = subprocess.run(
            ["sudo", "-n", "-iu", launch_user, "--", "sh", "-lc", script],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SEC,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # sudo not found or timeout: treat all as missing.
        return {name: None for name in names}

    result: dict[str, str | None] = {}
    if cp.returncode == 0:
        for line in (cp.stdout or "").splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                name, path = parts
                result[name] = path if path else None
    # If sudo failed (returncode != 0, e.g., no NOPASSWD), treat all as missing.
    # Fill in any missing results as None.
    for name in names:
        if name not in result:
            result[name] = None
    return result


# ── Install hints ────────────────────────────────────────────────────


def _tmux_install_hint() -> str:
    """Return a ready-to-paste install command for tmux."""
    return "sudo apt install tmux  # Debian/Ubuntu\nsudo dnf install tmux  # Fedora/RHEL"


def _claude_install_hint() -> str:
    """Return a ready-to-paste install command for claude."""
    return "npm install -g @anthropic-ai/claude-code"


def _codex_install_hint() -> str:
    """Return a ready-to-paste install command for codex."""
    return "npm install -g @openai/codex"


def _cursor_install_hint() -> str:
    """Return a ready-to-paste install command for cursor-agent."""
    return "curl https://cursor.com/install -fsSL | bash"


_INSTALL_HINTS = {
    "tmux": _tmux_install_hint(),
    "claude": _claude_install_hint(),
    "codex": _codex_install_hint(),
    "cursor-agent": _cursor_install_hint(),
}


# ── Main probe API ───────────────────────────────────────────────────


def probe_host(cfg, launch_user: str) -> HostReport:
    """Probe all binaries on the host for the given launch_user.

    Returns a HostReport with:
      - tmux: BinaryStatus for tmux
      - enabled: dict[agent_id -> BinaryStatus] for cfg.enabled_agents
      - detected: dict[agent_id -> BinaryStatus] for agents in the CATALOG
                  that are installed but not in enabled_agents
      - launch_user: the user for which the probe was run

    The probe uses `sh -lc 'command -v X'` via sudo if launch_user differs
    from the current user, or directly if it's the same user. This matches
    the login-shell semantics used by the launch builder.
    """
    from uxon import agents as uxon_agents

    # Determine which binaries to probe.
    tmux_names = ["tmux"]
    enabled_agent_names = []
    enabled_agent_ids = []
    for aid in cfg.enabled_agents:
        if aid in uxon_agents.CATALOG:
            enabled_agent_names.append(uxon_agents.CATALOG[aid].binary)
            enabled_agent_ids.append(aid)

    all_agent_names = [s.binary for s in uxon_agents.CATALOG.values()]
    all_agent_ids = list(uxon_agents.CATALOG.keys())

    # Single round-trip probe: tmux + all agents.
    probe_names = tmux_names + all_agent_names
    if launch_user == _current_user():
        paths = _resolve_paths_local(probe_names)
    else:
        paths = _resolve_paths_remote(probe_names, launch_user)

    # Build tmux status.
    tmux_path = paths.get("tmux")
    tmux_status = BinaryStatus(
        name="tmux",
        path=tmux_path,
        install_hint=_INSTALL_HINTS["tmux"],
    )

    # Build enabled agents dict.
    enabled: dict[str, BinaryStatus] = {}
    for aid, binary_name in zip(enabled_agent_ids, enabled_agent_names, strict=True):
        path = paths.get(binary_name)
        enabled[aid] = BinaryStatus(
            name=aid,
            path=path,
            install_hint=_INSTALL_HINTS.get(binary_name, ""),
        )

    # Build detected agents dict (those installed but not in enabled).
    detected: dict[str, BinaryStatus] = {}
    for aid in all_agent_ids:
        if aid not in cfg.enabled_agents:
            binary_name = uxon_agents.CATALOG[aid].binary
            path = paths.get(binary_name)
            if path is not None:  # only include if actually found
                detected[aid] = BinaryStatus(
                    name=aid,
                    path=path,
                    install_hint=_INSTALL_HINTS.get(binary_name, ""),
                )

    return HostReport(
        tmux=tmux_status,
        enabled=enabled,
        detected=detected,
        launch_user=launch_user,
    )
