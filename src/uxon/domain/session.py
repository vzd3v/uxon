# SPDX-License-Identifier: MIT
"""Session identity: naming, parsing, and allocation.

Pure session-name logic plus the rendering-facing DTOs (:class:`SessionInfo`,
:class:`TuiSession`). ``slugify`` lands here as the sole canonical copy. No
I/O: the impure session *collection* (tmux ``list-sessions``) and the
legacy-socket guardrails (which shell out) live in the infra/composition
layers.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from uxon.domain.authz import canonical, is_under
from uxon.domain.format import compact_time, format_cpu_pct, format_rss_kib
from uxon.errors import fail


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return slug or "workspace"


@dataclass
class SessionInfo:
    user: str
    name: str
    attached: str
    windows: str
    created: str
    last_attached: str
    pane_pids: tuple[int, ...]
    active_pid: int | None
    active_cmd: str
    active_path: str
    cpu_pct: float = 0.0
    rss_kib: int = 0
    agent: str = "claude"  # underlying agent id when known
    profile: str = ""  # launch profile id parsed from the session name
    legacy: bool = False  # True iff name uses a non-current (legacy) prefix
    # Container telemetry markers, read from the session env via the existing
    # ``list-sessions -F`` batch (``#{E:VAR}`` expands to "" for an unset var,
    # i.e. a non-container session). ``container`` is the bare container name
    # (the ``UXON_CONTAINER`` marker — non-empty iff the session is
    # containerized); ``container_cgroup`` is the host-side cgroup path
    # (``UXON_CONTAINER_CGROUP``) telemetry reads to enumerate in-container PIDs.
    # Both default to "" so a non-container session is byte-for-byte unchanged.
    container: str = ""
    container_cgroup: str = ""
    # True iff this session is containerized but its container is not running
    # (empty/absent ``cgroup.procs`` confirmed by ``is_running_cmd``). Renders a
    # distinct "container down" indicator rather than a silent idle 0/— (AC-P1.8).
    container_down: bool = False


@dataclass
class TuiSession:
    """Flattened session data for TUI rendering (decoupled from uxon internals)."""

    name: str
    short: str
    attached: bool
    pid: str
    cpu: str
    ram: str
    created: str
    last_activity: str
    cmd: str
    path: str
    user: str
    # Multi-agent fields (default to backward-compatible values).
    stem: str = ""  # bare project stem, e.g. "myproject"
    agent: str = "claude"  # underlying agent id when known
    profile: str = ""  # launch profile id parsed from the session name
    legacy: bool = False  # True when parsed from old cc-<stem> naming
    # Raw ISO 8601 timestamps preserved alongside the pre-formatted
    # display strings so dashboard sort by ``new`` / ``last`` ranks
    # local rows correctly. Empty string mirrors the wire schema
    # convention for "missing".
    created_iso: str = ""
    last_attached_iso: str = ""
    # True when the session is containerized but its container is not running —
    # the dashboard renders a distinct "container down" indicator in the
    # cpu/ram cells rather than a silent idle 0/— (AC-P1.8).
    container_down: bool = False


def session_stem_for_path(target_dir: str) -> str:
    return slugify(os.path.basename(target_dir))


def session_stem_for_worktree(repo_root: str, branch: str) -> str:
    # Always repo-qualified, even when the branch slug equals the repo slug
    # (§2.5). Collapsing to the bare repo slug would make a worktree on a
    # branch named like its repo share the primary tree's stem
    # (``session_stem_for_path``) and hard-fail on "session conflict".
    repo_slug = slugify(os.path.basename(repo_root))
    workspace_slug = slugify(branch)
    return f"{repo_slug}-{workspace_slug}"


def _modern_re(prefix: str) -> re.Pattern[str]:
    return re.compile(
        rf"^{re.escape(prefix)}(?P<stem>.+?)@(?P<profile>[a-z][a-z0-9_]*)(?:-(?P<index>\d+))?$"
    )


def parse_session_name(
    name: str,
    *,
    prefix: str = "uxon-",
    legacy_prefixes: tuple[str, ...] = (),
) -> tuple[str, str, int, bool] | None:
    """Return (stem, profile, index, legacy) or None if the name is not ours.

    Recognises the current ``prefix`` plus any ``legacy_prefixes`` in the
    ``<prefix><stem>@<profile>[-N]`` shape. ``legacy=True`` is returned for
    names matched via a non-current prefix.
    """
    for p in (prefix, *legacy_prefixes):
        m = _modern_re(p).match(name)
        if m:
            idx = int(m.group("index")) if m.group("index") else 1
            return m.group("stem"), m.group("profile"), idx, p != prefix
    return None


def candidate_session_name(stem: str, index: int, profile: str, *, prefix: str = "uxon-") -> str:
    base = f"{prefix}{stem}@{profile}"
    if index <= 1:
        return base
    return f"{base}-{index}"


def parse_plain_session_index(
    name: str,
    stem: str,
    profile: str,
    *,
    prefix: str = "uxon-",
    legacy_prefixes: tuple[str, ...] = (),
) -> int | None:
    parsed = parse_session_name(name, prefix=prefix, legacy_prefixes=legacy_prefixes)
    if parsed is None:
        return None
    p_stem, p_profile, p_index, _legacy = parsed
    if p_stem != stem or p_profile != profile:
        return None
    return p_index


def compatible_indexed_sessions(
    stem: str,
    profile: str,
    compatibility_root: str,
    sessions: list[SessionInfo],
    *,
    prefix: str = "uxon-",
    legacy_prefixes: tuple[str, ...] = (),
) -> list[SessionInfo]:
    matches: list[SessionInfo] = []
    for session in sessions:
        idx = parse_plain_session_index(
            session.name, stem, profile, prefix=prefix, legacy_prefixes=legacy_prefixes
        )
        if idx is None:
            continue
        if not session_path_compatible(session.active_path, compatibility_root):
            fail(
                "session conflict: "
                f"{session.name} already points to {session.active_path or '<unknown>'}, "
                f"not under {compatibility_root}"
            )
        matches.append(session)
    return matches


def choose_attach_session(
    existing: list[SessionInfo],
    stem: str,
    profile: str,
    *,
    prefix: str = "uxon-",
    legacy_prefixes: tuple[str, ...] = (),
) -> SessionInfo:
    if not existing:
        raise ValueError("expected at least one existing session")
    base_name = candidate_session_name(stem, 1, profile, prefix=prefix)
    attached = [s for s in existing if s.attached == "1"]
    for bucket in (attached, existing):
        for session in bucket:
            if session.name == base_name:
                return session
    return min(
        existing,
        key=lambda session: (
            parse_plain_session_index(
                session.name, stem, profile, prefix=prefix, legacy_prefixes=legacy_prefixes
            )
            or 9999
        ),
    )


def allocate_session_name(
    stem: str,
    profile: str,
    compatibility_root: str,
    sessions: list[SessionInfo],
    *,
    prefix: str = "uxon-",
) -> str:
    exact_base = candidate_session_name(stem, 1, profile, prefix=prefix)
    exact_base_hits = [s for s in sessions if s.name == exact_base]
    if exact_base_hits and not session_path_compatible(
        exact_base_hits[0].active_path, compatibility_root
    ):
        fail(
            "session conflict: "
            f"{exact_base} already points to {exact_base_hits[0].active_path or '<unknown>'}, "
            f"not under {compatibility_root}"
        )

    index = 1
    while True:
        candidate = candidate_session_name(stem, index, profile, prefix=prefix)
        existing = [s for s in sessions if s.name == candidate]
        if not existing:
            return candidate
        if not session_path_compatible(existing[0].active_path, compatibility_root):
            fail(
                "session conflict: "
                f"{candidate} already points to {existing[0].active_path or '<unknown>'}, "
                f"not under {compatibility_root}"
            )
        index += 1


def session_path_compatible(active_path: str, repo_root: str) -> bool:
    if not active_path:
        return True
    active = canonical(active_path)
    return is_under(active, repo_root)


def to_tui_session(
    s: SessionInfo, prefix: str, legacy_prefixes: tuple[str, ...] = ()
) -> TuiSession:
    short = s.name[len(prefix) :] if s.name.startswith(prefix) else s.name
    for lp in legacy_prefixes:
        if s.name.startswith(lp):
            short = s.name[len(lp) :]
            break
    parsed = parse_session_name(s.name, prefix=prefix, legacy_prefixes=legacy_prefixes)
    if parsed is not None:
        stem, profile, _idx, legacy = parsed
    else:
        stem, profile, legacy = s.name, "unknown", False
    agent = s.agent or profile
    return TuiSession(
        name=s.name,
        short=short,
        attached=s.attached == "1",
        pid=str(s.active_pid) if s.active_pid is not None else "-",
        cpu=format_cpu_pct(s.cpu_pct),
        ram=format_rss_kib(s.rss_kib),
        created=compact_time(s.created),
        last_activity=compact_time(s.last_attached),
        # For a containerized session the active pane command is the runtime
        # client (``docker``/``sh``), not the agent — substitute the resolved
        # agent id (AC-P1.4) so search-by-cmd matches the agent. Gated on the
        # ``UXON_CONTAINER`` marker; non-container sessions are unchanged.
        cmd=(agent if s.container else s.active_cmd) or "-",
        path=s.active_path or "-",
        user=s.user,
        stem=stem,
        agent=agent,
        profile=profile,
        legacy=legacy,
        created_iso=s.created,
        last_attached_iso=s.last_attached,
        container_down=s.container_down,
    )
