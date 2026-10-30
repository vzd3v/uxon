"""Cwd changes invalidate permission probes while link results remain visible."""

import unittest
from dataclasses import replace
from threading import Event

from harness.textual_ready import painted, wait_until
from helpers import make_dashboard_context as _mk_ctx

from uxon.domain.status import LinkHealthStatus
from uxon.tui.app import UxonApp
from uxon.tui.context import CallbackError
from uxon.tui.messages import _CwdWritableUpdated, _LinkHealthUpdated
from uxon.tui.widgets.action_row import ActionRow


class ProbeLandingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cwd_invalidation_stale_rejection_fresh_permission_and_link(self):
        started, release, finished = Event(), Event(), Event()

        def blocked_probe():
            started.set()
            try:
                if not release.wait(10):
                    raise TimeoutError("access probe release missing")
                return True
            finally:
                finished.set()

        ctx = _mk_ctx(
            cwd_writable=None,
            existing_projects=[("project", "")],
            on_probe_cwd_writable=blocked_probe,
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            try:
                await wait_until(pilot, started.is_set, "access probe started")
                screen = app.screen
                rows = list(screen.query(ActionRow))[:3]
                self.assertEqual(
                    [row.label for row in rows],
                    ["Open project", "Create new project", "Open in this folder"],
                )
                row = rows[2]
                self.assertFalse(row.can_focus)
                self.assertIn("checking access", row.detail)
                self.assertEqual(app.focused.id, "action-open")
                await pilot.press("down")
                self.assertEqual(app.focused.id, "action-new")
                await pilot.click("#action-cwd")
                self.assertIs(app.screen, screen)
                self.assertEqual(app.focused.id, "action-new")
                await pilot.press("s", "a")
                self.assertEqual(screen.query_one("#search-bar Input").value, "a")
                self.assertFalse(finished.is_set(), "input must work while access is blocked")
                await pilot.press("escape", "escape")

                screen.apply_loaded_ctx(
                    replace(
                        ctx,
                        cwd="/srv/other",
                        cwd_short="other",
                        on_probe_cwd_writable=lambda: False,
                    )
                )
                await wait_until(
                    pilot, lambda: app.state.cwd_writable.value is False, "new cwd denied"
                )
                release.set()
                await wait_until(pilot, finished.is_set, "old cwd probe finished")
                await painted(pilot)
                self.assertFalse(row.can_focus)
                self.assertIn("no writable launch profile", row.detail)
                app.post_message(_CwdWritableUpdated(True, cwd_at_start="/srv/other", generation=1))
                await painted(pilot)
                self.assertFalse(row.can_focus, "superseded probes cannot enable the row")

                app.ctx.on_probe_cwd_writable = lambda: True
                await pilot.press("r")
                await wait_until(pilot, lambda: row.can_focus, "fresh access granted")
                row.focus()
                await painted(pilot)
                self.assertIs(app.focused, row)
                self.assertEqual(row.detail, "(other)")
                await pilot.press("s")
                app.ctx.on_probe_cwd_writable = lambda: False
                app.kick_refresh(manual=True)
                await wait_until(
                    pilot, lambda: app.state.cwd_writable.value is False, "access revoked"
                )
                await pilot.press("escape")
                self.assertIsNotNone(app.focused, "search must return to an available item")
                self.assertIsNot(app.focused, row)

                def failed_probe():
                    raise CallbackError("Execution backend unreachable: connection refused")

                app.ctx.on_probe_cwd_writable = failed_probe
                await pilot.press("r")
                await wait_until(
                    pilot, lambda: "connection refused" in row.detail, "specific access failure"
                )
                self.assertFalse(row.can_focus)
                status = LinkHealthStatus(state="ok", summary="link healthy")
                app.post_message(_LinkHealthUpdated(status))
                await painted(pilot)
                self.assertEqual(app.state.link_health.value, status)
                self.assertIn("link healthy", str(screen.query_one("#server-status").content))
            finally:
                release.set()
