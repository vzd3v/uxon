# SPDX-License-Identifier: MIT
"""Sole adapter for commands in a target user's host execution context."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from uxon.domain.execution import ExecutionConfig, ExecutionTarget
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
        cmd = _render(backend.probe_command, target)
        timeout = backend.probe_timeout_seconds
    try:
        cp = run_query(cmd, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ExecutionProbe(backend=backend.id, ok=False, error=str(exc))
    error = (cp.stderr or cp.stdout or "").strip() if cp.returncode else ""
    return ExecutionProbe(backend=backend.id, ok=cp.returncode == 0, error=error)


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
