# SPDX-License-Identifier: MIT
"""Sole adapter for commands in a target user's host execution context."""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
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


def canonicalize_path(cfg: ExecutionConfigured, user: str, path: str, *, intended: bool) -> str:
    """Return the path resolved inside the selected target-user boundary."""
    target = str(Path(path).expanduser())
    if not Path(target).is_absolute():
        target = str(Path.cwd() / target)
    target = os.path.normpath(target)
    backend = cfg.execution.backend_for_user(user)
    if backend.kind == "local":
        from uxon.infra.path_probe import canonical_existing, canonical_intended

        try:
            return canonical_intended(target) if intended else canonical_existing(target)
        except (OSError, ValueError) as exc:
            fail(str(exc))
    cmd = wrap_command(
        cfg,
        user,
        [
            sys.executable,
            "-m",
            "uxon.infra.path_probe",
            "--mode",
            "intended" if intended else "existing",
            "--path",
            target,
        ],
        interactive=False,
    )
    try:
        result = run_query(cmd, timeout=backend.probe_timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f"execution backend could not canonicalize launch path: {exc}")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        fail(f"execution backend returned invalid path probe JSON: {exc}")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"ok", "path", "error"}
        or not isinstance(payload.get("ok"), bool)
        or not isinstance(payload.get("path"), str)
        or not isinstance(payload.get("error"), str)
    ):
        fail("execution backend returned an invalid path probe result")
    if result.returncode != 0 or payload["ok"] is not True:
        fail(payload["error"] or "execution backend could not canonicalize launch path")
    canonical = payload["path"]
    if not canonical.startswith("/") or os.path.normpath(canonical) != canonical:
        fail("execution backend returned a non-canonical launch path")
    return canonical


def probe(cfg: ExecutionConfigured, user: str) -> ExecutionProbe:
    target = resolve_target(cfg, user)
    backend = target.backend
    cmd = wrap_command(
        cfg,
        user,
        [sys.executable, "-m", "uxon.infra.execution_probe"],
        interactive=False,
    )
    try:
        cp = run_query(cmd, timeout=backend.probe_timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ExecutionProbe(backend=backend.id, ok=False, error=str(exc))
    if cp.returncode != 0:
        return ExecutionProbe(
            backend=backend.id,
            ok=False,
            error=(cp.stderr or cp.stdout or "execution probe failed").strip(),
        )
    try:
        result = json.loads(cp.stdout)
        expected = pwd.getpwnam(user)
    except (TypeError, json.JSONDecodeError, KeyError) as exc:
        return ExecutionProbe(backend=backend.id, ok=False, error=f"invalid probe result: {exc}")
    if not isinstance(result, dict) or set(result) != {"euid", "egid"}:
        return ExecutionProbe(backend=backend.id, ok=False, error="invalid probe result")
    if result != {"euid": expected.pw_uid, "egid": expected.pw_gid}:
        return ExecutionProbe(
            backend=backend.id,
            ok=False,
            error=f"execution backend did not enter target user {user!r}",
        )
    return ExecutionProbe(backend=backend.id, ok=True)
