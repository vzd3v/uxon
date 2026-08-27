"""Contract tests for the v4 target-user execution boundary."""

from __future__ import annotations

import ast
import getpass
import json
import os
import shutil
import socket as unix_socket
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from helpers import make_config, make_session

from uxon.app.doctor import _doctor_execution_rows
from uxon.app.launch_profile import resolve_launch_profile
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
    tmux_socket,
)
from uxon.infra.execution import (
    DirectoryEntry,
    ExecutionProbe,
    FilesystemUsage,
    canonicalize_path,
    filesystem_usage,
    list_directories,
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


def test_helper_ignoring_target_user_fails_before_path_or_launch_side_effects() -> None:
    """The fixed identity probe is the first launch boundary operation."""
    user = getpass.getuser()
    cfg = _command_cfg()
    wrong_identity = json.dumps({"euid": 0, "egid": 0, "groups": [0]})
    canonicalize = mock.Mock(side_effect=AssertionError("path probe must not run"))

    def require_real_probe(probe_cfg, probe_user):
        result = probe(probe_cfg, probe_user)
        if not result.ok:
            from uxon.errors import fail

            fail(result.error)
        return result

    with (
        mock.patch("uxon.infra.execution.run_query", return_value=_cp(stdout=wrong_identity)),
        mock.patch("uxon.infra.execution.require_probe", side_effect=require_real_probe),
        mock.patch("uxon.infra.execution.canonicalize_path", canonicalize),
        pytest.raises(SystemExit),
    ):
        resolve_launch_profile(cfg, user, "claude", "/srv/demo", None)
    canonicalize.assert_not_called()


def _managed_request_for_race() -> tuple[tmux.LaunchRequest, launch_records.PendingLaunchRecord]:
    pending = launch_records.PendingLaunchRecord(
        socket_path="/tmp/uxon-race.sock",
        session_name="uxon-race@claude",
        launch_nonce="owned-nonce",
        launch_profile="claude",
        agent="claude",
        launch_user=getpass.getuser(),
    )
    managed = tmux.ManagedTmuxLaunch(
        create_cmd=("tmux", "new-session"),
        query_cmd=("tmux", "display-message"),
        release_cmd=("tmux", "wait-for"),
        rollback_kill_prefix=("tmux", "kill-session", "-t"),
        record_socket=pending.socket_path,
        record_session=pending.session_name,
        record_nonce=pending.launch_nonce,
        record_dir="/tmp/uxon-race-records",
        launch_profile="claude",
        agent="claude",
        launch_user=pending.launch_user,
    )
    return tmux.LaunchRequest(cmd=("tmux", "attach"), managed=managed), pending


def test_concurrent_create_loser_never_kills_preexisting_session() -> None:
    request, pending = _managed_request_for_race()
    calls: list[tuple[str, ...]] = []

    def run(cmd, **_kwargs):
        calls.append(tuple(cmd))
        return _cp(returncode=1)

    with (
        mock.patch("uxon.infra.launch_records.create_pending_record"),
        mock.patch("uxon.infra.launch_records.fail_pending_record"),
        mock.patch("uxon.infra.process.run_cmd", side_effect=run),
        pytest.raises(SystemExit),
    ):
        tmux.prepare_managed_launch(request, pending)
    assert request.managed is not None
    assert not any(call[:3] == request.managed.rollback_kill_prefix for call in calls)
    assert request.managed.query_cmd not in calls


def test_post_create_cleanup_does_not_kill_nonce_mismatch() -> None:
    request, pending = _managed_request_for_race()
    assert request.managed is not None
    owned = "$1\t100\tuxon-race@claude\towned-nonce\n"
    winner = "$2\t101\tuxon-race@claude\twinner-nonce\n"
    responses = iter((_cp(), _cp(stdout=owned), _cp(stdout=winner)))
    calls: list[tuple[str, ...]] = []

    def run(cmd, **_kwargs):
        calls.append(tuple(cmd))
        return next(responses)

    with (
        mock.patch("uxon.infra.launch_records.create_pending_record"),
        mock.patch(
            "uxon.infra.launch_records.finalize_pending_record",
            side_effect=RuntimeError("record failed"),
        ),
        mock.patch("uxon.infra.launch_records.fail_pending_record"),
        mock.patch("uxon.infra.process.run_cmd", side_effect=run),
        pytest.raises(RuntimeError),
    ):
        tmux.prepare_managed_launch(request, pending)
    assert not any(call[:3] == request.managed.rollback_kill_prefix for call in calls)


def test_post_create_cleanup_targets_immutable_session_id_not_reused_name() -> None:
    request, pending = _managed_request_for_race()
    assert request.managed is not None
    owned = "$1\t100\tuxon-race@claude\towned-nonce\n"
    responses = iter((_cp(), _cp(stdout=owned), _cp(stdout=owned), _cp()))
    calls: list[tuple[str, ...]] = []

    def run(cmd, **_kwargs):
        calls.append(tuple(cmd))
        return next(responses)

    with (
        mock.patch("uxon.infra.launch_records.create_pending_record"),
        mock.patch(
            "uxon.infra.launch_records.finalize_pending_record",
            side_effect=RuntimeError("record failed"),
        ),
        mock.patch("uxon.infra.launch_records.fail_pending_record"),
        mock.patch("uxon.infra.process.run_cmd", side_effect=run),
        pytest.raises(RuntimeError),
    ):
        tmux.prepare_managed_launch(request, pending)

    rollback = (*request.managed.rollback_kill_prefix, "$1")
    assert rollback in calls
    assert (*request.managed.rollback_kill_prefix, pending.session_name) not in calls


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
    assert run.call_args.args[0][:7] == [
        "/usr/bin/sudo",
        "-n",
        "-H",
        "-u",
        "alice",
        "--",
        "sh",
    ]
    assert "-i" not in run.call_args.args[0]


@pytest.mark.parametrize("failure", ("timeout", "nonzero"))
def test_process_failures_never_echo_workload_argv_or_output(failure: str) -> None:
    secret = "api-key-super-secret"
    command = ["agent", "--api-key", secret, "private prompt"]
    if failure == "timeout":
        effect = subprocess.TimeoutExpired(command, 1.0)
        context = mock.patch("uxon.infra.process.run_query", side_effect=effect)
    else:
        context = mock.patch(
            "uxon.infra.process.run_query",
            return_value=subprocess.CompletedProcess(command, 7, "", secret),
        )
    with context, pytest.raises(SystemExit) as raised:
        from uxon.infra.process import run_cmd

        run_cmd(command, timeout=1.0)
    message = str(getattr(raised.value, "uxon_msg", ""))
    assert secret not in message
    assert "private prompt" not in message


@pytest.mark.parametrize("token", ("{user.__class__}", "{user!r}", "{user:>5}", "{"))
def test_execution_template_rejects_nonliteral_user_expansion(token: str) -> None:
    cfg = _command_cfg(command_prefix=("boundary", token))
    with pytest.raises(SystemExit):
        validate_execution_config(cfg.execution)


def test_backend_probe_verifies_target_uid_gid_and_groups() -> None:
    cfg = _command_cfg()
    with (
        mock.patch(
            "uxon.infra.execution.pwd.getpwnam",
            return_value=SimpleNamespace(pw_uid=1001, pw_gid=1001),
        ),
        mock.patch(
            "uxon.infra.execution.run_query",
            return_value=_cp(
                stdout=json.dumps({"euid": 1001, "egid": 1001, "groups": [1001, 2000]})
            ),
        ) as run,
        mock.patch("uxon.infra.execution.os.getgrouplist", return_value=[2000, 1001]),
    ):
        result = probe(cfg, "alice")
    assert result.ok
    assert run.call_args.args[0][:4] == [
        "/usr/local/libexec/fake-boundary",
        "alice",
        "--",
        mock.ANY,
    ]


def test_backend_probe_rejects_wrong_supplementary_groups() -> None:
    cfg = _command_cfg()
    with (
        mock.patch(
            "uxon.infra.execution.pwd.getpwnam",
            return_value=SimpleNamespace(pw_uid=1001, pw_gid=1001),
        ),
        mock.patch("uxon.infra.execution.os.getgrouplist", return_value=[1001, 2000]),
        mock.patch(
            "uxon.infra.execution.run_query",
            return_value=_cp(stdout=json.dumps({"euid": 1001, "egid": 1001, "groups": [1001]})),
        ),
    ):
        result = probe(cfg, "alice")
    assert not result.ok
    assert "did not enter target user" in result.error


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


def test_command_backend_lists_target_directories_inside_boundary() -> None:
    cfg = _command_cfg()
    payload = {
        "ok": True,
        "entries": [{"name": "demo", "mtime": 123}],
        "error": "",
    }
    with mock.patch(
        "uxon.infra.execution.run_query", return_value=_cp(stdout=json.dumps(payload))
    ) as run:
        assert list_directories(cfg, "alice", "/inside/projects") == (
            DirectoryEntry(name="demo", mtime=123),
        )
    argv = run.call_args.args[0]
    assert argv[:3] == ["/usr/local/libexec/fake-boundary", "alice", "--"]
    assert argv[-4:] == ["--mode", "list-directories", "--path", "/inside/projects"]


def test_command_backend_reads_target_filesystem_usage_inside_boundary() -> None:
    cfg = _command_cfg()
    payload = {"ok": True, "total": 4096, "available": 1024, "error": ""}
    with mock.patch(
        "uxon.infra.execution.run_query", return_value=_cp(stdout=json.dumps(payload))
    ) as run:
        assert filesystem_usage(cfg, "alice", "/inside/projects") == FilesystemUsage(
            total=4096, available=1024
        )
    argv = run.call_args.args[0]
    assert argv[:3] == ["/usr/local/libexec/fake-boundary", "alice", "--"]
    assert argv[-4:] == ["--mode", "filesystem-usage", "--path", "/inside/projects"]


def test_local_intended_path_rejects_symlink_component(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(SystemExit):
        canonicalize_path(make_config(), "alice", str(link / "new"), intended=True)

    broken = tmp_path / "broken"
    broken.symlink_to("missing-target")
    with pytest.raises(SystemExit):
        canonicalize_path(make_config(), "alice", str(broken), intended=True)

    with pytest.raises(SystemExit):
        canonicalize_path(
            make_config(), "alice", str(tmp_path / "missing" / ".." / "new"), intended=True
        )


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

    with (
        mock.patch(
            "uxon.infra.tmux.run_query",
            return_value=_cp(stdout=json.dumps({"state": "absent", "sessions": [], "error": ""})),
        ) as run,
        mock.patch("uxon.infra.sessions_probe.garbage_collect_records"),
    ):
        assert sessions_probe.collect_sessions_for_user(cfg, "alice", "uxon-", socket) == []
    assert run.call_args.args[0][: len(prefix)] == prefix
    assert run.call_args.args[0][-2:] == ["--socket", socket]

    kill = tmux.tmux_base(cfg, "alice", socket, nonint=True) + [
        "kill-session",
        "-t",
        session.name,
    ]
    assert kill[: len(prefix)] == prefix


def test_nested_controller_tmux_cannot_cross_a_command_boundary() -> None:
    cfg = _command_cfg()
    with (
        mock.patch("uxon.infra.tmux.tmux_host_socket", return_value="/tmp/controller.sock"),
        pytest.raises(SystemExit),
    ):
        tmux.tmux_nesting_mode(cfg, "alice", "/tmp/controller.sock")


def test_runtime_telemetry_uses_the_execution_boundary() -> None:
    from uxon.infra.runtime_telemetry import read_cgroup_members

    cfg = _command_cfg()
    payload = {"ok": True, "pids": [101, 102], "error": ""}
    with mock.patch(
        "uxon.infra.runtime_telemetry.run_query",
        return_value=_cp(stdout=json.dumps(payload)),
    ) as run:
        assert read_cgroup_members(cfg, "alice", "/demo.scope") == [101, 102]
    assert run.call_args.args[0][:3] == [
        "/usr/local/libexec/fake-boundary",
        "alice",
        "--",
    ]


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


def test_live_empty_tmux_server_is_preserved_in_session_snapshot() -> None:
    cfg = _command_cfg()
    socket = "/tmp/tmux-1001/uxon-boundary.sock"
    with (
        mock.patch(
            "uxon.infra.sessions_probe.tmux.probe_tmux_server",
            return_value=tmux.TmuxServerProbe("running", sessions=()),
        ),
        mock.patch(
            "uxon.infra.sessions_probe.tmux.tmux_base",
            return_value=["tmux", "-S", socket],
        ),
        mock.patch("uxon.infra.sessions_probe.run_cmd", return_value=_cp()),
        mock.patch("uxon.infra.sessions_probe.garbage_collect_records"),
    ):
        snapshot = sessions_probe.collect_session_snapshot_for_user(cfg, "alice", "uxon-", socket)
    assert snapshot.server_state == "running"
    assert snapshot.sessions == ()


def test_fixed_tmux_probe_distinguishes_absent_and_non_socket(tmp_path: Path) -> None:
    absent = tmux_server_probe.collect(tmp_path / "missing.sock")
    assert absent == {"state": "absent", "sessions": [], "error": ""}

    regular = tmp_path / "not-a-socket"
    regular.write_text("not tmux")
    unreachable = tmux_server_probe.collect(regular)
    assert unreachable["state"] == "unreachable"
    assert unreachable["sessions"] == []
    assert "not a socket" in unreachable["error"]


def test_fixed_tmux_probe_rejects_non_private_parent(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    result = tmux_server_probe.collect(tmp_path / "tmux.sock")
    assert result["state"] == "unreachable"
    assert "permissions must be 0700" in result["error"]


def test_fixed_tmux_probe_treats_closed_socket_as_absent(tmp_path: Path) -> None:
    socket_path = tmp_path / "tmux.sock"
    listener = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o600)
    listener.close()

    assert tmux_server_probe.collect(socket_path) == {
        "state": "absent",
        "sessions": [],
        "error": "",
    }


def test_fixed_tmux_probe_keeps_live_foreign_listener_unreachable(tmp_path: Path) -> None:
    socket_path = tmp_path / "tmux.sock"
    listener = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o600)
    listener.listen(1)
    try:
        with mock.patch(
            "uxon.infra.tmux_server_probe.run_query",
            return_value=_cp(returncode=1),
        ):
            result = tmux_server_probe.collect(socket_path)
    finally:
        listener.close()
    assert result["state"] == "unreachable"


def test_fixed_tmux_probe_accepts_owner_only_execute_bits(tmp_path: Path) -> None:
    socket_path = tmp_path / "tmux.sock"
    listener = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o700)
    listener.listen(1)
    try:
        with mock.patch(
            "uxon.infra.tmux_server_probe.run_query",
            return_value=_cp(stdout="session\n"),
        ):
            result = tmux_server_probe.collect(socket_path)
    finally:
        listener.close()
    assert result == {"state": "running", "sessions": ["session"], "error": ""}


def test_fixed_tmux_probe_rejects_group_socket_access(tmp_path: Path) -> None:
    socket_path = tmp_path / "tmux.sock"
    listener = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o660)
    try:
        result = tmux_server_probe.collect(socket_path)
    finally:
        listener.close()
    assert result["state"] == "unreachable"
    assert "owner-only" in result["error"]


def test_fixed_tmux_probe_does_not_accept_replaced_stale_inode(tmp_path: Path) -> None:
    socket_path = tmp_path / "tmux.sock"
    first = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
    first.bind(str(socket_path))
    socket_path.chmod(0o600)
    first.close()
    replacements: list[unix_socket.socket] = []

    def replace_during_query(*_args, **_kwargs):
        socket_path.unlink()
        replacement = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
        replacement.bind(str(socket_path))
        socket_path.chmod(0o600)
        replacement.listen(1)
        replacements.append(replacement)
        return _cp(returncode=1)

    try:
        with mock.patch("uxon.infra.tmux_server_probe.run_query", side_effect=replace_during_query):
            result = tmux_server_probe.collect(socket_path)
    finally:
        for replacement in replacements:
            replacement.close()
    assert result["state"] == "unreachable"


def test_tmux_socket_parent_is_created_private_and_rejects_symlinks(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    socket_path = parent / "tmux.sock"
    tmux_socket.prepare_socket_parent(socket_path)
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700

    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(tmux_socket.SocketDirectoryError):
        tmux_socket.prepare_socket_parent(linked / "tmux.sock")

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    private = unsafe / "private"
    private.mkdir(mode=0o700)
    with pytest.raises(tmux_socket.SocketDirectoryError):
        tmux_socket.prepare_socket_parent(private / "tmux.sock")


def test_git_fs_binary_and_runtime_commands_are_nested_under_boundary(tmp_path: Path) -> None:
    cfg = _command_cfg()
    prefix = ["/usr/local/libexec/fake-boundary", "alice", "--"]

    with (
        mock.patch("uxon.infra.git.run_query", return_value=_cp(stdout="/srv/repo\n")) as run,
        mock.patch(
            "uxon.infra.execution.run_query",
            return_value=_cp(stdout=json.dumps({"ok": True, "path": "/srv/repo", "error": ""})),
        ) as path_run,
    ):
        assert git.git_repo_root_nonint_as_user(cfg, "/srv/repo/sub", "alice") == "/srv/repo"
    assert run.call_args.args[0][: len(prefix)] == prefix
    assert "git" in run.call_args.args[0]
    assert path_run.call_args.args[0][: len(prefix)] == prefix

    with (
        mock.patch(
            "uxon.infra.execution.run_query",
            return_value=_cp(
                stdout=json.dumps(
                    {
                        "ok": True,
                        "path": str(tmp_path),
                        "exists": True,
                        "directory": True,
                        "writable": True,
                        "nearest_existing_ancestor": str(tmp_path),
                        "error": "",
                    }
                )
            ),
        ) as run,
    ):
        assert identity.probe_cwd_writable(cfg, "alice", str(tmp_path))
    assert run.call_args.args[0][: len(prefix)] == prefix
    assert run.call_args.args[0][-2:] == ["--path", str(tmp_path)]

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
        )
    assert request.managed is not None
    create = list(request.managed.create_cmd)
    boundary = ["/usr/local/libexec/fake-boundary", "alice", "--"]
    assert create[: len(boundary)] == boundary
    assert create[len(boundary) : len(boundary) + 3] == ["tmux", "-S", socket]
    for command in (
        request.managed.query_cmd,
        request.managed.release_cmd,
        request.managed.rollback_kill_prefix,
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
def test_tmux_clean_exit_stale_socket_is_absent_and_reusable(tmp_path: Path) -> None:
    socket_path = tmp_path / "tmux.sock"
    base = ["tmux", "-f", "/dev/null", "-S", str(socket_path)]
    try:
        subprocess.run(base + ["new-session", "-d", "-s", "short", "true"], check=True)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = subprocess.run(
                base + ["list-sessions"], capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                break
            time.sleep(0.01)
        else:
            pytest.fail("short tmux server did not exit")

        assert socket_path.is_socket()
        assert tmux_server_probe.collect(socket_path) == {
            "state": "absent",
            "sessions": [],
            "error": "",
        }

        subprocess.run(base + ["new-session", "-d", "-s", "reused", "sleep", "30"], check=True)
        running = tmux_server_probe.collect(socket_path)
        assert running == {"state": "running", "sessions": ["reused"], "error": ""}
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True, check=False)
        socket_path.unlink(missing_ok=True)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_tmux_live_empty_server_remains_running(tmp_path: Path) -> None:
    socket_path = tmp_path / "tmux.sock"
    base = ["tmux", "-f", "/dev/null", "-S", str(socket_path)]
    try:
        subprocess.run(base + ["new-session", "-d", "-s", "temporary", "sleep", "30"], check=True)
        subprocess.run(base + ["set-option", "-s", "exit-empty", "off"], check=True)
        subprocess.run(base + ["kill-session", "-t", "temporary"], check=True)
        assert tmux_server_probe.collect(socket_path) == {
            "state": "running",
            "sessions": [],
            "error": "",
        }
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True, check=False)
        socket_path.unlink(missing_ok=True)


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


def test_shared_launch_record_dir_is_group_readable_and_launch_user_cannot_write(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "records"
    directory.mkdir(mode=0o2770)
    directory.chmod(0o2770)
    real_lstat = os.lstat
    control_gid = os.getegid()

    def root_owned_lstat(path):
        result = real_lstat(path)
        if Path(path) == directory:
            values = list(result)
            values[0] = stat.S_IFDIR | 0o2770
            values[4] = 0
            values[5] = control_gid
            return os.stat_result(values)
        return result

    pending = launch_records.PendingLaunchRecord(
        socket_path="/run/uxon/alice.sock",
        session_name="uxon-demo@claude",
        launch_nonce="abcdefghijklmnop",
        launch_profile="claude",
        agent="claude",
        launch_user="alice",
    )
    with (
        mock.patch("uxon.infra.launch_records.os.lstat", side_effect=root_owned_lstat),
        mock.patch(
            "uxon.infra.launch_records.pwd.getpwnam",
            return_value=SimpleNamespace(pw_gid=2000),
        ),
        mock.patch("uxon.infra.launch_records.os.getgrouplist", return_value=[2000]),
        mock.patch("uxon.infra.launch_records._has_posix_acl", return_value=False),
    ):
        path = launch_records.create_pending_record(pending, override_dir=directory, shared=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_shared_launch_record_dir_rejects_launch_user_in_control_group(tmp_path: Path) -> None:
    directory = tmp_path / "records"
    directory.mkdir(mode=0o2770)
    directory.chmod(0o2770)
    metadata = SimpleNamespace(st_mode=stat.S_IFDIR | 0o2770, st_uid=0, st_gid=2000)
    real_lstat = os.lstat

    def shared_lstat(path):
        return metadata if Path(path) == directory else real_lstat(path)

    pending = launch_records.PendingLaunchRecord(
        socket_path="/run/uxon/alice.sock",
        session_name="uxon-demo@claude",
        launch_nonce="abcdefghijklmnop",
        launch_profile="claude",
        agent="claude",
        launch_user="alice",
    )
    with (
        mock.patch("uxon.infra.launch_records.os.lstat", side_effect=shared_lstat),
        mock.patch("uxon.infra.launch_records.os.geteuid", return_value=0),
        mock.patch(
            "uxon.infra.launch_records.pwd.getpwnam",
            return_value=SimpleNamespace(pw_gid=2000),
        ),
        mock.patch("uxon.infra.launch_records.os.getgrouplist", return_value=[2000]),
        mock.patch("uxon.infra.launch_records._has_posix_acl", return_value=False),
        pytest.raises(SystemExit, match="2"),
    ):
        launch_records.create_pending_record(pending, override_dir=directory, shared=True)


def test_launch_record_gc_covers_same_user_retired_sockets(tmp_path: Path) -> None:
    records: list[tuple[launch_records.PendingLaunchRecord, Path]] = []
    for socket, nonce in (
        ("/run/uxon/alice.sock", "abcdefghijklmnop"),
        ("/run/uxon/bob.sock", "ponmlkjihgfedcba"),
    ):
        pending = launch_records.PendingLaunchRecord(
            socket_path=socket,
            session_name="uxon-demo@claude",
            launch_nonce=nonce,
            launch_profile="claude",
            agent="claude",
            launch_user=getpass.getuser(),
        )
        launch_records.create_pending_record(pending, override_dir=tmp_path)
        path = launch_records.finalize_pending_record(
            pending,
            launch_records.TmuxSessionMetadata(
                session_id=f"${len(records) + 1}",
                created="1",
                name=pending.session_name,
                launch_nonce=nonce,
            ),
            override_dir=tmp_path,
        )
        records.append((pending, path))

    removed = launch_records.garbage_collect_records(
        set(),
        override_dir=tmp_path,
        launch_user=getpass.getuser(),
        now=10**12,
    )
    assert removed == 2
    assert not records[0][1].exists()
    assert not records[1][1].exists()


def test_launch_record_gc_never_deletes_another_users_live_record(tmp_path: Path) -> None:
    paths: dict[str, Path] = {}
    for user, nonce in (("alice", "abcdefghijklmnop"), ("bob", "ponmlkjihgfedcba")):
        pending = launch_records.PendingLaunchRecord(
            socket_path=f"/run/uxon/{user}.sock",
            session_name="uxon-demo@claude",
            launch_nonce=nonce,
            launch_profile="claude",
            agent="claude",
            launch_user=user,
        )
        launch_records.create_pending_record(pending, override_dir=tmp_path)
        paths[user] = launch_records.finalize_pending_record(
            pending,
            launch_records.TmuxSessionMetadata(
                session_id=f"${len(paths) + 1}",
                created="1",
                name=pending.session_name,
                launch_nonce=nonce,
            ),
            override_dir=tmp_path,
        )

    removed = launch_records.garbage_collect_records(
        set(), override_dir=tmp_path, launch_user="alice", now=10**12
    )
    assert removed == 1
    assert not paths["alice"].exists()
    assert paths["bob"].exists()


@pytest.mark.parametrize("leading_kind", ("other-user", "young"))
def test_launch_record_gc_cursor_reaches_stale_record_after_full_batch(
    tmp_path: Path, leading_kind: str
) -> None:
    now = 10**12
    for index in range(1024):
        payload = {
            "status": "finalized",
            "launch_user": "bob" if leading_kind == "other-user" else "alice",
            "socket_path": "/run/uxon/current.sock",
            "session_name": f"uxon-live-{index}@claude",
            "launch_nonce": f"nonce-{index:04d}",
            "finalized_at": 1 if leading_kind == "other-user" else now,
        }
        path = tmp_path / f"{index:04d}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
    stale = tmp_path / "zzzz.json"
    stale.write_text(
        json.dumps(
            {
                "status": "finalized",
                "launch_user": "alice",
                "socket_path": "/run/uxon/retired.sock",
                "session_name": "uxon-stale@claude",
                "launch_nonce": "stale-nonce",
                "finalized_at": 1,
            }
        ),
        encoding="utf-8",
    )
    stale.chmod(0o600)

    first = launch_records.garbage_collect_records(
        set(), override_dir=tmp_path, launch_user="alice", now=now
    )
    assert first == 0
    assert stale.exists()
    second = launch_records.garbage_collect_records(
        set(), override_dir=tmp_path, launch_user="alice", now=now
    )
    assert second == 1
    assert not stale.exists()


def test_launch_record_gc_refuses_unsafe_cursor_symlink(tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    record.write_text(
        json.dumps(
            {
                "status": "finalized",
                "launch_user": "alice",
                "socket_path": "/run/uxon/alice.sock",
                "session_name": "uxon-demo@claude",
                "launch_nonce": "abcdefghijklmnop",
                "finalized_at": 1,
            }
        ),
        encoding="utf-8",
    )
    record.chmod(0o600)
    victim = tmp_path / "victim"
    victim.write_text("do not replace", encoding="utf-8")
    cursor = launch_records._gc_cursor_path(tmp_path, "alice")
    cursor.symlink_to(victim)

    removed = launch_records.garbage_collect_records(
        set(), override_dir=tmp_path, launch_user="alice", now=10**12
    )
    assert removed == 0
    assert record.exists()
    assert cursor.is_symlink()
    assert victim.read_text(encoding="utf-8") == "do not replace"
