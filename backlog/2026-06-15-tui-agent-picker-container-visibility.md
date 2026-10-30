where: src/uxon/tui/state.py (visible_agent_ids, launch_options_state,
update_launch_options_after_availability), src/uxon/tui/screens/launch_options.py

why: With `[container].enabled`, the agent runs inside the operator's
container, so a binary absent from the host PATH is not a launch blocker —
host presence is suppressed on the launch path and in the CLI agent
resolver. But the TUI LaunchOptions **agent picker** still populates its
visible agents from host availability: `visible_agent_ids` filters out
enabled agents whose host probe reported `missing`/`timeout` (strict mode)
and shows only available agents (auto mode). Under container mode a
host-absent agent reads as `missing`, so the picker can end up empty even
though the launch is perfectly valid (the agent runs in the container). The
CLI path is unaffected — it resolves the agent directly and the host
presence gate is already suppressed under container mode.

done when: under `[container].enabled` the LaunchOptions agent picker shows
the resolvable agents (configured / catalogued) regardless of host
presence, mirroring the CLI behaviour — while keeping host-presence
filtering unchanged when container mode is disabled.

Follow-up: TUI agent-picker visibility under container mode.
