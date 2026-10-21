"""Contract tests for the v4 target-user execution boundary."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import socket as unix_socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from helpers import make_config

from uxon.app.launch_profile import resolve_launch_profile
from uxon.app.project_browsing import browse_project_directories
from uxon.domain.args import ParsedArgs
from uxon.domain.execution import (
    ExecutionBackendSpec,
    ExecutionConfig,
    validate_execution_config,
)
from uxon.domain.launch_profiles import LaunchProfile
from uxon.domain.runtime import WorkloadRuntimeSpec
from uxon.infra import (
    git,
    identity,
    launch_records,
    probes,
    sessions_probe,
    tmux,
    tmux_server_probe,
    tmux_socket,
)
from uxon.infra.execution import (
    DirectoryEntry,
    FilesystemUsage,
    canonicalize_path,
    filesystem_usage,
    list_directories,
    path_facts,
    probe,
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


def _cp(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.mark.usefixtures("real_execution", "real_host_probes")
def test_attestation_rejects_each_identity_fault_before_filesystem_or_launch(subtests):
    cfg = _command_cfg()
    valid = {"euid": 1001, "egid": 1002, "groups": [1002, 2000]}
    cases = {
        "valid": valid,
        "uid": dict(valid, euid=1003),
        "gid": dict(valid, egid=1003),
        "groups": dict(valid, groups=[1002]),
        "bool_uid": dict(valid, euid=True),
        "bool_gid": dict(valid, egid=True),
        "bool_group": dict(valid, groups=[True]),
        "malformed": "{",
        "denied": None,
        "timeout": subprocess.TimeoutExpired("boundary", 1.25),
        "unreachable": OSError("boundary unreachable"),
    }
    for label, payload in cases.items():
        with subtests.test(label=label):
            calls = []

            def run(argv, *, timeout, calls=calls, payload=payload):
                calls.append(argv)
                assert argv[:3] == ["/usr/local/libexec/fake-boundary", "alice", "--"]
                assert argv[-2:] == ["-m", "uxon.infra.execution_probe"]
                assert timeout == 1.25
                if isinstance(payload, Exception):
                    raise payload
                return _cp(
                    returncode=int(payload is None),
                    stdout=payload if isinstance(payload, str) else json.dumps(payload),
                    stderr="permission denied" if payload is None else "",
                )

            with (
                mock.patch(
                    "uxon.infra.execution.pwd.getpwnam",
                    return_value=SimpleNamespace(pw_uid=1001, pw_gid=1002),
                ),
                mock.patch("uxon.infra.execution.os.getgrouplist", return_value=[2000, 1002]),
                mock.patch("uxon.infra.execution.run_query", side_effect=run),
            ):
                if label == "valid":
                    assert probe(cfg, "alice").ok
                else:
                    with pytest.raises(SystemExit) as error:
                        resolve_launch_profile(cfg, "alice", "claude", "/srv/demo", None)
                    assert "failed identity attestation" in error.value.uxon_msg
            assert len(calls) == 1


def _managed_request_for_race(directory):
    pending = launch_records.PendingLaunchRecord(
        socket_path="/tmp/uxon-race.sock",
        session_name="uxon-race@claude_box",
        launch_nonce="a" * 32,
        launch_profile="claude_box",
        agent="claude",
        launch_user="alice",
        execution_backend="boundary",
        execution_fingerprint="boundary-fingerprint",
        runtime="box",
        runtime_kind="command",
        runtime_fingerprint="runtime-fingerprint",
        runtime_resource="box-repo",
        runtime_dir="/work/repo",
        project_slug="repo",
    )
    managed = tmux.ManagedTmuxLaunch(
        create_cmd=("tmux", "new-session"),
        query_cmd=("tmux", "display-message"),
        release_cmd=("tmux", "wait-for"),
        rollback_kill_prefix=("tmux", "kill-session", "-t"),
        record_socket=pending.socket_path,
        record_session=pending.session_name,
        record_nonce=pending.launch_nonce,
        record_dir=str(directory),
        launch_profile=pending.launch_profile,
        agent=pending.agent,
        launch_user=pending.launch_user,
        runtime_id="container-id",
        runtime_epoch="epoch-1",
        runtime_cgroup="/box.scope",
    )
    return tmux.LaunchRequest(cmd=("tmux", "attach"), managed=managed), pending


def test_managed_release_reads_fsynced_full_authority_and_rejects_metadata_tampering(tmp_path):
    request, pending = _managed_request_for_race(tmp_path / "records")
    managed = request.managed
    metadata = launch_records.TmuxSessionMetadata(
        "$1", "100", pending.session_name, pending.launch_nonce
    )
    encoded = "\t".join(dataclasses.astuple(metadata)) + "\n"
    real_fsync = os.fsync
    durable = []
    calls = []

    def fsync(fd):
        real_fsync(fd)
        st = os.fstat(fd)
        durable.append(("directory" if stat.S_ISDIR(st.st_mode) else "file", st.st_ino))

    def run(argv, **kwargs):
        command = tuple(argv)
        calls.append(command)
        assert kwargs["timeout"] > 0
        if command == managed.create_cmd:
            payload = json.loads(
                launch_records.record_path(pending, override_dir=tmp_path / "records").read_text()
            )
            assert payload["status"] == "pending"
            return _cp()
        if command == managed.query_cmd:
            return _cp(stdout=encoded)
        assert command == managed.release_cmd
        path = launch_records.record_path(pending, override_dir=tmp_path / "records")
        payload = launch_records.read_verified_record(
            pending.socket_path, metadata, override_dir=path.parent
        )
        assert payload is not None
        assert {
            key: payload[key]
            for key in dataclasses.asdict(pending)
            if key not in {"launch_profile"}
        } == {
            key: value
            for key, value in dataclasses.asdict(pending).items()
            if key != "launch_profile"
        }
        assert payload["profile"] == pending.launch_profile
        assert (
            payload["tmux_session_id"],
            payload["tmux_session_created"],
            payload["runtime_id"],
            payload["runtime_epoch"],
            payload["runtime_cgroup"],
        ) == ("$1", "100", "container-id", "epoch-1", "/box.scope")
        assert ("file", path.stat().st_ino) in durable
        assert ("directory", path.parent.stat().st_ino) in durable
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        return _cp()

    with (
        mock.patch("uxon.infra.process.run_cmd", side_effect=run),
        mock.patch("uxon.infra.launch_records.os.fsync", side_effect=fsync),
        mock.patch("uxon.infra.audit.audit") as audit,
    ):
        tmux.prepare_managed_launch(request, pending)
    assert calls == [managed.create_cmd, managed.query_cmd, managed.release_cmd]
    assert [(call.args[0], call.kwargs.get("outcome", "ok")) for call in audit.call_args_list] == [
        ("session.new", "ok")
    ]
    for changed in (
        dataclasses.replace(metadata, session_id="$2"),
        dataclasses.replace(metadata, created="101"),
        dataclasses.replace(metadata, name="uxon-other@claude_box"),
        dataclasses.replace(metadata, launch_nonce="b" * 32),
    ):
        assert (
            launch_records.read_verified_record(
                pending.socket_path, changed, override_dir=tmp_path / "records"
            )
            is None
        )


def test_managed_launch_failures_remove_pending_without_adopting_or_killing_winner(
    tmp_path, subtests
):
    for label in ("conflict", "partial_create", "owned", "id", "created", "name", "nonce"):
        with subtests.test(label=label):
            directory = tmp_path / label
            request, pending = _managed_request_for_race(directory)
            managed = request.managed
            owned = launch_records.TmuxSessionMetadata(
                "$1", "100", pending.session_name, pending.launch_nonce
            )
            observed = {
                "id": dataclasses.replace(owned, session_id="$2"),
                "created": dataclasses.replace(owned, created="101"),
                "name": dataclasses.replace(owned, name="uxon-winner@claude_box"),
                "nonce": dataclasses.replace(owned, launch_nonce="b" * 32),
            }.get(label, owned)
            winner_path = None
            if label == "conflict":
                winner = dataclasses.replace(pending, launch_nonce="b" * 32)
                observed = dataclasses.replace(
                    owned, session_id="$2", created="101", launch_nonce=winner.launch_nonce
                )
                launch_records.create_pending_record(winner, override_dir=directory)
                winner_path = launch_records.finalize_pending_record(
                    winner,
                    observed,
                    runtime_id="winner-id",
                    runtime_epoch="winner-epoch",
                    override_dir=directory,
                )
                winner_bytes = winner_path.read_bytes()
                winner_inode = winner_path.stat().st_ino
            calls, queries = [], []
            real_fsync = os.fsync
            sync_count = 0

            def fsync(fd, *, real_fsync=real_fsync):
                nonlocal sync_count
                sync_count += 1
                if sync_count == 2:
                    raise OSError("final record fsync failed")
                real_fsync(fd)

            def run(
                argv,
                *,
                calls=calls,
                label=label,
                managed=managed,
                observed=observed,
                owned=owned,
                queries=queries,
                **kwargs,
            ):
                command = tuple(argv)
                calls.append(command)
                assert kwargs["timeout"] > 0
                if command == managed.create_cmd:
                    return _cp(
                        returncode=int(label in {"conflict", "partial_create"}),
                        stderr="create denied",
                    )
                if command == managed.query_cmd:
                    if label == "conflict":
                        queries.append(observed)
                        return _cp(stdout="\t".join(dataclasses.astuple(observed)) + "\n")
                    meta = owned if not queries else observed
                    queries.append(meta)
                    return _cp(stdout="\t".join(dataclasses.astuple(meta)) + "\n")
                assert command == (*managed.rollback_kill_prefix, "$1")
                return _cp()

            with (
                mock.patch("uxon.infra.process.run_cmd", side_effect=run),
                mock.patch("uxon.infra.launch_records.os.fsync", side_effect=fsync),
                mock.patch("uxon.infra.audit.audit") as audit,
                pytest.raises((SystemExit, OSError)),
            ):
                tmux.prepare_managed_launch(request, pending)
            assert not launch_records.record_path(pending, override_dir=directory).exists()
            assert list(directory.iterdir()) == ([winner_path] if winner_path else [])
            if winner_path:
                assert (
                    winner_path.read_bytes() == winner_bytes
                    and winner_path.stat().st_ino == winner_inode
                )
                assert (
                    launch_records.read_verified_record(
                        pending.socket_path, observed, override_dir=directory
                    )["runtime_id"]
                    == "winner-id"
                )
            assert managed.release_cmd not in calls
            rollback = [call for call in calls if call[:3] == managed.rollback_kill_prefix]
            assert rollback == (
                [(*managed.rollback_kill_prefix, "$1")]
                if label in {"partial_create", "owned"}
                else []
            )
            assert [
                (call.args[0], call.kwargs.get("outcome")) for call in audit.call_args_list
            ] == [("session.new", "error")]


def test_bootstrap_release_timeout_and_cleanup_failures_never_execute_workload(subtests):
    from uxon.infra import launch_bootstrap

    nonce = "a" * 32
    channel = launch_records.handshake_channel(nonce, "release")
    base = ["tmux", "-S", "/tmp/owned.sock"]
    for fault in ("timeout", "cleanup", "nonzero", "transport", "released"):
        with subtests.test(fault=fault):
            calls = []

            def respond(argv, *, calls=calls, fault=fault, **kwargs):
                calls.append(list(argv))
                assert kwargs["start_new_session"] and kwargs["stdin"] == subprocess.DEVNULL
                if len(calls) == 1:
                    assert argv == base + ["wait-for", channel] and kwargs["timeout"] == 0.01
                    if fault in {"timeout", "cleanup"}:
                        raise subprocess.TimeoutExpired(argv, 0.01)
                    if fault == "transport":
                        raise OSError("wait transport lost")
                    return _cp(
                        returncode=int(fault == "nonzero"),
                        stderr="wait rejected" if fault == "nonzero" else "",
                    )
                assert kwargs["timeout"] == 5
                assert argv == base + (
                    ["wait-for", "-S", channel]
                    if len(calls) == 2
                    else ["kill-session", "-t", "uxon-repo@claude"]
                )
                if fault == "cleanup":
                    raise OSError("cleanup transport lost")
                return _cp()

            with (
                mock.patch("uxon.infra.run.subprocess.run", side_effect=respond),
                mock.patch.object(
                    launch_bootstrap.os, "execvp", side_effect=RuntimeError("exec reached")
                ) as execute,
            ):
                if fault == "released":
                    with pytest.raises(RuntimeError, match="exec reached"):
                        launch_bootstrap.wait_then_exec(
                            socket_path="/tmp/owned.sock",
                            session_name="uxon-repo@claude",
                            launch_nonce=nonce,
                            agent_argv=["claude", "literal ; arg"],
                            timeout_seconds=0.01,
                        )
                    execute.assert_called_once_with("claude", ["claude", "literal ; arg"])
                    assert len(calls) == 1
                else:
                    assert launch_bootstrap.wait_then_exec(
                        socket_path="/tmp/owned.sock",
                        session_name="uxon-repo@claude",
                        launch_nonce=nonce,
                        agent_argv=["claude"],
                        timeout_seconds=0.01,
                    ) == (124 if fault in {"timeout", "cleanup"} else 1)
                    execute.assert_not_called()
                    assert len(calls) == 3


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
    assert message.strip()
    assert secret not in message
    assert "private prompt" not in message


@pytest.mark.parametrize("token", ("{user.__class__}", "{user!r}", "{user:>5}", "{"))
def test_execution_template_rejects_nonliteral_user_expansion(token: str) -> None:
    cfg = _command_cfg(command_prefix=("boundary", token))
    with pytest.raises(SystemExit):
        validate_execution_config(cfg.execution)


def test_local_intended_path_rejects_symlink_component(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(SystemExit):
        canonicalize_path(make_config(), identity.process_user(), str(link / "new"), intended=True)

    broken = tmp_path / "broken"
    broken.symlink_to("missing-target")
    with pytest.raises(SystemExit):
        canonicalize_path(make_config(), identity.process_user(), str(broken), intended=True)

    with pytest.raises(SystemExit):
        canonicalize_path(
            make_config(),
            identity.process_user(),
            str(tmp_path / "missing" / ".." / "new"),
            intended=True,
        )


def test_nested_controller_tmux_cannot_cross_a_command_boundary() -> None:
    cfg = _command_cfg()
    with (
        mock.patch("uxon.infra.tmux.tmux_host_socket", return_value="/tmp/controller.sock"),
        pytest.raises(SystemExit),
    ):
        tmux.tmux_nesting_mode(cfg, "alice", "/tmp/controller.sock")


def test_denied_proc_marker_read_reports_failure_not_unmanaged():
    from uxon.infra.runtime_telemetry_probe import session_markers

    with mock.patch("pathlib.Path.read_bytes", side_effect=PermissionError("Permission denied")):
        result = session_markers([123])
    assert result["ok"] is False and result["markers"] == {}
    assert "Permission denied" in result["error"]


@pytest.mark.slow
def test_real_other_user_cannot_use_controller_private_directory(tmp_path: Path) -> None:
    import pwd
    import tempfile

    if shutil.which("sudo") is None:
        pytest.skip("sudo is unavailable for the real target-user proof")
    try:
        pwd.getpwnam("nobody")
    except KeyError:
        pytest.skip("the nobody target account is unavailable")
    cp = subprocess.run(
        ["sudo", "-n", "-H", "-u", "nobody", "--", "true"], capture_output=True, timeout=3
    )
    if cp.returncode:
        pytest.skip("passwordless target-user boundary is unavailable")
    # The source checkout may itself be private. Provision the shipped,
    # stdlib-only probe in a disposable public import root, not a host install.
    with tempfile.TemporaryDirectory(prefix="uxon-target-probe-") as public:
        import_root = Path(public)
        import_root.chmod(0o755)
        source = Path(probes.__file__).parent.parent
        (import_root / "uxon" / "infra").mkdir(parents=True)
        (import_root / "uxon").chmod(0o755)
        (import_root / "uxon" / "infra").chmod(0o755)
        for relative in ("__init__.py", "infra/__init__.py", "infra/path_probe.py"):
            destination = import_root / "uxon" / relative
            shutil.copyfile(source / relative, destination)
            destination.chmod(0o644)
        cfg = _command_cfg(
            command_prefix=(
                "/usr/bin/sudo",
                "-n",
                "-H",
                "-u",
                "{user}",
                "--",
                "env",
                f"PYTHONPATH={public}",
                "sh",
                "-c",
                'shift; exec /usr/bin/python3 "$@"',
                "uxon-test-probe",
            )
        )
        # The selected boundary must reach the actual probe before exercising
        # permissions; the controller's virtualenv may be private too.
        public_facts = path_facts(cfg, "nobody", public)
        assert public_facts.exists and public_facts.directory
        assert not public_facts.writable
        assert [
            entry.name for entry in list_directories(cfg, "nobody", public, missing_ok=False)
        ] == ["uxon"]
        private = tmp_path / "private"
        private.mkdir(mode=0o700)
        assert os.access(private, os.W_OK | os.X_OK)
        try:
            facts = path_facts(cfg, "nobody", str(private))
        except SystemExit as exc:
            assert "Permission denied" in getattr(exc, "uxon_msg", "")
        else:
            assert not facts.writable
        with pytest.raises(SystemExit):
            list_directories(cfg, "nobody", str(private), missing_ok=False)


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


def test_socket_address_follows_backend_id_and_optional_fingerprint():
    first = _command_cfg(command_prefix=("old-boundary", "{user}", "--"))
    changed = _command_cfg(command_prefix=("new-boundary", "{user}", "--"))
    other = _command_cfg(backend_id="other")
    with mock.patch("uxon.infra.tmux.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=1001)):
        assert tmux.tmux_socket_path(first, "alice") == tmux.tmux_socket_path(changed, "alice")
        assert tmux.tmux_socket_path(first, "alice") != tmux.tmux_socket_path(other, "alice")
        first.tmux_socket_template += "-{execution_fingerprint}"
        changed.tmux_socket_template = first.tmux_socket_template
        assert tmux.tmux_socket_path(first, "alice") != tmux.tmux_socket_path(changed, "alice")


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
        ["ip", "netns", "list"], capture_output=True, text=True, check=False, timeout=3
    ).stdout.split()
    if namespace not in listed:
        pytest.skip(f"network namespace {namespace!r} does not exist")
    user = identity.process_user()
    cfg = _command_cfg(
        backend_id="netns",
        command_prefix=(helper, "{user}", "--"),
    )
    socket = f"/tmp/uxon-netns-contract-{os.getpid()}.sock"
    base = tmux.tmux_base(cfg, user, socket)
    name = f"uxon-netns-contract-{os.getpid()}"
    try:
        subprocess.run(
            base + ["new-session", "-d", "-s", name, "sleep", "30"], check=True, timeout=5
        )
        server_pid = int(
            subprocess.check_output(
                base + ["display-message", "-p", "#{pid}"], text=True, timeout=3
            )
        )
        wanted = Path(f"/var/run/netns/{namespace}").stat().st_ino
        assert Path(f"/proc/{server_pid}/ns/net").stat().st_ino == wanted
        assert wanted != Path("/proc/self/ns/net").stat().st_ino
        out = subprocess.run(
            base + ["list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
        assert name in out.splitlines()
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True, check=False, timeout=5)


def test_shared_store_checks_owner_mode_membership_acl_and_record_access(tmp_path, subtests):
    control_gid = os.getegid()
    for fault in ("valid", "owner", "mode", "controller-group", "launch-group", "acl"):
        with subtests.test(fault=fault):
            directory = tmp_path / fault
            directory.mkdir(mode=0o2770)
            directory.chmod(0o2770)
            real_lstat = os.lstat
            real_getxattr = os.getxattr

            def metadata(
                path, *args, directory=directory, fault=fault, real_lstat=real_lstat, **kwargs
            ):
                result = real_lstat(path, *args, **kwargs)
                if Path(path) == directory:
                    values = list(result)
                    values[0] = stat.S_IFDIR | (0o2777 if fault == "mode" else 0o2770)
                    values[4] = 1003 if fault == "owner" else 0
                    values[5] = control_gid
                    return os.stat_result(values)
                return result

            def xattr(
                path,
                name,
                *,
                directory=directory,
                fault=fault,
                real_getxattr=real_getxattr,
                **kwargs,
            ):
                if Path(path) == directory and fault == "acl":
                    return b"access-acl"
                return real_getxattr(path, name, **kwargs)

            pending = launch_records.PendingLaunchRecord(
                socket_path="/run/uxon/alice.sock",
                session_name="uxon-demo@claude",
                launch_nonce="a" * 32,
                launch_profile="claude",
                agent="claude",
                launch_user="alice",
            )
            with (
                mock.patch("uxon.infra.launch_records.os.lstat", side_effect=metadata),
                mock.patch("uxon.infra.launch_records.os.geteuid", return_value=1001),
                mock.patch(
                    "uxon.infra.launch_records.os.getegid",
                    return_value=control_gid + 1 if fault == "controller-group" else control_gid,
                ),
                mock.patch("uxon.infra.launch_records.os.getgroups", return_value=[]),
                mock.patch(
                    "uxon.infra.launch_records.pwd.getpwnam",
                    return_value=SimpleNamespace(pw_uid=2001, pw_gid=control_gid + 1),
                ),
                mock.patch(
                    "uxon.infra.launch_records.os.getgrouplist",
                    return_value=[control_gid] if fault == "launch-group" else [control_gid + 1],
                ),
                mock.patch("uxon.infra.launch_records.os.getxattr", side_effect=xattr),
            ):
                if fault != "valid":
                    with pytest.raises(SystemExit) as error:
                        launch_records.create_pending_record(
                            pending, override_dir=directory, shared=True
                        )
                    expected = {
                        "owner": "root-owned",
                        "mode": "mode 2770",
                        "controller-group": "controller is not a member",
                        "launch-group": "launch user must not belong",
                        "acl": "POSIX access ACL",
                    }
                    assert expected[fault] in error.value.uxon_msg
                    assert list(directory.iterdir()) == []
                    continue
                path = launch_records.create_pending_record(
                    pending, override_dir=directory, shared=True
                )
                st = path.stat()
                assert stat.S_IMODE(st.st_mode) == 0o640 and st.st_gid == control_gid
                target_uid, target_groups = 2001, {control_gid + 1}
                permissions = stat.S_IMODE(st.st_mode)
                target_access = (
                    (permissions >> 6) & 7
                    if st.st_uid == target_uid
                    else (permissions >> 3) & 7
                    if st.st_gid in target_groups
                    else permissions & 7
                )
                assert target_access == 0
                assert ((permissions >> 3) & 7) == 4
                meta = launch_records.TmuxSessionMetadata(
                    "$1", "100", pending.session_name, pending.launch_nonce
                )
                launch_records.finalize_pending_record(
                    pending, meta, override_dir=directory, shared=True
                )
                assert (
                    launch_records.read_verified_record(
                        pending.socket_path,
                        meta,
                        override_dir=directory,
                        shared=True,
                        launch_user="alice",
                    )
                    is not None
                )
                path.chmod(0o660)
                assert (
                    launch_records.read_verified_record(
                        pending.socket_path,
                        meta,
                        override_dir=directory,
                        shared=True,
                        launch_user="alice",
                    )
                    is None
                )


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
            launch_user=identity.process_user(),
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
        launch_user=identity.process_user(),
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


def test_launch_record_gc_cursor_reaches_stale_record_after_full_batch(tmp_path: Path) -> None:
    now = 10**12
    for index in range(1024):
        payload = {
            "status": "finalized",
            "launch_user": "bob" if index % 2 else "alice",
            "socket_path": "/run/uxon/current.sock",
            "session_name": f"uxon-live-{index}@claude",
            "launch_nonce": f"nonce-{index:04d}",
            "finalized_at": 1 if index % 2 else now,
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

    original = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    assert len(original) == 1025
    victim = tmp_path / "victim"
    victim.write_text("do not replace")
    cursor = launch_records._gc_cursor_path(tmp_path, "alice")
    cursor.symlink_to(victim)
    assert (
        launch_records.garbage_collect_records(
            set(), override_dir=tmp_path, launch_user="alice", now=now
        )
        == 0
    )
    assert cursor.is_symlink() and victim.read_text() == "do not replace"
    assert {path.name: path.read_bytes() for path in tmp_path.glob("*.json")} == original
    cursor.unlink()

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
    assert {path.name: path.read_bytes() for path in tmp_path.glob("*.json")} == {
        name: data for name, data in original.items() if name != "zzzz.json"
    }
    assert not cursor.is_symlink() and stat.S_IMODE(cursor.stat().st_mode) == 0o600


@pytest.mark.usefixtures("real_execution", "real_host_probes")
def test_public_adapters_keep_server_files_git_workload_and_kill_inside_one_boundary(
    tmp_path, monkeypatch
):
    from harness.target_commands import TargetCommands, target_config

    from uxon.app import kill, launch
    from uxon.infra.runtime_telemetry import read_cgroup_members

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("TMUX", raising=False)
    target = tmp_path / "project"
    target.mkdir()
    (target / "child").mkdir()
    cfg = target_config(new_project_root=str(target), allowed_roots=[str(tmp_path)])
    cfg.runtimes["box"] = WorkloadRuntimeSpec(
        id="box",
        resource_name_template="box-{project_slug}",
        exec_prefix=("engine", "exec", "-w", "{runtime_dir}", "{resource}"),
        path_map=((str(target), "/work/repo"),),
        ready_command=("engine", "ready", "{resource}"),
        exists_command=("engine", "exists", "{resource}"),
        start_command=("engine", "start", "{resource}"),
        on_missing="start",
    )
    cfg.launch.profiles["claude_box"] = LaunchProfile(
        id="claude_box", agent="claude", runtime="box", launch_user="alice"
    )
    cfg.launch = dataclasses.replace(cfg.launch, enabled_profiles=("claude_box",))
    server_state = "running"
    emitted = []
    socket = "/tmp/uxon-alice.sock"

    def respond(user, command, kwargs):
        if user == "bob":
            assert kwargs["timeout"] in (20, 60)
            if command == ["sh", "-c", "command -v gh"]:
                return _cp(stdout="/usr/bin/gh\n")
            assert command[:1] == ["gh"]
            if command[1:3] == ["auth", "status"]:
                return _cp()
            if command[1:3] == ["repo", "view"]:
                return _cp(returncode=1)
            assert command == ["gh", "repo", "create", "github.com/example/project", "--private"]
            assert kwargs["timeout"] == 60
            return _cp()
        assert user == "alice"
        emitted.append(command)
        if "uxon.infra.tmux_server_probe" in command:
            return _cp(
                stdout=json.dumps(
                    {
                        "state": server_state,
                        "sessions": [],
                        "error": "denied" if server_state == "unreachable" else "",
                    }
                )
            )
        if command[:2] == ["ps", "-eo"]:
            return _cp(stdout="111 1 32 1.5\n")
        if command[0] == "git":
            assert kwargs["timeout"] == 1.25
            return _cp(
                stdout=str(target / ".git" if "--git-common-dir" in command else target) + "\n"
            )
        if "uxon.infra.runtime_telemetry_probe" in command:
            assert kwargs["timeout"] == 10
            assert command[-4:] == ["--mode", "cgroup-members", "--cgroup", "/demo.scope"]
            return _cp(stdout=json.dumps({"ok": True, "pids": [101, 102], "error": ""}))
        if command[:2] in (["engine", "ready"], ["engine", "exists"]):
            assert kwargs["timeout"] == 10
            return _cp(returncode=int(command[1] == "ready"))
        if command[:2] == ["engine", "exec"]:
            assert command[:5] == ["engine", "exec", "-w", "/work/repo", "box-project"]
            assert command[-1] == "claude" and "uxon-agent-probe" in command
            return _cp()
        if command[:2] == ["sh", "-c"]:
            assert command[-3:] == ["engine", "start", "box-project"]
            assert command[2] == f'cd {target} && exec "$@"' and kwargs["timeout"] == 120
            return _cp()
        assert command[:3] == ["tmux", "-S", socket], command
        if command[3] == "list-sessions":
            assert kwargs["timeout"] == 1.25
            return _cp(stdout="uxon-demo@claude\t$1\t0\t1\t100\t110\t\t\t\n")
        if command[3] == "list-panes":
            assert kwargs["timeout"] == 1.25
            return _cp(stdout=f"1\t111\tclaude\t{target}\t1\t%0\t0\t\t\n")
        assert command[3:] == ["kill-session", "-t", "uxon-demo@claude"], command
        return _cp()

    external = TargetCommands(respond)
    with external.installed_boundary():
        assert canonicalize_path(cfg, "alice", str(target), intended=False) == str(target)
        assert list_directories(cfg, "alice", str(target)) == (DirectoryEntry("child", mock.ANY),)
        assert path_facts(cfg, "alice", str(target)).writable
        usage = filesystem_usage(cfg, "alice", str(target))
        assert 0 <= usage.available <= usage.total and usage.total > 0
        assert [name for name, _ in browse_project_directories(cfg, "alice", "child")] == []
        assert git.git_repo_root_nonint_as_user(cfg, str(target), "alice") == str(target)
        assert git.git_common_dir_root_as_user(cfg, str(target), "alice") == str(target)
        assert read_cgroup_members(cfg, "alice", "/demo.scope") == [101, 102]
        resolved = resolve_launch_profile(cfg, "operator", "claude_box", str(target), "normal")
        assert (
            resolved.launch_user == "alice" and resolved.runtime_context.runtime_dir == "/work/repo"
        )
        launch.ensure_runtime_ready(cfg, str(target), resolved)
        from uxon.domain.git_profiles import GitRemoteProfile
        from uxon.gitremote import backend_gh

        credentials = GitRemoteProfile(
            "private", "github.com", "example", "gh", "bob", "", "private"
        )
        backend_gh.preflight(cfg, credentials, "project", "bob", "operator")
        assert (
            backend_gh.create_remote(cfg, credentials, "project", str(target), "bob", "operator")
            == "git@github.com:example/project.git"
        )
        req, _ = tmux.build_managed_tmux_launch_request(
            str(target),
            "uxon-project@claude_box",
            ParsedArgs(action="run"),
            cfg,
            None,
            resolved_profile=resolved,
        )
        assert req.managed is not None
        for command in (
            req.cmd,
            req.managed.create_cmd,
            req.managed.query_cmd,
            req.managed.release_cmd,
            req.managed.rollback_kill_prefix,
        ):
            assert command[:6] == ("boundary", "alice", "--", "tmux", "-S", socket)
        create = req.managed.create_cmd
        index = create.index("engine")
        assert create.index("uxon.infra.launch_bootstrap") < index
        assert create[index : index + 5] == ("engine", "exec", "-w", "/work/repo", "box-project")
        assert "claude" in create[index + 5 :]
        assert not any(str(tmp_path / "state") in arg for arg in create)
        snapshot = sessions_probe.collect_session_snapshot_for_user(cfg, "alice", "uxon-", socket)
        assert snapshot.server_state == "running" and [s.name for s in snapshot.sessions] == [
            "uxon-demo@claude"
        ]
        assert (snapshot.sessions[0].rss_kib, snapshot.sessions[0].cpu_pct) == (32, 1.5)
        attach = tmux._build_tmux_attach_request(snapshot.sessions[0], cfg, "alice")
        assert attach.cmd[:6] == ("boundary", "alice", "--", "tmux", "-S", socket)
        assert (
            kill.do_kill(
                ParsedArgs(action="kill", target_id="demo@claude", force=True), cfg, "alice"
            )
            == 0
        )
    assert sum(c[3:] == ["kill-session", "-t", "uxon-demo@claude"] for c in emitted) == 1
    assert {user for user, _, _ in external.calls} == {"alice", "bob"}
    assert [command[0] for user, command, _ in external.calls if user == "bob"] == [
        "sh",
        "gh",
        "gh",
        "gh",
    ]


@pytest.mark.usefixtures("real_execution")
def test_session_snapshot_preserves_server_state_and_reports_transport_errors(
    tmp_path, monkeypatch, subtests
):
    from harness.target_commands import TargetCommands, target_config

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = target_config()
    for state in ("absent", "running", "unreachable"):
        with subtests.test(state=state):

            def respond(user, command, kwargs, *, state=state):
                if "uxon.infra.tmux_server_probe" in command:
                    return _cp(
                        stdout=json.dumps(
                            {
                                "state": state,
                                "sessions": [],
                                "error": "permission denied" if state == "unreachable" else "",
                            }
                        )
                    )
                assert state == "running" and command[3] == "list-sessions"
                return _cp()

            with TargetCommands(respond).installed_boundary():
                if state == "unreachable":
                    with pytest.raises(SystemExit) as raised:
                        sessions_probe.collect_session_snapshot_for_user(
                            cfg, "alice", "uxon-", "/tmp/uxon-alice.sock"
                        )
                    assert "permission denied" in raised.value.uxon_msg
                else:
                    snapshot = sessions_probe.collect_session_snapshot_for_user(
                        cfg, "alice", "uxon-", "/tmp/uxon-alice.sock"
                    )
                    assert snapshot.server_state == state and snapshot.sessions == ()


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_native_server_release_failure_clean_exit_empty_and_reuse(tmp_path, monkeypatch):
    socket = tmp_path / "tmux.sock"
    base = ["tmux", "-f", "/dev/null", "-S", str(socket)]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def run(*args, **kwargs):
        return subprocess.run(
            base + list(args), capture_output=True, text=True, timeout=5, **kwargs
        )

    def until(predicate, message):
        deadline = time.monotonic() + 5
        while not predicate():
            assert time.monotonic() < deadline, message
            time.sleep(0.005)

    nonce = "a" * 32
    release = launch_records.handshake_channel(nonce, "release")
    other = launch_records.handshake_channel("b" * 32, "release")
    try:
        run("new-session", "-d", "-s", "anchor", "sleep", "30", check=True)
        run("set-option", "-s", "exit-empty", "off", check=True)
        run("wait-for", "-S", release, check=True)
        run("wait-for", release, check=True)
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run(base + ["wait-for", other], capture_output=True, timeout=0.1, check=True)
        for name, status in (("failed", 17), ("clean", 0)):
            run(
                "new-session",
                "-d",
                "-s",
                name,
                "sh",
                "-c",
                'tmux -S "$1" wait-for "$2"; printf "diagnostic-%s\\r\\n" "$2"; exit "$3"',
                "sh",
                str(socket),
                name,
                str(status),
                ";",
                "set-window-option",
                "-t",
                name,
                "remain-on-exit",
                "failed",
                check=True,
            )
            run("wait-for", "-S", name, check=True)
            if status:
                until(
                    lambda *, name=name: (
                        run(
                            "display-message", "-p", "-t", name, "#{pane_dead} #{pane_dead_status}"
                        ).stdout.strip()
                        == "1 17"
                    ),
                    "failed pane not retained",
                )
                assert (
                    "diagnostic-failed"
                    in run("capture-pane", "-p", "-S", "-", "-t", name, check=True).stdout
                )
            else:
                until(
                    lambda *, name=name: run("has-session", "-t", name).returncode != 0,
                    "clean pane retained",
                )
        run("kill-session", "-t", "failed", check=True)
        run("kill-session", "-t", "anchor", check=True)
        assert tmux_server_probe.collect(socket) == {
            "state": "running",
            "sessions": [],
            "error": "",
        }
        run("kill-server", check=True)
        until(
            lambda: tmux_server_probe.collect(socket)["state"] == "absent",
            "server stayed reachable",
        )
        assert socket.is_socket()
        run("new-session", "-d", "-s", "uxon-reused@claude", "sleep", "30", check=True)
        assert tmux_server_probe.collect(socket) == {
            "state": "running",
            "sessions": ["uxon-reused@claude"],
            "error": "",
        }
        cfg = make_config(tmux_socket_template=str(socket))
        snapshot = sessions_probe.collect_session_snapshot_for_user(
            cfg, identity.process_user(), "uxon-", str(socket)
        )
        [session] = snapshot.sessions
        assert (
            session.name == "uxon-reused@claude"
            and session.pane_pids
            and session.active_pid in session.pane_pids
        )
        assert session.rss_kib > 0 and not session.runtime_resource and not session.runtime_down
    finally:
        run("kill-server")
        socket.unlink(missing_ok=True)


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_native_default_socket_does_not_block_unrelated_sessions(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="uxon-tmux-") as directory:
        monkeypatch.setenv("TMUX_TMPDIR", directory)
        monkeypatch.delenv("TMUX", raising=False)
        socket = Path(directory) / f"tmux-{os.geteuid()}" / "default"
        base = ["tmux", "-f", "/dev/null"]
        try:
            subprocess.run(
                base + ["new-session", "-d", "-s", "unrelated-service", "sleep", "30"],
                capture_output=True,
                check=True,
                timeout=5,
            )
            assert stat.S_IMODE(socket.stat().st_mode) == 0o660
            for mode in (0o660, 0o770):
                socket.chmod(mode)
                assert tmux_server_probe.collect(socket) == {
                    "state": "running",
                    "sessions": ["unrelated-service"],
                    "error": "",
                }
                snapshot = sessions_probe.collect_session_snapshot_for_user(
                    make_config(), identity.process_user(), "uxon-", None
                )
                assert snapshot.server_state == "running" and snapshot.sessions == ()
                assert stat.S_IMODE(socket.stat().st_mode) == mode
        finally:
            subprocess.run(base + ["kill-server"], capture_output=True, timeout=5)


def test_socket_state_policy_and_replacement_never_adopt_foreign_listener(tmp_path, subtests):
    cases = (
        ("missing", "absent", ""),
        ("regular", "unreachable", "not a socket"),
        ("parent", "unreachable", "0700"),
        ("closed", "absent", ""),
        ("foreign", "unreachable", "foreign listener"),
        ("mode600", "running", ""),
        ("mode700", "running", ""),
        ("mode660", "running", ""),
        ("mode770", "running", ""),
        ("world", "unreachable", "permissions"),
        ("symlink", "unreachable", "not a socket"),
        ("replaced", "unreachable", "foreign listener"),
    )
    for label, state, error in cases:
        with subtests.test(label=label):
            directory = tmp_path / label
            directory.mkdir(mode=0o700)
            path = directory / "tmux.sock"
            listeners = []
            if label == "regular":
                path.write_text("keep")
            elif label not in {"missing", "parent"}:
                listener = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
                listeners.append(listener)
                listener.bind(str(path))
                path.chmod(
                    int(label[4:], 8)
                    if label.startswith("mode")
                    else 0o666
                    if label == "world"
                    else 0o600
                )
                if label in {"closed", "replaced"}:
                    listener.close()
                else:
                    listener.listen(1)
                if label == "symlink":
                    victim = directory / "victim.sock"
                    path.rename(victim)
                    path.symlink_to(victim)
            if label == "parent":
                directory.chmod(0o755)

            def run(argv, *, timeout, label=label, listeners=listeners, path=path):
                assert argv == ["tmux", "-S", str(path), "list-sessions", "-F", "#{session_name}"]
                assert timeout == 2
                if label == "replaced":
                    path.unlink()
                    replacement = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
                    replacement.bind(str(path))
                    path.chmod(0o600)
                    replacement.listen(1)
                    listeners.append(replacement)
                return _cp(
                    stdout="zeta\nalpha\n" if label.startswith("mode") else "",
                    stderr="foreign listener",
                    returncode=int(not label.startswith("mode")),
                )

            try:
                with mock.patch("uxon.infra.tmux_server_probe.run_query", side_effect=run):
                    result = tmux_server_probe.collect(path)
                assert result["state"] == state and result["sessions"] == (
                    ["alpha", "zeta"] if state == "running" else []
                )
                assert error in result["error"]
                if label == "regular":
                    assert path.read_text() == "keep"
                elif label == "symlink":
                    assert path.is_symlink() and victim.is_socket()
                elif label not in {"missing", "parent"}:
                    assert path.is_socket()
            finally:
                for listener in listeners:
                    listener.close()


def test_local_target_filesystem_adapters_use_nonlogin_prefix_and_exact_results(subtests):
    cfg = make_config()
    payloads = {
        "existing": {"ok": True, "path": "/inside/project", "error": ""},
        "list-directories": {"ok": True, "entries": [{"name": "child", "mtime": 123}], "error": ""},
        "inspect": {
            "ok": True,
            "path": "/inside/project",
            "exists": True,
            "directory": True,
            "writable": False,
            "nearest_existing_ancestor": "/inside/project",
            "error": "",
        },
        "filesystem-usage": {"ok": True, "total": 4096, "available": 1024, "error": ""},
    }
    calls = []

    def run(argv, *, timeout):
        assert argv[:6] == ["/usr/bin/sudo", "-n", "-H", "-u", "alice", "--"]
        assert argv[7:9] == ["-m", "uxon.infra.path_probe"]
        assert timeout == cfg.execution.backend_for_user("alice").probe_timeout_seconds
        mode = argv[argv.index("--mode") + 1]
        assert argv[-2:] == ["--path", "/outside/project"]
        calls.append(mode)
        return _cp(stdout=json.dumps(payloads[mode]))

    with (
        mock.patch("uxon.infra.execution.process_user", return_value="operator"),
        mock.patch("uxon.infra.execution.run_query", side_effect=run),
    ):
        assert (
            canonicalize_path(cfg, "alice", "/outside/project", intended=False) == "/inside/project"
        )
        assert list_directories(cfg, "alice", "/outside/project") == (DirectoryEntry("child", 123),)
        assert not path_facts(cfg, "alice", "/outside/project").writable
        assert filesystem_usage(cfg, "alice", "/outside/project") == FilesystemUsage(4096, 1024)
    assert calls == ["existing", "list-directories", "inspect", "filesystem-usage"]


@pytest.mark.usefixtures("real_execution", "real_host_probes")
def test_public_doctor_reports_selected_execution_attestation_success_and_failure(
    tmp_path, monkeypatch, subtests
):
    import io
    from contextlib import redirect_stdout

    from harness.target_commands import TargetCommands, target_config

    from uxon.app.doctor import do_doctor

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = target_config(
        new_project_root=str(tmp_path),
        allowed_roots=[str(tmp_path)],
        tmux_socket_template=str(tmp_path / "socket" / "tmux.sock"),
    )
    cfg.launch = dataclasses.replace(
        cfg.launch,
        enabled_profiles=("claude",),
        profiles={"claude": LaunchProfile(id="claude", agent="claude", launch_user="alice")},
    )
    for outcome in ("ok", "denied"):
        with subtests.test(outcome=outcome):

            def respond(user, command, kwargs):
                assert user == "alice"
                assert "uxon.infra.tmux_server_probe" in command
                return _cp(stdout=json.dumps({"state": "absent", "sessions": [], "error": ""}))

            external = TargetCommands(respond, installed=("tmux",))
            original = external.__call__

            def run(argv, *, original=original, outcome=outcome, **kwargs):
                if outcome == "denied" and argv[-2:] == ["-m", "uxon.infra.execution_probe"]:
                    assert argv[:3] == ["boundary", "alice", "--"] and kwargs["timeout"] == 1.25
                    return _cp(returncode=1, stderr="denied-sentinel")
                return original(argv, **kwargs)

            with (
                external.installed_boundary(),
                mock.patch("uxon.infra.run.subprocess.run", side_effect=run),
                redirect_stdout(io.StringIO()) as output,
            ):
                assert do_doctor(cfg, "operator", "alice", str(tmp_path), json_output=True) == 0
            [row] = json.loads(output.getvalue())["data"]["execution_backends"]
            assert row == {
                "launch_user": "alice",
                "backend": "boundary",
                "kind": "command",
                "fingerprint": cfg.execution.backend_for_user("alice").fingerprint,
                "status": "ok" if outcome == "ok" else "error",
                "error": "" if outcome == "ok" else "denied-sentinel",
            }
