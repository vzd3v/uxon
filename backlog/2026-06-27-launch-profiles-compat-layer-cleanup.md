where: src/uxon/domain/config.py (`container: ContainerConfig` field,
`enabled_agents`, `default_agent`), src/uxon/app/launch.py:122-127 (compat branch),
src/uxon/app/kill.py (kill_caveat(cfg.container)),
src/uxon/tui/context_builder.py (container_kill_caveat),
src/uxon/infra/container.py (legacy resolve_container / plan_container_launch /
resolve_container_identity / current_container_epoch), src/uxon/app/launch.py
worktree.create audit emission.

why: The launch-profiles redesign (4.0.0) routes every NEW managed launch
through a ResolvedLaunchProfile and selects container runtimes via per-profile
container_profiles. The pre-profiles singleton `[container]` / `cfg.container`
surface was RETAINED as a compat shim rather than removed: it is now always a
bare disabled `ContainerConfig()` (config_loader builds `ContainerConfig()`),
so `kill_caveat(cfg.container)` / `container_kill_caveat(cfg.container)` always
return `None` and the legacy `resolve_container` / `plan_container_launch` /
`resolve_container_identity` / `current_container_epoch` helpers are unreachable
on the resolved path. They are NOT dead, however — they remain exercised by ~38
test usages (ContainerConfig fixtures + resolve_container calls in
tests/test_uxon_container.py and the integration suite, which use
build_container_config). Removing them is therefore a test-rewriting refactor,
not a delete, so it was deferred out of the 4.0.0 finish. Separately, the Config
fields `enabled_agents` / `default_agent` now hold agent-ids-DERIVED-from-enabled-
profiles (config_loader:628-633), consumed by the TUI for mode/availability
probing (plan P6 permits this), but the names read like the old selector and
will mislead future readers. And the `worktree.create` audit event emits `agent`
but not `profile`, while its sibling `session.new` emits both — a spec-parity
question (R12); today doc and code agree (both omit `profile` there).

done when: the inert `cfg.container` singleton and its legacy container helpers
are removed (Config field dropped; kill_caveat / container_kill_caveat call sites
made profile-aware or deleted; the launch.py compat branch removed; the ~38 test
usages rewritten to the profile-scoped equivalents); `enabled_agents` /
`default_agent` either renamed to reflect they are profile-derived agent-catalog
data, or the TUI reads `cfg.launch` directly; and `worktree.create` either
carries `profile` for parity with `session.new`, or the audit-events doc notes
why it is agent-only.

Follow-up: launch-profiles compat-layer cleanup (cfg.container singleton removal
+ enabled_agents rename + worktree.create profile parity).
