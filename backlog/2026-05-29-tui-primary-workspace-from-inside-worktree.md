# Primary WORKSPACE row launches into cwd when TUI started inside a linked worktree

where: `src/uxon/tui/screens/main.py` (launch dispatch) + `src/uxon/cli.py` (`_plan_tui_run_agent`, primary path)

why: When the uxon TUI is launched from inside a linked worktree, selecting the primary WORKSPACE row launches into `cwd` (the current worktree) rather than the resolved primary repo root, because the primary path reuses the standard path-based planner (spec §3 "unchanged").

done when: selecting the primary WORKSPACE row launches into the resolved primary `repo_root` regardless of which worktree the TUI was started from.

Rare and non-corrupting: it only misfires when the operator opens the TUI from within a linked worktree *and* picks the primary row, and at worst it launches in the current worktree instead of the primary tree. Candidate fix: route the primary row through its resolved `repo_root` (via `git_common_dir_root_as_user`, already used to anchor new worktrees) instead of `cwd`. Deferred from the worktree-support change to avoid widening the primary-launch path, which spec §3 deliberately left unchanged.
