# SPDX-License-Identifier: MIT
"""Real-runtime container launch: in-container exec + kill-reaping.

Opt-in, marker-gated, and parametrized over docker and podman. Proves
the two properties a mocked subprocess boundary cannot:

* **In-container exec** — the agent command runs *inside* the container,
  evidenced by a marker file that only the in-container stub can create.
* **Kill-reaping** — ending the tmux session leaves no in-container agent
  process behind (``<runtime> top`` shows it gone). A kill that orphaned
  the agent would be a real regression, not just a tidiness issue.

The suite drives uxon's actual launch path: the project-layer config
merge (the unique container name comes from a ``.uxon.toml``), the
readiness probe + create step, and the single exec-site command builder.
It never builds an image — a stock base plus a bind-mounted stub agent
stands in for a real one.

Skipped entirely when no runtime is reachable, so a host without docker
or podman (or with an unreachable daemon) runs them as clean skips.
"""

from __future__ import annotations

import getpass
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest
from conftest import (  # type: ignore[import-not-found]
    AGENT_SENTINEL,
    BASE_IMAGE,
    COMPOSE_TEMPLATE,
    PROBE_TIMEOUT_SEC,
    Runtime,
    write_project,
)
from helpers import make_config  # type: ignore[import-not-found]

from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.launch_profiles import (
    ContainerContext,
    LaunchConfig,
    LaunchProfile,
    ResolvedLaunchProfile,
    builtin_launch_profiles,
)

_CONTAINER_PROFILE_ID = "it"

pytestmark = pytest.mark.container


def _operator_container_table(rt: Runtime, project_dir: Path) -> dict[str, object]:
    """The operator ``[container]`` table for ``rt`` (argv templates only).

    Mirrors what ``config/config.toml`` would carry: the exec wrap, the
    state probes, and a ``create_template`` delegating to ``compose`` —
    all opaque operator argv. The project ``.uxon.toml`` supplies only the
    name (merged in by :func:`_load_container`).
    """
    return {
        "enabled": True,
        # name comes from the project .uxon.toml; a name_template is still
        # required by validation, so give a harmless fallback.
        "name_template": "uxon-it-{project_slug}",
        # The compose file bind-mounts the host project dir at /work, so the
        # exec wrap's ``{dir}`` (``-w``) must resolve to the CONTAINER path,
        # not the host one — otherwise ``<runtime> exec -w <host-path>`` fails
        # with "chdir ... no such file or directory" before the agent runs.
        # This is the path translation a real container operator configures.
        "path_map": {str(project_dir): "/work"},
        "exec_template": [rt.binary, "exec", "-i", "-w", "{dir}", "{name}"],
        # Teardown mirror of the exec wrap: on kill, terminate exactly this
        # session's in-container PID (recorded by the launch wrapper into
        # ``{pidfile}``) and clean the file up. ``2>/dev/null`` + a trailing
        # ``rm -f`` keep the exit status 0 even when the agent is already gone.
        "stop_template": [
            rt.binary,
            "exec",
            "{name}",
            "sh",
            "-c",
            "kill $(cat {pidfile}) 2>/dev/null; rm -f {pidfile}",
        ],
        "resolve_cmd": [
            rt.binary,
            "inspect",
            "-f",
            "{{{{.Id}}}} {{{{.State.Pid}}}} {{{{.State.StartedAt}}}}",
            "{name}",
        ],
        "is_running_cmd": [rt.binary, "top", "{name}"],
        "exists_cmd": [rt.binary, "container", "inspect", "{name}"],
        # on_missing="create" still requires a start_template (a created
        # container can later be stopped and need restarting), even though
        # this run only ever takes the create path. Both delegate to compose.
        "start_template": [rt.binary, "compose", "start"],
        "create_template": [rt.binary, "compose", "up", "-d"],
        "on_missing": "create",
        "on_missing_mode": "auto",
    }


def _load_container(rt: Runtime, project_dir: Path):
    """Build a validated ContainerConfig the way the loader does.

    The unique runtime name is operator-owned test data. Project-level
    runtime policy is no longer read.
    """
    from uxon.infra import config_loader

    container_tbl = _operator_container_table(rt, project_dir)
    container_tbl["name"] = rt.container_name
    return config_loader.build_container_config(container_tbl)  # type: ignore[arg-type]


def _cfg(rt: Runtime, project_dir: Path, socket_path: Path) -> Config:
    container = _load_container(rt, project_dir)
    from uxon.domain.agents import default_agent_catalog
    from uxon.infra import config_loader

    agents = default_agent_catalog()
    launch_profiles = builtin_launch_profiles(agents)
    launch_profiles["claude"] = LaunchProfile(
        id="claude", agent="claude", container_profile=_CONTAINER_PROFILE_ID
    )
    profile_tbl = dict(_operator_container_table(rt, project_dir))
    profile_tbl.pop("enabled", None)
    profile_tbl["runtime_namespace"] = "per_user"
    profile_tbl["name_template"] = rt.container_name
    container_profiles = config_loader.build_container_profiles(
        {"profiles": {_CONTAINER_PROFILE_ID: profile_tbl}}
    )
    return make_config(
        allowed_roots=[str(project_dir)],
        # Per-test socket under tmp_path so this never touches a real uxon
        # server and teardown is total.
        tmux_socket_template=str(socket_path),
        container=container,
        agents=agents,
        launch=LaunchConfig(default_profile="claude", profiles=launch_profiles),
        container_profiles=container_profiles,
    )


def _resolved(cfg: Config, project_dir: Path, launch_user: str) -> ResolvedLaunchProfile:
    profile = cfg.launch.profiles["claude"]
    container_profile = cfg.container_profiles[_CONTAINER_PROFILE_ID]
    from uxon.domain.container import apply_path_map, resolve_profile_container_name
    from uxon.domain.session import slugify

    dir_token = apply_path_map(str(project_dir), container_profile.path_map)
    context = ContainerContext(
        profile_id=container_profile.id,
        name=resolve_profile_container_name(
            container_profile,
            user=launch_user,
            launch_profile=profile.id,
            agent=profile.agent,
            project_slug=slugify(project_dir.name),
        ),
        dir_token=dir_token,
        profile_fingerprint=container_profile.fingerprint,
    )
    return ResolvedLaunchProfile(
        profile=profile,
        agent=cfg.agents[profile.agent],
        launch_user=launch_user,
        mode_id="normal",
        container=context,
    )


def _in_container_pids(rt: Runtime, name: str) -> str:
    """``<runtime> top`` output, or '' if the container is gone."""
    cp = subprocess.run(
        [rt.binary, "top", name],
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SEC,
        check=False,
    )
    return cp.stdout if cp.returncode == 0 else ""


def _kill_tmux(socket_path: Path, session: str) -> None:
    subprocess.run(
        ["tmux", "-S", str(socket_path), "kill-session", "-t", session],
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SEC,
        check=False,
    )


def test_launch_runs_agent_in_container_and_kill_reaps(runtime: Runtime, tmp_path: Path) -> None:
    """End-to-end: create → exec inside container → kill reaps the agent.

    Asserts (a) the marker file the stub touches appears, proving the
    command ran inside the container, and (b) after ``tmux kill-session``
    the in-container agent process is gone.
    """
    from uxon.app import kill as kill_app
    from uxon.app import launch as launch_app
    from uxon.infra import tmux

    launch_user = getpass.getuser()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    socket_path = tmp_path / "uxon.sock"

    stub_path = write_project(project_dir, runtime.container_name)
    # The compose file is the create_template's input — stock base + the
    # bind mounts; no build.
    (project_dir / "compose.yml").write_text(
        COMPOSE_TEMPLATE.format(
            image=BASE_IMAGE,
            name=runtime.container_name,
            project_dir=project_dir,
            stub_path=stub_path,
        )
    )

    cfg = _cfg(runtime, project_dir, socket_path)

    # 1. Readiness: container is absent → on_missing="create" runs the
    #    compose create_template (the real ensure_container_ready path).
    resolved = _resolved(cfg, project_dir, launch_user)
    launch_app.ensure_container_ready(cfg, str(project_dir), resolved)

    # 2. Build the real launch request (the single exec-site wrap) and run
    #    it as a detached tmux session — the same argv the CLI would exec,
    #    minus the process-replacing execvp. Force the classic (new-session)
    #    flow so the request shape is deterministic even when this runs
    #    inside an outer tmux client.
    args = ParsedArgs(action="run", profile="claude", permission_mode="normal")
    with mock.patch("uxon.infra.tmux.tmux_nesting_mode", return_value="execvp"):
        req = tmux._build_tmux_launch_request(
            str(project_dir),
            "uxon-it@claude",
            args,
            cfg,
            None,
            resolved_profile=resolved,
        )
    for pre in req.prelaunch:
        subprocess.run(list(pre), check=True, timeout=PROBE_TIMEOUT_SEC)
    # Detach (-d) so the test process is not replaced; same argv otherwise.
    cmd = list(req.cmd)
    new_session_idx = cmd.index("new-session")
    cmd.insert(new_session_idx + 1, "-d")
    subprocess.run(cmd, check=True, timeout=PROBE_TIMEOUT_SEC)

    try:
        # 3. AC-C2: the stub ran inside the container — the marker appears
        #    on the bind-mounted /work (host-side project dir).
        marker = project_dir / ".agent-ran"
        deadline = time.monotonic() + PROBE_TIMEOUT_SEC
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert marker.exists(), "stub agent did not run inside the container"

        # 4. AC-B5: uxon's kill path reaps the in-container agent. Raw
        #    ``tmux kill-session`` only severs the client-side ``exec`` and
        #    orphans the agent under docker/podman; uxon's stop_template
        #    teardown (driven here through the real ``do_kill``) terminates
        #    the recorded in-container PID. The container itself stays up
        #    (PID 1 idles on ``tail``), so the agent's ``sleep`` is the
        #    unambiguous signal of an orphan.
        before = _in_container_pids(runtime, runtime.container_name)
        assert AGENT_SENTINEL in before, "expected the idle agent process before kill"

        kill_args = ParsedArgs(action="kill", target_id="uxon-it@claude", force=True)
        kill_app.do_kill(kill_args, cfg, launch_user)

        deadline = time.monotonic() + PROBE_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if AGENT_SENTINEL not in _in_container_pids(runtime, runtime.container_name):
                break
            time.sleep(0.2)
        after = _in_container_pids(runtime, runtime.container_name)
        assert AGENT_SENTINEL not in after, (
            "uxon kill left the in-container agent running (stop_template did not reap it)"
        )
    finally:
        _kill_tmux(socket_path, "uxon-it@claude")
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            capture_output=True,
            timeout=PROBE_TIMEOUT_SEC,
            check=False,
        )
