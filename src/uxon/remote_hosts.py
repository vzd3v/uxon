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

from dataclasses import dataclass, field

from uxon.duration import parse_duration_seconds


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
        interval: Optional per-host poll interval in seconds. ``None``
            means use the fleet-global ``tui_ssh_refresh_interval_seconds``.
            Accepts duration strings (``"5s"``, ``"500ms"``, ``"2m"``).
        connect_timeout: Optional per-host SSH ``ConnectTimeout`` in
            seconds. ``None`` means use the fleet-global default.
        total_timeout: Optional per-host total subprocess timeout in
            seconds. ``None`` means use the fleet-global default.
        extra_ssh_options: Tuple of extra ``-o`` (or other) tokens
            inserted into the default ssh argv before ``{ssh_alias}``.
            Ignored when ``command_template`` is set (operator owns
            the entire argv in that case).
        command_template: Optional full-argv override for the fetch
            command — replaces the default ssh template entirely.
            Placeholders from the closed set ``{ssh_alias}``,
            ``{remote_uxon}``, ``{connect_timeout}``,
            ``{ssh_control_dir}``, ``{remote_command}`` are substituted
            at call time. Used to wire kubectl-exec, docker-exec, or
            other transports.
        color: Optional Rich style spec used to paint the per-host
            block (tab text, NAME column glyph, HOST cell). When
            ``None``, the TUI auto-assigns from ``tui.color_palette``
            with adjacency skip. **Not validated against the palette**
            — operators may pin any Rich-accepted name; collisions
            with locals or peers are the operator's responsibility.
    """

    name: str
    ssh_alias: str
    description: str
    remote_uxon: str
    interval: float | None = None
    connect_timeout: float | None = None
    total_timeout: float | None = None
    extra_ssh_options: tuple[str, ...] = field(default_factory=tuple)
    command_template: tuple[str, ...] | None = None
    color: str | None = None


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

    interval = _opt_duration(raw, "interval", name)
    connect_timeout = _opt_duration(raw, "connect_timeout", name)
    total_timeout = _opt_duration(raw, "total_timeout", name)

    extra_ssh_options_raw = raw.get("extra_ssh_options", [])
    if not isinstance(extra_ssh_options_raw, list) or not all(
        isinstance(x, str) and x for x in extra_ssh_options_raw
    ):
        raise RemoteHostError(
            f"remote_hosts[{name}]: extra_ssh_options must be a list of non-empty strings"
        )
    extra_ssh_options: tuple[str, ...] = tuple(extra_ssh_options_raw)

    command_template_raw = raw.get("command_template")
    command_template: tuple[str, ...] | None
    if command_template_raw is None:
        command_template = None
    else:
        if not isinstance(command_template_raw, list) or not command_template_raw:
            raise RemoteHostError(
                f"remote_hosts[{name}]: command_template must be a non-empty list of strings"
            )
        if not all(isinstance(x, str) and x for x in command_template_raw):
            raise RemoteHostError(
                f"remote_hosts[{name}]: command_template tokens must be non-empty strings"
            )
        command_template = tuple(command_template_raw)
        # Step 6 wires the real placeholder validator. Local lazy import
        # keeps this module pure-data and avoids a circular dep with
        # remote_collector.
        try:
            from uxon.remote_collector import (  # noqa: PLC0415
                validate_command_template,
            )
        except ImportError:
            validate_command_template = None  # type: ignore[assignment]
        if validate_command_template is not None:
            try:
                validate_command_template(list(command_template))
            except ValueError as exc:
                raise RemoteHostError(f"remote_hosts[{name}]: {exc}") from exc

    color_raw = raw.get("color")
    if color_raw is None:
        color: str | None = None
    elif isinstance(color_raw, str) and color_raw.strip():
        color = color_raw.strip()
    else:
        raise RemoteHostError(f"remote_hosts[{name}]: color must be a non-empty string when set")

    # Reject unknown keys — better to fail loudly than silently ignore a
    # typo (e.g. ``ssh_alaias = "..."``) the operator expected to take
    # effect.
    known_keys = {
        "name",
        "ssh_alias",
        "description",
        "remote_uxon",
        "interval",
        "connect_timeout",
        "total_timeout",
        "extra_ssh_options",
        "command_template",
        "color",
    }
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
        interval=interval,
        connect_timeout=connect_timeout,
        total_timeout=total_timeout,
        extra_ssh_options=extra_ssh_options,
        command_template=command_template,
        color=color,
    )


def _opt_duration(raw: dict, field_name: str, host_name: str) -> float | None:
    """Parse an optional duration field. ``None``/missing → ``None``.

    Raises :class:`RemoteHostError` if the value is present but invalid
    or non-positive (zero/negative durations make no sense here).
    """
    if field_name not in raw:
        return None
    value = raw[field_name]
    if value is None:
        return None
    try:
        seconds = parse_duration_seconds(value)
    except ValueError as exc:
        raise RemoteHostError(f"remote_hosts[{host_name}]: {field_name}: {exc}") from exc
    if seconds <= 0:
        raise RemoteHostError(
            f"remote_hosts[{host_name}]: {field_name} must be positive, got {value!r}"
        )
    return seconds


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
