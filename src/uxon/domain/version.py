# SPDX-License-Identifier: MIT
"""Pure version-string builder.

The impure readers that gather ``version`` / ``commit`` / ``dirty``
(shelling out to git, reading the package version) live in the
composition root; this module only renders the display string.
"""

from __future__ import annotations


def format_version(version: str, commit: str | None, dirty: bool) -> str:
    """Render the ``uxon version`` display string.

    ``commit`` is the short HEAD sha or ``None`` when not in a git
    checkout; ``dirty`` marks an uncommitted working tree.
    """
    if commit:
        suffix = f"{commit}-dirty" if dirty else commit
        return f"uxon {version} ({suffix})"
    return f"uxon {version}"
