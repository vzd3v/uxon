"""Cwd changes invalidate permission probes while link results remain visible."""

import unittest
from dataclasses import replace

from helpers import make_dashboard_context as _mk_ctx

from uxon.domain.status import LinkHealthStatus
from uxon.tui.app import UxonApp
from uxon.tui.messages import _CwdWritableUpdated, _LinkHealthUpdated


class ProbeLandingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cwd_invalidation_stale_rejection_fresh_permission_and_link(self):
        ctx = _mk_ctx()
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            app.post_message(_CwdWritableUpdated(True, cwd_at_start=ctx.cwd))
            await pilot.pause()
            self.assertTrue(app.state.cwd_writable.value)
            self.assertIsNotNone(app.state.cwd_writable.last_attempt_at)
            screen = app.screen
            screen.apply_loaded_ctx(replace(ctx, cwd="/srv/other", cwd_short="other"))
            await pilot.pause()
            self.assertIsNone(app.state.cwd_writable.value)
            self.assertIsNone(app.state.cwd_writable.last_attempt_at)
            row = screen.query_one("#action-cwd")
            self.assertTrue(
                screen._cwd_writable_now(), "a cleared slot uses the fresh context seed"
            )
            self.assertEqual(row.detail, "(other)")
            app.post_message(_CwdWritableUpdated(True, cwd_at_start=ctx.cwd))
            await pilot.pause()
            self.assertIsNone(app.state.cwd_writable.last_attempt_at)
            app.post_message(_CwdWritableUpdated(False, cwd_at_start="/srv/other"))
            status = LinkHealthStatus(state="ok", summary="link healthy")
            app.post_message(_LinkHealthUpdated(status))
            await pilot.pause()
            self.assertFalse(app.state.cwd_writable.value)
            self.assertIsNotNone(app.state.cwd_writable.last_attempt_at)
            self.assertFalse(screen._cwd_writable_now())
            self.assertIn("not launchable", row.detail.lower())
            self.assertEqual(app.state.link_health.value, status)
            self.assertIsNotNone(app.state.link_health.last_attempt_at)
            self.assertIn("link healthy", str(screen.query_one("#server-status").content))
