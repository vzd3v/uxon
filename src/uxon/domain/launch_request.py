# SPDX-License-Identifier: MIT
"""Pure launch/attach request DTO for the TUI bridge.

The TUI never spawns subprocesses; its activation handlers return one of
these and the outer loop runs the command. Pure, stdlib-only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ManagedTmuxLaunch:
    """Metadata needed to create and finalize a managed tmux launch."""

    create_cmd: tuple[str, ...]
    query_cmd: tuple[str, ...]
    kill_cmd: tuple[str, ...]
    record_socket: str
    record_session: str
    record_nonce: str
    record_dir: str
    launch_profile: str
    agent: str
    launch_user: str
    container_profile: str = ""
    container_profile_fingerprint: str = ""
    container: str = ""
    container_id: str = ""
    container_cgroup: str = ""
    container_epoch: str = ""


@dataclass(frozen=True)
class LaunchRequest:
    """Describes a tmux invocation the TUI wants the outer loop to fork-and-wait.

    The TUI itself never spawns subprocesses; activation handlers return
    one of these, the main loop exits the fullscreen context, runs
    the ``prelaunch`` commands and then ``cmd``, waits for exit, and
    re-enters the main screen with a refreshed context.
    """

    cmd: tuple[str, ...]
    prelaunch: tuple[tuple[str, ...], ...] = ()
    label: str = ""
    managed: ManagedTmuxLaunch | None = None


def session_name_from_launch_label(label: str) -> str:
    """Extract the bare tmux session name from a LaunchRequest label.

    Labels are constructed as ``"<verb> <session>"`` (verbs ``launch``,
    ``attach``, ``switch-client``) with an optional ``" (nested)"``
    suffix on the switch-client form.  Audit ``session.*`` events take
    the bare session name in the ``session`` field; the labelled form
    breaks cross-event correlation with CLI emits.
    """
    if " " not in label:
        return label
    rest = label.split(" ", 1)[1]
    if rest.endswith(" (nested)"):
        rest = rest[: -len(" (nested)")]
    return rest
