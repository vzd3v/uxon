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
from typing import TYPE_CHECKING, Any

import platformdirs

from uxon.domain.launch_profiles import ResolvedLaunchProfile
from uxon.errors import fail

LAUNCH_PROFILE_ENV = "UXON_LAUNCH_PROFILE"
LAUNCH_NONCE_ENV = "UXON_LAUNCH_NONCE"
LAUNCH_AGENT_ENV = "UXON_AGENT"
RUNTIME_ENV = "UXON_RUNTIME"
RUNTIME_FINGERPRINT_ENV = "UXON_RUNTIME_FINGERPRINT"

_RECORD_VERSION = 2
_RECORD_MODE = 0o644

if TYPE_CHECKING:
    from uxon.infra.execution import ExecutionConfigured


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


def state_dir(*, override: Path | None = None) -> Path:
    if override is not None:
        return override
    # The bootstrap runs as the launch user, so the default store must be
    # readable/traversable by that user while remaining control-plane-owned and
    # not writable by it. Per-user runtime/state directories are commonly 0700,
    # which would deadlock cross-user and isolated-runtime launches.
    if os.name == "posix":
        return Path(tempfile.gettempdir()) / f"uxon-launch-records-{os.geteuid()}"
    return Path(platformdirs.user_state_dir("uxon", appauthor=False)) / "launch-records"


def execution_state_dir(cfg: ExecutionConfigured, launch_user: str) -> Path:
    backend = cfg.execution.backend_for_user(launch_user)
    if backend.kind == "command":
        return Path(cfg.execution.state_dir)
    return state_dir()


def execution_state_probe_command(
    cfg: ExecutionConfigured, launch_user: str, directory: Path
) -> tuple[str, ...]:
    from uxon.infra.execution import wrap_command

    backend = cfg.execution.backend_for_user(launch_user)
    if backend.kind == "local":
        return ()
    return tuple(wrap_command(cfg, launch_user, ["test", "-x", str(directory)], interactive=False))


def prepare_execution_state(record: PendingLaunchRecord, *, directory: Path) -> Path:
    return _ensure_store_ready(
        override_dir=directory,
        launch_user=record.launch_user,
        requires_control_plane=record.runtime_kind != "direct"
        or _uid_for_user(record.launch_user) != os.geteuid(),
    )


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
    return state_dir(override=override_dir) / f"{key}.json"


def create_pending_record(record: PendingLaunchRecord, *, override_dir: Path | None = None) -> Path:
    directory = _ensure_store_ready(
        override_dir=override_dir,
        launch_user=record.launch_user,
        requires_control_plane=record.runtime_kind != "direct"
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
    runtime_id: str = "",
    runtime_cgroup: str = "",
    runtime_epoch: str = "",
    override_dir: Path | None = None,
) -> Path:
    if metadata.name != record.session_name:
        fail(f"tmux created unexpected session {metadata.name!r}; expected {record.session_name!r}")
    if metadata.launch_nonce != record.launch_nonce:
        fail("tmux launch nonce mismatch; refusing to trust the created session")
    directory = _ensure_store_ready(
        override_dir=override_dir,
        launch_user=record.launch_user,
        requires_control_plane=record.runtime_kind != "direct"
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
            "runtime_id": runtime_id,
            "runtime_cgroup": runtime_cgroup,
            "runtime_epoch": runtime_epoch,
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


def wait_for_finalized_record_path(
    record_path: Path,
    socket_path: str,
    session_name: str,
    launch_nonce: str,
    *,
    timeout_seconds: float = 60.0,
    poll_seconds: float = 0.05,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            payload = _read_json(record_path, require_owner=False)
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            payload = None
        if payload is not None and (
            payload.get("version") == _RECORD_VERSION
            and payload.get("status") == "finalized"
            and payload.get("socket_path") == socket_path
            and payload.get("session_name") == session_name
            and payload.get("launch_nonce") == launch_nonce
        ):
            return payload
        time.sleep(poll_seconds)
    return None


def _base_payload(record: PendingLaunchRecord) -> dict[str, Any]:
    return {
        "version": _RECORD_VERSION,
        "socket_path": record.socket_path,
        "session_name": record.session_name,
        "launch_nonce": record.launch_nonce,
        "profile": record.launch_profile,
        "launch_profile": record.launch_profile,
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
    launch_user: str,
    requires_control_plane: bool,
) -> Path:
    directory = state_dir(override=override_dir)
    _ensure_private_dir(directory)
    if requires_control_plane and _user_can_write_dir(_uid_for_user(launch_user), directory):
        fail(
            "launch record store is writable by the launch user; "
            "cross-user and isolated-runtime launches require a separate control-plane store"
        )
    return directory


def _ensure_private_dir(path: Path) -> None:
    from uxon.infra.execution_state import ensure_state_dir

    ensure_state_dir(path)


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
