"""Parse and validate an ``uxon list --json`` wire envelope.

Shared between the live collector (:mod:`uxon.infra.remote.collector`)
and the demo loader (:mod:`uxon.infra.demo`) so both apply identical
shape checks against the same :data:`uxon.domain.wire_schema.WIRE_SCHEMA_VERSION`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import msgspec

from uxon.domain.wire_schema import (
    REMOTE_ERROR_CODES,
    WIRE_SCHEMA_VERSION,
    PeerInfo,
    RemoteErrorCode,
    RemoteSessionPayload,
)

# Stable substring emitted by a peer whose ``enable_all_users_list =
# false`` rejects ``list --all-users``. The collector greps stderr for
# this marker to decide whether to retry with own-only
# ``list --json``. Producer side: see ``cli``'s ``--all-users``
# failure paths.
ALL_USERS_DISABLED_MARKER = "uxon-error: all-users-disabled"


@dataclass(frozen=True)
class ParsedEnvelope:
    """Decoded response, retaining diagnostic metadata even on failure."""

    sessions: list[RemoteSessionPayload] | None = None
    scope_skipped: list[str] = field(default_factory=list)
    host_stats: dict[str, Any] | None = None
    error: str | None = None
    error_code: RemoteErrorCode | None = None
    peer: PeerInfo = field(default_factory=PeerInfo)


def _peer_info(value: dict[str, Any]) -> PeerInfo:
    peer: PeerInfo = {}
    version = value.get("uxon_version")
    schema = value.get("schema_version")
    if isinstance(version, str):
        peer["uxon_version"] = version
    if isinstance(schema, str):
        peer["schema_version"] = schema
    return peer


def decode_envelope(payload: str) -> ParsedEnvelope:
    """Decode metadata before checking the session schema; reject relayed errors."""
    peer: PeerInfo = {}

    def failure(message: str, code: RemoteErrorCode = "invalid_response") -> ParsedEnvelope:
        return ParsedEnvelope(error=message, error_code=code, peer=peer)

    try:
        env: Any = msgspec.json.decode(payload)
    except msgspec.DecodeError as exc:
        return failure(f"invalid JSON: {exc}")
    if not isinstance(env, dict):
        return failure("envelope is not a JSON object")
    peer = _peer_info(env)
    schema = env.get("schema_version")
    if schema != WIRE_SCHEMA_VERSION:
        code: RemoteErrorCode = "incompatible"
        if (
            isinstance(schema, str)
            and len(schema) <= 16
            and schema.isascii()
            and schema.isdecimal()
        ):
            if int(schema) < int(WIRE_SCHEMA_VERSION):
                code = "update_remote"
            elif int(schema) > int(WIRE_SCHEMA_VERSION):
                code = "update_local"
        return failure(
            f"schema_version mismatch: peer reports {schema!r}, local expects {WIRE_SCHEMA_VERSION!r}",
            code,
        )
    if env.get("kind") != "list":
        return failure(f"unexpected envelope kind {env.get('kind')!r}")
    data = env.get("data")
    if not isinstance(data, dict):
        return failure("envelope.data is not an object")
    raw_peer = data.get("peer")
    if isinstance(raw_peer, dict):
        peer = _peer_info(raw_peer)
    if data.get("error") is not None:
        raw_error = data["error"]
        if not isinstance(raw_error, dict) or not isinstance(raw_error.get("message"), str):
            return failure("envelope.data.error is not a remote error")
        raw_code = raw_error.get("code")
        return failure(
            raw_error["message"] or "remote request failed",
            cast(RemoteErrorCode, raw_code)
            if isinstance(raw_code, str) and raw_code in REMOTE_ERROR_CODES
            else "remote_error",
        )
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        return failure("envelope.data.sessions is not a list")
    if any(not isinstance(record, dict) for record in sessions):
        return failure("envelope.data.sessions contains a non-object record")
    raw_skipped = data.get("scope_skipped", [])
    skipped = (
        [u for u in raw_skipped if isinstance(u, str)] if isinstance(raw_skipped, list) else []
    )
    stats = env.get("host_stats")
    return ParsedEnvelope(sessions, skipped, stats if isinstance(stats, dict) else None, peer=peer)
