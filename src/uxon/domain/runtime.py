# SPDX-License-Identifier: MIT
"""Pure workload-runtime schema, resolution, and safety validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import string
from dataclasses import dataclass, field
from typing import Literal

from uxon.errors import fail

OnMissing = Literal["fail", "start", "create"]
Approval = Literal["prompt", "auto"]
ResourceScope = Literal["global", "per_user"]
RuntimeKind = Literal["direct", "command"]

# Session-environment variable carrying the resolved runtime resource from
# launch to kill. Set on the tmux session via ``new-session -e`` and read
# back with ``show-environment -t <session>`` when tearing the agent down.
# The name (operator-chosen, not a secret) is the one fact the kill path
# cannot recompute: an execution backend may set a different working directory, so
# the launch directory — and thus the {project_slug}-derived resource — is not
# reliably recoverable from the live session.
RUNTIME_RESOURCE_ENV = "UXON_RUNTIME_RESOURCE"

# Companion session-environment markers stashed alongside the resource at
# launch (separate vars — never overloaded into the name, which the kill path
# reads verbatim and validates). They carry the launch-time workload identity
# (id), the resolved host-side cgroup path, and the resource's start epoch,
# used by telemetry (cgroup → workload host PIDs) and the teardown
# PID-recycle guard. Each is set only when resolution yields a non-empty value;
# an absent var is the documented degrade (telemetry/teardown skip the feature).
RUNTIME_ID_ENV = "UXON_RUNTIME_ID"
RUNTIME_CGROUP_ENV = "UXON_RUNTIME_CGROUP"
RUNTIME_EPOCH_ENV = "UXON_RUNTIME_EPOCH"

# Agent-process environment marker exported by the launch wrapper (NOT a tmux
# session-env var): it lands in the workload agent's ``/proc/self/environ``
# so host-side telemetry can attribute each workload PID to its session.
SESSION_ENV = "UXON_SESSION"

# Where the launch wrapper drops the workload agent PID. The teardown reads it
# back within the same runtime resource and PID namespace.
_RUNTIME_PIDFILE_DIR = "/tmp"
# Filename-safe charset for the session-derived pidfile name; anything else
# is collapsed to ``_`` so the path is safe to embed in the operator's
# ``sh -c`` stop_command without quoting surprises.
_PIDFILE_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._@-]")

# Runtime-resource charset: a leading word char, then word/dot/dash. Rejecting a leading
# ``-``/``.``/``_`` is the security-critical part — it closes the slugify gap
# where a hostile directory name keeps a leading ``.``/``_`` and would
# otherwise inject an option-looking token into ``<runtime> exec … <name>``.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# A conservative portable resource-name limit.
_NAME_MAX_LEN = 128

# Placeholders accepted in templates / resource_name_template.
_NAME_PLACEHOLDERS = ("user", "project_slug", "dir")
_BASE_PROFILE_PLACEHOLDERS = frozenset(
    {"user", "launch_profile", "runtime", "agent", "project_slug"}
)
_TEMPLATE_PLACEHOLDERS: dict[str, frozenset[str]] = {
    "resource_name_template": _BASE_PROFILE_PLACEHOLDERS,
    "exec_prefix": _BASE_PROFILE_PLACEHOLDERS | {"resource", "runtime_dir"},
    "ready_command": _BASE_PROFILE_PLACEHOLDERS | {"resource", "runtime_dir"},
    "exists_command": _BASE_PROFILE_PLACEHOLDERS | {"resource", "runtime_dir"},
    "start_command": _BASE_PROFILE_PLACEHOLDERS | {"resource", "runtime_dir"},
    "create_command": _BASE_PROFILE_PLACEHOLDERS | {"resource", "runtime_dir"},
    "identity_command": _BASE_PROFILE_PLACEHOLDERS | {"resource", "runtime_dir"},
    "stop_command": _BASE_PROFILE_PLACEHOLDERS | {"resource", "pidfile"},
}


@dataclass(frozen=True)
class WorkloadRuntimeSpec:
    """Operator-owned reusable workload runtime."""

    id: str
    kind: RuntimeKind = "command"
    resource_scope: ResourceScope = "global"
    resource_name_template: str = ""
    exec_prefix: tuple[str, ...] = ()
    ready_command: tuple[str, ...] = ()
    exists_command: tuple[str, ...] = ()
    start_command: tuple[str, ...] = ()
    create_command: tuple[str, ...] = ()
    stop_command: tuple[str, ...] = ()
    identity_command: tuple[str, ...] = ()
    on_missing: OnMissing = "fail"
    approval: Approval = "prompt"
    telemetry: Literal["none", "cgroup"] = "none"
    probe_timeout_seconds: float = 10.0
    prepare_timeout_seconds: float = 120.0
    stop_timeout_seconds: float = 10.0
    path_map: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def fingerprint(self) -> str:
        payload = {
            "id": self.id,
            "kind": self.kind,
            "resource_scope": self.resource_scope,
            "resource_name_template": self.resource_name_template,
            "exec_prefix": self.exec_prefix,
            "ready_command": self.ready_command,
            "exists_command": self.exists_command,
            "start_command": self.start_command,
            "create_command": self.create_command,
            "stop_command": self.stop_command,
            "identity_command": self.identity_command,
            "on_missing": self.on_missing,
            "approval": self.approval,
            "telemetry": self.telemetry,
            "probe_timeout_seconds": self.probe_timeout_seconds,
            "prepare_timeout_seconds": self.prepare_timeout_seconds,
            "stop_timeout_seconds": self.stop_timeout_seconds,
            "path_map": self.path_map,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _validate_template_placeholders(
    template: str, *, field_name: str, source: str, allowed: frozenset[str]
) -> None:
    formatter = string.Formatter()
    try:
        parsed = formatter.parse(template)
        for _literal, placeholder, format_spec, conversion in parsed:
            if placeholder is None:
                continue
            if not placeholder:
                fail(f"{source}.{field_name} uses an empty placeholder")
            if "." in placeholder or "[" in placeholder or "]" in placeholder:
                fail(f"{source}.{field_name} uses unsupported placeholder {placeholder!r}")
            if placeholder not in allowed:
                valid = ", ".join(f"{{{p}}}" for p in sorted(allowed))
                fail(
                    f"{source}.{field_name} uses unsupported placeholder "
                    f"{placeholder!r}; valid: {valid}"
                )
            if format_spec or conversion:
                fail(f"{source}.{field_name} must not use format specs or conversions")
    except ValueError as exc:
        fail(f"{source}.{field_name} is not a valid format template: {exc}")


def validate_runtime(profile: WorkloadRuntimeSpec) -> WorkloadRuntimeSpec:
    """Validate a parsed ``[runtimes.<id>]`` record."""
    source = f"runtimes.{profile.id}"
    if profile.kind == "direct":
        if profile != WorkloadRuntimeSpec(id="direct", kind="direct"):
            fail("runtimes.direct is built in and cannot be configured")
        return profile
    if profile.kind != "command":
        fail(f"{source}.kind must be 'command'")
    if profile.resource_scope not in ("global", "per_user"):
        fail(f"{source}.resource_scope must be 'global' or 'per_user'")
    if not profile.resource_name_template:
        fail(f"{source}.resource_name_template must be a non-empty string")
    if not profile.exec_prefix:
        fail(f"{source}.exec_prefix must be a non-empty argv list")
    if profile.on_missing not in ("fail", "start", "create"):
        fail(f"{source}.on_missing must be 'fail', 'start', or 'create'")
    if profile.approval not in ("prompt", "auto"):
        fail(f"{source}.approval must be 'prompt' or 'auto'")
    if profile.telemetry not in ("none", "cgroup"):
        fail(f"{source}.telemetry must be 'none' or 'cgroup'")
    if profile.stop_command and not profile.identity_command:
        fail(f"{source}.session.stop_command requires {source}.identity.resolve_command")
    if profile.on_missing in ("start", "create"):
        if not profile.ready_command:
            fail(f"{source}.on_missing = {profile.on_missing!r} requires readiness.ready_command")
        if not profile.exists_command:
            fail(f"{source}.on_missing = {profile.on_missing!r} requires readiness.exists_command")
        if not profile.start_command:
            fail(f"{source}.on_missing = {profile.on_missing!r} requires readiness.start_command")
    if profile.on_missing == "create" and not profile.create_command:
        fail(f"{source}.on_missing = 'create' requires readiness.create_command")
    for field_name in (
        "probe_timeout_seconds",
        "prepare_timeout_seconds",
        "stop_timeout_seconds",
    ):
        if getattr(profile, field_name) <= 0:
            fail(f"{source}.{field_name} must be greater than 0")

    _validate_template_placeholders(
        profile.resource_name_template,
        field_name="resource_name_template",
        source=source,
        allowed=_TEMPLATE_PLACEHOLDERS["resource_name_template"],
    )
    for field_name in (
        "exec_prefix",
        "ready_command",
        "exists_command",
        "start_command",
        "create_command",
        "identity_command",
        "stop_command",
    ):
        for token in getattr(profile, field_name):
            _validate_template_placeholders(
                token,
                field_name=field_name,
                source=source,
                allowed=_TEMPLATE_PLACEHOLDERS[field_name],
            )
    return profile


def _validate_abs_normalized(path: str, what: str) -> str:
    """Require ``path`` to be absolute, normalized, and ``..``-free.

    Returns the normalized path. ``fail`` (a clear message, no traceback)
    otherwise. "Absolute" alone does not reject ``/work/../../etc`` — the
    ``..``-segment check after normalization is the security-critical part,
    because the value flows into the sudo-executed exec template.
    """
    if not isinstance(path, str) or not path:
        fail(f"runtime {what} must be a non-empty absolute path")
    if not path.startswith("/"):
        fail(f"runtime {what} must be an absolute path; got: {path!r}")
    # Reject any ``..`` segment in the RAW path, before normalization.
    # ``normpath`` would silently collapse ``/work/../../etc`` to ``/etc`` —
    # a path the operator plainly didn't write — so a residual-only check is
    # insufficient; the value flows into the sudo-executed exec template.
    if ".." in path.split("/"):
        fail(f"runtime {what} must not contain '..' segments; got: {path!r}")
    return os.path.normpath(path)


def validate_path_map(raw: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Validate and normalize a runtime ``path_map`` table.

    Keys (host prefixes) and values (runtime prefixes) must each be
    absolute, normalized, and ``..``-free. Values are **literals** — never
    re-``.format()``ed. Returns longest-host-prefix-first so resolution can
    take the first match.
    """
    pairs: list[tuple[str, str]] = []
    for host_prefix, runtime_prefix in raw.items():
        host_n = _validate_abs_normalized(host_prefix, "path_map host prefix")
        runtime_n = _validate_abs_normalized(runtime_prefix, "path_map runtime prefix")
        pairs.append((host_n, runtime_n))
    # Longest host prefix first → first match wins (most specific).
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return tuple(pairs)


def apply_path_map(host_dir: str, path_map: tuple[tuple[str, str], ...]) -> str:
    """Map a host directory to its runtime-side path (longest prefix).

    No mapping match → host path verbatim (the common bind-at-same-path
    case). The result is validated (absolute, ``..``-free, no leading ``-``)
    because it flows into the exec template's ``-w`` token.
    """
    host_n = os.path.normpath(host_dir)
    mapped = host_n
    for host_prefix, runtime_prefix in path_map:
        if host_n == host_prefix:
            mapped = runtime_prefix
            break
        if host_n.startswith(host_prefix.rstrip("/") + "/"):
            suffix = host_n[len(host_prefix.rstrip("/")) :]
            mapped = runtime_prefix.rstrip("/") + suffix
            break
    mapped_n = _validate_abs_normalized(mapped, "{runtime_dir} (after path_map)")
    if mapped_n.startswith("-"):
        # Defensive: an absolute path can't start with ``-``, but keep the
        # invariant explicit alongside the name check.
        fail(f"runtime {{runtime_dir}} must not start with '-'; got: {mapped_n!r}")
    return mapped_n


def path_map_under_prefix(host_dir: str, path_map: tuple[tuple[str, str], ...]) -> bool:
    """True iff ``host_dir`` falls under (or equals) some ``path_map`` host prefix.

    Pure predicate mirroring :func:`apply_path_map`'s matching rule (exact
    match or ``prefix/`` segment match, normalized). Used by the worktree
    launch gate (AC-P4.1): when a non-empty ``path_map`` is configured but the
    computed worktree path is under none of its host prefixes, the workload
    would have no mount backing that path — uxon fails fast with a clear
    message instead of deferring to an opaque runtime error.

    An **empty** ``path_map`` is the "host path verbatim" carve-out and is the
    caller's responsibility to skip — this predicate returns ``False`` for it
    (nothing covers the path), so the caller must guard on a non-empty map.
    """
    host_n = os.path.normpath(host_dir)
    for host_prefix, _runtime_prefix in path_map:
        if host_n == host_prefix:
            return True
        if host_n.startswith(host_prefix.rstrip("/") + "/"):
            return True
    return False


def _safe_format(template: str, what: str, **values: str) -> str:
    """``str.format`` with a clear message on a bad/unknown placeholder.

    Mirrors ``tmux_socket_path``'s guard: an operator typo like
    ``{proj}`` fails with a readable message, never a raw ``KeyError``
    traceback.
    """
    try:
        return template.format(**values)
    except KeyError as exc:
        placeholders = ", ".join(f"{{{p}}}" for p in sorted(values))
        fail(f"runtime {what} uses unsupported placeholder {exc.args[0]!r}; valid: {placeholders}")
    except (IndexError, ValueError) as exc:
        fail(f"runtime {what} is not a valid format template: {exc}")
    raise AssertionError("unreachable")


def is_valid_runtime_resource(resource: str) -> bool:
    """Non-raising form of :func:`validate_runtime_resource`.

    The kill path reads the runtime resource back from the live session
    environment and must not abort the destructive ``kill-session`` on a
    malformed value — it degrades to "skip teardown" instead. This keeps the
    same charset invariant without the ``fail`` (SystemExit) of the launch-
    time validator.
    """
    return bool(resource) and len(resource) <= _NAME_MAX_LEN and bool(_NAME_RE.match(resource))


def runtime_pidfile(session: str) -> str:
    """Deterministic runtime-side PID-file path for a tmux ``session``.

    The launch wrapper writes the workload agent PID here; the kill
    teardown reads it back. Keyed by the (server-unique) session name — never
    the runtime resource or agent — so each of the arbitrarily many sessions sharing
    one resource (indexed re-runs, worktrees, different agents) targets
    exactly its own process. The session is sanitized to a filename-safe
    charset so the path is safe to embed in the operator's ``sh -c``
    stop_command.
    """
    safe = _PIDFILE_UNSAFE_RE.sub("_", session)
    return f"{_RUNTIME_PIDFILE_DIR}/uxon-{safe}.pid"


def wrap_agent_for_runtime(
    agent_argv: list[str], *, session: str, pidfile: str | None
) -> list[str]:
    """Wrap the agent argv inside a command runtime.

    Applied to every command-runtime session (regardless of ``stop_command``).
    The wrapper runs inside the workload boundary, after the
    operator exec prefix:

    - It **always** exports ``UXON_SESSION=<session>`` so the marker lands in
      the workload agent's ``/proc/self/environ`` — host-side telemetry
      reads it back to attribute each workload PID to its session.
    - When a ``pidfile`` is supplied (the operator opted into teardown via
      ``stop_command``), it **also** records the workload agent PID with
      ``echo $$ > <pidfile>``: ``$$`` is the shell's PID and ``exec`` replaces
      that shell with the agent, so the agent inherits the same PID and the
      file holds its real runtime-side PID.

    ``exec "$@"`` keeps the agent argv a list of tokens — never re-parsed by
    the shell — preserving the argv-list injection invariant. Same idiom as
    ``infra.runtime._as_user_in_dir``. Requires ``sh`` in the workload environment.
    """
    parts = [f"export {SESSION_ENV}={shlex.quote(session)};"]
    if pidfile is not None:
        parts.append(f"echo $$ > {shlex.quote(pidfile)};")
    parts.append('exec "$@"')
    script = " ".join(parts)
    return ["sh", "-c", script, "uxon-agent", *agent_argv]


def render_stop_command(command: tuple[str, ...], *, resource: str, pidfile: str) -> list[str]:
    """Render the ``stop_command`` argv with ``{resource}``/``{pidfile}`` filled.

    Per-token formatting keeps the argv-list shape (each ``{resource}`` is one
    token). ``resource`` is the validated resolved resource read back from the
    session; ``pidfile`` the deterministic per-session path.
    """
    return [
        _safe_format(tok, "stop_command", resource=resource, pidfile=pidfile) for tok in command
    ]


def validate_runtime_resource(resource: str) -> str:
    """Validate the resolved runtime resource (after expansion, any source).

    The check runs on the final name — not the raw input — because
    ``{project_slug}`` is attacker-controlled (it derives from a hostile
    directory name via ``slugify``, which strips a leading ``-`` but keeps a
    leading ``.``/``_``). Rejects a leading ``-``/``.``/``_``, unsafe chars,
    and over-128-char names. ``fail`` otherwise; returns the name unchanged.
    """
    if not resource:
        fail("runtime resource resolved to empty; set resource_name_template")
    if len(resource) > _NAME_MAX_LEN:
        fail(f"runtime resource too long ({len(resource)} > {_NAME_MAX_LEN}): {resource!r}")
    if not _NAME_RE.match(resource):
        fail(
            f"unsafe runtime resource {resource!r}: must match "
            "[A-Za-z0-9][A-Za-z0-9_.-]* "
            "(no leading '-', '.', or '_', and no other special characters)"
        )
    return resource


def resolve_runtime_resource_name(
    profile: WorkloadRuntimeSpec,
    *,
    user: str,
    launch_profile: str,
    agent: str,
    project_slug: str,
) -> str:
    """Resolve a profile-scoped runtime resource and validate it."""
    expanded = _safe_format(
        profile.resource_name_template,
        "resource_name_template",
        user=user,
        launch_profile=launch_profile,
        runtime=profile.id,
        agent=agent,
        project_slug=project_slug,
    )
    return validate_runtime_resource(expanded)


def render_exec_prefix(
    exec_prefix: tuple[str, ...], *, resource: str, runtime_dir: str
) -> list[str]:
    """Render the exec-template argv list with ``{resource}``/``{runtime_dir}`` filled.

    Each token is formatted independently — the argv-list shape is the
    security invariant (``"{resource}"`` is one token, defeating shell
    injection). ``{resource}`` is the already-validated resource and
    ``{runtime_dir}`` the already-validated runtime path.
    """
    return [
        _safe_format(tok, "exec_prefix", resource=resource, runtime_dir=runtime_dir)
        for tok in exec_prefix
    ]


def render_profile_template(
    template: tuple[str, ...],
    *,
    profile: WorkloadRuntimeSpec,
    what: str,
    resource: str,
    runtime_dir: str = "",
    user: str,
    launch_profile: str,
    agent: str,
    project_slug: str,
    pidfile: str = "",
) -> list[str]:
    """Render a profile runtime argv template with the profile placeholder set."""
    values = {
        "user": user,
        "launch_profile": launch_profile,
        "runtime": profile.id,
        "agent": agent,
        "project_slug": project_slug,
        "resource": resource,
        "runtime_dir": runtime_dir,
        "pidfile": pidfile,
    }
    return [_safe_format(tok, what, **values) for tok in template]


Action = Literal["exec", "start", "create", "fail"]


def decide_runtime_action(
    *, running: bool, exists: bool, on_missing: OnMissing
) -> tuple[Action, str]:
    """Decide what uxon may do given probe results + the ``on_missing`` gate.

    Returns ``(action, reason)``. ``reason`` is a user-facing,
    internals-free phrase used to build the ``fail``/affordance text.
    Capability (start a stopped / create an absent runtime resource) is gated by
    ``on_missing`` (``off`` < ``start`` < ``create``).
    """
    if running:
        return "exec", "running"
    if exists:
        # Stopped — needs a start.
        if on_missing in ("start", "create"):
            return "start", "stopped"
        return "fail", "stopped"
    # Absent — needs a create.
    if on_missing == "create":
        return "create", "absent"
    return "fail", "absent"
