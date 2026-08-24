# SPDX-License-Identifier: MIT
"""Real-runtime container telemetry: per-session in-container CPU/RAM.

Opt-in, marker-gated, docker-only by reachability (the other runtime skips).
Proves the Phase-2 observability properties a mocked subprocess boundary
cannot:

* **Per-session split (AC-P1.6)** — two sessions in ONE container show
  independent CPU/RAM, attributed from each agent's ``UXON_SESSION`` environ
  marker via a single privileged batched read. A busy-loop agent in session A
  reddens A (>50% → the runaway style fires, AC-P1.2) and NOT idle session B.
* **Container down (AC-P1.8)** — once the container is stopped, a marked
  session reports the distinct down state, not a silent idle 0/—.
* **``cmd`` shows the agent id (AC-P1.4)** — the row's cmd is the resolved
  agent, not ``docker``/``sh``.

The suite drives uxon's real launch path (identity resolve → cgroup stash →
session-env markers) and its real telemetry path
(``collect_sessions_for_user`` → ``enrich_session_usage``). It never builds an
image; a stock base plus a bind-mounted selectable stub stands in for an agent.

Skipped when no reachable runtime, or when ``sudo -n`` to root is unavailable
for the per-session environ split (then only the unprivileged single-session
path would be exercisable). Runs rootless-under-the-current-user.
"""

from __future__ import annotations

import getpass
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest
from conftest import (  # type: ignore[import-not-found]
    BASE_IMAGE,
    COMPOSE_TEMPLATE,
    PROBE_TIMEOUT_SEC,
    STUB_AGENT_SELECTABLE,
    Runtime,
)
from helpers import make_config  # type: ignore[import-not-found]

from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.launch_profiles import (
    LaunchConfig,
    LaunchProfile,
    ResolvedLaunchProfile,
    RuntimeContext,
    builtin_launch_profiles,
)

pytestmark = pytest.mark.container
_RUNTIME_PROFILE_ID = "it"


def _sudo_root_available() -> bool:
    """True iff ``sudo -n true`` succeeds — the per-session environ split needs it."""
    try:
        cp = subprocess.run(
            ["sudo", "-n", "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0


def _operator_table(rt: Runtime, project_dir: Path) -> dict[str, object]:
    """Operator ``[container]`` table with a ``identity_command`` for the cgroup stash."""
    return {
        "enabled": True,
        "resource_name_template": "uxon-it-{project_slug}",
        "path_map": {str(project_dir): "/work"},
        "exec_prefix": [rt.binary, "exec", "-i", "-w", "{runtime_dir}", "{resource}"],
        "ready_command": [rt.binary, "top", "{resource}"],
        "exists_command": [rt.binary, "container", "inspect", "{resource}"],
        "start_command": [rt.binary, "compose", "start"],
        "create_command": [rt.binary, "compose", "up", "-d"],
        # Prints ``<id> <init_pid> <start_epoch>`` — uxon reads the cgroup path
        # from /proc/<init_pid>/cgroup (no hardcoded runtime layout). uxon
        # ``str.format``s each token, so the Go-template braces are DOUBLED
        # (``{{{{`` → ``{{``) to survive — the documented operator escaping for
        # a brace-using runtime template.
        "identity_command": [
            rt.binary,
            "inspect",
            "-f",
            "{{{{.Id}}}} {{{{.State.Pid}}}} {{{{.State.StartedAt}}}}",
            "{resource}",
        ],
        "on_missing": "create",
        "approval": "auto",
    }


def _build_cfg(rt: Runtime, project_dir: Path, socket_path: Path) -> Config:
    from uxon.domain.agents import default_agent_catalog
    from uxon.infra import config_loader

    agents = default_agent_catalog()
    launch_profiles = builtin_launch_profiles(agents)
    launch_profiles["claude"] = LaunchProfile(
        id="claude", agent="claude", runtime=_RUNTIME_PROFILE_ID
    )
    profile_tbl = dict(_operator_table(rt, project_dir))
    profile_tbl["resource_scope"] = "per_user"
    profile_tbl["resource_name_template"] = rt.runtime_name
    runtimes = config_loader.build_runtimes({"profiles": {_RUNTIME_PROFILE_ID: profile_tbl}})
    return make_config(
        allowed_roots=[str(project_dir)],
        tmux_socket_template=str(socket_path),
        agents=agents,
        launch=LaunchConfig(default_profile="claude", profiles=launch_profiles),
        runtimes=runtimes,
    )


def _resolved(cfg: Config, project_dir: Path, launch_user: str) -> ResolvedLaunchProfile:
    profile = cfg.launch.profiles["claude"]
    runtime = cfg.runtimes[_RUNTIME_PROFILE_ID]
    from uxon.domain.runtime import apply_path_map, resolve_runtime_resource_name
    from uxon.domain.session import slugify

    dir_token = apply_path_map(str(project_dir), runtime.path_map)
    context = RuntimeContext(
        runtime_id=runtime.id,
        resource=resolve_runtime_resource_name(
            runtime,
            user=launch_user,
            launch_profile=profile.id,
            agent=profile.agent,
            project_slug=slugify(project_dir.name),
        ),
        runtime_dir=dir_token,
        fingerprint=runtime.fingerprint,
    )
    return ResolvedLaunchProfile(
        profile=profile,
        agent=cfg.agents[profile.agent],
        launch_user=launch_user,
        mode_id="normal",
        runtime_context=context,
    )


def _write_project(project_dir: Path, _runtime_name: str) -> Path:
    stub = project_dir / "agent-stub"
    stub.write_text(STUB_AGENT_SELECTABLE)
    stub.chmod(0o755)
    return stub


def _launch(cfg: Config, project_dir: Path, session: str, launch_user: str, agent_args) -> None:
    """Drive the real launch request for ``session`` as a detached tmux session."""
    from uxon.infra import tmux

    args = ParsedArgs(
        action="run", profile="claude", permission_mode="normal", agent_args=agent_args
    )
    resolved = _resolved(cfg, project_dir, launch_user)
    with mock.patch("uxon.infra.tmux.tmux_nesting_mode", return_value="execvp"):
        req = tmux._build_tmux_launch_request(
            str(project_dir),
            session,
            args,
            cfg,
            None,
            resolved_profile=resolved,
        )
    for pre in req.prelaunch:
        subprocess.run(list(pre), check=True, timeout=PROBE_TIMEOUT_SEC)
    cmd = list(req.cmd)
    new_session_idx = cmd.index("new-session")
    cmd.insert(new_session_idx + 1, "-d")
    subprocess.run(cmd, check=True, timeout=PROBE_TIMEOUT_SEC)


def _kill_server(socket_path: Path) -> None:
    subprocess.run(
        ["tmux", "-S", str(socket_path), "kill-server"],
        capture_output=True,
        timeout=PROBE_TIMEOUT_SEC,
        check=False,
    )


def test_two_sessions_one_runtime_split_and_runaway(runtime: Runtime, tmp_path: Path) -> None:
    """AC-P1.6/P1.2: independent per-session usage; the busy session reddens, idle doesn't."""
    if runtime.binary != "docker":
        pytest.skip("telemetry split validated on docker; podman cgroup layout differs")
    if not _sudo_root_available():
        pytest.skip("sudo -n to root unavailable: per-session environ split not exercisable")

    from uxon.infra import sessions_probe
    from uxon.tui.dashboard.columns import format_cpu
    from uxon.tui.dashboard.row import from_tui_session

    launch_user = getpass.getuser()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    socket_path = tmp_path / "uxon.sock"
    stub = _write_project(project_dir, runtime.runtime_name)
    (project_dir / "compose.yml").write_text(
        COMPOSE_TEMPLATE.format(
            image=BASE_IMAGE, name=runtime.runtime_name, project_dir=project_dir, stub_path=stub
        )
    )
    cfg = _build_cfg(runtime, project_dir, socket_path)

    from uxon.app import launch as launch_app

    resolved = _resolved(cfg, project_dir, launch_user)
    launch_app.ensure_runtime_ready(cfg, str(project_dir), resolved)

    try:
        # Session A spins (busy), session B idles — same container.
        _launch(cfg, project_dir, "uxon-it@claude", launch_user, ("busy",))
        _launch(cfg, project_dir, "uxon-it@claude-2", launch_user, ())

        # Let the busy loop accumulate CPU and the markers settle.
        deadline = time.monotonic() + PROBE_TIMEOUT_SEC
        sess_a = sess_b = None
        while time.monotonic() < deadline:
            sessions = sessions_probe.collect_sessions_for_user(
                cfg,
                launch_user,
                cfg.session_prefix,
                str(socket_path),
                runtimes=cfg.runtimes,
            )
            by_name = {s.name: s for s in sessions}
            sess_a = by_name.get("uxon-it@claude")
            sess_b = by_name.get("uxon-it@claude-2")
            if sess_a and sess_b and sess_a.cpu_pct > 50.0:
                break
            time.sleep(0.5)

        assert sess_a is not None and sess_b is not None, "both sessions must be visible"
        # Both carry the marker (containerized) and the stashed cgroup.
        assert sess_a.runtime_resource == runtime.runtime_name
        assert sess_a.runtime_cgroup, "cgroup path must be stashed at launch"
        # Per-session split: A is the runaway, B is idle — independent figures.
        assert sess_a.cpu_pct > 50.0, f"busy session A should exceed 50% (got {sess_a.cpu_pct})"
        assert sess_b.cpu_pct < 50.0, f"idle session B should stay calm (got {sess_b.cpu_pct})"

        # AC-P1.2: the runaway-red style fires for A, not B.
        from uxon.domain.session import to_tui_session

        row_a = from_tui_session(to_tui_session(sess_a, cfg.session_prefix))
        row_b = from_tui_session(to_tui_session(sess_b, cfg.session_prefix))
        assert "red" in str(format_cpu(row_a).style)
        assert "red" not in str(format_cpu(row_b).style)
        # AC-P1.4: cmd shows the agent id, not docker/sh.
        assert row_a.cmd == "claude"
    finally:
        _kill_server(socket_path)


def test_stopped_runtime_shows_down(runtime: Runtime, tmp_path: Path) -> None:
    """AC-P1.8: a marked session whose container is stopped reports runtime_down.

    Drives the REAL telemetry path against a REAL stopped container — the
    empty-cgroup detection plus the operator's ``ready_command`` liveness
    confirm. The tmux session is decoupled from the agent's lifecycle here
    (we synthesize the marked :class:`SessionInfo` directly) because stopping
    the container also tears the agent's pane down, which would close the tmux
    session before it could be observed; the production down-state is for a
    session that outlives its container.
    """
    if runtime.binary != "docker":
        pytest.skip("telemetry validated on docker")

    from uxon.domain.session import SessionInfo
    from uxon.infra import sessions_probe

    launch_user = getpass.getuser()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    stub = _write_project(project_dir, runtime.runtime_name)
    (project_dir / "compose.yml").write_text(
        COMPOSE_TEMPLATE.format(
            image=BASE_IMAGE, name=runtime.runtime_name, project_dir=project_dir, stub_path=stub
        )
    )
    cfg = _build_cfg(runtime, project_dir, project_dir / "uxon.sock")

    from uxon.app import launch as launch_app
    from uxon.infra.runtime import resolve_runtime_identity_for_profile

    resolved = _resolved(cfg, project_dir, launch_user)
    launch_app.ensure_runtime_ready(cfg, str(project_dir), resolved)
    try:
        # Capture the real launch-time cgroup path while the container runs.
        ident = resolve_runtime_identity_for_profile(cfg, str(project_dir), resolved)
        assert ident.cgroup, "identity_command must yield a real cgroup path while running"
        # Stop the container — its cgroup.procs empties; ready_command → non-zero.
        subprocess.run(
            [runtime.binary, "stop", runtime.runtime_name],
            check=True,
            timeout=PROBE_TIMEOUT_SEC,
            capture_output=True,
        )
        sess = SessionInfo(
            user=launch_user,
            name="uxon-it@claude",
            attached="0",
            windows="1",
            created="2026-06-15T12:00:00+00:00",
            last_attached="2026-06-15T12:00:00+00:00",
            pane_pids=(),
            active_pid=None,
            active_cmd="docker",
            active_path=str(project_dir),
            agent="claude",
            runtime_resource=runtime.runtime_name,
            runtime=_RUNTIME_PROFILE_ID,
            runtime_cgroup=ident.cgroup,
        )
        sessions_probe.enrich_session_usage(
            cfg, [sess], runtimes=cfg.runtimes, launch_user=launch_user
        )
        assert sess.runtime_down, "a stopped container must show the distinct down state"
        assert sess.cpu_pct == 0.0
    finally:
        subprocess.run(
            [runtime.binary, "rm", "-f", runtime.runtime_name],
            capture_output=True,
            timeout=PROBE_TIMEOUT_SEC,
            check=False,
        )


def test_no_marker_session_takes_pane_walk(runtime: Runtime, tmp_path: Path) -> None:
    """AC-P0.4 (live): a non-container session enriches via the pane walk, no down state."""
    if runtime.binary != "docker":
        pytest.skip("single live runtime suffices for this invariant")
    from uxon.infra import sessions_probe

    launch_user = getpass.getuser()
    socket_path = tmp_path / "uxon.sock"
    # A plain (non-container) cfg + a real local tmux session.
    cfg = make_config(allowed_roots=[str(tmp_path)], tmux_socket_template=str(socket_path))
    subprocess.run(
        [
            "tmux",
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            "uxon-plain@claude",
            "sleep 60",
        ],
        check=True,
        timeout=PROBE_TIMEOUT_SEC,
    )
    try:
        sessions = sessions_probe.collect_sessions_for_user(
            cfg, launch_user, cfg.session_prefix, str(socket_path)
        )
        sess = next((s for s in sessions if s.name == "uxon-plain@claude"), None)
        assert sess is not None
        assert sess.runtime_resource == ""  # no marker
        assert not sess.runtime_down
    finally:
        _kill_server(socket_path)
