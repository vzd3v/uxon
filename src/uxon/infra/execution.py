# SPDX-License-Identifier: MIT
"""Sole adapter for commands in a target user's host execution context."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import secrets
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from uxon.domain.execution import ExecutionConfig, ExecutionIdentity, ExecutionTarget
from uxon.errors import fail
from uxon.infra.identity import process_user
from uxon.infra.run import run_query


class ExecutionConfigured(Protocol):
    @property
    def execution(self) -> ExecutionConfig: ...


@dataclass(frozen=True)
class ExecutionProbe:
    backend: str
    ok: bool
    error: str = ""
    identity: ExecutionIdentity | None = None


def resolve_target(cfg: ExecutionConfigured, user: str) -> ExecutionTarget:
    return ExecutionTarget(user=user, backend=cfg.execution.backend_for_user(user))


def _render(argv: tuple[str, ...], target: ExecutionTarget) -> list[str]:
    return [token.format(user=target.user) for token in argv]


def command_prefix(cfg: ExecutionConfigured, user: str, *, interactive: bool) -> list[str]:
    """Return the only permitted prefix for a target-user command."""
    target = resolve_target(cfg, user)
    backend = target.backend
    if backend.kind == "command":
        return _render(backend.command_prefix, target)
    if process_user() == user:
        return []
    return ["sudo", "-iu", user, "--"] if interactive else ["sudo", "-niu", user, "--"]


def binary_probe_prefix(cfg: ExecutionConfigured, user: str) -> list[str]:
    """Return an argv-safe prefix for target-user binary discovery."""
    target = resolve_target(cfg, user)
    if target.backend.kind == "command":
        return _render(target.backend.command_prefix, target)
    if process_user() == user:
        return []
    return ["sudo", "-n", "-H", "-u", user, "--"]


def wrap_command(
    cfg: ExecutionConfigured, user: str, argv: list[str], *, interactive: bool
) -> list[str]:
    return command_prefix(cfg, user, interactive=interactive) + argv


def probe(cfg: ExecutionConfigured, user: str) -> ExecutionProbe:
    target = resolve_target(cfg, user)
    backend = target.backend
    if backend.kind == "local":
        cmd = wrap_command(cfg, user, ["true"], interactive=False)
        timeout = backend.probe_timeout_seconds
    else:
        return _attest_command_backend(cfg, target)
    try:
        cp = run_query(cmd, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ExecutionProbe(backend=backend.id, ok=False, error=str(exc))
    error = (cp.stderr or cp.stdout or "").strip() if cp.returncode else ""
    return ExecutionProbe(backend=backend.id, ok=cp.returncode == 0, error=error)


def _attest_command_backend(cfg: ExecutionConfigured, target: ExecutionTarget) -> ExecutionProbe:
    from uxon.infra.execution_state import ensure_state_dir

    backend = target.backend
    state_path = ensure_state_dir(Path(cfg.execution.state_dir))
    state_stat = os.stat(state_path, follow_symlinks=False)
    sentinel_name = f".uxon-attest-{secrets.token_hex(16)}"
    sentinel = state_path / sentinel_name
    content = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(sentinel, flags, 0o444)
    try:
        os.fchmod(fd, 0o444)
        os.write(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)
    cmd = _render(backend.command_prefix, target) + [
        sys.executable,
        "-m",
        "uxon.infra.execution_probe",
        "--state-dir",
        str(state_path),
        "--sentinel",
        str(sentinel),
    ]
    try:
        expected_user = pwd.getpwnam(target.user)
        cp = run_query(cmd, timeout=backend.probe_timeout_seconds)
        if cp.returncode != 0:
            return ExecutionProbe(
                backend=backend.id,
                ok=False,
                error=(cp.stderr or cp.stdout or "backend attestation failed").strip(),
            )
        try:
            result = json.loads(cp.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            return ExecutionProbe(backend=backend.id, ok=False, error=f"invalid probe JSON: {exc}")
        expected_keys = {"ok", "identity", "sentinel_sha256", "sentinel_writable"}
        if (
            not isinstance(result, dict)
            or set(result) != expected_keys
            or result.get("ok") is not True
        ):
            return ExecutionProbe(backend=backend.id, ok=False, error="invalid probe result")
        identity_raw = result.get("identity")
        identity_keys = {
            "euid",
            "egid",
            "groups",
            "namespaces",
            "cgroup",
            "capabilities",
            "no_new_privs",
            "state_dev",
            "state_ino",
            "state_writable",
        }
        if not isinstance(identity_raw, dict) or set(identity_raw) != identity_keys:
            return ExecutionProbe(backend=backend.id, ok=False, error="invalid probe identity")
        namespaces = identity_raw.get("namespaces")
        capabilities = identity_raw.get("capabilities")
        groups = identity_raw.get("groups")
        if (
            not isinstance(namespaces, dict)
            or set(namespaces) != {"mnt", "uts", "ipc", "net", "pid"}
            or not all(isinstance(value, int) for value in namespaces.values())
            or not isinstance(capabilities, dict)
            or set(capabilities) != {"CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"}
            or not all(isinstance(value, str) for value in capabilities.values())
            or not isinstance(groups, list)
            or not all(isinstance(value, int) for value in groups)
        ):
            return ExecutionProbe(backend=backend.id, ok=False, error="invalid probe identity")
        expected = {
            "euid": expected_user.pw_uid,
            "egid": expected_user.pw_gid,
            "state_dev": state_stat.st_dev,
            "state_ino": state_stat.st_ino,
            "state_writable": False,
        }
        mismatches = [key for key, value in expected.items() if identity_raw.get(key) != value]
        if result.get("sentinel_sha256") != hashlib.sha256(content).hexdigest():
            mismatches.append("sentinel_sha256")
        if result.get("sentinel_writable") is not False:
            mismatches.append("sentinel_writable")
        if mismatches:
            return ExecutionProbe(
                backend=backend.id,
                ok=False,
                error="backend attestation mismatch: " + ", ".join(mismatches),
            )
        identity = ExecutionIdentity(
            euid=int(identity_raw["euid"]),
            egid=int(identity_raw["egid"]),
            groups=tuple(sorted(groups)),
            namespaces=tuple(sorted((str(key), int(value)) for key, value in namespaces.items())),
            cgroup=str(identity_raw["cgroup"]),
            capabilities=tuple(
                sorted((str(key), str(value)) for key, value in capabilities.items())
            ),
            no_new_privs=int(identity_raw["no_new_privs"]),
            state_dev=int(identity_raw["state_dev"]),
            state_ino=int(identity_raw["state_ino"]),
        )
        return ExecutionProbe(backend=backend.id, ok=True, identity=identity)
    except (OSError, KeyError, subprocess.TimeoutExpired) as exc:
        return ExecutionProbe(backend=backend.id, ok=False, error=str(exc))
    finally:
        try:
            sentinel.unlink()
        except FileNotFoundError:
            pass


def backend_fingerprint(cfg: ExecutionConfigured, user: str) -> str:
    return resolve_target(cfg, user).backend.fingerprint


def launch_compatibility_error(
    cfg: ExecutionConfigured, user: str, sessions: Sequence[object]
) -> str:
    """Return an actionable incompatibility message, or ``""`` when safe."""
    expected = backend_fingerprint(cfg, user)
    incompatible: list[str] = []
    unverifiable: list[str] = []
    for session in sessions:
        if getattr(session, "user", user) != user:
            continue
        name = str(getattr(session, "name", "<unknown>"))
        fingerprint = str(getattr(session, "execution_fingerprint", ""))
        verified = bool(getattr(session, "launch_record_verified", False))
        if not verified or not fingerprint:
            unverifiable.append(name)
        elif fingerprint != expected:
            incompatible.append(name)
    if not incompatible and not unverifiable:
        return ""
    details: list[str] = []
    if incompatible:
        details.append("backend fingerprint mismatch: " + ", ".join(sorted(incompatible)))
    if unverifiable:
        details.append("backend cannot be verified: " + ", ".join(sorted(unverifiable)))
    return (
        f"active tmux sessions for {user!r} use a different or unknown execution backend "
        f"({'; '.join(details)}). Drain them before changing [execution]: "
        f"uxon kill-all --user {user} --force; if the new backend cannot reach the old "
        "socket, restore the previous execution config, drain, then retry"
    )


def ensure_launch_compatible(
    cfg: ExecutionConfigured, user: str, sessions: Sequence[object]
) -> None:
    """Block new workloads when a live tmux server has another backend spec.

    List, attach, and kill remain available so the operator can drain the
    server. Only creating another workload is blocked.
    """
    error = launch_compatibility_error(cfg, user, sessions)
    if error:
        fail(error)
