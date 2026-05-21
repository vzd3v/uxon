"""AgentsUnavailableScreen — shown when no agent is installed.

Pushed by :class:`UxonApp` after the async availability probe completes
and finds zero usable agents. In strict-whitelist mode the constructor
receives the configured ``enabled_agents`` tuple and the body lists
each with its install hint. In auto-mode the tuple is empty (no
configured whitelist) and the body falls back to listing every
``CATALOG`` agent — the operator just needs to install one.

Pure informational — dismiss returns ``None``.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from ..keymap import bindings_with_aliases


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

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("escape", "dismiss_screen", "Close", show=True),
        Binding("enter", "dismiss_screen", "Close", show=True),
        Binding("q", "dismiss_screen", "Close", show=False),
    )

    def __init__(self, enabled_agents: tuple[str, ...], *, error: str = "") -> None:
        super().__init__()
        self._enabled_agents = tuple(enabled_agents)
        self._error = error
        # Exposed for tests: plain-text body so assertions don't depend
        # on textual's Rich renderable internals.
        self.body_text: str = self._render_body()

    def _render_body(self) -> str:
        from uxon import agents as uxon_agents

        ids: tuple[str, ...] = self._enabled_agents or tuple(uxon_agents.CATALOG)
        lines: list[str] = []
        for aid in ids:
            spec = uxon_agents.CATALOG.get(aid)
            if spec is None:
                lines.append(f"  • {aid}: unknown agent id")
            else:
                lines.append(f"  • {aid} ({spec.binary}): {spec.install_hint}")
        return "\n".join(lines)

    def compose(self) -> ComposeResult:
        if self._error:
            # Probe error path: uxon could not run the install probe at
            # all (sudo failure, missing ``sh``, etc.). The install
            # list still helps once the operator fixes the probe error.
            title = "Could not probe for agents"
            intro = (
                f"uxon failed to probe installed agents: {self._error}\n"
                "Fix the host (e.g. sudo NOPASSWD for the launch user), "
                "dismiss this message, then press 'r'."
            )
            footer = "After the probe succeeds the agent list will populate."
        elif self._enabled_agents:
            title = "No agents installed"
            intro = (
                "uxon could not find any of the configured agents on PATH.\n"
                "Install at least one, dismiss this message, then press 'r'."
            )
            footer = "Configured in config.toml → [agents] enabled"
        else:
            title = "No agents installed"
            intro = (
                "uxon is in auto-mode (no [agents].enabled in config) and "
                "found no known agent installed.\n"
                "Install one of the below, dismiss this message, then press 'r'."
            )
            footer = "Pin a strict subset via [agents] enabled in config.toml"
        with Vertical():
            yield Static(title, classes="title")
            yield Static(intro)
            yield Static(self.body_text, id="agents-unavailable-body")
            yield Static(footer, classes="footer-hint")

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)
