# SPDX-License-Identifier: MIT
"""Session identity: naming, parsing, and allocation.

Pure session-name logic plus the rendering-facing DTOs (:class:`SessionInfo`,
:class:`TuiSession`). ``slugify`` lands here as the sole canonical copy. No
I/O: the impure session *collection* (tmux ``list-sessions``) and the
legacy-socket guardrails (which shell out) live in the infra/composition
layers.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass

from uxon.domain.authz import is_under
from uxon.domain.format import compact_time, format_cpu_pct, format_rss_kib
from uxon.errors import fail


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return slug or "workspace"


@dataclass(frozen=True)
class SessionIdentity:
    """A captured target; name is a label, never a destructive selector."""

    user: str
    name: str
    session_id: str
    created: str
    launch_nonce: str
    socket_path: str
    execution_backend: str
    execution_fingerprint: str

    @property
    def key(self) -> str:
        # Display-only records may lack native identity. Names distinguish those
        # rows without granting authority; legacy IDs include creation to avoid
        # carrying selection over to a reused ID after a server restart.
        if self.launch_nonce:
            launch = f"nonce:{self.launch_nonce}"
        elif self.session_id and self.created:
            launch = f"native:{self.session_id}/{self.created}"
        else:
            launch = f"display:{self.name}"
        return f"{self.user}/{self.execution_backend}/{self.execution_fingerprint}/{self.socket_path}/{launch}"

    def matches(self, other: SessionIdentity) -> bool:
        return all(
            getattr(self, field) == getattr(other, field)
            for field in self.__dataclass_fields__
            if field != "name"
        )

    def invalid_fields(self, *, require_nonce: bool = True) -> tuple[str, ...]:
        """Return invalid identity fields without emitting CLI diagnostics."""
        checks = (
            ("session_id", re.fullmatch(r"\$[0-9]+", self.session_id) is not None),
            ("created", re.fullmatch(r"[0-9]+", self.created) is not None),
            (
                "launch_nonce",
                (not require_nonce and not self.launch_nonce)
                or re.fullmatch(r"[A-Za-z0-9_-]{16,64}", self.launch_nonce) is not None,
            ),
            ("user", bool(self.user)),
            ("name", bool(self.name)),
            ("socket_path", self.socket_path.startswith("/")),
            ("execution_backend", bool(self.execution_backend)),
            ("execution_fingerprint", bool(self.execution_fingerprint)),
        )
        return tuple(name for name, valid in checks if not valid)

    def validate(self, *, require_nonce: bool = True) -> None:
        invalid = self.invalid_fields(require_nonce=require_nonce)
        if invalid:
            hint = (
                "; destructive actions require launch authority: drain older sessions "
                "before upgrading or remove them explicitly with tmux"
                if require_nonce and "launch_nonce" in invalid
                else ""
            )
            fail(
                f"invalid captured identity for {self.name!r} ({self.session_id or 'unknown ID'}): "
                f"missing or invalid {', '.join(invalid)}{hint}",
                1,
            )

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_wire(cls, value: object) -> SessionIdentity | None:
        """Missing identities remain visible but cannot enable a kill action."""
        fields = cls.__dataclass_fields__
        if not isinstance(value, dict) or not set(fields).issubset(value):
            return None
        if not all(isinstance(value[key], str) for key in fields):
            return None
        return cls(**{key: value[key] for key in fields})

    @classmethod
    def from_json(cls, value: str, *, require_nonce: bool = True) -> SessionIdentity:
        try:
            payload = json.loads(value)
            if not isinstance(payload, dict) or set(payload) != set(cls.__dataclass_fields__):
                raise ValueError("unexpected identity fields")
            if not all(isinstance(item, str) for item in payload.values()):
                raise ValueError("identity fields must be strings")
            result = cls(**payload)
        except (TypeError, ValueError) as exc:
            fail(f"invalid --expect-session: {exc}")
        result.validate(require_nonce=require_nonce)
        return result


@dataclass(frozen=True)
class KillResult:
    identity: SessionIdentity
    action: str
    cleanup_outcome: str = "not_run"
    error: str = ""

    @property
    def ok(self) -> bool:
        return (
            self.action in {"killed", "already_absent", "would-kill"}
            and self.cleanup_outcome != "error"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.identity.name,
            "session_id": self.identity.session_id,
            "launch_nonce": self.identity.launch_nonce,
            "user": self.identity.user,
            "action": self.action,
            "cleanup_outcome": self.cleanup_outcome,
            "error": self.error,
        }


@dataclass(frozen=True)
class BulkKillResult:
    results: tuple[KillResult, ...] = ()

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def killed_count(self) -> int:
        return sum(result.action == "killed" for result in self.results)

    @property
    def failed_count(self) -> int:
        return sum(not result.ok for result in self.results)

    @property
    def summary(self) -> str:
        absent = sum(result.action == "already_absent" for result in self.results)
        return f"Removed {self.killed_count}/{len(self.results)} sessions; already absent {absent}; failed {self.failed_count}"


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
    agent: str = ""  # underlying agent id from the verified launch record
    profile: str = ""  # launch profile id from record, or suffix display fallback
    legacy: bool = False  # True iff name uses a non-current (legacy) prefix
    tmux_session_id: str = ""
    tmux_session_created: str = ""
    launch_nonce: str = ""
    socket_path: str = ""
    launch_record_verified: bool = False
    launch_user: str = ""
    execution_backend: str = ""
    execution_fingerprint: str = ""
    runtime: str = "direct"
    runtime_kind: str = "direct"
    runtime_fingerprint: str = ""
    runtime_id: str = ""
    runtime_epoch: str = ""
    # Diagnostic-only tmux environment marker. It is never used as runtime
    # authority; it only lets destructive paths explain why teardown was skipped
    # for an older or unmanaged workload session.
    runtime_marker: str = ""
    # Runtime resource/context comes from the verified launch record.
    # ``runtime_cgroup`` identifies the cgroup telemetry reads to enumerate
    # workload PIDs. Direct sessions have no workload resource or cgroup.
    runtime_resource: str = ""
    runtime_dir: str = ""
    project_slug: str = ""
    runtime_cgroup: str = ""
    # True iff this session's workload resource is not running
    # (empty/absent ``cgroup.procs`` confirmed by ``ready_command``). Renders a
    # distinct "runtime down" indicator rather than a silent idle 0/—.
    runtime_down: bool = False
    # Retained diagnostics are not running sessions. A mixed session still has
    # live panes; attaching may select one rather than its dead active pane.
    exited: bool = False
    has_diagnostics: bool = False
    active_pane_id: str = ""
    active_pane_dead: bool = False
    active_exit_status: int | None = None
    live_pane_id: str = ""
    dismissed_panes: tuple[str, ...] = ()

    @property
    def identity(self) -> SessionIdentity:
        return SessionIdentity(
            self.user,
            self.name,
            self.tmux_session_id,
            self.tmux_session_created,
            self.launch_nonce,
            self.socket_path,
            self.execution_backend,
            self.execution_fingerprint,
        )


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
    # Empty authority fields represent an unmanaged session.
    stem: str = ""  # bare project stem, e.g. "myproject"
    agent: str = ""  # underlying agent id when known
    profile: str = ""  # launch profile id when known
    legacy: bool = False  # True when the name matches a configured legacy prefix
    # Raw ISO 8601 timestamps preserved alongside the pre-formatted
    # display strings so dashboard sort by ``new`` / ``last`` ranks
    # local rows correctly. Empty string mirrors the wire schema
    # convention for "missing".
    created_iso: str = ""
    last_attached_iso: str = ""
    # True when the session's workload resource is not running — the dashboard
    # renders a distinct "runtime down" indicator in the
    # cpu/ram cells rather than a silent idle 0/—.
    runtime_down: bool = False
    exited: bool = False
    has_diagnostics: bool = False
    identity: SessionIdentity | None = None
    active_exit_status: int | None = None


def session_stem_for_path(target_dir: str) -> str:
    return slugify(os.path.basename(target_dir))


def session_stem_for_worktree(repo_root: str, branch: str) -> str:
    # Always repo-qualified, even when the branch slug equals the repo slug
    # Collapsing to the bare repo slug would make a worktree on a
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
    profile_id: str,
    compatibility_root: str,
    sessions: list[SessionInfo],
    *,
    prefix: str = "uxon-",
    legacy_prefixes: tuple[str, ...] = (),
    require_verified: bool = True,
) -> list[SessionInfo]:
    matches: list[SessionInfo] = []
    for session in sessions:
        if require_verified and not session.launch_record_verified:
            continue
        idx = parse_plain_session_index(
            session.name, stem, profile_id, prefix=prefix, legacy_prefixes=legacy_prefixes
        )
        if idx is None:
            continue
        if not session_path_compatible(session.active_path, compatibility_root):
            fail(
                "session conflict: "
                f"{session.name} already points to {session.active_path or '<unknown>'}, "
                f"not under {compatibility_root}"
            )
        if not session.exited:
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
    # Both sides are already target-boundary paths. Host-side resolution here
    # would reinterpret them through the controller's mount namespace.
    active = os.path.normpath(active_path)
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
        stem, fallback_profile, _idx, legacy = parsed
    else:
        stem, fallback_profile, legacy = s.name, "unknown", False
    profile = s.profile or fallback_profile
    agent = s.agent or "-"
    return TuiSession(
        name=s.name,
        short=short,
        attached=s.attached == "1",
        pid=str(s.active_pid) if s.active_pid is not None else "-",
        cpu=format_cpu_pct(s.cpu_pct),
        ram=format_rss_kib(s.rss_kib),
        created=compact_time(s.created),
        last_activity=compact_time(s.last_attached),
        # For an isolated-runtime session the active pane command is the runtime
        # client (``docker``/``sh``), not the agent — substitute the resolved
        # agent id so search-by-cmd matches the agent. Gated on the
        # runtime-resource marker; direct-runtime sessions are unchanged.
        cmd=(agent if s.runtime_resource else s.active_cmd) or "-",
        path=s.active_path or "-",
        user=s.user,
        stem=stem,
        agent=agent,
        profile=profile,
        legacy=legacy,
        created_iso=s.created,
        last_attached_iso=s.last_attached,
        runtime_down=s.runtime_down,
        exited=s.exited,
        active_exit_status=s.active_exit_status,
        has_diagnostics=s.has_diagnostics,
        identity=s.identity,
    )
