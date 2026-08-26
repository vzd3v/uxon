# SPDX-License-Identifier: MIT
"""Target-side ownership boundary for a dedicated tmux socket directory."""

from __future__ import annotations

import argparse
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class SocketDirectoryAbsentError(FileNotFoundError):
    """The dedicated socket directory has not been created yet."""


class SocketDirectoryError(RuntimeError):
    """The dedicated socket directory does not satisfy its security contract."""


def _require_safe_ancestor(directory_fd: int) -> None:
    metadata = os.fstat(directory_fd)
    permissions = stat.S_IMODE(metadata.st_mode)
    writable_by_other_authority = bool(permissions & 0o022)
    sticky = bool(permissions & stat.S_ISVTX)
    if writable_by_other_authority and not sticky:
        raise SocketDirectoryError(
            "tmux socket path crosses a group- or world-writable non-sticky directory"
        )


def _normalized_socket_path(raw_path: Path) -> Path:
    value = str(raw_path)
    if not value.startswith("/") or os.path.normpath(value) != value:
        raise SocketDirectoryError("tmux socket path must be normalized and absolute")
    if raw_path.name in {"", ".", ".."}:
        raise SocketDirectoryError("tmux socket path must have a file name")
    return raw_path


@contextmanager
def open_socket_parent(
    socket_path: Path,
    *,
    create: bool = False,
) -> Iterator[tuple[int, str]]:
    """Open a no-symlink parent and verify it is private to the effective UID."""
    socket_path = _normalized_socket_path(socket_path)
    components = socket_path.parent.parts[1:]
    if not components:
        raise SocketDirectoryError("tmux socket directory must not be the filesystem root")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_fd = os.open("/", flags)
    try:
        _require_safe_ancestor(directory_fd)
        for index, component in enumerate(components):
            final = index == len(components) - 1
            try:
                next_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    raise SocketDirectoryAbsentError(str(socket_path.parent)) from None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(component, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise SocketDirectoryError(
                    f"cannot open tmux socket directory without symlinks: {exc}"
                ) from exc
            os.close(directory_fd)
            directory_fd = next_fd
            _require_safe_ancestor(directory_fd)
            if final:
                metadata = os.fstat(directory_fd)
                permissions = stat.S_IMODE(metadata.st_mode)
                if metadata.st_uid != os.geteuid():
                    raise SocketDirectoryError(
                        "tmux socket directory is not owned by the launch user"
                    )
                if permissions != 0o700:
                    raise SocketDirectoryError("tmux socket directory permissions must be 0700")
        yield directory_fd, socket_path.name
    finally:
        os.close(directory_fd)


def prepare_socket_parent(socket_path: Path) -> None:
    with open_socket_parent(socket_path, create=True):
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uxon tmux-socket")
    parser.add_argument("--prepare", action="store_true", required=True)
    parser.add_argument("--socket", required=True)
    ns = parser.parse_args(argv)
    try:
        prepare_socket_parent(Path(ns.socket))
    except (OSError, SocketDirectoryError) as exc:
        parser.exit(1, f"uxon: cannot prepare tmux socket directory: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
