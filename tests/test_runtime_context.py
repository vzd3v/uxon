"""Real launch-record roundtrip and saved runtime rendering context."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from helpers import make_config, make_session

from uxon.app import kill
from uxon.domain.launch_profiles import LaunchProfile, ResolvedLaunchProfile, RuntimeContext
from uxon.domain.runtime import WorkloadRuntimeSpec, runtime_pidfile, validate_runtime
from uxon.infra import launch_records, runtime
from uxon.infra.identity import process_user

pytestmark = pytest.mark.slow


def test_verified_record_preserves_context_for_real_identity_and_stop(tmp_path: Path) -> None:
    user = process_user()
    expected = ["/work/repo", "claude_box", "claude", "repo", user, "box-repo"]
    program = (
        f"import json,os,sys; assert sys.argv[1:] == {expected!r}; "
        "print(json.dumps(dict(id='cid-1',host_pid=os.getpid(),epoch='epoch-1')))"
    )
    spec = validate_runtime(
        WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="box-{project_slug}",
            exec_prefix=("unused-exec", "{resource}"),
            identity_command=(
                sys.executable,
                "-c",
                program,
                "{runtime_dir}",
                "{launch_profile}",
                "{agent}",
                "{project_slug}",
                "{user}",
                "{resource}",
            ),
            stop_command=(
                "true",
                "{user}",
                "{launch_profile}",
                "{agent}",
                "{project_slug}",
                "{resource}",
                "{pidfile}",
            ),
        )
    )
    cfg = make_config(runtimes={"box": spec})
    resolved = ResolvedLaunchProfile(
        profile=LaunchProfile(id="claude_box", agent="claude", runtime="box"),
        agent=cfg.agents["claude"],
        launch_user=user,
        mode_id="normal",
        runtime_context=RuntimeContext(
            runtime_id="box",
            resource="box-repo",
            runtime_dir="/work/repo",
            fingerprint=spec.fingerprint,
        ),
    )
    nonce = "a" * 32
    pending = launch_records.pending_from_resolved(
        socket_path="/tmp/uxon-context.sock",
        session_name="uxon-repo@claude_box",
        resolved=resolved,
        target_dir="/srv/repo",
        nonce=nonce,
    )
    metadata = launch_records.TmuxSessionMetadata("$1", "1234", pending.session_name, nonce)
    directory = tmp_path / "records"
    launch_records.create_pending_record(pending, override_dir=directory)
    path = launch_records.finalize_pending_record(
        pending, metadata, runtime_id="cid-1", runtime_epoch="epoch-1", override_dir=directory
    )
    payload = launch_records.read_verified_record(
        pending.socket_path, metadata, override_dir=directory
    )
    assert payload is not None
    assert (payload["runtime_dir"], payload["project_slug"]) == ("/work/repo", "repo")
    assert path.stat().st_mode & 0o777 == 0o600
    session = make_session(pending.session_name, user=user)
    for field in (
        "launch_user",
        "agent",
        "launch_nonce",
        "runtime",
        "runtime_resource",
        "runtime_fingerprint",
        "runtime_id",
        "runtime_epoch",
        "runtime_dir",
        "project_slug",
    ):
        setattr(session, field, payload[field])
    session.profile = payload["profile"]
    session.active_path = "/unrelated/controller-directory"
    session.launch_record_verified = True
    teardown = kill.prepare_runtime_teardown(cfg, session)
    assert teardown is not None
    assert list(teardown.stop_cmd) == [
        "true",
        user,
        "claude_box",
        "claude",
        "repo",
        "box-repo",
        runtime_pidfile(nonce),
    ]
    assert kill.run_runtime_teardown(cfg, teardown, target_user=user, session_name=session.name)
    assert (
        runtime.current_runtime_identity_for_profile(
            cfg,
            spec,
            "box-repo",
            user,
            runtime_dir="/work/repo",
            launch_profile="claude_box",
            agent="claude",
            project_slug="repo",
        )
        is not None
    )
    for obsolete in ("version", "runtime_dir", "project_slug"):
        changed = dict(payload)
        if obsolete == "version":
            changed["version"] = 2
        else:
            del changed[obsolete]
        path.write_text(json.dumps(changed))
        assert (
            launch_records.read_verified_record(
                pending.socket_path, metadata, override_dir=directory
            )
            is None
        )
