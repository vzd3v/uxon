# SPDX-License-Identifier: MIT
"""Durable control-plane state for target-user execution backends."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from uxon.errors import fail

STATE_DIR_MODE = 0o711


def ensure_state_dir(path: Path) -> Path:
    if not path.is_absolute():
        fail(f"execution state directory must be absolute: {path}")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            current.mkdir(mode=STATE_DIR_MODE)
            metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode):
            fail(f"execution state path must not contain symlinks: {current}")
        if not stat.S_ISDIR(metadata.st_mode):
            fail(f"execution state path component is not a directory: {current}")
        if metadata.st_uid not in (0, os.geteuid()):
            fail(f"execution state directory is not owned by the control plane: {current}")
        mode = stat.S_IMODE(metadata.st_mode)
        if current == path and mode != STATE_DIR_MODE:
            os.chmod(current, STATE_DIR_MODE)
            mode = STATE_DIR_MODE
        if mode & 0o022 and not mode & stat.S_ISVTX:
            fail(f"execution state path component is writable by other users: {current}")
    return path
