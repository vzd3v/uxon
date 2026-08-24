# SPDX-License-Identifier: MIT
"""Pure per-target sudo capability DTO.

Populated by the impure probe in :mod:`uxon.infra.sudo_probe` and
consumed by the TUI; defined here so neither the TUI nor the probe owns
the type and the infra layer has no upward dependency on ``tui``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SudoCapability:
    """Per-target sudo snapshot consumed by the TUI.

    ``reachable_users`` is the subset of ``session_users`` the caller
    can enter through their configured execution backends. The set is
    frozen so consumers can hash or store it safely.
    """

    reachable_users: frozenset[str] = frozenset()
