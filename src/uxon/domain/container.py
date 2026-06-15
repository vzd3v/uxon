# SPDX-License-Identifier: MIT
"""Pure container-launch data + resolution/validation.

uxon can wrap the agent command in an operator-supplied exec prefix so the
agent process runs inside a container (``docker``/``podman``/``nerdctl``/…).
uxon models **no** runtime semantics — every command is an opaque argv
**list** supplied by the operator, never a shell string.

This module is pure (no I/O): the :class:`ContainerConfig` record, the
deterministic name/``{dir}`` resolution, and the safety validators that
keep an attacker-controlled directory name out of the sudo-executed exec
template. The impure probe/start/create shell-outs live in
:mod:`uxon.infra.container`.

Trust split (see ``config_loader.project_allowlist``): the project
``.uxon.toml`` may supply only ``container.name`` and ``container.path_map``
(pure data); every executed/policy key (``exec_template``,
``create_template``, ``on_missing``, …) is operator-only.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Literal

from uxon.errors import fail

OnMissing = Literal["off", "start", "create"]
OnMissingMode = Literal["prompt", "auto"]

# Session-environment variable carrying the resolved container name from
# launch to kill. Set on the tmux session via ``new-session -e`` and read
# back with ``show-environment -t <session>`` when tearing the agent down.
# The name (operator-chosen, not a secret) is the one fact the kill path
# cannot recompute: ``sudo -i`` resets the pane cwd to the agent's home, so
# the launch directory — and thus the {project_slug}-derived name — is not
# reliably recoverable from the live session.
CONTAINER_NAME_ENV = "UXON_CONTAINER"

# Companion session-environment markers stashed alongside ``UXON_CONTAINER`` at
# launch (separate vars — never overloaded into the name, which the kill path
# reads verbatim and validates). They carry the launch-time container identity
# (id), the resolved host-side cgroup path, and the container's start-epoch,
# used by telemetry (cgroup → in-container host PIDs) and the teardown
# PID-recycle guard. Each is set only when resolution yields a non-empty value;
# an absent var is the documented degrade (telemetry/teardown skip the feature).
CONTAINER_ID_ENV = "UXON_CONTAINER_ID"
CONTAINER_CGROUP_ENV = "UXON_CONTAINER_CGROUP"
CONTAINER_EPOCH_ENV = "UXON_CONTAINER_EPOCH"

# Agent-process environment marker exported by the launch wrapper (NOT a tmux
# session-env var): it lands in the in-container agent's ``/proc/self/environ``
# so host-side telemetry can attribute each in-container PID to its session.
SESSION_ENV = "UXON_SESSION"

# Where the launch wrapper drops the agent's in-container PID. ``/tmp`` is
# writable in essentially every base image; the teardown reads it back from
# inside the container (same PID namespace as the recorded pid).
_CONTAINER_PIDFILE_DIR = "/tmp"
# Filename-safe charset for the session-derived pidfile name; anything else
# is collapsed to ``_`` so the path is safe to embed in the operator's
# ``sh -c`` stop_template without quoting surprises.
_PIDFILE_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._@-]")

# Container name charset (mirrors the docker/podman name rule and AC-A6 for
# agent ids): a leading word char, then word/dot/dash. Rejecting a leading
# ``-``/``.``/``_`` is the security-critical part — it closes the slugify gap
# where a hostile directory name keeps a leading ``.``/``_`` and would
# otherwise inject an option-looking token into ``<runtime> exec … <name>``.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# docker/podman cap container names at 128 chars.
_NAME_MAX_LEN = 128

# Placeholders accepted in templates / name_template.
_NAME_PLACEHOLDERS = ("user", "project_slug", "dir")


@dataclass(frozen=True)
class ContainerConfig:
    """Operator container schema (off by default).

    Templates are argv **lists**. ``name_template``/``name`` placeholders:
    ``{user}``, ``{project_slug}``, ``{dir}``. ``exec_template`` placeholders:
    ``{name}`` (resolved container name) and ``{dir}`` (container-side path
    after ``path_map``). ``stop_template`` placeholders: ``{name}`` and
    ``{pidfile}`` (the in-container PID file uxon's launch wrapper records,
    keyed per session) — the mirror of the launch path that lets ``uxon
    kill`` terminate the exact agent process it started, leaving the shared
    container itself untouched.
    """

    enabled: bool = False
    name_template: str = ""
    exec_template: tuple[str, ...] = ()
    is_running_cmd: tuple[str, ...] = ()
    exists_cmd: tuple[str, ...] = ()
    start_template: tuple[str, ...] = ()
    create_template: tuple[str, ...] = ()
    stop_template: tuple[str, ...] = ()
    # Optional operator-supplied opaque argv that prints the container's host
    # ``<id> <init_pid> <start_epoch>`` on stdout, used to anchor telemetry +
    # the teardown PID-recycle guard. Off by default — empty means no shell-out
    # and the identity markers degrade to absent. Placeholders: ``{name}``,
    # ``{dir}``. Operator-only (not in the project allowlist).
    resolve_cmd: tuple[str, ...] = ()
    on_missing: OnMissing = "off"
    on_missing_mode: OnMissingMode = "prompt"
    # Project-layer (``.uxon.toml``) data-only overrides.
    name: str = ""
    path_map: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def _validate_abs_normalized(path: str, what: str) -> str:
    """Require ``path`` to be absolute, normalized, and ``..``-free.

    Returns the normalized path. ``fail`` (a clear message, no traceback)
    otherwise. "Absolute" alone does not reject ``/work/../../etc`` — the
    ``..``-segment check after normalization is the security-critical part,
    because the value flows into the sudo-executed exec template.
    """
    if not isinstance(path, str) or not path:
        fail(f"container {what} must be a non-empty absolute path")
    if not path.startswith("/"):
        fail(f"container {what} must be an absolute path; got: {path!r}")
    # Reject any ``..`` segment in the RAW path, before normalization.
    # ``normpath`` would silently collapse ``/work/../../etc`` to ``/etc`` —
    # a path the operator plainly didn't write — so a residual-only check is
    # insufficient; the value flows into the sudo-executed exec template.
    if ".." in path.split("/"):
        fail(f"container {what} must not contain '..' segments; got: {path!r}")
    return os.path.normpath(path)


def validate_path_map(raw: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Validate + normalize a ``[container.path_map]`` table.

    Keys (host prefixes) and values (container prefixes) must each be
    absolute, normalized, and ``..``-free. Values are **literals** — never
    re-``.format()``ed. Returns longest-host-prefix-first so resolution can
    take the first match.
    """
    pairs: list[tuple[str, str]] = []
    for host_prefix, container_prefix in raw.items():
        host_n = _validate_abs_normalized(host_prefix, "path_map host prefix")
        container_n = _validate_abs_normalized(container_prefix, "path_map container prefix")
        pairs.append((host_n, container_n))
    # Longest host prefix first → first match wins (most specific).
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return tuple(pairs)


def apply_path_map(host_dir: str, path_map: tuple[tuple[str, str], ...]) -> str:
    """Map a host directory to its container-side path (longest prefix).

    No mapping match → host path verbatim (the common bind-at-same-path
    case). The result is validated (absolute, ``..``-free, no leading ``-``)
    because it flows into the exec template's ``-w`` token.
    """
    host_n = os.path.normpath(host_dir)
    mapped = host_n
    for host_prefix, container_prefix in path_map:
        if host_n == host_prefix:
            mapped = container_prefix
            break
        if host_n.startswith(host_prefix.rstrip("/") + "/"):
            suffix = host_n[len(host_prefix.rstrip("/")) :]
            mapped = container_prefix.rstrip("/") + suffix
            break
    mapped_n = _validate_abs_normalized(mapped, "{dir} (after path_map)")
    if mapped_n.startswith("-"):
        # Defensive: an absolute path can't start with ``-``, but keep the
        # invariant explicit alongside the name check.
        fail(f"container {{dir}} must not start with '-'; got: {mapped_n!r}")
    return mapped_n


def path_map_under_prefix(host_dir: str, path_map: tuple[tuple[str, str], ...]) -> bool:
    """True iff ``host_dir`` falls under (or equals) some ``path_map`` host prefix.

    Pure predicate mirroring :func:`apply_path_map`'s matching rule (exact
    match or ``prefix/`` segment match, normalized). Used by the worktree
    launch gate (AC-P4.1): when a non-empty ``path_map`` is configured but the
    computed worktree path is under none of its host prefixes, the container
    would have no mount backing that path — uxon fails fast with a clear
    message instead of deferring to an opaque runtime error.

    An **empty** ``path_map`` is the "host path verbatim" carve-out and is the
    caller's responsibility to skip — this predicate returns ``False`` for it
    (nothing covers the path), so the caller must guard on a non-empty map.
    """
    host_n = os.path.normpath(host_dir)
    for host_prefix, _container_prefix in path_map:
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
        placeholders = ", ".join(f"{{{p}}}" for p in _NAME_PLACEHOLDERS)
        fail(
            f"container {what} uses unsupported placeholder {exc.args[0]!r}; valid: {placeholders}"
        )
    except (IndexError, ValueError) as exc:
        fail(f"container {what} is not a valid format template: {exc}")
    raise AssertionError("unreachable")


def is_valid_container_name(name: str) -> bool:
    """Non-raising form of :func:`validate_container_name`.

    The kill path reads the container name back from the live session
    environment and must not abort the destructive ``kill-session`` on a
    malformed value — it degrades to "skip teardown" instead. This keeps the
    same charset invariant without the ``fail`` (SystemExit) of the launch-
    time validator.
    """
    return bool(name) and len(name) <= _NAME_MAX_LEN and bool(_NAME_RE.match(name))


def container_pidfile(session: str) -> str:
    """Deterministic in-container PID-file path for a tmux ``session``.

    The launch wrapper writes the agent's in-container PID here; the kill
    teardown reads it back. Keyed by the (server-unique) session name — never
    the container or agent — so each of the arbitrarily many sessions sharing
    one container (indexed re-runs, worktrees, different agents) targets
    exactly its own process. The session is sanitized to a filename-safe
    charset so the path is safe to embed in the operator's ``sh -c``
    stop_template.
    """
    safe = _PIDFILE_UNSAFE_RE.sub("_", session)
    return f"{_CONTAINER_PIDFILE_DIR}/uxon-{safe}.pid"


def wrap_agent_for_container(
    agent_argv: list[str], *, session: str, pidfile: str | None
) -> list[str]:
    """Wrap the agent argv for an in-container launch.

    Applied to **every** ``container.enabled`` session (regardless of
    ``stop_template``). The wrapper runs *inside* the container, after the
    operator exec prefix:

    - It **always** exports ``UXON_SESSION=<session>`` so the marker lands in
      the in-container agent's ``/proc/self/environ`` — host-side telemetry
      reads it back to attribute each in-container PID to its session.
    - When a ``pidfile`` is supplied (the operator opted into teardown via
      ``stop_template``), it **also** records the agent's in-container PID with
      ``echo $$ > <pidfile>``: ``$$`` is the shell's PID and ``exec`` replaces
      that shell with the agent, so the agent inherits the same PID and the
      file holds its real in-container PID.

    ``exec "$@"`` keeps the agent argv a list of tokens — never re-parsed by
    the shell — preserving the argv-list injection invariant. Same idiom as
    ``infra.container._as_user_in_dir``. Needs ``sh`` in the image.
    """
    parts = [f"export {SESSION_ENV}={shlex.quote(session)};"]
    if pidfile is not None:
        parts.append(f"echo $$ > {shlex.quote(pidfile)};")
    parts.append('exec "$@"')
    script = " ".join(parts)
    return ["sh", "-c", script, "uxon-agent", *agent_argv]


def render_stop_template(template: tuple[str, ...], *, name: str, pidfile: str) -> list[str]:
    """Render the ``stop_template`` argv with ``{name}``/``{pidfile}`` filled.

    Per-token formatting keeps the argv-list shape (each ``{name}`` is one
    token). ``name`` is the validated resolved name read back from the
    session; ``pidfile`` the deterministic per-session path.
    """
    return [_safe_format(tok, "stop_template", name=name, pidfile=pidfile) for tok in template]


def validate_container_name(name: str) -> str:
    """Validate the RESOLVED container name (after expansion, any source).

    The check runs on the final name — not the raw input — because
    ``{project_slug}`` is attacker-controlled (it derives from a hostile
    directory name via ``slugify``, which strips a leading ``-`` but keeps a
    leading ``.``/``_``). Rejects a leading ``-``/``.``/``_``, unsafe chars,
    and over-128-char names. ``fail`` otherwise; returns the name unchanged.
    """
    if not name:
        fail("container name resolved to empty; set [container] name or name_template")
    if len(name) > _NAME_MAX_LEN:
        fail(f"container name too long ({len(name)} > {_NAME_MAX_LEN}): {name!r}")
    if not _NAME_RE.match(name):
        fail(
            f"unsafe container name {name!r}: must match [A-Za-z0-9][A-Za-z0-9_.-]* "
            "(no leading '-', '.', or '_', and no other special characters)"
        )
    return name


def resolve_container_name(
    cfg: ContainerConfig, *, user: str, project_slug: str, dir_token: str
) -> str:
    """Resolve the container name deterministically and validate it.

    Project ``.uxon.toml [container.name]`` wins over the operator
    ``name_template``. Both run through the same post-expansion validator
    (AC-B8). Same (user, project dir) → same name (never per-session).
    """
    source = cfg.name or cfg.name_template
    if not source:
        fail("container is enabled but neither [container] name nor name_template is set")
    expanded = _safe_format(source, "name", user=user, project_slug=project_slug, dir=dir_token)
    return validate_container_name(expanded)


def render_exec_prefix(exec_template: tuple[str, ...], *, name: str, dir_token: str) -> list[str]:
    """Render the exec-template argv list with ``{name}``/``{dir}`` filled.

    Each token is formatted independently — the argv-list shape is the
    security invariant (``"{name}"`` is one token, defeating shell
    injection). ``{name}`` is the already-validated resolved name and
    ``{dir}`` the already-validated container path.
    """
    return [_safe_format(tok, "exec_template", name=name, dir=dir_token) for tok in exec_template]


def render_template(
    template: tuple[str, ...], *, name: str, dir_token: str, what: str
) -> list[str]:
    """Render a probe / start / create argv list (``{name}``/``{dir}``)."""
    return [_safe_format(tok, what, name=name, dir=dir_token) for tok in template]


Action = Literal["exec", "start", "create", "fail"]


def kill_caveat(cfg: ContainerConfig) -> str | None:
    """One-line kill-path caveat, or None when no reminder is warranted.

    ``tmux kill-session`` reaps only the client-side ``<runtime> exec``
    process; the in-container agent does NOT die on that disconnect under
    docker or podman — it orphans. When a ``stop_template`` is configured,
    uxon's kill path runs it to terminate the recorded in-container PID, so
    the agent is genuinely reaped and no caveat is needed (None). When it is
    NOT configured, the orphan risk stands, so every surface that reports a
    kill appends this reminder.

    Carries ZERO agent/host internals: ``<runtime>`` is the operator's
    ``exec_template`` binary (a config value, e.g. ``docker``/``podman``);
    the container name is operator-chosen too. No usernames, host paths, or
    session names leak here.
    """
    if not cfg.enabled or cfg.stop_template:
        return None
    runtime = cfg.exec_template[0] if cfg.exec_template else "docker"
    return (
        f"the in-container agent is NOT reaped by the kill; confirm with "
        f"`{runtime} top <container>` and stop it, or set [container].stop_template "
        "so uxon terminates it for you"
    )


def decide_container_action(
    *, running: bool, exists: bool, on_missing: OnMissing
) -> tuple[Action, str]:
    """Decide what uxon may do given probe results + the ``on_missing`` gate.

    Returns ``(action, reason)``. ``reason`` is a user-facing,
    internals-free phrase used to build the ``fail``/affordance text.
    Capability (start a stopped / create an absent container) is gated by
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
