"""Frozen :class:`MainData` — output of one rebuild tick.

Each call to ``on_refresh()`` produces a fresh :class:`MainData` —
fields like ``sessions``, ``server_status`` and ``cwd_short`` change
on every tick, while configuration (``enabled_agents``, callbacks,
remote-hosts registry) and async-source state (``link_health``,
``remote_snapshots``) live elsewhere.

The ``loading`` field is **not** stored here: the local rebuild
source always succeeds, so "no rebuild has landed yet" is a property
of the slot store (``state.main is None``), not of the rebuild output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .context import ServerStatus, SudoCapability

if TYPE_CHECKING:
    from uxon.probes import HostStatsResult

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
    host_stats: HostStatsResult | None = None

    @classmethod
    def from_context(cls, ctx: TuiContext) -> MainData:
        """Snapshot the rebuild-derived fields of ``ctx`` into a :class:`MainData`.

        Sequence types are coerced to tuples so two snapshots of the
        same logical state compare equal even when the producer used
        different list instances.
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
            host_stats=getattr(ctx, "host_stats", None),
        )
