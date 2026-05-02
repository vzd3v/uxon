"""Detected-agents banner: a single-row hint suggesting the user enable
an agent that's installed on the host but not in ``[agents].enabled``.

Composition over inheritance: a thin :class:`Static` subclass that just
renders a stable label and exposes a couple of helpers. Actual key
handling (``a`` / ``d``) lives on :class:`MainScreen` so all key routing
goes through ``BINDINGS`` (see AGENTS.md hard rule).
"""

from __future__ import annotations

from textual.widgets import Static


def render_banner_text(
    detected_ids: list[str],
    *,
    repo_config_writable: bool,
) -> str:
    """Return the text shown by the banner for a given detected-agents list.

    The single-agent and multi-agent forms differ only in label; both
    spell out the actions on the right ("[a] enable / [d] dismiss").
    When the repo config is not writable by this user, ``[a]`` is hinted
    as inactive and a follow-up note tells the user how to proceed.
    """
    if not detected_ids:
        return ""
    if len(detected_ids) == 1:
        head = f"{detected_ids[0]} is installed but not enabled."
    else:
        joined = ", ".join(detected_ids)
        head = f"installed but not enabled: {joined}."
    if repo_config_writable:
        actions = "[a] add to config   [x] dismiss"
    else:
        actions = "[a] (read-only — ask operator to edit [agents].enabled)   [x] dismiss"
    return f"{head}  {actions}"


class DetectedAgentsBanner(Static):
    """Single-row banner for detected-but-not-enabled agents.

    Reactive state lives on ``ctx.detected_agents`` and the per-user
    dismissed list; the screen recomputes the banner text whenever the
    host probe lands.
    """

    DEFAULT_CSS = """
    DetectedAgentsBanner {
        color: $secondary;
        background: $boost;
        padding: 0 1;
        margin: 0 1;
        height: 1;
    }
    DetectedAgentsBanner.-hidden { display: none; }
    """

    def update_from(self, detected_ids: list[str], *, repo_config_writable: bool) -> None:
        text = render_banner_text(detected_ids, repo_config_writable=repo_config_writable)
        self.update(text)
        self.set_class(not text, "-hidden")
