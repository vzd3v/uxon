# Work in a git worktree

Run an agent in an isolated worktree of a repo — a separate
directory on its own branch, sharing the repo's `.git`. uxon
creates and owns the worktree; it never calls the agent's native
worktree flag. For *why*, see
[Why uxon manages worktrees itself](../../explain/worktrees.md).

## From the TUI

1. Start uxon in (or open) a git repository.
2. Trigger a launch ("New session in current folder").
3. In the launch dialog, move to the **WORKSPACE** column with `→`.
4. Pick the primary tree, an existing worktree, or **+ New
   worktree…**. For a new worktree, type a branch name (`/` is
   allowed) and press Enter.
5. uxon creates the worktree and launches the agent there.

## From the CLI

```bash
uxon run -w feature/auth     # worktree for feature/auth in cwd's repo
uxon new myproj -w feature/x # same, for <new_project_root>/myproj
```

See the [`-w` reference](../../reference/cli.md#worktrees--w-branch)
for the full flag behaviour. Worktree location and the base ref for
new branches are set by
[`worktree_root` and `worktree_base`](../../reference/configuration.md#top-level-keys).

## Copying gitignored files into a new worktree

Add a `.worktreeinclude` file (`.gitignore` syntax) at the repo
root. On worktree creation uxon copies untracked, gitignored files
that match its patterns (e.g. `.env`) into the new worktree.

## Removing a worktree

Not yet a uxon gesture — remove manually:

```bash
git worktree remove .uxon/worktrees/<branch-slug>
```
