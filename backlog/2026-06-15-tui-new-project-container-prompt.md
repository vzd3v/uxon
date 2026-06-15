where: src/uxon/tui/bridge.py (on_launch_new), src/uxon/tui

why: The TUI honours `[container].on_missing_mode = "prompt"` for normal
launches (it shows a confirm before any start/create), but **not** for the
new-project launch path. The new project's directory is created inside the
planner (`_plan_tui_create_new_agent`), so its container name / `{dir}`
cannot be resolved up front — which is what the prompt affordance needs. So
the new-project path falls back to the headless auto-if-permitted policy
(`ensure_container_ready` in `on_launch_new`), exactly as the CLI does. The
CLI is non-interactive so this is correct there; in the TUI it means a
permitted start/create runs without the confirm the operator configured.
This is the same parity gap as the new-worktree path
(`backlog/2026-06-15-tui-worktree-container-prompt.md`), on a sibling code
path.

done when: the TUI new-project path can show the `on_missing` confirm
before running the start/create. This requires creating the project
directory, then resolving the container and prompting, then preparing —
rather than the current single headless call after the planner has already
created the dir. The CLI keeps its auto-if-permitted behaviour unchanged.

Follow-up: TUI new-project container prompt parity.
