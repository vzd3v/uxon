"""Frozen :class:`MainData` — output of one rebuild tick.

The TUI's :class:`uxon.tui.context.TuiContext` mixes three different
kinds of state (immutable config, async-source-owned slots, rebuild
output). Stage 8 of the multi-host design splits these apart;
:class:`MainData` is the rebuild-output container.

Each call to ``on_refresh()`` produces a fresh :class:`MainData` —
fields like ``sessions``, ``server_status`` and ``cwd_short`` change
on every tick, while configuration (``enabled_agents``, callbacks,
remote-hosts registry) and async-source state (``link_health``,
``remote_snapshots``) live elsewhere.

This commit lands :class:`MainData` as a *read-only mirror* of the
rebuild-derived fields on :class:`TuiContext`; callers still go
through the live ctx. Subsequent commits (a) make ``state.main`` the
canonical store and (b) flip readers to consume ``MainData`` directly.

Note on the ``loading`` field: it is **not** stored here. The local
rebuild source always succeeds, so "no rebuild has landed yet" is a
property of the slot store ("the ``main`` slot is in its zero
state"), not of the rebuild output. Once :class:`TuiState` lands in
commit 3, ``loading`` is a derived view: ``state.main is None``.

The ``slots=True`` flag halves per-instance memory cost without
regressing pyright support (frozen + slots is well-supported in
Python 3.11+ which is the project's floor). ``msgspec.Struct`` was
considered (msgspec is already a project dep) and rejected for now —
the construction-cost difference is small relative to the 2 s
rebuild cadence; revisit if a profiler ever shows otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .context import ServerStatus, SudoCapability

if TYPE_CHECKING:
    from .context import TuiContext, TuiSession


@dataclass(frozen=True, slots=True)
class MainData:
    """Output of one local rebuild tick.

    Every field here is derived from the live system state by the
    rebuild path (today: ``cli._build_tui_context``); none come from
    an async source, none belong on :class:`uxon.tui.config.TuiConfig`.

    Sequences are tuples (not lists) so two equal :class:`MainData`
    instances compare ``==`` and can be hashed indirectly via the
    rebuild-source dispatcher's identity comparisons. Tuples also
    keep the dataclass shareable across watchers without aliasing
    accidents.
    """

    sessions: tuple[TuiSession, ...]
    other_sessions: tuple[TuiSession, ...]
    server_status: ServerStatus
    sudo_caps: SudoCapability
    scope_skipped_users: tuple[str, ...]
    cwd: str
    cwd_short: str
    new_project_root: str
    existing_projects: tuple[tuple[str, str], ...]
    total_cpu: str
    total_ram: str
    version: str
    repo_config_writable: bool

    @classmethod
    def from_context(cls, ctx: TuiContext) -> MainData:
        """Snapshot the rebuild-derived fields of ``ctx`` into a :class:`MainData`.

        Sequence types are coerced to tuples so two snapshots of the
        same logical state compare equal even when the producer used
        different list instances. The reverse direction (``MainData``
        → ``TuiContext``) is the rebuild source's job and lands in
        commit 7; commit 2 is the read-only mirror only.
        """
        return cls(
            sessions=tuple(ctx.sessions),
            other_sessions=tuple(ctx.other_sessions),
            server_status=ctx.server_status,
            sudo_caps=ctx.sudo_caps,
            scope_skipped_users=tuple(ctx.scope_skipped_users),
            cwd=ctx.cwd,
            cwd_short=ctx.cwd_short,
            new_project_root=ctx.new_project_root,
            existing_projects=tuple((name, mtime) for (name, mtime) in ctx.existing_projects),
            total_cpu=ctx.total_cpu,
            total_ram=ctx.total_ram,
            version=ctx.version,
            repo_config_writable=ctx.repo_config_writable,
        )
