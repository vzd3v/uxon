# SPDX-License-Identifier: MIT
"""Container probe / start / create shell-outs (the impure half).

The pure schema, name/path resolution, and the policy decision live in
:mod:`uxon.domain.container` and the resolution helpers in
:mod:`uxon.infra.tmux`. This module only shells out — the two exit-code
probes (``is_running_cmd`` / ``exists_cmd``) and the ``start``/``create``
templates — each under its **own bounded timeout** so a hung
``docker inspect`` against a wedged daemon can never block a uxon launch.

This timeout is deliberately **separate from** the 0.5 s passwordless-sudo
detector (``detect_passwordless_sudo``) — a container daemon stall and a
sudo grant are unrelated, and folding the two would either make the sudo
probe slow or the container probe too tight.

Every shell-out runs **as the launch user** — the same identity the agent
execs under at ``_build_tmux_launch_request`` (wrapped in
``command_prefix_for_user``). This matters for rootless docker/podman,
where the daemon is per-user: probing as the privileged uxon process while
the agent execs in the launch user's daemon would mismatch (probe one
daemon, exec another) and a ``create``/``start`` could hit a rootful
daemon, defeating the rootless sandbox. Probes use the non-interactive
prefix (``sudo -n`` — background work, no TTY); ``start``/``create`` use
the interactive prefix (launch time, login env) and run in the HOST
project directory so a compose file resolves.

Under the TUI these calls MUST run off the event loop (``app.run_off_loop``)
per the no-blocking hard rule — the orchestrator here only shells out, so
the caller owns the thread.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

from uxon.domain.config import Config
from uxon.domain.container import (
    Action,
    ContainerConfig,
    ContainerProfile,
    decide_container_action,
    render_profile_template,
    render_template,
)
from uxon.domain.launch_profiles import ResolvedLaunchProfile
from uxon.domain.session import slugify
from uxon.errors import fail
from uxon.infra import events
from uxon.infra.identity import command_prefix_for_user, nonint_command_prefix_for_user
from uxon.infra.run import run_query

# Bounded timeout (seconds) for every container shell-out. A daemon that
# can't answer ``inspect`` in this window is treated as a hard error rather
# than blocking the launch. Not tunable via config yet (one knob, sane
# default) — separate from the sudo detector's 0.5 s by design.
CONTAINER_CMD_TIMEOUT_SEC = 10.0


@dataclass(frozen=True)
class ContainerIdentity:
    """Launch-time anchor for a container's host-side identity.

    All three fields are empty strings on the degrade path (``resolve_cmd``
    unset, or any piece unresolvable) — telemetry and the teardown
    PID-recycle guard treat empty as "feature unavailable", never an error.

    - ``id`` — the runtime's container id (for the Phase-3 audit event and the
      recycle guard).
    - ``cgroup`` — the exact host-side cgroup path, read from
      ``/proc/<init_pid>/cgroup`` (runtime-layout-agnostic; no hardcoded
      docker/systemd path), used to enumerate the in-container host PIDs.
    - ``epoch`` — the container's start-epoch, the PID-recycle guard anchor.
    """

    id: str = ""
    cgroup: str = ""
    epoch: str = ""


EMPTY_IDENTITY = ContainerIdentity()


def parse_resolve_output(stdout: str) -> tuple[str, str, str] | None:
    """Parse ``resolve_cmd`` stdout → ``(id, init_pid, epoch)`` or None.

    Contract: the first non-empty line is whitespace-separated
    ``<id> <init_pid> <start_epoch>``. Defensive — any shape that does not
    yield three tokens with a numeric ``init_pid`` returns None (degrade),
    never raises.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            return None
        cid, init_pid, epoch = parts[0], parts[1], parts[2]
        if not cid or not init_pid.isdigit() or not epoch:
            return None
        return cid, init_pid, epoch
    return None


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
class ContainerPlan:
    """The resolved container state + what uxon must do before exec.

    ``action`` is the policy verdict (``exec`` / ``start`` / ``create`` /
    ``fail``). ``prepare_cmd`` is the rendered ``start``/``create`` argv to
    run before launch (empty for ``exec``/``fail``). ``message`` is a
    user-facing, internals-free description for the affordance/error.
    """

    name: str
    action: Action
    reason: str
    prepare_cmd: tuple[str, ...]
    message: str


def _as_user_in_dir(prefix: list[str], cmd: list[str], host_dir: str | None) -> list[str]:
    """Prepend the as-user ``prefix`` to ``cmd``, optionally cd'd to ``host_dir``.

    The launch user's prefix is ``sudo -iu`` (login shell), which resets cwd
    to the target's ``$HOME`` — so a plain ``subprocess(cwd=…)`` would be
    overridden cross-user. Mirror the git helpers' ``sh -c`` wrapping to cd
    into the HOST project dir first (``exec "$@"`` keeps the operator argv a
    list of tokens — never re-parsed by the shell, preserving the argv-list
    security invariant). ``host_dir is None`` (probes) skips the cd.
    """
    if host_dir is None:
        return prefix + cmd
    script = f'cd {shlex.quote(host_dir)} && exec "$@"'
    return prefix + ["sh", "-c", script, "uxon-container", *cmd]


def _probe_exit_ok(cmd: list[str], launch_user: str) -> bool:
    """Run an exit-code probe as ``launch_user``; True iff it exits 0 in time.

    Runs under the non-interactive (``sudo -n``) prefix so a missing NOPASSWD
    grant fails fast rather than blocking on a hidden prompt (no TTY here). A
    timeout or an unusable runtime binary is a hard ``fail`` (no traceback) —
    we cannot tell ``not running`` from ``daemon wedged``, and silently
    treating a wedged daemon as ``absent`` would mislead the policy.
    """
    full = _as_user_in_dir(nonint_command_prefix_for_user(launch_user), cmd, None)
    try:
        cp = run_query(full, timeout=CONTAINER_CMD_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        fail(
            f"container runtime did not respond within {CONTAINER_CMD_TIMEOUT_SEC:.0f}s "
            f"({cmd[0]}); is the daemon reachable?"
        )
    except OSError:
        # FileNotFoundError (no binary) or PermissionError (present but not
        # executable) — both mean the runtime isn't usable here.
        fail(f"container runtime {cmd[0]!r} not found or not executable for the launch user")
    return cp.returncode == 0


def _run_prepare(cmd: list[str], host_dir: str, launch_user: str) -> None:
    """Run a ``start``/``create`` template as ``launch_user`` in the HOST dir.

    ``create_template`` (e.g. ``docker compose up -d``) must run in the HOST
    project directory — that is where the compose file lives; the container
    path from ``path_map`` does not exist on the host. The ``{dir}`` token
    *inside* the rendered argv stays container-side; only the process cwd is
    the host dir. Uses the interactive (``sudo -i``) prefix so the runtime
    sees the launch user's login env. Failure is a hard ``fail`` carrying the
    runtime's stderr (operator-supplied command output — no uxon internals).
    """
    full = _as_user_in_dir(command_prefix_for_user(launch_user), cmd, host_dir)
    try:
        cp = run_query(full, timeout=CONTAINER_CMD_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        fail(f"container {cmd[0]} did not complete within {CONTAINER_CMD_TIMEOUT_SEC:.0f}s")
    except OSError:
        fail(f"container runtime {cmd[0]!r} not found or not executable for the launch user")
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        fail(detail or f"container command failed: {cmd[0]}")


def _project_slug(target_dir: str) -> str:
    from os.path import basename, normpath

    return slugify(basename(normpath(target_dir)))


def _render_profile_cmd(
    template: tuple[str, ...],
    *,
    profile: ContainerProfile,
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
        name=context.name,
        dir_token=context.dir_token,
        user=resolved.launch_user,
        launch_profile=resolved.profile.id,
        agent=resolved.agent.id,
        project_slug=_project_slug(target_dir),
        pidfile=pidfile,
    )


def run_teardown(stop_cmd: list[str], launch_user: str) -> tuple[bool, str]:
    """Run a rendered ``stop_template`` as ``launch_user`` — best-effort.

    The mirror of the exec wrap: ``uxon kill`` terminates the in-container
    agent process uxon started, by the per-session PID the launch wrapper
    recorded. Runs as the launch user (same per-user rootless daemon the
    agent execs under), non-interactive prefix (the kill path has no TTY),
    bounded timeout. Returns ``(ok, detail)`` and NEVER raises — a teardown
    failure must not abort the ``tmux kill-session`` that follows it; the
    caller surfaces ``detail`` as a note and proceeds.

    This terminates the agent *process*, never the container: uxon still does
    not stop or remove a container (that is the operator's resource); it only
    cleans up the process it launched.
    """
    full = _as_user_in_dir(nonint_command_prefix_for_user(launch_user), stop_cmd, None)
    try:
        cp = run_query(full, timeout=CONTAINER_CMD_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return False, f"container teardown did not complete within {CONTAINER_CMD_TIMEOUT_SEC:.0f}s"
    except OSError:
        return False, f"container runtime {stop_cmd[0]!r} not found or not executable"
    if cp.returncode != 0:
        return False, (cp.stderr or cp.stdout or "").strip() or "container teardown failed"
    return True, ""


def probe_container_running(
    container_cfg: ContainerConfig | None, name: str, launch_user: str
) -> bool:
    """Non-raising liveness probe: True iff the container ``name`` is running.

    The telemetry "container down" path (AC-P1.8) needs to confirm a stopped
    container WITHOUT the hard ``fail`` of :func:`_probe_exit_ok` — a refresh
    must never abort. So this renders the operator's ``is_running_cmd`` for
    ``name`` and runs it under the bounded non-interactive prefix, returning a
    plain bool: exit 0 → running; any non-zero, timeout, OSError, or render
    error → **True** (conservative: do not assert "down" on a probe we could
    not cleanly run — only a clean non-zero exit means stopped).

    ``container_cfg`` is a :class:`uxon.domain.container.ContainerConfig`. Caller
    bounds this to once per distinct container per refresh (AC-P1.5) and only
    when the cgroup is empty.
    """
    if container_cfg is None or not container_cfg.is_running_cmd:
        return True
    try:
        cmd = render_template(
            container_cfg.is_running_cmd, name=name, dir_token="/", what="is_running_cmd"
        )
    except SystemExit:
        # ``render_template`` ``fail``s on a bad placeholder — never abort a
        # telemetry refresh over it; treat as "cannot confirm down".
        return True
    full = _as_user_in_dir(nonint_command_prefix_for_user(launch_user), cmd, None)
    try:
        cp = run_query(full, timeout=CONTAINER_CMD_TIMEOUT_SEC)
    except (subprocess.TimeoutExpired, OSError):
        return True
    return cp.returncode == 0


def current_container_epoch(cfg: Config, name: str, launch_user: str) -> str | None:
    """Re-resolve the running container ``name``'s start-epoch host-side → epoch or None.

    The teardown PID-recycle guard (AC-P3.5) compares this live epoch against
    the one stashed at launch (``UXON_CONTAINER_EPOCH``): a different epoch
    means the container restarted, so the recorded in-container PID now names
    an unrelated process and the kill must be skipped.

    Renders the operator's ``resolve_cmd`` against the already-known ``name``
    (no ``target_dir`` needed — the kill path reads the name from the session
    env). Returns None when ``resolve_cmd`` is unset, the container is
    gone/unresolvable, or anything fails — the caller treats None as "cannot
    confirm a restart" and proceeds with the kill (degrade, never block).
    """
    c = cfg.container
    if not c.resolve_cmd:
        return None
    try:
        cmd = render_template(c.resolve_cmd, name=name, dir_token="/", what="resolve_cmd")
    except SystemExit:
        return None
    full = _as_user_in_dir(nonint_command_prefix_for_user(launch_user), cmd, None)
    try:
        cp = run_query(full, timeout=CONTAINER_CMD_TIMEOUT_SEC)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if cp.returncode != 0:
        return None
    parsed = parse_resolve_output(cp.stdout)
    if parsed is None:
        return None
    _cid, _init_pid, epoch = parsed
    return epoch


def current_container_epoch_for_profile(
    profile: ContainerProfile,
    name: str,
    launch_user: str,
) -> str | None:
    """Best-effort live start epoch for a profile-scoped container."""
    if not profile.resolve_cmd:
        return None
    try:
        cmd = render_profile_template(
            profile.resolve_cmd,
            profile=profile,
            what="resolve_cmd",
            name=name,
            dir_token="/",
            user=launch_user,
            launch_profile="",
            agent="",
            project_slug="",
        )
    except SystemExit:
        return None
    full = _as_user_in_dir(nonint_command_prefix_for_user(launch_user), cmd, None)
    try:
        cp = run_query(full, timeout=CONTAINER_CMD_TIMEOUT_SEC)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if cp.returncode != 0:
        return None
    parsed = parse_resolve_output(cp.stdout)
    if parsed is None:
        return None
    _cid, _init_pid, epoch = parsed
    return epoch


def probe_container_state(
    container_cfg: ContainerConfig | None, name: str, launch_user: str
) -> tuple[str, str]:
    """Non-raising doctor probe → ``(running, exists)`` as human labels.

    Runs the operator's ``is_running_cmd`` / ``exists_cmd`` for ``name`` under
    the bounded non-interactive prefix and maps the outcome to a label:
    ``"yes"`` / ``"no"`` on a clean exit, ``"?"`` when the probe could not be
    run cleanly (timeout, missing binary, render error, or the template is
    unset). Never raises — ``uxon doctor`` must not abort on a wedged daemon.
    Bounded to one call each (doctor probes a single representative target).
    """

    def _probe(cmd_template: tuple[str, ...], what: str) -> str:
        if container_cfg is None or not cmd_template:
            return "?"
        try:
            cmd = render_template(cmd_template, name=name, dir_token="/", what=what)
        except SystemExit:
            return "?"
        full = _as_user_in_dir(nonint_command_prefix_for_user(launch_user), cmd, None)
        try:
            cp = run_query(full, timeout=CONTAINER_CMD_TIMEOUT_SEC)
        except (subprocess.TimeoutExpired, OSError):
            return "?"
        return "yes" if cp.returncode == 0 else "no"

    running = _probe(container_cfg.is_running_cmd if container_cfg else (), "is_running_cmd")
    exists = _probe(container_cfg.exists_cmd if container_cfg else (), "exists_cmd")
    return running, exists


def probe_container_state_for_profile(
    profile: ContainerProfile | None,
    name: str,
    launch_user: str,
) -> tuple[str, str]:
    """Non-raising doctor probe for a profile-scoped container."""

    def _probe(cmd_template: tuple[str, ...], what: str) -> str:
        if profile is None or not cmd_template:
            return "?"
        try:
            cmd = render_profile_template(
                cmd_template,
                profile=profile,
                what=what,
                name=name,
                dir_token="/",
                user=launch_user,
                launch_profile="",
                agent="",
                project_slug="",
            )
        except SystemExit:
            return "?"
        full = _as_user_in_dir(nonint_command_prefix_for_user(launch_user), cmd, None)
        try:
            cp = run_query(full, timeout=CONTAINER_CMD_TIMEOUT_SEC)
        except (subprocess.TimeoutExpired, OSError):
            return "?"
        return "yes" if cp.returncode == 0 else "no"

    running = _probe(profile.is_running_cmd if profile else (), "is_running_cmd")
    exists = _probe(profile.exists_cmd if profile else (), "exists_cmd")
    return running, exists


def plan_container_launch(cfg: Config, target_dir: str, launch_user: str) -> ContainerPlan:
    """Probe the container and decide the not-ready action (no side effects).

    Resolves + validates the name ONCE (reused for probe + start/create +
    exec, so it can't drift mid-sequence — the probe is advisory, never
    atomic with exec). Runs ``is_running_cmd`` then, if not running,
    ``exists_cmd``; feeds the results to the pure ``decide_container_action``
    gated by ``on_missing``. Returns the verdict; the caller runs
    ``prepare_cmd`` (auto/prompt-confirmed) before launch.
    """
    from uxon.infra.tmux import resolve_container

    name, dir_token = resolve_container(cfg, target_dir, launch_user)
    c = cfg.container

    def _render(template: tuple[str, ...], what: str) -> list[str]:
        return render_template(template, name=name, dir_token=dir_token, what=what)

    running = (
        _probe_exit_ok(_render(c.is_running_cmd, "is_running_cmd"), launch_user)
        if c.is_running_cmd
        else False
    )
    if running:
        exists = True
    elif c.exists_cmd:
        exists = _probe_exit_ok(_render(c.exists_cmd, "exists_cmd"), launch_user)
    else:
        exists = False

    action, reason = decide_container_action(
        running=running, exists=exists, on_missing=c.on_missing
    )
    events.debug(
        "container", reason="probe", name=name, running=running, exists=exists, action=action
    )

    prepare: tuple[str, ...] = ()
    if action == "start":
        prepare = tuple(_render(c.start_template, "start_template"))
        message = f"Container {name!r} is stopped — start and launch?"
    elif action == "create":
        prepare = tuple(_render(c.create_template, "create_template"))
        message = f"Container {name!r} does not exist — create and launch?"
    elif action == "fail":
        if reason == "stopped":
            message = (
                f"Container {name!r} is stopped and on_missing = {c.on_missing!r} "
                "does not permit starting it"
            )
        else:
            message = (
                f"Container {name!r} does not exist and on_missing = {c.on_missing!r} "
                "does not permit creating it"
            )
    else:  # exec
        message = f"Container {name!r} is running"

    return ContainerPlan(
        name=name, action=action, reason=reason, prepare_cmd=prepare, message=message
    )


def plan_container_launch_for_profile(
    cfg: Config,
    target_dir: str,
    resolved: ResolvedLaunchProfile,
) -> ContainerPlan:
    """Probe the selected container profile and decide the prepare action."""
    context = resolved.container_context
    if context is None:
        fail("internal: container plan requested for a host-only launch profile")
    profile = cfg.container_profiles[context.profile_id]

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
        _probe_exit_ok(_render(profile.is_running_cmd, "is_running_cmd"), resolved.launch_user)
        if profile.is_running_cmd
        else False
    )
    if running:
        exists = True
    elif profile.exists_cmd:
        exists = _probe_exit_ok(_render(profile.exists_cmd, "exists_cmd"), resolved.launch_user)
    else:
        exists = False

    action, reason = decide_container_action(
        running=running, exists=exists, on_missing=profile.on_missing
    )
    events.debug(
        "container",
        reason="probe",
        profile=context.profile_id,
        name=context.name,
        running=running,
        exists=exists,
        action=action,
    )

    prepare: tuple[str, ...] = ()
    if action == "start":
        prepare = tuple(_render(profile.start_template, "start_template"))
        message = f"Container {context.name!r} is stopped — start and launch?"
    elif action == "create":
        prepare = tuple(_render(profile.create_template, "create_template"))
        message = f"Container {context.name!r} does not exist — create and launch?"
    elif action == "fail":
        if reason == "stopped":
            message = (
                f"Container {context.name!r} is stopped and on_missing = "
                f"{profile.on_missing!r} does not permit starting it"
            )
        else:
            message = (
                f"Container {context.name!r} does not exist and on_missing = "
                f"{profile.on_missing!r} does not permit creating it"
            )
    else:
        message = f"Container {context.name!r} is running"

    return ContainerPlan(
        name=context.name,
        action=action,
        reason=reason,
        prepare_cmd=prepare,
        message=message,
    )


def probe_agent_in_container(
    cfg: Config,
    target_dir: str,
    resolved: ResolvedLaunchProfile,
) -> None:
    """Verify the selected agent binary is resolvable inside the container."""
    context = resolved.container_context
    if context is None:
        return
    profile = cfg.container_profiles[context.profile_id]
    exec_prefix = _render_profile_cmd(
        profile.exec_template,
        profile=profile,
        context=context,
        resolved=resolved,
        target_dir=target_dir,
        what="exec_template",
    )
    cmd = exec_prefix + [
        "sh",
        "-lc",
        'command -v -- "$1" >/dev/null 2>&1',
        "uxon-agent-probe",
        resolved.agent.binary,
    ]
    full = _as_user_in_dir(nonint_command_prefix_for_user(resolved.launch_user), cmd, None)
    try:
        cp = run_query(full, timeout=CONTAINER_CMD_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        fail(
            f"container runtime did not respond within {CONTAINER_CMD_TIMEOUT_SEC:.0f}s "
            f"while probing agent {resolved.agent.id!r}"
        )
    except OSError:
        fail(f"container runtime {cmd[0]!r} not found or not executable for the launch user")
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        fail(
            f"agent {resolved.agent.id!r} for launch profile {resolved.profile.id!r} "
            "is not installed inside the selected container" + (f": {detail}" if detail else ""),
            1,
        )


def run_prepare(plan: ContainerPlan, target_dir: str, launch_user: str) -> None:
    """Execute a plan's ``start``/``create`` command (after the policy/prompt).

    No-op for ``exec``. ``fail`` raises with the plan's message. uxon never
    stops or removes the container itself — there is no stop/rm path here;
    :func:`run_teardown` (the kill path) only terminates the agent *process*.
    ``target_dir`` is the HOST project dir; the prepare runs there as
    ``launch_user``.
    """
    if plan.action == "exec":
        return
    if plan.action == "fail":
        fail(plan.message)
    # cwd is the HOST project dir (where a compose file lives); the rendered
    # argv already carries the container-side ``{dir}`` token where needed.
    _run_prepare(list(plan.prepare_cmd), target_dir, launch_user)


def _read_proc_cgroup(init_pid: str) -> str:
    """Read host-side ``/proc/<init_pid>/cgroup`` and parse out the path → "".

    The init-PID's cgroup is the container's exact cgroup (kernel's view, no
    runtime-layout assumptions). The file is world-readable; uxon reads it
    directly (it is already privileged). Any read/parse failure → "" (degrade).
    """
    from pathlib import Path

    try:
        content = Path(f"/proc/{init_pid}/cgroup").read_text()
    except OSError:
        return ""
    return parse_proc_cgroup(content)


def resolve_container_identity(cfg: Config, target_dir: str, launch_user: str) -> ContainerIdentity:
    """Resolve a container's launch-time identity (id + cgroup + start-epoch).

    Mechanism locked by ``docs/decisions/2026-06-15-container-identity-resolution.md``:
    an optional operator-supplied opaque ``resolve_cmd`` prints the container's
    host ``<id> <init_pid> <start_epoch>``; uxon reads the exact cgroup path
    from the init-PID's world-readable ``/proc/<init_pid>/cgroup`` (no hardcoded
    runtime layout). Used by Phase-2 telemetry and the Phase-3 teardown guard.

    ``resolve_cmd`` unset → :data:`EMPTY_IDENTITY` with **no** shell-out (keeps
    unit tests hermetic). Any runtime/parse failure also degrades to
    :data:`EMPTY_IDENTITY` — this never raises and never blocks the launch
    (AC-P1.3 / AC-P3.5). The container is already ensured-running before the
    launch request is built, so ``resolve_cmd`` has a live container to inspect.
    """
    from uxon.infra.tmux import resolve_container

    c = cfg.container
    if not c.resolve_cmd:
        return EMPTY_IDENTITY
    try:
        # Name resolution and template render both fail() (SystemExit) on a
        # misconfigured name/resolve template; degrade rather than abort the
        # launch — identity is telemetry-only and opt-in (matches the sibling
        # resolvers current_container_epoch / probe_container_state).
        name, dir_token = resolve_container(cfg, target_dir, launch_user)
        cmd = render_template(c.resolve_cmd, name=name, dir_token=dir_token, what="resolve_cmd")
        full = _as_user_in_dir(nonint_command_prefix_for_user(launch_user), cmd, None)
        cp = run_query(full, timeout=CONTAINER_CMD_TIMEOUT_SEC)
    except (subprocess.TimeoutExpired, OSError, SystemExit):
        return EMPTY_IDENTITY
    if cp.returncode != 0:
        return EMPTY_IDENTITY
    parsed = parse_resolve_output(cp.stdout)
    if parsed is None:
        return EMPTY_IDENTITY
    cid, init_pid, epoch = parsed
    cgroup = _read_proc_cgroup(init_pid)
    return ContainerIdentity(id=cid, cgroup=cgroup, epoch=epoch)


def resolve_container_identity_for_profile(
    cfg: Config,
    target_dir: str,
    resolved: ResolvedLaunchProfile,
) -> ContainerIdentity:
    """Resolve launch-time identity for the selected container profile."""
    context = resolved.container_context
    if context is None:
        return EMPTY_IDENTITY
    profile = cfg.container_profiles[context.profile_id]
    if not profile.resolve_cmd:
        return EMPTY_IDENTITY
    try:
        cmd = _render_profile_cmd(
            profile.resolve_cmd,
            profile=profile,
            context=context,
            resolved=resolved,
            target_dir=target_dir,
            what="resolve_cmd",
        )
        full = _as_user_in_dir(nonint_command_prefix_for_user(resolved.launch_user), cmd, None)
        cp = run_query(full, timeout=CONTAINER_CMD_TIMEOUT_SEC)
    except (subprocess.TimeoutExpired, OSError, SystemExit):
        return EMPTY_IDENTITY
    if cp.returncode != 0:
        return EMPTY_IDENTITY
    parsed = parse_resolve_output(cp.stdout)
    if parsed is None:
        return EMPTY_IDENTITY
    cid, init_pid, epoch = parsed
    cgroup = _read_proc_cgroup(init_pid)
    return ContainerIdentity(id=cid, cgroup=cgroup, epoch=epoch)
