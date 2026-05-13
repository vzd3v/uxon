"""Regression tests for the probe worker-thread race fix.

The worker (``_probe_host_worker``) runs under ``run_worker(thread=True)``
and must not mutate ``self.ctx.<field>`` directly: it builds a local
availability dict and posts it in a :class:`_HostReportUpdated`
message; the on-loop handler folds the payload into the slot store
via :func:`slot_state.apply`. No ctx field is touched from the thread.

The test pins both halves of the contract:

1. **Thread-id**: ``slot_state.apply`` records ``threading.get_ident()``
   every call. Recorded ids must all be the event-loop thread.
2. **Payload**: ``state.agent_availability.value`` reflects the worker's
   payload via the message path — verifies the data made it through,
   not just that nothing wrote on the wrong thread.
"""

from __future__ import annotations

import asyncio
import threading
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
            cmd=("/bin/true",), label="existing"
        ),
    )
    base.update(overrides)
    return TuiContext(**base)


@unittest.skipUnless(_textual_available(), "textual not installed")
class ProbeWorkerRaceFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_runs_only_on_event_loop(self) -> None:
        from uxon.tui import app as app_mod
        from uxon.tui.app import UxonApp, _HostReportUpdated

        # Spy on slot_state.apply: capture thread id when called.
        # Patching the module-level binding inside ``app`` keeps the
        # production import path honest — the dispatcher does
        # ``from .slot_state import apply as apply_slot`` inside the
        # method, so we patch slot_state's ``apply`` and the
        # re-import resolves to our spy.
        thread_ids: list[int] = []
        from uxon.tui import slot_state as ss_mod

        real_apply = ss_mod.apply

        def spy_apply(prev, r, **kw):
            thread_ids.append(threading.get_ident())
            return real_apply(prev, r, **kw)

        ss_mod.apply = spy_apply  # type: ignore[assignment]
        try:
            ctx = _mk_ctx()
            app = UxonApp(ctx, probe_agents=False)
            async with app.run_test(size=(80, 24)) as pilot:
                event_loop_tid = threading.get_ident()
                # Post a non-bare message — exercises the apply path.
                app.post_message(
                    _HostReportUpdated(
                        availability={"claude": "OK"},
                    )
                )
                await pilot.pause()
                # Apply was called at least once for availability.
                self.assertGreaterEqual(len(thread_ids), 1)
                # Every call ran on the event-loop thread.
                self.assertTrue(
                    all(tid == event_loop_tid for tid in thread_ids),
                    f"apply ran on non-event-loop thread(s): {thread_ids}, "
                    f"event-loop tid={event_loop_tid}",
                )
                # Payload landed in state.
                self.assertEqual(app.state.agent_availability.value, {"claude": "OK"})
        finally:
            ss_mod.apply = real_apply  # type: ignore[assignment]
            # Silence unused-import in case asyncio import got optimized.
            _ = asyncio
            _ = app_mod


@unittest.skipUnless(_textual_available(), "textual not installed")
class ProbeWorkerNoCtxMutationTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_builds_local_dicts_and_posts_payload(self) -> None:
        """Pin the new shape: ``_probe_host_worker`` runs the probe,
        builds local dicts, and posts a :class:`_HostReportUpdated`
        with the dicts in the payload — no in-place mutation of
        ``self.ctx.<field>`` from the thread.

        Stub ``probes.probe_host`` so the test doesn't shell out;
        capture the posted message and assert payload shape.
        """
        from uxon import agents as uxon_agents
        from uxon import probes as uxon_probes
        from uxon.tui.app import UxonApp, _HostReportUpdated

        class _BinaryStatus:
            def __init__(self, name, path):
                self.name = name
                self.path = path

        class _Report:
            agents = {
                "claude": _BinaryStatus("claude", "/usr/bin/claude"),
                "codex": _BinaryStatus("codex", None),
                "cursor": _BinaryStatus("cursor-agent", None),
            }
            tmux = _BinaryStatus("tmux", "/usr/bin/tmux")

        original_probe = uxon_probes.probe_host
        uxon_probes.probe_host = lambda _user: _Report()  # type: ignore[assignment]

        try:
            ctx = _mk_ctx()
            app = UxonApp(ctx, probe_agents=False)

            posted: list[_HostReportUpdated] = []
            real_post = app.post_message

            def capture_post(message):
                if isinstance(message, _HostReportUpdated):
                    posted.append(message)
                return real_post(message)

            app.post_message = capture_post  # type: ignore[assignment]

            async with app.run_test(size=(80, 24)) as pilot:
                # Run the worker explicitly. Production wires it via
                # ``run_worker(thread=True)``; we run synchronously
                # here in a thread to keep the test deterministic
                # while still exercising the off-loop code path.
                event_loop = asyncio.get_running_loop()
                done = event_loop.create_future()

                def runner():
                    try:
                        app._probe_host_worker()
                    finally:
                        event_loop.call_soon_threadsafe(done.set_result, None)

                t = threading.Thread(target=runner)
                t.start()
                await done
                t.join()
                await pilot.pause()

                # The worker posted exactly one HostReportUpdated.
                self.assertEqual(len(posted), 1)
                msg = posted[0]
                self.assertFalse(msg.error)
                self.assertIn("claude", msg.availability or {})
                self.assertEqual(
                    msg.availability["claude"],  # type: ignore[index]
                    uxon_agents.AgentAvailability(status="ok", path="/usr/bin/claude"),
                )
                # ``ctx`` here uses the new default empty enabled_agents,
                # so the worker is in auto-mode: only installed agents
                # are surfaced in the availability dict (no "missing"
                # entries).
                self.assertNotIn("codex", msg.availability or {})
        finally:
            uxon_probes.probe_host = original_probe  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
