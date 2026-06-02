"""Parse and validate an ``uxon list --json`` wire envelope.

Shared between the live collector (:mod:`uxon.infra.remote.collector`)
and the demo loader (:mod:`uxon.infra.demo`) so both apply identical
shape checks against the same :data:`uxon.domain.wire_schema.WIRE_SCHEMA_VERSION`.
"""

from __future__ import annotations

from typing import Any

import msgspec

from uxon.domain.wire_schema import WIRE_SCHEMA_VERSION, RemoteSessionPayload

# Stable substring emitted by a peer whose ``enable_all_users_list =
# false`` rejects ``list --all-users``. The collector greps stderr for
# this marker to decide whether to retry with own-only
# ``list --json``. Producer side: see ``cli``'s ``--all-users``
# failure paths.
ALL_USERS_DISABLED_MARKER = "uxon-error: all-users-disabled"


def parse_envelope(
    payload: str,
) -> tuple[list[RemoteSessionPayload] | None, list[str], dict[str, Any] | None, str | None]:
    """Validate and unpack an ``uxon list --json`` envelope.

    Returns ``(sessions, scope_skipped, host_stats, None)`` on success,
    or ``(None, [], None, error)`` when the payload is malformed.
    Failure modes:

    - JSON parse error.
    - Top-level shape is not a dict.
    - ``schema_version`` is missing or differs from the local
      :data:`WIRE_SCHEMA_VERSION`. Cross-version peers are rejected
      explicitly so a future schema bump fails loud rather than
      silently dropping fields.
    - ``kind`` is not ``"list"`` (the collector only ever runs
      ``list``; anything else is a remote bug or a wrong binary).
    - ``data.sessions`` is missing or not a list.

    ``scope_skipped`` is the optional per-target-sudo skipped-users
    list emitted by peers that ran the per-target probe. Older peers
    omit the field — we treat that as ``[]`` (forward-compatible
    addition to the schema, no version bump).

    No deep validation of individual session records — they're
    treated as opaque dicts. If a peer renames a session field, the
    TUI will surface the absence; we don't want to fail the whole
    snapshot for one bad session.
    """
    # ``msgspec.json.decode`` is the C-accelerated decoder; we keep
    # the result as untyped ``Any`` here because the remaining shape
    # checks below already validate the envelope and individual
    # session dicts are intentionally opaque (see docstring).
    try:
        env: Any = msgspec.json.decode(payload)
    except msgspec.DecodeError as exc:
        return None, [], None, f"invalid JSON: {exc}"
    if not isinstance(env, dict):
        return None, [], None, "envelope is not a JSON object"
    schema_version = env.get("schema_version")
    if schema_version != WIRE_SCHEMA_VERSION:
        return (
            None,
            [],
            None,
            (
                f"schema_version mismatch: peer reports {schema_version!r}, "
                f"local expects {WIRE_SCHEMA_VERSION!r}"
            ),
        )
    if env.get("kind") != "list":
        return None, [], None, f"unexpected envelope kind {env.get('kind')!r}"
    data = env.get("data")
    if not isinstance(data, dict):
        return None, [], None, "envelope.data is not an object"
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        return None, [], None, "envelope.data.sessions is not a list"
    raw_skipped = data.get("scope_skipped", [])
    if isinstance(raw_skipped, list):
        scope_skipped = [str(u) for u in raw_skipped if isinstance(u, str)]
    else:
        scope_skipped = []
    # Optional, additive ``host_stats`` block. Older peers omit it;
    # we treat absence as ``None`` rather than a parse error.
    host_stats_raw = env.get("host_stats")
    host_stats = host_stats_raw if isinstance(host_stats_raw, dict) else None
    return sessions, scope_skipped, host_stats, None
