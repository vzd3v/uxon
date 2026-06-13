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

from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.container import (
    ContainerConfig,
    apply_path_map,
    decide_container_action,
    kill_caveat,
    resolve_container_name,
    validate_container_name,
    validate_path_map,
)


def _cfg(container: ContainerConfig, **overrides) -> Config:
    """Minimal Config carrying a ContainerConfig (other fields are inert here)."""
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
        container=container,
    )
    base.update(overrides)
    return Config(**base)


_EXEC = ("docker", "exec", "-it", "-w", "{dir}", "{name}")


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
        args = ParsedArgs(action="run", agent="claude", permission_mode="yolo")
        # ``tmux_nesting_mode`` reads $TMUX; force the classic (execvp) path so
        # the request is deterministic regardless of the test runner's tmux.
        with mock.patch("uxon.infra.tmux.tmux_nesting_mode", return_value="execvp"):
            return tmux._build_tmux_launch_request(
                target_dir, "uxon-myapp@claude", args, cfg, None, launch_user
            )

    def test_disabled_is_byte_for_byte_identical(self) -> None:
        disabled = self._build(_cfg(ContainerConfig()))
        # A config with [container] present but enabled=false must match the
        # no-container build exactly (AC-B6 parity).
        also_disabled = self._build(_cfg(ContainerConfig(enabled=False, name_template="x")))
        self.assertEqual(disabled.cmd, also_disabled.cmd)
        # And the agent binary leads the final_cmd (no exec prefix).
        self.assertIn("claude", disabled.cmd)
        idx = disabled.cmd.index("claude")
        self.assertNotIn("docker", disabled.cmd[:idx])

    def test_enabled_prepends_exec_template(self) -> None:
        c = ContainerConfig(enabled=True, name_template="proj-{project_slug}", exec_template=_EXEC)
        req = self._build(_cfg(c))
        # exec_template(resolved) + [binary] + ... + mode_flags, in order.
        cmd = list(req.cmd)
        # The agent command tail must be: docker exec -it -w /srv/projects/myapp
        # proj-myapp claude --dangerously-skip-permissions
        agent_tail = cmd[cmd.index("docker") :]
        self.assertEqual(
            agent_tail,
            [
                "docker",
                "exec",
                "-it",
                "-w",
                "/srv/projects/myapp",
                "proj-myapp",
                "claude",
                "--dangerously-skip-permissions",
            ],
        )

    def test_enabled_with_path_map_uses_container_dir(self) -> None:
        c = ContainerConfig(
            enabled=True,
            name_template="proj-{project_slug}",
            exec_template=_EXEC,
            path_map=validate_path_map({"/srv/projects/myapp": "/work"}),
        )
        req = self._build(_cfg(c))
        cmd = list(req.cmd)
        # -w token is the container-side path, not the host path.
        self.assertIn("/work", cmd)
        self.assertNotIn("/srv/projects/myapp", cmd[cmd.index("docker") :])


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


if __name__ == "__main__":
    unittest.main()
