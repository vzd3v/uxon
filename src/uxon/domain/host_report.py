# SPDX-License-Identifier: MIT
"""Pure host-availability data types.

``BinaryStatus`` / ``HostReport`` are immutable, stdlib-only DTOs. The
impure probe machinery that *produces* them lives in :mod:`uxon.infra.probes`
(an infra adapter); it imports these types from here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BinaryStatus:
    """Status of a single binary on the host."""

    name: str  # "tmux" or an open-ended agent id from the merged catalog
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
