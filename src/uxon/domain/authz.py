# SPDX-License-Identifier: MIT
"""Pure path-authorization predicates.

``lexical_absolute`` / ``is_under`` / ``is_under_allowed_roots`` are pure
string/path predicates (no filesystem access — they reason about path
*shapes*, not the FS). Canonical filesystem paths come only from the selected
execution backend. The filesystem-and-subprocess gates that build on these
predicates live in the composition root for now.
"""

from __future__ import annotations

import os
from pathlib import Path

from uxon.domain.config import Config


def lexical_absolute(path: str) -> str:
    """Normalize an absolute policy path without resolving host-side symlinks."""
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        raise ValueError("policy path must be absolute")
    return os.path.normpath(expanded)


def is_under(path: str, base: str) -> bool:
    path_p = Path(path)
    base_p = Path(base)
    try:
        path_p.relative_to(base_p)
        return True
    except ValueError:
        return False


def is_under_allowed_roots(cfg: Config, path: str) -> bool:
    """Single source of truth for the ``allowed_roots`` whitelist policy.

    Empty ``cfg.allowed_roots`` → no whitelist; any path passes (the
    caller is expected to have its own write/existence gate). Non-empty
    → strict whitelist: ``path`` must sit under one of the listed roots.

    Consumed by every site that gates on ``allowed_roots`` so the
    "empty list = any writable directory" semantics introduced in 3.1.0
    behave uniformly across the launch flow, the new-project flow, the
    project-config discovery walk, and the doctor diagnostics.
    """
    if not cfg.allowed_roots:
        return True
    return any(is_under(path, base) for base in cfg.allowed_roots)
