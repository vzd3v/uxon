# Why uxon manages worktrees itself

uxon creates and owns every worktree (`git worktree add`, then
launches the agent in it — the tmux session's working directory *is*
the worktree path), rather than delegating to an agent's native
worktree flag. Three reasons:

- **Uniform across agents.** The same gesture works for `claude`,
  `codex`, and `cursor`, even though only `claude` has a native
  `-w`.
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
manual `.gitignore` edit. (`claude -w` only advises a manual
`.gitignore` change.)

## Two deliberate deviations from `claude -w`

1. **uxon manages the worktree, not the agent.** uxon does not call
   `claude -w`; it runs `git worktree add` itself for every agent.
   The one behaviour not replicated is claude's exit-time
   auto-cleanup — worktree *removal* is manual for now.
2. **The base ref defaults to local (no fetch).** `claude -w`
   fetches `origin` by default so the new branch tracks the latest
   remote. uxon defaults to a local base because in the
   multi-user / `sudo` launch context an implicit per-create
   `git fetch` against a possibly private remote can hang or prompt
   for credentials. Switch to remote for claude-like freshness — see
   [`worktree_base`](../reference/configuration.md#top-level-keys).
