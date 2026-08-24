# SPDX-License-Identifier: MIT
"""Config loading: produce a :class:`uxon.domain.Config`.

Reads the operator-owned repo config, validates every field, and returns the
typed domain ``Config``. Impure: reads TOML files, ``os.environ`` (demo mode),
and composes the git-profile / remote-host / demo adapters.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from uxon.domain.agents import DEFAULT_AGENT_CATALOG, AgentSpec, PermissionMode
from uxon.domain.authz import canonical, lexical_absolute
from uxon.domain.config import (
    DEFAULT_CONFIG,
    Config,
    merge_config,
    validate_repeat_mode,
    validate_worktree_base,
)
from uxon.domain.execution import (
    LOCAL_PROBE_TIMEOUT_SECONDS,
    ExecutionBackendSpec,
    ExecutionConfig,
    validate_execution_config,
)
from uxon.domain.launch_profiles import (
    GitRemotePolicy,
    LaunchConfig,
    LaunchPathRule,
    LaunchProfile,
    builtin_launch_profiles,
    validate_tmux_safe_id,
)
from uxon.domain.runtime import (
    WorkloadRuntimeSpec,
    validate_path_map,
    validate_runtime,
)
from uxon.errors import fail
from uxon.infra import demo, version_probe

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


def repo_config_path() -> Path:
    return version_probe.repo_root() / "config" / "config.toml"


def resolve_config_layers(_cwd: str) -> tuple[dict[str, Any], list[Path]]:
    merged = dict(DEFAULT_CONFIG)
    sources: list[Path] = []
    repo_cfg = repo_config_path()
    if repo_cfg.exists():
        sources.append(repo_cfg)
    merged = merge_config(merged, load_toml(repo_cfg))
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
        _reject_unknown_keys(m, {"id", "label", "flags", "dangerous"}, source=f"agents.{aid}.mode")
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
    config_ids = [k for k, v in agents_tbl.items() if isinstance(v, dict)]
    # Non-dict ``[agents.<id>]`` (e.g. ``claude = 5``) is a config error.
    for k, v in agents_tbl.items():
        if not isinstance(v, dict):
            fail(f"'agents.{k}' must be a TOML table")
    union_ids: list[str] = list(DEFAULT_AGENT_CATALOG)
    for aid in config_ids:
        if aid not in union_ids:
            union_ids.append(aid)

    catalog: dict[str, AgentSpec] = {}
    for aid in union_ids:
        validate_tmux_safe_id(aid, what="agent id")
        default_spec = DEFAULT_AGENT_CATALOG.get(aid)
        sub = agents_tbl.get(aid, {})
        if not isinstance(sub, dict):
            fail(f"'agents.{aid}' must be a TOML table")
        _reject_unknown_keys(
            sub,
            {"binary", "version_args", "install_hint", "default_args", "mode"},
            source=f"agents.{aid}",
        )

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


def _reject_unknown_keys(raw: dict[str, Any], allowed: set[str], *, source: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        fail(f"{source}: unknown key(s) {unknown!r}; expected one of {sorted(allowed)!r}")


def validate_operator_schema(raw_repo: dict[str, Any]) -> None:
    """Reject unknown operator keys before defaults can mask them."""
    _reject_unknown_keys(raw_repo, set(DEFAULT_CONFIG), source="config")


def _string_list(value: Any, *, source: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        fail(f"{source} must be a list of strings")
    return tuple(value)


def _string_value(
    raw: dict[str, Any], key: str, *, source: str, default: str = "", strip: bool = False
) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        fail(f"{source}.{key} must be a string")
    return value.strip() if strip else value


def _argv_list_from(tbl: dict[str, Any], key: str, *, source: str) -> tuple[str, ...]:
    raw = tbl.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        fail(f"{source}.{key} must be a list of strings (an argv list, not a shell string)")
    return tuple(raw)


def build_execution_config(execution_tbl: dict[str, Any]) -> ExecutionConfig:
    """Parse the operator-owned target-user execution boundary."""
    source = "execution"
    _reject_unknown_keys(
        execution_tbl,
        {"default_backend", "backend_by_launch_user", "backends"},
        source=source,
    )
    default_backend = _string_value(
        execution_tbl, "default_backend", source=source, default="local", strip=True
    )
    by_user_raw = execution_tbl.get("backend_by_launch_user", {})
    if not isinstance(by_user_raw, dict):
        fail("execution.backend_by_launch_user must be a TOML table")
    by_user: dict[str, str] = {}
    for user, backend_id in by_user_raw.items():
        if not isinstance(backend_id, str):
            fail(f"execution.backend_by_launch_user.{user} must be a string")
        by_user[str(user).strip()] = backend_id.strip()

    backends: dict[str, ExecutionBackendSpec] = {
        "local": ExecutionBackendSpec(
            id="local", kind="local", probe_timeout_seconds=LOCAL_PROBE_TIMEOUT_SECONDS
        )
    }
    backends_raw = execution_tbl.get("backends", {})
    if not isinstance(backends_raw, dict):
        fail("execution.backends must be a TOML table")
    if "local" in backends_raw:
        fail("execution.backends.local is built in and cannot be configured")
    for backend_id, raw in backends_raw.items():
        validate_tmux_safe_id(str(backend_id), what="execution backend id")
        if not isinstance(raw, dict):
            fail(f"execution.backends.{backend_id} must be a TOML table")
        backend_source = f"execution.backends.{backend_id}"
        _reject_unknown_keys(
            raw,
            {
                "kind",
                "command_prefix",
                "probe_timeout_seconds",
            },
            source=backend_source,
        )
        kind = _string_value(raw, "kind", source=backend_source, strip=True)
        if kind != "command":
            fail(f"{backend_source}.kind must be 'command'")
        timeout_raw = raw.get("probe_timeout_seconds", 5.0)
        try:
            timeout = float(timeout_raw)
        except (TypeError, ValueError):
            fail(f"{backend_source}.probe_timeout_seconds must be a number")
        backends[str(backend_id)] = ExecutionBackendSpec(
            id=str(backend_id),
            kind="command",
            command_prefix=_argv_list_from(raw, "command_prefix", source=backend_source),
            probe_timeout_seconds=timeout,
        )
    return validate_execution_config(
        ExecutionConfig(
            default_backend=default_backend,
            backend_by_launch_user=by_user,
            backends=backends,
        )
    )


def build_runtimes(runtime_tbl: dict[str, Any]) -> dict[str, WorkloadRuntimeSpec]:
    out: dict[str, WorkloadRuntimeSpec] = {
        "direct": WorkloadRuntimeSpec(id="direct", kind="direct")
    }
    if "direct" in runtime_tbl:
        fail("runtimes.direct is built in and cannot be configured")
    for cid, raw in runtime_tbl.items():
        validate_tmux_safe_id(str(cid), what="runtime id")
        if not isinstance(raw, dict):
            fail(f"'runtimes.{cid}' must be a TOML table")
        source = f"runtimes.{cid}"
        _reject_unknown_keys(
            raw,
            {
                "kind",
                "resource_scope",
                "resource_name_template",
                "exec_prefix",
                "readiness",
                "identity",
                "session",
                "timeouts",
                "telemetry",
                "path_map",
            },
            source=source,
        )
        kind = _string_value(raw, "kind", source=source, strip=True)
        if kind != "command":
            fail(f"{source}.kind must be 'command'")
        path_map_raw = raw.get("path_map", {})
        if not isinstance(path_map_raw, dict):
            fail(f"{source}.path_map must be a TOML table of host_prefix -> runtime_prefix")
        readiness = raw.get("readiness", {})
        identity = raw.get("identity", {})
        session = raw.get("session", {})
        timeouts = raw.get("timeouts", {})
        for table_name, table in (
            ("readiness", readiness),
            ("identity", identity),
            ("session", session),
            ("timeouts", timeouts),
        ):
            if not isinstance(table, dict):
                fail(f"{source}.{table_name} must be a TOML table")
        _reject_unknown_keys(
            readiness,
            {
                "ready_command",
                "exists_command",
                "start_command",
                "create_command",
                "on_missing",
                "approval",
            },
            source=f"{source}.readiness",
        )
        _reject_unknown_keys(identity, {"resolve_command"}, source=f"{source}.identity")
        _reject_unknown_keys(session, {"stop_command"}, source=f"{source}.session")
        _reject_unknown_keys(
            timeouts,
            {"probe_seconds", "prepare_seconds", "stop_seconds"},
            source=f"{source}.timeouts",
        )

        def _timeout(
            key: str,
            default: float,
            *,
            _timeouts: dict[str, Any] = timeouts,
            _source: str = source,
        ) -> float:
            try:
                return float(_timeouts.get(key, default))
            except (TypeError, ValueError):
                fail(f"{_source}.timeouts.{key} must be a number")
            raise AssertionError("unreachable")

        profile = WorkloadRuntimeSpec(
            id=str(cid),
            kind="command",
            resource_scope=_string_value(
                raw, "resource_scope", source=source, default="global", strip=True
            ),  # type: ignore[arg-type]
            resource_name_template=_string_value(raw, "resource_name_template", source=source),
            exec_prefix=_argv_list_from(raw, "exec_prefix", source=source),
            ready_command=_argv_list_from(readiness, "ready_command", source=f"{source}.readiness"),
            exists_command=_argv_list_from(
                readiness, "exists_command", source=f"{source}.readiness"
            ),
            start_command=_argv_list_from(readiness, "start_command", source=f"{source}.readiness"),
            create_command=_argv_list_from(
                readiness, "create_command", source=f"{source}.readiness"
            ),
            stop_command=_argv_list_from(session, "stop_command", source=f"{source}.session"),
            identity_command=_argv_list_from(
                identity, "resolve_command", source=f"{source}.identity"
            ),
            on_missing=_string_value(
                readiness, "on_missing", source=f"{source}.readiness", default="fail", strip=True
            ),  # type: ignore[arg-type]
            approval=_string_value(
                readiness, "approval", source=f"{source}.readiness", default="prompt", strip=True
            ),  # type: ignore[arg-type]
            telemetry=_string_value(raw, "telemetry", source=source, default="none", strip=True),  # type: ignore[arg-type]
            probe_timeout_seconds=_timeout("probe_seconds", 10.0),
            prepare_timeout_seconds=_timeout("prepare_seconds", 120.0),
            stop_timeout_seconds=_timeout("stop_seconds", 10.0),
            path_map=validate_path_map({str(k): str(v) for k, v in path_map_raw.items()}),
        )
        out[str(cid)] = validate_runtime(profile)
    return out


def _parse_git_policy(raw: dict[str, Any], *, source: str) -> GitRemotePolicy:
    allowed = _string_list(
        raw.get("allowed_git_remote_profiles"), source=f"{source}.allowed_git_remote_profiles"
    )
    default = _string_value(raw, "default_git_remote_profile", source=source, strip=True)
    if default and default not in allowed:
        fail(
            f"{source}.default_git_remote_profile={default!r} is not allowed by {source}.allowed_git_remote_profiles"
        )
    return GitRemotePolicy(allowed_profiles=allowed, default_profile=default)


def _validate_git_names(names: tuple[str, ...], valid_names: set[str], *, source: str) -> None:
    for name in names:
        if name not in valid_names:
            fail(f"{source} references unknown git_remote_profiles entry {name!r}")


def build_launch_config(
    launch_tbl: dict[str, Any],
    *,
    agents: dict[str, AgentSpec],
    runtimes: dict[str, WorkloadRuntimeSpec],
    git_remote_profile_names: set[str],
) -> LaunchConfig:
    _reject_unknown_keys(
        launch_tbl,
        {"enabled_profiles", "default_profile", "profiles", "path_rules"},
        source="launch",
    )
    profiles = builtin_launch_profiles(agents)
    builtin_profile_ids = set(profiles)
    enabled_profiles = _string_list(
        launch_tbl.get("enabled_profiles", []), source="launch.enabled_profiles"
    )
    profiles_tbl = launch_tbl.get("profiles", {})
    if not isinstance(profiles_tbl, dict):
        fail("'launch.profiles' must be a TOML table")
    for pid, raw in profiles_tbl.items():
        validate_tmux_safe_id(str(pid), what="launch profile id")
        if not isinstance(raw, dict):
            fail(f"'launch.profiles.{pid}' must be a TOML table")
        if not enabled_profiles and str(pid) in builtin_profile_ids:
            fail(
                f"launch.profiles.{pid} overrides a shipped auto-mode profile; "
                "set launch.enabled_profiles to name it explicitly"
            )
        source = f"launch.profiles.{pid}"
        _reject_unknown_keys(
            raw,
            {
                "agent",
                "display_name",
                "launch_user",
                "runtime",
                "allowed_git_remote_profiles",
                "default_git_remote_profile",
            },
            source=source,
        )
        agent = _string_value(raw, "agent", source=source, strip=True)
        if not agent:
            fail(f"{source}.agent is required")
        if agent not in agents:
            fail(f"{source}.agent={agent!r} is not in the configured agent catalog")
        runtime = _string_value(raw, "runtime", source=source, default="direct", strip=True)
        if runtime not in runtimes:
            fail(f"{source}.runtime={runtime!r} is not configured")
        policy = _parse_git_policy(raw, source=source)
        _validate_git_names(
            policy.allowed_profiles,
            git_remote_profile_names,
            source=f"{source}.allowed_git_remote_profiles",
        )
        profiles[str(pid)] = LaunchProfile(
            id=str(pid),
            agent=agent,
            display_name=_string_value(raw, "display_name", source=source),
            launch_user=_string_value(raw, "launch_user", source=source, strip=True),
            runtime=runtime,
            git_remote=policy,
        )

    for pid in enabled_profiles:
        if pid not in profiles:
            fail(f"launch.enabled_profiles references unknown launch profile {pid!r}")
    default_profile = _string_value(launch_tbl, "default_profile", source="launch", strip=True)
    effective_enabled = enabled_profiles or tuple(
        pid for pid in ("claude", "codex", "cursor") if pid in profiles
    )
    if default_profile and default_profile not in effective_enabled:
        fail(f"launch.default_profile={default_profile!r} is not enabled")

    path_rules_raw = launch_tbl.get("path_rules", [])
    if not isinstance(path_rules_raw, list) or not all(
        isinstance(rule, dict) for rule in path_rules_raw
    ):
        fail("'launch.path_rules' must be an array of tables")
    path_rules: list[LaunchPathRule] = []
    enabled_set = set(effective_enabled)
    for index, raw in enumerate(path_rules_raw):
        source = f"launch.path_rules[{index}]"
        _reject_unknown_keys(
            raw,
            {
                "path_prefix",
                "allowed_profiles",
                "default_profile",
                "allowed_git_remote_profiles",
                "default_git_remote_profile",
            },
            source=source,
        )
        prefix = raw.get("path_prefix")
        if not isinstance(prefix, str) or not prefix.strip():
            fail(f"{source}.path_prefix must be a non-empty absolute path")
        if not prefix.startswith("/"):
            fail(f"{source}.path_prefix must be an absolute path")
        if os.path.normpath(prefix) != prefix or ".." in prefix.split("/"):
            fail(f"{source}.path_prefix must be an absolute normalized path without '..'")
        path_prefix = lexical_absolute(prefix)
        allowed = _string_list(raw.get("allowed_profiles"), source=f"{source}.allowed_profiles")
        if not allowed:
            fail(f"{source}.allowed_profiles must be a non-empty list")
        for pid in allowed:
            if pid not in enabled_set:
                fail(f"{source}.allowed_profiles references disabled launch profile {pid!r}")
        rule_default = _string_value(raw, "default_profile", source=source, strip=True)
        if rule_default and rule_default not in allowed:
            fail(f"{source}.default_profile={rule_default!r} is not in allowed_profiles")
        rule_allowed_git: tuple[str, ...] | None = None
        if "allowed_git_remote_profiles" in raw:
            rule_allowed_git = _string_list(
                raw.get("allowed_git_remote_profiles"),
                source=f"{source}.allowed_git_remote_profiles",
            )
            _validate_git_names(
                rule_allowed_git,
                git_remote_profile_names,
                source=f"{source}.allowed_git_remote_profiles",
            )
        rule_default_git = _string_value(
            raw, "default_git_remote_profile", source=source, strip=True
        )
        if rule_default_git and rule_default_git not in git_remote_profile_names:
            fail(
                f"{source}.default_git_remote_profile references unknown git_remote_profiles entry {rule_default_git!r}"
            )
        for pid in allowed:
            profile_policy = profiles[pid].git_remote
            effective_allowed = (
                tuple(
                    name
                    for name in profile_policy.allowed_profiles
                    if rule_allowed_git is not None and name in rule_allowed_git
                )
                if rule_allowed_git is not None
                else profile_policy.allowed_profiles
            )
            effective_default = rule_default_git or profile_policy.default_profile
            if effective_default and effective_default not in effective_allowed:
                fail(
                    f"{source}.default_git_remote_profile={effective_default!r} is not "
                    f"allowed for launch profile {pid!r} after git-remote policy intersection"
                )
        path_rules.append(
            LaunchPathRule(
                path_prefix=path_prefix,
                allowed_profiles=allowed,
                default_profile=rule_default,
                allowed_git_remote_profiles=rule_allowed_git,
                default_git_remote_profile=rule_default_git,
            )
        )

    return LaunchConfig(
        enabled_profiles=enabled_profiles,
        default_profile=default_profile,
        profiles=profiles,
        path_rules=tuple(path_rules),
    )


def load_config(cwd: str) -> Config:
    from uxon.domain import git_profiles as uxon_git_profiles

    merged, _ = resolve_config_layers(cwd)
    # Load raw repo data (before merge with defaults) so the removed flat
    # keys surface an error instead of being masked.
    _raw_repo = load_toml(repo_config_path())
    validate_operator_schema(_raw_repo)
    default_launch_user = str(
        merged.get("default_launch_user", DEFAULT_CONFIG["default_launch_user"])
    ).strip()
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
        session_users = [default_launch_user] if default_launch_user else []
    enable_all_users_list = bool(
        merged.get("enable_all_users_list", DEFAULT_CONFIG["enable_all_users_list"])
    )
    session_prefix = str(merged.get("session_prefix", DEFAULT_CONFIG["session_prefix"]))
    legacy_raw = merged.get("legacy_session_prefixes", DEFAULT_CONFIG["legacy_session_prefixes"])
    if not isinstance(legacy_raw, list) or not all(isinstance(p, str) for p in legacy_raw):
        fail("legacy_session_prefixes must be a list of strings")
    legacy_session_prefixes = tuple(p for p in legacy_raw if p and p != session_prefix)
    allowed_roots_raw = merged.get("allowed_roots", DEFAULT_CONFIG["allowed_roots"])
    if not isinstance(allowed_roots_raw, list) or not all(
        isinstance(path, str) for path in allowed_roots_raw
    ):
        fail("allowed_roots must be a list of absolute paths")
    try:
        allowed_roots = [lexical_absolute(path) for path in allowed_roots_raw]
    except ValueError as exc:
        fail(f"allowed_roots: {exc}")

    agents_tbl = merged.get("agents", {})
    if not isinstance(agents_tbl, dict):
        fail("'agents' must be a TOML table")
    # Merged catalog (defaults ⊕ operator config). The valid-id set is
    # derived dynamically from its keys — there is no static VALID_AGENT_IDS.
    agents = build_agent_catalog(agents_tbl)
    execution_tbl = merged.get("execution", DEFAULT_CONFIG["execution"])
    if not isinstance(execution_tbl, dict):
        fail("'execution' must be a TOML table")
    execution = build_execution_config(execution_tbl)
    try:
        git_remote_profiles = uxon_git_profiles.load_profiles(
            merged.get("git_remote_profiles", DEFAULT_CONFIG["git_remote_profiles"])
        )
    except uxon_git_profiles.ProfileError as exc:
        fail(str(exc))
    git_remote_profile_names = {profile.name for profile in git_remote_profiles}

    runtime_tbl = merged.get("runtimes", DEFAULT_CONFIG["runtimes"])
    if not isinstance(runtime_tbl, dict):
        fail("'runtimes' must be a TOML table")
    runtimes = build_runtimes(runtime_tbl)

    launch_tbl = merged.get("launch", DEFAULT_CONFIG["launch"])
    if not isinstance(launch_tbl, dict):
        fail("'launch' must be a TOML table")
    launch = build_launch_config(
        launch_tbl,
        agents=agents,
        runtimes=runtimes,
        git_remote_profile_names=git_remote_profile_names,
    )

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
    _reject_unknown_keys(tui_tbl, {"table", "search", "color_palette"}, source="tui")
    tui_table_tbl = tui_tbl.get("table", {})
    if not isinstance(tui_table_tbl, dict):
        fail("'tui.table' must be a TOML table")
    _reject_unknown_keys(tui_table_tbl, {"columns", "default_view"}, source="tui.table")
    tui_table_columns_raw = tui_table_tbl.get("columns")
    if tui_table_columns_raw is None or tui_table_columns_raw == []:
        # Absent or explicit empty list → "use REGISTRY defaults".
        tui_table_columns: tuple[str, ...] | None = None
    elif not isinstance(tui_table_columns_raw, list):
        fail("tui.table.columns must be a list of column ids")
    else:
        tui_table_columns = tuple(str(x) for x in tui_table_columns_raw)
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
    _reject_unknown_keys(tui_search_tbl, {"fields"}, source="tui.search")
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
    _reject_unknown_keys(local_host_tbl, {"color"}, source="local_host")
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
    default_git_remote_profile = ""

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
    _reject_unknown_keys(audit_tbl, {"enabled", "syslog_facility"}, source="audit")
    audit_enabled = bool(audit_tbl.get("enabled", True))
    audit_syslog_facility = str(audit_tbl.get("syslog_facility", "user"))

    tmux_tbl = merged.get("tmux", DEFAULT_CONFIG["tmux"])
    if not isinstance(tmux_tbl, dict):
        fail("'tmux' must be a TOML table")
    _reject_unknown_keys(
        tmux_tbl,
        {"manage_options", "options", "server_options", "append_server_options"},
        source="tmux",
    )
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

    return Config(
        default_launch_user=default_launch_user,
        default_launch_mode=default_launch_mode,
        enable_all_users_list=enable_all_users_list,
        launch_user_by_caller=launch_user_by_caller,
        session_users=session_users,
        allowed_roots=allowed_roots,
        session_prefix=session_prefix,
        legacy_session_prefixes=legacy_session_prefixes,
        agents=agents,
        launch=launch,
        execution=execution,
        runtimes=runtimes,
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
    )
