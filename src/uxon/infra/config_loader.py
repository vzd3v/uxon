# SPDX-License-Identifier: MIT
"""Config layering + TOML loading: produce a :class:`uxon.domain.Config`.

Walks the repo + project ``.uxon.toml`` layers, validates every field,
and returns the immutable domain ``Config``. Impure: reads the
filesystem (TOML files), ``os.environ`` (demo mode), and composes the
git-profile / remote-host / demo adapters.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from uxon.domain.agents import DEFAULT_AGENT_CATALOG, AgentSpec, PermissionMode
from uxon.domain.authz import canonical, is_under
from uxon.domain.config import (
    DEFAULT_CONFIG,
    Config,
    merge_config,
    validate_repeat_mode,
    validate_worktree_base,
)
from uxon.domain.container import (
    ContainerConfig,
    validate_container_name,
    validate_path_map,
)
from uxon.errors import fail
from uxon.infra import demo, events, version_probe

# Agent ids must match the session-name scanner charset (``session.py``)
# and be tmux-safe (no ``:``/``.``). Validated on the merged catalog so a
# custom operator-declared id is rejected here, not at launch.
_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if tomllib is None:
        fail("python tomllib is unavailable on this host", 1)
    with path.open("rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            # Convert the malformed-TOML escape path into the same
            # ``SystemExit`` shape every other config error already takes,
            # so ``main()``'s ``try/except SystemExit`` can audit it.
            fail(f"invalid TOML in {path}: {exc}", 1)
    if not isinstance(data, dict):
        return {}
    return data


def normalize_user_list(values: list[str]) -> list[str]:
    users: list[str] = []
    seen: set[str] = set()
    for value in values:
        user = str(value).strip()
        if not user or user in seen:
            continue
        seen.add(user)
        users.append(user)
    return users


def find_project_config(cwd: str, allowed_roots: list[str]) -> Path | None:
    """Walk up from ``cwd`` looking for a ``.uxon.toml``.

    With a non-empty ``allowed_roots`` whitelist, the file is only
    accepted when it lives under one of the listed roots — same strict-
    whitelist semantics as ``is_launch_target_allowed``. With an
    empty list the whitelist is bypassed and the first ``.uxon.toml``
    found while walking up is returned (matches the "empty = no
    restriction beyond the launch user's filesystem" policy used
    everywhere else for ``allowed_roots``).
    """
    cur = Path(cwd)
    allowed = [str(Path(p)) for p in allowed_roots]
    for parent in [cur] + list(cur.parents):
        candidate = parent / ".uxon.toml"
        try:
            exists = candidate.exists()
        except PermissionError:
            continue
        if not exists:
            continue
        if not allowed:
            return candidate
        for root in allowed:
            if is_under(str(parent), root):
                return candidate
        return None
    return None


def repo_config_path() -> Path:
    return version_probe.repo_root() / "config" / "config.toml"


# Project-layer (``.uxon.toml``) keys that survive the deny-by-default
# allowlist. ``agents``/``container`` are descended into leaf-by-leaf
# (only the listed leaves are merged onto the running config); ``tui`` is
# allowed whole. Everything else — ``allowed_roots``,
# ``launch_user_by_caller``, ``local_host``, every ``[agents.<id>]`` argv
# sub-table, every operator-only ``[container]`` key — is dropped.
#
# This is the security spine: a ``.uxon.toml`` discovered by walking up from
# ``cwd`` is *untrusted* (a cloned repo can carry one), so it may never inject
# argv or policy that ``uxon`` then executes under ``sudo -iu``. The operator
# ``config/config.toml`` layer is unaffected — it keeps full power.
_PROJECT_AGENTS_LEAVES = ("default", "enabled")
# Project-allowed ``[container]`` leaves: pure data only. ``name``/``path_map``
# never become argv that uxon couldn't already see — the name is re-validated
# against the safe charset and ``path_map`` against the absolute/``..``-free
# rules in ``load_config`` (so a kept project leaf is never trusted
# unvalidated). Every executed/policy key (``exec_template``,
# ``create_template``, ``on_missing``, ``enabled``, …) stays operator-only and
# is dropped from the project layer.
_PROJECT_CONTAINER_LEAVES: tuple[str, ...] = ("name", "path_map")


def project_allowlist(project: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Rewrite an untrusted ``.uxon.toml`` dict to only its allowlisted keys.

    Built as a **fresh empty dict** — never a copy of ``project`` with keys
    deleted — so any unlisted top-level key (``allowed_roots``,
    ``launch_user_by_caller``, ``local_host``, raw ``[agents.<id>]`` argv,
    operator-only ``[container]`` keys) is absent by construction.

    ``agents``/``container`` are leaf-granular: their allowed leaves are
    merged onto ``base[...]`` (the running default ⊕ operator config) so
    operator-defined ``[agents.<id>]`` sub-tables survive while the
    project's ``[agents.<id>]`` argv is dropped. A non-dict value for any of
    these tables (a malformed ``agents = "x"``) is treated as absent and
    dropped — never iterated — so it cannot raise mid-merge.

    ``tui`` is pure UI data and allowed whole.
    """
    filtered: dict[str, Any] = {}

    def _merge_leaves(table_key: str, leaves: tuple[str, ...]) -> None:
        sub = project.get(table_key)
        if not isinstance(sub, dict):
            if table_key in project:
                events.debug("config", reason="project_layer_dropped_nondict", key=table_key)
            return
        base_sub = base.get(table_key)
        running = dict(base_sub) if isinstance(base_sub, dict) else {}
        kept = {k: v for k, v in sub.items() if k in leaves}
        for dropped in (k for k in sub if k not in leaves):
            events.debug("config", reason="project_layer_dropped_key", key=f"{table_key}.{dropped}")
        if kept:
            filtered[table_key] = {**running, **kept}

    _merge_leaves("agents", _PROJECT_AGENTS_LEAVES)
    _merge_leaves("container", _PROJECT_CONTAINER_LEAVES)
    tui = project.get("tui")
    if isinstance(tui, dict):
        filtered["tui"] = tui
    elif "tui" in project:
        events.debug("config", reason="project_layer_dropped_nondict", key="tui")
    for dropped in (k for k in project if k not in ("agents", "container", "tui")):
        events.debug("config", reason="project_layer_dropped_key", key=dropped)
    return filtered


def resolve_config_layers(cwd: str) -> tuple[dict[str, Any], list[Path]]:
    merged = dict(DEFAULT_CONFIG)
    sources: list[Path] = []
    repo_cfg = repo_config_path()
    if repo_cfg.exists():
        sources.append(repo_cfg)
    # Operator layer: full power, merged verbatim.
    merged = merge_config(merged, load_toml(repo_cfg))
    seed_allowed = [canonical(p) for p in merged.get("allowed_roots", [])]
    proj_cfg = find_project_config(cwd, seed_allowed)
    if proj_cfg is not None:
        sources.append(proj_cfg)
        # Project layer: untrusted — filter through the deny-by-default
        # allowlist BEFORE merging (rewrite the override, never post-process
        # ``merged``), so unlisted keys never reach the running config.
        filtered = project_allowlist(load_toml(proj_cfg), merged)
        merged = merge_config(merged, filtered)
    return merged, sources


def _parse_modes(aid: str, raw_modes: Any) -> tuple[PermissionMode, ...]:
    """Parse a ``[[agents.<id>.mode]]`` array-of-tables into PermissionModes.

    First entry is the default mode. Each table: required ``id``, optional
    ``label`` (defaults to ``id``), optional ``flags`` (list of strings),
    optional ``dangerous`` (bool). REPLACE-not-merge: the caller only calls
    this when at least one mode table is present.
    """
    if not isinstance(raw_modes, list) or not all(isinstance(m, dict) for m in raw_modes):
        fail(f"'agents.{aid}.mode' must be an array of tables")
    modes: list[PermissionMode] = []
    for m in raw_modes:
        mid = m.get("id")
        if not isinstance(mid, str) or not mid:
            fail(f"'agents.{aid}.mode' entry requires a non-empty string 'id'")
        label = m.get("label", "")
        if not isinstance(label, str):
            fail(f"'agents.{aid}.mode.{mid}.label' must be a string")
        flags_raw = m.get("flags", [])
        if not isinstance(flags_raw, list) or not all(isinstance(f, str) for f in flags_raw):
            fail(f"'agents.{aid}.mode.{mid}.flags' must be a list of strings")
        modes.append(
            PermissionMode(
                id=mid,
                label=label,
                flags=tuple(flags_raw),
                dangerous=bool(m.get("dangerous", False)),
            )
        )
    if not modes:
        fail(f"'agents.{aid}' supplies an empty mode list; omit it to inherit defaults")
    return tuple(modes)


def build_agent_catalog(agents_tbl: dict[str, Any]) -> dict[str, AgentSpec]:
    """Merge operator ``[agents.<id>]`` tables over ``DEFAULT_AGENT_CATALOG``.

    Iterates the UNION of default-catalog ids and config-supplied ids (so a
    custom agent is not dropped). Per-agent scalar/list fields merge
    field-by-field over the default for that id; a custom id with no default
    gets ``binary`` = id, ``version_args = ("--version",)``,
    ``default_args = ()``, ``install_hint = ""``. The ``[[mode]]`` list is
    REPLACE-not-merge: any supplied mode table wholly replaces that id's
    default modes; none supplied inherits the defaults.

    The validators here run on the *expanded/merged* id set so a hostile or
    typo'd custom id fails ``load_config`` naming the offender (AC-A6).
    """
    config_ids = [
        k for k, v in agents_tbl.items() if k not in ("enabled", "default") and isinstance(v, dict)
    ]
    # Non-dict ``[agents.<id>]`` (e.g. ``claude = 5``) is a config error.
    for k, v in agents_tbl.items():
        if k in ("enabled", "default"):
            continue
        if not isinstance(v, dict):
            fail(f"'agents.{k}' must be a TOML table")
    union_ids: list[str] = list(DEFAULT_AGENT_CATALOG)
    for aid in config_ids:
        if aid not in union_ids:
            union_ids.append(aid)

    catalog: dict[str, AgentSpec] = {}
    for aid in union_ids:
        if not _AGENT_ID_RE.match(aid) or ":" in aid or "." in aid:
            fail(
                f"invalid agent id {aid!r}: must match [a-z][a-z0-9_]* and "
                "contain no ':' or '.' (tmux-safe)"
            )
        default_spec = DEFAULT_AGENT_CATALOG.get(aid)
        sub = agents_tbl.get(aid, {})
        if not isinstance(sub, dict):
            fail(f"'agents.{aid}' must be a TOML table")

        binary = sub.get("binary")
        if binary is None:
            binary = default_spec.binary if default_spec else aid
        elif not isinstance(binary, str) or not binary:
            fail(f"'agents.{aid}.binary' must be a non-empty string")

        if "version_args" in sub:
            va_raw = sub["version_args"]
            if not isinstance(va_raw, list) or not all(isinstance(x, str) for x in va_raw):
                fail(f"'agents.{aid}.version_args' must be a list of strings")
            version_args = tuple(va_raw)
        else:
            version_args = default_spec.version_args if default_spec else ("--version",)

        if "install_hint" in sub:
            hint = sub["install_hint"]
            if not isinstance(hint, str):
                fail(f"'agents.{aid}.install_hint' must be a string")
            install_hint = hint
        else:
            install_hint = default_spec.install_hint if default_spec else ""

        if "default_args" in sub:
            da_raw = sub["default_args"]
            if not isinstance(da_raw, list) or not all(isinstance(x, str) for x in da_raw):
                fail(f"'agents.{aid}.default_args' must be a list of strings")
            default_args = tuple(da_raw)
        else:
            default_args = default_spec.default_args if default_spec else ()

        # REPLACE-not-merge: any supplied [[mode]] wholly replaces defaults.
        if "mode" in sub:
            modes = _parse_modes(aid, sub["mode"])
        elif default_spec is not None:
            modes = default_spec.permission_modes
        else:
            fail(
                f"custom agent {aid!r} declares no [[agents.{aid}.mode]] tables; "
                "a new agent must define at least one permission mode"
            )

        catalog[aid] = AgentSpec(
            id=aid,
            binary=binary,
            permission_modes=modes,
            install_hint=install_hint,
            version_args=version_args,
            default_args=default_args,
        )
    return catalog


def _argv_list(tbl: dict[str, Any], key: str) -> tuple[str, ...]:
    """Read an operator ``[container]`` argv template (list of strings).

    Templates are argv **lists**, never shell strings — a non-list (or a
    list with a non-string element) is rejected so a `"docker exec …"`
    string can't slip through as one opaque token.
    """
    raw = tbl.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        fail(f"'container.{key}' must be a list of strings (an argv list, not a shell string)")
    return tuple(raw)


def build_container_config(container_tbl: dict[str, Any]) -> ContainerConfig:
    """Parse + validate the merged ``[container]`` table into ContainerConfig.

    ``enabled``/templates/policy are operator-only (the project layer already
    dropped them in ``project_allowlist``). ``name``/``path_map`` may come
    from the project layer, so they are re-validated here against the safe
    charset / absolute-``..``-free rules (AC-B8) — a kept project leaf is
    never trusted unvalidated. When ``enabled = false`` no template is
    required and validation is skipped beyond the data leaves.
    """
    enabled = bool(container_tbl.get("enabled", False))

    on_missing = str(container_tbl.get("on_missing", "off"))
    if on_missing not in ("off", "start", "create"):
        fail(f"container.on_missing must be 'off', 'start', or 'create'; got {on_missing!r}")
    on_missing_mode = str(container_tbl.get("on_missing_mode", "prompt"))
    if on_missing_mode not in ("prompt", "auto"):
        fail(f"container.on_missing_mode must be 'prompt' or 'auto'; got {on_missing_mode!r}")

    name_template = str(container_tbl.get("name_template", ""))
    exec_template = _argv_list(container_tbl, "exec_template")
    is_running_cmd = _argv_list(container_tbl, "is_running_cmd")
    exists_cmd = _argv_list(container_tbl, "exists_cmd")
    start_template = _argv_list(container_tbl, "start_template")
    create_template = _argv_list(container_tbl, "create_template")
    stop_template = _argv_list(container_tbl, "stop_template")

    # Project-layer data leaves (re-validated even though deny-by-default
    # admitted only these two keys).
    name_raw = container_tbl.get("name", "")
    if not isinstance(name_raw, str):
        fail("container.name must be a string")
    name = name_raw
    if name:
        # Validate the literal override here; a template-derived name is
        # re-validated post-expansion at resolve time (both go through the
        # same charset check).
        validate_container_name(name)

    path_map_raw = container_tbl.get("path_map", {})
    if not isinstance(path_map_raw, dict):
        fail("container.path_map must be a TOML table of host_prefix -> container_prefix")
    path_map = validate_path_map({str(k): str(v) for k, v in path_map_raw.items()})

    if enabled:
        if not exec_template:
            fail("container.enabled = true requires a non-empty container.exec_template")
        if not name and not name_template:
            fail("container.enabled = true requires container.name_template (or .uxon.toml name)")
        if on_missing in ("start", "create") and not is_running_cmd:
            fail(
                f"container.on_missing = {on_missing!r} requires container.is_running_cmd "
                "and container.exists_cmd to probe the container state"
            )
        if on_missing in ("start", "create") and not exists_cmd:
            fail(
                f"container.on_missing = {on_missing!r} requires container.exists_cmd "
                "to detect an absent vs stopped container"
            )
        if on_missing in ("start", "create") and not start_template:
            fail(f"container.on_missing = {on_missing!r} requires container.start_template")
        if on_missing == "create" and not create_template:
            fail("container.on_missing = 'create' requires container.create_template")

    return ContainerConfig(
        enabled=enabled,
        name_template=name_template,
        exec_template=exec_template,
        is_running_cmd=is_running_cmd,
        exists_cmd=exists_cmd,
        start_template=start_template,
        create_template=create_template,
        stop_template=stop_template,
        on_missing=on_missing,  # type: ignore[arg-type]
        on_missing_mode=on_missing_mode,  # type: ignore[arg-type]
        name=name,
        path_map=path_map,
    )


def load_config(cwd: str) -> Config:
    from uxon.domain import git_profiles as uxon_git_profiles

    merged, _ = resolve_config_layers(cwd)
    # Load raw repo data (before merge with defaults) so the removed flat
    # ``default_claude_args`` key surfaces an error instead of being masked.
    _raw_repo = load_toml(repo_config_path())
    runtime_user = str(merged.get("runtime_user", DEFAULT_CONFIG["runtime_user"])).strip()
    default_launch_mode = str(
        merged.get("default_launch_mode", DEFAULT_CONFIG["default_launch_mode"])
    ).strip()
    if default_launch_mode not in {"fixed", "caller"}:
        fail(f"invalid default_launch_mode: {default_launch_mode!r} (expected 'fixed' or 'caller')")
    launch_user_by_caller_raw = merged.get(
        "launch_user_by_caller", DEFAULT_CONFIG["launch_user_by_caller"]
    )
    if not isinstance(launch_user_by_caller_raw, dict):
        fail("launch_user_by_caller must be a TOML table")
    launch_user_by_caller = {
        str(k).strip(): str(v).strip()
        for k, v in launch_user_by_caller_raw.items()
        if str(k).strip() and str(v).strip()
    }
    session_users_raw = merged.get("session_users", DEFAULT_CONFIG["session_users"])
    if not isinstance(session_users_raw, list):
        fail("session_users must be a TOML array")
    session_users = normalize_user_list([str(x) for x in session_users_raw])
    if not session_users:
        session_users = [runtime_user] if runtime_user else []
    enable_all_users_list = bool(
        merged.get("enable_all_users_list", DEFAULT_CONFIG["enable_all_users_list"])
    )
    session_prefix = str(merged.get("session_prefix", DEFAULT_CONFIG["session_prefix"]))
    legacy_raw = merged.get("legacy_session_prefixes", DEFAULT_CONFIG["legacy_session_prefixes"])
    if not isinstance(legacy_raw, list) or not all(isinstance(p, str) for p in legacy_raw):
        fail("legacy_session_prefixes must be a list of strings")
    legacy_session_prefixes = tuple(p for p in legacy_raw if p and p != session_prefix)
    allowed_roots = [
        canonical(p) for p in merged.get("allowed_roots", DEFAULT_CONFIG["allowed_roots"])
    ]

    # Hard-reject the removed flat ``default_claude_args`` key with a
    # clear migration message. Check raw repo config (not merged with
    # defaults) so DEFAULT_CONFIG's nested ``[agents.claude]`` block
    # doesn't mask the failure.
    if "default_claude_args" in _raw_repo:
        fail(
            "config key 'default_claude_args' was replaced by "
            "'[agents.claude] default_args = [...]'. "
            "Update config/config.toml accordingly."
        )

    agents_tbl = merged.get("agents", {})
    if not isinstance(agents_tbl, dict):
        fail("'agents' must be a TOML table")
    # Merged catalog (defaults ⊕ operator config). The valid-id set is
    # derived dynamically from its keys — there is no static VALID_AGENT_IDS.
    agents = build_agent_catalog(agents_tbl)
    valid_agent_ids = tuple(agents)
    # ``enabled`` empty/absent = auto-mode (every installed catalogued
    # agent is launchable). Non-empty list = strict whitelist.
    enabled_raw = agents_tbl.get("enabled", [])
    if not isinstance(enabled_raw, list):
        fail("'agents.enabled' must be a list (use [] for auto-mode)")
    enabled = tuple(str(x) for x in enabled_raw)
    for aid in enabled:
        if aid not in agents:
            fail(f"unknown agent id in agents.enabled: {aid!r} (expected one of {valid_agent_ids})")
    default_agent = str(agents_tbl.get("default", ""))
    if default_agent:
        if default_agent not in agents:
            fail(
                f"unknown agent id in agents.default: {default_agent!r} "
                f"(expected one of {valid_agent_ids})"
            )
        if enabled and default_agent not in enabled:
            fail(f"agents.default={default_agent!r} is not in agents.enabled={list(enabled)}")

    new_project_root = canonical(
        str(merged.get("new_project_root", DEFAULT_CONFIG["new_project_root"]))
    )
    repeat_noninteractive_mode = validate_repeat_mode(
        str(merged.get("repeat_noninteractive_mode", DEFAULT_CONFIG["repeat_noninteractive_mode"])),
        "repeat_noninteractive_mode",
    )
    tmux_socket_template = str(
        merged.get("tmux_socket_template", DEFAULT_CONFIG["tmux_socket_template"])
    ).strip()
    if not tmux_socket_template:
        fail("tmux_socket_template must not be empty")
    try:
        tui_refresh_interval_seconds = float(
            merged.get(
                "tui_refresh_interval_seconds",
                DEFAULT_CONFIG["tui_refresh_interval_seconds"],
            )
        )
    except (TypeError, ValueError):
        fail("tui_refresh_interval_seconds must be a number")
    if tui_refresh_interval_seconds <= 0:
        fail("tui_refresh_interval_seconds must be greater than 0")
    try:
        tui_ssh_refresh_interval_seconds = float(
            merged.get(
                "tui_ssh_refresh_interval_seconds",
                DEFAULT_CONFIG["tui_ssh_refresh_interval_seconds"],
            )
        )
    except (TypeError, ValueError):
        fail("tui_ssh_refresh_interval_seconds must be a number")
    if tui_ssh_refresh_interval_seconds <= 0:
        fail("tui_ssh_refresh_interval_seconds must be greater than 0")

    # ── [tui.table] dashboard column layout ──────────────────────────
    # Read defensively. Both ``tui`` and ``tui.table`` may be absent
    # from the TOML; that maps to "use REGISTRY defaults" (signalled
    # by ``tui_table_columns is None``).
    tui_tbl = merged.get("tui", {})
    if not isinstance(tui_tbl, dict):
        fail("'tui' must be a TOML table")
    tui_table_tbl = tui_tbl.get("table", {})
    if not isinstance(tui_table_tbl, dict):
        fail("'tui.table' must be a TOML table")
    tui_table_columns_raw = tui_table_tbl.get("columns")
    if tui_table_columns_raw is None or tui_table_columns_raw == []:
        # Absent or explicit empty list → "use REGISTRY defaults".
        tui_table_columns: tuple[str, ...] | None = None
    elif not isinstance(tui_table_columns_raw, list):
        fail("tui.table.columns must be a list of column ids")
    else:
        tui_table_columns = tuple(str(x) for x in tui_table_columns_raw)
    # ``tui.table.default_sort_by`` was removed in 3.4 (sort is now a
    # hard contract — locals → cfg-order remotes → recency). Any value
    # carried over from older configs is silently ignored; emit one
    # ``UXON_DEBUG=tui`` line so operators can spot the fossil.
    if "default_sort_by" in tui_table_tbl:
        try:
            events.debug(
                "tui",
                reason="ignored_default_sort_by",
                id=str(tui_table_tbl.get("default_sort_by", "")),
            )
        except Exception:
            # Telemetry, not a correctness path.
            pass

    tui_table_default_view_raw = tui_table_tbl.get("default_view", "flat")
    if tui_table_default_view_raw not in ("by_host", "flat"):
        fail(
            f"tui.table.default_view must be 'by_host' or 'flat', "
            f"got {tui_table_default_view_raw!r}"
        )
    tui_table_default_view: Literal["by_host", "flat"] = tui_table_default_view_raw

    tui_search_tbl = tui_tbl.get("search", {})
    if not isinstance(tui_search_tbl, dict):
        fail("'tui.search' must be a TOML table")
    fields_raw = tui_search_tbl.get("fields", ["name", "user"])
    allowed = {"name", "user", "host", "path", "cmd"}
    if not isinstance(fields_raw, list) or not all(f in allowed for f in fields_raw):
        bad = (
            [f for f in fields_raw if f not in allowed]
            if isinstance(fields_raw, list)
            else fields_raw
        )
        fail(f"tui.search.fields: unknown entries {bad!r}; allowed {sorted(allowed)!r}")
    tui_search_fields = tuple(fields_raw)

    palette_raw = tui_tbl.get("color_palette", ["cyan", "blue"])
    if not isinstance(palette_raw, list) or not all(isinstance(c, str) and c for c in palette_raw):
        fail("tui.color_palette must be a list of non-empty strings")
    tui_color_palette = tuple(palette_raw)

    local_host_tbl = merged.get("local_host", {})
    if not isinstance(local_host_tbl, dict):
        fail("'local_host' must be a TOML table")
    local_host_color = str(local_host_tbl.get("color", "green"))
    if not local_host_color:
        fail("local_host.color must be non-empty")

    worktree_root = str(merged.get("worktree_root", DEFAULT_CONFIG["worktree_root"]))
    worktree_base = validate_worktree_base(
        str(merged.get("worktree_base", DEFAULT_CONFIG["worktree_base"])),
        "worktree_base",
    )

    ssh_multiplex = str(merged.get("ssh_multiplex", DEFAULT_CONFIG["ssh_multiplex"]))
    if ssh_multiplex not in ("auto", "off"):
        fail(f"ssh_multiplex must be 'auto' or 'off', got {ssh_multiplex!r}")
    persist_raw = merged.get(
        "ssh_control_persist_seconds", DEFAULT_CONFIG["ssh_control_persist_seconds"]
    )
    # SSH's ControlPersist option only accepts whole seconds — reject
    # floats outright rather than truncating them silently. ``bool`` is
    # an ``int`` subclass in Python, so it needs its own guard.
    if isinstance(persist_raw, bool) or not isinstance(persist_raw, int):
        fail(f"ssh_control_persist_seconds must be a positive integer, got {persist_raw!r}")
    ssh_control_persist_seconds = persist_raw
    if ssh_control_persist_seconds <= 0:
        fail("ssh_control_persist_seconds must be > 0; disable via ssh_multiplex=off")
    try:
        fetch_concurrency = int(
            merged.get("fetch_concurrency", DEFAULT_CONFIG["fetch_concurrency"])
        )
    except (TypeError, ValueError):
        fail("fetch_concurrency must be an integer")
    if fetch_concurrency <= 0:
        fail("fetch_concurrency must be greater than 0")

    git_create_enabled = bool(
        merged.get("git_create_enabled", DEFAULT_CONFIG["git_create_enabled"])
    )
    default_git_remote_profile = str(
        merged.get("default_git_remote_profile", DEFAULT_CONFIG["default_git_remote_profile"])
    ).strip()
    try:
        git_remote_profiles = uxon_git_profiles.load_profiles(
            merged.get("git_remote_profiles", DEFAULT_CONFIG["git_remote_profiles"])
        )
    except uxon_git_profiles.ProfileError as exc:
        fail(str(exc))

    from uxon.infra import remote_hosts as uxon_remote_hosts

    try:
        remote_hosts = uxon_remote_hosts.load_remote_hosts(
            merged.get("remote_hosts", DEFAULT_CONFIG["remote_hosts"])
        )
    except uxon_remote_hosts.RemoteHostError as exc:
        fail(str(exc))

    # Demo-mode short-circuit: when UXON_DEMO_HOSTS=<dir> is set, replace
    # the configured peer list with synthetic hosts derived from the
    # envelope files in that directory. The collector hook in
    # ``remote.collector.fetch_remote_snapshot`` then reads each envelope
    # from disk instead of running ssh. Operator config is ignored in
    # this mode by design — the scenario is the only source of truth.
    _demo_dir = demo.demo_hosts_dir()
    if _demo_dir is not None:
        remote_hosts = demo.synthesize_remote_hosts(_demo_dir)

    audit_tbl = merged.get("audit", DEFAULT_CONFIG["audit"])
    if not isinstance(audit_tbl, dict):
        fail("'audit' must be a TOML table")
    audit_enabled = bool(audit_tbl.get("enabled", True))
    audit_syslog_facility = str(audit_tbl.get("syslog_facility", "user"))

    tmux_tbl = merged.get("tmux", DEFAULT_CONFIG["tmux"])
    if not isinstance(tmux_tbl, dict):
        fail("'tmux' must be a TOML table")
    # Off by default (matches DEFAULT_CONFIG): nothing is applied until the
    # operator sets ``manage_options = true``. ``merged`` is seeded from
    # DEFAULT_CONFIG, so the recommended scope tables are present-but-dormant —
    # flipping the toggle on yields the recommended set with no further config.
    tmux_manage_options = bool(tmux_tbl.get("manage_options", False))
    tmux_scope_tables: dict[str, dict] = {}
    for _scope in ("options", "server_options", "append_server_options"):
        # Per-scope fallback to the recommended default (mirrors how [audit]
        # reads each leaf with its own default). merge_config is a shallow
        # top-level merge, so an operator [tmux] table that omits a scope —
        # e.g. the TUI toggle writing only manage_options — would otherwise
        # wipe that scope; falling back keeps the recommended set intact and
        # makes overrides per-scope rather than whole-[tmux]-wholesale.
        _sub = tmux_tbl.get(_scope, DEFAULT_CONFIG["tmux"][_scope])
        if not isinstance(_sub, dict):
            fail(f"'tmux.{_scope}' must be a TOML table")
        for _k, _v in _sub.items():
            # bool before int: bool is an int subclass (mirrors the audit /
            # ssh_control_persist guards elsewhere in this loader).
            if not isinstance(_v, (bool, int, str)):
                fail(f"'tmux.{_scope}.{_k}' must be a scalar (bool/int/str)")
        tmux_scope_tables[_scope] = dict(_sub)

    if default_git_remote_profile and not uxon_git_profiles.find_profile(
        git_remote_profiles, default_git_remote_profile
    ):
        fail(
            f"default_git_remote_profile={default_git_remote_profile!r} does not "
            f"exist in git_remote_profiles"
        )

    container_tbl = merged.get("container", DEFAULT_CONFIG["container"])
    if not isinstance(container_tbl, dict):
        fail("'container' must be a TOML table")
    container = build_container_config(container_tbl)

    return Config(
        runtime_user=runtime_user,
        default_launch_mode=default_launch_mode,
        enable_all_users_list=enable_all_users_list,
        launch_user_by_caller=launch_user_by_caller,
        session_users=session_users,
        allowed_roots=allowed_roots,
        session_prefix=session_prefix,
        legacy_session_prefixes=legacy_session_prefixes,
        enabled_agents=enabled,
        default_agent=default_agent,
        agents=agents,
        new_project_root=new_project_root,
        repeat_noninteractive_mode=repeat_noninteractive_mode,
        tmux_socket_template=tmux_socket_template,
        tui_refresh_interval_seconds=tui_refresh_interval_seconds,
        tui_ssh_refresh_interval_seconds=tui_ssh_refresh_interval_seconds,
        git_create_enabled=git_create_enabled,
        default_git_remote_profile=default_git_remote_profile,
        git_remote_profiles=git_remote_profiles,
        remote_hosts=remote_hosts,
        ssh_multiplex=ssh_multiplex,
        ssh_control_persist_seconds=ssh_control_persist_seconds,
        fetch_concurrency=fetch_concurrency,
        audit_enabled=audit_enabled,
        audit_syslog_facility=audit_syslog_facility,
        tui_table_columns=tui_table_columns,
        tui_table_default_view=tui_table_default_view,
        tui_search_fields=tui_search_fields,
        tui_color_palette=tui_color_palette,
        local_host_color=local_host_color,
        worktree_root=worktree_root,
        worktree_base=worktree_base,
        tmux_manage_options=tmux_manage_options,
        tmux_options=tmux_scope_tables["options"],
        tmux_server_options=tmux_scope_tables["server_options"],
        tmux_append_server_options=tmux_scope_tables["append_server_options"],
        container=container,
    )
