# SPDX-License-Identifier: MIT
"""Authoritative launch records for managed tmux sessions."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import pwd
import re
import secrets
import stat
import time
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import platformdirs

from uxon.domain.launch_profiles import ResolvedLaunchProfile
from uxon.errors import fail

LAUNCH_PROFILE_ENV = "UXON_LAUNCH_PROFILE"
LAUNCH_NONCE_ENV = "UXON_LAUNCH_NONCE"
LAUNCH_AGENT_ENV = "UXON_AGENT"
RUNTIME_ENV = "UXON_RUNTIME"
RUNTIME_FINGERPRINT_ENV = "UXON_RUNTIME_FINGERPRINT"

_RECORD_VERSION = 2
_STORE_MODE = 0o700
_RECORD_MODE = 0o600
_SHARED_STORE_MODE = 0o2770
_SHARED_RECORD_MODE = 0o640
_STALE_FINALIZED_SECONDS = 7 * 24 * 60 * 60
_STALE_PENDING_SECONDS = 10 * 60
_MAX_GC_RECORDS = 1024
_GC_CURSOR_VERSION = 1
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")


@dataclass(frozen=True)
class PendingLaunchRecord:
    socket_path: str
    session_name: str
    launch_nonce: str
    launch_profile: str
    agent: str
    launch_user: str
    execution_backend: str = "local"
    execution_fingerprint: str = ""
    runtime: str = "direct"
    runtime_kind: str = "direct"
    runtime_fingerprint: str = ""
    runtime_resource: str = ""


@dataclass(frozen=True)
class TmuxSessionMetadata:
    session_id: str
    created: str
    name: str
    launch_nonce: str


def new_launch_nonce() -> str:
    return secrets.token_urlsafe(24)


def default_launch_record_dir(*, override: Path | None = None) -> Path:
    if override is not None:
        return override
    return Path(platformdirs.user_state_dir("uxon", appauthor=False)) / "launch-records"


def handshake_channel(launch_nonce: str, phase: str) -> str:
    """Return a nonce-scoped tmux wait-for channel."""
    if _NONCE_RE.fullmatch(launch_nonce) is None:
        fail("invalid launch nonce for tmux handshake")
    if phase != "release":
        fail("invalid tmux launch handshake phase")
    return f"uxon-launch-{launch_nonce}-{phase}"


def pending_from_resolved(
    *,
    socket_path: str,
    session_name: str,
    resolved: ResolvedLaunchProfile,
    nonce: str | None = None,
) -> PendingLaunchRecord:
    context = resolved.runtime_context
    execution = resolved.execution
    return PendingLaunchRecord(
        socket_path=socket_path,
        session_name=session_name,
        launch_nonce=nonce or new_launch_nonce(),
        launch_profile=resolved.profile.id,
        agent=resolved.agent.id,
        launch_user=resolved.launch_user,
        execution_backend=execution.backend.id if execution is not None else "local",
        execution_fingerprint=(execution.backend.fingerprint if execution is not None else ""),
        runtime=resolved.profile.runtime,
        runtime_kind="command" if context is not None else "direct",
        runtime_fingerprint=context.fingerprint if context is not None else "",
        runtime_resource=context.resource if context is not None else "",
    )


def record_path(record: PendingLaunchRecord, *, override_dir: Path | None = None) -> Path:
    return _record_path(record.socket_path, record.session_name, record.launch_nonce, override_dir)


def _record_path(
    socket_path: str, session_name: str, launch_nonce: str, override_dir: Path | None = None
) -> Path:
    import hashlib

    key = hashlib.sha256(f"{socket_path}\0{session_name}\0{launch_nonce}".encode()).hexdigest()
    return default_launch_record_dir(override=override_dir) / f"{key}.json"


def create_pending_record(
    record: PendingLaunchRecord,
    *,
    override_dir: Path | None = None,
    shared: bool = False,
) -> Path:
    directory = _ensure_store_ready(
        override_dir=override_dir, shared=shared, launch_user=record.launch_user
    )
    path = record_path(record, override_dir=directory)
    payload = _base_payload(record)
    payload["status"] = "pending"
    _write_new_json(path, payload, shared=shared, shared_gid=os.lstat(directory).st_gid)
    return path


def finalize_pending_record(
    record: PendingLaunchRecord,
    metadata: TmuxSessionMetadata,
    *,
    runtime_id: str = "",
    runtime_cgroup: str = "",
    runtime_epoch: str = "",
    override_dir: Path | None = None,
    shared: bool = False,
) -> Path:
    if metadata.name != record.session_name:
        fail(f"tmux created unexpected session {metadata.name!r}; expected {record.session_name!r}")
    if metadata.launch_nonce != record.launch_nonce:
        fail("tmux launch nonce mismatch; refusing to trust the created session")
    directory = _ensure_store_ready(
        override_dir=override_dir, shared=shared, launch_user=record.launch_user
    )
    path = record_path(record, override_dir=directory)
    pending = _read_json(
        path, require_owner=not shared, shared_gid=os.lstat(directory).st_gid if shared else None
    )
    if pending.get("status") != "pending":
        fail("launch record is not pending")
    payload = _base_payload(record)
    payload.update(
        {
            "status": "finalized",
            "tmux_session_id": metadata.session_id,
            "tmux_session_created": metadata.created,
            "tmux_session_name": metadata.name,
            "runtime_id": runtime_id,
            "runtime_cgroup": runtime_cgroup,
            "runtime_epoch": runtime_epoch,
            "finalized_at": time.time(),
        }
    )
    _replace_json(path, payload, shared=shared, shared_gid=os.lstat(directory).st_gid)
    return path


def fail_pending_record(
    record: PendingLaunchRecord,
    *,
    override_dir: Path | None = None,
    shared: bool = False,
) -> None:
    directory = _ensure_store_ready(
        override_dir=override_dir, shared=shared, launch_user=record.launch_user
    )
    path = record_path(record, override_dir=directory)
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
    shared: bool = False,
    launch_user: str = "",
) -> dict[str, Any] | None:
    directory = default_launch_record_dir(override=override_dir)
    try:
        _ensure_store_ready(override_dir=directory, shared=shared, launch_user=launch_user)
    except SystemExit:
        return None
    path = _record_path(socket_path, session_name, launch_nonce, directory)
    try:
        payload = _read_json(
            path,
            require_owner=require_owner and not shared,
            shared_gid=os.lstat(directory).st_gid if shared else None,
        )
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return None
    if payload.get("status") != "finalized":
        return None
    if payload.get("version") != _RECORD_VERSION:
        return None
    if (
        payload.get("socket_path") != socket_path
        or payload.get("session_name") != session_name
        or payload.get("launch_nonce") != launch_nonce
    ):
        return None
    return payload


def read_verified_record(
    socket_path: str,
    metadata: TmuxSessionMetadata,
    *,
    override_dir: Path | None = None,
    require_owner: bool = True,
    shared: bool = False,
    launch_user: str = "",
) -> dict[str, Any] | None:
    """Return the finalized record only when it matches live tmux metadata."""
    if not metadata.launch_nonce:
        return None
    payload = read_finalized_record(
        socket_path,
        metadata.name,
        metadata.launch_nonce,
        override_dir=override_dir,
        require_owner=require_owner,
        shared=shared,
        launch_user=launch_user,
    )
    if payload is None:
        return None
    if (
        payload.get("tmux_session_id") != metadata.session_id
        or payload.get("tmux_session_created") != metadata.created
        or payload.get("tmux_session_name") != metadata.name
    ):
        return None
    return payload


def delete_verified_record(
    socket_path: str,
    metadata: TmuxSessionMetadata,
    *,
    override_dir: Path | None = None,
    shared: bool = False,
    launch_user: str = "",
) -> bool:
    """Delete exactly one record after its matching tmux session was killed."""
    payload = read_verified_record(
        socket_path,
        metadata,
        override_dir=override_dir,
        require_owner=not shared,
        shared=shared,
        launch_user=launch_user,
    )
    if payload is None:
        return False
    path = _record_path(socket_path, metadata.name, metadata.launch_nonce, override_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        fail(f"unable to remove finalized launch record: {exc}")
    return True


def garbage_collect_records(
    live: set[tuple[str, str, str]],
    *,
    override_dir: Path | None = None,
    shared: bool = False,
    launch_user: str,
    now: float | None = None,
) -> int:
    """Remove bounded old records for one authoritatively enumerated user.

    The persisted per-user cursor rotates across every ``.json`` record, so
    unrelated or live entries cannot permanently starve later stale records.
    Records for any other launch user are never deleted. Within the selected
    user's scope, an expired record from a drained retired socket is eligible.
    """
    if not launch_user:
        fail("launch-record collection requires a launch user")
    directory = _ensure_store_ready(
        override_dir=override_dir, shared=shared, launch_user=launch_user
    )
    current = time.time() if now is None else now
    removed = 0
    try:
        entries = sorted(
            (path for path in directory.iterdir() if path.suffix == ".json"),
            key=lambda item: item.name,
        )
    except OSError:
        return 0
    if not entries:
        return 0
    shared_gid = os.lstat(directory).st_gid if shared else None
    cursor_path = _gc_cursor_path(directory, launch_user)
    last_name = _read_gc_cursor(
        cursor_path,
        require_owner=not shared,
        shared_gid=shared_gid,
    )
    if last_name is None:
        return 0
    names = [path.name for path in entries]
    start = bisect_right(names, last_name) if last_name else 0
    ordered = entries[start:] + entries[:start]
    batch = ordered[:_MAX_GC_RECORDS]
    for path in batch:
        try:
            payload = _read_json(path, require_owner=not shared, shared_gid=shared_gid)
            if payload.get("launch_user") != launch_user:
                continue
            key = (
                str(payload.get("socket_path", "")),
                str(payload.get("session_name", "")),
                str(payload.get("launch_nonce", "")),
            )
            status = payload.get("status")
            timestamp = float(payload.get("finalized_at", payload.get("created_at", current)))
            retention = _STALE_PENDING_SECONDS if status == "pending" else _STALE_FINALIZED_SECONDS
            if key in live or current - timestamp < retention:
                continue
            path.unlink()
            removed += 1
        except (OSError, TypeError, ValueError):
            continue
    try:
        _replace_json(
            cursor_path,
            {"version": _GC_CURSOR_VERSION, "last_name": batch[-1].name},
            shared=shared,
            shared_gid=shared_gid,
        )
    except OSError:
        pass
    return removed


def _gc_cursor_path(directory: Path, launch_user: str) -> Path:
    key = hashlib.sha256(launch_user.encode("utf-8")).hexdigest()[:24]
    return directory / f".gc-cursor-{key}"


def _read_gc_cursor(
    path: Path,
    *,
    require_owner: bool,
    shared_gid: int | None,
) -> str | None:
    try:
        payload = _read_json(path, require_owner=require_owner, shared_gid=shared_gid)
    except FileNotFoundError:
        return ""
    except (PermissionError, OSError, ValueError, json.JSONDecodeError):
        return None
    if payload.get("version") != _GC_CURSOR_VERSION or not isinstance(
        payload.get("last_name"), str
    ):
        return None
    return payload["last_name"]


def _base_payload(record: PendingLaunchRecord) -> dict[str, Any]:
    return {
        "version": _RECORD_VERSION,
        "socket_path": record.socket_path,
        "session_name": record.session_name,
        "launch_nonce": record.launch_nonce,
        "profile": record.launch_profile,
        "agent": record.agent,
        "launch_user": record.launch_user,
        "execution_backend": record.execution_backend,
        "execution_fingerprint": record.execution_fingerprint,
        "runtime": record.runtime,
        "runtime_kind": record.runtime_kind,
        "runtime_fingerprint": record.runtime_fingerprint,
        "runtime_resource": record.runtime_resource,
        "created_at": time.time(),
    }


def _ensure_store_ready(
    *,
    override_dir: Path | None,
    shared: bool = False,
    launch_user: str = "",
) -> Path:
    directory = default_launch_record_dir(override=override_dir)
    if shared:
        _validate_shared_dir(directory, launch_user)
    else:
        _ensure_private_dir(directory)
    return directory


def _has_posix_acl(path: Path) -> bool:
    try:
        os.getxattr(path, "system.posix_acl_access", follow_symlinks=False)
    except OSError as exc:
        if exc.errno in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return False
        raise
    return True


def _validate_shared_dir(path: Path, launch_user: str) -> None:
    """Validate the pre-provisioned multi-controller record directory."""
    if not path.is_absolute():
        fail(f"launch_record_dir must be absolute: {path}")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            fail(f"shared launch_record_dir must be pre-provisioned: {path}")
        if stat.S_ISLNK(metadata.st_mode):
            fail(f"launch_record_dir path must not contain symlinks: {current}")
        if not stat.S_ISDIR(metadata.st_mode):
            fail(f"launch_record_dir component is not a directory: {current}")
        mode = stat.S_IMODE(metadata.st_mode)
        if current != path and mode & 0o022 and not mode & stat.S_ISVTX:
            fail(f"launch_record_dir parent is writable by other users: {current}")
    metadata = os.lstat(path)
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid != 0 or mode != _SHARED_STORE_MODE:
        fail("shared launch_record_dir must be root-owned with mode 2770")
    controller_groups = {os.getegid(), *os.getgroups()}
    if metadata.st_gid not in controller_groups and os.geteuid() != 0:
        fail("controller is not a member of launch_record_dir's control group")
    if _has_posix_acl(path):
        fail("shared launch_record_dir must not have a POSIX access ACL")
    if launch_user:
        try:
            account = pwd.getpwnam(launch_user)
            launch_groups = set(os.getgrouplist(launch_user, account.pw_gid))
        except KeyError:
            fail(f"unknown launch user {launch_user!r}")
        if metadata.st_gid in launch_groups:
            fail("launch user must not belong to launch_record_dir's control group")


def _ensure_private_dir(path: Path) -> None:
    if not path.is_absolute():
        fail(f"launch record directory must be absolute: {path}")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            current.mkdir(mode=_STORE_MODE)
            metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode):
            fail(f"launch record path must not contain symlinks: {current}")
        if not stat.S_ISDIR(metadata.st_mode):
            fail(f"launch record path component is not a directory: {current}")
        if metadata.st_uid not in (0, os.geteuid()):
            fail(f"launch record directory is not owned by the control plane: {current}")
        mode = stat.S_IMODE(metadata.st_mode)
        if current == path and mode != _STORE_MODE:
            os.chmod(current, _STORE_MODE)
            mode = stat.S_IMODE(os.lstat(current).st_mode)
        if mode & 0o022 and not mode & stat.S_ISVTX:
            fail(f"launch record path component is writable by other users: {current}")


def _write_new_json(
    path: Path,
    payload: dict[str, Any],
    *,
    shared: bool = False,
    shared_gid: int | None = None,
) -> None:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    mode = _SHARED_RECORD_MODE if shared else _RECORD_MODE
    fd = os.open(path, flags, mode)
    try:
        if shared and shared_gid is not None:
            os.fchown(fd, -1, shared_gid)
        os.fchmod(fd, mode)
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


def _replace_json(
    path: Path,
    payload: dict[str, Any],
    *,
    shared: bool = False,
    shared_gid: int | None = None,
) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    _write_new_json(tmp, payload, shared=shared, shared_gid=shared_gid)
    os.replace(tmp, path)
    flags = os.O_RDONLY | (os.O_DIRECTORY if hasattr(os, "O_DIRECTORY") else 0)
    directory_fd = os.open(path.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_json(
    path: Path,
    *,
    require_owner: bool = True,
    shared_gid: int | None = None,
) -> dict[str, Any]:
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
        mode = stat.S_IMODE(st.st_mode)
        if shared_gid is not None and (st.st_gid != shared_gid or mode != _SHARED_RECORD_MODE):
            raise PermissionError("shared launch record has unsafe group or mode")
        if shared_gid is None and mode & 0o022:
            raise PermissionError("launch record has unsafe ownership or mode")
        with os.fdopen(fd, "rb") as f:
            close_fd = False
            return json.loads(f.read().decode("utf-8"))
    except Exception:
        if close_fd:
            os.close(fd)
        raise
