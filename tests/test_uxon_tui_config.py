"""Unit tests for :class:`uxon.tui.config.TuiConfig`.

Stage 8 commit 1: introduces :class:`TuiConfig` as the immutable side
of the TUI's state. Existing :class:`TuiContext` keeps its current
shape; the config snapshot is duplicated state populated from the
ctx at App-construction time.

The contract pinned here:

* Every field listed in the target shape (plan §"Target") round-trips
  from a representative ``TuiContext`` through :meth:`from_context`.
* The dataclass is frozen — attribute assignment after construction
  raises ``dataclasses.FrozenInstanceError``.
* Identity of ``TuiConfig.refresh_sources`` is stable when the same
  list is fed in twice — i.e. tuple-isation is value-deterministic
  but not memoised; the constructor produces equal-but-not-identical
  tuples, which is the behaviour callers expect.
* The App constructs ``TuiConfig`` once and keeps the same instance
  across rebuild ticks (``ctx`` is replaced; ``app.cfg`` is not).
  The Pilot-level guarantee lands in commit 7 once the rebuild path
  produces ``MainData``; commit 1 only pins the static factory.
"""

from __future__ import annotations

import dataclasses
import unittest

from uxon.tui.config import TuiConfig
from uxon.tui.context import LaunchRequest, TuiContext
from uxon.tui.refresh import SourceSpec


def _mk_ctx(**overrides) -> TuiContext:
    base = dict(
        sessions=[],
        total_cpu="0",
        total_ram="0",
        version="0.0.0-test",
        cwd="/srv/work",
        cwd_short="work",
        new_project_root="/srv/work",
        existing_projects=[],
        cwd_writable=True,
        current_user="devagent",
        launch_user="devagent",
        enabled_agents=("claude", "codex"),
        default_agent="claude",
        tui_refresh_interval_seconds=2.5,
        tui_ssh_refresh_interval_seconds=11.0,
        ssh_multiplex="off",
        fetch_concurrency=8,
        git_create_enabled=True,
        default_git_remote_profile="github",
        git_remote_profile_options=[("github", "github.com via gh")],
        on_attach=lambda u, n: LaunchRequest(cmd=("/bin/true",), label="attach"),
        on_kill=lambda u, n: None,
        on_kill_all=lambda: None,
        on_kill_all_global=lambda: None,
        on_remote_kill=lambda h, u, n: None,
        on_remote_attach=lambda h, u, n: LaunchRequest(cmd=("/bin/true",), label="remote-attach"),
        on_refresh=lambda: _mk_ctx(),
        on_probe_link_health=lambda: None,
        on_probe_cwd_writable=lambda: True,
        on_launch_cwd=lambda a, m: LaunchRequest(cmd=("/bin/true",), label="cwd"),
        on_launch_new=lambda n, a, m, g: LaunchRequest(cmd=("/bin/true",), label="new"),
        on_launch_existing=lambda n, a, m: LaunchRequest(cmd=("/bin/true",), label="exist"),
        get_settings_entries=lambda: [],
        on_setting_save=lambda k, v: None,
        on_setting_remove=lambda k: None,
        on_setting_save_mapping=lambda k, m: None,
        get_git_remote_profile_rows=lambda: [],
        on_enable_detected_agent=lambda aid: None,
        on_dismiss_detected_agent=lambda aid: None,
        get_dismissed_detected_agents=lambda: [],
    )
    base.update(overrides)
    return TuiContext(**base)


class FromContextRoundTripTests(unittest.TestCase):
    def test_every_field_round_trips(self) -> None:
        spec = SourceSpec(
            name="main_ctx_rebuild",
            fetch=lambda: None,  # type: ignore[arg-type]
            cadence_seconds_attr="tui_refresh_interval_seconds",
        )
        ctx = _mk_ctx()
        ctx.refresh_sources = [spec]
        cfg = TuiConfig.from_context(ctx)

        self.assertEqual(cfg.current_user, "devagent")
        self.assertEqual(cfg.launch_user, "devagent")
        self.assertEqual(cfg.enabled_agents, ("claude", "codex"))
        self.assertEqual(cfg.default_agent, "claude")
        self.assertEqual(cfg.tui_refresh_interval_seconds, 2.5)
        self.assertEqual(cfg.tui_ssh_refresh_interval_seconds, 11.0)
        self.assertEqual(cfg.ssh_multiplex, "off")
        self.assertEqual(cfg.fetch_concurrency, 8)
        self.assertEqual(cfg.remote_hosts, ())
        self.assertEqual(cfg.refresh_sources, (spec,))
        self.assertTrue(cfg.git_create_enabled)
        self.assertEqual(cfg.default_git_remote_profile, "github")
        self.assertEqual(cfg.git_remote_profile_options, (("github", "github.com via gh"),))

        # Callable identity is preserved (callbacks pass through by reference).
        self.assertIs(cfg.on_attach, ctx.on_attach)
        self.assertIs(cfg.on_kill, ctx.on_kill)
        self.assertIs(cfg.on_kill_all, ctx.on_kill_all)
        self.assertIs(cfg.on_kill_all_global, ctx.on_kill_all_global)
        self.assertIs(cfg.on_remote_kill, ctx.on_remote_kill)
        self.assertIs(cfg.on_remote_attach, ctx.on_remote_attach)
        self.assertIs(cfg.on_refresh, ctx.on_refresh)
        self.assertIs(cfg.on_probe_link_health, ctx.on_probe_link_health)
        self.assertIs(cfg.on_probe_cwd_writable, ctx.on_probe_cwd_writable)
        self.assertIs(cfg.on_launch_cwd, ctx.on_launch_cwd)
        self.assertIs(cfg.on_launch_new, ctx.on_launch_new)
        self.assertIs(cfg.on_launch_existing, ctx.on_launch_existing)
        self.assertIs(cfg.get_settings_entries, ctx.get_settings_entries)
        self.assertIs(cfg.on_setting_save, ctx.on_setting_save)
        self.assertIs(cfg.on_setting_remove, ctx.on_setting_remove)
        self.assertIs(cfg.on_setting_save_mapping, ctx.on_setting_save_mapping)
        self.assertIs(cfg.get_git_remote_profile_rows, ctx.get_git_remote_profile_rows)
        self.assertIs(cfg.on_enable_detected_agent, ctx.on_enable_detected_agent)
        self.assertIs(cfg.on_dismiss_detected_agent, ctx.on_dismiss_detected_agent)
        self.assertIs(cfg.get_dismissed_detected_agents, ctx.get_dismissed_detected_agents)


class OnRemoteAttachPropagatedTests(unittest.TestCase):
    def test_on_remote_attach_propagated(self) -> None:
        attach_calls: list[tuple[str, str, str]] = []

        def fake_attach(host: str, user: str, name: str) -> LaunchRequest:
            attach_calls.append((host, user, name))
            return LaunchRequest(cmd=("true",), label="t")

        ctx = _mk_ctx(on_remote_attach=fake_attach)
        cfg = TuiConfig.from_context(ctx)
        self.assertIs(cfg.on_remote_attach, ctx.on_remote_attach)
        cfg.on_remote_attach("h", "u", "n")
        self.assertEqual(attach_calls, [("h", "u", "n")])


class FrozenSemanticsTests(unittest.TestCase):
    def test_assignment_after_construction_raises(self) -> None:
        cfg = TuiConfig.from_context(_mk_ctx())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.tui_refresh_interval_seconds = 99.0  # type: ignore[misc]

    def test_remote_hosts_is_a_tuple(self) -> None:
        # Frozen-ness implies sequence types are immutable too.
        cfg = TuiConfig.from_context(_mk_ctx())
        self.assertIsInstance(cfg.remote_hosts, tuple)
        self.assertIsInstance(cfg.refresh_sources, tuple)
        self.assertIsInstance(cfg.git_remote_profile_options, tuple)


class IdentityStabilityTests(unittest.TestCase):
    def test_app_holds_one_cfg_across_rebuild_ticks(self) -> None:
        """The App's ``cfg`` is constructed at ``__init__`` and kept
        across ctx rebuilds. Pinning this here keeps later commits
        from accidentally reconstructing ``cfg`` per tick (which
        would break selectors that key on ``id(cfg)`` and would
        defeat the whole point of moving to an immutable config).
        """
        try:
            import textual  # noqa: F401
        except ImportError:
            self.skipTest("textual not available")
        from uxon.tui.app import UxonApp

        # Synthesise a skeleton ctx via the same helper screens use; we
        # don't run the App, just construct it to inspect ``self.cfg``.
        ctx = _mk_ctx(loading=True)
        app = UxonApp(ctx, probe_agents=False)
        first_cfg = app.cfg
        # Simulate a rebuild landing — App.ctx is replaced but cfg is not.
        new_ctx = _mk_ctx(loading=False)
        app.ctx = new_ctx
        self.assertIs(app.cfg, first_cfg)


if __name__ == "__main__":
    unittest.main()
