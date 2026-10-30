# SPDX-License-Identifier: MIT
"""tmux adapter: socket-path resolution, nesting detection, the managed-
options ``set`` chain, and the agent + tmux launch/attach command builders.

``_build_tmux_launch_request`` is the single place where an agent command
line is assembled — do not add any other
agent-exec call site. It reads the agent's spec from the merged catalog
``cfg.agents`` (built from :data:`uxon.domain.agents.DEFAULT_AGENT_CATALOG`
⊕ operator config in ``load_config``).
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.launch_profiles import ResolvedLaunchProfile
from uxon.domain.launch_request import LaunchRequest, ManagedTmuxLaunch, TmuxHandoff
from uxon.domain.runtime import (
    RUNTIME_CGROUP_ENV,
    RUNTIME_EPOCH_ENV,
    RUNTIME_ID_ENV,
    RUNTIME_RESOURCE_ENV,
    render_profile_template,
    runtime_pidfile,
    wrap_agent_for_runtime,
)
from uxon.domain.session import SessionIdentity, SessionInfo, slugify
from uxon.errors import fail
from uxon.infra import launch_records, process
from uxon.infra.execution import command_prefix, wrap_command
from uxon.infra.run import run_query
from uxon.infra.tmux_diagnostics import diagnostics_query, diagnostics_script

_LAUNCH_HANDSHAKE_TIMEOUT_SECONDS = 60.0
_TMUX_CONTROL_TIMEOUT_SECONDS = 10.0
_IDENTITY_REFUSED = "selected session identity changed; operation refused"


@dataclass(frozen=True)
class TmuxServerProbe:
    state: Literal["absent", "running", "unreachable"]
    sessions: tuple[str, ...] = ()
    error: str = ""


def probe_tmux_server(cfg: Config, target_user: str, socket_path: str | None) -> TmuxServerProbe:
    """Return the strict result of the fixed probe inside the execution backend."""
    backend = cfg.execution.backend_for_user(target_user)
    socket_args = ["--socket", socket_path] if socket_path is not None else ["--default-socket"]
    cmd = wrap_command(
        cfg,
        target_user,
        [sys.executable, "-m", "uxon.infra.tmux_server_probe", *socket_args],
        interactive=False,
    )
    try:
        result = run_query(cmd, timeout=backend.probe_timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return TmuxServerProbe("unreachable", error=str(exc))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"probe exited {result.returncode}").strip()
        return TmuxServerProbe("unreachable", error=detail)
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        return TmuxServerProbe("unreachable", error=f"invalid tmux probe JSON: {exc}")
    if not isinstance(payload, dict) or set(payload) != {"state", "sessions", "error"}:
        return TmuxServerProbe("unreachable", error="invalid tmux probe result")
    state = payload.get("state")
    sessions = payload.get("sessions")
    error = payload.get("error")
    if (
        state not in {"absent", "running", "unreachable"}
        or not isinstance(sessions, list)
        or not all(isinstance(value, str) and value for value in sessions)
        or not isinstance(error, str)
        or (state == "running" and error)
        or (state == "absent" and (sessions or error))
        or (state == "unreachable" and (sessions or not error))
    ):
        return TmuxServerProbe("unreachable", error="invalid tmux probe result")
    return TmuxServerProbe(state, tuple(sessions), error)  # type: ignore[arg-type]


def require_tmux_server(cfg: Config, target_user: str, socket_path: str) -> None:
    result = probe_tmux_server(cfg, target_user, socket_path)
    if result.state == "running":
        return
    if result.state == "absent":
        fail(f"tmux server for {target_user!r} is absent at {socket_path}")
    fail(f"tmux server for {target_user!r} is unreachable at {socket_path}: {result.error}")


def exact_session_exists(cfg: Config, user: str, session_id: str) -> bool:
    """Prove absence without name-prefix or launch-record discovery filters."""
    result = run_query(
        configured_tmux_base(cfg, user, nonint=True) + ["list-sessions", "-F", "#{session_id}"],
        timeout=cfg.execution.backend_for_user(user).probe_timeout_seconds,
    )
    if result.returncode == 0:
        return session_id in result.stdout.splitlines()
    server = probe_tmux_server(cfg, user, tmux_socket_path(cfg, user))
    if server.state == "absent":
        return False
    detail = (result.stderr or result.stdout or server.error).strip()
    fail(f"unable to verify terminal removal: {detail or 'tmux query failed'}")
    raise AssertionError("unreachable")


def validate_session_context(
    cfg: Config, expected: SessionIdentity, *, require_nonce: bool = True
) -> None:
    expected.validate(require_nonce=require_nonce)
    backend = cfg.execution.backend_for_user(expected.user)
    if (
        expected.socket_path != tmux_socket_path(cfg, expected.user)
        or expected.execution_backend != backend.id
        or expected.execution_fingerprint != backend.fingerprint
    ):
        fail(f"session {expected.name!r} ({expected.session_id}) has a stale execution context", 1)


def exact_identity_exists(
    cfg: Config, expected: SessionIdentity, *, require_nonce: bool = True
) -> bool:
    """Prove full identity absence; a refused/unreachable query is unknown."""
    validate_session_context(cfg, expected, require_nonce=require_nonce)
    result = run_query(
        configured_tmux_base(cfg, expected.user, nonint=True)
        + ["list-sessions", "-F", "#{session_id}\t#{session_created}\t#{E:UXON_LAUNCH_NONCE}"],
        timeout=cfg.execution.backend_for_user(expected.user).probe_timeout_seconds,
    )
    if result.returncode == 0:
        rows = result.stdout.splitlines()
        if any(len(row.split("\t")) != 3 for row in rows):
            fail("unable to prove terminal removal: malformed identity query", 1)
        for row in rows:
            session_id, created, nonce = row.split("\t")
            if (
                session_id == expected.session_id
                and created == expected.created
                and nonce != expected.launch_nonce
            ):
                fail(
                    "session launch marker changed while the tmux identity still exists; removal is unproven",
                    1,
                )
        return f"{expected.session_id}\t{expected.created}\t{expected.launch_nonce}" in rows
    server = probe_tmux_server(cfg, expected.user, expected.socket_path)
    if server.state == "absent":
        return False
    detail = (result.stderr or result.stdout or server.error).strip()
    fail(
        f"unable to prove removal of {expected.name!r} ({expected.session_id}): {detail or 'tmux query failed'}",
        1,
    )
    raise AssertionError("unreachable")


def conditional_kill_argv(cfg: Config, expected: SessionIdentity) -> list[str]:
    """Check and mutate the same target on tmux's synchronous native queue."""
    validate_session_context(cfg, expected)
    return configured_tmux_base(cfg, expected.user, nonint=True) + guarded_session_commands(
        expected,
        f"display-message -p uxon-kill-matched ; kill-session -t {expected.session_id}",
    )


def guarded_session_commands(
    expected: SessionIdentity | launch_records.TmuxSessionMetadata, commands: str
) -> list[str]:
    """Assert captured identity and execute native commands on the same queue."""
    return [
        "if-shell",
        "-F",
        "-t",
        expected.session_id,
        session_identity_predicate(expected),
        commands,
        f"display-message -p {shlex.quote('uxon: ' + _IDENTITY_REFUSED)} ; run-shell 'exit 1'",
    ]


def _command_text(chain: list[str]) -> str:
    """Quote the fixed native command chains used for titles and pane selection."""
    commands: list[str] = []
    start = 0
    for index, value in enumerate(chain):
        if value == ";":
            commands.append(shlex.join(chain[start:index]))
            start = index + 1
    if start < len(chain):
        commands.append(shlex.join(chain[start:]))
    return " ; ".join(commands)


def _control_error(result: subprocess.CompletedProcess[str]) -> str:
    if _IDENTITY_REFUSED in result.stdout:
        return _IDENTITY_REFUSED
    return (result.stderr or result.stdout).strip()


def session_identity_predicate(
    expected: SessionIdentity | launch_records.TmuxSessionMetadata,
) -> str:
    if isinstance(expected, SessionIdentity):
        expected.validate(require_nonce=False)
    elif (
        re.fullmatch(r"\$[0-9]+", expected.session_id) is None
        or re.fullmatch(r"[0-9]+", expected.created) is None
        or re.fullmatch(r"[A-Za-z0-9_-]{16,64}", expected.launch_nonce) is None
    ):
        fail("invalid captured tmux identity", 1)
    return (
        "#{&&:#{==:#{session_id}," + expected.session_id + "},#{&&:"
        "#{==:#{session_created}," + expected.created + "},"
        "#{==:#{E:UXON_LAUNCH_NONCE}," + expected.launch_nonce + "}}}"
    )


def conditional_kill(cfg: Config, expected: SessionIdentity) -> tuple[bool, str]:
    try:
        result = run_query(
            conditional_kill_argv(cfg, expected),
            timeout=cfg.execution.backend_for_user(expected.user).probe_timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"conditional kill response unavailable: {exc}"
    if result.returncode == 0 and result.stdout.strip() == "uxon-kill-matched":
        return True, ""
    detail = _control_error(result)
    if detail == _IDENTITY_REFUSED:
        return False, "selected session identity changed; removal refused"
    return (
        False,
        f"conditional kill failed (rc={result.returncode}): {detail or 'no identity acknowledgement'}",
    )


def tmux_base(
    cfg: Config, target_user: str, socket_path: str | None = None, *, nonint: bool = False
) -> list[str]:
    """Build the tmux command base for ``target_user``.

    ``nonint=False`` (default, launch path): wraps tmux with the
    interactive sudo prefix (``sudo -H -u``). The launch path has a TTY,
    so an unreachable target prompts/fails with a clear sudo error.

    ``nonint=True`` (listing / probing / TUI background polling): wraps
    tmux with the non-interactive prefix (``sudo -n -H -u``). A missing
    NOPASSWD grant returns a non-zero exit immediately rather than
    blocking on a hidden password prompt.
    """
    prefix = command_prefix(cfg, target_user, interactive=not nonint)
    base = prefix + ["tmux"]
    if socket_path:
        base.extend(["-S", socket_path])
    return base


def tmux_socket_path(cfg: Config, target_user: str) -> str:
    try:
        uid = pwd.getpwnam(target_user).pw_uid
    except KeyError:
        fail(f"unknown launch user for tmux socket expansion: {target_user}", 1)
    try:
        rendered = cfg.tmux_socket_template.format(
            user=target_user,
            uid=uid,
            execution_backend=cfg.execution.backend_for_user(target_user).id,
            execution_fingerprint=(cfg.execution.backend_for_user(target_user).fingerprint[:12]),
        )
    except KeyError as exc:
        fail(f"tmux_socket_template uses unsupported placeholder: {exc.args[0]!r}")
    if not rendered.startswith("/"):
        fail(f"tmux_socket_template must render to an absolute path; got: {rendered}")
    return os.path.normpath(rendered)


def configured_tmux_base(cfg: Config, target_user: str, *, nonint: bool = False) -> list[str]:
    return tmux_base(cfg, target_user, tmux_socket_path(cfg, target_user), nonint=nonint)


def tmux_host_socket() -> str | None:
    """Return the socket path of the tmux server this process is already
    inside, or ``None`` if ``$TMUX`` is unset.

    tmux exports ``$TMUX`` as ``<socket>,<server-pid>,<session-id>``.
    We only care about the socket component.
    """
    raw = os.environ.get("TMUX", "")
    if not raw:
        return None
    socket = raw.split(",", 1)[0]
    return socket or None


def tmux_nesting_mode(cfg: Config, launch_user: str, target_socket: str) -> str:
    """Decide how to launch/attach a tmux session given the current ``$TMUX``.

    Returns ``"execvp"`` for the external interactive-client mode when the
    process is not already inside tmux.
    Returns ``"switch"`` when the process is inside a tmux client on the
    same socket that owns ``target_socket`` — the caller should then use
    ``tmux switch-client -t <name>`` (plus a detached ``new-session`` for
    the launch path) so tmux does not refuse to nest.
    Raises ``SystemExit`` (via :func:`fail`) when ``$TMUX`` names a
    different socket: nesting across tmux servers is not something uxon
    can do cleanly, and the user must detach first.
    """
    host = tmux_host_socket()
    if host is None:
        return "execvp"
    if cfg.execution.backend_for_user(launch_user).kind == "command":
        fail(
            "uxon: already inside a controller-side tmux client while the target "
            "server uses a command execution backend; detach first (Ctrl-B d) and rerun uxon"
        )
    try:
        host_real = os.path.realpath(host)
    except OSError:
        host_real = host
    try:
        target_real = os.path.realpath(target_socket)
    except OSError:
        target_real = target_socket
    if host_real == target_real:
        return "switch"
    fail(
        "uxon: already inside a tmux session on a different socket "
        f"({host}); detach first (Ctrl-B d) and rerun uxon"
    )
    raise AssertionError("unreachable")


def _tmux_opt_value(value: object) -> str:
    """Render a tmux option value as a single argv token.

    ``bool`` is checked before ``int`` because ``bool`` is an ``int`` subclass;
    booleans become tmux's ``on``/``off`` rather than ``1``/``0``. Everything
    else (validated to int/str at load time) is rendered with ``str``.
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def _tmux_set_chain(cfg: Config, *, server_running: bool = False) -> list[str]:
    """Flat argv token list applying the configured tmux options.

    Returns ``[]`` when ``manage_options`` is off or all three tables are
    empty, so no option-setting argv is emitted.
    Otherwise emits one ``set <scope> <key> <value> ;`` command per option,
    in the fixed inter-table order global -> server -> append-server, TOML
    declaration order within each table (``tomllib`` preserves insertion
    order). The separator is a bare ``;`` argv token — there is no shell
    or interpolation.

    ``server_running`` gates the **append-server** scope: the tmux
    server is per launch-user and these options are server-scoped, so they
    only need applying once, at server birth. ``-g``/``-s`` *overwrite*, so
    re-asserting them on every launch/attach is idempotent and lets a
    ``config.toml`` edit take effect on the next launch without a
    ``tmux kill-server``. ``-as`` *appends* (non-idempotent) — re-emitting it
    on a live server would grow the target list unbounded — so it is emitted
    ONLY when this launch births the server (``server_running`` is False).
    """
    if not cfg.tmux_manage_options:
        return []
    scopes: list[tuple[str, dict]] = [
        ("-g", cfg.tmux_options),
        ("-s", cfg.tmux_server_options),
    ]
    if not server_running:
        scopes.append(("-as", cfg.tmux_append_server_options))
    chain: list[str] = []
    for flag, table in scopes:
        for key, value in table.items():
            chain += ["set", flag, str(key), _tmux_opt_value(value), ";"]
    return chain


def _tmux_title_chain(cfg: Config, session: str) -> list[str]:
    """Use native terminal capabilities and keep title policy session-local.

    Reassert on entry, including off, so existing sessions reflect config edits.
    A live session format follows renames and ignores workload pane titles.
    """
    # set-option accepts a pane target; the colon makes '=' match the session.
    target = f"{session}:" if session.startswith("$") else f"={session}:"
    chain = []
    if cfg.tmux_set_terminal_title:
        chain += ["set-option", "-t", target, "set-titles-string", "#{session_name}", ";"]
    chain += [
        "set-option",
        "-t",
        target,
        "set-titles",
        "on" if cfg.tmux_set_terminal_title else "off",
        ";",
    ]
    return chain


def _build_tmux_attach_request(target: SessionInfo, cfg: Config, launch_user: str):
    """Return the LaunchRequest for attaching to an existing session.

    Reads ``$TMUX`` via :func:`tmux_nesting_mode` to decide between a
    classic ``attach-session`` (when the process is not already inside
    tmux) and a ``switch-client`` (when it is, on the same socket).
    Raises ``SystemExit`` when ``$TMUX`` names a different socket.
    Used by the shared CLI/TUI interactive handoff.
    """
    base = configured_tmux_base(cfg, launch_user)
    # Attaching means the server is already alive, so re-assert the
    # overwrite scopes (``-g``/``-s``) — this is how a config.toml edit to
    # e.g. ``mouse`` takes effect when the operator re-enters an existing
    # session, without a kill-server. ``-as`` is skipped (server_running) so
    # the append list does not grow. ``[]`` when managed options are off.
    set_chain = _tmux_set_chain(cfg, server_running=True)
    target_id = target.tmux_session_id or target.name
    target_chain = _tmux_title_chain(cfg, target_id)
    if target.active_pane_dead and target.live_pane_id:
        target_chain += [
            "select-window",
            "-t",
            target.live_pane_id,
            ";",
            "select-pane",
            "-t",
            target.live_pane_id,
            ";",
        ]
    handoff = TmuxHandoff(cfg, launch_user, target.name, target.launch_record_verified)
    mode = tmux_nesting_mode(cfg, launch_user, tmux_socket_path(cfg, launch_user))
    action = "switch-client" if mode == "switch" else "attach-session"
    attach = [action, "-t", target_id]
    if target.tmux_session_id:
        validate_session_context(cfg, target.identity, require_nonce=False)
        # Only refusal runs a shell, to give the client a nonzero status.
        # The matching native branch contains no wait between check and attach.
        attach = guarded_session_commands(target.identity, _command_text(target_chain + attach))
    else:
        attach = target_chain + attach
    if mode == "switch":
        full = tuple(base + set_chain + attach)
        return LaunchRequest(
            cmd=full,
            label=f"switch-client {target.name}",
            handoff=handoff,
            identity=target.identity if target.tmux_session_id else None,
        )
    full = tuple(base + set_chain + attach)
    return LaunchRequest(
        cmd=full,
        label=f"attach {target.name}",
        handoff=handoff,
        identity=target.identity if target.tmux_session_id else None,
    )


def prepare_diagnostics_attach(req: LaunchRequest) -> tuple[str, ...]:
    """Capture and install native controls once, never during list refresh."""
    context = req.handoff
    if context is None or not context.diagnostics:
        return req.cmd
    base = configured_tmux_base(context.config, context.user)
    if req.identity is None:
        fail("cannot configure diagnostics: captured session identity is unavailable", 1)
    validate_session_context(context.config, req.identity, require_nonce=False)
    query = guarded_session_commands(
        req.identity, _command_text(diagnostics_query(req.identity.session_id)[1:])
    )
    result = process.run_query(
        base + _tmux_set_chain(context.config, server_running=True) + query,
        timeout=_TMUX_CONTROL_TIMEOUT_SECONDS,
    )
    if result.returncode:
        detail = _control_error(result)
        fail(f"could not read tmux bindings: {detail or 'tmux query failed'}")
    live_table, _, bindings = result.stdout.partition("\n")
    if not live_table:
        fail("tmux did not return the live key table")
    _install_diagnostics(
        base,
        diagnostics_script(req.identity.session_id, live_table, bindings),
        expected=req.identity,
    )
    return req.cmd


def _install_diagnostics(
    base: list[str],
    script: str,
    after: list[str] | None = None,
    *,
    expected: SessionIdentity,
) -> None:
    """Feed native configuration through stdin without files or argv limits."""
    body = script + ("\n" + shlex.join(after) if after else "")
    script = shlex.join(guarded_session_commands(expected, body)) + "\n"
    command = [*base, "source-file", "-"]
    result = process.run_query(command, input=script, timeout=_TMUX_CONTROL_TIMEOUT_SECONDS)
    if result.returncode:
        detail = _control_error(result)
        fail(f"could not configure tmux diagnostics: {detail or 'tmux rejected configuration'}")


def _build_tmux_launch_request(
    target_dir: str,
    session: str,
    args: ParsedArgs,
    cfg: Config,
    branch: str | None,
    *,
    resolved_profile: ResolvedLaunchProfile | None = None,
    server_running: bool = False,
    pending_record: launch_records.PendingLaunchRecord | None = None,
):
    """Assemble the agent + tmux argv plus the socket-parent mkdir.

    This is the single place where the agent command line is built
    The shared CLI/TUI interactive handoff reuses it.

    ``server_running`` comes from the same target-side snapshot that collected
    the session rows. It is independent of list length because a reachable tmux
    server may deliberately remain alive with zero sessions. The flag gates the
    ``-as`` scope of the managed-options chain; see :func:`_tmux_set_chain`.

    The launch profile is expected to be resolved before this is called.
    Install-gating is owned by ``app.launch_profile.resolve_launch_profile``.
    ``resolved_profile`` carries the selected profile, agent, and mode.

    ``branch`` is informational only (still printed by the dry-run path
    in ``launch_in_tmux``): uxon launches a worktree by creating it
    with ``git worktree add`` and pointing tmux at the worktree directory
    via ``-c <worktree_path>`` — it never delegates to the agent's native
    ``-w`` flag, so this parameter does not affect ``final_cmd``.
    """
    from uxon.domain.agents import permission_mode_for

    if resolved_profile is None:
        fail("internal: launch profile must be resolved before _build_tmux_launch_request")
    launch_user = resolved_profile.launch_user
    spec = resolved_profile.agent
    mode_id = resolved_profile.mode_id
    # Open modes: an unset ``--mode`` resolves to the agent's first (default)
    # mode; only an explicitly-requested unknown id reaches the fail path.
    mode_obj = permission_mode_for(spec, mode_id)
    if mode_obj is None:
        valid = ", ".join(m.id for m in spec.permission_modes)
        fail(f"unknown --mode {mode_id!r} for agent {spec.id!r}; valid modes: {valid}")
    agent_argv = (
        [spec.binary] + list(spec.default_args) + list(args.agent_args) + list(mode_obj.flags)
    )
    if pending_record is None:
        pending_record = launch_records.pending_from_resolved(
            socket_path=tmux_socket_path(cfg, launch_user),
            session_name=session,
            resolved=resolved_profile,
            target_dir=target_dir,
        )
    record_shared = bool(cfg.launch_record_dir)
    record_dir = str(
        launch_records.default_launch_record_dir(
            override=Path(cfg.launch_record_dir) if cfg.launch_record_dir else None
        )
    )
    # Workload-runtime wrap (off by default): the operator's opaque
    # ``exec_prefix`` enters the selected resource. Direct runtime keeps
    # ``exec_prefix`` empty and preserves the host agent argv. The
    # not-ready probe/start/create policy is orchestrated separately (the
    # caller runs it off the loop before launch); this single exec-site only
    # composes the resolved prefix into ``final_cmd``.
    exec_prefix: list[str] = []
    session_env: list[str] = [
        "-e",
        f"{launch_records.LAUNCH_PROFILE_ENV}={resolved_profile.profile.id}",
        "-e",
        f"{launch_records.LAUNCH_NONCE_ENV}={pending_record.launch_nonce}",
        "-e",
        f"{launch_records.LAUNCH_AGENT_ENV}={resolved_profile.agent.id}",
    ]
    runtime_identity = None
    if resolved_profile.runtime_context is not None:
        from uxon.infra.runtime import resolve_runtime_identity_for_profile

        context = resolved_profile.runtime_context
        runtime = cfg.runtimes[context.runtime_id]
        name = context.resource
        session_env += [
            "-e",
            f"{launch_records.RUNTIME_ENV}={context.runtime_id}",
            "-e",
            f"{launch_records.RUNTIME_FINGERPRINT_ENV}={context.fingerprint}",
            "-e",
            f"{RUNTIME_RESOURCE_ENV}={name}",
        ]
        exec_prefix = render_profile_template(
            runtime.exec_prefix,
            profile=runtime,
            what="exec_prefix",
            resource=context.resource,
            runtime_dir=context.runtime_dir,
            user=launch_user,
            launch_profile=resolved_profile.profile.id,
            agent=resolved_profile.agent.id,
            project_slug=slugify(os.path.basename(os.path.normpath(target_dir))),
        )
        # Launch-time telemetry markers (hoisted to the ``enabled`` guard so
        # EVERY enabled session carries them, regardless of teardown opt-in).
        # ``UXON_RUNTIME_RESOURCE`` stays the bare resource — the kill path reads
        # it verbatim and re-validates it. The workload identity and telemetry
        # SEPARATE vars, each appended only when resolution yields a non-empty
        # value; an absent variable is the documented degraded state.
        identity = resolve_runtime_identity_for_profile(cfg, target_dir, resolved_profile)
        runtime_identity = identity
        for var, value in (
            (RUNTIME_ID_ENV, identity.id),
            (RUNTIME_CGROUP_ENV, identity.cgroup),
            (RUNTIME_EPOCH_ENV, identity.epoch),
        ):
            if value:
                session_env += ["-e", f"{var}={value}"]
        # Wrap the agent for every enabled session so it exports
        # ``UXON_SESSION`` into its workload environ (telemetry attribution).
        # The pidfile write stays gated on ``stop_command`` (teardown opt-in):
        # the kill path then terminates exactly this session's recorded
        # workload PID, leaving the shared resource running.
        pidfile = runtime_pidfile(pending_record.launch_nonce) if runtime.stop_command else None
        agent_argv = wrap_agent_for_runtime(
            agent_argv, session=session, launch_nonce=pending_record.launch_nonce, pidfile=pidfile
        )
    final_cmd = exec_prefix + agent_argv
    socket_path = tmux_socket_path(cfg, launch_user)
    ensure_socket_parent = tuple(
        command_prefix(cfg, launch_user, interactive=True)
        + [sys.executable, "-m", "uxon.infra.tmux_socket", "--prepare", "--socket", socket_path]
    )
    base = configured_tmux_base(cfg, launch_user)
    # uxon-managed tmux options (3.5.0). The chain must ride the SAME
    # invocation as its ``new-session`` (fail-fast ordering) — never a
    # standalone prelaunch entry, which would birth a session-less server that
    # exits before the next subprocess. ``[]`` when disabled/empty. The
    # ``-as`` scope rides only the birthing launch (server_running False); a
    # live server already carries it (see _tmux_set_chain).
    set_chain = _tmux_set_chain(cfg, server_running=server_running)
    mode = tmux_nesting_mode(cfg, launch_user, socket_path)
    bootstrap_cmd = [
        sys.executable,
        "-m",
        "uxon.infra.launch_bootstrap",
        f"--socket={socket_path}",
        f"--session={session}",
        f"--nonce={pending_record.launch_nonce}",
        "--timeout",
        str(_LAUNCH_HANDSHAKE_TIMEOUT_SECONDS),
        "--",
        *final_cmd,
    ]
    create_cmd = tuple(
        base
        + set_chain
        + ["new-session", "-d", "-s", session, "-c", target_dir]
        + session_env
        + bootstrap_cmd
        # Retain failed terminals with tmux's native diagnostic format.
        + [
            ";",
            "set-window-option",
            "-t",
            "=" + session + ":",
            "remain-on-exit",
            "failed",
        ]
    )
    query_cmd = tuple(
        base
        + [
            "display-message",
            "-p",
            "-t",
            "=" + session + ":",
            "#{session_id}\t#{session_created}\t#{session_name}\t#{E:"
            + launch_records.LAUNCH_NONCE_ENV
            + "}",
        ]
        + diagnostics_query("=" + session + ":")
    )
    release_cmd = tuple(
        base
        + [
            "wait-for",
            "-S",
            launch_records.handshake_channel(pending_record.launch_nonce, "release"),
        ]
    )
    rollback_kill_prefix = tuple(base + ["kill-session", "-t"])
    managed = ManagedTmuxLaunch(
        create_cmd=create_cmd,
        query_cmd=query_cmd,
        release_cmd=release_cmd,
        rollback_kill_prefix=rollback_kill_prefix,
        record_socket=socket_path,
        record_session=session,
        record_nonce=pending_record.launch_nonce,
        record_dir=record_dir,
        launch_profile=pending_record.launch_profile,
        agent=pending_record.agent,
        launch_user=pending_record.launch_user,
        project=target_dir,
        branch=branch or "",
        record_shared=record_shared,
        execution_backend=pending_record.execution_backend,
        execution_fingerprint=pending_record.execution_fingerprint,
        runtime=pending_record.runtime,
        runtime_kind=pending_record.runtime_kind,
        runtime_fingerprint=pending_record.runtime_fingerprint,
        runtime_resource=pending_record.runtime_resource,
        runtime_dir=pending_record.runtime_dir,
        project_slug=pending_record.project_slug,
        runtime_id=getattr(runtime_identity, "id", ""),
        runtime_cgroup=getattr(runtime_identity, "cgroup", ""),
        runtime_epoch=getattr(runtime_identity, "epoch", ""),
        diagnostics_prefix=tuple(base),
    )
    if mode == "switch":
        switch = tuple(base + _tmux_title_chain(cfg, session) + ["switch-client", "-t", session])
        return LaunchRequest(
            cmd=switch,
            prelaunch=(ensure_socket_parent,),
            label=f"switch-client {session} (nested)",
            managed=managed,
            handoff=TmuxHandoff(cfg, launch_user),
        )
    attach = tuple(base + _tmux_title_chain(cfg, session) + ["attach-session", "-t", session])
    return LaunchRequest(
        cmd=attach,
        prelaunch=(ensure_socket_parent,),
        label=f"launch {session}",
        managed=managed,
        handoff=TmuxHandoff(cfg, launch_user),
    )


def build_managed_tmux_launch_request(
    target_dir: str,
    session: str,
    args: ParsedArgs,
    cfg: Config,
    branch: str | None,
    *,
    resolved_profile: ResolvedLaunchProfile,
    server_running: bool = False,
) -> tuple[LaunchRequest, launch_records.PendingLaunchRecord]:
    socket_path = tmux_socket_path(cfg, resolved_profile.launch_user)
    pending = launch_records.pending_from_resolved(
        socket_path=socket_path,
        session_name=session,
        resolved=resolved_profile,
        target_dir=target_dir,
    )
    req = _build_tmux_launch_request(
        target_dir,
        session,
        args,
        cfg,
        branch,
        resolved_profile=resolved_profile,
        server_running=server_running,
        pending_record=pending,
    )
    return req, pending


def pending_record_from_request(req: LaunchRequest) -> launch_records.PendingLaunchRecord:
    managed = req.managed
    if managed is None:
        fail("internal: managed launch metadata missing")
    return launch_records.PendingLaunchRecord(
        socket_path=managed.record_socket,
        session_name=managed.record_session,
        launch_nonce=managed.record_nonce,
        launch_profile=managed.launch_profile,
        agent=managed.agent,
        launch_user=managed.launch_user,
        execution_backend=managed.execution_backend,
        execution_fingerprint=managed.execution_fingerprint,
        runtime=managed.runtime,
        runtime_kind=managed.runtime_kind,
        runtime_fingerprint=managed.runtime_fingerprint,
        runtime_resource=managed.runtime_resource,
        runtime_dir=managed.runtime_dir,
        project_slug=managed.project_slug,
    )


def prepare_managed_launch(
    req: LaunchRequest, pending: launch_records.PendingLaunchRecord | None = None
) -> launch_records.TmuxSessionMetadata | None:
    managed = req.managed
    if managed is None:
        return
    if pending is None:
        pending = pending_record_from_request(req)
    record_dir = Path(managed.record_dir)
    from uxon.infra import audit as _audit

    audit_fields = {
        "profile": managed.launch_profile,
        "agent": managed.agent,
        "target_user": managed.launch_user,
        "project": managed.project,
        "branch": managed.branch,
        "session": managed.record_session,
        "dry_run": False,
        "session_id": "",
        "launch_nonce": managed.record_nonce,
        "execution_backend": managed.execution_backend,
    }
    create_attempted = False
    metadata: launch_records.TmuxSessionMetadata | None = None
    try:
        launch_records.create_pending_record(
            pending, override_dir=record_dir, shared=managed.record_shared
        )
        for prelaunch in req.prelaunch:
            process.run_cmd(list(prelaunch), timeout=_TMUX_CONTROL_TIMEOUT_SECONDS)
        create_attempted = True
        cp = process.run_cmd(
            list(managed.create_cmd), check=False, timeout=_TMUX_CONTROL_TIMEOUT_SECONDS
        )
        if cp.returncode != 0:
            detail = (cp.stderr or cp.stdout).strip()
            suffix = f": {detail[:512]}" if detail else ""
            fail(f"tmux session {pending.session_name!r} could not be prepared{suffix}")
        meta_cp = process.run_cmd(
            list(managed.query_cmd), check=True, timeout=_TMUX_CONTROL_TIMEOUT_SECONDS
        )
        metadata = _parse_tmux_launch_metadata(meta_cp.stdout)
        if metadata.name == pending.session_name and metadata.launch_nonce == pending.launch_nonce:
            audit_fields["session_id"] = metadata.session_id
        script = ""
        if managed.diagnostics_prefix:
            _, live_table, bindings = meta_cp.stdout.split("\n", 2)
            script = diagnostics_script(metadata.session_id, live_table, bindings)
        launch_records.finalize_pending_record(
            pending,
            metadata,
            runtime_id=managed.runtime_id,
            runtime_cgroup=managed.runtime_cgroup,
            runtime_epoch=managed.runtime_epoch,
            override_dir=record_dir,
            shared=managed.record_shared,
        )
        if script:
            expected_identity = managed_session_identity(managed, metadata)
            _install_diagnostics(
                list(managed.diagnostics_prefix),
                script,
                list(managed.release_cmd[len(managed.diagnostics_prefix) :]),
                expected=expected_identity,
            )
        else:
            process.run_cmd(
                list(managed.release_cmd), check=True, timeout=_TMUX_CONTROL_TIMEOUT_SECONDS
            )
    except BaseException as exc:
        if create_attempted:
            _kill_created_session_if_owned(managed, pending, metadata)
        try:
            launch_records.fail_pending_record(
                pending, override_dir=record_dir, shared=managed.record_shared
            )
        except BaseException:
            pass
        _audit.audit(
            "session.new",
            outcome="error",
            **audit_fields,
            error=(getattr(exc, "uxon_msg", None) or str(exc) or type(exc).__name__)[:512],
            error_type=type(exc).__name__,
        )
        raise
    _audit.audit("session.new", **audit_fields)
    return metadata


def _kill_created_session_if_owned(
    managed: ManagedTmuxLaunch,
    pending: launch_records.PendingLaunchRecord,
    expected: launch_records.TmuxSessionMetadata | None,
) -> None:
    """Best-effort rollback asserting the captured identity on the native queue."""
    try:
        observed = expected
        if observed is None:
            cp = process.run_cmd(
                list(managed.query_cmd), check=False, timeout=_TMUX_CONTROL_TIMEOUT_SECONDS
            )
            if cp.returncode != 0:
                return
            observed = _parse_tmux_launch_metadata(cp.stdout)
        if observed.name != pending.session_name or observed.launch_nonce != pending.launch_nonce:
            return
        process.run_cmd(
            [*managed.rollback_kill_prefix[:-2]]
            + guarded_session_commands(
                managed_session_identity(managed, observed),
                f"kill-session -t {observed.session_id}",
            ),
            check=False,
            timeout=_TMUX_CONTROL_TIMEOUT_SECONDS,
        )
    except BaseException:
        return


def managed_session_identity(
    managed: ManagedTmuxLaunch, metadata: launch_records.TmuxSessionMetadata
) -> SessionIdentity:
    return SessionIdentity(
        managed.launch_user,
        metadata.name,
        metadata.session_id,
        metadata.created,
        metadata.launch_nonce,
        managed.record_socket,
        managed.execution_backend,
        managed.execution_fingerprint,
    )


def prepared_launch_command(req: LaunchRequest, expected: SessionIdentity) -> tuple[str, ...]:
    """Bind a new launch's deferred terminal handoff to finalized metadata."""
    assert req.managed is not None and req.handoff is not None
    cfg = req.handoff.config
    validate_session_context(cfg, expected)
    action = req.cmd[-3]
    assert action in {"attach-session", "switch-client"}
    return tuple(
        configured_tmux_base(cfg, expected.user)
        + guarded_session_commands(
            expected,
            _command_text(
                _tmux_title_chain(cfg, expected.session_id) + [action, "-t", expected.session_id]
            ),
        )
    )


def _parse_tmux_launch_metadata(stdout: str) -> launch_records.TmuxSessionMetadata:
    line = stdout.strip().splitlines()[0] if stdout.strip() else ""
    parts = line.split("\t")
    if len(parts) != 4:
        fail("tmux did not return launch metadata for the created session")
    return launch_records.TmuxSessionMetadata(
        session_id=parts[0],
        created=parts[1],
        name=parts[2],
        launch_nonce=parts[3],
    )
