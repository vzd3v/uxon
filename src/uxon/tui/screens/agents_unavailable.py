"""AgentsUnavailableScreen — shown when no agent is installed.

Pushed by :class:`UxonApp` after the async availability probe completes
and finds zero usable agents. In strict-whitelist mode the constructor
receives the configured ``enabled_profiles`` tuple and the body lists
each with its install hint. In auto-mode the tuple is empty (no
configured whitelist) and the body falls back to listing every
catalogued agent (the threaded ``agents`` catalog) — the operator just
needs to install one.

Pure informational — dismiss returns ``None``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from ..keymap import bindings_with_aliases
from .modal_base import CardModal


class AgentsUnavailableScreen(CardModal[None]):
    # Card chrome comes from CardModal; this informational modal recolours
    # the card border and title to ``$error`` and adds body spacing.
    DEFAULT_CSS = """
    AgentsUnavailableScreen .modal-card {
        width: 80;
        border: round $error;
    }
    AgentsUnavailableScreen .title { color: $error; }
    AgentsUnavailableScreen #agents-unavailable-body { margin-bottom: 1; }
    AgentsUnavailableScreen .footer-hint { color: $text-muted; }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("escape", "dismiss_screen", "Close", show=True),
        Binding("enter", "dismiss_screen", "Close", show=True),
        Binding("q", "dismiss_screen", "Close", show=False),
    )

    def __init__(
        self,
        enabled_profiles: tuple[str, ...],
        *,
        agents: dict[str, Any] | None = None,
        launch_profiles: dict[str, Any] | None = None,
        availability: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        super().__init__()
        self._enabled_profiles = tuple(enabled_profiles)
        self._agents: dict[str, Any] = dict(agents or {})
        self._launch_profiles: dict[str, Any] = dict(launch_profiles or {})
        self._error = error
        self._availability = dict(availability or {})
        # Exposed for tests: plain-text body so assertions don't depend
        # on textual's Rich renderable internals.
        self.body_text: str = self._render_body()

    def _render_body(self) -> str:
        ids: tuple[str, ...] = self._enabled_profiles or tuple(self._agents)
        lines: list[str] = []
        for aid in ids:
            profile = self._launch_profiles.get(aid)
            agent_id = getattr(profile, "agent", aid)
            spec = self._agents.get(agent_id)
            detail = getattr(self._availability.get(aid), "error", None)
            if detail:
                lines.append(f"  • {aid}: {detail}")
                continue
            if spec is None:
                lines.append(f"  • {aid}: unknown agent id")
            elif profile is not None:
                lines.append(f"  • {aid} ({agent_id}/{spec.binary}): {spec.install_hint}")
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
                "then restart uxon."
            )
            footer = "Command discovery runs once per uxon process."
        elif self._enabled_profiles:
            title = "No agents available"
            intro = (
                "No configured launch profile is available.\n"
                "Fix the reported failure, then restart uxon."
            )
            footer = "Configured in config.toml → [launch] enabled_profiles"
        else:
            title = "No agents installed"
            intro = (
                "uxon is in auto-mode (no [launch].enabled_profiles in config) and "
                "found no known agent installed.\n"
                "Install one of the below, then restart uxon."
            )
            footer = "Pin a strict subset via [launch].enabled_profiles in config.toml"
        with self.card():
            yield Static(title, classes="title")
            yield Static(intro)
            yield Static(self.body_text, id="agents-unavailable-body")
            yield Static(footer, classes="footer-hint")

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)
