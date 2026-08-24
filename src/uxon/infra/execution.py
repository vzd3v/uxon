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
from typing import Protocol, cast

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


@dataclass(frozen=True)
class PathFacts:
    path: str
    exists: bool
    directory: bool
    writable: bool
    nearest_existing_ancestor: str


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    mtime: int


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
    prefix = ["/usr/bin/sudo"]
    if not interactive:
        prefix.append("-n")
    return [*prefix, "-H", "-u", user, "--"]


def binary_probe_prefix(cfg: ExecutionConfigured, user: str) -> list[str]:
    """Return an argv-safe prefix for target-user binary discovery."""
    target = resolve_target(cfg, user)
    if target.backend.kind == "command":
        return _render(target.backend.command_prefix, target)
    if process_user() == user:
        return []
    return ["/usr/bin/sudo", "-n", "-H", "-u", user, "--"]


def wrap_command(
    cfg: ExecutionConfigured, user: str, argv: list[str], *, interactive: bool
) -> list[str]:
    return command_prefix(cfg, user, interactive=interactive) + argv


def canonicalize_path(cfg: ExecutionConfigured, user: str, path: str, *, intended: bool) -> str:
    """Return the path resolved inside the selected target-user boundary."""
    target = str(Path(path).expanduser())
    if not Path(target).is_absolute():
        target = str(Path.cwd() / target)
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


def path_facts(cfg: ExecutionConfigured, user: str, path: str) -> PathFacts:
    """Inspect a path inside the selected target-user filesystem boundary."""
    target = str(Path(path).expanduser())
    if not Path(target).is_absolute():
        target = str(Path.cwd() / target)
    backend = cfg.execution.backend_for_user(user)
    if backend.kind == "local":
        from uxon.infra.path_probe import inspect

        try:
            payload = inspect(target)
        except (OSError, ValueError) as exc:
            fail(str(exc))
    else:
        cmd = wrap_command(
            cfg,
            user,
            [sys.executable, "-m", "uxon.infra.path_probe", "--mode", "inspect", "--path", target],
            interactive=False,
        )
        try:
            result = run_query(cmd, timeout=backend.probe_timeout_seconds)
        except (OSError, subprocess.TimeoutExpired) as exc:
            fail(f"execution backend could not inspect target path: {exc}")
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            fail(f"execution backend returned invalid path facts JSON: {exc}")
        if result.returncode != 0:
            error = payload.get("error", "") if isinstance(payload, dict) else ""
            fail(error or "execution backend could not inspect target path")
    expected = {
        "ok",
        "path",
        "exists",
        "directory",
        "writable",
        "nearest_existing_ancestor",
        "error",
    }
    path_value = payload.get("path") if isinstance(payload, dict) else None
    exists_value = payload.get("exists") if isinstance(payload, dict) else None
    directory_value = payload.get("directory") if isinstance(payload, dict) else None
    writable_value = payload.get("writable") if isinstance(payload, dict) else None
    ancestor_value = payload.get("nearest_existing_ancestor") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != expected
        or payload.get("ok") is not True
        or not isinstance(path_value, str)
        or not isinstance(exists_value, bool)
        or not isinstance(directory_value, bool)
        or not isinstance(writable_value, bool)
        or not isinstance(ancestor_value, str)
        or not isinstance(payload.get("error"), str)
    ):
        fail("execution backend returned an invalid path facts result")
    for value in (path_value, ancestor_value):
        if value and (not value.startswith("/") or os.path.normpath(value) != value):
            fail("execution backend returned a non-canonical path fact")
    return PathFacts(
        path=path_value,
        exists=exists_value,
        directory=directory_value,
        writable=writable_value,
        nearest_existing_ancestor=ancestor_value,
    )


def list_directories(cfg: ExecutionConfigured, user: str, path: str) -> tuple[DirectoryEntry, ...]:
    """List direct child directories inside the target filesystem boundary."""
    target = str(Path(path).expanduser())
    if not Path(target).is_absolute():
        target = str(Path.cwd() / target)
    backend = cfg.execution.backend_for_user(user)
    if backend.kind == "local":
        from uxon.infra.path_probe import list_directories as inspect_directories

        try:
            payload = inspect_directories(target)
        except (OSError, ValueError) as exc:
            fail(str(exc))
    else:
        cmd = wrap_command(
            cfg,
            user,
            [
                sys.executable,
                "-m",
                "uxon.infra.path_probe",
                "--mode",
                "list-directories",
                "--path",
                target,
            ],
            interactive=False,
        )
        try:
            result = run_query(cmd, timeout=backend.probe_timeout_seconds)
        except (OSError, subprocess.TimeoutExpired) as exc:
            fail(f"execution backend could not list target directory: {exc}")
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            fail(f"execution backend returned invalid directory listing JSON: {exc}")
        if result.returncode != 0:
            error = payload.get("error", "") if isinstance(payload, dict) else ""
            fail(error or "execution backend could not list target directory")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"ok", "entries", "error"}
        or payload.get("ok") is not True
        or not isinstance(payload.get("entries"), list)
        or not isinstance(payload.get("error"), str)
    ):
        fail("execution backend returned invalid directory listing")
    parsed: list[DirectoryEntry] = []
    for item in cast(list[object], payload["entries"]):
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "mtime"}
            or not isinstance(item.get("name"), str)
            or not item["name"]
            or "/" in item["name"]
            or item["name"] in {".", ".."}
            or not isinstance(item.get("mtime"), int)
            or isinstance(item["mtime"], bool)
        ):
            fail("execution backend returned invalid directory entry")
        parsed.append(DirectoryEntry(name=item["name"], mtime=item["mtime"]))
    return tuple(parsed)


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
    if (
        not isinstance(result, dict)
        or set(result) != {"euid", "egid", "groups"}
        or not isinstance(result.get("euid"), int)
        or isinstance(result.get("euid"), bool)
        or not isinstance(result.get("egid"), int)
        or isinstance(result.get("egid"), bool)
        or not isinstance(result.get("groups"), list)
        or not all(
            isinstance(group, int) and not isinstance(group, bool) for group in result["groups"]
        )
    ):
        return ExecutionProbe(backend=backend.id, ok=False, error="invalid probe result")
    if result != {
        "euid": expected.pw_uid,
        "egid": expected.pw_gid,
        "groups": sorted(os.getgrouplist(user, expected.pw_gid)),
    }:
        return ExecutionProbe(
            backend=backend.id,
            ok=False,
            error=f"execution backend did not enter target user {user!r}",
        )
    return ExecutionProbe(backend=backend.id, ok=True)
