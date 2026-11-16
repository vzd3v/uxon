# Work in a git worktree

Run an agent in an isolated worktree of a repo — a separate
directory on its own branch, sharing the repo's `.git`. uxon
creates and owns the worktree; it never calls the agent's native
worktree flag. For *why*, see
[Why uxon manages worktrees itself](../../explain/worktrees.md).

## From the TUI

1. Start uxon in (or open) a git repository.
2. Trigger a launch — either **Open in this folder** (when uxon
   runs inside the repo) or **Open project** (for a git project
   under `new_project_root`). Git discovery starts in the background while
   browsing and choosing a profile; status and errors appear inline.
3. Choose the launch profile and permission mode. Enter does not wait for Git:
   pending, failed or non-git discovery continues in the selected folder.
   Ready discovery opens the workspace screen; late results never open it.
4. Keep **Current checkout** (the default), pick another workspace, or **+ New
   worktree…**. For a new worktree, type a branch name (`/` is
   allowed) and press Enter.
5. Uxon launches in the selected workspace, creating a worktree only for
   **+ New worktree…**.

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
Only ordinary files are copied. Symlink traversal, existing destination files
and tracked destination conflicts are rejected; source file permissions are preserved.
Directory access follows the destination checkout, including for copied files
whose source access relied on a private parent directory.
Uncommitted tracked changes are not transferred.

If a step after creation fails, the error names the retained worktree and failed
stage. Inspect that directory and open it again after resolving the problem;
Uxon does not automatically delete the checkout or branch.

## Removing a worktree

Not yet a uxon gesture — remove manually, from the repo root:

```bash
git worktree remove .uxon/worktrees/<branch-slug>
```

(If you set `worktree_root`, the path is
`<worktree_root>/<repo-slug>/<branch-slug>` instead — run
`git worktree list` to see the exact path.)
