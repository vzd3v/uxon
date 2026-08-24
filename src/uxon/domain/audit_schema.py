# SPDX-License-Identifier: MIT
"""Public audit-event field and outcome vocabulary for schema version 2."""

from __future__ import annotations

AUDIT_COMMON_OPTIONAL_FIELDS = frozenset({"correlation_id"})

AUDIT_EVENT_FIELDS: dict[str, frozenset[str]] = {
    "attach.remote.out.dispatch": frozenset(
        {"peer_name", "ssh_alias", "target_user", "target_session", "dry_run"}
    ),
    "cli.start": frozenset(
        {
            "flags",
            "profiles_enabled",
            "enable_all_users_list",
            "audit_enabled",
            "allowed_roots_count",
            "remote_hosts_count",
        }
    ),
    "config.error": frozenset({"path", "error"}),
    "config.render": frozenset({"input", "output", "error_type"}),
    "git.remote.create": frozenset(
        {"profile", "git_remote_profile", "repo", "creds_user", "launch_user", "rc"}
    ),
    "kill.remote.out": frozenset(
        {
            "peer_name",
            "ssh_alias",
            "target_user",
            "target_session",
            "force",
            "dry_run",
            "rc",
            "error",
        }
    ),
    "list.peek": frozenset({"scope_users", "scope_skipped"}),
    "list.remote.out": frozenset({"peer_name", "ssh_alias", "scope", "from_cache", "rc", "error"}),
    "runtime.prepare": frozenset({"action", "runtime_resource", "error", "error_type"}),
    "runtime.session_stop": frozenset(
        {"runtime", "runtime_resource", "action", "reason", "session", "target_user", "error"}
    ),
    "session.attach.dispatch": frozenset({"session", "target_user", "profile", "agent", "dry_run"}),
    "session.ended": frozenset({"session", "rc", "wall_seconds", "error"}),
    "session.kill": frozenset(
        {"session", "target_user", "profile", "agent", "force", "dry_run", "rc", "error"}
    ),
    "session.kill_all": frozenset(
        {
            "target_users",
            "attempted_count",
            "killed_count",
            "failed_count",
            "cleanup_failed_count",
            "dry_run",
        }
    ),
    "session.new": frozenset(
        {
            "profile",
            "agent",
            "target_user",
            "project",
            "branch",
            "session",
            "dry_run",
            "error",
            "error_type",
        }
    ),
    "tui.open": frozenset(),
    "worktree.create": frozenset(
        {"profile", "agent", "project", "branch", "path", "base", "session"}
    ),
}

AUDIT_EVENT_OUTCOMES: dict[str, frozenset[str]] = {
    "attach.remote.out.dispatch": frozenset({"ok", "not_found"}),
    "cli.start": frozenset({"ok"}),
    "config.error": frozenset({"error"}),
    "config.render": frozenset({"ok", "error"}),
    "git.remote.create": frozenset({"ok", "error"}),
    "kill.remote.out": frozenset({"ok", "error", "not_found"}),
    "list.peek": frozenset({"ok", "denied"}),
    "list.remote.out": frozenset({"ok", "error"}),
    "runtime.prepare": frozenset({"ok", "error"}),
    "runtime.session_stop": frozenset({"ok", "error", "skipped"}),
    "session.attach.dispatch": frozenset({"ok", "denied", "not_found"}),
    "session.ended": frozenset({"ok", "error"}),
    "session.kill": frozenset({"ok", "denied", "error", "not_found"}),
    "session.kill_all": frozenset({"ok", "error"}),
    "session.new": frozenset({"ok", "error"}),
    "tui.open": frozenset({"ok"}),
    "worktree.create": frozenset({"ok"}),
}
