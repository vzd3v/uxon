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
from dataclasses import dataclass, field
from typing import Literal

from uxon.errors import fail

OnMissing = Literal["off", "start", "create"]
OnMissingMode = Literal["prompt", "auto"]

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
    ``{user}``, ``{project_slug}``, ``{dir}``. Template placeholders:
    ``{name}`` (resolved container name) and ``{dir}`` (container-side path
    after ``path_map``).
    """

    enabled: bool = False
    name_template: str = ""
    exec_template: tuple[str, ...] = ()
    is_running_cmd: tuple[str, ...] = ()
    exists_cmd: tuple[str, ...] = ()
    start_template: tuple[str, ...] = ()
    create_template: tuple[str, ...] = ()
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
    """One-line kill-path caveat when container launch is enabled, else None.

    ``tmux kill-session`` reaps the client-side ``<runtime> exec`` process,
    but whether the in-container agent dies on disconnect is runtime/version
    dependent (podman in particular may orphan it). So at every surface that
    tells the user a kill happened, append this reminder to confirm with the
    runtime and stop the container if needed.

    Carries ZERO agent/host internals: ``<runtime>`` is the operator's
    ``exec_template`` binary (a config value, e.g. ``docker``/``podman``);
    the container name is operator-chosen too. No usernames, host paths, or
    session names leak here.
    """
    if not cfg.enabled:
        return None
    runtime = cfg.exec_template[0] if cfg.exec_template else "docker"
    return (
        f"if the agent runs in a container, confirm with `{runtime} top <container>` "
        "and stop the container if it is still running"
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
