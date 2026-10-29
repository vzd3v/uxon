"""External tmux/workload responses backed by real protected launch records."""

import json
import subprocess
from dataclasses import dataclass

from harness.launch_records import save_launch_record
from harness.target_commands import TargetCommands, target_config
from uxon.app.launch_profile import resolve_launch_profile
from uxon.domain.launch_profiles import LaunchConfig, LaunchProfile
from uxon.domain.runtime import WorkloadRuntimeSpec


def session_config(target, **overrides):
    cfg = target_config(
        allowed_roots=[str(target.parent)],
        new_project_root=str(target.parent),
        tmux_manage_options=False,
        **overrides,
    )
    cfg.runtimes["box"] = WorkloadRuntimeSpec(
        id="box",
        resource_scope="per_user",
        resource_name_template="box-{project_slug}",
        exec_prefix=("engine", "exec", "{resource}"),
        ready_command=("engine", "ready", "{resource}"),
        identity_command=("engine", "identity", "{resource}"),
        stop_command=("engine", "stop", "{resource}", "{pidfile}"),
        probe_timeout_seconds=1.5,
        stop_timeout_seconds=3.5,
    )
    cfg.launch = LaunchConfig(
        default_profile="fast",
        enabled_profiles=("fast", "safe", "boxed"),
        profiles={
            name: LaunchProfile(id=name, agent="claude", runtime=runtime)
            for name, runtime in (("fast", "direct"), ("safe", "direct"), ("boxed", "box"))
        },
    )
    return cfg


@dataclass
class SessionRow:
    user: str
    name: str
    session_id: str
    nonce: str
    path: object = None
    visible: bool = True
    failure: str = ""


class SessionCommands(TargetCommands):
    def __init__(self, *, denied=(), resource_names=("box-repo",)):
        super().__init__(self.respond, denied=denied)
        self.resource_names = set(resource_names)
        self.rows = []
        self.mutations = []
        self.stops = []

    def add(
        self, cfg, target, *, user="alice", profile="boxed", stem="repo", failure="", managed=True
    ):
        index = len(self.rows) + 1
        row = SessionRow(
            user, f"uxon-{stem}@{profile}", f"${index}", f"{index:032x}", failure=failure
        )
        if managed:
            resolved = resolve_launch_profile(cfg, user, profile, str(target), "normal")
            _, _, row.path = save_launch_record(
                cfg,
                resolved,
                target_dir=str(target),
                name=row.name,
                nonce=row.nonce,
                session_id=row.session_id,
            )
        self.rows.append(row)
        return row

    def respond(self, user, command, kwargs):
        def result(stdout="", rc=0, stderr=""):
            return subprocess.CompletedProcess(command, rc, stdout, stderr)

        visible = [row for row in self.rows if row.user == user and row.visible]
        if "uxon.infra.tmux_server_probe" in command:
            assert kwargs["timeout"] == 1.25
            return result(
                json.dumps({"state": "running", "sessions": [r.name for r in visible], "error": ""})
            )
        if command[:2] == ["ps", "-eo"]:
            return result("111 1 4096 3.0\n")
        if command[:2] == ["engine", "identity"]:
            assert len(command) == 3 and command[2] in self.resource_names
            assert kwargs["timeout"] == 1.5
            return result(json.dumps({"id": "cid-1", "host_pid": 42, "epoch": "epoch-1"}))
        if command[:2] == ["engine", "ready"]:
            assert len(command) == 3 and command[2] in self.resource_names
            assert kwargs["timeout"] == 1.5
            return result()
        if "uxon.infra.runtime_telemetry_probe" in command:
            assert kwargs["timeout"] == 10
            mode = command[command.index("--mode") + 1]
            if mode == "process-cgroup":
                assert command[-2:] == ["--pid", "42"]
                return result(json.dumps({"ok": True, "cgroup": "/box.scope", "error": ""}))
            assert mode == "cgroup-members" and command[-2:] == ["--cgroup", "/box.scope"]
            return result(json.dumps({"ok": True, "pids": [], "error": ""}))
        if command[:2] == ["engine", "stop"]:
            assert kwargs["timeout"] == 3.5
            row = next(r for r in self.rows if command[-1] == f"/tmp/uxon-{r.nonce}.pid")
            assert command == ["engine", "stop", "box-repo", f"/tmp/uxon-{row.nonce}.pid"]
            assert not row.visible and row.user == user
            self.stops.append(row.name)
            return result(
                rc=7 if row.failure == "stop" else 0,
                stderr="stop refused" if row.failure == "stop" else "",
            )
        assert command[:2] == ["tmux", "-S"], command
        action = command[3]
        if action == "list-sessions":
            assert kwargs["timeout"] == 1.25
            if command[4:] == ["-F", "#{session_id}"]:
                return result("\n".join(row.session_id for row in visible))
            if command[4:] == ["-F", "#{session_id}\t#{session_created}\t#{E:UXON_LAUNCH_NONCE}"]:
                return result("\n".join(f"{row.session_id}\t100\t{row.nonce}" for row in visible))
            assert command[4] == "-F" and command[5].startswith("#{session_name}\t#{session_id}")
            return result(
                "\n".join(
                    "\t".join(
                        [
                            r.name,
                            r.session_id,
                            "0",
                            "1",
                            "100",
                            "200",
                            r.nonce,
                            "spoofed-box",
                            "/spoof.scope",
                        ]
                    )
                    for r in visible
                )
            )
        if action == "list-panes":
            assert kwargs["timeout"] == 1.25
            assert command[4:6] == ["-s", "-t"] and any(r.session_id == command[6] for r in visible)
            return result("1\t111\tengine\t/mutable-pane-dir\t1\t%1\t0\t\t")
        assert action == "if-shell" and command[4:6] == ["-F", "-t"]
        row = next((r for r in visible if r.session_id == command[6]), None)
        if row is None:
            return result(rc=1, stderr="session absent")
        if row.nonce not in command[7]:
            return result("uxon-kill-stale\n")
        self.mutations.append((user, row.name))
        if row.failure == "tmux":
            return result(rc=9, stderr="tmux refused")
        row.visible = False
        if row.failure == "record":
            row.path.chmod(0o666)
        return result("uxon-kill-matched\n")
