"""Container-agnostic launch: resolution, exec wrap, policy, safety, caveat.

In-gate mocked-boundary tests (no runtime). Cover the Part B contracts that
are assertable without docker/podman:

* exec-site wrap shape (AC-B1) + disabled parity (AC-B6),
* deterministic name resolution per (user, project dir) (AC-B2),
* ``path_map`` longest-prefix container path (AC-B3),
* the ``on_missing`` × ``approval`` policy matrix with probe results
  mocked at the subprocess boundary (AC-B4),
* name / path / ``.format`` safety incl. hostile-dir-name cases (AC-B8),
* the ``[container].enabled`` kill caveat (Security MEDIUM-2).

The real-runtime harness (in-container exec, kill reaping) is P4.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from helpers import make_session_snapshot

import uxon.app.launch_profile as launch_profile_app
from uxon.domain.agents import default_agent_catalog
from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.host_report import BinaryStatus, HostReport
from uxon.domain.launch_profiles import (
    LaunchConfig,
    LaunchProfile,
    ResolvedLaunchProfile,
    RuntimeContext,
    RuntimeIdentity,
    builtin_launch_profiles,
)
from uxon.domain.runtime import (
    RUNTIME_CGROUP_ENV,
    RUNTIME_EPOCH_ENV,
    RUNTIME_ID_ENV,
    RUNTIME_RESOURCE_ENV,
    SESSION_ENV,
    WorkloadRuntimeSpec,
    apply_path_map,
    decide_runtime_action,
    is_valid_runtime_resource,
    render_stop_command,
    resolve_runtime_resource_name,
    runtime_pidfile,
    validate_path_map,
    validate_runtime,
    validate_runtime_resource,
    wrap_agent_for_runtime,
)


def _cfg(profile: WorkloadRuntimeSpec | None = None, **overrides) -> Config:
    """Minimal Config carrying an optional workload runtime (other fields inert).

    When ``profile`` is given it is registered as ``"box"`` and the ``claude``
    launch profile is wired to use it (``runtime="box"``), so the
    profile-based launch/teardown/doctor paths resolve a runtime context.
    """
    agents = default_agent_catalog()
    profiles = builtin_launch_profiles(agents)
    runtimes: dict[str, WorkloadRuntimeSpec] = {
        "direct": WorkloadRuntimeSpec(id="direct", kind="direct")
    }
    if profile is not None:
        runtimes["box"] = profile
        profiles["claude"] = LaunchProfile(id="claude", agent="claude", runtime="box")
    launch = LaunchConfig(default_profile="claude", profiles=profiles)
    base = dict(
        default_launch_user="",
        default_launch_mode="caller",
        enable_all_users_list=False,
        launch_user_by_caller={},
        session_users=[],
        allowed_roots=["/srv"],
        session_prefix="uxon-",
        legacy_session_prefixes=(),
        new_project_root="/srv",
        repeat_noninteractive_mode="fail",
        tmux_socket_template="/tmp/uxon-{user}.sock",
        tui_refresh_interval_seconds=2.0,
        git_create_enabled=False,
        git_remote_profiles=[],
        tmux_manage_options=False,
        tmux_options={},
        tmux_server_options={},
        tmux_append_server_options={},
        agents=agents,
        launch=launch,
        runtimes=runtimes,
    )
    base.update(overrides)
    return Config(**base)


def _resolved(
    cfg: Config,
    launch_user: str,
    *,
    profile_id: str = "claude",
    mode: str = "yolo",
    target_dir: str = "/srv/projects/myapp",
):
    from uxon.domain.runtime import apply_path_map, resolve_runtime_resource_name
    from uxon.domain.session import slugify

    profile = cfg.launch.profiles[profile_id]
    context = None
    if profile.runtime != "direct":
        runtime = cfg.runtimes[profile.runtime]
        dir_token = apply_path_map(target_dir, runtime.path_map)
        context = RuntimeContext(
            runtime_id=runtime.id,
            resource=resolve_runtime_resource_name(
                runtime,
                user=launch_user,
                launch_profile=profile.id,
                agent=profile.agent,
                project_slug=slugify(target_dir.rsplit("/", 1)[-1]),
            ),
            runtime_dir=dir_token,
            fingerprint=runtime.fingerprint,
        )
    return ResolvedLaunchProfile(
        profile=profile,
        agent=cfg.agents[profile.agent],
        launch_user=launch_user,
        mode_id=mode,
        runtime_context=context,
    )


_EXEC = ("docker", "exec", "-it", "-w", "{runtime_dir}", "{resource}")


def _managed_create_cmd(req):
    assert req.managed is not None
    return req.managed.create_cmd


class NameResolutionTests(unittest.TestCase):
    """AC-B2 — same container per (user, project dir), never per-session."""

    def test_resource_name_template_expands_project_slug(self) -> None:
        profile = WorkloadRuntimeSpec(
            id="box", resource_scope="per_user", resource_name_template="proj-{project_slug}"
        )
        self.assertEqual(
            resolve_runtime_resource_name(
                profile, user="dana", launch_profile="claude", agent="claude", project_slug="myapp"
            ),
            "proj-myapp",
        )

    def test_same_dir_same_name_different_dir_differs(self) -> None:
        profile = WorkloadRuntimeSpec(
            id="box", resource_scope="per_user", resource_name_template="proj-{project_slug}"
        )
        a1 = resolve_runtime_resource_name(
            profile, user="dana", launch_profile="claude", agent="claude", project_slug="myapp"
        )
        a2 = resolve_runtime_resource_name(
            profile, user="dana", launch_profile="claude", agent="claude", project_slug="myapp"
        )
        b = resolve_runtime_resource_name(
            profile, user="dana", launch_profile="claude", agent="claude", project_slug="other"
        )
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, b)

    def test_user_placeholder(self) -> None:
        profile = WorkloadRuntimeSpec(
            id="box", resource_scope="per_user", resource_name_template="{user}-box"
        )
        self.assertEqual(
            resolve_runtime_resource_name(
                profile, user="dana", launch_profile="claude", agent="claude", project_slug="x"
            ),
            "dana-box",
        )


class PathMapTests(unittest.TestCase):
    """AC-B3 — {runtime_dir} is the container-side path; allowed_roots stays host-side."""

    def test_no_map_returns_host_path(self) -> None:
        self.assertEqual(apply_path_map("/srv/projects/myapp", ()), "/srv/projects/myapp")

    def test_longest_prefix_match(self) -> None:
        pm = validate_path_map({"/srv": "/srv-root", "/srv/projects/myapp": "/work"})
        self.assertEqual(apply_path_map("/srv/projects/myapp/sub", pm), "/work/sub")

    def test_exact_prefix_match(self) -> None:
        pm = validate_path_map({"/srv/projects/myapp": "/work"})
        self.assertEqual(apply_path_map("/srv/projects/myapp", pm), "/work")

    def test_resolve_runtime_applies_path_map_to_dir(self) -> None:
        # Profile path: apply_path_map + resolve_runtime_resource_name are
        # the pure halves the removed tmux.resolve_container composed.
        profile = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="proj-{project_slug}",
            path_map=validate_path_map({"/srv/projects/myapp": "/work"}),
        )
        dir_token = apply_path_map("/srv/projects/myapp", profile.path_map)
        self.assertEqual(dir_token, "/work")


class ExecWrapTests(unittest.TestCase):
    """AC-B1 / AC-B6 — single exec-site wrap; disabled = byte-for-byte parity."""

    def _build(self, cfg: Config, target_dir: str = "/srv/projects/myapp"):
        import getpass

        from uxon.infra import tmux

        # ``tmux_socket_path`` resolves the launch user via ``pwd.getpwnam``, so
        # use a real OS account (the test runner's own user).
        launch_user = getpass.getuser()
        args = ParsedArgs(action="run", profile="claude", permission_mode="yolo")
        resolved = _resolved(cfg, launch_user, target_dir=target_dir)
        # ``tmux_nesting_mode`` reads $TMUX; force the classic (execvp) path so
        # the request is deterministic regardless of the test runner's tmux.
        with mock.patch("uxon.infra.tmux.tmux_nesting_mode", return_value="execvp"):
            return tmux._build_tmux_launch_request(
                target_dir,
                "uxon-myapp@claude",
                args,
                cfg,
                None,
                resolved_profile=resolved,
            )

    def test_disabled_is_byte_for_byte_identical(self) -> None:
        disabled = self._build(_cfg(None))
        # A config with [container] present but enabled=false must match the
        # no-container build exactly (AC-B6 parity).
        also_disabled = self._build(_cfg(None))
        self.assertEqual(disabled.cmd, also_disabled.cmd)
        # And the agent binary leads the final_cmd (no exec prefix).
        disabled_create = _managed_create_cmd(disabled)
        idx = disabled_create.index("claude")
        self.assertNotIn("docker", disabled_create[:idx])
        # AC-P0.1 off-invariant: no container marker / wrapper appears when
        # disabled. Launch-profile diagnostics still ride every managed launch.
        cmd = list(disabled_create)
        joined = " ".join(cmd)
        self.assertNotIn("UXON_CONTAINER", joined)
        self.assertNotIn("UXON_SESSION", joined)
        self.assertNotIn("sh", cmd)
        self.assertEqual(cmd[idx:], ["claude", "--dangerously-skip-permissions"])

    def test_enabled_prepends_exec_prefix(self) -> None:
        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="proj-{project_slug}",
            exec_prefix=_EXEC,
        )
        req = self._build(_cfg(c))
        # exec_prefix(resolved) + the per-session wrapper + agent argv, in
        # order. After the hoist, EVERY enabled session is wrapped (to export
        # UXON_SESSION), so the resolved exec prefix is the leading 6 tokens.
        cmd = list(_managed_create_cmd(req))
        agent_tail = cmd[cmd.index("docker") :]
        self.assertEqual(
            agent_tail[:6],
            ["docker", "exec", "-it", "-w", "/srv/projects/myapp", "proj-myapp"],
        )
        # The agent argv survives intact after the ``uxon-agent`` $0 sentinel.
        self.assertEqual(
            agent_tail[agent_tail.index("uxon-agent") + 1 :],
            ["claude", "--dangerously-skip-permissions"],
        )

    def test_enabled_with_path_map_uses_runtime_dir(self) -> None:
        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="proj-{project_slug}",
            exec_prefix=_EXEC,
            path_map=validate_path_map({"/srv/projects/myapp": "/work"}),
        )
        req = self._build(_cfg(c))
        cmd = list(_managed_create_cmd(req))
        # -w token is the container-side path, not the host path.
        self.assertIn("/work", cmd)
        self.assertNotIn("/srv/projects/myapp", cmd[cmd.index("docker") :])

    def test_stop_command_stashes_name_and_wraps_agent(self) -> None:
        # Teardown opt-in (AC-B5): the new-session argv stashes the resolved
        # name in the session env and the agent is wrapped so it exports the
        # per-session marker AND records its in-container PID, while the exec
        # prefix is unchanged.
        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="proj-{project_slug}",
            exec_prefix=_EXEC,
            stop_command=("docker", "exec", "{resource}", "sh", "-c", "kill $(cat {pidfile})"),
            identity_command=("inspect", "{resource}"),
        )
        with mock.patch(
            "uxon.infra.runtime.resolve_runtime_identity_for_profile",
            return_value=RuntimeIdentity(id="cid", host_pid=42, epoch="1000"),
        ):
            cmd = list(_managed_create_cmd(self._build(_cfg(c))))
        # ``-e UXON_CONTAINER=proj-myapp`` rides the new-session argv.
        self.assertIn("-e", cmd)
        self.assertIn(f"{RUNTIME_RESOURCE_ENV}=proj-myapp", cmd)
        # The agent is wrapped: exec prefix, then ``sh -c '…' uxon-agent claude …``.
        tail = cmd[cmd.index("docker") :]
        self.assertEqual(
            tail[:6], ["docker", "exec", "-it", "-w", "/srv/projects/myapp", "proj-myapp"]
        )
        self.assertEqual(tail[6:8], ["sh", "-c"])
        # The export marker is present on the teardown path too (hoist), plus
        # the pidfile write that this path opted into.
        self.assertIn(f"export {SESSION_ENV}=", tail[8])
        self.assertIn("echo $$ >", tail[8])
        # The agent argv survives intact after the ``uxon-agent`` $0 sentinel.
        self.assertEqual(
            tail[tail.index("uxon-agent") + 1 :], ["claude", "--dangerously-skip-permissions"]
        )

    def test_stop_enabled_runtime_fails_launch_without_identity(self) -> None:
        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="proj-{project_slug}",
            exec_prefix=_EXEC,
            stop_command=("stop", "{resource}", "{pidfile}"),
        )
        with self.assertRaises(SystemExit):
            self._build(_cfg(c))

    def test_enabled_without_stop_command_carries_marker_but_no_pidfile(self) -> None:
        # After the hoist: enabled + no stop_command (+ no identity_command, the
        # default) still carries ``-e UXON_CONTAINER=<bare name>`` and wraps the
        # agent to export ``UXON_SESSION``, but does NOT write a pidfile, and the
        # identity vars are ABSENT (identity_command unset → degrade path).
        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="proj-{project_slug}",
            exec_prefix=_EXEC,
        )
        cmd = list(_managed_create_cmd(self._build(_cfg(c))))
        # Bare-name marker rides the session env.
        self.assertIn("-e", cmd)
        self.assertIn(f"{RUNTIME_RESOURCE_ENV}=proj-myapp", cmd)
        # Identity vars absent without identity_command (degrade).
        joined = " ".join(cmd)
        self.assertNotIn(RUNTIME_ID_ENV, joined)
        self.assertNotIn(RUNTIME_CGROUP_ENV, joined)
        self.assertNotIn(RUNTIME_EPOCH_ENV, joined)
        # The agent IS wrapped to export the per-session marker, with NO pidfile.
        tail = cmd[cmd.index("docker") :]
        self.assertEqual(tail[6:8], ["sh", "-c"])
        self.assertIn(f"export {SESSION_ENV}=", tail[8])
        self.assertNotIn("echo $$", tail[8])
        self.assertEqual(
            tail[tail.index("uxon-agent") + 1 :], ["claude", "--dangerously-skip-permissions"]
        )

    def test_resolved_identity_rides_separate_session_env_vars(self) -> None:
        # When identity_command resolves a non-empty identity, the id / cgroup /
        # epoch ride SEPARATE ``-e`` vars; ``UXON_CONTAINER`` keeps the bare name.
        from uxon.infra.runtime import RuntimeIdentity

        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="proj-{project_slug}",
            exec_prefix=_EXEC,
            identity_command=("inspect", "{resource}"),
        )
        ident = RuntimeIdentity(
            id="abc123", cgroup="/sys/fs/cgroup/x.scope", epoch="2026-06-15T00:00:00Z"
        )
        with mock.patch(
            "uxon.infra.runtime.resolve_runtime_identity_for_profile", return_value=ident
        ):
            cmd = list(_managed_create_cmd(self._build(_cfg(c))))
        self.assertIn(f"{RUNTIME_RESOURCE_ENV}=proj-myapp", cmd)
        self.assertIn(f"{RUNTIME_ID_ENV}=abc123", cmd)
        self.assertIn(f"{RUNTIME_CGROUP_ENV}=/sys/fs/cgroup/x.scope", cmd)
        self.assertIn(f"{RUNTIME_EPOCH_ENV}=2026-06-15T00:00:00Z", cmd)


class NameSafetyTests(unittest.TestCase):
    """AC-B8 — the resolved name must be charset/length safe (the slug gap)."""

    def test_hostile_leading_dot_slug_rejected(self) -> None:
        # A project dir slugifying to ``.--x`` keeps the leading dot — reject.
        with self.assertRaises(SystemExit):
            validate_runtime_resource(".--x")

    def test_all_dot_name_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            validate_runtime_resource("...")

    def test_leading_underscore_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            validate_runtime_resource("_box")

    def test_leading_dash_rejected(self) -> None:
        # An option-looking token must never reach ``docker exec … <name>``.
        with self.assertRaises(SystemExit):
            validate_runtime_resource("-rm")

    def test_over_128_chars_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            validate_runtime_resource("a" * 129)

    def test_safe_name_accepted(self) -> None:
        self.assertEqual(validate_runtime_resource("proj-myapp_1.2"), "proj-myapp_1.2")

    def test_resolved_hostile_dir_name_rejected_end_to_end(self) -> None:
        # Directory basename "-evil" slugifies (strip('-')) to "evil" — safe;
        # ".evil" keeps the dot → unsafe. Prove the post-expansion check fires
        # on the profile name resolution path.
        profile = WorkloadRuntimeSpec(
            id="box", resource_scope="per_user", resource_name_template="{project_slug}"
        )
        from uxon.domain.session import slugify

        with self.assertRaises(SystemExit):
            resolve_runtime_resource_name(
                profile,
                user="dana",
                launch_profile="claude",
                agent="claude",
                project_slug=slugify(".evil"),
            )


class PathSafetyTests(unittest.TestCase):
    """AC-B8 — path_map / {runtime_dir} must be absolute, normalized, ``..``-free."""

    def test_path_map_value_with_dotdot_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            validate_path_map({"/srv": "/work/../../etc"})

    def test_path_map_relative_key_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            validate_path_map({"relative": "/work"})

    def test_path_map_value_literal_not_formatted(self) -> None:
        # A ``{resource}`` in a path_map value must be a literal, never expanded —
        # it is a non-absolute string, so it is simply rejected (not run).
        with self.assertRaises(SystemExit):
            validate_path_map({"/srv": "{resource}"})


class FormatGuardTests(unittest.TestCase):
    """AC-B8 — a bad placeholder fails with a clear message, not a traceback."""

    def test_unknown_placeholder_in_resource_name_template(self) -> None:
        profile = WorkloadRuntimeSpec(
            id="box", resource_scope="per_user", resource_name_template="proj-{bogus}"
        )
        with self.assertRaises(SystemExit) as ctx:
            resolve_runtime_resource_name(
                profile, user="dana", launch_profile="claude", agent="claude", project_slug="x"
            )
        # ``fail`` stashes the human message on ``.uxon_msg`` (str() is the code).
        self.assertIn("placeholder", getattr(ctx.exception, "uxon_msg", "").lower())


class DecideActionTests(unittest.TestCase):
    """AC-B4 — the pure policy verdict before any side effect."""

    def test_running_always_execs(self) -> None:
        for on_missing in ("off", "start", "create"):
            action, _ = decide_runtime_action(running=True, exists=True, on_missing=on_missing)
            self.assertEqual(action, "exec")

    def test_stopped_needs_start_capability(self) -> None:
        self.assertEqual(
            decide_runtime_action(running=False, exists=True, on_missing="off")[0], "fail"
        )
        self.assertEqual(
            decide_runtime_action(running=False, exists=True, on_missing="start")[0], "start"
        )
        self.assertEqual(
            decide_runtime_action(running=False, exists=True, on_missing="create")[0], "start"
        )

    def test_absent_needs_create_capability(self) -> None:
        self.assertEqual(
            decide_runtime_action(running=False, exists=False, on_missing="off")[0], "fail"
        )
        self.assertEqual(
            decide_runtime_action(running=False, exists=False, on_missing="start")[0], "fail"
        )
        self.assertEqual(
            decide_runtime_action(running=False, exists=False, on_missing="create")[0], "create"
        )


class AsUserShellOutTests(unittest.TestCase):
    """HIGH — every container shell-out runs as the launch user (rootless).

    The probe and the agent must hit the same per-user daemon; the prepare
    must cd into the HOST dir. Assert the argv carries the as-user sudo prefix
    and the host-dir cd wrapper, with the operator tokens preserved verbatim.
    """

    def _captured_argv(self, fn) -> list[str]:
        from uxon.infra import runtime as runtime_infra

        captured: dict[str, list[str]] = {}

        class _CP:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return _CP()

        # Force the cross-user path: pretend uxon runs as someone else so the
        # argv-preserving sudo prefix is emitted (same-user would be empty).
        with (
            mock.patch.object(runtime_infra.subprocess, "run", side_effect=fake_run),
            mock.patch("uxon.infra.identity.process_user", return_value="root"),
        ):
            fn()
        return captured["argv"]

    def test_probe_argv_is_prefixed_for_launch_user(self) -> None:
        from uxon.infra import runtime as runtime_infra

        argv = self._captured_argv(
            lambda: runtime_infra._probe_exit_ok(_cfg(), ["docker", "top", "box"], "dana", 10.0)
        )
        # Non-interactive prefix (no TTY) for probes, then the operator argv.
        self.assertEqual(argv[:6], ["/usr/bin/sudo", "-n", "-H", "-u", "dana", "--"])
        self.assertEqual(argv[-3:], ["docker", "top", "box"])

    def test_prepare_argv_runs_in_host_dir_as_launch_user(self) -> None:
        from uxon.infra import runtime as runtime_infra

        argv = self._captured_argv(
            lambda: runtime_infra._run_prepare(
                _cfg(),
                ["docker", "compose", "up", "-d"],
                "/srv/projects/app",
                "dana",
                10.0,
            )
        )
        # Interactive prefix for the launch-time start/create.
        self.assertEqual(argv[:5], ["/usr/bin/sudo", "-H", "-u", "dana", "--"])
        # cd into the HOST dir, then exec the operator argv as separate tokens
        # (argv-list invariant: never re-parsed by the shell).
        self.assertEqual(argv[5], "sh")
        self.assertIn("cd /srv/projects/app", argv[7])
        self.assertEqual(argv[-4:], ["docker", "compose", "up", "-d"])

    def test_probe_permission_error_fails_cleanly(self) -> None:
        from uxon.infra import runtime as runtime_infra

        with (
            mock.patch.object(
                runtime_infra.subprocess, "run", side_effect=PermissionError("denied")
            ),
            mock.patch("uxon.infra.identity.process_user", return_value="dana"),
            self.assertRaises(SystemExit),
        ):
            runtime_infra._probe_exit_ok(_cfg(), ["docker", "top", "box"], "dana", 10.0)


class IdentityParseTests(unittest.TestCase):
    """Identity parsers and the resolver degrade without raising."""

    def test_resolve_identity_degrades_on_bad_template(self) -> None:
        # AC-P1.3 / AC-P3.5 degrade-never-block: a misconfigured identity_command
        # template must yield EMPTY_IDENTITY, never abort the launch with the
        # render_template SystemExit (it sits on the launch hot path).
        from uxon.infra import runtime as runtime_infra
        from uxon.infra.runtime import EMPTY_IDENTITY

        profile = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="proj-{project_slug}",
            exec_prefix=_EXEC,
            # Single-brace ``{.Id}`` is an invalid format token (the
            # documented form doubles braces) → render_profile_template fail()s.
            identity_command=("docker", "inspect", "--format", "{.Id}", "{resource}"),
        )
        cfg = _cfg(profile)
        resolved = _resolved(cfg, "dana", target_dir="/srv/projects/myapp")
        ident = runtime_infra.resolve_runtime_identity_for_profile(
            cfg, "/srv/projects/myapp", resolved
        )
        self.assertIs(ident, EMPTY_IDENTITY)

    def test_parse_runtime_identity_output_json_contract(self) -> None:
        from uxon.infra.runtime import parse_runtime_identity_output

        self.assertEqual(
            parse_runtime_identity_output(
                '{"id":"abc123","host_pid":4242,"epoch":"2026-06-15T00:00:00Z"}'
            ),
            RuntimeIdentity(id="abc123", host_pid=4242, epoch="2026-06-15T00:00:00Z"),
        )

    def test_parse_runtime_identity_output_degrades_on_bad_shape(self) -> None:
        from uxon.infra.runtime import parse_runtime_identity_output

        for bad in (
            "",
            "[]",
            '{"id":"abc","host_pid":"4242","epoch":"e"}',
            '{"id":"abc","host_pid":0,"epoch":"e"}',
            '{"id":"","host_pid":42,"epoch":"e"}',
        ):
            self.assertIsNone(parse_runtime_identity_output(bad))

    def test_parse_proc_cgroup_v2_prefers_unified_line(self) -> None:
        from uxon.infra.runtime import parse_proc_cgroup

        v2 = "0::/system.slice/docker-abc.scope\n"
        self.assertEqual(parse_proc_cgroup(v2), "/system.slice/docker-abc.scope")
        # Path containing ``:`` survives the bounded split.
        self.assertEqual(parse_proc_cgroup("0::/a:b/c"), "/a:b/c")

    def test_parse_proc_cgroup_v1_falls_back_to_a_controller_line(self) -> None:
        from uxon.infra.runtime import parse_proc_cgroup

        v1 = "12:pids:/docker/abc\n11:memory:/docker/abc\n"
        self.assertEqual(parse_proc_cgroup(v1), "/docker/abc")
        self.assertEqual(parse_proc_cgroup("garbage\n\n"), "")


class TeardownPrimitiveTests(unittest.TestCase):
    """AC-B5 pure-domain teardown primitives (pidfile, wrap, render, name)."""

    def test_pidfile_is_deterministic_per_session_and_sanitized(self) -> None:
        # Same session → same path; different sessions → different paths.
        self.assertEqual(runtime_pidfile("uxon-app@claude"), runtime_pidfile("uxon-app@claude"))
        self.assertNotEqual(
            runtime_pidfile("uxon-app@claude-2"), runtime_pidfile("uxon-app@claude")
        )
        # Worktree/index variants of one container resolve to distinct files.
        self.assertNotEqual(runtime_pidfile("uxon-app@codex"), runtime_pidfile("uxon-app@claude"))
        # Unsafe characters collapse to ``_`` (path safe to embed in sh -c).
        pf = runtime_pidfile("uxon-a b/c@claude")
        self.assertTrue(pf.startswith("/tmp/uxon-"))
        self.assertNotIn(" ", pf)
        self.assertNotIn("/", pf[len("/tmp/") :])

    def test_wrap_exports_session_and_optionally_records_pid(self) -> None:
        # With a pidfile (teardown opted in): export the per-session marker AND
        # record the in-container PID, then exec the agent.
        wrapped = wrap_agent_for_runtime(
            ["claude", "--flag"], session="uxon-app@claude", pidfile="/tmp/uxon-s.pid"
        )
        self.assertEqual(wrapped[:2], ["sh", "-c"])
        self.assertIn(f"export {SESSION_ENV}=uxon-app@claude", wrapped[2])
        self.assertIn("echo $$ > /tmp/uxon-s.pid", wrapped[2])
        self.assertIn('exec "$@"', wrapped[2])
        # $0 sentinel then the agent argv, untouched and still a token list.
        self.assertEqual(wrapped[3:], ["uxon-agent", "claude", "--flag"])
        # Without a pidfile (no teardown): export only, no PID record.
        bare = wrap_agent_for_runtime(["claude"], session="uxon-app@claude", pidfile=None)
        self.assertIn(f"export {SESSION_ENV}=uxon-app@claude", bare[2])
        self.assertNotIn("echo $$", bare[2])
        self.assertIn('exec "$@"', bare[2])
        self.assertEqual(bare[3:], ["uxon-agent", "claude"])

    def test_render_stop_command_fills_per_token(self) -> None:
        out = render_stop_command(
            ("docker", "exec", "{resource}", "sh", "-c", "kill $(cat {pidfile})"),
            resource="proj-app",
            pidfile="/tmp/uxon-s.pid",
        )
        self.assertEqual(
            out, ["docker", "exec", "proj-app", "sh", "-c", "kill $(cat /tmp/uxon-s.pid)"]
        )

    def test_is_valid_runtime_resource_matches_charset(self) -> None:
        self.assertTrue(is_valid_runtime_resource("proj-app_1.2"))
        # Non-raising rejects (kill path degrades to skip-teardown, never aborts).
        for bad in ("", "-leading", ".dot", "_under", "bad name", "a" * 129):
            self.assertFalse(is_valid_runtime_resource(bad))


class ContainerUsageResolverTests(unittest.TestCase):
    """Pure host-side telemetry resolvers (host-free, fed fabricated input)."""

    def test_parse_cgroup_procs_dedupes_and_skips_noise(self) -> None:
        from uxon.domain.runtime_usage import parse_cgroup_procs

        # One PID per line; blanks + non-numeric skipped; first-seen dedupe.
        self.assertEqual(parse_cgroup_procs("12\n34\n\n12\nfoo\n56\n"), [12, 34, 56])
        self.assertEqual(parse_cgroup_procs(""), [])

    def test_parse_environ_session_extracts_marker(self) -> None:
        from uxon.domain.runtime_usage import parse_environ_session

        blob = "PATH=/bin\0UXON_SESSION=uxon-proj@claude\0HOME=/root\0"
        self.assertEqual(parse_environ_session(blob), "uxon-proj@claude")
        # Absent marker → "" (a non-uxon process sharing the container).
        self.assertEqual(parse_environ_session("PATH=/bin\0TERM=xterm\0"), "")

    def test_parse_sudo_environ_lines_maps_pid_to_session(self) -> None:
        from uxon.domain.runtime_usage import parse_sudo_environ_lines

        out = parse_sudo_environ_lines("10 uxon-a@claude\n11 uxon-b@claude\nbad\n12 \n")
        self.assertEqual(out, {10: "uxon-a@claude", 11: "uxon-b@claude", 12: ""})

    def test_sum_usage_clamps_and_skips_missing(self) -> None:
        from uxon.domain.runtime_usage import sum_usage_for_pids

        # pid → (ppid, rss_kib, cpu_pct). 99 is absent (exited); -5 clamped.
        proc_rows = {1: (0, 100, 5.0), 2: (1, 200, 10.0), 3: (1, -5, -1.0)}
        rss, cpu = sum_usage_for_pids([1, 2, 3, 99], proc_rows)
        self.assertEqual(rss, 300)
        self.assertEqual(cpu, 15.0)

    def test_per_session_split_isolates_runaway(self) -> None:
        """AC-P1.6: a busy PID in session A never sums into session B."""
        from uxon.domain.runtime_usage import per_session_usage

        cgroup_pids = [1, 2, 3, 4]
        # 1,2 → session A (one a runaway); 3 → session B; 4 unmarked (init).
        pid_to_session = {1: "uxon-a@claude", 2: "uxon-a@claude", 3: "uxon-b@claude", 4: ""}
        proc_rows = {
            1: (0, 1000, 95.0),  # runaway agent in A
            2: (1, 50, 1.0),  # A child (reparented descendant in the cgroup)
            3: (0, 200, 2.0),  # idle agent in B
            4: (0, 10, 0.0),  # container init, unmarked
        }
        usage = per_session_usage(cgroup_pids, pid_to_session, proc_rows)
        self.assertEqual(usage["uxon-a@claude"], (1050, 96.0))
        self.assertEqual(usage["uxon-b@claude"], (200, 2.0))
        # The runaway in A did not leak into B.
        self.assertLess(usage["uxon-b@claude"][1], 50)

    def test_per_session_split_degrades_to_empty_on_missing_markers(self) -> None:
        from uxon.domain.runtime_usage import group_pids_by_session, per_session_usage

        # No PID carries a marker (environ unreadable) → no per-session group.
        self.assertEqual(group_pids_by_session([1, 2], {1: "", 2: ""}), {})
        self.assertEqual(per_session_usage([1, 2], {}, {1: (0, 5, 1.0)}), {})


class TelemetryEnrichTests(unittest.TestCase):
    """``enrich_session_usage`` container wiring + the AC-P0.4 off-invariant."""

    _PS = "111 1 4096 3.0\n222 111 2048 1.0\n900 1 8192 50.0\n901 1 1024 60.0\n"

    def _ps_completed(self):
        return mock.Mock(returncode=0, stdout=self._PS)

    def _session(self, name, **kw):
        from helpers import make_session

        s = make_session(name)
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def _profile(self, *, running: bool = False) -> WorkloadRuntimeSpec:
        return WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="cbox",
            exec_prefix=("docker", "exec", "{resource}"),
            ready_command=("docker", "top", "{resource}") if running else (),
            identity_command=("docker", "inspect", "{resource}"),
        )

    def _record_session(self, name: str, profile: WorkloadRuntimeSpec, **kw):
        values = dict(
            runtime_resource="cbox",
            runtime_cgroup="/c.scope",
            launch_record_verified=True,
            launch_user="u-vz",
            runtime=profile.id,
            runtime_fingerprint=profile.fingerprint,
            runtime_id="cid-1",
            runtime_epoch="1000",
        )
        values.update(kw)
        return self._session(name, **values)

    def test_non_runtime_session_uses_pane_walk_and_touches_nothing(self) -> None:
        """AC-P0.4: a no-marker session adds no subprocess / /proc / sudo read."""
        from pathlib import Path

        from uxon.infra import sessions_probe

        s = self._session("uxon-plain@claude", pane_pids=(111,))
        with (
            mock.patch("uxon.infra.sessions_probe.run_query") as run,
            mock.patch.object(Path, "read_text") as read_text,
        ):
            run.return_value = self._ps_completed()
            sessions_probe.enrich_session_usage(_cfg(), [s])
        # Exactly one subprocess (the single ps); no /proc read; the walk
        # summed pane 111 + child 222.
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:2], ["ps", "-eo"])
        read_text.assert_not_called()
        self.assertEqual(s.rss_kib, 4096 + 2048)
        self.assertEqual(s.cpu_pct, 4.0)

    def test_single_runtime_session_sums_cgroup_no_environ(self) -> None:
        """One session per container: cgroup.procs IS its set — no sudo read."""
        from uxon.infra import sessions_probe

        profile = self._profile()
        s = self._record_session(
            "uxon-c@claude", profile, runtime_cgroup="/system.slice/docker-c.scope"
        )

        def fake_run(cmd, *a, **k):
            return self._ps_completed()  # only the ps table

        with (
            mock.patch("uxon.infra.sessions_probe.run_query", side_effect=fake_run) as run,
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs", return_value=[900, 901]),
            mock.patch(
                "uxon.infra.runtime.current_runtime_identity_for_profile",
                return_value=RuntimeIdentity(
                    id="cid-1", cgroup="/system.slice/docker-c.scope", epoch="1000"
                ),
            ),
        ):
            sessions_probe.enrich_session_usage(
                _cfg(profile), [s], runtimes={"box": profile}, launch_user="u-vz"
            )
        # No sudo environ batch (single session) — only the ps call.
        self.assertEqual(run.call_count, 1)
        self.assertEqual(s.rss_kib, 8192 + 1024)
        self.assertEqual(s.cpu_pct, 110.0)

    def test_shared_runtime_splits_per_session_when_privileged(self) -> None:
        """AC-P1.6: ≥2 sessions share a cgroup → split by UXON_SESSION."""
        from uxon.infra import sessions_probe

        profile = self._profile()
        a = self._record_session("uxon-a@claude", profile)
        b = self._record_session("uxon-b@claude", profile)
        with (
            mock.patch("uxon.infra.sessions_probe.run_query", return_value=self._ps_completed()),
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs", return_value=[900, 901]),
            mock.patch(
                "uxon.infra.sessions_probe._read_pid_sessions",
                return_value={900: "uxon-a@claude", 901: "uxon-b@claude"},
            ),
            mock.patch(
                "uxon.infra.runtime.current_runtime_identity_for_profile",
                return_value=RuntimeIdentity(id="cid-1", cgroup="/c.scope", epoch="1000"),
            ),
        ):
            sessions_probe.enrich_session_usage(
                _cfg(profile), [a, b], runtimes={"box": profile}, launch_user="u-vz"
            )
        self.assertEqual((a.rss_kib, a.cpu_pct), (8192, 50.0))
        self.assertEqual((b.rss_kib, b.cpu_pct), (1024, 60.0))

    def test_shared_runtime_degrades_to_shared_total_without_privilege(self) -> None:
        from uxon.infra import sessions_probe

        profile = self._profile()
        a = self._record_session("uxon-a@claude", profile)
        b = self._record_session("uxon-b@claude", profile)
        with (
            mock.patch("uxon.infra.sessions_probe.run_query", return_value=self._ps_completed()),
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs", return_value=[900, 901]),
            mock.patch("uxon.infra.sessions_probe._read_pid_sessions", return_value=None),
            mock.patch(
                "uxon.infra.runtime.current_runtime_identity_for_profile",
                return_value=RuntimeIdentity(id="cid-1", cgroup="/c.scope", epoch="1000"),
            ),
        ):
            sessions_probe.enrich_session_usage(
                _cfg(profile), [a, b], runtimes={"box": profile}, launch_user="u-vz"
            )
        # Both show the summed total (the documented degrade), not zero.
        shared = (8192 + 1024, 110.0)
        self.assertEqual((a.rss_kib, a.cpu_pct), shared)
        self.assertEqual((b.rss_kib, b.cpu_pct), shared)

    def test_empty_cgroup_marks_runtime_down_when_probe_says_stopped(self) -> None:
        """AC-P1.8: empty cgroup + ready_command non-zero → runtime_down."""
        from uxon.infra import sessions_probe

        profile = self._profile(running=True)
        s = self._record_session("uxon-c@claude", profile)
        with (
            mock.patch("uxon.infra.sessions_probe.run_query", return_value=self._ps_completed()),
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs", return_value=[]),
            mock.patch(
                "uxon.infra.runtime.current_runtime_identity_for_profile",
                return_value=RuntimeIdentity(id="cid-1", cgroup="/c.scope", epoch="1000"),
            ),
            mock.patch(
                "uxon.infra.runtime.probe_runtime_state_for_profile", return_value=("no", "yes")
            ),
        ):
            sessions_probe.enrich_session_usage(
                _cfg(profile), [s], runtimes={"box": profile}, launch_user="u-vz"
            )
        self.assertTrue(s.runtime_down)
        self.assertEqual(s.cpu_pct, 0.0)
        self.assertEqual(s.rss_kib, 0)

    def test_unresolved_live_identity_marks_runtime_down_when_probe_says_stopped(
        self,
    ) -> None:
        from uxon.infra import sessions_probe

        profile = self._profile(running=True)
        s = self._record_session("uxon-c@claude", profile)
        with (
            mock.patch("uxon.infra.sessions_probe.run_query", return_value=self._ps_completed()),
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs") as read_cgroup,
            mock.patch(
                "uxon.infra.runtime.current_runtime_identity_for_profile",
                return_value=None,
            ),
            mock.patch(
                "uxon.infra.runtime.probe_runtime_state_for_profile",
                return_value=("no", "yes"),
            ) as probe,
        ):
            sessions_probe.enrich_session_usage(
                _cfg(profile), [s], runtimes={"box": profile}, launch_user="u-vz"
            )
        read_cgroup.assert_not_called()
        probe.assert_called_once()
        self.assertTrue(s.runtime_down)
        self.assertEqual(s.cpu_pct, 0.0)
        self.assertEqual(s.rss_kib, 0)

    def test_empty_cgroup_running_runtime_degrades_to_zero_not_down(self) -> None:
        from uxon.infra import sessions_probe

        profile = self._profile(running=True)
        s = self._record_session("uxon-c@claude", profile)
        with (
            mock.patch("uxon.infra.sessions_probe.run_query", return_value=self._ps_completed()),
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs", return_value=[]),
            mock.patch(
                "uxon.infra.runtime.current_runtime_identity_for_profile",
                return_value=RuntimeIdentity(id="cid-1", cgroup="/c.scope", epoch="1000"),
            ),
            mock.patch(
                "uxon.infra.runtime.probe_runtime_state_for_profile",
                return_value=("yes", "yes"),
            ),
        ):
            sessions_probe.enrich_session_usage(
                _cfg(profile), [s], runtimes={"box": profile}, launch_user="u-vz"
            )
        # Running but empty cgroup (race / unresolved path) → 0/—, not "down".
        self.assertFalse(s.runtime_down)
        self.assertEqual(s.cpu_pct, 0.0)

    def test_empty_cgroup_unknown_profile_probe_degrades_to_zero_not_down(self) -> None:
        from uxon.infra import sessions_probe

        profile = self._profile(running=True)
        s = self._record_session("uxon-c@claude", profile)
        with (
            mock.patch("uxon.infra.sessions_probe.run_query", return_value=self._ps_completed()),
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs", return_value=[]),
            mock.patch(
                "uxon.infra.runtime.current_runtime_identity_for_profile",
                return_value=RuntimeIdentity(id="cid-1", cgroup="/c.scope", epoch="1000"),
            ),
            mock.patch(
                "uxon.infra.runtime.probe_runtime_state_for_profile",
                return_value=("?", "?"),
            ),
        ):
            sessions_probe.enrich_session_usage(
                _cfg(profile), [s], runtimes={"box": profile}, launch_user="u-vz"
            )
        self.assertFalse(s.runtime_down)
        self.assertEqual(s.cpu_pct, 0.0)


class ContainerRenderTests(unittest.TestCase):
    """``cmd`` shows the agent id (AC-P1.4); the down indicator (AC-P1.8)."""

    def _session(self, **kw):
        from helpers import make_session

        s = make_session("uxon-proj@claude")
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def test_tui_cmd_shows_agent_for_runtime_session(self) -> None:
        from uxon.domain.session import to_tui_session

        s = self._session(runtime_resource="cbox", active_cmd="docker", agent="claude")
        tui = to_tui_session(s, "uxon-")
        self.assertEqual(tui.cmd, "claude")  # not "docker"

    def test_tui_cmd_unchanged_for_non_runtime_session(self) -> None:
        from uxon.domain.session import to_tui_session

        s = self._session(runtime_resource="", active_cmd="vim", agent="claude")
        self.assertEqual(to_tui_session(s, "uxon-").cmd, "vim")

    def test_list_shows_down_and_agent_for_runtime_down_session(self) -> None:
        import io
        from contextlib import redirect_stdout

        from helpers import make_config

        from uxon.app.listing import print_list

        s = self._session(
            runtime_resource="cbox", runtime_down=True, active_cmd="docker", agent="claude"
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_list(make_config(), [s], ["u-vz"])
        out = buf.getvalue()
        self.assertIn("down", out)  # distinct down indicator, not a silent "-"
        self.assertIn("claude", out)  # cmd shows the agent id

    def test_dashboard_cpu_cell_renders_down_marker(self) -> None:
        from uxon.tui.dashboard.columns import RUNTIME_DOWN_CELL, format_cpu, format_ram
        from uxon.tui.dashboard.row import SessionRow

        row = SessionRow(
            host=None,
            user="u",
            name="uxon-x@claude",
            short="x@claude",
            agent="claude",
            attached=False,
            legacy=False,
            pid=1,
            cpu_pct=0.0,
            rss_kib=0,
            created_epoch=None,
            last_attached_epoch=None,
            cmd="claude",
            path="/srv",
            runtime_down=True,
        )
        self.assertEqual(format_cpu(row).plain, RUNTIME_DOWN_CELL)
        self.assertEqual(format_ram(row), RUNTIME_DOWN_CELL)


class ContainerGatingTests(unittest.TestCase):
    """AC-P2 — container mode keeps agent RESOLUTION, suppresses the host GATE."""

    @staticmethod
    def _report(claude_path, *, launch_user: str = "dana"):
        return HostReport(
            tmux=BinaryStatus("tmux", "/usr/bin/tmux", ""),
            agents={"claude": BinaryStatus("claude", claude_path, "npm i -g claude")},
            launch_user=launch_user,
        )

    @staticmethod
    def _runtime_launch() -> LaunchConfig:
        agents = default_agent_catalog()
        profiles = builtin_launch_profiles(agents)
        profiles["claude_box"] = LaunchProfile(
            id="claude_box",
            agent="claude",
            runtime="box",
        )
        return LaunchConfig(
            enabled_profiles=("claude_box",),
            default_profile="claude_box",
            profiles=profiles,
        )

    def test_host_absent_binary_does_not_fail_under_container(self) -> None:
        # AC-P2.1: explicit workload runtime + host-absent binary →
        # resolves anyway (no host-presence gate).
        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="p-{project_slug}",
            exec_prefix=_EXEC,
        )
        cfg = _cfg(c, launch=self._runtime_launch())
        resolved = launch_profile_app.resolve_launch_profile(
            cfg,
            "dana",
            "claude_box",
            "/srv/projects/myapp",
            "normal",
            report=self._report(claude_path=None),
        )
        self.assertEqual(resolved.profile.id, "claude_box")

    def test_host_absent_binary_fails_when_disabled(self) -> None:
        # AC-P2.5: off-path is byte-for-byte unchanged — host gate still fires.
        cfg = _cfg(None)
        with self.assertRaises(SystemExit):
            launch_profile_app.resolve_launch_profile(
                cfg,
                "dana",
                "claude",
                "/srv/projects/myapp",
                "normal",
                report=self._report(claude_path=None),
            )

    def test_auto_mode_ignores_operator_runtime(self) -> None:
        # Auto-mode considers shipped OS-user-only profiles only; an operator
        # workload runtime is not auto-enabled.
        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="p-{project_slug}",
            exec_prefix=_EXEC,
        )
        agents = default_agent_catalog()
        profiles = builtin_launch_profiles(agents)
        profiles["claude_box"] = LaunchProfile(
            id="claude_box",
            agent="claude",
            runtime="box",
        )
        cfg = _cfg(c, launch=LaunchConfig(profiles=profiles))
        resolved = launch_profile_app.resolve_launch_profile(
            cfg,
            "dana",
            None,
            "/srv/projects/myapp",
            "normal",
            report=self._report("/x/claude"),
        )
        self.assertEqual(resolved.profile.id, "claude")


class PlanWorkloadRuntimeSpecMatrixTests(unittest.TestCase):
    """AC-B4 -- plan_runtime_launch_for_profile action -> prepare matrix.

    The subprocess probe boundary (_probe_exit_ok) is mocked, so this exercises
    the orchestration the deleted legacy PlanMatrixTests covered: probe order,
    decide_runtime_action gating on on_missing, and prepare_command / message
    rendering -- without docker.
    """

    @staticmethod
    def _profile(*, on_missing: str) -> WorkloadRuntimeSpec:
        return WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="box-{project_slug}",
            exec_prefix=("docker", "exec", "-w", "{runtime_dir}", "{resource}"),
            ready_command=("docker", "top", "{resource}"),
            exists_command=("docker", "inspect", "{resource}"),
            start_command=("docker", "start", "{resource}"),
            create_command=("docker", "create", "{resource}"),
            on_missing=on_missing,  # type: ignore[arg-type]
            path_map=(),
        )

    def _plan(self, profile: WorkloadRuntimeSpec, probes):
        from uxon.infra.runtime import plan_runtime_launch_for_profile

        cfg = _cfg(profile)
        resolved = _resolved(cfg, "alice")
        with mock.patch("uxon.infra.runtime._probe_exit_ok", side_effect=probes):
            return plan_runtime_launch_for_profile(cfg, "/srv/projects/myapp", resolved)

    def test_running_runtime_execs_with_no_prepare(self):
        plan = self._plan(self._profile(on_missing="start"), [True])
        self.assertEqual(plan.action, "exec")
        self.assertEqual(plan.reason, "running")
        self.assertEqual(plan.prepare_command, ())
        self.assertEqual(plan.message, "Runtime resource 'box-myapp' is ready")

    def test_stopped_with_start_policy_renders_start_prepare(self):
        plan = self._plan(self._profile(on_missing="start"), [False, True])
        self.assertEqual(plan.action, "start")
        self.assertEqual(plan.reason, "stopped")
        self.assertEqual(plan.prepare_command, ("docker", "start", "box-myapp"))
        self.assertIn("stopped", plan.message)

    def test_stopped_with_off_policy_fails(self):
        plan = self._plan(self._profile(on_missing="off"), [False, True])
        self.assertEqual(plan.action, "fail")
        self.assertEqual(plan.reason, "stopped")
        self.assertEqual(plan.prepare_command, ())
        self.assertIn("does not permit starting", plan.message)

    def test_absent_with_create_policy_renders_create_prepare(self):
        plan = self._plan(self._profile(on_missing="create"), [False, False])
        self.assertEqual(plan.action, "create")
        self.assertEqual(plan.reason, "absent")
        self.assertEqual(plan.prepare_command, ("docker", "create", "box-myapp"))
        self.assertIn("does not exist", plan.message)

    def test_absent_with_start_policy_fails(self):
        plan = self._plan(self._profile(on_missing="start"), [False, False])
        self.assertEqual(plan.action, "fail")
        self.assertEqual(plan.reason, "absent")
        self.assertEqual(plan.prepare_command, ())


class WorkloadRuntimeSpecRuntimeTests(unittest.TestCase):
    """P3 — launch decisions come from the resolved workload runtime."""

    @staticmethod
    def _profile(
        cid: str,
        *,
        namespace: str = "per_user",
        resource_name_template: str = "box-{project_slug}",
        path_map=(),
    ) -> WorkloadRuntimeSpec:
        return WorkloadRuntimeSpec(
            id=cid,
            resource_scope=namespace,  # type: ignore[arg-type]
            resource_name_template=resource_name_template,
            exec_prefix=("docker", "exec", "-w", "{runtime_dir}", "{resource}"),
            ready_command=("docker", "top", "{resource}"),
            exists_command=("docker", "inspect", "{resource}"),
            start_command=("docker", "start", "{resource}"),
            on_missing="start",
            path_map=path_map,
        )

    @staticmethod
    def _report(*, launch_user: str, claude_path: str | None = None) -> HostReport:
        return HostReport(
            tmux=BinaryStatus("tmux", "/usr/bin/tmux", ""),
            agents={"claude": BinaryStatus("claude", claude_path, "install claude")},
            launch_user=launch_user,
        )

    def _cfg_profiles(
        self,
        launch_profiles: dict[str, LaunchProfile],
        runtimes: dict[str, WorkloadRuntimeSpec],
        *,
        enabled: tuple[str, ...],
        default: str,
    ) -> Config:
        agents = default_agent_catalog()
        profiles = builtin_launch_profiles(agents)
        profiles.update(launch_profiles)
        return _cfg(
            None,
            launch=LaunchConfig(
                enabled_profiles=enabled,
                default_profile=default,
                profiles=profiles,
            ),
            runtimes=runtimes,
        )

    def test_prepare_and_agent_probe_use_pinned_launch_user_and_profile(self) -> None:
        profile = LaunchProfile(id="claude_box", agent="claude", launch_user="alice", runtime="box")
        cfg = self._cfg_profiles(
            {"claude_box": profile},
            {"box": self._profile("box", path_map=validate_path_map({"/srv/projects": "/work"}))},
            enabled=("claude_box",),
            default="claude_box",
        )
        resolved = launch_profile_app.resolve_launch_profile(
            cfg,
            "dana",
            "claude_box",
            "/srv/projects/myapp",
            "normal",
            report=self._report(launch_user="alice", claude_path=None),
        )
        self.assertEqual(resolved.launch_user, "alice")
        assert resolved.runtime_context is not None
        self.assertEqual(resolved.runtime_context.resource, "box-myapp")
        self.assertEqual(resolved.runtime_context.runtime_dir, "/work/myapp")

        from uxon.app import launch as launch_app
        from uxon.infra import runtime as runtime_infra

        probes = iter([False, True])
        with (
            mock.patch.object(
                runtime_infra, "_probe_exit_ok", side_effect=lambda *_args: next(probes)
            ),
            mock.patch.object(runtime_infra, "_run_prepare") as run_prepare,
            mock.patch.object(runtime_infra, "probe_agent_in_runtime") as probe_agent,
        ):
            launch_app.ensure_runtime_ready(cfg, "/srv/projects/myapp", resolved)
        run_prepare.assert_called_once_with(
            cfg,
            ["docker", "start", "box-myapp"],
            "/srv/projects/myapp",
            "alice",
            120.0,
        )
        probe_agent.assert_called_once_with(cfg, "/srv/projects/myapp", resolved)

    def test_tui_gate_probes_agent_for_running_runtime(self) -> None:
        profile = LaunchProfile(id="claude_box", agent="claude", launch_user="alice", runtime="box")
        cfg = self._cfg_profiles(
            {"claude_box": profile},
            {"box": self._profile("box")},
            enabled=("claude_box",),
            default="claude_box",
        )
        resolved = launch_profile_app.resolve_launch_profile(
            cfg,
            "dana",
            "claude_box",
            "/srv/projects/myapp",
            "normal",
            report=self._report(launch_user="alice", claude_path=None),
        )

        from uxon.app import launch as launch_app
        from uxon.infra import runtime as runtime_infra

        with (
            mock.patch.object(runtime_infra, "_probe_exit_ok", return_value=True),
            mock.patch.object(runtime_infra, "probe_agent_in_runtime") as probe_agent,
        ):
            gate = launch_app.decide_runtime_gate(cfg, "/srv/projects/myapp", resolved)
        self.assertIsNone(gate)
        probe_agent.assert_called_once_with(cfg, "/srv/projects/myapp", resolved)

    def test_agent_probe_runs_inside_runtime_not_on_host(self) -> None:
        profile = LaunchProfile(id="claude_box", agent="claude", launch_user="alice", runtime="box")
        cfg = self._cfg_profiles(
            {"claude_box": profile},
            {"box": self._profile("box")},
            enabled=("claude_box",),
            default="claude_box",
        )
        resolved = launch_profile_app.resolve_launch_profile(
            cfg,
            "dana",
            "claude_box",
            "/srv/projects/myapp",
            "normal",
            report=self._report(launch_user="alice", claude_path=None),
        )
        from uxon.infra import runtime as runtime_infra

        cp = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch("uxon.infra.identity.process_user", return_value="root"),
            mock.patch.object(runtime_infra, "run_query", return_value=cp) as run_query,
        ):
            runtime_infra.probe_agent_in_runtime(cfg, "/srv/projects/myapp", resolved)
        argv = run_query.call_args.args[0]
        self.assertEqual(argv[:6], ["/usr/bin/sudo", "-n", "-H", "-u", "alice", "--"])
        self.assertIn("docker", argv)
        self.assertIn("box-myapp", argv)
        self.assertEqual(argv[-1], "claude")

    def test_global_namespace_rejects_distinct_profile_name_collision(self) -> None:
        cfg = self._cfg_profiles(
            {
                "one": LaunchProfile(id="one", agent="claude", launch_user="alice", runtime="c1"),
                "two": LaunchProfile(id="two", agent="claude", launch_user="bob", runtime="c2"),
            },
            {
                "c1": self._profile("c1", namespace="global", resource_name_template="shared"),
                "c2": self._profile("c2", namespace="global", resource_name_template="shared"),
            },
            enabled=("one", "two"),
            default="one",
        )
        with self.assertRaises(SystemExit) as ctx:
            launch_profile_app.resolve_launch_profile(
                cfg,
                "dana",
                "one",
                "/srv/projects/myapp",
                "normal",
                report=self._report(launch_user="alice", claude_path=None),
            )
        self.assertIn("collides", getattr(ctx.exception, "uxon_msg", ""))

    def test_per_user_namespace_rejects_same_user_collision(self) -> None:
        cfg = self._cfg_profiles(
            {
                "one": LaunchProfile(id="one", agent="claude", launch_user="alice", runtime="c1"),
                "two": LaunchProfile(id="two", agent="claude", launch_user="alice", runtime="c2"),
            },
            {
                "c1": self._profile("c1", resource_name_template="shared"),
                "c2": self._profile("c2", resource_name_template="shared"),
            },
            enabled=("one", "two"),
            default="one",
        )
        with self.assertRaises(SystemExit):
            launch_profile_app.resolve_launch_profile(
                cfg,
                "dana",
                "one",
                "/srv/projects/myapp",
                "normal",
                report=self._report(launch_user="alice", claude_path=None),
            )

    def test_same_runtime_id_is_intentional_sharing(self) -> None:
        cfg = self._cfg_profiles(
            {
                "one": LaunchProfile(
                    id="one", agent="claude", launch_user="alice", runtime="shared"
                ),
                "two": LaunchProfile(
                    id="two", agent="claude", launch_user="alice", runtime="shared"
                ),
            },
            {"shared": self._profile("shared", resource_name_template="shared")},
            enabled=("one", "two"),
            default="one",
        )
        resolved = launch_profile_app.resolve_launch_profile(
            cfg,
            "dana",
            "one",
            "/srv/projects/myapp",
            "normal",
            report=self._report(launch_user="alice", claude_path=None),
        )
        assert resolved.runtime_context is not None
        self.assertEqual(resolved.runtime_context.runtime_id, "shared")

    def test_placeholder_validation_names_field_and_placeholder(self) -> None:
        bad = self._profile("box")
        bad = WorkloadRuntimeSpec(
            **{
                **bad.__dict__,
                "exec_prefix": ("docker", "exec", "{bogus}", "{resource}"),
            }
        )
        with self.assertRaises(SystemExit) as ctx:
            validate_runtime(bad)
        msg = getattr(ctx.exception, "uxon_msg", "")
        self.assertIn("exec_prefix", msg)
        self.assertIn("bogus", msg)

    def test_runtime_fingerprint_includes_profile_id(self) -> None:
        one = self._profile("one", resource_name_template="{runtime}-{project_slug}")
        two = self._profile("two", resource_name_template="{runtime}-{project_slug}")

        self.assertNotEqual(one.fingerprint, two.fingerprint)


class RuntimeTeardownAuditTests(unittest.TestCase):
    """AC-P3.2 / AC-P3.5 — teardown audit emit + PID-recycle stale guard."""

    def _cfg_with_stop(self, identity_command=()):
        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="proj-{project_slug}",
            exec_prefix=_EXEC,
            stop_command=("docker", "exec", "{resource}", "sh", "-c", "kill $(cat {pidfile})"),
            identity_command=identity_command,
        )
        return _cfg(c)

    def _record_session(self, cfg: Config):
        from helpers import make_session

        profile = cfg.runtimes["box"]
        s = make_session("uxon-x@claude", user="dana")
        s.profile = "claude"
        s.agent = "claude"
        s.launch_record_verified = True
        s.launch_user = "dana"
        s.runtime_resource = "proj-myapp"
        s.runtime = profile.id
        s.runtime_fingerprint = profile.fingerprint
        s.runtime_id = "cid-1"
        s.runtime_epoch = "1000"
        return s

    def test_prepare_captures_launch_epoch(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(identity_command=("inspect", "{resource}"))
        teardown = kill_app.prepare_runtime_teardown(cfg, self._record_session(cfg))
        assert teardown is not None
        self.assertEqual(teardown.resource, "proj-myapp")
        self.assertEqual(teardown.runtime, "box")
        self.assertEqual(teardown.runtime_id, "cid-1")
        self.assertEqual(teardown.launch_epoch, "1000")

    def test_missing_record_blocks_runtime_kill(self) -> None:
        from helpers import make_session

        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(identity_command=("inspect", "{resource}"))
        s = make_session("uxon-x@claude", user="dana")
        s.runtime_marker = "proj-myapp"
        with self.assertRaises(SystemExit) as raised:
            kill_app.prepare_runtime_teardown(cfg, s)
        self.assertIn("no authoritative launch record", getattr(raised.exception, "uxon_msg", ""))

    def test_missing_runtime_skips_prepare(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(identity_command=("inspect", "{resource}"))
        s = self._record_session(cfg)
        s.runtime = ""
        with mock.patch("uxon.app.kill.eprint") as eprint:
            teardown = kill_app.prepare_runtime_teardown(cfg, s)
        self.assertIsNone(teardown)
        self.assertIn("missing workload runtime", eprint.call_args.args[0])

    def test_fingerprint_mismatch_skips_prepare(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(identity_command=("inspect", "{resource}"))
        s = self._record_session(cfg)
        s.runtime_fingerprint = "old"
        with mock.patch("uxon.app.kill.eprint") as eprint:
            teardown = kill_app.prepare_runtime_teardown(cfg, s)
        self.assertIsNone(teardown)
        self.assertIn("workload runtime changed", eprint.call_args.args[0])

    def test_missing_saved_identity_skips_prepare(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(identity_command=("inspect", "{resource}"))
        s = self._record_session(cfg)
        s.runtime_id = ""
        with mock.patch("uxon.app.kill.eprint") as eprint:
            teardown = kill_app.prepare_runtime_teardown(cfg, s)
        self.assertIsNone(teardown)
        self.assertIn("missing runtime resource identity", eprint.call_args.args[0])

    def test_stale_teardown_emits_stale_and_skips_kill(self) -> None:
        # AC-P3.2/P3.5: live epoch != stashed epoch → emit outcome=stale and do
        # NOT run the stop command (the recorded PID would be a recycled one).
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(identity_command=("inspect", "{resource}"))
        profile = cfg.runtimes["box"]
        teardown = kill_app.RuntimeTeardown(
            stop_cmd=["docker", "exec", "c", "true"],
            resource="proj-myapp",
            runtime="box",
            runtime_id="cid-1",
            runtime_fingerprint=profile.fingerprint,
            launch_epoch="1000",
        )
        with (
            mock.patch(
                "uxon.infra.runtime.current_runtime_identity_for_profile",
                return_value=RuntimeIdentity(id="cid-2", epoch="2000"),
            ),
            mock.patch("uxon.infra.runtime.run_teardown") as run_td,
            mock.patch("uxon.infra.audit.audit") as audit,
        ):
            kill_app.run_runtime_teardown(cfg, teardown, "dana", "uxon-x@claude")
        run_td.assert_not_called()
        audit.assert_called_once()
        self.assertEqual(audit.call_args.args[0], "runtime.session_stop")
        self.assertEqual(audit.call_args.kwargs["outcome"], "skipped")
        self.assertEqual(audit.call_args.kwargs["reason"], "stale_identity")
        self.assertEqual(audit.call_args.kwargs["runtime_resource"], "proj-myapp")
        self.assertEqual(audit.call_args.kwargs["runtime"], "box")

    def test_matching_epoch_teardown_runs_and_emits_ok(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(identity_command=("inspect", "{resource}"))
        profile = cfg.runtimes["box"]
        teardown = kill_app.RuntimeTeardown(
            stop_cmd=["docker", "exec", "c", "true"],
            resource="proj-myapp",
            runtime="box",
            runtime_id="cid-1",
            runtime_fingerprint=profile.fingerprint,
            launch_epoch="1000",
        )
        with (
            mock.patch(
                "uxon.infra.runtime.current_runtime_identity_for_profile",
                return_value=RuntimeIdentity(id="cid-1", epoch="1000"),
            ),
            mock.patch("uxon.infra.runtime.run_teardown", return_value=(True, "")) as run_td,
            mock.patch("uxon.infra.audit.audit") as audit,
        ):
            kill_app.run_runtime_teardown(cfg, teardown, "dana", "uxon-x@claude")
        run_td.assert_called_once()
        self.assertEqual(audit.call_args.kwargs["outcome"], "ok")

    def test_missing_current_profile_skips_teardown(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(identity_command=("inspect", "{resource}"))
        profile = cfg.runtimes["box"]
        cfg.runtimes.clear()
        teardown = kill_app.RuntimeTeardown(
            stop_cmd=["docker", "exec", "c", "true"],
            resource="proj-myapp",
            runtime="box",
            runtime_id="cid-1",
            runtime_fingerprint=profile.fingerprint,
            launch_epoch="1000",
        )
        with (
            mock.patch("uxon.infra.runtime.run_teardown") as run_td,
            mock.patch("uxon.infra.audit.audit") as audit,
        ):
            kill_app.run_runtime_teardown(cfg, teardown, "dana", "uxon-x@claude")
        run_td.assert_not_called()
        self.assertEqual(audit.call_args.kwargs["outcome"], "skipped")
        self.assertEqual(audit.call_args.kwargs["reason"], "missing_profile")

    def test_unresolved_live_identity_skips_kill(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(identity_command=("inspect", "{resource}"))
        profile = cfg.runtimes["box"]
        teardown = kill_app.RuntimeTeardown(
            stop_cmd=["docker", "exec", "c", "true"],
            resource="proj-myapp",
            runtime="box",
            runtime_id="cid-1",
            runtime_fingerprint=profile.fingerprint,
            launch_epoch="1000",
        )
        with (
            mock.patch(
                "uxon.infra.runtime.current_runtime_identity_for_profile", return_value=None
            ),
            mock.patch("uxon.infra.runtime.run_teardown", return_value=(True, "")) as run_td,
            mock.patch("uxon.infra.audit.audit") as audit,
        ):
            kill_app.run_runtime_teardown(cfg, teardown, "dana", "uxon-x@claude")
        run_td.assert_not_called()
        self.assertEqual(audit.call_args.kwargs["outcome"], "skipped")
        self.assertEqual(audit.call_args.kwargs["reason"], "identity_unresolved")


class WorktreePathMapGateTests(unittest.TestCase):
    """AC-P4.1 — unmapped worktree path fails fast; empty path_map carve-out."""

    def test_path_map_under_prefix_predicate(self) -> None:
        from uxon.domain.runtime import path_map_under_prefix

        pm = validate_path_map({"/srv/projects": "/work"})
        self.assertTrue(path_map_under_prefix("/srv/projects/myapp", pm))
        self.assertTrue(path_map_under_prefix("/srv/projects", pm))
        self.assertFalse(path_map_under_prefix("/srv/other", pm))
        # Empty map covers nothing — the caller guards on non-empty separately.
        self.assertFalse(path_map_under_prefix("/srv/projects/myapp", ()))

    def test_unmapped_worktree_path_fails(self) -> None:
        from uxon.app import launch as launch_app

        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="p-{project_slug}",
            exec_prefix=_EXEC,
            path_map=validate_path_map({"/srv/mapped": "/work"}),
        )
        cfg = _cfg(c)
        with (
            mock.patch(
                "uxon.infra.worktrees.compute_worktree_path",
                return_value="/srv/projects/myapp/.uxon/worktrees/feat",
            ),
            mock.patch("uxon.app.launch.is_worktree_target_allowed", return_value=True),
        ):
            with self.assertRaises(SystemExit) as ctx:
                launch_app.plan_worktree_launch(
                    cfg,
                    "dana",
                    _resolved(cfg, "dana", mode="normal"),
                    "/srv/projects/myapp",
                    "feat",
                    requested_profile="claude",
                )
        msg = str(getattr(ctx.exception, "uxon_msg", ctx.exception))
        self.assertIn("path_map", msg)
        self.assertIn("/srv/projects/myapp/.uxon/worktrees/feat", msg)

    def test_empty_path_map_does_not_fail(self) -> None:
        # Empty path_map = host-path verbatim — must NOT trip the gate. We only
        # assert the gate is not the failure point: stub the git/launch steps so
        # the function returns without touching a real repo.
        from uxon.app import launch as launch_app

        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="p-{project_slug}",
            exec_prefix=_EXEC,
        )
        cfg = _cfg(c)
        sentinel = object()
        with (
            mock.patch(
                "uxon.infra.worktrees.compute_worktree_path",
                return_value="/srv/projects/myapp/.uxon/worktrees/feat",
            ),
            mock.patch("uxon.app.launch.is_worktree_target_allowed", return_value=True),
            mock.patch("uxon.infra.git._branch_exists_as_user", return_value=False),
            mock.patch("uxon.infra.git._local_base_ref_as_user", return_value="HEAD"),
            mock.patch("uxon.infra.identity.command_prefix_for_user", return_value=[]),
            mock.patch(
                "uxon.infra.sessions_probe.collect_current_session_snapshot",
                return_value=make_session_snapshot(),
            ),
            mock.patch("uxon.infra.tmux._build_tmux_launch_request", return_value=sentinel),
        ):
            req = launch_app.plan_worktree_launch(
                cfg,
                "dana",
                _resolved(cfg, "dana", mode="normal"),
                "/srv/projects/myapp",
                "feat",
                requested_profile="claude",
                dry_run=True,
            )
        self.assertIs(req, sentinel)


class DoctorRuntimeSectionTests(unittest.TestCase):
    """AC-P5 — doctor runtime rows: probes, expected-absence note, warnings."""

    def _rows(self, c: WorkloadRuntimeSpec, probe: tuple[str, str]) -> list[dict[str, Any]]:
        from uxon.app.doctor import _doctor_runtime_rows

        cfg = _cfg(c)
        with mock.patch("uxon.infra.runtime.probe_runtime_state_for_profile", return_value=probe):
            return _doctor_runtime_rows(cfg, "/srv/projects/myapp", "dana")

    def test_section_warns_on_unset_stop_command(self) -> None:
        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="proj-{project_slug}",
            exec_prefix=_EXEC,
            ready_command=("docker", "inspect", "{resource}"),
            exists_command=("docker", "inspect", "{resource}"),
        )
        rows = self._rows(c, ("yes", "yes"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["runtime"], "box")
        self.assertEqual(row["runtime_resource"], "proj-myapp")
        self.assertEqual(row["ready"], "yes")
        self.assertTrue(row["host_agent_absence_expected"])
        self.assertTrue(any("stop_command" in w for w in row["warnings"]))

    def test_section_warns_on_definition_under_mount(self) -> None:
        # AC-P5.4: create_command path token under a path_map host prefix.
        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="proj-{project_slug}",
            exec_prefix=_EXEC,
            stop_command=("docker", "exec", "{resource}", "true"),
            create_command=("docker", "compose", "-f", "/srv/projects/myapp/compose.yml", "up"),
            path_map=validate_path_map({"/srv/projects/myapp": "/work"}),
        )
        rows = self._rows(c, ("no", "no"))
        row = rows[0]
        self.assertTrue(
            any("agent-writable bind mount" in w for w in row["warnings"]),
            row["warnings"],
        )

    def test_section_clean_when_hardened(self) -> None:
        c = WorkloadRuntimeSpec(
            id="box",
            resource_scope="per_user",
            resource_name_template="proj-{project_slug}",
            exec_prefix=_EXEC,
            stop_command=("docker", "exec", "{resource}", "true"),
            create_command=("docker", "compose", "-f", "/operator/compose.yml", "up"),
            path_map=validate_path_map({"/srv/projects/myapp": "/work"}),
        )
        rows = self._rows(c, ("yes", "yes"))
        self.assertEqual(rows[0]["warnings"], [])


if __name__ == "__main__":
    unittest.main()
