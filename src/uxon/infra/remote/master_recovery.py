"""Bounded native SSH control request after a multiplexed command times out."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from uxon.infra.remote.ssh_argv import _ssh_control_dir
from uxon.infra.run import run_query

if TYPE_CHECKING:
    from uxon.infra.remote_hosts import RemoteHost

# Recovery must not wait indefinitely for an unresponsive master.
_RECOVER_STEP_TIMEOUT_SEC = 2


def recover_wedged_master(host: RemoteHost) -> None:
    """Ask the current master to exit without assuming the timeout is repaired.

    Custom command templates own their transport; callers also skip recovery
    when multiplexing is off. A failed native control request leaves the
    original operation's timeout unchanged. Never infer process or socket
    identity from a title or pathname to force cleanup.
    """
    if host.command_template:
        return
    try:
        control_dir = _ssh_control_dir()
    except OSError:
        return
    template = f"{control_dir}/ssh-%C"

    try:
        run_query(
            [
                "ssh",
                "-O",
                "exit",
                "-o",
                f"ControlPath={template}",
                *host.extra_ssh_options,
                host.ssh_alias,
            ],
            timeout=_RECOVER_STEP_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
