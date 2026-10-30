where: src/uxon/app/launch.py (plan_worktree_launch), src/uxon/tui

why: The TUI honours `[container].on_missing_mode = "prompt"` for normal
launches (it shows a confirm before any start/create), but **not** for the
new-worktree launch path. The worktree directory does not exist until
after `git worktree add`, so its container name / `{dir}` cannot be
resolved up front — which is what the prompt affordance needs. So the
worktree path falls back to the headless auto-if-permitted policy
(`ensure_container_ready` inside `plan_worktree_launch`), exactly as the
CLI does. The CLI is non-interactive so this is correct there; in the TUI
it means a permitted start/create runs without the confirm the operator
configured. This is documented (configuration.md, the not-ready flow), not
a silent surprise — but it is a parity gap.

done when: the TUI new-worktree path can show the `on_missing` confirm
before running the start/create. This requires splitting create-from-
prepare: thread the computed worktree path out of `plan_worktree_launch`,
run `git worktree add`, then resolve the container and prompt, then
prepare — rather than the current single headless call. The CLI keeps its
auto-if-permitted behaviour unchanged.

Follow-up: TUI worktree-launch container prompt parity.
