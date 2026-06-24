# SPDX-License-Identifier: MIT
"""Authoritative launch records for managed tmux sessions."""

from __future__ import annotations

import grp
import json
import os
import pwd
import secrets
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import platformdirs

from uxon.domain.launch_profiles import ResolvedLaunchProfile
from uxon.errors import fail

LAUNCH_PROFILE_ENV = "UXON_LAUNCH_PROFILE"
LAUNCH_NONCE_ENV = "UXON_LAUNCH_NONCE"
LAUNCH_AGENT_ENV = "UXON_AGENT"
CONTAINER_PROFILE_ENV = "UXON_CONTAINER_PROFILE"
CONTAINER_PROFILE_FINGERPRINT_ENV = "UXON_CONTAINER_PROFILE_FINGERPRINT"

LAUNCH_RECORD_DIR_ENV = "UXON_LAUNCH_RECORD_DIR"
_STORE_ENV = LAUNCH_RECORD_DIR_ENV
_RECORD_VERSION = 1
_STORE_MODE = 0o711
_RECORD_MODE = 0o644


@dataclass(frozen=True)
class PendingLaunchRecord:
    socket_path: str
    session_name: str
    launch_nonce: str
    launch_profile: str
    agent: str
    launch_user: str
    container_profile: str = ""
    container_profile_fingerprint: str = ""
    container: str = ""


@dataclass(frozen=True)
class TmuxSessionMetadata:
    session_id: str
    created: str
    name: str
    launch_nonce: str


def new_launch_nonce() -> str:
    return secrets.token_urlsafe(24)


def state_dir(*, override: Path | None = None) -> Path:
    if override is not None:
        return override
    configured = os.environ.get(_STORE_ENV)
    if configured:
        return Path(configured)
    # The bootstrap runs as the launch user, so the default store must be
    # readable/traversable by that user while remaining control-plane-owned and
    # not writable by it. Per-user runtime/state directories are commonly 0700,
    # which would deadlock cross-user and containerized launches.
    if os.name == "posix":
        return Path(tempfile.gettempdir()) / f"uxon-launch-records-{os.geteuid()}"
    return Path(platformdirs.user_state_dir("uxon", appauthor=False)) / "launch-records"


def pending_from_resolved(
    *,
    socket_path: str,
    session_name: str,
    resolved: ResolvedLaunchProfile,
    nonce: str | None = None,
) -> PendingLaunchRecord:
    context = resolved.container_context
    return PendingLaunchRecord(
        socket_path=socket_path,
        session_name=session_name,
        launch_nonce=nonce or new_launch_nonce(),
        launch_profile=resolved.profile.id,
        agent=resolved.agent.id,
        launch_user=resolved.launch_user,
        container_profile=context.profile_id if context is not None else "",
        container_profile_fingerprint=context.profile_fingerprint if context is not None else "",
        container=context.name if context is not None else "",
    )


def record_path(record: PendingLaunchRecord, *, override_dir: Path | None = None) -> Path:
    return _record_path(record.socket_path, record.session_name, record.launch_nonce, override_dir)


def _record_path(
    socket_path: str, session_name: str, launch_nonce: str, override_dir: Path | None = None
) -> Path:
    import hashlib

    key = hashlib.sha256(f"{socket_path}\0{session_name}\0{launch_nonce}".encode()).hexdigest()
    return state_dir(override=override_dir) / f"{key}.json"


def create_pending_record(record: PendingLaunchRecord, *, override_dir: Path | None = None) -> Path:
    directory = _ensure_store_ready(
        override_dir=override_dir,
        launch_user=record.launch_user,
        requires_control_plane=bool(record.container_profile)
        or _uid_for_user(record.launch_user) != os.geteuid(),
    )
    path = record_path(record, override_dir=directory)
    payload = _base_payload(record)
    payload["status"] = "pending"
    _write_new_json(path, payload)
    return path


def finalize_pending_record(
    record: PendingLaunchRecord,
    metadata: TmuxSessionMetadata,
    *,
    container_id: str = "",
    container_cgroup: str = "",
    container_epoch: str = "",
    override_dir: Path | None = None,
) -> Path:
    if metadata.name != record.session_name:
        fail(f"tmux created unexpected session {metadata.name!r}; expected {record.session_name!r}")
    if metadata.launch_nonce != record.launch_nonce:
        fail("tmux launch nonce mismatch; refusing to trust the created session")
    directory = _ensure_store_ready(
        override_dir=override_dir,
        launch_user=record.launch_user,
        requires_control_plane=bool(record.container_profile)
        or _uid_for_user(record.launch_user) != os.geteuid(),
    )
    path = record_path(record, override_dir=directory)
    pending = _read_json(path)
    if pending.get("status") != "pending":
        fail("launch record is not pending")
    payload = _base_payload(record)
    payload.update(
        {
            "status": "finalized",
            "tmux_session_id": metadata.session_id,
            "tmux_session_created": metadata.created,
            "tmux_session_name": metadata.name,
            "container_id": container_id,
            "container_cgroup": container_cgroup,
            "container_epoch": container_epoch,
            "finalized_at": time.time(),
        }
    )
    _replace_json(path, payload)
    return path


def fail_pending_record(record: PendingLaunchRecord, *, override_dir: Path | None = None) -> None:
    path = record_path(record, override_dir=override_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        fail(f"unable to remove pending launch record: {exc}")


def read_finalized_record(
    socket_path: str,
    session_name: str,
    launch_nonce: str,
    *,
    override_dir: Path | None = None,
    require_owner: bool = True,
) -> dict[str, Any] | None:
    path = _record_path(socket_path, session_name, launch_nonce, override_dir)
    try:
        payload = _read_json(path, require_owner=require_owner)
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return None
    if payload.get("status") != "finalized":
        return None
    if (
        payload.get("socket_path") != socket_path
        or payload.get("session_name") != session_name
        or payload.get("launch_nonce") != launch_nonce
    ):
        return None
    return payload


def wait_for_finalized_record(
    socket_path: str,
    session_name: str,
    launch_nonce: str,
    *,
    timeout_seconds: float = 60.0,
    poll_seconds: float = 0.05,
    override_dir: Path | None = None,
    require_owner: bool = True,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        record = read_finalized_record(
            socket_path,
            session_name,
            launch_nonce,
            override_dir=override_dir,
            require_owner=require_owner,
        )
        if record is not None:
            return record
        time.sleep(poll_seconds)
    return None


def _base_payload(record: PendingLaunchRecord) -> dict[str, Any]:
    return {
        "version": _RECORD_VERSION,
        "socket_path": record.socket_path,
        "session_name": record.session_name,
        "launch_nonce": record.launch_nonce,
        "launch_profile": record.launch_profile,
        "agent": record.agent,
        "launch_user": record.launch_user,
        "container_profile": record.container_profile,
        "container_profile_fingerprint": record.container_profile_fingerprint,
        "container": record.container,
        "created_at": time.time(),
    }


def _ensure_store_ready(
    *,
    override_dir: Path | None,
    launch_user: str,
    requires_control_plane: bool,
) -> Path:
    directory = state_dir(override=override_dir)
    _ensure_private_dir(directory)
    if requires_control_plane and _user_can_write_dir(_uid_for_user(launch_user), directory):
        fail(
            "launch record store is writable by the launch user; "
            "cross-user and containerized launches require a separate control-plane store"
        )
    return directory


def _ensure_private_dir(path: Path) -> None:
    if not path.is_absolute():
        fail(f"launch record directory must be absolute: {path}")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            current.mkdir(mode=_STORE_MODE)
            st = os.lstat(current)
        if stat.S_ISLNK(st.st_mode):
            fail(f"launch record path must not contain symlinks: {current}")
        if not stat.S_ISDIR(st.st_mode):
            fail(f"launch record path component is not a directory: {current}")
        if st.st_uid not in (0, os.geteuid()):
            fail(f"launch record directory is not owned by the control plane: {current}")
        mode = stat.S_IMODE(st.st_mode)
        if current == path and mode != _STORE_MODE:
            os.chmod(current, _STORE_MODE)
            st = os.lstat(current)
            mode = stat.S_IMODE(st.st_mode)
        if mode & 0o022 and not mode & stat.S_ISVTX:
            fail(f"launch record path component is writable by other users: {current}")


def _write_new_json(path: Path, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, _RECORD_MODE)
    try:
        os.fchmod(fd, _RECORD_MODE)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _replace_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    _write_new_json(tmp, payload)
    os.replace(tmp, path)


def _read_json(path: Path, *, require_owner: bool = True) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    close_fd = True
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("launch record is not a regular file")
        if require_owner and st.st_uid != os.geteuid():
            raise PermissionError("launch record has unsafe ownership")
        if stat.S_IMODE(st.st_mode) & 0o022:
            raise PermissionError("launch record has unsafe ownership or mode")
        with os.fdopen(fd, "rb") as f:
            close_fd = False
            return json.loads(f.read().decode("utf-8"))
    except Exception:
        if close_fd:
            os.close(fd)
        raise


def _uid_for_user(user: str) -> int:
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        fail(f"unknown launch user for launch record checks: {user}")
    raise AssertionError("unreachable")


def _user_can_write_dir(uid: int, path: Path) -> bool:
    st = os.stat(path)
    mode = stat.S_IMODE(st.st_mode)
    if uid == st.st_uid and mode & 0o200:
        return True
    if mode & 0o020 and _user_in_gid(uid, st.st_gid):
        return True
    if mode & 0o002:
        return True
    return False


def _user_in_gid(uid: int, gid: int) -> bool:
    try:
        pw = pwd.getpwuid(uid)
    except KeyError:
        return False
    if pw.pw_gid == gid:
        return True
    try:
        group = grp.getgrgid(gid)
    except KeyError:
        return False
    return pw.pw_name in group.gr_mem
