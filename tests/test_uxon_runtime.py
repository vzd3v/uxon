"""Workload policy, authority and attribution through external OS responses."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest
from harness.launch_records import save_launch_record, session_from_record
from harness.target_commands import TargetCommands, target_config
from helpers import make_session

from uxon.app import doctor, kill, launch
from uxon.app.launch_profile import resolve_launch_profile
from uxon.domain.args import ParsedArgs
from uxon.domain.launch_profiles import (
    LaunchConfig,
    LaunchProfile,
    RuntimeIdentity,
)
from uxon.domain.runtime import (
    WorkloadRuntimeSpec,
    apply_path_map,
    is_valid_runtime_resource,
    path_map_under_prefix,
    resolve_runtime_resource_name,
    runtime_pidfile,
    validate_path_map,
    validate_runtime,
    validate_runtime_resource,
)
from uxon.domain.runtime_usage import parse_cgroup_procs, per_session_usage, sum_usage_for_pids
from uxon.infra import runtime, sessions_probe, tmux

pytestmark = pytest.mark.usefixtures("real_execution", "real_host_probes")


def _cp(stdout="", *, rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


def _spec(**changes):
    return dataclasses.replace(
        WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="box-{project_slug}",
            exec_prefix=("engine", "exec", "-w", "{runtime_dir}", "{resource}"),
            ready_command=("engine", "ready", "{resource}"),
            exists_command=("engine", "exists", "{resource}"),
            start_command=("engine", "start", "{resource}"),
            create_command=("engine", "create", "{resource}"),
            identity_command=(
                "engine",
                "identity",
                "{resource}",
                "{runtime_dir}",
                "{launch_profile}",
                "{agent}",
                "{project_slug}",
                "{user}",
            ),
            probe_timeout_seconds=1.5,
            prepare_timeout_seconds=2.5,
            stop_timeout_seconds=3.5,
        ),
        **changes,
    )


def _cfg(target, spec=None, **changes):
    cfg = target_config(
        allowed_roots=[str(target.parent)],
        new_project_root=str(target.parent),
        tmux_manage_options=False,
        tmux_options={},
        tmux_server_options={},
        tmux_append_server_options={},
        **changes,
    )
    cfg.runtimes["box"] = spec or _spec()
    cfg.launch = LaunchConfig(
        default_profile="claude",
        enabled_profiles=("claude",),
        profiles={
            "claude": LaunchProfile(id="claude", agent="claude", launch_user="alice", runtime="box")
        },
    )
    return cfg


def _resolve(cfg, target):
    return resolve_launch_profile(cfg, "operator", "claude", str(target), "normal")


def test_resource_names_paths_and_templates_have_literal_semantics(subtests):
    for template, slug, expected in (
        ("proj-{project_slug}", "myapp", "proj-myapp"),
        ("proj-{project_slug}", "other", "proj-other"),
        ("{user}-box", "x", "alice-box"),
        (
            "{user}-{runtime}-{launch_profile}-{agent}-{project_slug}",
            "repo",
            "alice-box-fast-claude-repo",
        ),
    ):
        with subtests.test(template=template, slug=slug):
            assert (
                resolve_runtime_resource_name(
                    _spec(resource_name_template=template),
                    user="alice",
                    launch_profile="fast",
                    agent="claude",
                    project_slug=slug,
                )
                == expected
            )
    for value, valid in (
        ("proj-app_1.2", True),
        ("a" * 128, True),
        ("a" * 129, False),
        ("", False),
        ("-option", False),
        (".dot", False),
        ("...", False),
        ("_under", False),
        ("bad name", False),
        ("bad\x00name", False),
    ):
        with subtests.test(resource=value):
            assert is_valid_runtime_resource(value) == valid
            if valid:
                assert validate_runtime_resource(value) == value
            else:
                with pytest.raises(SystemExit):
                    validate_runtime_resource(value)
    with pytest.raises(SystemExit):
        resolve_runtime_resource_name(
            _spec(resource_name_template="{project_slug}"),
            user="alice",
            launch_profile="claude",
            agent="claude",
            project_slug=".evil",
        )
    maps = validate_path_map({"/srv": "/root", "/srv/projects/app": "/work/{resource}"})
    for path, expected, covered in (
        ("/srv", "/root", True),
        ("/srv/projects/app", "/work/{resource}", True),
        ("/srv/projects/app/sub", "/work/{resource}/sub", True),
        ("/srv/projects/app-old", "/root/projects/app-old", True),
        ("/srv-old/app", "/srv-old/app", False),
        ("/other", "/other", False),
    ):
        with subtests.test(path=path):
            assert (
                apply_path_map(path, maps) == expected
                and path_map_under_prefix(path, maps) == covered
            )
            assert apply_path_map(path, ()) == path and not path_map_under_prefix(path, ())
    for invalid in (
        {"relative": "/work"},
        {"/srv": "relative"},
        {"/srv": "{resource}"},
        {"/srv": "/work/../../etc"},
        {"/srv/../etc": "/work"},
    ):
        with subtests.test(invalid=invalid), pytest.raises(SystemExit):
            validate_path_map(invalid)
    for field in (
        "resource_name_template",
        "exec_prefix",
        "ready_command",
        "exists_command",
        "start_command",
        "create_command",
        "identity_command",
        "stop_command",
    ):
        with subtests.test(field=field), pytest.raises(SystemExit) as error:
            value = "x-{bogus}" if field == "resource_name_template" else ("engine", "{bogus}")
            validate_runtime(_spec(**{field: value}))
        assert field in error.value.uxon_msg and "bogus" in error.value.uxon_msg
    first = _spec()
    for changed in (
        dataclasses.replace(first, id="other"),
        dataclasses.replace(first, exec_prefix=("other", "{resource}")),
        dataclasses.replace(first, path_map=(("/srv", "/work"),)),
    ):
        assert changed.fingerprint != first.fingerprint


def test_identity_cgroup_and_nonce_contracts(subtests):
    valid = {"id": "cid-1", "host_pid": 4242, "epoch": "epoch-1"}
    for payload, expected in (
        (valid, RuntimeIdentity("cid-1", 4242, epoch="epoch-1")),
        (dict(valid, extra="accepted"), RuntimeIdentity("cid-1", 4242, epoch="epoch-1")),
        ([], None),
        (dict(valid, id=""), None),
        (dict(valid, host_pid=True), None),
        (dict(valid, host_pid=0), None),
        (dict(valid, host_pid="42"), None),
        (dict(valid, epoch=""), None),
        ({"id": "cid-1", "host_pid": 42}, None),
    ):
        with subtests.test(payload=payload):
            assert runtime.parse_runtime_identity_output(json.dumps(payload)) == expected
    assert runtime.parse_runtime_identity_output("") is None
    for content, expected in (
        ("12:pids:/v1\n0::/v2\n11:memory:/other\n", "/v2"),
        ("0::/a:b/c", "/a:b/c"),
        ("12:pids:/v1\n11:memory:/other", "/v1"),
        ("garbage\n\n0::\n", ""),
    ):
        with subtests.test(content=content):
            assert runtime.parse_proc_cgroup(content) == expected
    for nonce in ("a" * 32, "b" * 32):
        assert runtime_pidfile(nonce) == f"/tmp/uxon-{nonce}.pid"
    for nonce in ("", "short", "../" + "a" * 32, "uxon-app@claude"):
        with subtests.test(nonce=nonce), pytest.raises(SystemExit):
            runtime_pidfile(nonce)
    pids = parse_cgroup_procs("1\n2\n\n2\nnoise\n3\n4\n99\n")
    assert pids == [1, 2, 3, 4, 99] and parse_cgroup_procs("") == []
    rows = {1: (0, 100, 95.0), 2: (1, 50, 1.0), 3: (0, 200, 2.0), 4: (0, -5, -1.0)}
    assert sum_usage_for_pids(pids, rows) == (350, 98.0)
    assert per_session_usage(pids, {1: "a", 2: "a", 3: "b", 4: ""}, rows) == {
        "a": (150, 96.0),
        "b": (200, 2.0),
    }
    assert per_session_usage(pids, {}, rows) == {}


def test_optional_bad_identity_template_degrades_without_external_command(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    cfg = _cfg(target, _spec(identity_command=("engine", "{.Id}")))
    with TargetCommands().installed_boundary():
        resolved = _resolve(cfg, target)
        assert (
            runtime.resolve_runtime_identity_for_profile(cfg, str(target), resolved)
            == runtime.EMPTY_IDENTITY
        )


def test_readiness_state_policy_and_prepare_commands_are_bounded(tmp_path, subtests):
    target = tmp_path / "repo with spaces;literal"
    target.mkdir()
    for state in ("running", "stopped", "absent"):
        for policy in ("fail", "start", "create"):
            with subtests.test(state=state, policy=policy):
                spec = _spec(on_missing=policy, path_map=((str(target), "/work/repo"),))
                cfg = _cfg(target, spec)
                emitted = []
                expected = (
                    "exec"
                    if state == "running"
                    else "start"
                    if state == "stopped" and policy in {"start", "create"}
                    else "create"
                    if state == "absent" and policy == "create"
                    else "fail"
                )

                def respond(
                    user, command, kwargs, *, emitted=emitted, expected=expected, state=state
                ):
                    assert user == "alice"
                    emitted.append(command)
                    if command[:2] == ["engine", "ready"]:
                        assert kwargs["timeout"] == 1.5
                        return _cp(rc=int(state != "running"))
                    if command[:2] == ["engine", "exists"]:
                        assert kwargs["timeout"] == 1.5
                        return _cp(rc=int(state == "absent"))
                    assert command[:2] == ["sh", "-c"] and command[-3:-1] == ["engine", expected]
                    assert command[2] == f'cd {__import__("shlex").quote(str(target))} && exec "$@"'
                    assert kwargs["timeout"] == 2.5
                    return _cp()

                with TargetCommands(respond).installed_boundary():
                    resolved = _resolve(cfg, target)
                    plan = runtime.plan_runtime_launch_for_profile(cfg, str(target), resolved)
                    expected = (
                        "exec"
                        if state == "running"
                        else "start"
                        if state == "stopped" and policy in {"start", "create"}
                        else "create"
                        if state == "absent" and policy == "create"
                        else "fail"
                    )
                    assert (plan.action, plan.reason) == (expected, state)
                    assert plan.prepare_command == (
                        ("engine", expected, resolved.runtime_context.resource)
                        if expected in {"start", "create"}
                        else ()
                    )
                    assert plan.prepare_timeout_seconds == 2.5 and plan.message
                    if expected == "fail":
                        with pytest.raises(SystemExit):
                            runtime.run_prepare(cfg, plan, str(target), "alice")
                    else:
                        runtime.run_prepare(cfg, plan, str(target), "alice")
                assert len(emitted) == 1 + (state != "running") + (expected in {"start", "create"})


def test_runtime_probe_transport_failure_never_creates_resource(tmp_path, subtests):
    target = tmp_path / "repo"
    target.mkdir()
    cfg = _cfg(target, _spec(on_missing="create"))
    for failure in ("timeout", "permission", "missing"):
        with subtests.test(failure=failure):
            emitted = []

            def respond(user, command, kwargs, *, emitted=emitted, failure=failure):
                emitted.append(command)
                assert command[:2] == ["engine", "ready"] and kwargs["timeout"] == 1.5
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(command, 1.5)
                if failure == "permission":
                    raise PermissionError("denied")
                raise FileNotFoundError("missing")

            with TargetCommands(respond).installed_boundary():
                resolved = _resolve(cfg, target)
                with pytest.raises(SystemExit) as error:
                    launch.ensure_runtime_ready(cfg, str(target), resolved)
            assert (
                len(emitted) == 1
                and ("did not respond" if failure == "timeout" else "unavailable")
                in error.value.uxon_msg
            )


def test_cli_and_tui_gate_probe_workload_agent_after_pinned_prepare(tmp_path, subtests):
    target = tmp_path / "repo"
    target.mkdir()
    cfg = _cfg(target, _spec(on_missing="start", path_map=((str(target), "/work/repo"),)))
    cfg.agents["claude"] = dataclasses.replace(cfg.agents["claude"], binary="custom-agent")
    for caller, state in (("cli", "stopped"), ("tui", "running"), ("tui", "stopped")):
        with subtests.test(caller=caller, state=state):
            emitted = []

            def respond(user, command, kwargs, *, emitted=emitted, state=state):
                assert user == "alice"
                emitted.append(command)
                if command[:2] == ["engine", "ready"]:
                    return _cp(rc=int(state != "running"))
                if command[:2] == ["engine", "exists"]:
                    return _cp()
                if command[:2] == ["sh", "-c"]:
                    assert (
                        command[-3:] == ["engine", "start", "box-repo"]
                        and str(target) in command[2]
                    )
                    return _cp()
                assert command[:5] == ["engine", "exec", "-w", "/work/repo", "box-repo"]
                assert command[-3:] == [
                    'command -v -- "$1" >/dev/null 2>&1',
                    "uxon-agent-probe",
                    "custom-agent",
                ]
                return _cp()

            with TargetCommands(respond, installed=("tmux",)).installed_boundary():
                resolved = _resolve(cfg, target)
                if caller == "cli":
                    launch.ensure_runtime_ready(cfg, str(target), resolved)
                else:
                    gate = launch.decide_runtime_gate(cfg, str(target), resolved)
                    if state == "running":
                        assert gate is None
                    else:
                        assert gate.needs_prepare and gate.needs_prompt and not gate.fail_message
                        assert not any(c[:2] == ["sh", "-c"] for c in emitted)
                        gate.prepare()
            assert sum(c[:2] == ["engine", "exec"] for c in emitted) == 1


def test_builder_emits_direct_mapped_and_stop_enabled_workload_without_losing_argv(
    tmp_path, monkeypatch, subtests
):
    target = tmp_path / "repo"
    target.mkdir()
    monkeypatch.delenv("TMUX", raising=False)
    stub = target / "claude"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "nonce = os.environ['UXON_LAUNCH_NONCE']\n"
        "record = pathlib.Path('/tmp/uxon-' + nonce + '.pid')\n"
        "print(json.dumps(dict(argv=sys.argv[1:], nonce=nonce, "
        "session=os.environ['UXON_SESSION'], pid=os.getpid(), "
        "record=record.exists())))\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(target) + os.pathsep + os.environ["PATH"])
    for kind in ("direct", "bare", "mapped", "identity", "stop", "missing-identity"):
        with subtests.test(kind=kind):
            spec = _spec(
                identity_command=()
                if kind in {"bare", "mapped", "missing-identity"}
                else _spec().identity_command,
                path_map=((str(target), "/work/repo"),) if kind == "mapped" else (),
                stop_command=("engine", "stop", "{resource}", "{pidfile}")
                if kind in {"stop", "missing-identity"}
                else (),
            )
            cfg = _cfg(target, spec)
            if kind == "direct":
                cfg.launch = dataclasses.replace(
                    cfg.launch,
                    profiles={
                        "claude": LaunchProfile(id="claude", agent="claude", launch_user="alice")
                    },
                )

            def respond(user, command, kwargs):
                assert command[:2] == ["engine", "identity"] and kwargs["timeout"] == 1.5
                return _cp(json.dumps({"id": "cid-1", "host_pid": 42, "epoch": "epoch-1"}))

            with TargetCommands(respond).installed_boundary():
                resolved = _resolve(cfg, target)
                if kind == "missing-identity":
                    with pytest.raises(SystemExit) as error:
                        tmux.build_managed_tmux_launch_request(
                            str(target),
                            "uxon-repo@claude",
                            ParsedArgs(action="run"),
                            cfg,
                            None,
                            resolved_profile=resolved,
                        )
                    assert "requires identity_command" in error.value.uxon_msg
                    continue
                args = ParsedArgs(action="run", agent_args=["space arg", "$HOME;literal"])
                request, pending = tmux.build_managed_tmux_launch_request(
                    str(target), "uxon-repo@claude", args, cfg, None, resolved_profile=resolved
                )
            create = request.managed.create_cmd
            bootstrap = create.index("uxon.infra.launch_bootstrap")
            workload = create[create.index("--", bootstrap) + 1 : create.index(";", bootstrap)]
            assert create[:3] == ("boundary", "alice", "--")
            assert pending.launch_user == "alice" and pending.execution_backend == "boundary"
            if kind == "direct":
                assert workload == ("claude", "space arg", "$HOME;literal")
            else:
                assert workload[:5] == (
                    "engine",
                    "exec",
                    "-w",
                    "/work/repo" if kind == "mapped" else str(target),
                    "box-repo",
                )
                assert workload[workload.index("uxon-agent") + 1 :] == (
                    "claude",
                    "space arg",
                    "$HOME;literal",
                )
                assert "UXON_RUNTIME_RESOURCE=box-repo" in create
                assert f"UXON_LAUNCH_NONCE={pending.launch_nonce}" in create
                if kind in {"identity", "stop"}:
                    assert (
                        "UXON_RUNTIME_ID=cid-1" in create and "UXON_RUNTIME_EPOCH=epoch-1" in create
                    )
                else:
                    assert not any(arg.startswith("UXON_RUNTIME_ID=") for arg in create)
                pidfile = Path(runtime_pidfile(pending.launch_nonce))
                assert not pidfile.exists()
                try:
                    result = subprocess.run(
                        workload[5:], capture_output=True, text=True, timeout=3, check=True
                    )
                    actual = json.loads(result.stdout)
                    assert (actual["argv"], actual["nonce"], actual["session"]) == (
                        ["space arg", "$HOME;literal"],
                        pending.launch_nonce,
                        pending.session_name,
                    )
                    assert actual["record"] == pidfile.exists() == (kind == "stop")
                    if kind == "stop":
                        pid, ticks = map(int, pidfile.read_text().split())
                        assert pid == actual["pid"] and ticks > 0
                        assert pidfile.stat().st_mode & 0o777 == 0o600
                finally:
                    pidfile.unlink(missing_ok=True)
            assert request.managed.runtime_dir == (
                "/work/repo" if kind == "mapped" else str(target) if kind != "direct" else ""
            )


def test_runtime_collision_scope_includes_different_user_and_intentional_sharing(
    tmp_path, subtests
):
    target = tmp_path / "repo"
    target.mkdir()
    for scope, user, same_id, same_resource, denied in (
        ("global", "bob", False, True, True),
        ("per_user", "alice", False, True, True),
        ("per_user", "bob", False, True, False),
        ("per_user", "alice", True, True, False),
        ("global", "alice", False, False, False),
    ):
        with subtests.test(scope=scope, user=user, same_id=same_id, same_resource=same_resource):
            cfg = _cfg(target)
            cfg.runtimes = {
                "one": _spec(id="one", resource_scope=scope, resource_name_template="shared"),
                "two": _spec(
                    id="two",
                    resource_scope=scope,
                    resource_name_template="shared" if same_resource else "other",
                ),
            }
            cfg.launch = LaunchConfig(
                default_profile="first",
                enabled_profiles=("first", "second"),
                profiles={
                    "first": LaunchProfile(
                        id="first", agent="claude", launch_user="alice", runtime="one"
                    ),
                    "second": LaunchProfile(
                        id="second",
                        agent="claude",
                        launch_user=user,
                        runtime="one" if same_id else "two",
                    ),
                },
            )
            with TargetCommands(installed=("tmux",)).installed_boundary():
                if denied:
                    with pytest.raises(SystemExit) as error:
                        resolve_launch_profile(cfg, "operator", "first", str(target), "normal")
                    assert "collides" in error.value.uxon_msg
                else:
                    resolved = resolve_launch_profile(
                        cfg, "operator", "first", str(target), "normal"
                    )
                    assert (
                        resolved.runtime_context.resource == "shared"
                        and resolved.runtime_context.runtime_id == "one"
                    )


def test_teardown_rechecks_each_authority_dimension_and_retains_failed_stop_for_retry(
    tmp_path, monkeypatch, subtests
):
    target = tmp_path / "repo"
    target.mkdir()
    for label, reason in (
        ("missing-runtime", "missing_runtime"),
        ("missing-profile", "missing_profile"),
        ("fingerprint", "fingerprint_mismatch"),
        ("saved-id", "identity_unresolved"),
        ("saved-epoch", "identity_unresolved"),
        ("late-profile", "missing_profile"),
        ("late-fingerprint", "fingerprint_mismatch"),
        ("live-id", "stale_identity"),
        ("live-epoch", "stale_identity"),
        ("live-both", "stale_identity"),
        ("unresolved", "identity_unresolved"),
        ("stop-fail", None),
        ("exact", None),
    ):
        with subtests.test(label=label):
            monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / label))
            cfg = _cfg(
                target,
                _spec(
                    stop_command=(
                        "engine",
                        "stop",
                        "{resource}",
                        "{pidfile}",
                        "{user}",
                        "{launch_profile}",
                        "{agent}",
                        "{project_slug}",
                    )
                ),
            )
            stopped = {"recovered": False}
            emitted = []

            def respond(user, command, kwargs, *, emitted=emitted, label=label, stopped=stopped):
                assert user == "alice"
                if command[0] == "tmux" and "list-sessions" in command:
                    return _cp("")
                emitted.append(command)
                if command[:2] == ["engine", "identity"]:
                    assert command == [
                        "engine",
                        "identity",
                        "box-repo",
                        str(target),
                        "claude",
                        "claude",
                        "repo",
                        "alice",
                    ]
                    assert kwargs["timeout"] == 1.5
                    return _cp(
                        json.dumps(
                            {
                                "id": "cid-other" if label in {"live-id", "live-both"} else "cid-1",
                                "host_pid": 42,
                                "epoch": "epoch-other"
                                if label in {"live-epoch", "live-both"}
                                else "epoch-1",
                            }
                        ),
                        rc=int(label == "unresolved"),
                    )
                assert command == [
                    "engine",
                    "stop",
                    "box-repo",
                    runtime_pidfile("a" * 32),
                    "alice",
                    "claude",
                    "claude",
                    "repo",
                ]
                assert kwargs["timeout"] == 3.5
                return _cp(
                    rc=7 if label == "stop-fail" and not stopped["recovered"] else 0,
                    stderr="stop-sentinel",
                )

            with TargetCommands(respond).installed_boundary():
                resolved = _resolve(cfg, target)
                pending, metadata, path = save_launch_record(cfg, resolved, target_dir=str(target))
                payload = json.loads(path.read_text())
                if label in {"missing-runtime", "saved-id", "saved-epoch"}:
                    payload[
                        {
                            "missing-runtime": "runtime",
                            "saved-id": "runtime_id",
                            "saved-epoch": "runtime_epoch",
                        }[label]
                    ] = ""
                    path.write_text(json.dumps(payload))
                session = session_from_record(pending, metadata)
                session.active_path = "/mutable/pane/decoy"
                if label == "missing-profile":
                    cfg.runtimes.clear()
                elif label == "fingerprint":
                    cfg.runtimes["box"] = _spec(id="changed")
                with mock.patch("uxon.infra.audit.audit") as audit:
                    teardown = kill.prepare_runtime_teardown(cfg, session)
                    if label in {
                        "missing-runtime",
                        "missing-profile",
                        "fingerprint",
                        "saved-id",
                        "saved-epoch",
                    }:
                        assert isinstance(teardown, kill.CleanupResult) and not teardown
                        assert not kill.finish_killed_session(
                            cfg, session, teardown, target_user="alice"
                        )
                        assert emitted == [] and path.exists()
                    else:
                        assert teardown is not None and (
                            teardown.runtime_id,
                            teardown.launch_epoch,
                        ) == ("cid-1", "epoch-1")
                        if label == "late-profile":
                            cfg.runtimes.clear()
                        elif label == "late-fingerprint":
                            cfg.runtimes["box"] = _spec(id="changed")
                        result = kill.finish_killed_session(
                            cfg, session, teardown, target_user="alice"
                        )
                        assert bool(result) == (label == "exact")
                        assert path.exists() == (label != "exact")
                    [event] = audit.call_args_list
                    assert event.args == ("runtime.session_stop",)
                    assert event.kwargs["outcome"] == (
                        "skipped" if reason else "error" if label == "stop-fail" else "ok"
                    )
                    assert event.kwargs.get("reason") == reason
                    assert (
                        event.kwargs["runtime_resource"] == "box-repo"
                        and event.kwargs["target_user"] == "alice"
                    )
                    if reason:
                        assert not any(command[:2] == ["engine", "stop"] for command in emitted)
                    if label == "stop-fail":
                        assert "stop-sentinel" in event.kwargs["error"]
                        audit.reset_mock()
                        stopped["recovered"] = True
                        assert kill.finish_killed_session(
                            cfg, session, teardown, target_user="alice"
                        )
                        assert not path.exists()
                        assert (
                            len(audit.call_args_list) == 1
                            and audit.call_args.kwargs["outcome"] == "ok"
                        )


def test_telemetry_mixed_direct_managed_and_foreign_nonces_uses_exact_queries(
    tmp_path, monkeypatch, subtests
):
    target = tmp_path / "repo"
    target.mkdir()
    for label in (
        "split",
        "single-visible",
        "denied",
        "empty-markers",
        "empty-stopped",
        "empty-running",
        "unknown-ready",
        "unresolved-stopped",
        "stale-id",
        "stale-epoch",
        "missing-profile",
        "fingerprint",
    ):
        with subtests.test(label=label):
            monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / label))
            spec = _spec(telemetry="cgroup")
            cfg = _cfg(target, spec)
            emitted = []

            def respond(user, command, kwargs, *, emitted=emitted, label=label):
                assert user == "alice"
                emitted.append(command)
                if command[:2] == ["ps", "-eo"]:
                    return _cp(
                        "111 1 4096 3.0\n222 111 2048 1.0\n899 1 64 0\n900 1 8192 50\n901 1 1024 60\n902 1 4096 400\ninvalid\n"
                    )
                if command[:2] == ["engine", "identity"]:
                    assert kwargs["timeout"] == 1.5
                    return _cp(
                        json.dumps(
                            {
                                "id": "other" if label == "stale-id" else "cid-1",
                                "host_pid": 42,
                                "epoch": "other" if label == "stale-epoch" else "epoch-1",
                            }
                        ),
                        rc=int(label == "unresolved-stopped"),
                    )
                if "uxon.infra.runtime_telemetry_probe" in command:
                    assert kwargs["timeout"] == 10
                    mode = command[command.index("--mode") + 1]
                    if mode == "process-cgroup":
                        assert command[-2:] == ["--pid", "42"]
                        return _cp(json.dumps({"ok": True, "cgroup": "/box.scope", "error": ""}))
                    if mode == "cgroup-members":
                        assert command[-2:] == ["--cgroup", "/box.scope"]
                        return _cp(
                            json.dumps(
                                {
                                    "ok": True,
                                    "pids": []
                                    if label in {"empty-stopped", "empty-running", "unknown-ready"}
                                    else [899, 900, 901, 902, 903],
                                    "error": "",
                                }
                            )
                        )
                    assert mode == "markers" and command[-10:] == [
                        "--pid",
                        "899",
                        "--pid",
                        "900",
                        "--pid",
                        "901",
                        "--pid",
                        "902",
                        "--pid",
                        "903",
                    ]
                    return _cp(
                        json.dumps(
                            {
                                "ok": label != "denied",
                                "markers": {}
                                if label in {"denied", "empty-markers"}
                                else {
                                    "900": "a" * 32,
                                    "901": "b" * 32,
                                    "902": "foreign-nonce",
                                    "899": "",
                                },
                                "error": "denied" if label == "denied" else "",
                            }
                        ),
                        rc=int(label == "denied"),
                    )
                assert command[:2] in (["engine", "ready"], ["engine", "exists"])
                assert kwargs["timeout"] == 1.5
                if label == "unknown-ready":
                    raise subprocess.TimeoutExpired(command, 1.5)
                return _cp(rc=int(command[1] == "ready" and label != "empty-running"))

            external = TargetCommands(respond)
            with external.installed_boundary():
                resolved = _resolve(cfg, target)
                managed = []
                for index, nonce in enumerate(("a" * 32, "b" * 32)):
                    pending, metadata, _ = save_launch_record(
                        cfg,
                        resolved,
                        target_dir=str(target),
                        name=f"uxon-managed-{index}@claude",
                        nonce=nonce,
                        session_id=f"${index + 1}",
                    )
                    managed.append(session_from_record(pending, metadata))
                if label == "single-visible":
                    managed = managed[:1]
                if label == "missing-profile":
                    cfg.runtimes.clear()
                elif label == "fingerprint":
                    cfg.runtimes["box"] = dataclasses.replace(spec, id="changed")
                direct = make_session("uxon-direct@claude", user="alice")
                direct.pane_pids = (111, 111)
                sessions_probe.enrich_session_usage(
                    cfg, [direct, *managed], runtimes=cfg.runtimes, launch_user="alice"
                )
            assert (direct.rss_kib, direct.cpu_pct, direct.runtime_down) == (6144, 4.0, False)
            expected = (
                [(8192, 50.0, False), (1024, 60.0, False)]
                if label in {"split", "single-visible"}
                else [(13376, 510.0, False)] * 2
                if label == "denied"
                else [(0, 0.0, label in {"empty-stopped", "unresolved-stopped"})] * 2
            )
            assert [(s.rss_kib, s.cpu_pct, s.runtime_down) for s in managed] == expected[
                : len(managed)
            ]
            assert [(s.runtime_id, s.runtime_epoch, s.runtime_resource) for s in managed] == [
                ("cid-1", "epoch-1", "box-repo")
            ] * len(managed)
            modes = [
                command[command.index("--mode") + 1]
                for command in emitted
                if "uxon.infra.runtime_telemetry_probe" in command
            ]
            stale = label in {"missing-profile", "fingerprint"}
            assert sum(c[:2] == ["engine", "identity"] for c in emitted) == (
                0 if stale else len(managed)
            )
            assert modes.count("process-cgroup") == (
                0 if stale or label == "unresolved-stopped" else len(managed)
            )
            has_group = not stale and label not in {"unresolved-stopped", "stale-id", "stale-epoch"}
            assert modes.count("cgroup-members") == int(has_group)
            assert modes.count("markers") == int(
                label in {"split", "single-visible", "denied", "empty-markers"}
            )
            assert sum(c[:2] == ["engine", "ready"] for c in emitted) == int(
                label in {"empty-stopped", "empty-running", "unknown-ready", "unresolved-stopped"}
            )
            assert sum(c[:2] == ["ps", "-eo"] for c in emitted) == 1


def test_direct_only_telemetry_walks_children_with_one_process_query():
    session = make_session(user="alice")
    session.pane_pids = (111, 111)

    def respond(user, command, kwargs):
        assert user == "alice" and command == ["ps", "-eo", "pid=,ppid=,rss=,%cpu="]
        return _cp("111 1 4096 3\n222 111 2048 1\n999 1 99999 999\n")

    external = TargetCommands(respond)
    with (
        external.installed_boundary(),
        mock.patch(
            "pathlib.Path.read_text",
            side_effect=AssertionError("unexpected filesystem telemetry read"),
        ),
    ):
        sessions_probe.enrich_session_usage(target_config(), [session], launch_user="alice")
    assert (session.rss_kib, session.cpu_pct, session.runtime_down) == (6144, 4, False)
    assert len(external.calls) == 1


def test_runtime_presentation_keeps_agent_direct_command_and_down_status():
    from uxon.app.listing import print_list
    from uxon.domain.session import to_tui_session
    from uxon.tui.dashboard.columns import format_cpu, format_ram
    from uxon.tui.dashboard.row import from_tui_session

    cfg = target_config()
    sessions = []
    for name, resource, down, command, expected in (
        ("direct", "", False, "vim", "vim"),
        ("live", "box", False, "engine", "claude"),
        ("down", "box", True, "engine", "claude"),
    ):
        session = make_session(f"uxon-{name}@claude", user="alice")
        session.agent = "claude"
        session.runtime_resource = resource
        session.runtime_down = down
        session.active_cmd = command
        tui = to_tui_session(session, "uxon-")
        assert tui.cmd == expected
        if down:
            row = from_tui_session(tui)
            assert "down" in format_cpu(row).plain.lower() and "down" in format_ram(row).lower()
        sessions.append(session)
    with redirect_stdout(io.StringIO()) as output:
        print_list(cfg, sessions, ["alice"])
    assert (
        "vim" in output.getvalue()
        and "claude" in output.getvalue()
        and "down" in output.getvalue().lower()
    )


def test_public_doctor_reports_runtime_state_and_definition_warnings(tmp_path, monkeypatch):
    target = tmp_path / "repo"
    target.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = _cfg(target)
    cfg.runtimes.update(
        {
            "basic": _spec(id="basic", identity_command=()),
            "mounted": _spec(
                id="mounted",
                stop_command=("engine", "stop", "{pidfile}"),
                path_map=((str(target), "/work"),),
                create_command=("engine", "compose", "-f", str(target / "compose.yml")),
            ),
            "hardened": _spec(
                id="hardened",
                stop_command=("engine", "stop", "{pidfile}"),
                path_map=((str(target), "/work"),),
                create_command=("engine", "compose", "-f", "/operator/compose.yml"),
            ),
        }
    )
    cfg.launch = LaunchConfig(
        default_profile="basic",
        enabled_profiles=("basic", "mounted", "hardened"),
        profiles={
            name: LaunchProfile(id=name, agent="claude", launch_user="alice", runtime=name)
            for name in ("basic", "mounted", "hardened")
        },
    )

    def respond(user, command, kwargs):
        assert user == "alice"
        if "uxon.infra.tmux_server_probe" in command:
            return _cp(json.dumps({"state": "absent", "sessions": [], "error": ""}))
        assert command[:2] in (["engine", "ready"], ["engine", "exists"])
        return _cp()

    with (
        TargetCommands(respond, installed=("tmux",)).installed_boundary(),
        redirect_stdout(io.StringIO()) as output,
    ):
        assert doctor.do_doctor(cfg, "operator", "alice", str(target), json_output=True) == 0
    rows = json.loads(output.getvalue())["data"]["runtimes"]
    assert [
        (row["runtime"], row["ready"], row["exists"], row["host_agent_absence_expected"])
        for row in rows
    ] == [
        ("basic", "yes", "yes", True),
        ("mounted", "yes", "yes", True),
        ("hardened", "yes", "yes", True),
    ]
    assert any("stop_command" in w for w in rows[0]["warnings"])
    assert any("agent-writable bind mount" in w for w in rows[1]["warnings"])
    assert rows[2]["warnings"] == []
