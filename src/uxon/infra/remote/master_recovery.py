"""Wedged-master self-heal for the SSH ControlMaster multiplex socket.

A poll/kill slave that times out is almost always blocked at
``unix_wait_for_peer`` against an unresponsive master; the master
survives and every subsequent slave hangs identically. This module
breaks that loop. Imports :func:`_ssh_control_dir` from
:mod:`uxon.infra.remote.ssh_argv` (one-way — ssh_argv never imports
back).
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from uxon.infra.remote.ssh_argv import _ssh_control_dir

if TYPE_CHECKING:
    from uxon.infra.remote_hosts import RemoteHost

# How long any one recovery subprocess is allowed to block. Each step
# (``ssh -O exit``, ``ssh -G``) is bounded independently so a single
# wedged invocation cannot stall the whole sequence.
_RECOVER_STEP_TIMEOUT_SEC = 2


def _resolved_control_path(
    ssh_alias: str,
    control_dir: str,
    *,
    _runner: Any = None,
) -> str | None:
    """Resolve ``%C`` in our default ControlPath template for ``ssh_alias``.

    Uses ``ssh -G`` — the option-resolution mode that prints the effective
    config without opening a connection. Returns the absolute socket path
    or ``None`` on any subprocess failure / timeout / parse miss.

    ``_runner`` is the ``subprocess.run``-compatible seam used by tests;
    a ``None`` default is resolved to the module-level ``subprocess.run``
    at call time so a test that patches ``subprocess.run`` at the module
    level (e.g. via ``mock.patch.object(...subprocess, "run", ...)``)
    propagates into this seam too. Capturing ``subprocess.run`` as a
    direct default would freeze the original reference at def-time and
    bypass any later patch.
    """
    runner = subprocess.run if _runner is None else _runner
    template = f"{control_dir}/ssh-%C"
    try:
        cp = runner(
            ["ssh", "-G", "-o", f"ControlPath={template}", ssh_alias],
            capture_output=True,
            text=True,
            timeout=_RECOVER_STEP_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if cp.returncode != 0:
        return None
    for line in cp.stdout.splitlines():
        # ``ssh -G`` lower-cases option names; the value follows a
        # single space.
        if line.lower().startswith("controlpath "):
            return line.split(" ", 1)[1].strip()
    return None


def _find_master_pid_by_path(control_path: str) -> int | None:
    """Find the SSH ``ControlMaster`` pid by scanning ``/proc``.

    OpenSSH's master writes its argv to ``ssh: <control-path> [mux]``
    once it backgrounds; that string is the value of ``argv[0]``. On
    builds where ``setproctitle`` zeroes the rest of the argv block
    the cmdline collapses to that single NUL-padded field; on builds
    where it doesn't, the original ``argv[1:]`` survives as additional
    NUL-separated tokens. Either way, ``cmdline.split(b"\\x00", 1)[0]``
    is the proctitle. Returns the pid or ``None`` when the master is
    gone, ``/proc`` is unreadable (rare on Linux, possible in stripped
    containers), or the cmdline has been replaced.

    Linux-only — uxon is a Linux tool, so the ``/proc`` dependency is
    fine. Falling back to ``pgrep`` would add another dependency for
    no benefit on supported platforms.
    """
    needle = f"ssh: {control_path} [mux]"
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                first = fh.read().split(b"\x00", 1)[0]
                raw = first.decode("utf-8", errors="ignore")
        except OSError:
            continue
        if raw == needle:
            try:
                return int(entry)
            except ValueError:
                return None
    return None


def recover_wedged_master(
    host: RemoteHost,
    *,
    _runner: Any = None,
    _resolve: Any = None,
    _find_pid: Any = None,
    _kill: Any = None,
) -> None:
    """Best-effort cleanup of a wedged ControlMaster after a slave timeout.

    A poll/kill slave that hits ``subprocess.TimeoutExpired`` is almost
    always blocked at ``unix_wait_for_peer`` against an unresponsive
    master. The slave gets killed by the timeout itself; the master
    survives and every subsequent slave hangs identically. This routine
    breaks that loop:

    1. ``ssh -O exit`` (graceful) — works against any responsive master,
       no-op otherwise. Bounded by :data:`_RECOVER_STEP_TIMEOUT_SEC`.
    2. If the socket file survived, find the master pid via ``/proc``
       and ``SIGKILL`` it.
    3. ``unlink`` the socket file if anything is left.

    Skipped when the host uses a custom ``command_template`` (we don't
    own that argv) or when multiplexing is off (no master to clean).
    Never raises — failure of any step is acceptable, the next tick's
    breaker half-open will get another chance.

    All shell-out points and the kill primitive are dependency-injected
    so tests don't need to fork real ``ssh`` or scan a real ``/proc``.
    """
    if host.command_template:
        return
    # Resolve seams at call time (not at def time) so module-level
    # patches of ``subprocess.run`` / ``os.kill`` propagate into both
    # the production callers (which don't pass kwargs) and any test
    # that patches the module attribute rather than injecting kwargs.
    runner = subprocess.run if _runner is None else _runner
    resolve = _resolved_control_path if _resolve is None else _resolve
    find_pid = _find_master_pid_by_path if _find_pid is None else _find_pid
    kill = os.kill if _kill is None else _kill

    control_dir = _ssh_control_dir()
    template = f"{control_dir}/ssh-%C"

    # Step 1: graceful exit through the control socket.
    try:
        runner(
            ["ssh", "-O", "exit", "-o", f"ControlPath={template}", host.ssh_alias],
            capture_output=True,
            timeout=_RECOVER_STEP_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Step 2: hard-kill any master that survived, then unlink the stale
    # socket so the next ssh becomes a fresh master (a stale socket
    # left behind would keep ``ControlMaster=auto`` trying the slave
    # path and hang). Both steps are gated on the same ``exists()``
    # check so a socket that appeared between Step 1 and Step 2 (a
    # concurrent fetch on the same host raced past us) is left alone
    # rather than killed-and-unlinked from under the new master.
    resolved = resolve(host.ssh_alias, control_dir, _runner=runner)
    if resolved is None:
        return
    socket_path = Path(resolved)
    if not socket_path.exists():
        return
    pid = find_pid(resolved)
    if pid is not None:
        try:
            kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        socket_path.unlink(missing_ok=True)
    except OSError:
        pass
