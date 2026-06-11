# SPDX-License-Identifier: MIT
"""Repeat-launch decision use-case.

When a compatible session already exists, decide whether to attach to it
or spawn a parallel one. Impure: :func:`resolve_repeat_decision` reads the
terminal (``input``) interactively and consults ``os.environ`` /
``identity.is_interactive_tty`` non-interactively, so this is an ``app``
use-case, not a pure domain rule.
"""

from __future__ import annotations

import os

from uxon.domain.config import Config, validate_repeat_mode
from uxon.domain.session import SessionInfo
from uxon.errors import fail
from uxon.infra import identity


def prompt_repeat_action(
    target_desc: str, attach_target: SessionInfo, existing: list[SessionInfo]
) -> str:
    session_names = ", ".join(session.name for session in existing)
    print(f"uxon: compatible sessions already exist for {target_desc}: {session_names}")
    prompt = f"[Enter] attach {attach_target.name}, type 'new' for a parallel session, or 'q' to cancel: "
    try:
        response = input(prompt).strip().lower()
    except EOFError:
        fail("unable to read response from terminal; rerun with --attach-existing or --new-session")
    if response in ("", "a", "attach"):
        return "attach"
    if response in ("n", "new"):
        return "new"
    if response in ("q", "quit", "cancel"):
        fail("cancelled", 130)
    fail("expected Enter/attach, new, or q; rerun with --attach-existing or --new-session")
    raise AssertionError("unreachable")


def get_env_repeat_noninteractive_mode() -> str | None:
    value = os.environ.get("UXON_REPEAT_NONINTERACTIVE_POLICY", "").strip()
    if not value:
        return None
    return validate_repeat_mode(value, "UXON_REPEAT_NONINTERACTIVE_POLICY")


def resolve_repeat_decision(
    explicit_mode: str | None,
    cfg: Config,
    target_desc: str,
    attach_target: SessionInfo,
    existing: list[SessionInfo],
) -> str:
    if explicit_mode is not None:
        return explicit_mode
    if identity.is_interactive_tty():
        return prompt_repeat_action(target_desc, attach_target, existing)
    env_mode = get_env_repeat_noninteractive_mode()
    decision = env_mode or cfg.repeat_noninteractive_mode
    if decision in {"attach", "new"}:
        return decision
    fail(
        "compatible session already exists and no interactive TTY is available; rerun with "
        "--attach-existing or --new-session, set UXON_REPEAT_NONINTERACTIVE_POLICY=attach|new, "
        "or configure repeat_noninteractive_mode. Use 'uxon doctor' to inspect the active socket/config."
    )
    raise AssertionError("unreachable")
