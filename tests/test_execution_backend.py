"""Contract tests for the v4 target-user execution boundary."""

from __future__ import annotations

import ast
import getpass
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from helpers import make_config, make_session

from uxon.app.doctor import _doctor_execution_rows
from uxon.domain.args import ParsedArgs
from uxon.domain.execution import (
    ExecutionBackendSpec,
    ExecutionConfig,
    validate_execution_config,
)
from uxon.domain.launch_profiles import LaunchProfile, ResolvedLaunchProfile, RuntimeContext
from uxon.domain.runtime import WorkloadRuntimeSpec
from uxon.infra import agents, git, identity, probes, runtime, sessions_probe, tmux
from uxon.infra.execution import (
    ensure_launch_compatible,
    probe,
    resolve_target,
    wrap_command,
)


def _command_cfg(
    *,
    backend_id: str = "boundary",
    command_prefix: tuple[str, ...] = ("fake-boundary", "--user", "{user}", "--"),
):
    backend = ExecutionBackendSpec(
        id=backend_id,
        kind="command",
        command_prefix=command_prefix,
        probe_command=("fake-boundary-probe", "{user}"),
        probe_timeout_seconds=1.25,
    )
    execution = ExecutionConfig(
        default_backend=backend_id,
        backends={
            "local": ExecutionConfig().backends["local"],
            backend_id: backend,
        },
    )
    return make_config(
        execution=execution,
        tmux_socket_template="/tmp/uxon-{user}-{execution_backend}.sock",
    )


def _cp(*, stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout, "")


def test_one_command_prefix_for_interactive_and_background_work() -> None:
    cfg = _command_cfg()
    expected = ["fake-boundary", "--user", "alice", "--", "true"]
    assert wrap_command(cfg, "alice", ["true"], interactive=True) == expected
    assert wrap_command(cfg, "alice", ["true"], interactive=False) == expected


@pytest.mark.parametrize("token", ("{user.__class__}", "{user!r}", "{user:>5}", "{"))
def test_execution_template_rejects_nonliteral_user_expansion(token: str) -> None:
    cfg = _command_cfg(command_prefix=("boundary", token))
    with pytest.raises(SystemExit):
        validate_execution_config(cfg.execution)


def test_backend_probe_uses_its_probe_contract() -> None:
    cfg = _command_cfg()
    with mock.patch("uxon.infra.execution.run_query", return_value=_cp()) as run:
        result = probe(cfg, "alice")
    assert result.ok
    run.assert_called_once_with(["fake-boundary-probe", "alice"], timeout=1.25)


def test_doctor_exposes_old_config_drain_policy() -> None:
    cfg = _command_cfg()
    profile = cfg.launch.profiles["claude"]
    cfg.launch.profiles["claude"] = LaunchProfile(
        id=profile.id,
        agent=profile.agent,
        launch_user="alice",
    )
    with mock.patch("uxon.infra.execution.run_query", return_value=_cp()):
        rows = _doctor_execution_rows(cfg, "operator")
    assert rows[0]["backend"] == "boundary"
    assert rows[0]["change_policy"] == "drain-with-old-config"


def test_tmux_server_list_attach_and_kill_share_the_boundary() -> None:
    cfg = _command_cfg()
    prefix = ["fake-boundary", "--user", "alice", "--"]
    socket = "/tmp/uxon-alice-boundary.sock"
    assert tmux.tmux_base(cfg, "alice", socket) == prefix + ["tmux", "-S", socket]

    session = make_session(user="alice")
    with (
        mock.patch("uxon.infra.tmux.tmux_socket_path", return_value=socket),
        mock.patch.dict(os.environ, {}, clear=False),
    ):
        os.environ.pop("TMUX", None)
        attach = tmux._build_tmux_attach_request(session, cfg, "alice")
    assert list(attach.cmd[: len(prefix)]) == prefix
    assert "attach-session" in attach.cmd

    with (
        mock.patch("uxon.infra.sessions_probe.run_query", return_value=_cp(returncode=1)) as run,
    ):
        assert sessions_probe.collect_sessions_for_user(cfg, "alice", "uxon-", socket) == []
    assert run.call_args.args[0][: len(prefix)] == prefix
    assert run.call_args.args[0][-1] == "list-sessions"

    kill = tmux.tmux_base(cfg, "alice", socket, nonint=True) + [
        "kill-session",
        "-t",
        session.name,
    ]
    assert kill[: len(prefix)] == prefix


def test_git_fs_binary_and_runtime_commands_are_nested_under_boundary(tmp_path: Path) -> None:
    cfg = _command_cfg()
    prefix = ["fake-boundary", "--user", "alice", "--"]

    with mock.patch("uxon.infra.git.run_query", return_value=_cp(stdout="/srv/repo\n")) as run:
        assert git.git_repo_root_nonint_as_user(cfg, "/srv/repo/sub", "alice") == "/srv/repo"
    assert run.call_args.args[0][: len(prefix)] == prefix
    assert "git" in run.call_args.args[0]

    with (
        mock.patch("uxon.infra.identity.process_user", return_value="alice"),
        mock.patch("uxon.infra.identity.run_query", return_value=_cp()) as run,
    ):
        assert identity.probe_cwd_writable(cfg, "alice", str(tmp_path))
    assert run.call_args.args[0][: len(prefix)] == prefix
    assert run.call_args.args[0][-3:] == ["test", "-w", str(tmp_path)]

    with mock.patch("uxon.infra.agents.run_query", return_value=_cp(stdout="1.0\n")) as run:
        assert agents._probe_one(cfg, "claude", "alice").status == "ok"
    assert run.call_args.args[0][: len(prefix)] == prefix

    with mock.patch(
        "uxon.infra.probes.run_query",
        return_value=_cp(stdout="tmux\t/usr/bin/tmux\n"),
    ) as run:
        assert probes._resolve_paths_remote(cfg, ["tmux"], "alice")["tmux"] == "/usr/bin/tmux"
    assert run.call_args.args[0][: len(prefix)] == prefix

    with mock.patch("uxon.infra.runtime.run_query", return_value=_cp()) as run:
        assert runtime._probe_exit_ok(cfg, ["docker", "top", "box"], "alice", 2.0)
    assert run.call_args.args[0] == prefix + ["docker", "top", "box"]

    with mock.patch("uxon.infra.runtime.run_query", return_value=_cp()) as run:
        runtime._run_prepare(
            cfg,
            ["docker", "start", "box"],
            "/srv/repo",
            "alice",
            3.0,
        )
    prepare = run.call_args.args[0]
    assert prepare[: len(prefix)] == prefix
    assert prepare[len(prefix) : len(prefix) + 3] == ["sh", "-c", mock.ANY]
    assert prepare[-3:] == ["docker", "start", "box"]


def test_nested_runtime_never_replaces_execution_boundary() -> None:
    runtime_spec = WorkloadRuntimeSpec(
        id="box",
        kind="command",
        resource_name_template="box-{project_slug}",
        exec_prefix=("docker", "exec", "-w", "{runtime_dir}", "{resource}"),
    )
    cfg = _command_cfg()
    cfg.runtimes["box"] = runtime_spec
    resolved = ResolvedLaunchProfile(
        profile=LaunchProfile(id="claude_box", agent="claude", runtime="box"),
        agent=cfg.agents["claude"],
        launch_user="alice",
        mode_id="normal",
        execution=resolve_target(cfg, "alice"),
        runtime_context=RuntimeContext(
            runtime_id="box",
            resource="box-demo",
            runtime_dir="/work/demo",
            fingerprint=runtime_spec.fingerprint,
        ),
    )
    socket = "/tmp/uxon-alice-boundary.sock"
    with (
        mock.patch("uxon.infra.tmux.tmux_socket_path", return_value=socket),
        mock.patch.dict(os.environ, {}, clear=False),
    ):
        os.environ.pop("TMUX", None)
        request, _pending = tmux.build_managed_tmux_launch_request(
            "/srv/demo",
            "uxon-demo@claude_box",
            ParsedArgs(action="run"),
            cfg,
            None,
            resolved_profile=resolved,
            include_runtime_identity=False,
        )
    assert request.managed is not None
    create = list(request.managed.create_cmd)
    boundary = ["fake-boundary", "--user", "alice", "--"]
    assert create[: len(boundary)] == boundary
    assert create[len(boundary) : len(boundary) + 3] == ["tmux", "-S", socket]
    runtime_start = create.index("docker")
    assert create[runtime_start : runtime_start + 5] == [
        "docker",
        "exec",
        "-w",
        "/work/demo",
        "box-demo",
    ]
    assert create[runtime_start + 5] == "sh"
    assert "claude" in create[runtime_start + 6 :]


def test_same_backend_id_change_blocks_launch_but_keeps_socket_addressable() -> None:
    old_cfg = _command_cfg(command_prefix=("nsenter", "--net=old", "--"))
    new_cfg = _command_cfg(command_prefix=("nsenter", "--net=new", "--"))
    session = make_session(user="alice")
    session.launch_record_verified = True
    session.execution_backend = "boundary"
    session.execution_fingerprint = old_cfg.execution.backends["boundary"].fingerprint

    with mock.patch("uxon.infra.tmux.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=1001)):
        assert tmux.tmux_socket_path(old_cfg, "alice") == tmux.tmux_socket_path(new_cfg, "alice")
    with pytest.raises(SystemExit) as exc:
        ensure_launch_compatible(new_cfg, "alice", [session])
    message = str(getattr(exc.value, "uxon_msg", exc.value))
    assert "Drain them before changing [execution]" in message
    assert "restore the previous execution config" in message


def test_backend_id_change_selects_a_distinct_socket() -> None:
    first = _command_cfg(backend_id="netns_a")
    second = _command_cfg(backend_id="netns_b")
    with mock.patch("uxon.infra.tmux.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=1001)):
        assert tmux.tmux_socket_path(first, "alice") != tmux.tmux_socket_path(second, "alice")


def test_target_user_sudo_prefix_is_centralized() -> None:
    root = Path(__file__).resolve().parent.parent / "src" / "uxon"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name == "execution.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            values = [elt.value for elt in node.elts if isinstance(elt, ast.Constant)]
            if values and values[0] == "sudo" and any(v in {"-iu", "-niu"} for v in values):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, (
        "target-user sudo argv must live only in infra/execution.py: " + ", ".join(offenders)
    )


def test_command_backend_can_own_a_tmux_server_inside_existing_netns() -> None:
    """Privileged opt-in proof that the tmux server, not only agent argv, enters netns."""
    namespace = os.environ.get("UXON_TEST_NETNS", "")
    if not namespace or os.geteuid() != 0 or shutil.which("ip") is None:
        pytest.skip("set UXON_TEST_NETNS and run as root to exercise an existing network namespace")
    listed = subprocess.run(
        ["ip", "netns", "list"], capture_output=True, text=True, check=False
    ).stdout.split()
    if namespace not in listed:
        pytest.skip(f"network namespace {namespace!r} does not exist")
    user = getpass.getuser()
    cfg = _command_cfg(
        backend_id="netns",
        command_prefix=("ip", "netns", "exec", namespace),
    )
    socket = f"/tmp/uxon-netns-contract-{os.getpid()}.sock"
    base = tmux.tmux_base(cfg, user, socket)
    name = f"uxon-netns-contract-{os.getpid()}"
    try:
        subprocess.run(base + ["new-session", "-d", "-s", name, "sleep", "30"], check=True)
        out = subprocess.run(
            base + ["list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert name in out.splitlines()
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True, check=False)
