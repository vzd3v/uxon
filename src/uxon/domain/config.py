# SPDX-License-Identifier: MIT
"""Configuration schema, defaults, and pure config validators.

``Config`` is the parsed, typed config record consumed across uxon.
``DEFAULT_CONFIG`` / ``RECOMMENDED_TMUX_OPTIONS`` are the seed data.
``merge_config`` and the ``validate_*`` helpers are pure (the validators
call :func:`uxon.errors.fail`, the leaf exit primitive). The impure
config *loader* (TOML reading, project-config discovery) lives in the
infra layer and produces a :class:`Config`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from uxon.domain.agents import AgentSpec, default_agent_catalog
from uxon.domain.container import ContainerConfig
from uxon.domain.git_profiles import GitRemoteProfile
from uxon.errors import fail

# Recommended uxon-managed tmux options (3.5.0). This is the SINGLE source of
# the recommended option set: it seeds ``DEFAULT_CONFIG["tmux"]`` (scaffolded
# but DORMANT — ``manage_options`` is off by default, so nothing is applied
# until the operator opts in by setting ``manage_options = true``) AND scaffolds
# ``config/config.example.toml`` plus the configuration docs. Shipping the set
# as ready-to-use tables means flipping the single toggle yields sane tmux
# behaviour with no further config. Split across the three tmux scopes:
#   options               -> set -g   (global session options)
#   server_options        -> set -s   (server options)
#   append_server_options -> set -as  (append to a server option's list)
RECOMMENDED_TMUX_OPTIONS: dict[str, dict[str, object]] = {
    "options": {"mouse": "on", "allow-passthrough": "on"},
    "server_options": {"extended-keys": "on"},
    "append_server_options": {"terminal-features": "xterm*:extkeys"},
}


DEFAULT_CONFIG: dict[str, Any] = {
    "runtime_user": "",
    "default_launch_mode": "caller",
    "enable_all_users_list": False,
    "launch_user_by_caller": {},
    "session_users": [],
    # Empty by default = "trust the OS write-access check" — `uxon run`
    # / `uxon new -w` launch wherever the launch user can write
    # (matching the TUI's "new session in current folder" gate). Set
    # this to a non-empty list to switch to strict-whitelist mode:
    # `uxon run` / `uxon new -w` then refuse anything outside the
    # listed paths, including $HOME. `uxon new` (creating a new
    # project directory) always uses strict-whitelist semantics and
    # requires a non-empty allowed_roots.
    "allowed_roots": [],
    "session_prefix": "uxon-",
    # Empty by default. Operators upgrading from a host that ran a
    # different ``session_prefix`` add the previous value here to keep
    # already-running sessions reachable; new installs leave it empty.
    "legacy_session_prefixes": [],
    # ``enabled`` is empty by default — auto-mode: uxon picks up
    # whichever agents are actually installed for the launch user.
    # Set it to a non-empty list (e.g. ``["claude", "codex"]``) to
    # switch to strict-whitelist mode. ``default`` may also be empty;
    # consumers fall back to the first available agent at launch time.
    "agents": {
        "enabled": [],
        "default": "",
    },
    "new_project_root": str(Path.home() / "projects"),
    "repeat_noninteractive_mode": "fail",
    # Worktree layout + base ref (3.5.0). Empty worktree_root → default
    # <repo>/.uxon/worktrees/<slug>. worktree_base picks where a new
    # branch is based: "local" (default, no fetch) off local origin/HEAD
    # else local HEAD; "remote" fetches origin first (claude-like).
    "worktree_root": "",
    "worktree_base": "local",
    "tmux_socket_template": "/tmp/uxon-{user}.sock",
    "tui_refresh_interval_seconds": 2.0,
    "tui_ssh_refresh_interval_seconds": 10.0,
    # Dashboard column layout. ``columns`` is a list of column ids (see
    # :data:`uxon.tui.dashboard.KNOWN_COLUMN_IDS`); empty / absent means
    # "use the registry defaults". Sort is a hard contract owned by the
    # selector (locals → cfg-order remotes → recency), not configurable.
    "tui": {
        "table": {
            "columns": [],
            "default_view": "flat",
        },
        "search": {"fields": ["name", "user"]},
        "color_palette": ["cyan", "blue"],
    },
    "local_host": {"color": "green"},
    # Stage 5: ssh transport hardening.
    # ``ssh_multiplex = "auto"`` adds ControlMaster/Path/Persist to the
    # default fetch template (warm tick: 5-20 ms vs cold 200-500 ms).
    # ``"off"`` strips them — for environments that prohibit the socket.
    "ssh_multiplex": "auto",
    # ControlPersist (seconds) for the SSH master connection. Must be a
    # positive integer; disable multiplexing via ``ssh_multiplex = "off"``
    # rather than zeroing this out.
    "ssh_control_persist_seconds": 300,
    # ``fetch_concurrency`` caps concurrent SSH fetch workers fleet-wide
    # so a 50-host post-outage stampede doesn't exhaust the FD ulimit.
    # Stage 5 step 7 wires the semaphore.
    "fetch_concurrency": 16,
    "git_create_enabled": False,
    "default_git_remote_profile": "",
    "git_remote_profiles": [],
    "remote_hosts": [],
    # Application-level audit channel.  ``enabled`` is the only kill-switch
    # (no env-var override).  ``syslog_facility`` is consulted only on the
    # ``/dev/log`` fallback path; journald native carries its own metadata.
    "audit": {"enabled": True, "syslog_facility": "user"},
    # uxon-managed tmux options (3.5.0). OFF BY DEFAULT: the three tables map
    # 1:1 to tmux scopes (``-g`` / ``-s`` / ``-as``) and are rendered into the
    # launch invocation by :func:`_tmux_set_chain` — but only once the operator
    # opts in with ``manage_options = true``. Seeded from
    # ``RECOMMENDED_TMUX_OPTIONS`` so opting in is a one-line toggle that yields
    # sane tmux behaviour; override the ``[tmux]`` tables to customise.
    # ``dict(...)`` copies keep the shared constant immutable from per-load
    # mutation.
    "tmux": {
        "manage_options": False,
        "options": dict(RECOMMENDED_TMUX_OPTIONS["options"]),
        "server_options": dict(RECOMMENDED_TMUX_OPTIONS["server_options"]),
        "append_server_options": dict(RECOMMENDED_TMUX_OPTIONS["append_server_options"]),
    },
    # Container-agnostic agent launch (off by default). When
    # ``enabled = true`` uxon prefixes the agent command with the operator's
    # opaque ``exec_template`` so the agent runs inside a container; every
    # other key is an argv/policy operator-only value. ``.uxon.toml`` may set
    # only ``name`` / ``path_map`` (data). See ``domain/container.py``.
    "container": {
        "enabled": False,
        "name_template": "",
        "exec_template": [],
        "is_running_cmd": [],
        "exists_cmd": [],
        "start_template": [],
        "create_template": [],
        "on_missing": "off",
        "on_missing_mode": "prompt",
    },
}


@dataclass
class Config:
    runtime_user: str
    default_launch_mode: str
    enable_all_users_list: bool
    launch_user_by_caller: dict[str, str]
    session_users: list[str]
    allowed_roots: list[str]
    session_prefix: str
    legacy_session_prefixes: tuple[str, ...]
    enabled_agents: tuple[str, ...]
    default_agent: str
    new_project_root: str
    repeat_noninteractive_mode: str
    tmux_socket_template: str
    tui_refresh_interval_seconds: float
    git_create_enabled: bool
    default_git_remote_profile: str
    git_remote_profiles: list[GitRemoteProfile]  # parsed once in load_config
    tui_ssh_refresh_interval_seconds: float = 10.0
    remote_hosts: list = field(
        default_factory=list
    )  # list[RemoteHost] — parsed once in load_config
    ssh_multiplex: str = "auto"  # "auto" | "off"
    ssh_control_persist_seconds: int = 300
    fetch_concurrency: int = 16
    audit_enabled: bool = True
    audit_syslog_facility: str = "user"
    # Merged agent catalog (shipped ``DEFAULT_AGENT_CATALOG`` ⊕ operator
    # ``[agents.<id>]`` tables), built once in ``load_config``. Replaces the
    # old ``agent_default_args`` map — binaries, default_args, install hints,
    # version probe args, and permission modes all live here as data.
    agents: dict[str, AgentSpec] = field(default_factory=default_agent_catalog)
    # ``None`` is the load-time signal "use REGISTRY defaults". An empty
    # tuple would mean "operator explicitly cleared the column list" —
    # that's not a state we want to expose distinctly, so absent / ``[]``
    # in TOML both collapse to ``None`` here. ``build_active_columns``
    # consumes this contract directly.
    tui_table_columns: tuple[str, ...] | None = None
    tui_table_default_view: Literal["by_host", "flat"] = "flat"
    tui_search_fields: tuple[str, ...] = ("name", "user")
    tui_color_palette: tuple[str, ...] = ("cyan", "blue")
    local_host_color: str = "green"
    worktree_root: str = ""
    worktree_base: str = "local"
    # uxon-managed tmux options (3.5.0). OFF BY DEFAULT, seeded from
    # ``RECOMMENDED_TMUX_OPTIONS`` to match ``DEFAULT_CONFIG`` — the three dicts
    # hold raw ``key -> value`` pairs per tmux scope (``-g`` / ``-s`` / ``-as``).
    # Values are bool/int/str and rendered verbatim by :func:`_tmux_set_chain`
    # (no name/value validation — D4), but only once ``manage_options = true``.
    tmux_manage_options: bool = False
    tmux_options: dict = field(default_factory=lambda: dict(RECOMMENDED_TMUX_OPTIONS["options"]))
    tmux_server_options: dict = field(
        default_factory=lambda: dict(RECOMMENDED_TMUX_OPTIONS["server_options"])
    )
    tmux_append_server_options: dict = field(
        default_factory=lambda: dict(RECOMMENDED_TMUX_OPTIONS["append_server_options"])
    )
    # Container-agnostic launch (off by default). Parsed + validated once in
    # ``load_config``; the disabled default is byte-for-byte the pre-container
    # behaviour (no exec wrap).
    container: ContainerConfig = field(default_factory=ContainerConfig)


def merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        out[key] = value
    return out


def validate_repeat_mode(value: str, source: str) -> str:
    mode = value.strip().lower()
    if mode not in {"fail", "attach", "new"}:
        fail(f"invalid {source}: {value!r} (expected 'fail', 'attach', or 'new')")
    return mode


def validate_worktree_base(value: str, source: str) -> str:
    mode = value.strip().lower()
    if mode not in {"local", "remote"}:
        fail(f"invalid {source}: {value!r} (expected 'local' or 'remote')")
    return mode
