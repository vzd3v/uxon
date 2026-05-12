"""Binary availability probes for tmux and coding agents on a host.

Pure-data dataclasses + a single batched probe. No textual, no TUI imports.
Uses only stdlib: subprocess, shlex, dataclasses, pwd.
"""

from __future__ import annotations

import os
import platform
import pwd
import shlex
import subprocess
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class BinaryStatus:
    """Status of a single binary on the host."""

    name: str  # "tmux" | "claude" | "codex" | "cursor-agent"
    path: str | None  # resolved absolute path or None
    install_hint: str  # ready-to-paste shell command(s)


@dataclass(frozen=True)
class HostReport:
    """Complete host availability snapshot.

    ``agents`` carries one entry per ``CATALOG`` id; consumers decide
    which subset is "in scope" (the strict whitelist from
    ``[agents].enabled`` if non-empty, or the auto-mode set of all
    installed agents otherwise). The previous ``enabled``/``detected``
    split was tied to the now-removed detected-agents banner.
    """

    tmux: BinaryStatus
    agents: dict[str, BinaryStatus]
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

    Uses a single batched `sudo -nHu USER -- sh -lc ...` call with
    `command -v` for each binary name. Timeout is PROBE_TIMEOUT_SEC (2.0 s).

    The timeout applies to the entire sudo + sh subprocess, not to
    detect_passwordless_sudo. If sudo needs a password (no NOPASSWD),
    `sudo -n` fails in ~10 ms (with non-zero exit code), so the 2s budget
    is only ever consumed by an actual hung shell command.

    Why ``-Hu`` and not ``-iu`` (unlike ``command_prefix_for_user`` and
    ``agents._probe_one``): with ``-i`` sudo concatenates everything after
    ``--`` into a single string and runs it via the target's login shell
    ``-c``. The login bash then expands the script BEFORE it reaches the
    inner ``sh -lc``, so loop variables like ``$c`` get substituted in the
    wrong scope (empty) and ``command -v`` returns nothing for every name.
    Dropping ``-i`` removes that double-shell wrap; ``-H`` still pins
    ``HOME`` to the target so the inner ``sh -l`` sources the correct
    ``~/.profile`` and PATH ends up with ``~/.local/bin``, ``nvm`` etc.
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
            ["sudo", "-n", "-H", "-u", launch_user, "--", "sh", "-lc", script],
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


def probe_host(launch_user: str) -> HostReport:
    """Probe tmux + every ``CATALOG`` agent on the host for ``launch_user``.

    Returns a :class:`HostReport` with:
      - ``tmux``: :class:`BinaryStatus` for tmux
      - ``agents``: dict[agent_id -> BinaryStatus] for every agent in
        :data:`uxon.agents.CATALOG`. Entries with ``path=None`` are not
        installed (used for "missing" status in auto-mode this just
        omits them; in strict-whitelist mode the consumer surfaces
        them as "missing").
      - ``launch_user``: the user for which the probe was run

    The probe uses ``sh -lc 'command -v X'`` via sudo if ``launch_user``
    differs from the current user, or directly if it's the same user.
    This matches the login-shell semantics used by the launch builder.
    """
    from uxon import agents as uxon_agents

    all_agent_names = [s.binary for s in uxon_agents.CATALOG.values()]
    all_agent_ids = list(uxon_agents.CATALOG.keys())

    # Single round-trip probe: tmux + every CATALOG agent.
    probe_names = ["tmux", *all_agent_names]
    if launch_user == _current_user():
        paths = _resolve_paths_local(probe_names)
    else:
        paths = _resolve_paths_remote(probe_names, launch_user)

    tmux_status = BinaryStatus(
        name="tmux",
        path=paths.get("tmux"),
        install_hint=_INSTALL_HINTS["tmux"],
    )

    agents: dict[str, BinaryStatus] = {}
    for aid in all_agent_ids:
        binary_name = uxon_agents.CATALOG[aid].binary
        agents[aid] = BinaryStatus(
            name=aid,
            path=paths.get(binary_name),
            install_hint=_INSTALL_HINTS.get(binary_name, ""),
        )

    return HostReport(
        tmux=tmux_status,
        agents=agents,
        launch_user=launch_user,
    )


# ── Host metrics probe ───────────────────────────────────────────────

_PROC = "/proc"
_CPU_DELAY_S = 0.05


@dataclass(frozen=True, slots=True)
class HostStatsResult:
    """Concrete shape returned by :func:`read_host_stats`.

    Mirrors the wire-schema ``HostStats`` typeddict; converted to a
    plain ``dict`` by the envelope builder before serialisation.
    """

    cpu_pct: float
    mem_used_kib: int
    mem_total_kib: int
    loadavg_1m: float
    uptime_s: int
    kernel: str


def _cpu_busy_pair() -> tuple[int, int]:
    with open(f"{_PROC}/stat") as fh:
        head = fh.readline()
    fields = [int(x) for x in head.split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    total = sum(fields)
    return total - idle, total


def _read_meminfo() -> tuple[int, int]:
    try:
        with open(f"{_PROC}/meminfo") as fh:
            blob = fh.read()
    except FileNotFoundError:
        return 0, 0
    total = 0
    avail = 0
    for line in blob.splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            avail = int(line.split()[1])
    return total, avail


def _read_loadavg_1m() -> float:
    try:
        with open(f"{_PROC}/loadavg") as fh:
            return float(fh.read().split()[0])
    except FileNotFoundError:
        return 0.0


def _read_uptime() -> int:
    try:
        with open(f"{_PROC}/uptime") as fh:
            return int(float(fh.read().split()[0]))
    except FileNotFoundError:
        return 0


def read_host_stats() -> HostStatsResult:
    """Sample /proc for one host_stats snapshot. Stdlib only.

    Two ``/proc/stat`` reads ~50 ms apart yield a CPU delta. Memory
    / loadavg / uptime are single-shot. ``kernel`` is ``platform.release()``.
    """
    busy_a, total_a = _cpu_busy_pair()
    if _CPU_DELAY_S > 0:
        time.sleep(_CPU_DELAY_S)
    busy_b, total_b = _cpu_busy_pair()
    cpu_pct = 0.0 if total_b <= total_a else 100.0 * (busy_b - busy_a) / (total_b - total_a)
    total_kib, avail_kib = _read_meminfo()
    used_kib = max(0, total_kib - avail_kib) if total_kib else 0
    return HostStatsResult(
        cpu_pct=max(0.0, min(100.0, cpu_pct)),
        mem_used_kib=used_kib,
        mem_total_kib=total_kib,
        loadavg_1m=_read_loadavg_1m(),
        uptime_s=_read_uptime(),
        kernel=platform.release(),
    )
