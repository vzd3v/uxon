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
from typing import TYPE_CHECKING

from uxon.domain.host_report import BinaryStatus, HostReport
from uxon.infra.execution import ExecutionConfigured, binary_probe_prefix, resolve_target
from uxon.infra.run import run_query

if TYPE_CHECKING:
    from uxon.domain.agents import AgentSpec

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
        cp = run_query(
            ["sh", "-lc", script],
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
    cfg: ExecutionConfigured,
    names: list[str],
    launch_user: str,
) -> dict[str, str | None]:
    """Resolve binary paths through the selected execution backend.

    Uses one argv-safe ``sh -c`` call with ``command -v`` for each binary.
    It deliberately does not source a login shell; the execution backend owns
    PATH, and agent catalog entries may use absolute binaries.

    The timeout applies to the entire execution-backend + shell subprocess.
    The built-in local backend uses non-interactive sudo for this probe, so a
    password prompt fails closed instead of consuming the timeout budget.

    """
    if not names:
        return {}

    # Build the sh script (same as local, but run as launch_user).
    lines = ["for c in " + " ".join(shlex.quote(n) for n in names) + "; do"]
    lines.append('    printf "%s\\t%s\\n" "$c" "$(command -v "$c" 2>/dev/null)"')
    lines.append("done")
    script = "\n".join(lines)

    try:
        cp = run_query(
            binary_probe_prefix(cfg, launch_user) + ["sh", "-c", script],
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


# tmux is infra, not an agent — its hint stays here. Per-agent install hints
# are now read from the merged catalog (``AgentSpec.install_hint``), so there
# is no second hardcoded agent-hint source to drift (C1).
_INSTALL_HINTS = {
    "tmux": _tmux_install_hint(),
}


# ── Main probe API ───────────────────────────────────────────────────


def probe_host(
    cfg: ExecutionConfigured, launch_user: str, catalog: dict[str, AgentSpec]
) -> HostReport:
    """Probe tmux + every agent in ``catalog`` on the host for ``launch_user``.

    ``catalog`` is the merged agent catalog (``cfg.agents``); passing it
    explicitly keeps the dependency visible and avoids threading a
    ``TuiConfig`` shape into infra.

    Returns a :class:`HostReport` with:
      - ``tmux``: :class:`BinaryStatus` for tmux
      - ``agents``: dict[agent_id -> BinaryStatus] for every agent in
        ``catalog``. Entries with ``path=None`` are not installed (auto-mode
        just omits them; strict-whitelist mode surfaces them as "missing").
        Each agent's ``install_hint`` is read from its catalog entry.
      - ``launch_user``: the user for which the probe was run

    The probe uses ``sh -lc 'command -v X'`` via sudo if ``launch_user``
    differs from the current user, or directly if it's the same user.
    This matches the login-shell semantics used by the launch builder.
    """
    all_agent_ids = list(catalog.keys())
    all_agent_names = [catalog[aid].binary for aid in all_agent_ids]

    # Single round-trip probe: tmux + every catalogued agent.
    probe_names = ["tmux", *all_agent_names]
    if launch_user == _current_user() and resolve_target(cfg, launch_user).backend.kind == "local":
        paths = _resolve_paths_local(probe_names)
    else:
        paths = _resolve_paths_remote(cfg, probe_names, launch_user)

    tmux_status = BinaryStatus(
        name="tmux",
        path=paths.get("tmux"),
        install_hint=_INSTALL_HINTS["tmux"],
    )

    agents: dict[str, BinaryStatus] = {}
    for aid in all_agent_ids:
        spec = catalog[aid]
        agents[aid] = BinaryStatus(
            name=aid,
            path=paths.get(spec.binary),
            install_hint=spec.install_hint,
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
