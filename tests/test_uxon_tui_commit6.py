"""Stage 8 commit 6 — link_health + cwd_writable migration to slots,
plus the cwd-change invalidation contract.

Pinned contracts:

* link_health probe results land on ``state.link_health`` via
  ``slot_state.apply``. The shim ``ctx.link_health_status`` reads
  ``state.link_health.value``.
* cwd_writable probe results land on ``state.cwd_writable`` via the
  same path; the shim returns the slot value when probed, else
  legacy.
* The on-loop handler drops a cwd-write probe whose ``cwd_at_start``
  no longer matches the live ``ctx.cwd`` (in-flight reattribution
  guard).
* A cwd transition during ``apply_loaded_ctx`` resets
  ``state.cwd_writable`` to its zero state — the row reverts to
  "checking…" until the next probe lands.
* ``state.cwd_writable.last_attempt_at is None`` is now the
  loading-vs-loaded sentinel; the synchronous fallback at
  ``_launch_cwd`` checks this rather than the value-``None`` path.
"""

from __future__ import annotations

import unittest


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _mk_ctx(**overrides):
    from uxon.tui.context import LaunchRequest, TuiContext

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
        on_launch_cwd=lambda agent_id, mode_id: LaunchRequest(cmd=("/bin/true",), label="cwd"),
        on_launch_new=lambda n, agent_id, mode_id, g: LaunchRequest(
            cmd=("/bin/true",), label="new"
        ),
        on_launch_existing=lambda n, agent_id, mode_id: LaunchRequest(
            cmd=("/bin/true",), label="exist"
        ),
    )
    base.update(overrides)
    return TuiContext(**base)


@unittest.skipUnless(_textual_available(), "textual not installed")
class LinkHealthSlotApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_health_lands_on_state_slot(self) -> None:
        from uxon.tui.app import UxonApp, _LinkHealthUpdated
        from uxon.tui.context import LinkHealthStatus

        ctx = _mk_ctx()
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(80, 24)) as pilot:
            self.assertIsNone(app.state.link_health.value)
            status = LinkHealthStatus(state="ok", summary="all green")
            app.post_message(_LinkHealthUpdated(status))
            await pilot.pause()
            self.assertIs(app.state.link_health.value, status)
            self.assertIsNotNone(app.state.link_health.last_attempt_at)
            # Shim reads through.
            self.assertIs(app.ctx.link_health_status, status)


@unittest.skipUnless(_textual_available(), "textual not installed")
class CwdWritableSlotApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_cwd_writable_lands_on_state_slot(self) -> None:
        from uxon.tui.app import UxonApp, _CwdWritableUpdated

        ctx = _mk_ctx()
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(80, 24)) as pilot:
            app.post_message(_CwdWritableUpdated(True, cwd_at_start="/srv/work"))
            await pilot.pause()
            self.assertEqual(app.state.cwd_writable.value, True)
            self.assertIsNotNone(app.state.cwd_writable.last_attempt_at)

    async def test_stale_probe_dropped_when_cwd_changed(self) -> None:
        """Pin the in-flight reattribution guard: a probe that started
        against ``cwd_old`` lands after the user switched to ``cwd_new``.
        The handler must drop the result; the slot stays in zero state.
        """
        from uxon.tui.app import UxonApp, _CwdWritableUpdated

        ctx = _mk_ctx()  # cwd = /srv/work
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(80, 24)) as pilot:
            # Switch cwd before the probe lands.
            app.ctx.cwd = "/elsewhere"
            self.assertIsNone(app.state.cwd_writable.last_attempt_at)
            app.post_message(_CwdWritableUpdated(True, cwd_at_start="/srv/work"))
            await pilot.pause()
            # Stale probe was dropped → slot still in zero state.
            self.assertIsNone(app.state.cwd_writable.last_attempt_at)

    async def test_fresh_probe_against_current_cwd_lands(self) -> None:
        from uxon.tui.app import UxonApp, _CwdWritableUpdated

        ctx = _mk_ctx()
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(80, 24)) as pilot:
            app.ctx.cwd = "/elsewhere"
            app.post_message(_CwdWritableUpdated(False, cwd_at_start="/elsewhere"))
            await pilot.pause()
            self.assertEqual(app.state.cwd_writable.value, False)
            self.assertIsNotNone(app.state.cwd_writable.last_attempt_at)


@unittest.skipUnless(_textual_available(), "textual not installed")
class CwdChangeInvalidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cwd_change_resets_slot_to_zero(self) -> None:
        """``apply_loaded_ctx`` resets ``state.cwd_writable`` when the
        new ctx's cwd differs from the previous tick's cwd. Pre-fix
        behaviour was a stale ``True`` lingering until the next probe.
        """
        from uxon.tui.app import UxonApp, _CwdWritableUpdated

        ctx = _mk_ctx()  # cwd = /srv/work
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(80, 24)) as pilot:
            # Land a probe so the slot is non-zero.
            app.post_message(_CwdWritableUpdated(True, cwd_at_start="/srv/work"))
            await pilot.pause()
            self.assertIsNotNone(app.state.cwd_writable.last_attempt_at)

            # Synthesise an apply_loaded_ctx with a *different* cwd.
            from uxon.tui.app import _MainCtxLoaded

            new_ctx = _mk_ctx(cwd="/srv/other", cwd_short="other")
            app.post_message(_MainCtxLoaded(new_ctx))
            await pilot.pause()
            # Slot reset to zero — row reverts to "checking…".
            self.assertIsNone(app.state.cwd_writable.last_attempt_at)
            self.assertIsNone(app.state.cwd_writable.value)


if __name__ == "__main__":
    unittest.main()
