# SPDX-License-Identifier: MIT
"""Pure policy for entering a launch user's host execution context."""

from __future__ import annotations

import hashlib
import json
import string
from dataclasses import dataclass, field
from typing import Literal

from uxon.errors import fail

LOCAL_PROBE_TIMEOUT_SECONDS = 0.5


@dataclass(frozen=True)
class ExecutionBackendSpec:
    id: str
    kind: Literal["local", "command"]
    command_prefix: tuple[str, ...] = ()
    probe_timeout_seconds: float = 5.0

    @property
    def fingerprint(self) -> str:
        payload = {
            "id": self.id,
            "kind": self.kind,
            "command_prefix": self.command_prefix,
            "probe_timeout_seconds": self.probe_timeout_seconds,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class ExecutionConfig:
    default_backend: str = "local"
    state_dir: str = ""
    backend_by_launch_user: dict[str, str] = field(default_factory=dict)
    backends: dict[str, ExecutionBackendSpec] = field(
        default_factory=lambda: {
            "local": ExecutionBackendSpec(
                id="local", kind="local", probe_timeout_seconds=LOCAL_PROBE_TIMEOUT_SECONDS
            )
        }
    )

    def backend_for_user(self, user: str) -> ExecutionBackendSpec:
        backend_id = self.backend_by_launch_user.get(user, self.default_backend)
        try:
            return self.backends[backend_id]
        except KeyError:
            fail(f"execution backend {backend_id!r} selected for {user!r} is not configured")
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class ExecutionTarget:
    user: str
    backend: ExecutionBackendSpec


@dataclass(frozen=True)
class ExecutionIdentity:
    euid: int
    egid: int
    groups: tuple[int, ...]
    namespaces: tuple[tuple[str, int], ...]
    cgroup: str
    capabilities: tuple[tuple[str, str], ...]
    no_new_privs: int
    state_dev: int
    state_ino: int


def _validate_token(token: str, *, source: str) -> None:
    try:
        for _literal, placeholder, format_spec, conversion in string.Formatter().parse(token):
            if placeholder is None:
                continue
            if placeholder != "user":
                fail(f"{source} uses unsupported placeholder {placeholder!r}; valid: {{user}}")
            if format_spec or conversion:
                fail(f"{source} must not use format specs or conversions")
    except ValueError as exc:
        fail(f"{source} is not a valid format template: {exc}")


def validate_execution_config(config: ExecutionConfig) -> ExecutionConfig:
    builtin = ExecutionBackendSpec(
        id="local", kind="local", probe_timeout_seconds=LOCAL_PROBE_TIMEOUT_SECONDS
    )
    if config.backends.get("local") != builtin:
        fail("execution.backends.local is reserved for the built-in local backend")
    if config.default_backend not in config.backends:
        fail(f"execution.default_backend references unknown backend {config.default_backend!r}")
    for user, backend_id in config.backend_by_launch_user.items():
        if not user:
            fail("execution.backend_by_launch_user keys must be non-empty")
        if backend_id not in config.backends:
            fail(
                f"execution.backend_by_launch_user.{user} references unknown backend {backend_id!r}"
            )
    for backend_id, backend in config.backends.items():
        if backend.id != backend_id:
            fail(f"execution backend key {backend_id!r} does not match id {backend.id!r}")
        if backend.kind == "local":
            if backend_id != "local":
                fail("only the built-in execution backend may use kind='local'")
            continue
        if backend.kind != "command":
            fail(f"execution.backends.{backend_id}.kind must be 'command'")
        for key, argv in (("command_prefix", backend.command_prefix),):
            if not argv:
                fail(f"execution.backends.{backend_id}.{key} must not be empty")
            if not any("{user}" in token for token in argv):
                fail(f"execution.backends.{backend_id}.{key} must include {{user}}")
            for token in argv:
                _validate_token(token, source=f"execution.backends.{backend_id}.{key}")
        prefix = backend.command_prefix
        helper_index = 3 if prefix[:3] == ("sudo", "-n", "--") else 0
        if len(prefix) < helper_index + 3 or not prefix[helper_index].startswith("/"):
            fail(
                f"execution.backends.{backend_id}.command_prefix must invoke a fixed absolute "
                "helper, optionally through 'sudo -n --'"
            )
        if prefix[-2:] != ("{user}", "--") or prefix.count("{user}") != 1:
            fail(
                f"execution.backends.{backend_id}.command_prefix must end with "
                "'{user} --' target-user framing"
            )
        if backend.probe_timeout_seconds <= 0:
            fail(f"execution.backends.{backend_id}.probe_timeout_seconds must be greater than 0")
    has_command_backend = any(backend.kind == "command" for backend in config.backends.values())
    if has_command_backend:
        state_dir = config.state_dir
        if not state_dir or not state_dir.startswith("/"):
            fail(
                "execution.state_dir must be an absolute path when command backends are configured"
            )
        if state_dir != state_dir.rstrip("/") or ".." in state_dir.split("/"):
            fail("execution.state_dir must be an absolute normalized path without '..'")
    return config
