# Why uxon manages worktrees itself

uxon creates and owns every worktree (`git worktree add`, then
launches the agent in it — the tmux session's working directory *is*
the worktree path), rather than delegating to an agent's native
worktree flag. Three reasons:

- **Uniform across coding tools.** The same gesture works for any
  configured tool, whether or not it has its own worktree feature.
- **Consistent session ↔ worktree identity.** Every session's tmux
  working directory *is* its worktree path, so the attach-vs-new
  guard can match a session to a workspace reliably. Sessions are
  repo-qualified (`<repo>-<branch>`), so two repos with a
  same-named branch never collide.
- **Multi-user gating.** uxon places worktrees under paths it
  controls and checks them against the launch user's `allowed_roots`
  before any git work runs — the worktree inherits the same
  isolation boundary as every other launch.

Worktrees live under `<repo>/.uxon/worktrees/<branch-slug>/` and
are excluded from git automatically via `.git/info/exclude` — no
manual `.gitignore` edit. Setting `worktree_root` relocates them to
`<worktree_root>/<repo-slug>/<branch-slug>/`, outside the repo;
there the `.git/info/exclude` entry is unnecessary and is not
written.

## Worktree lifetime and freshness

1. **Worktrees outlive their coding-tool sessions.** Uxon creates them;
   removal is manual, so ending a session preserves the workspace.
2. **The base ref defaults to local (no fetch).** Uxon uses
   `origin/HEAD` if the
   local remote-tracking symref is present, otherwise `HEAD`, with
   no network I/O either way — because in the multi-user / `sudo`
   launch context an implicit per-create `git fetch` against a
   possibly private remote can hang or prompt for credentials.
   Enable fetching when you need the current remote base — see
   [`worktree_base`](../reference/configuration.md#top-level-keys).
