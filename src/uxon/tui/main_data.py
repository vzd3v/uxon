"""Frozen local snapshots published by independent dashboard collectors.

``MainData`` combines the latest accepted session, project and host data.
Readiness belongs to the corresponding context fields, so project or host
results can be published while sessions are still loading.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from uxon.domain.status import ServerStatus
from uxon.domain.sudo import SudoCapability

if TYPE_CHECKING:
    from uxon.domain.session import TuiSession
    from uxon.infra.probes import HostStatsResult

    from .context import TuiContext


@dataclass(frozen=True, slots=True)
class ProjectsData:
    root: str
    projects: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class HostData:
    server_status: ServerStatus
    host_stats: HostStatsResult | None


@dataclass(frozen=True, slots=True)
class MainData:
    """Output of one local rebuild tick.

    The source dispatcher merges independent local results into this snapshot;
    these data fields do not belong on :class:`uxon.tui.config.TuiConfig`.

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
            host_stats=getattr(ctx, "host_stats", None),
        )
