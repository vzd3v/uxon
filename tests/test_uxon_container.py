"""Container-agnostic launch: resolution, exec wrap, policy, safety, caveat.

In-gate mocked-boundary tests (no runtime). Cover the Part B contracts that
are assertable without docker/podman:

* exec-site wrap shape (AC-B1) + disabled parity (AC-B6),
* deterministic name resolution per (user, project dir) (AC-B2),
* ``path_map`` longest-prefix container path (AC-B3),
* the ``on_missing`` × ``on_missing_mode`` policy matrix with probe results
  mocked at the subprocess boundary (AC-B4),
* name / path / ``.format`` safety incl. hostile-dir-name cases (AC-B8),
* the ``[container].enabled`` kill caveat (Security MEDIUM-2).

The real-runtime harness (in-container exec, kill reaping) is P4.
"""

from __future__ import annotations

import unittest
from unittest import mock

import uxon.app.launch_profile as launch_profile_app
from uxon.domain.agents import default_agent_catalog
from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.container import (
    CONTAINER_CGROUP_ENV,
    CONTAINER_EPOCH_ENV,
    CONTAINER_ID_ENV,
    CONTAINER_NAME_ENV,
    SESSION_ENV,
    ContainerConfig,
    ContainerProfile,
    apply_path_map,
    container_pidfile,
    decide_container_action,
    is_valid_container_name,
    kill_caveat,
    render_stop_template,
    resolve_container_name,
    validate_container_name,
    validate_container_profile,
    validate_path_map,
    wrap_agent_for_container,
)
from uxon.domain.host_report import BinaryStatus, HostReport
from uxon.domain.launch_profiles import (
    ContainerContext,
    ContainerIdentity,
    LaunchConfig,
    LaunchProfile,
    ResolvedLaunchProfile,
    builtin_launch_profiles,
)


def _cfg(container: ContainerConfig, **overrides) -> Config:
    """Minimal Config carrying a ContainerConfig (other fields are inert here)."""
    agents = default_agent_catalog()
    profiles = builtin_launch_profiles(agents)
    container_profiles = {}
    if container.enabled:
        container_profiles["box"] = ContainerProfile(
            id="box",
            runtime_namespace="per_user",
            name_template=container.name or container.name_template,
            exec_template=container.exec_template,
            is_running_cmd=container.is_running_cmd,
            exists_cmd=container.exists_cmd,
            start_template=container.start_template,
            create_template=container.create_template,
            stop_template=container.stop_template,
            resolve_cmd=container.resolve_cmd,
            on_missing=container.on_missing,
            on_missing_mode=container.on_missing_mode,
            path_map=container.path_map,
        )
        profiles["claude"] = LaunchProfile(id="claude", agent="claude", container_profile="box")
    launch = LaunchConfig(default_profile="claude", profiles=profiles)
    base = dict(
        runtime_user="",
        default_launch_mode="caller",
        enable_all_users_list=False,
        launch_user_by_caller={},
        session_users=[],
        allowed_roots=["/srv"],
        session_prefix="uxon-",
        legacy_session_prefixes=(),
        enabled_agents=("claude",),
        default_agent="claude",
        new_project_root="/srv",
        repeat_noninteractive_mode="fail",
        tmux_socket_template="/tmp/uxon-{user}.sock",
        tui_refresh_interval_seconds=2.0,
        git_create_enabled=False,
        default_git_remote_profile="",
        git_remote_profiles=[],
        tmux_manage_options=False,
        tmux_options={},
        tmux_server_options={},
        tmux_append_server_options={},
        agents=agents,
        launch=launch,
        container=container,
        container_profiles=container_profiles,
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
    from uxon.domain.container import apply_path_map, resolve_profile_container_name
    from uxon.domain.session import slugify

    profile = cfg.launch.profiles[profile_id]
    context = None
    if profile.container_profile:
        container_profile = cfg.container_profiles[profile.container_profile]
        dir_token = apply_path_map(target_dir, container_profile.path_map)
        context = ContainerContext(
            profile_id=container_profile.id,
            name=resolve_profile_container_name(
                container_profile,
                user=launch_user,
                launch_profile=profile.id,
                agent=profile.agent,
                project_slug=slugify(target_dir.rsplit("/", 1)[-1]),
            ),
            dir_token=dir_token,
            profile_fingerprint=container_profile.fingerprint,
        )
    return ResolvedLaunchProfile(
        profile=profile,
        agent=cfg.agents[profile.agent],
        launch_user=launch_user,
        mode_id=mode,
        container=context,
    )


_EXEC = ("docker", "exec", "-it", "-w", "{dir}", "{name}")


def _managed_create_cmd(req):
    assert req.managed is not None
    return req.managed.create_cmd


class NameResolutionTests(unittest.TestCase):
    """AC-B2 — same container per (user, project dir), never per-session."""

    def test_name_template_expands_project_slug(self) -> None:
        c = ContainerConfig(enabled=True, name_template="proj-{project_slug}", exec_template=_EXEC)
        from uxon.infra.tmux import resolve_container

        name, _dir = resolve_container(_cfg(c), "/srv/projects/myapp", "dana")
        self.assertEqual(name, "proj-myapp")

    def test_same_dir_same_name_different_dir_differs(self) -> None:
        c = ContainerConfig(enabled=True, name_template="proj-{project_slug}", exec_template=_EXEC)
        from uxon.infra.tmux import resolve_container

        cfg = _cfg(c)
        a1, _ = resolve_container(cfg, "/srv/projects/myapp", "dana")
        a2, _ = resolve_container(cfg, "/srv/projects/myapp", "dana")
        b, _ = resolve_container(cfg, "/srv/projects/other", "dana")
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, b)

    def test_project_name_override_wins_over_template(self) -> None:
        c = ContainerConfig(
            enabled=True, name_template="proj-{project_slug}", exec_template=_EXEC, name="myapp-dev"
        )
        self.assertEqual(
            resolve_container_name(c, user="dana", project_slug="myapp", dir_token="/work"),
            "myapp-dev",
        )

    def test_user_placeholder(self) -> None:
        c = ContainerConfig(enabled=True, name_template="{user}-box", exec_template=_EXEC)
        self.assertEqual(
            resolve_container_name(c, user="dana", project_slug="x", dir_token="/work"),
            "dana-box",
        )


class PathMapTests(unittest.TestCase):
    """AC-B3 — {dir} is the container-side path; allowed_roots stays host-side."""

    def test_no_map_returns_host_path(self) -> None:
        self.assertEqual(apply_path_map("/srv/projects/myapp", ()), "/srv/projects/myapp")

    def test_longest_prefix_match(self) -> None:
        pm = validate_path_map({"/srv": "/srv-root", "/srv/projects/myapp": "/work"})
        self.assertEqual(apply_path_map("/srv/projects/myapp/sub", pm), "/work/sub")

    def test_exact_prefix_match(self) -> None:
        pm = validate_path_map({"/srv/projects/myapp": "/work"})
        self.assertEqual(apply_path_map("/srv/projects/myapp", pm), "/work")

    def test_resolve_container_applies_path_map_to_dir(self) -> None:
        c = ContainerConfig(
            enabled=True,
            name_template="proj-{project_slug}",
            exec_template=_EXEC,
            path_map=validate_path_map({"/srv/projects/myapp": "/work"}),
        )
        from uxon.infra.tmux import resolve_container

        _name, dir_token = resolve_container(_cfg(c), "/srv/projects/myapp", "dana")
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
        disabled = self._build(_cfg(ContainerConfig()))
        # A config with [container] present but enabled=false must match the
        # no-container build exactly (AC-B6 parity).
        also_disabled = self._build(_cfg(ContainerConfig(enabled=False, name_template="x")))
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

    def test_enabled_prepends_exec_template(self) -> None:
        c = ContainerConfig(enabled=True, name_template="proj-{project_slug}", exec_template=_EXEC)
        req = self._build(_cfg(c))
        # exec_template(resolved) + the per-session wrapper + agent argv, in
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

    def test_enabled_with_path_map_uses_container_dir(self) -> None:
        c = ContainerConfig(
            enabled=True,
            name_template="proj-{project_slug}",
            exec_template=_EXEC,
            path_map=validate_path_map({"/srv/projects/myapp": "/work"}),
        )
        req = self._build(_cfg(c))
        cmd = list(_managed_create_cmd(req))
        # -w token is the container-side path, not the host path.
        self.assertIn("/work", cmd)
        self.assertNotIn("/srv/projects/myapp", cmd[cmd.index("docker") :])

    def test_stop_template_stashes_name_and_wraps_agent(self) -> None:
        # Teardown opt-in (AC-B5): the new-session argv stashes the resolved
        # name in the session env and the agent is wrapped so it exports the
        # per-session marker AND records its in-container PID, while the exec
        # prefix is unchanged.
        c = ContainerConfig(
            enabled=True,
            name_template="proj-{project_slug}",
            exec_template=_EXEC,
            stop_template=("docker", "exec", "{name}", "sh", "-c", "kill $(cat {pidfile})"),
        )
        cmd = list(_managed_create_cmd(self._build(_cfg(c))))
        # ``-e UXON_CONTAINER=proj-myapp`` rides the new-session argv.
        self.assertIn("-e", cmd)
        self.assertIn(f"{CONTAINER_NAME_ENV}=proj-myapp", cmd)
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

    def test_enabled_without_stop_template_carries_marker_but_no_pidfile(self) -> None:
        # After the hoist: enabled + no stop_template (+ no resolve_cmd, the
        # default) still carries ``-e UXON_CONTAINER=<bare name>`` and wraps the
        # agent to export ``UXON_SESSION``, but does NOT write a pidfile, and the
        # identity vars are ABSENT (resolve_cmd unset → degrade path).
        c = ContainerConfig(enabled=True, name_template="proj-{project_slug}", exec_template=_EXEC)
        cmd = list(_managed_create_cmd(self._build(_cfg(c))))
        # Bare-name marker rides the session env.
        self.assertIn("-e", cmd)
        self.assertIn(f"{CONTAINER_NAME_ENV}=proj-myapp", cmd)
        # Identity vars absent without resolve_cmd (degrade).
        joined = " ".join(cmd)
        self.assertNotIn(CONTAINER_ID_ENV, joined)
        self.assertNotIn(CONTAINER_CGROUP_ENV, joined)
        self.assertNotIn(CONTAINER_EPOCH_ENV, joined)
        # The agent IS wrapped to export the per-session marker, with NO pidfile.
        tail = cmd[cmd.index("docker") :]
        self.assertEqual(tail[6:8], ["sh", "-c"])
        self.assertIn(f"export {SESSION_ENV}=", tail[8])
        self.assertNotIn("echo $$", tail[8])
        self.assertEqual(
            tail[tail.index("uxon-agent") + 1 :], ["claude", "--dangerously-skip-permissions"]
        )

    def test_resolved_identity_rides_separate_session_env_vars(self) -> None:
        # When resolve_cmd resolves a non-empty identity, the id / cgroup /
        # epoch ride SEPARATE ``-e`` vars; ``UXON_CONTAINER`` keeps the bare name.
        from uxon.infra.container import ContainerIdentity

        c = ContainerConfig(
            enabled=True,
            name_template="proj-{project_slug}",
            exec_template=_EXEC,
            resolve_cmd=("inspect", "{name}"),
        )
        ident = ContainerIdentity(
            id="abc123", cgroup="/sys/fs/cgroup/x.scope", epoch="2026-06-15T00:00:00Z"
        )
        with mock.patch(
            "uxon.infra.container.resolve_container_identity_for_profile", return_value=ident
        ):
            cmd = list(_managed_create_cmd(self._build(_cfg(c))))
        self.assertIn(f"{CONTAINER_NAME_ENV}=proj-myapp", cmd)
        self.assertIn(f"{CONTAINER_ID_ENV}=abc123", cmd)
        self.assertIn(f"{CONTAINER_CGROUP_ENV}=/sys/fs/cgroup/x.scope", cmd)
        self.assertIn(f"{CONTAINER_EPOCH_ENV}=2026-06-15T00:00:00Z", cmd)


class NameSafetyTests(unittest.TestCase):
    """AC-B8 — the resolved name must be charset/length safe (the slug gap)."""

    def test_hostile_leading_dot_slug_rejected(self) -> None:
        # A project dir slugifying to ``.--x`` keeps the leading dot — reject.
        with self.assertRaises(SystemExit):
            validate_container_name(".--x")

    def test_all_dot_name_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            validate_container_name("...")

    def test_leading_underscore_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            validate_container_name("_box")

    def test_leading_dash_rejected(self) -> None:
        # An option-looking token must never reach ``docker exec … <name>``.
        with self.assertRaises(SystemExit):
            validate_container_name("-rm")

    def test_over_128_chars_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            validate_container_name("a" * 129)

    def test_safe_name_accepted(self) -> None:
        self.assertEqual(validate_container_name("proj-myapp_1.2"), "proj-myapp_1.2")

    def test_resolved_hostile_dir_name_rejected_end_to_end(self) -> None:
        # Directory basename "-evil" slugifies (strip('-')) to "evil" — safe;
        # ".evil" keeps the dot → unsafe. Prove the post-expansion check fires.
        c = ContainerConfig(enabled=True, name_template="{project_slug}", exec_template=_EXEC)
        from uxon.infra.tmux import resolve_container

        with self.assertRaises(SystemExit):
            resolve_container(_cfg(c), "/srv/projects/.evil", "dana")


class PathSafetyTests(unittest.TestCase):
    """AC-B8 — path_map / {dir} must be absolute, normalized, ``..``-free."""

    def test_path_map_value_with_dotdot_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            validate_path_map({"/srv": "/work/../../etc"})

    def test_path_map_relative_key_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            validate_path_map({"relative": "/work"})

    def test_path_map_value_literal_not_formatted(self) -> None:
        # A ``{name}`` in a path_map value must be a literal, never expanded —
        # it is a non-absolute string, so it is simply rejected (not run).
        with self.assertRaises(SystemExit):
            validate_path_map({"/srv": "{name}"})


class FormatGuardTests(unittest.TestCase):
    """AC-B8 — a bad placeholder fails with a clear message, not a traceback."""

    def test_unknown_placeholder_in_name_template(self) -> None:
        c = ContainerConfig(enabled=True, name_template="proj-{bogus}", exec_template=_EXEC)
        with self.assertRaises(SystemExit) as ctx:
            resolve_container_name(c, user="dana", project_slug="x", dir_token="/work")
        # ``fail`` stashes the human message on ``.uxon_msg`` (str() is the code).
        self.assertIn("placeholder", getattr(ctx.exception, "uxon_msg", "").lower())


class DecideActionTests(unittest.TestCase):
    """AC-B4 — the pure policy verdict before any side effect."""

    def test_running_always_execs(self) -> None:
        for on_missing in ("off", "start", "create"):
            action, _ = decide_container_action(running=True, exists=True, on_missing=on_missing)
            self.assertEqual(action, "exec")

    def test_stopped_needs_start_capability(self) -> None:
        self.assertEqual(
            decide_container_action(running=False, exists=True, on_missing="off")[0], "fail"
        )
        self.assertEqual(
            decide_container_action(running=False, exists=True, on_missing="start")[0], "start"
        )
        self.assertEqual(
            decide_container_action(running=False, exists=True, on_missing="create")[0], "start"
        )

    def test_absent_needs_create_capability(self) -> None:
        self.assertEqual(
            decide_container_action(running=False, exists=False, on_missing="off")[0], "fail"
        )
        self.assertEqual(
            decide_container_action(running=False, exists=False, on_missing="start")[0], "fail"
        )
        self.assertEqual(
            decide_container_action(running=False, exists=False, on_missing="create")[0], "create"
        )


class PlanMatrixTests(unittest.TestCase):
    """AC-B4 — the orchestrator with the subprocess probe boundary mocked."""

    def _cfg(self, on_missing: str) -> Config:
        return _cfg(
            ContainerConfig(
                enabled=True,
                name_template="proj-{project_slug}",
                exec_template=_EXEC,
                is_running_cmd=("docker", "top", "{name}"),
                exists_cmd=("docker", "container", "inspect", "{name}"),
                start_template=("docker", "start", "{name}"),
                create_template=("docker", "compose", "up", "-d"),
                on_missing=on_missing,  # type: ignore[arg-type]
            )
        )

    def _plan(self, on_missing: str, *, running: bool, exists: bool):
        from uxon.infra import container as container_infra

        # Probe order: is_running_cmd first, then exists_cmd (only if not
        # running). Return the mocked exit-code outcomes in that order.
        rcs = iter([running] + ([exists] if not running else []))

        def fake_probe(cmd, launch_user):
            return next(rcs)

        with mock.patch.object(container_infra, "_probe_exit_ok", side_effect=fake_probe):
            return container_infra.plan_container_launch(
                self._cfg(on_missing), "/srv/projects/myapp", "dana"
            )

    def test_running_plan_is_exec(self) -> None:
        plan = self._plan("create", running=True, exists=True)
        self.assertEqual(plan.action, "exec")
        self.assertEqual(plan.prepare_cmd, ())

    def test_stopped_with_start_renders_start_cmd(self) -> None:
        plan = self._plan("start", running=False, exists=True)
        self.assertEqual(plan.action, "start")
        self.assertEqual(plan.prepare_cmd, ("docker", "start", "proj-myapp"))

    def test_absent_with_create_renders_create_cmd(self) -> None:
        plan = self._plan("create", running=False, exists=False)
        self.assertEqual(plan.action, "create")
        self.assertEqual(plan.prepare_cmd, ("docker", "compose", "up", "-d"))

    def test_stopped_without_capability_fails(self) -> None:
        plan = self._plan("off", running=False, exists=True)
        self.assertEqual(plan.action, "fail")
        from uxon.infra import container as container_infra

        with self.assertRaises(SystemExit):
            container_infra.run_prepare(plan, "/srv/projects/myapp", "dana")

    def test_auto_runs_prepare_then_succeeds(self) -> None:
        # ``run_prepare`` shells out for start/create; mock that boundary too.
        plan = self._plan("start", running=False, exists=True)
        from uxon.infra import container as container_infra

        with mock.patch.object(container_infra, "_run_prepare") as run_prep:
            container_infra.run_prepare(plan, "/srv/projects/myapp", "dana")
        run_prep.assert_called_once()


class AsUserShellOutTests(unittest.TestCase):
    """HIGH — every container shell-out runs as the launch user (rootless).

    The probe and the agent must hit the same per-user daemon; the prepare
    must cd into the HOST dir. Assert the argv carries the as-user sudo prefix
    and the host-dir cd wrapper, with the operator tokens preserved verbatim.
    """

    def _captured_argv(self, fn) -> list[str]:
        from uxon.infra import container as container_infra

        captured: dict[str, list[str]] = {}

        class _CP:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return _CP()

        # Force the cross-user path: pretend uxon runs as someone else so the
        # ``sudo -iu``/``-niu`` prefix is emitted (same-user would be empty).
        with (
            mock.patch.object(container_infra.subprocess, "run", side_effect=fake_run),
            mock.patch("uxon.infra.identity.process_user", return_value="root"),
        ):
            fn()
        return captured["argv"]

    def test_probe_argv_is_prefixed_for_launch_user(self) -> None:
        from uxon.infra import container as container_infra

        argv = self._captured_argv(
            lambda: container_infra._probe_exit_ok(["docker", "top", "box"], "dana")
        )
        # Non-interactive prefix (no TTY) for probes, then the operator argv.
        self.assertEqual(argv[:4], ["sudo", "-niu", "dana", "--"])
        self.assertEqual(argv[-3:], ["docker", "top", "box"])

    def test_prepare_argv_runs_in_host_dir_as_launch_user(self) -> None:
        from uxon.infra import container as container_infra

        argv = self._captured_argv(
            lambda: container_infra._run_prepare(
                ["docker", "compose", "up", "-d"], "/srv/projects/app", "dana"
            )
        )
        # Interactive prefix for the launch-time start/create.
        self.assertEqual(argv[:4], ["sudo", "-iu", "dana", "--"])
        # cd into the HOST dir, then exec the operator argv as separate tokens
        # (argv-list invariant: never re-parsed by the shell).
        self.assertEqual(argv[4], "sh")
        self.assertIn("cd /srv/projects/app", argv[6])
        self.assertEqual(argv[-4:], ["docker", "compose", "up", "-d"])

    def test_probe_permission_error_fails_cleanly(self) -> None:
        from uxon.infra import container as container_infra

        with (
            mock.patch.object(
                container_infra.subprocess, "run", side_effect=PermissionError("denied")
            ),
            mock.patch("uxon.infra.identity.process_user", return_value="dana"),
            self.assertRaises(SystemExit),
        ):
            container_infra._probe_exit_ok(["docker", "top", "box"], "dana")


class IdentityParseTests(unittest.TestCase):
    """Phase 1 identity resolution — parsers and the resolver degrade, never raise."""

    def test_resolve_identity_degrades_on_bad_template(self) -> None:
        # AC-P1.3 / AC-P3.5 degrade-never-block: a misconfigured resolve_cmd
        # template must yield EMPTY_IDENTITY, never abort the launch with the
        # render_template SystemExit (it sits on the launch hot path).
        from uxon.infra import container as container_infra
        from uxon.infra.container import EMPTY_IDENTITY

        cfg = _cfg(
            ContainerConfig(
                enabled=True,
                name_template="proj-{project_slug}",
                exec_template=_EXEC,
                # Single-brace ``{.Id}`` is an invalid format token (the
                # documented form doubles braces) → render_template fail()s.
                resolve_cmd=("docker", "inspect", "--format", "{.Id}", "{name}"),
            )
        )
        with mock.patch("uxon.infra.tmux.resolve_container", return_value=("proj-x", "/work")):
            ident = container_infra.resolve_container_identity(cfg, "/srv/projects/myapp", "dana")
        self.assertIs(ident, EMPTY_IDENTITY)

    def test_parse_resolve_output_first_line_three_tokens(self) -> None:
        from uxon.infra.container import parse_resolve_output

        self.assertEqual(
            parse_resolve_output("\n  abc123 4242 2026-06-15T00:00:00Z \nextra\n"),
            ("abc123", "4242", "2026-06-15T00:00:00Z"),
        )

    def test_parse_resolve_output_degrades_on_bad_shape(self) -> None:
        from uxon.infra.container import parse_resolve_output

        for bad in ("", "   ", "only-two tokens", "abc notapid 123"):
            self.assertIsNone(parse_resolve_output(bad))

    def test_parse_proc_cgroup_v2_prefers_unified_line(self) -> None:
        from uxon.infra.container import parse_proc_cgroup

        v2 = "0::/system.slice/docker-abc.scope\n"
        self.assertEqual(parse_proc_cgroup(v2), "/system.slice/docker-abc.scope")
        # Path containing ``:`` survives the bounded split.
        self.assertEqual(parse_proc_cgroup("0::/a:b/c"), "/a:b/c")

    def test_parse_proc_cgroup_v1_falls_back_to_a_controller_line(self) -> None:
        from uxon.infra.container import parse_proc_cgroup

        v1 = "12:pids:/docker/abc\n11:memory:/docker/abc\n"
        self.assertEqual(parse_proc_cgroup(v1), "/docker/abc")
        self.assertEqual(parse_proc_cgroup("garbage\n\n"), "")


class ContainerGateTests(unittest.TestCase):
    """HIGH (AC-B4) — TUI prompt-vs-auto decision (pure, probe mocked)."""

    def _gate(self, on_missing: str, on_missing_mode: str, *, running: bool, exists: bool):
        from uxon.app import launch as launch_app
        from uxon.infra import container as container_infra

        cfg = _cfg(
            ContainerConfig(
                enabled=True,
                name_template="proj-{project_slug}",
                exec_template=_EXEC,
                is_running_cmd=("docker", "top", "{name}"),
                exists_cmd=("docker", "container", "inspect", "{name}"),
                start_template=("docker", "start", "{name}"),
                create_template=("docker", "compose", "up", "-d"),
                on_missing=on_missing,  # type: ignore[arg-type]
                on_missing_mode=on_missing_mode,  # type: ignore[arg-type]
            )
        )
        rcs = iter([running] + ([exists] if not running else []))
        with mock.patch.object(
            container_infra, "_probe_exit_ok", side_effect=lambda c, u: next(rcs)
        ):
            return launch_app.decide_container_gate(cfg, "/srv/projects/myapp", "dana")

    def test_disabled_returns_none(self) -> None:
        from uxon.app import launch as launch_app

        cfg = _cfg(ContainerConfig(enabled=False))
        self.assertIsNone(launch_app.decide_container_gate(cfg, "/srv/projects/x", "dana"))

    def test_running_gates_through(self) -> None:
        self.assertIsNone(self._gate("create", "prompt", running=True, exists=True))

    def test_stopped_prompt_needs_confirm(self) -> None:
        gate = self._gate("start", "prompt", running=False, exists=True)
        assert gate is not None
        self.assertTrue(gate.needs_prepare)
        self.assertTrue(gate.needs_prompt)
        self.assertFalse(gate.fail_message)
        self.assertIn("stopped", gate.message)

    def test_stopped_auto_skips_prompt(self) -> None:
        gate = self._gate("start", "auto", running=False, exists=True)
        assert gate is not None
        self.assertTrue(gate.needs_prepare)
        self.assertFalse(gate.needs_prompt)

    def test_out_of_policy_carries_fail_message(self) -> None:
        gate = self._gate("off", "prompt", running=False, exists=True)
        assert gate is not None
        self.assertFalse(gate.needs_prepare)
        self.assertTrue(gate.fail_message)


class KillCaveatTests(unittest.TestCase):
    """Security MEDIUM-2 — the container kill caveat string."""

    def test_disabled_returns_none(self) -> None:
        self.assertIsNone(kill_caveat(ContainerConfig(enabled=False)))

    def test_enabled_names_runtime_and_carries_no_internals(self) -> None:
        c = ContainerConfig(enabled=True, exec_template=("podman", "exec", "{name}"))
        caveat = kill_caveat(c)
        assert caveat is not None
        self.assertIn("podman top", caveat)
        # Zero internals: no usernames, host paths, or session names.
        for leaked in ("dana", "/srv", "uxon-", "@claude"):
            self.assertNotIn(leaked, caveat)

    def test_stop_template_set_suppresses_caveat(self) -> None:
        # AC-B5: with teardown configured the agent is reaped, so no caveat.
        c = ContainerConfig(
            enabled=True,
            exec_template=("docker", "exec", "{name}"),
            stop_template=("docker", "exec", "{name}", "sh", "-c", "kill $(cat {pidfile})"),
        )
        self.assertIsNone(kill_caveat(c))


class TeardownPrimitiveTests(unittest.TestCase):
    """AC-B5 pure-domain teardown primitives (pidfile, wrap, render, name)."""

    def test_pidfile_is_deterministic_per_session_and_sanitized(self) -> None:
        # Same session → same path; different sessions → different paths.
        self.assertEqual(container_pidfile("uxon-app@claude"), container_pidfile("uxon-app@claude"))
        self.assertNotEqual(
            container_pidfile("uxon-app@claude-2"), container_pidfile("uxon-app@claude")
        )
        # Worktree/index variants of one container resolve to distinct files.
        self.assertNotEqual(
            container_pidfile("uxon-app@codex"), container_pidfile("uxon-app@claude")
        )
        # Unsafe characters collapse to ``_`` (path safe to embed in sh -c).
        pf = container_pidfile("uxon-a b/c@claude")
        self.assertTrue(pf.startswith("/tmp/uxon-"))
        self.assertNotIn(" ", pf)
        self.assertNotIn("/", pf[len("/tmp/") :])

    def test_wrap_exports_session_and_optionally_records_pid(self) -> None:
        # With a pidfile (teardown opted in): export the per-session marker AND
        # record the in-container PID, then exec the agent.
        wrapped = wrap_agent_for_container(
            ["claude", "--flag"], session="uxon-app@claude", pidfile="/tmp/uxon-s.pid"
        )
        self.assertEqual(wrapped[:2], ["sh", "-c"])
        self.assertIn(f"export {SESSION_ENV}=uxon-app@claude", wrapped[2])
        self.assertIn("echo $$ > /tmp/uxon-s.pid", wrapped[2])
        self.assertIn('exec "$@"', wrapped[2])
        # $0 sentinel then the agent argv, untouched and still a token list.
        self.assertEqual(wrapped[3:], ["uxon-agent", "claude", "--flag"])
        # Without a pidfile (no teardown): export only, no PID record.
        bare = wrap_agent_for_container(["claude"], session="uxon-app@claude", pidfile=None)
        self.assertIn(f"export {SESSION_ENV}=uxon-app@claude", bare[2])
        self.assertNotIn("echo $$", bare[2])
        self.assertIn('exec "$@"', bare[2])
        self.assertEqual(bare[3:], ["uxon-agent", "claude"])

    def test_render_stop_template_fills_per_token(self) -> None:
        out = render_stop_template(
            ("docker", "exec", "{name}", "sh", "-c", "kill $(cat {pidfile})"),
            name="proj-app",
            pidfile="/tmp/uxon-s.pid",
        )
        self.assertEqual(
            out, ["docker", "exec", "proj-app", "sh", "-c", "kill $(cat /tmp/uxon-s.pid)"]
        )

    def test_is_valid_container_name_matches_charset(self) -> None:
        self.assertTrue(is_valid_container_name("proj-app_1.2"))
        # Non-raising rejects (kill path degrades to skip-teardown, never aborts).
        for bad in ("", "-leading", ".dot", "_under", "bad name", "a" * 129):
            self.assertFalse(is_valid_container_name(bad))


class CliKillCaveatTests(unittest.TestCase):
    """Security MEDIUM-2 — the caveat reaches the CLI kill success surfaces."""

    def _container_cfg(self):
        from helpers import make_config

        return make_config(
            container=ContainerConfig(enabled=True, exec_template=("docker", "exec", "{name}"))
        )

    def test_do_kill_self_emits_caveat(self) -> None:
        import io
        from contextlib import redirect_stdout

        from helpers import make_session

        import uxon.app.kill as kill_app

        cfg = self._container_cfg()
        target = make_session("uxon-demo@claude")
        args = ParsedArgs(action="kill", target_id="demo@claude", force=True)
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[target]),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/x.sock"),
            mock.patch("uxon.infra.process.run_cmd", return_value=completed),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("killed:", out)
        self.assertIn("docker top", out)

    def test_do_kill_all_emits_caveat_once(self) -> None:
        import io
        from contextlib import redirect_stdout

        from helpers import make_session

        import uxon.app.kill as kill_app

        cfg = self._container_cfg()
        sessions = [make_session("uxon-a@claude"), make_session("uxon-b@claude")]
        args = ParsedArgs(action="kill-all", force=True)
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=sessions),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/x.sock"),
            mock.patch("uxon.infra.process.run_cmd", return_value=completed),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill_all(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        # Emitted once for the bulk operation (not per-session).
        self.assertEqual(out.count("docker top"), 1)


class ContainerUsageResolverTests(unittest.TestCase):
    """Pure host-side telemetry resolvers (host-free, fed fabricated input)."""

    def test_parse_cgroup_procs_dedupes_and_skips_noise(self) -> None:
        from uxon.domain.container_usage import parse_cgroup_procs

        # One PID per line; blanks + non-numeric skipped; first-seen dedupe.
        self.assertEqual(parse_cgroup_procs("12\n34\n\n12\nfoo\n56\n"), [12, 34, 56])
        self.assertEqual(parse_cgroup_procs(""), [])

    def test_parse_environ_session_extracts_marker(self) -> None:
        from uxon.domain.container_usage import parse_environ_session

        blob = "PATH=/bin\0UXON_SESSION=uxon-proj@claude\0HOME=/root\0"
        self.assertEqual(parse_environ_session(blob), "uxon-proj@claude")
        # Absent marker → "" (a non-uxon process sharing the container).
        self.assertEqual(parse_environ_session("PATH=/bin\0TERM=xterm\0"), "")

    def test_parse_sudo_environ_lines_maps_pid_to_session(self) -> None:
        from uxon.domain.container_usage import parse_sudo_environ_lines

        out = parse_sudo_environ_lines("10 uxon-a@claude\n11 uxon-b@claude\nbad\n12 \n")
        self.assertEqual(out, {10: "uxon-a@claude", 11: "uxon-b@claude", 12: ""})

    def test_sum_usage_clamps_and_skips_missing(self) -> None:
        from uxon.domain.container_usage import sum_usage_for_pids

        # pid → (ppid, rss_kib, cpu_pct). 99 is absent (exited); -5 clamped.
        proc_rows = {1: (0, 100, 5.0), 2: (1, 200, 10.0), 3: (1, -5, -1.0)}
        rss, cpu = sum_usage_for_pids([1, 2, 3, 99], proc_rows)
        self.assertEqual(rss, 300)
        self.assertEqual(cpu, 15.0)

    def test_per_session_split_isolates_runaway(self) -> None:
        """AC-P1.6: a busy PID in session A never sums into session B."""
        from uxon.domain.container_usage import per_session_usage

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
        from uxon.domain.container_usage import group_pids_by_session, per_session_usage

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

    def _profile(self, *, running: bool = False) -> ContainerProfile:
        return ContainerProfile(
            id="box",
            runtime_namespace="per_user",
            name_template="cbox",
            exec_template=("docker", "exec", "{name}"),
            is_running_cmd=("docker", "top", "{name}") if running else (),
            resolve_cmd=("docker", "inspect", "{name}"),
        )

    def _record_session(self, name: str, profile: ContainerProfile, **kw):
        values = dict(
            container="cbox",
            container_cgroup="/c.scope",
            launch_record_verified=True,
            launch_user="u-vz",
            container_profile=profile.id,
            container_profile_fingerprint=profile.fingerprint,
            container_id="cid-1",
            container_epoch="1000",
        )
        values.update(kw)
        return self._session(name, **values)

    def test_non_container_session_uses_pane_walk_and_touches_nothing(self) -> None:
        """AC-P0.4: a no-marker session adds no subprocess / /proc / sudo read."""
        from pathlib import Path

        from uxon.infra import sessions_probe

        s = self._session("uxon-plain@claude", pane_pids=(111,))
        with (
            mock.patch("uxon.infra.sessions_probe.subprocess.run") as run,
            mock.patch.object(Path, "read_text") as read_text,
        ):
            run.return_value = self._ps_completed()
            sessions_probe.enrich_session_usage([s])
        # Exactly one subprocess (the single ps); no /proc read; the walk
        # summed pane 111 + child 222.
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:2], ["ps", "-eo"])
        read_text.assert_not_called()
        self.assertEqual(s.rss_kib, 4096 + 2048)
        self.assertEqual(s.cpu_pct, 4.0)

    def test_single_container_session_sums_cgroup_no_environ(self) -> None:
        """One session per container: cgroup.procs IS its set — no sudo read."""
        from uxon.infra import sessions_probe

        profile = self._profile()
        s = self._record_session(
            "uxon-c@claude", profile, container_cgroup="/system.slice/docker-c.scope"
        )

        def fake_run(cmd, *a, **k):
            return self._ps_completed()  # only the ps table

        with (
            mock.patch("uxon.infra.sessions_probe.subprocess.run", side_effect=fake_run) as run,
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs", return_value=[900, 901]),
            mock.patch(
                "uxon.infra.container.current_container_identity_for_profile",
                return_value=ContainerIdentity(
                    id="cid-1", cgroup="/system.slice/docker-c.scope", epoch="1000"
                ),
            ),
        ):
            sessions_probe.enrich_session_usage(
                [s], container_profiles={"box": profile}, launch_user="u-vz"
            )
        # No sudo environ batch (single session) — only the ps call.
        self.assertEqual(run.call_count, 1)
        self.assertEqual(s.rss_kib, 8192 + 1024)
        self.assertEqual(s.cpu_pct, 110.0)

    def test_shared_container_splits_per_session_when_privileged(self) -> None:
        """AC-P1.6: ≥2 sessions share a cgroup → split by UXON_SESSION."""
        from uxon.infra import sessions_probe

        profile = self._profile()
        a = self._record_session("uxon-a@claude", profile)
        b = self._record_session("uxon-b@claude", profile)
        with (
            mock.patch(
                "uxon.infra.sessions_probe.subprocess.run", return_value=self._ps_completed()
            ),
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs", return_value=[900, 901]),
            mock.patch(
                "uxon.infra.sessions_probe._read_pid_sessions",
                return_value={900: "uxon-a@claude", 901: "uxon-b@claude"},
            ),
            mock.patch(
                "uxon.infra.container.current_container_identity_for_profile",
                return_value=ContainerIdentity(id="cid-1", cgroup="/c.scope", epoch="1000"),
            ),
        ):
            sessions_probe.enrich_session_usage(
                [a, b], container_profiles={"box": profile}, launch_user="u-vz"
            )
        self.assertEqual((a.rss_kib, a.cpu_pct), (8192, 50.0))
        self.assertEqual((b.rss_kib, b.cpu_pct), (1024, 60.0))

    def test_shared_container_degrades_to_shared_total_without_privilege(self) -> None:
        from uxon.infra import sessions_probe

        profile = self._profile()
        a = self._record_session("uxon-a@claude", profile)
        b = self._record_session("uxon-b@claude", profile)
        with (
            mock.patch(
                "uxon.infra.sessions_probe.subprocess.run", return_value=self._ps_completed()
            ),
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs", return_value=[900, 901]),
            mock.patch("uxon.infra.sessions_probe._read_pid_sessions", return_value=None),
            mock.patch(
                "uxon.infra.container.current_container_identity_for_profile",
                return_value=ContainerIdentity(id="cid-1", cgroup="/c.scope", epoch="1000"),
            ),
        ):
            sessions_probe.enrich_session_usage(
                [a, b], container_profiles={"box": profile}, launch_user="u-vz"
            )
        # Both show the summed total (the documented degrade), not zero.
        shared = (8192 + 1024, 110.0)
        self.assertEqual((a.rss_kib, a.cpu_pct), shared)
        self.assertEqual((b.rss_kib, b.cpu_pct), shared)

    def test_empty_cgroup_marks_container_down_when_probe_says_stopped(self) -> None:
        """AC-P1.8: empty cgroup + is_running_cmd non-zero → container_down."""
        from uxon.infra import sessions_probe

        profile = self._profile(running=True)
        s = self._record_session("uxon-c@claude", profile)
        with (
            mock.patch(
                "uxon.infra.sessions_probe.subprocess.run", return_value=self._ps_completed()
            ),
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs", return_value=[]),
            mock.patch(
                "uxon.infra.container.current_container_identity_for_profile",
                return_value=ContainerIdentity(id="cid-1", cgroup="/c.scope", epoch="1000"),
            ),
            mock.patch(
                "uxon.infra.container.probe_container_state_for_profile", return_value=("no", "yes")
            ),
        ):
            sessions_probe.enrich_session_usage(
                [s], container_profiles={"box": profile}, launch_user="u-vz"
            )
        self.assertTrue(s.container_down)
        self.assertEqual(s.cpu_pct, 0.0)
        self.assertEqual(s.rss_kib, 0)

    def test_unresolved_live_identity_marks_container_down_when_probe_says_stopped(
        self,
    ) -> None:
        from uxon.infra import sessions_probe

        profile = self._profile(running=True)
        s = self._record_session("uxon-c@claude", profile)
        with (
            mock.patch(
                "uxon.infra.sessions_probe.subprocess.run", return_value=self._ps_completed()
            ),
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs") as read_cgroup,
            mock.patch(
                "uxon.infra.container.current_container_identity_for_profile",
                return_value=None,
            ),
            mock.patch(
                "uxon.infra.container.probe_container_state_for_profile",
                return_value=("no", "yes"),
            ) as probe,
        ):
            sessions_probe.enrich_session_usage(
                [s], container_profiles={"box": profile}, launch_user="u-vz"
            )
        read_cgroup.assert_not_called()
        probe.assert_called_once()
        self.assertTrue(s.container_down)
        self.assertEqual(s.cpu_pct, 0.0)
        self.assertEqual(s.rss_kib, 0)

    def test_empty_cgroup_running_container_degrades_to_zero_not_down(self) -> None:
        from uxon.infra import sessions_probe

        profile = self._profile(running=True)
        s = self._record_session("uxon-c@claude", profile)
        with (
            mock.patch(
                "uxon.infra.sessions_probe.subprocess.run", return_value=self._ps_completed()
            ),
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs", return_value=[]),
            mock.patch(
                "uxon.infra.container.current_container_identity_for_profile",
                return_value=ContainerIdentity(id="cid-1", cgroup="/c.scope", epoch="1000"),
            ),
            mock.patch(
                "uxon.infra.container.probe_container_state_for_profile",
                return_value=("yes", "yes"),
            ),
        ):
            sessions_probe.enrich_session_usage(
                [s], container_profiles={"box": profile}, launch_user="u-vz"
            )
        # Running but empty cgroup (race / unresolved path) → 0/—, not "down".
        self.assertFalse(s.container_down)
        self.assertEqual(s.cpu_pct, 0.0)

    def test_empty_cgroup_unknown_profile_probe_degrades_to_zero_not_down(self) -> None:
        from uxon.infra import sessions_probe

        profile = self._profile(running=True)
        s = self._record_session("uxon-c@claude", profile)
        with (
            mock.patch(
                "uxon.infra.sessions_probe.subprocess.run", return_value=self._ps_completed()
            ),
            mock.patch("uxon.infra.sessions_probe._read_cgroup_procs", return_value=[]),
            mock.patch(
                "uxon.infra.container.current_container_identity_for_profile",
                return_value=ContainerIdentity(id="cid-1", cgroup="/c.scope", epoch="1000"),
            ),
            mock.patch(
                "uxon.infra.container.probe_container_state_for_profile",
                return_value=("?", "?"),
            ),
        ):
            sessions_probe.enrich_session_usage(
                [s], container_profiles={"box": profile}, launch_user="u-vz"
            )
        self.assertFalse(s.container_down)
        self.assertEqual(s.cpu_pct, 0.0)


class ContainerRenderTests(unittest.TestCase):
    """``cmd`` shows the agent id (AC-P1.4); the down indicator (AC-P1.8)."""

    def _session(self, **kw):
        from helpers import make_session

        s = make_session("uxon-proj@claude")
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def test_tui_cmd_shows_agent_for_container_session(self) -> None:
        from uxon.domain.session import to_tui_session

        s = self._session(container="cbox", active_cmd="docker", agent="claude")
        tui = to_tui_session(s, "uxon-")
        self.assertEqual(tui.cmd, "claude")  # not "docker"

    def test_tui_cmd_unchanged_for_non_container_session(self) -> None:
        from uxon.domain.session import to_tui_session

        s = self._session(container="", active_cmd="vim", agent="claude")
        self.assertEqual(to_tui_session(s, "uxon-").cmd, "vim")

    def test_list_shows_down_and_agent_for_container_down_session(self) -> None:
        import io
        from contextlib import redirect_stdout

        from helpers import make_config

        from uxon.app.listing import print_list

        s = self._session(
            container="cbox", container_down=True, active_cmd="docker", agent="claude"
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_list(make_config(), [s], ["u-vz"])
        out = buf.getvalue()
        self.assertIn("down", out)  # distinct down indicator, not a silent "-"
        self.assertIn("claude", out)  # cmd shows the agent id

    def test_dashboard_cpu_cell_renders_down_marker(self) -> None:
        from uxon.tui.dashboard.columns import CONTAINER_DOWN_CELL, format_cpu, format_ram
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
            container_down=True,
        )
        self.assertEqual(format_cpu(row).plain, CONTAINER_DOWN_CELL)
        self.assertEqual(format_ram(row), CONTAINER_DOWN_CELL)


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
    def _container_launch() -> LaunchConfig:
        agents = default_agent_catalog()
        profiles = builtin_launch_profiles(agents)
        profiles["claude_box"] = LaunchProfile(
            id="claude_box",
            agent="claude",
            container_profile="box",
        )
        return LaunchConfig(
            enabled_profiles=("claude_box",),
            default_profile="claude_box",
            profiles=profiles,
        )

    def test_host_absent_binary_does_not_fail_under_container(self) -> None:
        # AC-P2.1: explicit container profile + host-absent binary →
        # resolves anyway (no host-presence gate).
        c = ContainerConfig(enabled=True, name_template="p-{project_slug}", exec_template=_EXEC)
        cfg = _cfg(c, launch=self._container_launch())
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
        c = ContainerConfig(enabled=False)
        cfg = _cfg(c)
        with self.assertRaises(SystemExit):
            launch_profile_app.resolve_launch_profile(
                cfg,
                "dana",
                "claude",
                "/srv/projects/myapp",
                "normal",
                report=self._report(claude_path=None),
            )

    def test_auto_mode_ignores_operator_container_profile(self) -> None:
        # Auto-mode considers shipped OS-user-only profiles only; an operator
        # container profile is not auto-enabled.
        c = ContainerConfig(enabled=True, name_template="p-{project_slug}", exec_template=_EXEC)
        agents = default_agent_catalog()
        profiles = builtin_launch_profiles(agents)
        profiles["claude_box"] = LaunchProfile(
            id="claude_box",
            agent="claude",
            container_profile="box",
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

    def test_tui_predicates_suppressed_under_container(self) -> None:
        # AC-P2.2: both launch-gate predicates return False under container mode
        # even when every enabled agent resolved missing.
        from uxon.tui.state import compute_all_missing, should_show_agents_unavailable

        missing = {"claude": mock.MagicMock(status="missing")}
        self.assertTrue(compute_all_missing(enabled_agents=("claude",), availability=missing))
        self.assertFalse(
            compute_all_missing(
                enabled_agents=("claude",), availability=missing, container_enabled=True
            )
        )
        self.assertFalse(
            should_show_agents_unavailable(
                enabled_agents=("claude",),
                availability=missing,
                already_shown=False,
                container_enabled=True,
            )
        )


class ContainerProfileRuntimeTests(unittest.TestCase):
    """P3 — launch runtime decisions come from the resolved container profile."""

    @staticmethod
    def _profile(
        cid: str,
        *,
        namespace: str = "per_user",
        name_template: str = "box-{project_slug}",
        path_map=(),
    ) -> ContainerProfile:
        return ContainerProfile(
            id=cid,
            runtime_namespace=namespace,  # type: ignore[arg-type]
            name_template=name_template,
            exec_template=("docker", "exec", "-w", "{dir}", "{name}"),
            is_running_cmd=("docker", "top", "{name}"),
            exists_cmd=("docker", "inspect", "{name}"),
            start_template=("docker", "start", "{name}"),
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
        container_profiles: dict[str, ContainerProfile],
        *,
        enabled: tuple[str, ...],
        default: str,
    ) -> Config:
        agents = default_agent_catalog()
        profiles = builtin_launch_profiles(agents)
        profiles.update(launch_profiles)
        return _cfg(
            ContainerConfig(),
            launch=LaunchConfig(
                enabled_profiles=enabled,
                default_profile=default,
                profiles=profiles,
            ),
            container_profiles=container_profiles,
        )

    def test_prepare_and_agent_probe_use_pinned_launch_user_and_profile(self) -> None:
        profile = LaunchProfile(
            id="claude_box", agent="claude", launch_user="alice", container_profile="box"
        )
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
        assert resolved.container_context is not None
        self.assertEqual(resolved.container_context.name, "box-myapp")
        self.assertEqual(resolved.container_context.dir_token, "/work/myapp")

        from uxon.app import launch as launch_app
        from uxon.infra import container as container_infra

        probes = iter([False, True])
        with (
            mock.patch.object(
                container_infra, "_probe_exit_ok", side_effect=lambda c, u: next(probes)
            ),
            mock.patch.object(container_infra, "_run_prepare") as run_prepare,
            mock.patch.object(container_infra, "probe_agent_in_container") as probe_agent,
        ):
            launch_app.ensure_container_ready(cfg, "/srv/projects/myapp", resolved)
        run_prepare.assert_called_once_with(
            ["docker", "start", "box-myapp"], "/srv/projects/myapp", "alice"
        )
        probe_agent.assert_called_once_with(cfg, "/srv/projects/myapp", resolved)

    def test_tui_gate_probes_agent_for_running_container_profile(self) -> None:
        profile = LaunchProfile(
            id="claude_box", agent="claude", launch_user="alice", container_profile="box"
        )
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
        from uxon.infra import container as container_infra

        with (
            mock.patch.object(container_infra, "_probe_exit_ok", return_value=True),
            mock.patch.object(container_infra, "probe_agent_in_container") as probe_agent,
        ):
            gate = launch_app.decide_container_gate(cfg, "/srv/projects/myapp", resolved)
        self.assertIsNone(gate)
        probe_agent.assert_called_once_with(cfg, "/srv/projects/myapp", resolved)

    def test_agent_probe_runs_inside_container_not_on_host(self) -> None:
        profile = LaunchProfile(
            id="claude_box", agent="claude", launch_user="alice", container_profile="box"
        )
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
        from uxon.infra import container as container_infra

        cp = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch("uxon.infra.identity.process_user", return_value="root"),
            mock.patch.object(container_infra, "run_query", return_value=cp) as run_query,
        ):
            container_infra.probe_agent_in_container(cfg, "/srv/projects/myapp", resolved)
        argv = run_query.call_args.args[0]
        self.assertEqual(argv[:4], ["sudo", "-niu", "alice", "--"])
        self.assertIn("docker", argv)
        self.assertIn("box-myapp", argv)
        self.assertEqual(argv[-1], "claude")

    def test_global_namespace_rejects_distinct_profile_name_collision(self) -> None:
        cfg = self._cfg_profiles(
            {
                "one": LaunchProfile(
                    id="one", agent="claude", launch_user="alice", container_profile="c1"
                ),
                "two": LaunchProfile(
                    id="two", agent="claude", launch_user="bob", container_profile="c2"
                ),
            },
            {
                "c1": self._profile("c1", namespace="global", name_template="shared"),
                "c2": self._profile("c2", namespace="global", name_template="shared"),
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
                "one": LaunchProfile(
                    id="one", agent="claude", launch_user="alice", container_profile="c1"
                ),
                "two": LaunchProfile(
                    id="two", agent="claude", launch_user="alice", container_profile="c2"
                ),
            },
            {
                "c1": self._profile("c1", name_template="shared"),
                "c2": self._profile("c2", name_template="shared"),
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

    def test_same_container_profile_id_is_intentional_sharing(self) -> None:
        cfg = self._cfg_profiles(
            {
                "one": LaunchProfile(
                    id="one", agent="claude", launch_user="alice", container_profile="shared"
                ),
                "two": LaunchProfile(
                    id="two", agent="claude", launch_user="alice", container_profile="shared"
                ),
            },
            {"shared": self._profile("shared", name_template="shared")},
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
        assert resolved.container_context is not None
        self.assertEqual(resolved.container_context.profile_id, "shared")

    def test_placeholder_validation_names_field_and_placeholder(self) -> None:
        bad = self._profile("box")
        bad = ContainerProfile(
            **{
                **bad.__dict__,
                "exec_template": ("docker", "exec", "{bogus}", "{name}"),
            }
        )
        with self.assertRaises(SystemExit) as ctx:
            validate_container_profile(bad)
        msg = getattr(ctx.exception, "uxon_msg", "")
        self.assertIn("exec_template", msg)
        self.assertIn("bogus", msg)

    def test_container_profile_fingerprint_includes_profile_id(self) -> None:
        one = self._profile("one", name_template="{container_profile}-{project_slug}")
        two = self._profile("two", name_template="{container_profile}-{project_slug}")

        self.assertNotEqual(one.fingerprint, two.fingerprint)


class ContainerTeardownAuditTests(unittest.TestCase):
    """AC-P3.2 / AC-P3.5 — teardown audit emit + PID-recycle stale guard."""

    def _cfg_with_stop(self, resolve_cmd=()):
        c = ContainerConfig(
            enabled=True,
            name_template="proj-{project_slug}",
            exec_template=_EXEC,
            stop_template=("docker", "exec", "{name}", "sh", "-c", "kill $(cat {pidfile})"),
            resolve_cmd=resolve_cmd,
        )
        return _cfg(c)

    def _record_session(self, cfg: Config):
        from helpers import make_session

        profile = cfg.container_profiles["box"]
        s = make_session("uxon-x@claude", user="dana")
        s.profile = "claude"
        s.agent = "claude"
        s.launch_record_verified = True
        s.launch_user = "dana"
        s.container = "proj-myapp"
        s.container_profile = profile.id
        s.container_profile_fingerprint = profile.fingerprint
        s.container_id = "cid-1"
        s.container_epoch = "1000"
        return s

    def test_prepare_captures_launch_epoch(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(resolve_cmd=("inspect", "{name}"))
        teardown = kill_app.prepare_container_teardown(cfg, self._record_session(cfg))
        assert teardown is not None
        self.assertEqual(teardown.name, "proj-myapp")
        self.assertEqual(teardown.container_profile, "box")
        self.assertEqual(teardown.container_id, "cid-1")
        self.assertEqual(teardown.launch_epoch, "1000")

    def test_missing_record_skips_prepare_with_warning(self) -> None:
        from helpers import make_session

        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(resolve_cmd=("inspect", "{name}"))
        s = make_session("uxon-x@claude", user="dana")
        s.container_marker = "proj-myapp"
        with mock.patch("uxon.app.kill.eprint") as eprint:
            teardown = kill_app.prepare_container_teardown(cfg, s)
        self.assertIsNone(teardown)
        self.assertIn("no verified launch record", eprint.call_args.args[0])

    def test_missing_container_profile_skips_prepare(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(resolve_cmd=("inspect", "{name}"))
        s = self._record_session(cfg)
        s.container_profile = ""
        with mock.patch("uxon.app.kill.eprint") as eprint:
            teardown = kill_app.prepare_container_teardown(cfg, s)
        self.assertIsNone(teardown)
        self.assertIn("missing container profile", eprint.call_args.args[0])

    def test_fingerprint_mismatch_skips_prepare(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(resolve_cmd=("inspect", "{name}"))
        s = self._record_session(cfg)
        s.container_profile_fingerprint = "old"
        with mock.patch("uxon.app.kill.eprint") as eprint:
            teardown = kill_app.prepare_container_teardown(cfg, s)
        self.assertIsNone(teardown)
        self.assertIn("container profile changed", eprint.call_args.args[0])

    def test_missing_saved_identity_skips_prepare(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(resolve_cmd=("inspect", "{name}"))
        s = self._record_session(cfg)
        s.container_id = ""
        with mock.patch("uxon.app.kill.eprint") as eprint:
            teardown = kill_app.prepare_container_teardown(cfg, s)
        self.assertIsNone(teardown)
        self.assertIn("missing container identity", eprint.call_args.args[0])

    def test_stale_teardown_emits_stale_and_skips_kill(self) -> None:
        # AC-P3.2/P3.5: live epoch != stashed epoch → emit outcome=stale and do
        # NOT run the stop command (the recorded PID would be a recycled one).
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(resolve_cmd=("inspect", "{name}"))
        profile = cfg.container_profiles["box"]
        teardown = kill_app.ContainerTeardown(
            stop_cmd=["docker", "exec", "c", "true"],
            container_profile="box",
            name="proj-myapp",
            container_id="cid-1",
            profile_fingerprint=profile.fingerprint,
            launch_epoch="1000",
        )
        with (
            mock.patch(
                "uxon.infra.container.current_container_identity_for_profile",
                return_value=ContainerIdentity(id="cid-2", epoch="2000"),
            ),
            mock.patch("uxon.infra.container.run_teardown") as run_td,
            mock.patch("uxon.infra.audit.audit") as audit,
        ):
            kill_app.run_container_teardown(cfg, teardown, "dana", "uxon-x@claude")
        run_td.assert_not_called()
        audit.assert_called_once()
        self.assertEqual(audit.call_args.args[0], "container.teardown")
        self.assertEqual(audit.call_args.kwargs["outcome"], "stale")
        self.assertEqual(audit.call_args.kwargs["container"], "proj-myapp")
        self.assertEqual(audit.call_args.kwargs["container_profile"], "box")

    def test_matching_epoch_teardown_runs_and_emits_ok(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(resolve_cmd=("inspect", "{name}"))
        profile = cfg.container_profiles["box"]
        teardown = kill_app.ContainerTeardown(
            stop_cmd=["docker", "exec", "c", "true"],
            container_profile="box",
            name="proj-myapp",
            container_id="cid-1",
            profile_fingerprint=profile.fingerprint,
            launch_epoch="1000",
        )
        with (
            mock.patch(
                "uxon.infra.container.current_container_identity_for_profile",
                return_value=ContainerIdentity(id="cid-1", epoch="1000"),
            ),
            mock.patch("uxon.infra.container.run_teardown", return_value=(True, "")) as run_td,
            mock.patch("uxon.infra.audit.audit") as audit,
        ):
            kill_app.run_container_teardown(cfg, teardown, "dana", "uxon-x@claude")
        run_td.assert_called_once()
        self.assertEqual(audit.call_args.kwargs["outcome"], "ok")

    def test_missing_current_profile_skips_teardown(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(resolve_cmd=("inspect", "{name}"))
        profile = cfg.container_profiles["box"]
        cfg.container_profiles.clear()
        teardown = kill_app.ContainerTeardown(
            stop_cmd=["docker", "exec", "c", "true"],
            container_profile="box",
            name="proj-myapp",
            container_id="cid-1",
            profile_fingerprint=profile.fingerprint,
            launch_epoch="1000",
        )
        with (
            mock.patch("uxon.infra.container.run_teardown") as run_td,
            mock.patch("uxon.infra.audit.audit") as audit,
        ):
            kill_app.run_container_teardown(cfg, teardown, "dana", "uxon-x@claude")
        run_td.assert_not_called()
        self.assertEqual(audit.call_args.kwargs["outcome"], "missing_profile")

    def test_unresolved_live_identity_skips_kill(self) -> None:
        from uxon.app import kill as kill_app

        cfg = self._cfg_with_stop(resolve_cmd=("inspect", "{name}"))
        profile = cfg.container_profiles["box"]
        teardown = kill_app.ContainerTeardown(
            stop_cmd=["docker", "exec", "c", "true"],
            container_profile="box",
            name="proj-myapp",
            container_id="cid-1",
            profile_fingerprint=profile.fingerprint,
            launch_epoch="1000",
        )
        with (
            mock.patch(
                "uxon.infra.container.current_container_identity_for_profile", return_value=None
            ),
            mock.patch("uxon.infra.container.run_teardown", return_value=(True, "")) as run_td,
            mock.patch("uxon.infra.audit.audit") as audit,
        ):
            kill_app.run_container_teardown(cfg, teardown, "dana", "uxon-x@claude")
        run_td.assert_not_called()
        self.assertEqual(audit.call_args.kwargs["outcome"], "identity_unresolved")


class WorktreePathMapGateTests(unittest.TestCase):
    """AC-P4.1 — unmapped worktree path fails fast; empty path_map carve-out."""

    def test_path_map_under_prefix_predicate(self) -> None:
        from uxon.domain.container import path_map_under_prefix

        pm = validate_path_map({"/srv/projects": "/work"})
        self.assertTrue(path_map_under_prefix("/srv/projects/myapp", pm))
        self.assertTrue(path_map_under_prefix("/srv/projects", pm))
        self.assertFalse(path_map_under_prefix("/srv/other", pm))
        # Empty map covers nothing — the caller guards on non-empty separately.
        self.assertFalse(path_map_under_prefix("/srv/projects/myapp", ()))

    def test_unmapped_worktree_path_fails(self) -> None:
        from uxon.app import launch as launch_app

        c = ContainerConfig(
            enabled=True,
            name_template="p-{project_slug}",
            exec_template=_EXEC,
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

        c = ContainerConfig(enabled=True, name_template="p-{project_slug}", exec_template=_EXEC)
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
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
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


class DoctorContainerSectionTests(unittest.TestCase):
    """AC-P5 — doctor container section: probes, expected-absence note, warnings."""

    def test_section_warns_on_unset_stop_template(self) -> None:
        from uxon.app.doctor import _doctor_container_section

        c = ContainerConfig(
            enabled=True,
            name_template="proj-{project_slug}",
            exec_template=_EXEC,
            is_running_cmd=("docker", "inspect", "{name}"),
            exists_cmd=("docker", "inspect", "{name}"),
        )
        cfg = _cfg(c)
        with mock.patch("uxon.infra.container.probe_container_state", return_value=("yes", "yes")):
            section = _doctor_container_section(cfg, "/srv/projects/myapp", "dana")
        self.assertTrue(section["enabled"])
        self.assertEqual(section["resolved_name"], "proj-myapp")
        self.assertEqual(section["is_running"], "yes")
        self.assertTrue(section["host_agent_absence_expected"])
        self.assertTrue(any("stop_template" in w for w in section["warnings"]))

    def test_section_warns_on_definition_under_mount(self) -> None:
        # AC-P5.4: create_template path token under a path_map host prefix.
        from uxon.app.doctor import _doctor_container_section

        c = ContainerConfig(
            enabled=True,
            name_template="proj-{project_slug}",
            exec_template=_EXEC,
            stop_template=("docker", "exec", "{name}", "true"),
            create_template=("docker", "compose", "-f", "/srv/projects/myapp/compose.yml", "up"),
            path_map=validate_path_map({"/srv/projects/myapp": "/work"}),
        )
        cfg = _cfg(c)
        with mock.patch("uxon.infra.container.probe_container_state", return_value=("no", "no")):
            section = _doctor_container_section(cfg, "/srv/projects/myapp", "dana")
        self.assertTrue(
            any("agent-writable bind mount" in w for w in section["warnings"]),
            section["warnings"],
        )

    def test_section_clean_when_hardened(self) -> None:
        from uxon.app.doctor import _doctor_container_section

        c = ContainerConfig(
            enabled=True,
            name_template="proj-{project_slug}",
            exec_template=_EXEC,
            stop_template=("docker", "exec", "{name}", "true"),
            create_template=("docker", "compose", "-f", "/operator/compose.yml", "up"),
            path_map=validate_path_map({"/srv/projects/myapp": "/work"}),
        )
        cfg = _cfg(c)
        with mock.patch("uxon.infra.container.probe_container_state", return_value=("yes", "yes")):
            section = _doctor_container_section(cfg, "/srv/projects/myapp", "dana")
        self.assertEqual(section["warnings"], [])


if __name__ == "__main__":
    unittest.main()
