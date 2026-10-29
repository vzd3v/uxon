"""Parse and validate an ``uxon list --json`` wire envelope.

Shared between the live collector (:mod:`uxon.infra.remote.collector`)
and the demo loader (:mod:`uxon.infra.demo`) so both apply identical
shape checks against the same :data:`uxon.domain.wire_schema.WIRE_SCHEMA_VERSION`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, cast, get_type_hints

import msgspec

from uxon.domain.session import SessionIdentity
from uxon.domain.wire_schema import (
    REMOTE_ERROR_CODES,
    WIRE_SCHEMA_VERSION,
    HostStats,
    PeerInfo,
    RemoteErrorCode,
    RemoteSessionPayload,
    SessionRecord,
)

_SESSION_FIELDS = {
    key: value for key, value in get_type_hints(SessionRecord).items() if key != "identity"
}
_HOST_FIELDS = get_type_hints(HostStats)
_NONNEGATIVE_FIELDS = {
    "cpu_pct",
    "rss_kib",
    "mem_used_kib",
    "mem_total_kib",
    "loadavg_1m",
    "uptime_s",
}


def _validate_fields(value: dict[str, Any], fields: dict[str, Any], path: str) -> str | None:
    """Validate present known fields; missing and unknown additions stay usable."""
    for key, annotation in fields.items():
        if key not in value:
            continue
        try:
            checked = msgspec.convert(value[key], type=annotation, strict=True)
        except msgspec.ValidationError as exc:
            return f"{path}.{key}: {exc}"
        if key in _NONNEGATIVE_FIELDS and (
            checked < 0 or isinstance(checked, float) and not math.isfinite(checked)
        ):
            return f"{path}.{key}: expected a finite nonnegative number"
        if key == "active_pid" and checked is not None and checked <= 0:
            return f"{path}.{key}: expected a positive PID or null"
        if key == "pane_pids" and isinstance(checked, list) and any(pid <= 0 for pid in checked):
            return f"{path}.{key}: expected positive PIDs"
    return None


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
    seen: dict[str, int] = {}
    for index, record in enumerate(sessions):
        path = f"envelope.data.sessions[{index}]"
        error = _validate_fields(record, _SESSION_FIELDS, path)
        if error:
            return failure(error)
        identity = None
        if "identity" in record:
            try:
                identity = msgspec.convert(record["identity"], type=SessionIdentity, strict=True)
            except msgspec.ValidationError as exc:
                return failure(f"{path}.identity: {exc}")
            invalid = identity.invalid_fields(require_nonce=False)
            if invalid:
                return failure(f"{path}.identity: invalid {', '.join(invalid)}")
            if identity.user != record.get("user") or identity.name != record.get("name"):
                return failure(f"{path}.identity: user/name differ from the session record")
        key = identity.key if identity else f"{record.get('user', '')}/{record.get('name', '')}"
        if key in seen:
            return failure(f"{path}: duplicate identity already reported at sessions[{seen[key]}]")
        seen[key] = index
    error = _validate_fields(
        data, {"scope_skipped": list[str], "scope_users": list[str]}, "envelope.data"
    )
    if error:
        return failure(error)
    stats = env.get("host_stats")
    if stats is not None:
        if not isinstance(stats, dict):
            return failure("envelope.host_stats is not an object or null")
        error = _validate_fields(stats, _HOST_FIELDS, "envelope.host_stats")
        if error:
            return failure(error)
    return ParsedEnvelope(sessions, data.get("scope_skipped", []), stats, peer=peer)
