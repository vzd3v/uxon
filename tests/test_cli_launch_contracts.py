"""Launch consumers exercise real policy, argv construction and one Git lifecycle."""

import dataclasses
import json
import os
import pwd
import subprocess
import sys

import pytest
from helpers import make_config

from uxon.app import launch_profile
from uxon.cli.parsing import parse_args
from uxon.domain.execution import ExecutionBackendSpec, ExecutionConfig
from uxon.domain.launch_profiles import RuntimeContext
from uxon.domain.runtime import WorkloadRuntimeSpec
from uxon.infra import execution, launch_records, probes, tmux

_REAL_REQUIRE_PROBE = execution.require_probe
_REAL_PROBE_HOST = probes.probe_host


@pytest.fixture
def git_audit_sink(tmp_path, monkeypatch):
    import socket

    from uxon.infra import audit

    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sink:
        sink.bind(str(tmp_path / "git-audit.sock"))
        sink.settimeout(2)
        monkeypatch.setattr(audit, "_JOURNAL_SOCKET_PATH", str(tmp_path / "git-audit.sock"))
        monkeypatch.setattr(audit, "_DEV_LOG_PATH", str(tmp_path / "absent-audit.sock"))
        audit.configure(enabled=True, syslog_facility="user", subcmd="run")
        yield sink


@pytest.fixture
def launch_boundary(tmp_path, monkeypatch):
    user = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("UXON_REPEAT_NONINTERACTIVE_POLICY", raising=False)
    root = tmp_path / "projects"
    root.mkdir()
    cfg = make_config(
        allowed_roots=[str(root)],
        new_project_root=str(root),
        tmux_socket_template=str(tmp_path / "sockets" / "{uid}-{execution_backend}.sock"),
        tmux_manage_options=False,
        tmux_options={},
        tmux_server_options={},
        tmux_append_server_options={},
        tmux_set_terminal_title=False,
    )
    calls = []

    def identity(argv, *, timeout):
        assert argv == [sys.executable, "-m", "uxon.infra.execution_probe"]
        assert timeout == cfg.execution.backend_for_user(user).probe_timeout_seconds
        calls.append(("identity", tuple(argv)))
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "euid": os.getuid(),
                    "egid": os.getgid(),
                    "groups": sorted(os.getgrouplist(user, os.getgid())),
                }
            ),
            "",
        )

    def binaries(argv, *, timeout):
        assert timeout == 2.0
        names = ["claude", "codex", "cursor-agent", "custom-bin"]
        scripts = {
            f'for c in tmux {name}; do\n    printf "%s\\t%s\\n" "$c" "$(command -v "$c" 2>/dev/null)"\ndone': name
            for name in names
        }
        assert argv[:2] == ["sh", "-lc"] and argv[2] in scripts
        name = scripts[argv[2]]
        calls.append(("binaries", name))
        return subprocess.CompletedProcess(
            argv, 0, f"tmux\t/usr/bin/tmux\n{name}\t/fake/{name}\n", ""
        )

    def server(argv, *, timeout):
        assert argv[:3] == [sys.executable, "-m", "uxon.infra.tmux_server_probe"]
        assert argv[3:] in [
            ["--socket", str(tmp_path / "sockets" / f"{os.getuid()}-local.sock")],
            ["--default-socket"],
        ]
        assert timeout == cfg.execution.backend_for_user(user).probe_timeout_seconds
        calls.append(("server", tuple(argv)))
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"state": "absent", "sessions": [], "error": ""}), ""
        )

    monkeypatch.setattr(execution, "require_probe", _REAL_REQUIRE_PROBE)
    monkeypatch.setattr(execution, "run_query", identity)
    monkeypatch.setattr(probes, "probe_host", _REAL_PROBE_HOST)
    monkeypatch.setattr(probes, "run_query", binaries)
    monkeypatch.setattr(tmux, "run_query", server)
    yield cfg, user, root, calls


def request(cfg, user, path, *, profile="claude", mode=None, running=False, branch=None):
    resolved = launch_profile.resolve_launch_profile(cfg, user, profile, str(path), mode)
    session = f"uxon-example@{profile}"
    record = launch_records.pending_from_resolved(
        socket_path=tmux.tmux_socket_path(cfg, user),
        session_name=session,
        resolved=resolved,
        target_dir=str(path),
        nonce="a" * 32,
    )
    req = tmux._build_tmux_launch_request(
        str(path),
        session,
        parse_args(["run", "--profile", profile, "--", "argument with spaces", "semi;colon"]),
        cfg,
        branch,
        resolved_profile=resolved,
        pending_record=record,
        server_running=running,
    )
    assert req.managed is not None
    return req, resolved


def workload(req):
    create = list(req.managed.create_cmd)
    start = create.index("uxon.infra.launch_bootstrap")
    separator = create.index("--", start)
    return create[separator + 1 : create.index(";", separator)]


def test_builtin_modes_are_consumed_by_launch_with_safe_default_and_literal_native_flags(
    launch_boundary,
):
    cfg, user, root, _ = launch_boundary
    path = root / ".uxon" / "worktrees" / "feature-auth"
    path.mkdir(parents=True)
    assert set(cfg.agents) == {"claude", "codex", "cursor"}
    expected = {
        "claude": [
            ("normal", [], False),
            ("auto", ["--permission-mode", "auto"], False),
            ("yolo", ["--dangerously-skip-permissions"], True),
        ],
        "codex": [
            ("normal", [], False),
            ("auto", ["--approve-for-me"], False),
            ("yolo", ["--dangerously-bypass-approvals-and-sandbox"], True),
        ],
        "cursor": [("normal", [], False), ("yolo", ["--yolo"], True)],
    }
    for aid, modes in expected.items():
        assert [
            (m.id, list(m.flags), m.dangerous) for m in cfg.agents[aid].permission_modes
        ] == modes
        binary = "cursor-agent" if aid == "cursor" else aid
        for index, (mode, flags, dangerous) in enumerate(modes):
            req, resolved = request(
                cfg,
                user,
                path,
                profile=aid,
                mode=None if index == 0 else mode,
                branch="feature/auth",
            )
            assert resolved.mode_id == mode
            assert workload(req) == [binary, "--", "argument with spaces", "semi;colon", *flags]
            assert req.managed.create_cmd[req.managed.create_cmd.index("-c") + 1] == str(path)
            assert "-w" not in workload(req)
            assert (
                next(m for m in resolved.agent.permission_modes if m.id == mode).dangerous
                is dangerous
            )


def test_rich_launch_preserves_execution_socket_workload_nonce_and_controller_separation(
    launch_boundary,
):
    cfg, user, root, _ = launch_boundary
    path = root / "project"
    path.mkdir()
    cfg.agents["claude"] = dataclasses.replace(
        cfg.agents["claude"], default_args=("--model", "model with spaces")
    )
    req, resolved = request(cfg, user, path, mode="yolo")
    boundary = ExecutionBackendSpec(
        id="boundary", kind="command", command_prefix=("boundary", "--target", "{user}")
    )
    cfg.execution = ExecutionConfig(
        default_backend="boundary",
        backends={"local": ExecutionConfig().backends["local"], "boundary": boundary},
    )
    cfg.launch_record_dir = str(root.parent / "controller-records")
    runtime = WorkloadRuntimeSpec(
        id="isolated",
        kind="command",
        exec_prefix=("workload", "--resource", "{resource}", "--cwd", "{runtime_dir}", "--"),
    )
    cfg.runtimes["isolated"] = runtime
    resolved = dataclasses.replace(
        resolved,
        profile=dataclasses.replace(resolved.profile, runtime="isolated"),
        execution=execution.resolve_target(cfg, user),
        runtime_context=RuntimeContext(
            "isolated", "workload-resource", "/workspace/project", runtime.fingerprint
        ),
    )
    record = launch_records.pending_from_resolved(
        socket_path=tmux.tmux_socket_path(cfg, user),
        session_name="uxon-project@claude",
        resolved=resolved,
        target_dir=str(path),
        nonce="a" * 32,
    )
    req = tmux._build_tmux_launch_request(
        str(path),
        "uxon-project@claude",
        parse_args(["run", "--mode", "yolo", "--extra", "semi;colon"]),
        cfg,
        None,
        resolved_profile=resolved,
        pending_record=record,
    )
    managed = req.managed
    assert managed is not None
    prefix = ("boundary", "--target", user)
    socket = str(root.parent / "sockets" / f"{os.getuid()}-boundary.sock")
    assert managed.create_cmd[:6] == (*prefix, "tmux", "-S", socket)
    assert req.prelaunch == (
        (*prefix, sys.executable, "-m", "uxon.infra.tmux_socket", "--prepare", "--socket", socket),
    )
    assert managed.create_cmd[-6:] == (
        ";",
        "set-window-option",
        "-t",
        "uxon-project@claude",
        "remain-on-exit",
        "failed",
    )
    assert not {"-A", "-As", "-dA", "--record-path"} & set(managed.create_cmd)
    assert cfg.launch_record_dir not in repr(
        (managed.create_cmd, req.prelaunch, managed.release_cmd)
    )
    assert (
        managed.record_dir,
        managed.record_nonce,
        managed.execution_backend,
        managed.runtime_resource,
    ) == (cfg.launch_record_dir, "a" * 32, "boundary", "workload-resource")
    assert managed.release_cmd == (
        *prefix,
        "tmux",
        "-S",
        socket,
        "wait-for",
        "-S",
        "uxon-launch-" + "a" * 32 + "-release",
    )
    argv = workload(req)
    assert argv[:6] == [
        "workload",
        "--resource",
        "workload-resource",
        "--cwd",
        "/workspace/project",
        "--",
    ]
    assert argv[-6:] == [
        "claude",
        "--model",
        "model with spaces",
        "--extra",
        "semi;colon",
        "--dangerously-skip-permissions",
    ]
    assert "--pidfile" not in argv


def test_native_options_apply_to_create_birth_live_off_empty_and_nested_attach(
    launch_boundary, monkeypatch
):
    cfg, user, root, _ = launch_boundary
    path = root / "project"
    path.mkdir()
    cfg.tmux_manage_options = True
    cfg.tmux_options = {"mouse": False, "history-limit": 5}
    cfg.tmux_server_options = {"extended-keys": "on"}
    cfg.tmux_append_server_options = {"terminal-features": "xterm*:extkeys"}
    live = [
        "set",
        "-g",
        "mouse",
        "off",
        ";",
        "set",
        "-g",
        "history-limit",
        "5",
        ";",
        "set",
        "-s",
        "extended-keys",
        "on",
        ";",
    ]
    birth = live + ["set", "-as", "terminal-features", "xterm*:extkeys", ";"]
    for nested in (False, True):
        socket = tmux.tmux_socket_path(cfg, user)
        monkeypatch.setenv("TMUX", f"{socket},123,0" if nested else "")
        for running, expected in ((False, birth), (True, live)):
            req, _ = request(cfg, user, path, running=running)
            create = list(req.managed.create_cmd)
            assert create[3 : create.index("new-session")] == expected
            assert ("switch-client" if nested else "attach-session") in req.cmd
            assert "set" not in req.cmd
        from helpers import make_session

        target = make_session("uxon-example@claude", user=user)
        attached = tmux._build_tmux_attach_request(target, cfg, user)
        assert attached.managed is None and attached.prelaunch == ()
        assert list(attached.cmd)[3 : 3 + len(live)] == live
        assert ("switch-client" if nested else "attach-session") in attached.cmd
    import io
    from contextlib import redirect_stdout

    from uxon.app import session_handoff

    monkeypatch.delenv("TMUX")
    resolved = launch_profile.resolve_launch_profile(cfg, user, "claude", str(path), "normal")
    with monkeypatch.context() as scoped, redirect_stdout(io.StringIO()) as output:
        scoped.setattr(
            subprocess, "call", lambda *args, **kwargs: pytest.fail("dry-run spawned client")
        )
        assert (
            session_handoff.launch_in_tmux(
                str(path),
                "uxon-project@claude",
                parse_args(["run", "--dry-run"]),
                cfg,
                None,
                resolved_profile=resolved,
            )
            == 0
        )
    assert "set -g mouse off" in output.getvalue()
    assert "set -g history-limit 5" in output.getvalue()
    assert "set -as terminal-features" in output.getvalue()
    cfg.tmux_manage_options = False
    off, _ = request(cfg, user, path)
    cfg.tmux_manage_options = True
    cfg.tmux_options = cfg.tmux_server_options = cfg.tmux_append_server_options = {}
    empty, _ = request(cfg, user, path)
    assert off.managed.create_cmd == empty.managed.create_cmd
    assert "set" not in off.managed.create_cmd
    monkeypatch.setenv("TMUX", "/foreign/socket,123,0")
    with pytest.raises(SystemExit) as caught:
        request(cfg, user, path)
    assert "different socket" in caught.value.uxon_msg


def test_unknown_mode_fails_in_public_resolver_before_any_workload(launch_boundary):
    cfg, user, root, calls = launch_boundary
    path = root / "project"
    path.mkdir()
    args = parse_args(["run", "--profile", "cursor", "--mode", "auto", "--dry-run"])
    with pytest.raises(SystemExit) as caught:
        launch_profile.resolve_launch_profile(
            cfg, user, args.profile, str(path), args.permission_mode
        )
    assert (
        "unknown --mode 'auto' for agent 'cursor'; valid modes: normal, yolo"
        == caught.value.uxon_msg
    )
    assert [call[0] for call in calls] == ["identity"]


def test_public_new_remote_selection_dry_run_and_policy_refusals(
    launch_boundary, monkeypatch, capsys
):
    from uxon.app import new
    from uxon.domain.git_profiles import GitRemoteProfile
    from uxon.domain.launch_profiles import GitRemotePolicy, LaunchPathRule
    from uxon.gitremote import backend_gh
    from uxon.infra import process

    cfg, user, root, _ = launch_boundary
    cfg.git_create_enabled = True
    cfg.git_remote_profiles = [
        GitRemoteProfile("work", "git.example.com", "acme", "gh", "", "", "private"),
        GitRemoteProfile("personal", "github.com", "alice", "gh", "", "", "private"),
    ]
    profiles = dict(cfg.launch.profiles)
    profiles["claude"] = dataclasses.replace(
        profiles["claude"], git_remote=GitRemotePolicy(("work", "personal"), "personal")
    )
    cfg.launch = dataclasses.replace(
        cfg.launch,
        profiles=profiles,
        path_rules=(LaunchPathRule(str(root), ("claude",), "claude", ("work",), "work"),),
    )
    target = root / "demo"
    expected = [
        ["test", "-d", str(target / ".git")],
        ["sh", "-c", "command -v gh"],
        ["gh", "auth", "status", "--hostname", "git.example.com"],
        ["gh", "repo", "view", "git.example.com/acme/demo", "--json", "name"],
    ]
    calls = []

    def remote(argv, *, timeout=20):
        assert timeout == 20 and argv == expected[len(calls)]
        calls.append(argv)
        index = len(calls) - 1
        return backend_gh.RunResult(
            1 if index in (0, 3) else 0, "/usr/bin/gh\n" if index == 1 else "", ""
        )

    monkeypatch.setattr(backend_gh, "run_query", remote)
    monkeypatch.setattr(process, "run_cmd", lambda *a, **k: pytest.fail("dry-run mutation"))
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: pytest.fail("dry-run client"))
    monkeypatch.chdir(root.parent)
    from uxon.tui import context_builder

    ctx = context_builder.build_tui_context(cfg, user, user, str(root.parent), skeleton=True)
    assert ctx.on_git_remote_options("demo", "claude", "normal") == (
        [("work", f"git.example.com/acme  via {user} [gh]")],
        "work",
    )
    for selector in ("work", "default"):
        calls.clear()
        args = parse_args(
            [
                "new",
                "demo",
                "--profile",
                "claude",
                "--git-remote",
                selector,
                "--git-visibility",
                "public",
                "--dry-run",
            ]
        )
        assert new.do_new(args, cfg, user) == 0
        output = capsys.readouterr().out
        assert calls == expected
        assert "git.example.com/acme/demo --public" in output
        assert "git@ git.example.com" not in output
        assert "git-remote ssh_url=git@git.example.com:acme/demo.git" in output
        assert "uxon-demo@claude" in output
        assert not target.exists()
    for argv, enabled, diagnostic in [
        (["new", "demo", "--git-remote", "work", "--dry-run"], False, "git_create_enabled=false"),
        (
            ["new", "demo", "-w", "feature", "--git-remote", "work", "--dry-run"],
            True,
            "cannot combine --git-remote with -w",
        ),
    ]:
        cfg.git_create_enabled = enabled
        calls.clear()
        with pytest.raises(SystemExit) as caught:
            new.do_new(parse_args(argv), cfg, user)
        assert diagnostic in caught.value.uxon_msg
        assert calls == [] and not target.exists()


def test_target_gates_use_real_filesystem_and_preserve_no_mutation(launch_boundary, monkeypatch):
    from uxon.app import launch, new
    from uxon.domain.authz import is_under_allowed_roots

    cfg, user, root, _ = launch_boundary
    target = root / "project"
    target.mkdir()
    outside = root.parent / "outside"
    outside.mkdir()
    missing = root / "missing"
    assert launch.is_launch_target_allowed(cfg, user, str(target))
    for path in (outside, missing):
        assert not launch.is_launch_target_allowed(cfg, user, str(path))
        with pytest.raises(SystemExit):
            launch.ensure_launch_target_allowed(cfg, user, str(path))
    assert is_under_allowed_roots(cfg, str(target))
    assert not is_under_allowed_roots(cfg, str(outside))
    cfg.allowed_roots = []
    assert is_under_allowed_roots(cfg, str(outside))
    assert launch.is_launch_target_allowed(cfg, user, str(outside))
    actual_access = os.access
    with monkeypatch.context() as scoped:
        scoped.setattr(
            os,
            "access",
            lambda path, mode, **kwargs: (
                False if str(path) == str(root) else actual_access(path, mode, **kwargs)
            ),
        )
        assert not new.is_new_project_target_allowed(cfg, user, str(missing))
        with pytest.raises(SystemExit) as caught:
            new.do_new(parse_args(["new", "missing", "--dry-run"]), cfg, user)
        assert "no write access" in caught.value.uxon_msg
    cfg.allowed_roots = [str(root)]
    cfg.new_project_root = str(outside)
    with pytest.raises(SystemExit) as caught:
        new.do_new(parse_args(["new", "denied", "--dry-run"]), cfg, user)
    assert "got:" in caught.value.uxon_msg
    assert not (outside / "denied").exists() and not missing.exists()


def test_public_main_missing_tmux_agent_and_list_without_agents(
    launch_boundary, monkeypatch, capsys
):
    from pathlib import Path

    from uxon.cli.main import main
    from uxon.infra import config_loader
    from uxon.infra.config_renderer import render_config

    cfg, user, root, _ = launch_boundary
    cfg.audit_enabled = False
    cfg.default_launch_mode = "fixed"
    cfg.default_launch_user = user
    cfg.session_users = [user]
    operator = root.parent / "operator.toml"
    operator.write_text(
        render_config(
            dict(
                default_launch_mode="fixed",
                default_launch_user=user,
                session_users=[user],
                new_project_root=str(root),
                allowed_roots=[str(root)],
                audit={"enabled": False},
                tmux_socket_template=cfg.tmux_socket_template,
            )
        )
    )
    monkeypatch.setattr(config_loader, "OPERATOR_CONFIG_PATH", operator)
    monkeypatch.chdir(root)
    real_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *a, **k: (
            str(os.getuid()) if path == Path("/proc/self/loginuid") else real_read(path, *a, **k)
        ),
    )
    calls = []
    state = {"tmux": False, "claude": True}

    def discovery(argv, *, timeout):
        names = (
            ["tmux", "claude"]
            if argv[2].startswith("for c in tmux claude;")
            else ["tmux", "claude", "codex", "cursor-agent"]
        )
        script = (
            "for c in "
            + " ".join(names)
            + '; do\n    printf "%s\\t%s\\n" "$c" "$(command -v "$c" 2>/dev/null)"\ndone'
        )
        assert argv == ["sh", "-lc", script] and timeout == 2.0
        calls.append(names)
        return subprocess.CompletedProcess(
            argv,
            0,
            "".join(
                f"{name}\t{('/fake/' + name) if state.get(name, False) else ''}\n" for name in names
            ),
            "",
        )

    monkeypatch.setattr(probes, "run_query", discovery)
    with pytest.raises(SystemExit) as caught:
        main(["run", "--profile", "claude", "--dry-run"])
    assert "tmux is not installed" in caught.value.uxon_msg
    state.update(tmux=True, claude=False)
    with pytest.raises(SystemExit) as caught:
        main(["run", "--profile", "claude", "--dry-run"])
    assert "claude" in caught.value.uxon_msg and "not installed" in caught.value.uxon_msg
    capsys.readouterr()
    assert main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["sessions"] == []
    assert calls == [
        ["tmux", "claude"],
        ["tmux", "claude"],
        ["tmux", "claude", "codex", "cursor-agent"],
    ]


def test_project_listing_reads_one_real_tree_and_handles_absent_root(launch_boundary):
    import re

    from uxon.app.project_browsing import list_project_directories

    cfg, user, root, _ = launch_boundary
    for name in ("beta", "alpha", ".hidden"):
        (root / name).mkdir()
    (root / "file").write_text("sentinel")
    (root / "link").symlink_to(root / "alpha", target_is_directory=True)
    entries = list_project_directories(cfg, user, str(root))
    assert [name for name, _ in entries] == ["alpha", "beta"]
    assert all(re.fullmatch(r"\d\d[:-]\d\d", stamp) for _, stamp in entries)
    assert list_project_directories(cfg, user, str(root / "absent")) == []


@pytest.fixture
def session_boundary(launch_boundary, monkeypatch):
    from uxon.infra import sessions_probe

    cfg, user, root, calls = launch_boundary
    path = root / "project"
    path.mkdir()
    socket = tmux.tmux_socket_path(cfg, user)
    rows = []
    paths = {}

    def add(name, profile, path, *, dead=False):
        index = len(rows) + 1
        resolved = launch_profile.resolve_launch_profile(cfg, user, profile, str(path), "normal")
        pending = launch_records.pending_from_resolved(
            socket_path=socket,
            session_name=name,
            resolved=resolved,
            target_dir=str(path),
            nonce=str(index) * 32,
        )
        metadata = launch_records.TmuxSessionMetadata(
            f"${index}", "123", name, pending.launch_nonce
        )
        launch_records.create_pending_record(pending)
        launch_records.finalize_pending_record(pending, metadata)
        rows.append(
            f"{name}\t${index}\t{1 if index == 1 else 0}\t1\t123\t456\t{pending.launch_nonce}\t\t"
        )
        paths[f"${index}"] = [str(path), dead]

    add("uxon-project@claude", "claude", path)
    add("uxon-project@codex", "codex", path)
    add("uxon-other@claude", "claude", root)
    add("uxon-project@claude-2", "claude", path, dead=True)

    def server(argv, *, timeout):
        assert argv == [sys.executable, "-m", "uxon.infra.tmux_server_probe", "--socket", socket]
        assert timeout == cfg.execution.backend_for_user(user).probe_timeout_seconds
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {"state": "running", "sessions": [row.split("\t")[0] for row in rows], "error": ""}
            ),
            "",
        )

    def query(argv, *, check=False, timeout):
        assert (
            check is False and timeout == cfg.execution.backend_for_user(user).probe_timeout_seconds
        )
        base = ["tmux", "-S", socket]
        session_format = "#{session_name}\t#{session_id}\t#{session_attached}\t#{session_windows}\t#{session_created}\t#{session_activity}\t#{E:UXON_LAUNCH_NONCE}\t#{E:UXON_RUNTIME_RESOURCE}\t#{E:UXON_RUNTIME_CGROUP}"
        pane_format = "#{pane_active}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}\t#{window_active}\t#{pane_id}\t#{pane_dead}\t#{pane_dead_status}\t#{@uxon-dismissed}"
        if argv == base + ["list-sessions", "-F", session_format]:
            output = "\n".join(rows)
        else:
            assert argv[:6] == base + ["list-panes", "-s", "-t"]
            ident = argv[6]
            assert ident in paths and argv[7:] == ["-F", pane_format]
            target, dead = paths[ident]
            output = (
                f"1\t999999\tclaude\t{target}\t1\t%{ident[1:]}\t{int(dead)}\t{7 if dead else ''}\t"
            )
        calls.append(("snapshot", tuple(argv)))
        return subprocess.CompletedProcess(argv, 0, output, "")

    def usage(argv, **kwargs):
        assert argv == ["ps", "-eo", "pid=,ppid=,rss=,%cpu="] and kwargs == {}
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tmux, "run_query", server)
    monkeypatch.setattr(sessions_probe, "run_cmd", query)
    monkeypatch.setattr(sessions_probe, "run_query", usage)
    yield cfg, user, root, path, rows, paths, add


def test_actual_probe_and_planner_share_verified_compatibility_and_path_conflicts(session_boundary):
    from uxon.infra import sessions_probe
    from uxon.tui import context_builder
    from uxon.tui.context import CallbackError

    cfg, user, root, path, rows, paths, add = session_boundary
    profiles = dict(cfg.launch.profiles)
    profiles["claude"] = dataclasses.replace(profiles["claude"], launch_user=user)
    cfg.launch = dataclasses.replace(cfg.launch, profiles=profiles)
    from uxon.infra.remote_hosts import RemoteHost

    cfg.remote_hosts = [RemoteHost("peer1", "peer1", "", "uxon")]
    ctx = context_builder.build_tui_context(cfg, "bob", "bob", str(path), skeleton=True)
    assert ctx.loading is True and ctx.tmux_set_terminal_title is False
    assert {"main_ctx_rebuild", "remote:peer1"} <= {s.name for s in ctx.refresh_sources}
    found = sessions_probe.probe_tui_compatible_sessions(cfg, user, str(path), "claude")
    assert [(s.name, s.agent, s.launch_record_verified, s.attached) for s in found] == [
        ("uxon-project@claude", "claude", True, "1")
    ]
    assert ctx.on_probe_existing_sessions(str(path), "claude", "normal") == (
        (user, "uxon-project@claude", True),
    )
    planned = ctx.on_launch_cwd("claude", "normal", str(path))
    assert planned.managed.record_session == "uxon-project@claude-3"
    assert planned.managed.project == str(path) and planned.managed.launch_user == user
    assert ctx.on_runtime_gate(str(path), "claude", "normal") is None
    wt = root / "myapp" / ".uxon" / "worktrees" / "feature-auth"
    wt.mkdir(parents=True)
    add("uxon-myapp-feature-auth@claude", "claude", wt)
    add("uxon-second-feature-auth@claude", "claude", path)
    assert ctx.on_probe_existing_worktree_sessions(
        str(wt), str(root / "myapp"), "feature/auth", "claude", "normal"
    ) == ((user, "uxon-myapp-feature-auth@claude", False),)
    planned = ctx.on_launch_existing_worktree(
        str(root / "myapp"), "feature/auth", str(wt), "claude", "normal"
    )
    assert planned.managed.record_session == "uxon-myapp-feature-auth@claude-2"
    assert (
        ctx.on_probe_existing_worktree_sessions(
            str(wt), str(root / "myapp"), "myapp", "claude", "normal"
        )
        == ()
    )
    assert (
        ctx.on_probe_existing_worktree_sessions(
            str(wt), str(root / "other"), "feature/auth", "claude", "normal"
        )
        == ()
    )
    assert (
        ctx.on_launch_existing_worktree(
            str(root / "myapp"), "myapp", str(wt), "claude", "normal"
        ).managed.record_session
        == "uxon-myapp-myapp@claude"
    )
    assert (
        ctx.on_launch_existing_worktree(
            str(root / "other"), "feature/auth", str(wt), "claude", "normal"
        ).managed.record_session
        == "uxon-other-feature-auth@claude"
    )
    for index in ("$1", "$4"):
        old = paths[index][0]
        paths[index][0] = str(root)
        with pytest.raises(CallbackError, match="session conflict.*not under"):
            ctx.on_probe_existing_sessions(str(path), "claude", "normal")
        paths[index][0] = old


def test_public_new_repeat_tty_environment_and_force_precedence(
    session_boundary, monkeypatch, capsys
):
    import io

    from uxon.app import new
    from uxon.infra import process

    cfg, user, root, path, _, _, _ = session_boundary
    monkeypatch.chdir(root.parent)
    monkeypatch.setattr(
        process, "run_cmd", lambda *a, **k: pytest.fail("dry-run mkdir or mutation")
    )
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: pytest.fail("dry-run client"))
    for interactive, override, explicit, policy, expected in [
        (True, None, [], "ask", "attach-session"),
        (True, None, ["--new-session"], "ask", "uxon-project@claude-3"),
        (False, "attach", [], "new", "attach-session"),
        (False, "attach", ["--new-session"], "ask", "uxon-project@claude-3"),
    ]:
        cfg.repeat_noninteractive_mode = policy

        class Terminal(io.StringIO):
            def isatty(self, interactive=interactive):
                return interactive

        monkeypatch.setattr(sys, "stdin", Terminal("\n"))
        monkeypatch.setattr(sys, "stdout", Terminal())
        if override:
            monkeypatch.setenv("UXON_REPEAT_NONINTERACTIVE_POLICY", override)
        else:
            monkeypatch.delenv("UXON_REPEAT_NONINTERACTIVE_POLICY", raising=False)
        assert (
            new.do_new(
                parse_args(["new", "project", "--profile", "claude", "--dry-run", *explicit]),
                cfg,
                user,
            )
            == 0
        )
        assert expected in sys.stdout.getvalue()
        assert ("compatible sessions already exist" in sys.stdout.getvalue()) is (
            interactive and not explicit
        )
    cfg.repeat_noninteractive_mode = "ask"
    monkeypatch.delenv("UXON_REPEAT_NONINTERACTIVE_POLICY", raising=False)
    with pytest.raises(SystemExit) as caught:
        new.do_new(parse_args(["new", "project", "--profile", "claude", "--dry-run"]), cfg, user)
    assert "no interactive TTY" in caught.value.uxon_msg
    assert path.is_dir() and not (path / ".git").exists()


def test_public_new_refuses_compatible_unverified_legacy_default_socket(
    launch_boundary, monkeypatch
):
    from uxon.app import new
    from uxon.infra import sessions_probe

    cfg, user, root, _ = launch_boundary
    path = root / "project"
    path.mkdir()
    normal_server = tmux.run_query

    def server(argv, *, timeout):
        if argv[-1] != "--default-socket":
            return normal_server(argv, timeout=timeout)
        assert argv == [sys.executable, "-m", "uxon.infra.tmux_server_probe", "--default-socket"]
        assert timeout == cfg.execution.backend_for_user(user).probe_timeout_seconds
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({"state": "running", "sessions": ["uxon-project@claude"], "error": ""}),
            "",
        )

    calls = []

    def rows(argv, *, timeout, check=False):
        assert (
            check is False and timeout == cfg.execution.backend_for_user(user).probe_timeout_seconds
        )
        if argv[:2] == ["tmux", "list-sessions"]:
            assert argv[2:] == [
                "-F",
                "#{session_name}\t#{session_id}\t#{session_attached}\t#{session_windows}\t#{session_created}\t#{session_activity}\t#{E:UXON_LAUNCH_NONCE}\t#{E:UXON_RUNTIME_RESOURCE}\t#{E:UXON_RUNTIME_CGROUP}",
            ]
            output = "uxon-project@claude\t$1\t0\t1\t123\t456\t\t\t"
        else:
            assert argv == [
                "tmux",
                "list-panes",
                "-s",
                "-t",
                "$1",
                "-F",
                "#{pane_active}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}\t#{window_active}\t#{pane_id}\t#{pane_dead}\t#{pane_dead_status}\t#{@uxon-dismissed}",
            ]
            output = f"1\t999999\tclaude\t{path}\t1\t%1\t0\t\t"
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, output, "")

    def ps(argv, **kw):
        assert argv == ["ps", "-eo", "pid=,ppid=,rss=,%cpu="] and kw == {}
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tmux, "run_query", server)
    monkeypatch.setattr(sessions_probe, "run_cmd", rows)
    monkeypatch.setattr(sessions_probe, "run_query", ps)
    with pytest.raises(SystemExit) as caught:
        new.do_new(parse_args(["new", "project", "--profile", "claude", "--dry-run"]), cfg, user)
    assert "compatible session(s) exist on the legacy default tmux socket" in caught.value.uxon_msg
    assert tmux.tmux_socket_path(cfg, user) in caught.value.uxon_msg
    assert "tmux attach-session -t uxon-project@claude" in caught.value.uxon_msg
    assert len(calls) == 2 and list(path.iterdir()) == []


def test_public_list_and_destructive_json_dry_empty_and_refusal(
    session_boundary, monkeypatch, capsys
):
    import io

    from uxon import __version__
    from uxon.app import kill, listing
    from uxon.infra.remote.envelope import decode_envelope

    cfg, user, _, _, rows, _, _ = session_boundary
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: pytest.fail("mutation"))
    assert listing.do_list(parse_args(["list", "--json"]), cfg, user) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert (envelope["schema_version"], envelope["uxon_version"], envelope["kind"]) == (
        "3",
        __version__,
        "list",
    )
    assert envelope["data"]["scope_users"] == [user]
    decoded = decode_envelope(json.dumps(envelope))
    assert decoded.error is None
    records = envelope["data"]["sessions"]
    assert [
        (r["name"], r["profile"], r["agent"], r["runtime"], r["active_path"])
        for r in sorted(records, key=lambda r: r["name"])
    ] == [
        ("uxon-other@claude", "claude", "claude", "direct", str(session_boundary[2])),
        ("uxon-project@claude", "claude", "claude", "direct", str(session_boundary[3])),
        ("uxon-project@claude-2", "claude", "claude", "direct", str(session_boundary[3])),
        ("uxon-project@codex", "codex", "codex", "direct", str(session_boundary[3])),
    ]
    assert (
        kill.do_kill(parse_args(["kill", "project@codex", "--json", "--dry-run"]), cfg, user) == 0
    )
    assert json.loads(capsys.readouterr().out)["data"] == {
        "target": "uxon-project@codex",
        "user": user,
        "socket": tmux.tmux_socket_path(cfg, user),
        "action": "would-kill",
        "dry_run": True,
    }
    assert kill.do_kill_all(parse_args(["kill-all", "--json", "--dry-run"]), cfg, user) == 0
    assert json.loads(capsys.readouterr().out)["data"] == {
        "user": user,
        "socket": tmux.tmux_socket_path(cfg, user),
        "dry_run": True,
        "sessions": [
            {"name": r.split("\t")[0], "action": "would-kill", "cleanup_outcome": "not_run"}
            for r in rows
        ],
    }
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("JSON prompt"))
    with pytest.raises(SystemExit) as caught:
        kill.do_kill_all(parse_args(["kill-all"]), cfg, user)
    assert "kill-all is destructive; rerun with --force" in caught.value.uxon_msg
    for tty in (False, True):

        class Terminal(io.StringIO):
            def isatty(self, tty=tty):
                return tty

        with monkeypatch.context() as scoped:
            scoped.setattr(sys, "stdin", Terminal())
            scoped.setattr(sys, "stdout", Terminal())
            with pytest.raises(SystemExit) as caught:
                kill.do_kill_all(parse_args(["kill-all", "--json"]), cfg, user)
            assert caught.value.uxon_msg == "kill-all --json requires --force or --dry-run"
            assert sys.stdout.getvalue() == ""
    rows.clear()
    assert kill.do_kill_all(parse_args(["kill-all", "--json"]), cfg, user) == 0
    assert json.loads(capsys.readouterr().out)["data"]["sessions"] == []


def test_one_git_tree_covers_include_intersection_planners_and_refusals(
    launch_boundary, monkeypatch, git_audit_sink
):
    import io
    from contextlib import redirect_stdout

    from uxon.app import launch, run
    from uxon.infra import git
    from uxon.infra.run import run_query
    from uxon.infra.worktrees import Workspace
    from uxon.tui import context_builder
    from uxon.tui.context import CallbackError

    cfg, user, root, calls = launch_boundary
    repo = root / "myapp"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, timeout=10)

    def git_command(*args):
        cp = subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, timeout=10
        )
        return cp.stdout

    git_command("config", "user.email", "alice@example.com")
    git_command("config", "user.name", "Alice")
    destination = repo / ".uxon" / "copy-check"
    destination.mkdir(parents=True)
    git.copy_worktreeinclude_matches(cfg, str(repo), str(destination), user)
    assert list(destination.iterdir()) == []
    (repo / "tracked.txt").write_text("tracked-sentinel")
    (repo / ".gitignore").write_text(".env\n*.log\nnested/private\n")
    (repo / ".worktreeinclude").write_text(".env\ntracked.txt\nuntracked.txt\nnested/private\n")
    (repo / ".env").write_text("ENV=sentinel\n")
    (repo / "debug.log").write_text("ignored-not-included")
    (repo / "untracked.txt").write_text("included-not-ignored")
    (repo / "nested").mkdir()
    (repo / "nested" / "private").write_bytes(b"nested\x00sentinel")
    git_command("add", "tracked.txt", ".gitignore", ".worktreeinclude")
    git_command("commit", "-qm", "Initial files")
    git.copy_worktreeinclude_matches(cfg, str(repo), str(destination), user)
    assert {
        str(p.relative_to(destination)): p.read_bytes()
        for p in destination.rglob("*")
        if p.is_file()
    } == {".env": b"ENV=sentinel\n", "nested/private": b"nested\x00sentinel"}
    exclude = repo / ".git" / "info" / "exclude"
    before_exclude = exclude.read_bytes()
    git.write_uxon_exclude_entry(cfg, str(repo), user)
    git.write_uxon_exclude_entry(cfg, str(repo), user)
    assert exclude.read_bytes() == before_exclude + b".uxon/\n"
    assert ".uxon/" not in git_command("status", "--porcelain")

    def snapshot():
        return (
            git_command("worktree", "list", "--porcelain"),
            git_command("show-ref", "--heads"),
            exclude.read_bytes(),
            sorted(str(p.relative_to(repo)) for p in repo.rglob("*")),
        )

    def resolve(branch):
        target = repo / ".uxon" / "worktrees" / branch.replace("/", "-")
        return launch_profile.resolve_launch_profile(
            cfg, user, "claude", str(target), "normal", target_may_not_exist=True
        )

    monkeypatch.chdir(repo)
    original = snapshot()
    output = io.StringIO()
    with redirect_stdout(output):
        assert (
            run.do_run(
                parse_args(
                    [
                        "run",
                        "-w",
                        "feature/auth",
                        "--dry-run",
                        "--profile",
                        "claude",
                        "--extra",
                        "value with spaces",
                    ]
                ),
                cfg,
                user,
            )
            == 0
        )
    assert snapshot() == original
    assert f"worktree_path={repo}/.uxon/worktrees/feature-auth" in output.getvalue()
    assert "-b feature/auth HEAD" in output.getvalue()
    assert "uxon-myapp-feature-auth@claude" in output.getvalue()
    req = launch.plan_worktree_launch(
        cfg,
        user,
        resolve("feature/auth"),
        str(repo),
        "feature/auth",
        requested_profile="claude",
        agent_args=["--extra", "value with spaces"],
    )
    wt = repo / ".uxon" / "worktrees" / "feature-auth"
    assert req.managed.record_session == "uxon-myapp-feature-auth@claude"
    assert req.managed.project == str(wt)
    assert workload(req) == ["claude", "--extra", "value with spaces"]
    assert (wt / ".env").read_bytes() == b"ENV=sentinel\n"
    assert (wt / "nested" / "private").read_bytes() == b"nested\x00sentinel"
    assert (wt / "tracked.txt").read_bytes() == b"tracked-sentinel"
    assert not (wt / "debug.log").exists() and not (wt / "untracked.txt").exists()
    packets = []
    while True:
        packet = git_audit_sink.recv(65536)
        packets.append(packet)
        if b"EVENT=worktree.create\n" in packet:
            break
    assert not any(b"EVENT=worktree.create\n" in p for p in packets[:-1])
    for field in [
        b"PROFILE=claude\n",
        b"AGENT=claude\n",
        f"PROJECT={repo}\n".encode(),
        b"BRANCH=feature/auth\n",
        f"PATH={wt}\n".encode(),
        b"BASE=local\n",
        b"SESSION=uxon-myapp-feature-auth@claude\n",
    ]:
        assert field in packets[-1]
    assert git.git_repo_root_nonint_as_user(cfg, str(wt), user) == str(wt)
    assert git.git_common_dir_root_as_user(cfg, str(wt), user) == str(repo)
    assert git.git_repo_root_nonint_as_user(cfg, str(root), user) is None
    from uxon.app import new
    from uxon.infra import sessions_probe

    resolved = launch_profile.resolve_launch_profile(cfg, user, "claude", str(wt), "normal")
    socket = tmux.tmux_socket_path(cfg, user)
    pending = launch_records.pending_from_resolved(
        socket_path=socket,
        session_name="uxon-myapp-feature-auth@claude",
        resolved=resolved,
        target_dir=str(wt),
        nonce="z" * 32,
    )
    launch_records.create_pending_record(pending)
    launch_records.finalize_pending_record(
        pending,
        launch_records.TmuxSessionMetadata(
            "$10", "123", pending.session_name, pending.launch_nonce
        ),
    )

    def active_server(argv, *, timeout):
        assert argv == [sys.executable, "-m", "uxon.infra.tmux_server_probe", "--socket", socket]
        assert timeout == cfg.execution.backend_for_user(user).probe_timeout_seconds
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({"state": "running", "sessions": [pending.session_name], "error": ""}),
            "",
        )

    def worktree_snapshot(argv, *, check=False, timeout):
        assert (
            check is False and timeout == cfg.execution.backend_for_user(user).probe_timeout_seconds
        )
        base = ["tmux", "-S", socket]
        if argv[:4] == base + ["list-sessions"]:
            assert argv[4:] == [
                "-F",
                "#{session_name}\t#{session_id}\t#{session_attached}\t#{session_windows}\t#{session_created}\t#{session_activity}\t#{E:UXON_LAUNCH_NONCE}\t#{E:UXON_RUNTIME_RESOURCE}\t#{E:UXON_RUNTIME_CGROUP}",
            ]
            output = f"{pending.session_name}\t$10\t0\t1\t123\t456\t{pending.launch_nonce}\t\t"
        else:
            assert argv == base + [
                "list-panes",
                "-s",
                "-t",
                "$10",
                "-F",
                "#{pane_active}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}\t#{window_active}\t#{pane_id}\t#{pane_dead}\t#{pane_dead_status}\t#{@uxon-dismissed}",
            ]
            output = f"1\t999999\tclaude\t{wt}\t1\t%10\t0\t\t"
        return subprocess.CompletedProcess(argv, 0, output, "")

    def usage(argv, **kwargs):
        assert argv == ["ps", "-eo", "pid=,ppid=,rss=,%cpu="] and kwargs == {}
        return subprocess.CompletedProcess(argv, 0, "", "")

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    before_repeat = snapshot()
    with monkeypatch.context() as scoped:
        scoped.setattr(tmux, "run_query", active_server)
        scoped.setattr(sessions_probe, "run_cmd", worktree_snapshot)
        scoped.setattr(sessions_probe, "run_query", usage)
        scoped.setattr(sys, "stdin", Terminal("\n"))
        scoped.setattr(sys, "stdout", Terminal())
        assert (
            new.do_new(
                parse_args(
                    ["new", "myapp", "-w", "feature/auth", "--profile", "claude", "--dry-run"]
                ),
                cfg,
                user,
            )
            == 0
        )
        assert "attach-session -t uxon-myapp-feature-auth@claude" in sys.stdout.getvalue()
        scoped.setattr(sys, "stdin", io.StringIO())
        cfg.repeat_noninteractive_mode = "new"
        with pytest.raises(SystemExit) as caught:
            new.do_new(
                parse_args(
                    ["new", "myapp", "-w", "feature/auth", "--profile", "claude", "--dry-run"]
                ),
                cfg,
                user,
            )
        assert "path already exists or git refused" in caught.value.uxon_msg
    assert snapshot() == before_repeat
    packet = git_audit_sink.recv(65536)
    assert b"EVENT=session.attach.dispatch\n" in packet
    assert b"SESSION=uxon-myapp-feature-auth@claude\n" in packet
    assert b"PROFILE=claude\n" in packet and b"AGENT=claude\n" in packet
    git_command("branch", "existing", "HEAD")
    existing_req = launch.plan_worktree_launch(
        cfg, user, resolve("existing"), str(repo), "existing", requested_profile="claude"
    )
    existing = repo / ".uxon" / "worktrees" / "existing"
    assert existing_req.managed.record_session == "uxon-myapp-existing@claude"
    assert git_command("branch", "--list", "existing").strip() == "+ existing"
    assert b"BRANCH=existing\n" in git_audit_sink.recv(65536)
    ctx = context_builder.build_tui_context(cfg, user, user, str(repo), skeleton=True)
    assert ctx.on_probe_worktrees(str(repo), "claude", "normal") == [
        Workspace("main", "main", str(repo), True),
        Workspace("existing", "existing", str(existing), False),
        Workspace("feature/auth", "feature/auth", str(wt), False),
    ]
    assert ctx.on_probe_worktrees(str(root), "claude", "normal") == []
    actual_query = run_query

    def rejected_list(argv, **kwargs):
        if list(argv) == ["git", "-C", str(repo), "worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(argv, 128, "", "worktree-list-sentinel")
        return actual_query(argv, **kwargs)

    import uxon.tui.bridge as bridge

    with monkeypatch.context() as scoped:
        scoped.setattr(bridge, "run_query", rejected_list)
        with pytest.raises(CallbackError, match="worktree-list-sentinel"):
            ctx.on_probe_worktrees(str(repo), "claude", "normal")
    original = snapshot()
    cfg.worktree_root = str(root / "relocated")
    with pytest.raises(SystemExit) as caught:
        launch.plan_worktree_launch(
            cfg, user, resolve("existing"), str(repo), "existing", requested_profile="claude"
        )
    assert (
        "already used by worktree" in caught.value.uxon_msg
        or "already checked out" in caught.value.uxon_msg
    )
    assert git_command("worktree", "list", "--porcelain") == original[0]
    assert git_command("show-ref", "--heads") == original[1]
    assert not (root / "relocated" / "myapp" / "existing").exists()
    cfg.worktree_root = str(root.parent / "forbidden")
    before = snapshot()
    with pytest.raises(SystemExit) as caught:
        launch.plan_worktree_launch(
            cfg, user, resolve("blocked"), str(repo), "blocked", requested_profile="claude"
        )
    assert "worktree_root" in caught.value.uxon_msg
    assert snapshot() == before
    assert not (root.parent / "forbidden").exists()
    cfg.worktree_root = ""
    before = snapshot()
    with pytest.raises(SystemExit) as caught:
        launch.plan_worktree_launch(
            cfg,
            user,
            resolve("feature-auth"),
            str(repo),
            "feature-auth",
            requested_profile="claude",
        )
    assert "path already exists or git refused" in caught.value.uxon_msg
    assert snapshot() == before
    cfg.worktree_base = "remote"
    before = snapshot()
    with redirect_stdout(io.StringIO()) as planned:
        launch.plan_worktree_launch(
            cfg,
            user,
            resolve("remote-dry"),
            str(repo),
            "remote-dry",
            requested_profile="claude",
            dry_run=True,
        )
    assert "fetch=" in planned.getvalue()
    assert snapshot() == before
    assert not (repo / ".uxon" / "worktrees" / "remote-dry").exists()
