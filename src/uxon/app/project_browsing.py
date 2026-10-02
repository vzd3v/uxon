# SPDX-License-Identifier: MIT
"""Target-user directory enumeration for the project picker."""

from __future__ import annotations

from uxon.domain.authz import is_under, project_directory
from uxon.domain.config import Config
from uxon.domain.format import compact_time, fmt_epoch
from uxon.errors import fail
from uxon.infra import execution


def list_project_directories(
    cfg: Config, user: str, root: str, *, missing_ok: bool = True
) -> list[tuple[str, str]]:
    """List visible child directories by name with compact modification times."""
    return [
        (entry.name, compact_time(fmt_epoch(str(entry.mtime))))
        for entry in execution.list_directories(cfg, user, root, missing_ok=missing_ok)
    ]


def browse_project_directories(cfg: Config, user: str, relative_path: str) -> list[tuple[str, str]]:
    """Read one existing directory, confined to the target-user project root."""
    try:
        target = project_directory(cfg.new_project_root, relative_path)
    except ValueError as exc:
        fail(str(exc))
    root = execution.canonicalize_path(cfg, user, cfg.new_project_root, intended=False)
    target = execution.canonicalize_path(cfg, user, target, intended=False)
    if not is_under(target, root):
        fail("project directory is outside new_project_root")
    return list_project_directories(cfg, user, target, missing_ok=False)
