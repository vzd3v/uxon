"""Contract tests for the v4 target-user execution boundary."""

from __future__ import annotations

import ast
import getpass
import json
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
from uxon.infra import (
    agents,
    git,
    identity,
    launch_records,
    probes,
    runtime,
    sessions_probe,
    tmux,
    tmux_server_probe,
)
from uxon.infra.execution import (
    ExecutionProbe,
    canonicalize_path,
    probe,
    resolve_target,
    wrap_command,
)


def _command_cfg(
    *,
    backend_id: str = "boundary",
    command_prefix: tuple[str, ...] = ("/usr/local/libexec/fake-boundary", "{user}", "--"),
):
    backend = ExecutionBackendSpec(
        id=backend_id,
        kind="command",
        command_prefix=command_prefix,
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
    expected = ["/usr/local/libexec/fake-boundary", "alice", "--", "true"]
    assert wrap_command(cfg, "alice", ["true"], interactive=True) == expected
    assert wrap_command(cfg, "alice", ["true"], interactive=False) == expected


def test_local_binary_probe_uses_argv_safe_nonlogin_sudo() -> None:
    cfg = make_config()
    with (
        mock.patch("uxon.infra.execution.process_user", return_value="operator"),
        mock.patch(
            "uxon.infra.probes.run_query",
            return_value=_cp(stdout="tmux\t/usr/bin/tmux\n"),
        ) as run,
    ):
        assert probes._resolve_paths_remote(cfg, ["tmux"], "alice")["tmux"] == "/usr/bin/tmux"
    assert run.call_args.args[0][:7] == ["sudo", "-n", "-H", "-u", "alice", "--", "sh"]
    assert "-i" not in run.call_args.args[0]


@pytest.mark.parametrize("token", ("{user.__class__}", "{user!r}", "{user:>5}", "{"))
def test_execution_template_rejects_nonliteral_user_expansion(token: str) -> None:
    cfg = _command_cfg(command_prefix=("boundary", token))
    with pytest.raises(SystemExit):
        validate_execution_config(cfg.execution)


def test_backend_probe_verifies_target_uid_and_gid() -> None:
    cfg = _command_cfg()
    with (
        mock.patch(
            "uxon.infra.execution.pwd.getpwnam",
            return_value=SimpleNamespace(pw_uid=1001, pw_gid=1001),
        ),
        mock.patch(
            "uxon.infra.execution.run_query",
            return_value=_cp(stdout=json.dumps({"euid": 1001, "egid": 1001})),
        ) as run,
    ):
        result = probe(cfg, "alice")
    assert result.ok
    assert run.call_args.args[0][:4] == [
        "/usr/local/libexec/fake-boundary",
        "alice",
        "--",
        mock.ANY,
    ]


def test_command_backend_returns_authoritative_canonical_path() -> None:
    cfg = _command_cfg()
    payload = {"ok": True, "path": "/inside/projects/demo", "error": ""}
    with mock.patch(
        "uxon.infra.execution.run_query", return_value=_cp(stdout=json.dumps(payload))
    ) as run:
        result = canonicalize_path(cfg, "alice", "/outside/projects/demo", intended=False)
    assert result == "/inside/projects/demo"
    argv = run.call_args.args[0]
    assert argv[:3] == ["/usr/local/libexec/fake-boundary", "alice", "--"]
    assert argv[-2:] == ["--path", "/outside/projects/demo"]


def test_local_intended_path_rejects_symlink_component(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(SystemExit):
        canonicalize_path(make_config(), "alice", str(link / "new"), intended=True)


def test_doctor_reports_fixed_backend_probe() -> None:
    cfg = _command_cfg()
    profile = cfg.launch.profiles["claude"]
    cfg.launch.profiles["claude"] = LaunchProfile(
        id=profile.id,
        agent=profile.agent,
        launch_user="alice",
    )
    with mock.patch(
        "uxon.app.doctor.execution_infra.probe",
        return_value=ExecutionProbe(backend="boundary", ok=True),
    ):
        rows = _doctor_execution_rows(cfg, "operator")
    assert rows[0]["backend"] == "boundary"
    assert rows[0]["status"] == "ok"
    assert "change_policy" not in rows[0]


def test_tmux_server_list_attach_and_kill_share_the_boundary() -> None:
    cfg = _command_cfg()
    prefix = ["/usr/local/libexec/fake-boundary", "alice", "--"]
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

    with mock.patch(
        "uxon.infra.tmux.run_query",
        return_value=_cp(stdout=json.dumps({"state": "absent", "sessions": [], "error": ""})),
    ) as run:
        assert sessions_probe.collect_sessions_for_user(cfg, "alice", "uxon-", socket) == []
    assert run.call_args.args[0][: len(prefix)] == prefix
    assert run.call_args.args[0][-2:] == ["--socket", socket]

    kill = tmux.tmux_base(cfg, "alice", socket, nonint=True) + [
        "kill-session",
        "-t",
        session.name,
    ]
    assert kill[: len(prefix)] == prefix


def test_unreachable_tmux_server_is_not_reported_as_empty() -> None:
    cfg = _command_cfg()
    socket = "/tmp/uxon-alice-boundary.sock"
    payload = {"state": "unreachable", "sessions": [], "error": "permission denied"}
    with (
        mock.patch("uxon.infra.tmux.run_query", return_value=_cp(stdout=json.dumps(payload))),
        pytest.raises(SystemExit) as raised,
    ):
        sessions_probe.collect_sessions_for_user(cfg, "alice", "uxon-", socket)
    assert "permission denied" in getattr(raised.value, "uxon_msg", "")


def test_fixed_tmux_probe_distinguishes_absent_and_non_socket(tmp_path: Path) -> None:
    absent = tmux_server_probe.collect(tmp_path / "missing.sock")
    assert absent == {"state": "absent", "sessions": [], "error": ""}

    regular = tmp_path / "not-a-socket"
    regular.write_text("not tmux")
    unreachable = tmux_server_probe.collect(regular)
    assert unreachable["state"] == "unreachable"
    assert unreachable["sessions"] == []
    assert "not a socket" in unreachable["error"]


def test_fixed_tmux_probe_preserves_lstat_error() -> None:
    with mock.patch(
        "uxon.infra.tmux_server_probe.os.stat",
        side_effect=PermissionError("permission denied"),
    ):
        result = tmux_server_probe.collect(Path("/private/tmux.sock"))
    assert result["state"] == "unreachable"
    assert "permission denied" in result["error"]


def test_git_fs_binary_and_runtime_commands_are_nested_under_boundary(tmp_path: Path) -> None:
    cfg = _command_cfg()
    prefix = ["/usr/local/libexec/fake-boundary", "alice", "--"]

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
    boundary = ["/usr/local/libexec/fake-boundary", "alice", "--"]
    assert create[: len(boundary)] == boundary
    assert create[len(boundary) : len(boundary) + 3] == ["tmux", "-S", socket]
    for command in (
        request.managed.query_cmd,
        request.managed.release_cmd,
        request.managed.kill_cmd,
    ):
        assert list(command[: len(boundary)]) == boundary
    runtime_start = create.index("docker")
    assert create.index("uxon.infra.launch_bootstrap") < runtime_start
    assert create[runtime_start : runtime_start + 5] == [
        "docker",
        "exec",
        "-w",
        "/work/demo",
        "box-demo",
    ]
    assert create[runtime_start + 5] == "sh"
    assert "claude" in create[runtime_start + 6 :]


def test_same_backend_id_keeps_socket_address_stable() -> None:
    old_cfg = _command_cfg(command_prefix=("/usr/local/libexec/old-boundary", "{user}", "--"))
    new_cfg = _command_cfg(command_prefix=("/usr/local/libexec/new-boundary", "{user}", "--"))

    with mock.patch("uxon.infra.tmux.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=1001)):
        assert tmux.tmux_socket_path(old_cfg, "alice") == tmux.tmux_socket_path(new_cfg, "alice")


def test_backend_id_change_selects_a_distinct_socket() -> None:
    first = _command_cfg(backend_id="netns_a")
    second = _command_cfg(backend_id="netns_b")
    with mock.patch("uxon.infra.tmux.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=1001)):
        assert tmux.tmux_socket_path(first, "alice") != tmux.tmux_socket_path(second, "alice")


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_tmux_release_channel_is_sticky_and_nonce_scoped(tmp_path: Path) -> None:
    socket = tmp_path / "tmux.sock"
    session = "uxon-handshake-contract"
    nonce = "abcdefghijklmnop"
    release = launch_records.handshake_channel(nonce, "release")
    other = launch_records.handshake_channel("ponmlkjihgfedcba", "release")
    base = ["tmux", "-S", str(socket)]
    try:
        subprocess.run(base + ["new-session", "-d", "-s", session, "sleep", "30"], check=True)
        subprocess.run(base + ["wait-for", "-S", release], check=True)
        subprocess.run(base + ["wait-for", release], check=True, timeout=2)
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run(base + ["wait-for", other], check=True, timeout=0.1)
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True, check=False)


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
    helper = os.environ.get("UXON_TEST_EXEC_HELPER", "")
    if not namespace or not helper or os.geteuid() != 0 or shutil.which("ip") is None:
        pytest.skip(
            "set UXON_TEST_NETNS and UXON_TEST_EXEC_HELPER, then run as root to exercise "
            "an existing network namespace"
        )
    listed = subprocess.run(
        ["ip", "netns", "list"], capture_output=True, text=True, check=False
    ).stdout.split()
    if namespace not in listed:
        pytest.skip(f"network namespace {namespace!r} does not exist")
    user = getpass.getuser()
    cfg = _command_cfg(
        backend_id="netns",
        command_prefix=(helper, "{user}", "--"),
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
