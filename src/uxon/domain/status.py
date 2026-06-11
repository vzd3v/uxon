# SPDX-License-Identifier: MIT
"""Pure host-status DTOs rendered by the TUI.

The impure readers that populate these (``read_server_status``,
``read_ssh_link_health_status``) live in
:mod:`uxon.infra.host_status_probe`; they import these types from here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServerStatus:
    """Compact host health snapshot rendered on the main TUI screen."""

    load: str = ""
    cpu: str = ""
    ram: str = ""
    disk: str = ""
    uptime: str = ""


@dataclass(frozen=True)
class LinkHealthStatus:
    """Async SSH-path health probe rendered on the main TUI screen."""

    state: str = "hidden"  # hidden | ok | error | info
    summary: str = ""
