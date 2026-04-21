"""AgentsUnavailableScreen — shown when no enabled agent is installed.

Pushed by :class:`CcwApp` after the async availability probe completes
iff every agent listed in ``ctx.enabled_agents`` resolved to
``missing`` / ``timeout``. Lists each enabled agent together with the
install hint from :data:`ccw_agents.CATALOG`, plus a short "configured
in agents.enabled" footer so the operator knows which knob to look at.

Pure informational — dismiss returns ``None``.
"""
from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


class AgentsUnavailableScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    AgentsUnavailableScreen { align: center middle; }
    AgentsUnavailableScreen > Vertical {
        width: 80; height: auto; padding: 1 2;
        border: round $error; background: $surface;
    }
    AgentsUnavailableScreen .title {
        text-style: bold; color: $error; margin-bottom: 1;
    }
    AgentsUnavailableScreen #agents-unavailable-body { margin-bottom: 1; }
    AgentsUnavailableScreen .footer-hint { color: $text-muted; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss_screen", "Close", show=True),
        Binding("enter", "dismiss_screen", "Close", show=True),
        Binding("q", "dismiss_screen", "Close", show=False),
    ]

    def __init__(self, enabled_agents: tuple[str, ...]) -> None:
        super().__init__()
        self._enabled_agents = tuple(enabled_agents)
        # Exposed for tests: plain-text body so assertions don't depend
        # on textual's Rich renderable internals.
        self.body_text: str = self._render_body()

    def _render_body(self) -> str:
        import ccw_agents

        lines: list[str] = []
        for aid in self._enabled_agents:
            spec = ccw_agents.CATALOG.get(aid)
            if spec is None:
                lines.append(f"  • {aid}: unknown agent id")
            else:
                lines.append(f"  • {aid} ({spec.binary}): {spec.install_hint}")
        return "\n".join(lines) if lines else "  (no agents enabled)"

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("No agents installed", classes="title")
            yield Static(
                "ccw could not find any of the configured agents on PATH.\n"
                "Install at least one, then quit (q) and restart ccw:",
            )
            yield Static(self.body_text, id="agents-unavailable-body")
            yield Static(
                "Configured in config.toml → [agents] enabled",
                classes="footer-hint",
            )

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)
