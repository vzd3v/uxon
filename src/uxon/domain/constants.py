# SPDX-License-Identifier: MIT
"""Pure constants shared across the domain layer."""

from __future__ import annotations

# Known agent ids. Kept in sync with uxon_agents.CATALOG (verified by tests);
# declared here as a literal so CLI parsing doesn't need the lazy lib import.
VALID_AGENT_IDS: tuple[str, ...] = ("claude", "codex", "cursor")
