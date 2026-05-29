# Worktree support (3.5.0) — design

Native, first-class git-worktree support in `uxon`: create worktrees,
attach to existing ones, and remove them — for `claude` (native `-w` by
default) and `codex` (uxon-managed). Surfaced through a single unified
launch screen that replaces the `LaunchOptions` → `SessionChoice` modal
chain.

Status: approved design, pre-plan.

---

## 1. Background

`uxon` is a multi-user `tmux` wrapper for AI coding agents. Worktrees
let several agents edit the same repository in parallel without
colliding — each on its own branch, its own directory, sharing one
`.git`. By Q1 2026 worktrees became load-bearing for parallel AI
coding; the major agents added support:

- **Claude Code** — native `--worktree` / `-w`. `claude -w feature-auth`
  creates `.claude/worktrees/feature-auth/`, branch `worktree-feature-auth`
  off `origin/HEAD`, starts the session there. Supports `-w` with no name
  (generates one), `-w "#1234"` (from a PR), `.worktreeinclude` copying of
  gitignored files, and prompts for cleanup on exit.
- **Codex CLI** — no native worktree flag (open upstream request). Only
  manual `git worktree add` + `cd && codex`.
- **Cursor** — out of scope.

### Current state in uxon

- A CLI-only `-w/--worktree <branch>` flag exists, **claude-only**, that
  passes `-w <branch>` straight to the `claude` binary — i.e. delegates
  entirely to claude's native mechanism. It launches tmux with
  `-c <repo_root>` and lets claude re-root into the worktree.
- Session naming is already worktree-aware:
  `session_stem_for_worktree(repo_root, branch)`.
- **No TUI integration whatsoever** — worktrees cannot be created or
  attached from the dashboard or the launch flow.

### The gap

The TUI has no worktree story. The recently added `SessionChoiceScreen`
(attach-vs-new modal) is the natural place to fold worktrees in, but
they are a different axis (a repo's worktrees) from what that modal
covers (one directory's sessions).

---

## 2. Key design decisions

### 2.1 Ownership — hybrid, config-toggled

A new config key **`claude_native_worktree`** (bool, default `true`)
selects the worktree-creation backend **for claude**:

- `true` (default): claude worktrees are created natively via
  `claude -w <name>`. Claude owns the directory (`.claude/worktrees/`),
  branch naming (`worktree-<name>`), cleanup-on-exit prompts,
  `.worktreeinclude` handling, and the `-w` extras (no-name generation,
  `#PR`).
- `false`: claude worktrees are created by uxon (`git worktree add` +
  `claude -c <path>`).

**codex and any other non-native agent are always uxon-managed** — the
toggle is ignored for them.

The native-vs-uxon decision is made in **one place**: the launch
planner. No backend branching scattered through the code.

**Rationale.** Native default gives claude users the behaviour they
already expect (cleanup prompts, `.worktreeinclude`, PR worktrees) for
free, while uxon-managed creation makes the feature work for codex and
for users who want a uniform, agent-agnostic layout.

### 2.2 Listing is always unified via git

`git worktree list --porcelain` (run as `launch_user` in the repo) is
the single source of truth for *existing* worktrees, regardless of who
created them — claude's `.claude/worktrees/...`, uxon's `.worktrees/...`,
or manual `git worktree add`. **No uxon-side worktree registry.** The
ownership decision in 2.1 only governs *creation*.

### 2.3 Disk layout for uxon-managed worktrees

Best-practice consensus (git docs, gitworktree.org, 2026 worktree
managers) is that worktrees should be **siblings outside** the repo
tree, never nested inside it — nesting breaks tools that walk up the
tree to find `.git` and causes duplicate-tree scanning by watchers /
linters / build context. (Claude's `.claude/worktrees/` is a sanctioned
exception because claude manages and ignores it; uxon should follow the
sibling convention.)

- **Default:** `<repo_parent>/.worktrees/<repo-name>/<branch-slug>/` — a
  hidden `.worktrees` directory beside the repo (i.e. under
  `new_project_root` when the repo lives there), grouped by repo then
  branch. Outside every repo tree (no nested scan); inside
  `allowed_roots` (same parent as the repo); hidden from the
  existing-project picker, which already skips dot-directories.
- **Override:** config key **`worktree_root`** (default empty). When
  set: `<worktree_root>/<repo-slug>/<branch-slug>/`.
- **Gating:** the computed path is always run through
  `ensure_launch_target_allowed`. If it falls outside `allowed_roots`,
  fail with a clear message suggesting `worktree_root`. No silent
  fallback to a nested layout.

### 2.4 `.worktreeinclude` (copying gitignored files)

A fresh worktree lacks untracked files like `.env`. Decision: support
copying them.

- **Native claude:** handled by claude itself — no uxon work.
- **uxon-managed (codex, or claude with the toggle off):** uxon
  implements `.worktreeinclude` copying using `.gitignore` syntax, so
  behaviour matches claude. Only files that match a pattern **and** are
  gitignored are copied (never tracked files).

### 2.5 Removal — guarded, in scope

uxon provides an explicit, guarded remove gesture (works for both
uxon-managed and native-claude worktrees, since both are real git
worktrees):

- A key binding on a worktree row (in the unified launch screen and/or
  the session dashboard) → confirmation → `git worktree remove`.
- **Refuse** if the worktree has uncommitted changes, untracked files,
  or unpushed commits (mirrors claude's safety), unless force-confirmed.
- Optionally delete the branch too.
- **Never auto-delete.**

---

## 3. Unified launch screen

Replace the `LaunchOptions` → `SessionChoice` two-modal chain with a
**single columnar screen**. Columns, with ←/→ to move between columns,
↑/↓ within a column, Enter to commit, Esc to cancel:

```
┌ Launch · myapp ──────────────────────────────────────────────────────┐
│  AGENT          PERMISSION         WORKSPACE                           │
│ ▸ claude        ▸ default          ▸ main           ● attached         │
│   codex           auto               feature-auth   ○                  │
│   cursor          plan               bugfix-123      ●                  │
│                   danger             + New worktree…                    │
│                                                                        │
│  ←/→ panel · ↑/↓ move · Enter launch/attach · Esc cancel               │
└──────────────────────────────────────────────────────────────────────┘
```

### Columns

- **AGENT** — the current `LaunchOptions` left panel (enabled +
  available agents). Hidden when a single agent is enabled (unchanged).
- **PERMISSION** — the current `LaunchOptions` right panel (permission
  modes for the focused agent). Rebuilt on agent change (unchanged).
- **WORKSPACE** — new. Rows: `main` (repo root) + one row per existing
  worktree (from `git worktree list`) + a `+ New worktree…` row. Default
  highlight is `main`.

### Behaviour

- Each workspace row carries a session marker for the **currently
  highlighted agent** (sessions are per-agent):
  - `●` a compatible session exists → Enter **attaches** (to the most
    recent; permission mode is irrelevant on attach).
  - `○` none → Enter **launches** a new session there with the chosen
    agent + permission mode.
- `+ New worktree…` + Enter → inline branch-name input → planner decides
  native vs uxon-managed → create → launch with the chosen agent + mode.
- **Reactivity:** changing the highlighted AGENT rebuilds PERMISSION (as
  today) **and** recomputes the `●/○` markers in WORKSPACE. The worktree
  *list* is agent-independent (git); only the session markers depend on
  the agent.
- **Degradation:**
  - Single agent → AGENT column hidden.
  - Non-git target → WORKSPACE column shows only the folder's sessions +
    a "new" affordance (the former `SessionChoice` behaviour, as a
    column).
  - Git repo with no worktrees and no sessions → WORKSPACE column is just
    `main` + `+ New worktree…`; no extra clicks.

This collapses two modals into one screen and unifies agent / mode /
workspace / attach / new-worktree selection.

### New-worktree input semantics

- **Native claude (`claude_native_worktree=true`):** the input passes
  through to `claude -w <value>`. Empty value → claude generates a name;
  `#1234` → claude creates from that PR; otherwise a named worktree.
- **uxon-managed:** the input is a branch name (required — no empty / no
  `#PR` in this release). If the branch exists,
  `git worktree add <path> <branch>` (checkout); otherwise
  `git worktree add <path> -b <branch>` (new branch off `origin/HEAD`,
  fallback to local `HEAD`). Then `.worktreeinclude` copying, then launch
  with `-c <path>`.

---

## 4. Architecture

### 4.1 Launch planner

A single planner function decides the worktree backend and builds the
launch:

```
plan_worktree_launch(cfg, launch_user, repo_root, branch_or_name,
                     agent_id, mode_id) -> LaunchRequest
```

- claude + `claude_native_worktree=true` → tmux launch with
  `-c <repo_root>` and `claude … -w <name>` (native; current passthrough
  behaviour, generalised).
- otherwise → compute uxon path (§2.3), gate it, `git worktree add` as
  `launch_user`, copy `.worktreeinclude` matches, tmux launch with
  `-c <worktree_path>` (no agent `-w`).

Both the TUI callback and the CLI `-w` path go through this planner. The
existing "`-w` only for claude" guard is removed.

### 4.2 Probe callback

New TUI callback:

```
on_probe_workspaces(repo_root, agent_id) -> list[Workspace]
```

where `Workspace = (label, path, sessions: tuple[SessionInfo, ...])`,
built from `git worktree list --porcelain` (as `launch_user`) plus
`compatible_indexed_sessions` per workspace directory. A non-git target
returns a single workspace (the directory itself). Generalises the
current `on_probe_existing_sessions`.

### 4.3 Multi-user

All git operations (`rev-parse`, `worktree list`, `worktree add`,
`worktree remove`, status checks) run under
`command_prefix_for_user(launch_user)` (interactive, launch-time) or
`nonint_command_prefix_for_user` (background probes), consistent with
`git_repo_root_as_user`.

### 4.4 Pure helpers (testable, no Textual)

Per the project's TUI test policy, branchy logic lives in pure helpers
in `tui/state.py` (or a sibling pure module):

- worktree path computation (default sibling vs `worktree_root`).
- branch/name → slug.
- workspace-model assembly from a `git worktree list` parse + per-dir
  session matches.
- the "skip / collapse" decisions (when WORKSPACE degrades to `main`).
- remove eligibility (clean vs dirty/untracked/unpushed).

Pure git porcelain **parsing** also lives in a pure function fed raw
porcelain text, so it is unit-tested without spawning git.

### 4.5 Config keys (follow the 5-step process in conventions.md)

- `claude_native_worktree: bool = true`
- `worktree_root: str = ""` (empty → default sibling layout)

Each: extend `DEFAULT_CONFIG` / `Config` / `load_config`; validation;
`SettingSpec` in `settings.py`; doc entry in
`docs/reference/configuration.md`; `load_config` + round-trip tests.

---

## 5. CLI parity

`uxon -w/--worktree <branch>` (and `new -w`) route through
`plan_worktree_launch`, so CLI and TUI share one backend. Behaviour
change: `-w` is no longer claude-only and, when uxon-managed, creates a
sibling `.worktrees/...` worktree instead of delegating to claude. Note
in CHANGELOG.

---

## 6. Out of scope (this release)

- `#PR` / empty-name worktrees in the **uxon-managed** path (native
  claude still supports them via passthrough).
- Remote (peer) worktree creation — worktrees are a local-repo gesture;
  no SSH variant.
- Bare-clone / central-worktree layouts beyond the `worktree_root`
  override.

---

## 7. Testing

- Pure unit tests for every helper in §4.4 (path computation, slug,
  porcelain parsing, workspace assembly, degrade decisions, remove
  eligibility).
- `load_config` + settings round-trip tests for the two config keys.
- CLI dry-run tests for `-w` in both backends (native passthrough vs
  uxon `git worktree add`).
- One `Pilot` smoke test for the unified launch screen: agent change
  refreshes permission + workspace markers; Enter attaches on `●`,
  launches on `○`; `+ New worktree…` opens the input.
- Keep branchy assertions in pure tests; reserve `Pilot` for wiring /
  focus / async-worker behaviour, per the TUI test policy.
