"""Helpers for batching simple Textual Pilot screen scenarios.

Use these only for smoke/wiring tests where scenarios do not share app state
except through the intentional host lifecycle. Keep complex regression tests
as separate `run_test()` cases when isolation is part of the behavior.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from textual.app import App
    from textual.screen import Screen
else:
    App = Any
    Screen = Any


Interact = Callable[[App, Any], Awaitable[None]]
ScreenFactory = Callable[[], Screen]


@dataclass(frozen=True)
class ScreenScenario:
    name: str
    screen_factory: ScreenFactory
    interact: Interact
    expected: Any


def _make_scenario_host() -> App:
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class ScenarioHost(App):
        """Minimal host that can run multiple scenarios in one app."""

        CSS = ""

        def compose(self) -> ComposeResult:
            yield Static("scenario-host")

    return ScenarioHost()


def press_keys(*keys: str) -> Interact:
    async def interact(app: App, pilot: Any) -> None:
        await pilot.press(*keys)

    return interact


async def run_screen_scenarios(
    scenarios: list[ScreenScenario],
    *,
    size: tuple[int, int] = (100, 30),
) -> list[Any]:
    """Run compatible screen scenarios inside one `App.run_test()` lifecycle."""
    app = _make_scenario_host()
    results: list[Any] = []
    async with app.run_test(size=size) as pilot:
        for scenario in scenarios:
            result: Any = "unset"

            def done(value: Any) -> None:
                nonlocal result
                result = value

            app.push_screen(scenario.screen_factory(), done)
            await pilot.pause()
            await scenario.interact(app, pilot)
            await pilot.pause()
            results.append(result)
            while len(app.screen_stack) > 1:
                app.pop_screen()
                await pilot.pause()
        app.exit()
    return results
