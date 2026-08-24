# SPDX-License-Identifier: MIT
"""Workload-runtime probe, prepare, exec, and stop shell-outs.

The pure schema, name/path resolution, and the policy decision live in
:mod:`uxon.domain.runtime` and the resolution helpers in
:mod:`uxon.infra.tmux`. This module only shells out — the two exit-code
probes (``ready_command`` / ``exists_command``) and the ``start``/``create``
templates — each under its own bounded timeout so a stalled workload runtime
cannot block a uxon launch indefinitely.

Every shell-out runs **as the launch user** — the same identity the agent
execs under at ``_build_tmux_launch_request`` (wrapped in
the configured execution backend). This is required for any per-user runtime
service: probes and lifecycle commands must cross the same identity boundary
as the workload. Prepare commands run in the host project directory.

Under the TUI these calls MUST run off the event loop (``app.run_off_loop``)
per the no-blocking hard rule — the orchestrator here only shells out, so
the caller owns the thread.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass

from uxon.domain.config import Config
from uxon.domain.launch_profiles import ResolvedLaunchProfile, RuntimeIdentity
from uxon.domain.runtime import (
    Action,
    WorkloadRuntimeSpec,
    decide_runtime_action,
    render_profile_template,
)
from uxon.domain.session import slugify
from uxon.errors import fail
from uxon.infra import events
from uxon.infra.execution import command_prefix
from uxon.infra.run import run_query

# Default timeout retained for callers that do not select a runtime profile.
RUNTIME_CMD_TIMEOUT_SEC = 10.0


EMPTY_IDENTITY = RuntimeIdentity(id="")


def parse_runtime_identity_output(stdout: str) -> RuntimeIdentity | None:
    """Parse the stable JSON identity contract.

    The command must emit one object with non-empty ``id`` and ``epoch`` plus
    a positive integer ``host_pid``. Additional keys are ignored.
    """
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    identity = payload.get("id")
    host_pid = payload.get("host_pid")
    epoch = payload.get("epoch")
    if not isinstance(identity, str) or not identity:
        return None
    if isinstance(host_pid, bool) or not isinstance(host_pid, int) or host_pid <= 0:
        return None
    if not isinstance(epoch, str) or not epoch:
        return None
    return RuntimeIdentity(id=identity, host_pid=host_pid, epoch=epoch)


def parse_proc_cgroup(content: str) -> str:
    """Extract the cgroup path from ``/proc/<pid>/cgroup`` content → path or "".

    Each line is ``<hierarchy-id>:<controllers>:<path>``. cgroup v2 uses the
    single ``0::<path>`` line; v1 has one line per controller. Prefer the v2
    line; otherwise fall back to the first line carrying a usable path.
    Returns "" when nothing parses (degrade).
    """
    fallback = ""
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        # ``split(":", 2)`` keeps a path containing ``:`` intact.
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        hid, controllers, path = fields
        if not path:
            continue
        if hid == "0" and controllers == "":
            # cgroup v2 unified hierarchy — the authoritative line.
            return path
        if not fallback:
            fallback = path
    return fallback


@dataclass(frozen=True)
class RuntimePlan:
    """The resolved resource state and required action before exec.

    ``action`` is the policy verdict (``exec`` / ``start`` / ``create`` /
    ``fail``). ``prepare_command`` is the rendered ``start``/``create`` argv to
    run before launch (empty for ``exec``/``fail``). ``message`` is a
    user-facing, internals-free description for the affordance/error.
    """

    resource: str
    action: Action
    reason: str
    prepare_command: tuple[str, ...]
    prepare_timeout_seconds: float
    message: str


def _as_user_in_dir(prefix: list[str], cmd: list[str], host_dir: str | None) -> list[str]:
    """Prepend the as-user ``prefix`` to ``cmd``, optionally cd'd to ``host_dir``.

    An execution prefix may choose a different working directory, including
    the target's ``$HOME``, so a plain ``subprocess(cwd=…)`` would be
    overridden cross-user. Mirror the git helpers' ``sh -c`` wrapping to cd
    into the HOST project dir first (``exec "$@"`` keeps the operator argv a
    list of tokens — never re-parsed by the shell, preserving the argv-list
    security invariant). ``host_dir is None`` (probes) skips the cd.
    """
    if host_dir is None:
        return prefix + cmd
    script = f'cd {shlex.quote(host_dir)} && exec "$@"'
    return prefix + ["sh", "-c", script, "uxon-runtime", *cmd]


def _probe_exit_ok(cfg: Config, cmd: list[str], launch_user: str, timeout_seconds: float) -> bool:
    """Run an exit-code probe as ``launch_user``; True iff it exits 0 in time.

    Runs under the non-interactive (``sudo -n``) prefix so a missing NOPASSWD
    grant fails fast rather than blocking on a hidden prompt (no TTY here). A
    timeout or an unusable runtime binary is a hard ``fail`` (no traceback) —
    we cannot tell ``not running`` from ``daemon wedged``, and silently
    treating a wedged daemon as ``absent`` would mislead the policy.
    """
    full = _as_user_in_dir(command_prefix(cfg, launch_user, interactive=False), cmd, None)
    try:
        cp = run_query(full, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        fail(
            f"workload runtime did not respond within {timeout_seconds:.0f}s "
            f"({cmd[0]}); is the daemon reachable?"
        )
    except OSError:
        # FileNotFoundError (no binary) or PermissionError (present but not
        # executable) — both mean the runtime isn't usable here.
        fail(f"workload runtime command {cmd[0]!r} is unavailable for the launch user")
    return cp.returncode == 0


def _run_prepare(
    cfg: Config,
    cmd: list[str],
    host_dir: str,
    launch_user: str,
    timeout_seconds: float,
) -> None:
    """Run a ``start``/``create`` template as ``launch_user`` in the HOST dir.

    ``create_command`` must run in the host project directory, while the
    rendered ``{runtime_dir}`` remains runtime-side. Failure is a hard ``fail`` carrying the
    runtime's stderr (operator-supplied command output — no uxon internals).
    """
    full = _as_user_in_dir(command_prefix(cfg, launch_user, interactive=True), cmd, host_dir)
    try:
        cp = run_query(full, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        fail(f"workload runtime {cmd[0]} did not complete within {timeout_seconds:.0f}s")
    except OSError:
        fail(f"workload runtime command {cmd[0]!r} is unavailable for the launch user")
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        fail(detail or f"workload runtime command failed: {cmd[0]}")


def _project_slug(target_dir: str) -> str:
    from os.path import basename, normpath

    return slugify(basename(normpath(target_dir)))


def _render_profile_cmd(
    template: tuple[str, ...],
    *,
    profile: WorkloadRuntimeSpec,
    context,
    resolved: ResolvedLaunchProfile,
    target_dir: str,
    what: str,
    pidfile: str = "",
) -> list[str]:
    return render_profile_template(
        template,
        profile=profile,
        what=what,
        resource=context.resource,
        runtime_dir=context.runtime_dir,
        user=resolved.launch_user,
        launch_profile=resolved.profile.id,
        agent=resolved.agent.id,
        project_slug=_project_slug(target_dir),
        pidfile=pidfile,
    )


def run_teardown(
    cfg: Config,
    stop_cmd: list[str],
    launch_user: str,
    timeout_seconds: float,
) -> tuple[bool, str]:
    """Run a rendered ``stop_command`` as ``launch_user`` — best-effort.

    The mirror of the exec wrap: ``uxon kill`` terminates the workload
    agent process uxon started, by the per-session PID the launch wrapper
    recorded. Runs as the launch user (same per-user rootless daemon the
    agent execs under), non-interactive prefix (the kill path has no TTY),
    bounded timeout. Returns ``(ok, detail)`` and NEVER raises — a teardown
    failure must not abort the ``tmux kill-session`` that follows it; the
    caller surfaces ``detail`` as a note and proceeds.

    This terminates the agent process, never the operator-owned runtime resource.
    """
    full = _as_user_in_dir(command_prefix(cfg, launch_user, interactive=False), stop_cmd, None)
    try:
        cp = run_query(full, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return False, f"runtime session stop did not complete within {timeout_seconds:.0f}s"
    except OSError:
        return False, f"workload runtime command {stop_cmd[0]!r} is unavailable"
    if cp.returncode != 0:
        return False, (cp.stderr or cp.stdout or "").strip() or "runtime session stop failed"
    return True, ""


def current_runtime_identity_for_profile(
    cfg: Config,
    profile: WorkloadRuntimeSpec,
    resource: str,
    launch_user: str,
) -> RuntimeIdentity | None:
    """Best-effort live identity for a profile-scoped runtime resource."""
    if not profile.identity_command:
        return None
    try:
        cmd = render_profile_template(
            profile.identity_command,
            profile=profile,
            what="identity_command",
            resource=resource,
            runtime_dir="/",
            user=launch_user,
            launch_profile="",
            agent="",
            project_slug="",
        )
    except SystemExit:
        return None
    full = _as_user_in_dir(command_prefix(cfg, launch_user, interactive=False), cmd, None)
    try:
        cp = run_query(full, timeout=profile.probe_timeout_seconds)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if cp.returncode != 0:
        return None
    identity = parse_runtime_identity_output(cp.stdout)
    if identity is None:
        return None
    if profile.telemetry == "cgroup":
        from uxon.infra.runtime_telemetry import read_process_cgroup

        cgroup = read_process_cgroup(cfg, launch_user, identity.host_pid)
    else:
        cgroup = ""
    return RuntimeIdentity(
        id=identity.id,
        host_pid=identity.host_pid,
        cgroup=cgroup,
        epoch=identity.epoch,
    )


def probe_runtime_state_for_profile(
    cfg: Config,
    profile: WorkloadRuntimeSpec | None,
    resource: str,
    launch_user: str,
    *,
    launch_profile: str = "",
    agent: str = "",
    project_slug: str = "",
) -> tuple[str, str]:
    """Non-raising doctor probe for a profile-scoped runtime resource."""

    def _probe(cmd_template: tuple[str, ...], what: str) -> str:
        if profile is None or not cmd_template:
            return "?"
        try:
            cmd = render_profile_template(
                cmd_template,
                profile=profile,
                what=what,
                resource=resource,
                runtime_dir="/",
                user=launch_user,
                launch_profile=launch_profile,
                agent=agent,
                project_slug=project_slug,
            )
        except SystemExit:
            return "?"
        full = _as_user_in_dir(command_prefix(cfg, launch_user, interactive=False), cmd, None)
        try:
            cp = run_query(full, timeout=profile.probe_timeout_seconds)
        except (subprocess.TimeoutExpired, OSError):
            return "?"
        return "yes" if cp.returncode == 0 else "no"

    running = _probe(profile.ready_command if profile else (), "ready_command")
    exists = _probe(profile.exists_command if profile else (), "exists_command")
    return running, exists


def plan_runtime_launch_for_profile(
    cfg: Config,
    target_dir: str,
    resolved: ResolvedLaunchProfile,
) -> RuntimePlan:
    """Probe the selected workload runtime and decide the prepare action."""
    context = resolved.runtime_context
    if context is None:
        fail("internal: workload runtime plan requested for a direct launch profile")
    profile = cfg.runtimes[context.runtime_id]

    def _render(template: tuple[str, ...], what: str) -> list[str]:
        return _render_profile_cmd(
            template,
            profile=profile,
            context=context,
            resolved=resolved,
            target_dir=target_dir,
            what=what,
        )

    running = (
        _probe_exit_ok(
            cfg,
            _render(profile.ready_command, "ready_command"),
            resolved.launch_user,
            profile.probe_timeout_seconds,
        )
        if profile.ready_command
        else False
    )
    if running:
        exists = True
    elif profile.exists_command:
        exists = _probe_exit_ok(
            cfg,
            _render(profile.exists_command, "exists_command"),
            resolved.launch_user,
            profile.probe_timeout_seconds,
        )
    else:
        exists = False

    action, reason = decide_runtime_action(
        running=running, exists=exists, on_missing=profile.on_missing
    )
    events.debug(
        "runtime",
        reason="probe",
        profile=context.runtime_id,
        resource=context.resource,
        running=running,
        exists=exists,
        action=action,
    )

    prepare: tuple[str, ...] = ()
    if action == "start":
        prepare = tuple(_render(profile.start_command, "start_command"))
        message = f"Runtime resource {context.resource!r} is stopped — start and launch?"
    elif action == "create":
        prepare = tuple(_render(profile.create_command, "create_command"))
        message = f"Runtime resource {context.resource!r} does not exist — create and launch?"
    elif action == "fail":
        if reason == "stopped":
            message = (
                f"Runtime resource {context.resource!r} is stopped and on_missing = "
                f"{profile.on_missing!r} does not permit starting it"
            )
        else:
            message = (
                f"Runtime resource {context.resource!r} does not exist and on_missing = "
                f"{profile.on_missing!r} does not permit creating it"
            )
    else:
        message = f"Runtime resource {context.resource!r} is ready"

    return RuntimePlan(
        resource=context.resource,
        action=action,
        reason=reason,
        prepare_command=prepare,
        prepare_timeout_seconds=profile.prepare_timeout_seconds,
        message=message,
    )


def probe_agent_in_runtime(
    cfg: Config,
    target_dir: str,
    resolved: ResolvedLaunchProfile,
) -> None:
    """Verify the selected agent binary is resolvable inside the workload runtime."""
    context = resolved.runtime_context
    if context is None:
        return
    profile = cfg.runtimes[context.runtime_id]
    exec_prefix = _render_profile_cmd(
        profile.exec_prefix,
        profile=profile,
        context=context,
        resolved=resolved,
        target_dir=target_dir,
        what="exec_prefix",
    )
    cmd = exec_prefix + [
        "sh",
        "-lc",
        'command -v -- "$1" >/dev/null 2>&1',
        "uxon-agent-probe",
        resolved.agent.binary,
    ]
    full = _as_user_in_dir(command_prefix(cfg, resolved.launch_user, interactive=False), cmd, None)
    try:
        cp = run_query(full, timeout=profile.probe_timeout_seconds)
    except subprocess.TimeoutExpired:
        fail(
            f"workload runtime did not respond within {profile.probe_timeout_seconds:.0f}s "
            f"while probing agent {resolved.agent.id!r}"
        )
    except OSError:
        fail(f"workload runtime command {cmd[0]!r} is unavailable for the launch user")
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        fail(
            f"agent {resolved.agent.id!r} for launch profile {resolved.profile.id!r} "
            "is unavailable inside the selected workload runtime"
            + (f": {detail}" if detail else ""),
            1,
        )


def run_prepare(cfg: Config, plan: RuntimePlan, target_dir: str, launch_user: str) -> None:
    """Execute a plan's ``start``/``create`` command (after the policy/prompt).

    No-op for ``exec``. ``fail`` raises with the plan's message. uxon never
    stops or removes the runtime resource itself;
    :func:`run_teardown` (the kill path) only terminates the agent *process*.
    ``target_dir`` is the HOST project dir; the prepare runs there as
    ``launch_user``.
    """
    if plan.action == "exec":
        return
    if plan.action == "fail":
        fail(plan.message)
    # cwd is the HOST project dir (where a compose file lives); the rendered
    # argv already carries the runtime-side ``{runtime_dir}`` token where needed.
    _run_prepare(
        cfg,
        list(plan.prepare_command),
        target_dir,
        launch_user,
        plan.prepare_timeout_seconds,
    )


def resolve_runtime_identity_for_profile(
    cfg: Config,
    target_dir: str,
    resolved: ResolvedLaunchProfile,
) -> RuntimeIdentity:
    """Resolve launch-time identity for the selected workload runtime."""
    context = resolved.runtime_context
    if context is None:
        return EMPTY_IDENTITY
    profile = cfg.runtimes[context.runtime_id]
    required = bool(profile.stop_command)
    if not profile.identity_command:
        if required:
            fail(f"runtime {profile.id!r} requires identity_command before session stop is enabled")
        return EMPTY_IDENTITY
    try:
        cmd = _render_profile_cmd(
            profile.identity_command,
            profile=profile,
            context=context,
            resolved=resolved,
            target_dir=target_dir,
            what="identity_command",
        )
        full = _as_user_in_dir(
            command_prefix(cfg, resolved.launch_user, interactive=False), cmd, None
        )
        cp = run_query(full, timeout=profile.probe_timeout_seconds)
    except subprocess.TimeoutExpired:
        if required:
            fail(f"runtime identity probe timed out for stop-enabled runtime {profile.id!r}")
        return EMPTY_IDENTITY
    except (OSError, SystemExit) as exc:
        if required:
            detail = getattr(exc, "uxon_msg", None) or "identity probe unavailable"
            fail(f"runtime identity probe failed for stop-enabled runtime {profile.id!r}: {detail}")
        return EMPTY_IDENTITY
    if cp.returncode != 0:
        if required:
            fail(f"runtime identity probe failed for stop-enabled runtime {profile.id!r}")
        return EMPTY_IDENTITY
    identity = parse_runtime_identity_output(cp.stdout)
    if identity is None:
        if required:
            fail(f"runtime identity probe returned invalid data for runtime {profile.id!r}")
        return EMPTY_IDENTITY
    if profile.telemetry == "cgroup":
        from uxon.infra.runtime_telemetry import read_process_cgroup

        cgroup = read_process_cgroup(cfg, resolved.launch_user, identity.host_pid)
        if required and not cgroup:
            fail(f"runtime cgroup identity is unavailable for stop-enabled runtime {profile.id!r}")
    else:
        cgroup = ""
    return RuntimeIdentity(
        id=identity.id,
        host_pid=identity.host_pid,
        cgroup=cgroup,
        epoch=identity.epoch,
    )
