"""Multi-host: ``[[remote_hosts]]`` schema.

A *remote host* is a peer machine where uxon is also installed and
that the local TUI can poll over SSH for its session list. Each
entry names one such peer: a label, the SSH alias to connect with,
and the command to invoke on the far side.

Profiles come from ``config/config.toml`` under ``[[remote_hosts]]``
and are parsed into immutable :class:`RemoteHost` instances by
:func:`load_remote_hosts`. Validation errors surface through a
single :class:`RemoteHostError` so the caller (cli.load_config) can
``fail()`` with a clean message.

This module is pure data: no subprocess, no filesystem, no network.
The actual SSH transport, snapshot caching, and TUI wiring live in
later commits in this sequence (RemoteCollector → TUI HOST column
→ CLI ``--host`` flag).
"""

from __future__ import annotations

from dataclasses import dataclass


class RemoteHostError(ValueError):
    """Raised when a ``[[remote_hosts]]`` entry is malformed."""


@dataclass(frozen=True)
class RemoteHost:
    """One ``[[remote_hosts]]`` entry after validation.

    Attributes:
        name: Stable label. Used as the snapshot-cache key
            (``~/.local/state/uxon/remote/<name>.json``), the TUI
            ``HOST`` column value, and the ``--host`` CLI selector.
            Must be unique across the array, ASCII, no whitespace —
            it ends up in a filename.
        ssh_alias: Host token passed verbatim to ``ssh``. Resolved
            through the user's ``~/.ssh/config`` (per the multi-host
            design: SSH config is the single source of connection
            truth — no inline user/port/identity here).
        description: Free-form human note shown in TUI tooltips.
            Empty string if absent.
        remote_uxon: Command invoked on the remote side. Defaults
            to ``"uxon"`` so a standard install on the peer Just
            Works; override when the peer keeps uxon under an
            unusual path. Quoted/escaped at call time by the
            collector — do NOT embed shell metacharacters here.
    """

    name: str
    ssh_alias: str
    description: str
    remote_uxon: str


_VALID_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


def _validate_host(raw: dict, index: int, seen_names: set[str]) -> RemoteHost:
    def _req_str(field: str) -> str:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise RemoteHostError(f"remote_hosts[{index}]: missing or empty '{field}'")
        return value.strip()

    name = _req_str("name")
    # ``name`` becomes a filename in the snapshot cache; reject characters
    # that would force quoting or path-escape tricks. Conservative ASCII
    # whitelist keeps us out of unicode-edge-case territory.
    bad = [c for c in name if c not in _VALID_NAME_CHARS]
    if bad:
        raise RemoteHostError(
            f"remote_hosts[{index}]: name {name!r} contains invalid character(s) "
            f"{''.join(sorted(set(bad)))!r}; allowed: ASCII letters, digits, '_', '-', '.'"
        )
    if name in seen_names:
        raise RemoteHostError(f"remote_hosts: duplicate name {name!r}")

    ssh_alias = _req_str("ssh_alias")

    description_raw = raw.get("description", "")
    if not isinstance(description_raw, str):
        raise RemoteHostError(f"remote_hosts[{name}]: description must be a string")
    description = description_raw.strip()

    remote_uxon_raw = raw.get("remote_uxon", "uxon")
    if not isinstance(remote_uxon_raw, str) or not remote_uxon_raw.strip():
        raise RemoteHostError(f"remote_hosts[{name}]: remote_uxon must be a non-empty string")
    remote_uxon = remote_uxon_raw.strip()

    # Reject unknown keys — better to fail loudly than silently ignore a
    # typo (e.g. ``ssh_alaias = "..."``) the operator expected to take
    # effect.
    known_keys = {"name", "ssh_alias", "description", "remote_uxon"}
    extra = set(raw.keys()) - known_keys
    if extra:
        raise RemoteHostError(
            f"remote_hosts[{name}]: unknown key(s) {sorted(extra)!r}; "
            f"expected one of {sorted(known_keys)!r}"
        )

    return RemoteHost(
        name=name,
        ssh_alias=ssh_alias,
        description=description,
        remote_uxon=remote_uxon,
    )


def load_remote_hosts(raw_list: object) -> list[RemoteHost]:
    """Parse and validate ``[[remote_hosts]]``. ``raw_list`` is the raw
    value read from TOML — a list of tables, or ``None``/missing.

    Returns an empty list when the section is absent. Raises
    :class:`RemoteHostError` on the first malformed entry; partial
    parsing is intentionally not supported — a config with a typo
    should fail loudly at startup rather than silently dropping a
    host the operator expected to see.
    """
    if raw_list is None:
        return []
    if not isinstance(raw_list, list):
        raise RemoteHostError("remote_hosts must be an array of tables")
    hosts: list[RemoteHost] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_list):
        if not isinstance(raw, dict):
            raise RemoteHostError(f"remote_hosts[{i}] must be a table")
        host = _validate_host(raw, i, seen)
        seen.add(host.name)
        hosts.append(host)
    return hosts


def find_host(hosts: list[RemoteHost], name: str) -> RemoteHost | None:
    """Return the host with ``name`` or ``None``."""
    for h in hosts:
        if h.name == name:
            return h
    return None
