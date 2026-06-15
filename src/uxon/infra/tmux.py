# SPDX-License-Identifier: MIT
"""tmux adapter: socket-path resolution, nesting detection, the managed-
options ``set`` chain, and the agent + tmux launch/attach command builders.

``_build_tmux_launch_request`` is the single place where an agent command
line is assembled (see AGENTS.md hard rules) — do not add any other
agent-exec call site. It reads the agent's spec from the merged catalog
``cfg.agents`` (built from :data:`uxon.domain.agents.DEFAULT_AGENT_CATALOG`
⊕ operator config in ``load_config``).
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

from uxon.domain.args import ParsedArgs
from uxon.domain.authz import canonical
from uxon.domain.config import Config
from uxon.domain.container import (
    CONTAINER_CGROUP_ENV,
    CONTAINER_EPOCH_ENV,
    CONTAINER_ID_ENV,
    CONTAINER_NAME_ENV,
    apply_path_map,
    container_pidfile,
    render_exec_prefix,
    resolve_container_name,
    wrap_agent_for_container,
)
from uxon.domain.launch_request import LaunchRequest
from uxon.domain.session import SessionInfo, slugify
from uxon.errors import fail
from uxon.infra import process
from uxon.infra.identity import command_prefix_for_user, nonint_command_prefix_for_user


def tmux_base(
    target_user: str, socket_path: str | None = None, *, nonint: bool = False
) -> list[str]:
    """Build the tmux command base for ``target_user``.

    ``nonint=False`` (default, launch path): wraps tmux with the
    interactive sudo prefix (``sudo -iu``). The launch path has a TTY,
    so an unreachable target prompts/fails with a clear sudo error.

    ``nonint=True`` (listing / probing / TUI background polling): wraps
    tmux with the non-interactive prefix (``sudo -niu``). A missing
    NOPASSWD grant returns a non-zero exit immediately rather than
    blocking on a hidden password prompt.
    """
    prefix = (
        nonint_command_prefix_for_user(target_user)
        if nonint
        else command_prefix_for_user(target_user)
    )
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
        rendered = cfg.tmux_socket_template.format(user=target_user, uid=uid)
    except KeyError as exc:
        fail(f"tmux_socket_template uses unsupported placeholder: {exc.args[0]!r}")
    if not rendered.startswith("/"):
        fail(f"tmux_socket_template must render to an absolute path; got: {rendered}")
    socket_path = canonical(rendered)
    return socket_path


def configured_tmux_base(cfg: Config, target_user: str, *, nonint: bool = False) -> list[str]:
    return tmux_base(target_user, tmux_socket_path(cfg, target_user), nonint=nonint)


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


def tmux_nesting_mode(target_socket: str) -> str:
    """Decide how to launch/attach a tmux session given the current ``$TMUX``.

    Returns ``"execvp"`` when the process is not already inside tmux
    (classic flow: ``execvp tmux attach-session`` / ``new-session``).
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
    """Render a tmux option value as a single argv token (D4/AC8a).

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
    empty — so the launch argv is byte-identical to pre-3.5.0 (AC1/AC-empty).
    Otherwise emits one ``set <scope> <key> <value> ;`` command per option,
    in the fixed inter-table order global -> server -> append-server, TOML
    declaration order within each table (``tomllib`` preserves insertion
    order). The separator is a bare ``;`` argv token — there is no shell
    (D2/D5).

    ``server_running`` gates the **append-server** scope (D9): the tmux
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


def resolve_container(cfg: Config, target_dir: str, launch_user: str) -> tuple[str, str]:
    """Resolve the (validated name, container-side {dir}) for ``target_dir``.

    Deterministic in (launch_user, target dir) — never per-session — so every
    session for a user in one project execs into the same container. The
    ``{project_slug}`` placeholder derives from the host directory basename
    (attacker-controllable), so the resolved name is re-validated against the
    safe charset (AC-B8). ``{dir}`` is the host path with ``path_map``
    longest-prefix applied (container-side); no map → host path verbatim.
    ``allowed_roots`` and the writable-probe keep running on the HOST path.
    """
    dir_token = apply_path_map(target_dir, cfg.container.path_map)
    project_slug = slugify(os.path.basename(os.path.normpath(target_dir)))
    name = resolve_container_name(
        cfg.container, user=launch_user, project_slug=project_slug, dir_token=dir_token
    )
    return name, dir_token


def read_session_env(cfg: Config, user: str, session: str, var: str) -> str | None:
    """Read one session-environment variable from a live tmux ``session``.

    Used by the kill path to recover the container name uxon stashed at
    launch (``new-session -e``). Runs under the non-interactive sudo prefix
    (the kill path has no TTY). Returns the value, or None when the variable
    is unset, the session is gone, or tmux errors — every "can't read it"
    case degrades to "skip teardown", never an abort of the kill.
    """
    base = tmux_base(user, tmux_socket_path(cfg, user), nonint=True)
    cp = process.run_cmd(base + ["show-environment", "-t", session, var], check=False)
    if cp.returncode != 0:
        return None
    # tmux prints ``VAR=value`` when set, or ``-VAR`` when explicitly unset.
    line = cp.stdout.strip()
    prefix = f"{var}="
    return line[len(prefix) :] if line.startswith(prefix) else None


def _build_tmux_attach_request(target: SessionInfo, cfg: Config, launch_user: str):
    """Return the LaunchRequest for attaching to an existing session.

    Reads ``$TMUX`` via :func:`tmux_nesting_mode` to decide between a
    classic ``attach-session`` (when the process is not already inside
    tmux) and a ``switch-client`` (when it is, on the same socket).
    Raises ``SystemExit`` when ``$TMUX`` names a different socket.
    Used by both the CLI execvp path (``attach_session``) and the
    TUI fork-and-wait path.
    """
    base = configured_tmux_base(cfg, launch_user)
    # Attaching means the server is already alive, so re-assert the
    # overwrite scopes (``-g``/``-s``) — this is how a config.toml edit to
    # e.g. ``mouse`` takes effect when the operator re-enters an existing
    # session, without a kill-server. ``-as`` is skipped (server_running) so
    # the append list does not grow. ``[]`` when managed options are off.
    set_chain = _tmux_set_chain(cfg, server_running=True)
    mode = tmux_nesting_mode(tmux_socket_path(cfg, launch_user))
    if mode == "switch":
        full = tuple(base + set_chain + ["switch-client", "-t", target.name])
        return LaunchRequest(cmd=full, prelaunch=(), label=f"switch-client {target.name}")
    full = tuple(base + set_chain + ["attach-session", "-t", target.name])
    return LaunchRequest(cmd=full, prelaunch=(), label=f"attach {target.name}")


def _build_tmux_launch_request(
    target_dir: str,
    session: str,
    args: ParsedArgs,
    cfg: Config,
    branch: str | None,
    launch_user: str,
    *,
    server_running: bool = False,
):
    """Assemble the agent + tmux argv plus the socket-parent mkdir.

    This is the single place where the agent command line is built
    (see AGENTS.md "hard rules"). Both the CLI execvp path
    (``launch_in_tmux``) and the TUI fork-and-wait path reuse it.

    ``server_running`` (derived by callers from the already-collected
    per-user session list — a non-empty list means the user's tmux server
    is live) gates the ``-as`` scope of the managed-options chain: see
    :func:`_tmux_set_chain`. Defaults to False (treat as a server birth →
    full chain); every real launch path passes the actual value.

    NB: ``bool(sessions)`` is a liveness *proxy* — it reuses the data the
    launch/dashboard path already collected (no extra probe). It
    under-reports (False on a live server) only if the dedicated per-user
    socket carries sessions that don't match uxon's name prefix, which
    AGENTS.md disallows (that socket is uxon-exclusive). In that off-policy
    state the ``-as`` append scope would re-emit and its list would grow;
    a precise fix would cost a dedicated ``list-sessions`` liveness call per
    launch, deliberately not added.

    The agent is expected to be resolved before this is called —
    install-gating is owned by ``resolve_agent_id`` (run from
    the action handlers and TUI callbacks). This function only
    enforces that the picked id is in ``cfg.agents``; if ``args.agent``
    is unset it falls back to ``cfg.default_agent`` as a last-ditch
    policy hook for callers that legitimately skip resolution
    (dry-run tests, etc.).

    ``branch`` is informational only (still printed by the dry-run path
    in ``launch_in_tmux``): uxon launches a worktree by creating it
    with ``git worktree add`` and pointing tmux at the worktree directory
    via ``-c <worktree_path>`` — it never delegates to the agent's native
    ``-w`` flag, so this parameter does not affect ``final_cmd`` (§2.1).
    """
    from uxon.domain.agents import permission_mode_for

    agent_id = args.agent or cfg.default_agent
    if not agent_id:
        fail("internal: no agent resolved before _build_tmux_launch_request")
    if agent_id not in cfg.agents:
        fail(f"unknown agent id {agent_id!r}")
    spec = cfg.agents[agent_id]
    # Open modes: an unset ``--mode`` resolves to the agent's first (default)
    # mode; only an explicitly-requested unknown id reaches the fail path.
    mode_id = args.permission_mode or spec.permission_modes[0].id
    mode_obj = permission_mode_for(spec, mode_id)
    if mode_obj is None:
        valid = ", ".join(m.id for m in spec.permission_modes)
        fail(f"unknown --mode {mode_id!r} for agent {agent_id!r}; valid modes: {valid}")
    agent_argv = (
        [spec.binary] + list(spec.default_args) + list(args.agent_args) + list(mode_obj.flags)
    )
    # Container exec wrap (off by default): when enabled, the operator's
    # opaque ``exec_template`` prefixes the agent command so it runs inside a
    # container. Disabled → ``exec_prefix``/``session_env`` stay empty and
    # ``final_cmd`` is byte-for-byte the pre-container argv (AC-B6). The
    # not-ready probe/start/create policy is orchestrated separately (the
    # caller runs it off the loop before launch); this single exec-site only
    # composes the resolved prefix into ``final_cmd``.
    exec_prefix: list[str] = []
    session_env: list[str] = []
    if cfg.container.enabled:
        from uxon.infra.container import resolve_container_identity

        name, dir_token = resolve_container(cfg, target_dir, launch_user)
        exec_prefix = render_exec_prefix(
            cfg.container.exec_template, name=name, dir_token=dir_token
        )
        # Launch-time telemetry markers (hoisted to the ``enabled`` guard so
        # EVERY enabled session carries them, regardless of teardown opt-in).
        # ``UXON_CONTAINER``'s value stays the BARE name — the kill path reads
        # it verbatim and re-validates with ``is_valid_container_name``. The
        # container identity (id / host-side cgroup path / start-epoch) rides
        # SEPARATE vars, each appended only when resolution yields a non-empty
        # value (absent var = the documented degrade; AC-P1.3 / AC-P3.5).
        session_env = ["-e", f"{CONTAINER_NAME_ENV}={name}"]
        identity = resolve_container_identity(cfg, target_dir, launch_user)
        for var, value in (
            (CONTAINER_ID_ENV, identity.id),
            (CONTAINER_CGROUP_ENV, identity.cgroup),
            (CONTAINER_EPOCH_ENV, identity.epoch),
        ):
            if value:
                session_env += ["-e", f"{var}={value}"]
        # Wrap the agent for every enabled session so it exports
        # ``UXON_SESSION`` into its in-container environ (telemetry attribution).
        # The pidfile write stays gated on ``stop_template`` (teardown opt-in):
        # the kill path then terminates exactly this session's recorded
        # in-container PID, leaving the shared container running.
        pidfile = container_pidfile(session) if cfg.container.stop_template else None
        agent_argv = wrap_agent_for_container(agent_argv, session=session, pidfile=pidfile)
    final_cmd = exec_prefix + agent_argv
    socket_path = tmux_socket_path(cfg, launch_user)
    socket_parent = str(Path(socket_path).parent)
    ensure_socket_parent = tuple(
        command_prefix_for_user(launch_user) + ["mkdir", "-p", socket_parent]
    )
    base = configured_tmux_base(cfg, launch_user)
    # uxon-managed tmux options (3.5.0). The chain must ride the SAME
    # invocation as its ``new-session`` (fail-fast ordering, D5) — never a
    # standalone prelaunch entry, which would birth a session-less server that
    # exits before the next subprocess. ``[]`` when disabled/empty (AC1). The
    # ``-as`` scope rides only the birthing launch (server_running False); a
    # live server already carries it (D9, see _tmux_set_chain).
    set_chain = _tmux_set_chain(cfg, server_running=server_running)
    mode = tmux_nesting_mode(socket_path)
    if mode == "switch":
        # Already inside tmux on the target socket — classic
        # ``new-session -As`` would try to attach and tmux refuses to
        # nest. Instead create the session detached (idempotent via
        # ``-dA``; claude is ignored when the session already exists)
        # and then switch the current client over to it. The set chain
        # rides the detached-create entry, before its ``new-session`` (AC3).
        create = tuple(
            base
            + set_chain
            + ["new-session", "-dA", "-s", session, "-c", target_dir]
            + session_env
            + final_cmd
        )
        switch = tuple(base + ["switch-client", "-t", session])
        return LaunchRequest(
            cmd=switch,
            prelaunch=(ensure_socket_parent, create),
            label=f"switch-client {session} (nested)",
        )
    full = tuple(
        base
        + set_chain
        + ["new-session", "-As", session, "-c", target_dir]
        + session_env
        + final_cmd
    )
    return LaunchRequest(cmd=full, prelaunch=(ensure_socket_parent,), label=f"launch {session}")


def launch_in_tmux(
    target_dir: str,
    session: str,
    args: ParsedArgs,
    cfg: Config,
    branch: str | None,
    launch_user: str,
    *,
    server_running: bool = False,
) -> int:
    import shlex

    req = _build_tmux_launch_request(
        target_dir, session, args, cfg, branch, launch_user, server_running=server_running
    )
    if args.dry_run:
        print(f"launch_user={shlex.quote(launch_user)}")
        print(f"dir={shlex.quote(target_dir)}")
        print(f"socket={shlex.quote(tmux_socket_path(cfg, launch_user))}")
        for pre in req.prelaunch:
            print(f"socket_parent_mkdir={shlex.join(pre)}")
        print(f"session={shlex.quote(session)}")
        if branch:
            print(f"branch={shlex.quote(branch)}")
        print(f"exec {shlex.join(req.cmd)}")
        return 0
    for pre in req.prelaunch:
        process.run_cmd(list(pre))
    os.execvp(req.cmd[0], list(req.cmd))
    return 0


def launch_in_tmux_blocking(
    target_dir: str,
    session: str,
    args: ParsedArgs,
    cfg: Config,
    branch: str | None,
    launch_user: str,
    *,
    server_running: bool = False,
) -> int:
    """Fork-and-wait variant of :func:`launch_in_tmux` for the TUI path."""
    import subprocess

    req = _build_tmux_launch_request(
        target_dir, session, args, cfg, branch, launch_user, server_running=server_running
    )
    for pre in req.prelaunch:
        rc = subprocess.call(list(pre))
        if rc != 0:
            return rc
    return subprocess.call(list(req.cmd))
