"""Bounded message/paint synchronization for non-animation Pilot contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from textual.events import Key
from textual.pilot import Pilot


async def painted(pilot: Pilot) -> None:
    """Drain widget queues and positively acknowledge a completed screen refresh.

    This does not establish worker completion or animation completion. Tests must
    observe their worker result separately; geometry/animation tests use Pilot's
    ordinary synchronization.
    """
    async with asyncio.timeout(5):
        await pilot.pause(0)
        assert await pilot.app.screen.wait_for_refresh(), "screen did not acknowledge refresh"


async def wait_until(pilot: Pilot, ready: Callable[[], bool], description: str) -> None:
    """Wait for an observable continuation and its paint, with a bounded failure."""
    try:
        async with asyncio.timeout(5):
            while True:
                await painted(pilot)
                if ready():
                    return
    except TimeoutError:
        raise AssertionError(f"UI did not become ready: {description}") from None


async def send_key(pilot: Pilot, key: str, character: str | None = None) -> None:
    """Dispatch a real Key through the app queue, then acknowledge its paint.

    Use only when queue/paint completion is the contract. Pilot.press remains the
    driver for tests that need to wait for animations as part of the gesture.
    """
    event = Key(key, character)
    event.set_sender(pilot.app)
    assert pilot.app.post_message(event), "app refused key event"
    # A screen marker alone can precede the app forwarding the key to focus.
    # Acknowledge the app queue first, then drain the receiving widget queues.
    dispatched = asyncio.Event()
    assert pilot.app.call_later(dispatched.set), "app refused key dispatch acknowledgement"
    async with asyncio.timeout(5):
        await dispatched.wait()
    await painted(pilot)
