# SPDX-License-Identifier: MIT
"""Local host server/SSH-link health probes.

Impure adapter: reads ``/proc`` and shells out to ``ss`` to populate the
:class:`ServerStatus` / :class:`LinkHealthStatus` DTOs from
:mod:`uxon.domain.status`. Split out of :mod:`uxon.infra.sessions_probe`
so session collection and host-status reading stay single-responsibility;
the readers cross into the TUI layer (``tui/bridge.py``), hence the
public (no leading underscore) names.
"""

from __future__ import annotations

import os
import re
import subprocess

from uxon.domain.format import (
    _compact_duration,
    _format_bytes,
    _pct,
)
from uxon.domain.status import LinkHealthStatus, ServerStatus


def read_server_status(disk_path: str) -> ServerStatus:
    load = ""
    cpu = ""
    try:
        with open("/proc/loadavg", encoding="utf-8") as fh:
            load = fh.read().split()[0]
        cores = os.cpu_count() or 1
        cpu = f"{(float(load) / cores) * 100:.0f}%"
    except (OSError, ValueError, IndexError):
        pass

    ram = ""
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                value = rest.strip().split()[0]
                meminfo[key] = int(value) * 1024
        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", 0)
        used = total - available
        if total > 0 and used >= 0:
            ram = f"{_format_bytes(used)}/{_format_bytes(total)} {_pct(used, total)}"
    except (OSError, ValueError, IndexError):
        pass

    disk = ""
    try:
        path = disk_path if os.path.exists(disk_path) else "/"
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        available = st.f_bavail * st.f_frsize
        used = total - available
        if total > 0 and used >= 0:
            disk = f"{_format_bytes(used)}/{_format_bytes(total)} {_pct(used, total)}"
    except OSError:
        pass

    uptime = ""
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            uptime = _compact_duration(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        pass

    return ServerStatus(load=load, cpu=cpu, ram=ram, disk=disk, uptime=uptime)


def read_ssh_link_health_status() -> LinkHealthStatus | None:
    ssh_connection = os.environ.get("SSH_CONNECTION", "").strip()
    if not ssh_connection:
        return None
    parts = ssh_connection.split()
    if len(parts) != 4:
        return None
    peer_ip, peer_port, local_ip, local_port = parts
    try:
        cp = subprocess.run(
            ["ss", "-tin"],
            text=True,
            capture_output=True,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None

    def parse_endpoint(endpoint: str) -> tuple[str, str] | None:
        endpoint = endpoint.strip()
        if not endpoint:
            return None
        if endpoint.startswith("[") and "]:" in endpoint:
            host, _, port = endpoint[1:].rpartition("]:")
            return host, port
        host, sep, port = endpoint.rpartition(":")
        if not sep:
            return None
        return host, port

    lines = cp.stdout.splitlines()
    for idx, line in enumerate(lines):
        if not line.startswith("ESTAB"):
            continue
        fields = line.split()
        if len(fields) < 5:
            continue
        local = parse_endpoint(fields[3])
        peer = parse_endpoint(fields[4])
        if local != (local_ip, local_port) or peer != (peer_ip, peer_port):
            continue
        metrics = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
        rtt_match = re.search(r"\brtt:([0-9.]+)/([0-9.]+)", metrics)
        retrans_match = re.search(r"\bretrans:(\d+)(?:/(\d+))?", metrics)
        if not rtt_match:
            return None
        rtt_ms = float(rtt_match.group(1))
        var_ms = float(rtt_match.group(2))
        retrans_now = int(retrans_match.group(1)) if retrans_match else 0
        summary = f"{rtt_ms:.0f}ms rtt | {var_ms:.0f}ms var | retrans {retrans_now}"
        alert = rtt_ms >= 180.0 or var_ms >= 25.0 or retrans_now > 0
        return LinkHealthStatus(
            state="error" if alert else "ok",
            summary=summary,
        )
    return None
