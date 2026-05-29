# Worktree support (3.5.0) — design

Native, first-class git-worktree support in `uxon`: create worktrees,
attach to existing ones, and remove them — for `claude` (native `-w` by
default) and `codex` (uxon-managed). Surfaced by extending the launch
flow with a workspace column; the existing `SessionChoice` modal stays
as the attach-vs-new guard.

Status: approved design, pre-plan.

---

## 1. Background

`uxon` is a multi-user `tmux` wrapper for AI coding agents, typically
installed globally and run by several users; agents run as `launch_user`
via `sudo`. Worktrees let several agents edit one repository in parallel,
each on its own branch and directory, sharing one `.git`.

Native agent support:

- **Claude Code** — native `--worktree` / `-w`. `claude -w feature-auth`
  creates `.claude/worktrees/feature-auth/` **inside the repo**, branch
  `worktree-feature-auth` off `origin/HEAD`, starts the session there.
  Supports `-w` with no name (generated), `-w "#1234"` (from a PR),
  `.worktreeinclude` copying of gitignored files, and cleanup-on-exit
  prompts.
- **Codex CLI** — no native worktree flag. Only manual `git worktree add`
  + `cd && codex`.
- **Cursor** — out of scope.

### Current state in uxon

- A CLI-only `-w/--worktree <branch>` flag exists, **claude-only**, that
  passes `-w <branch>` straight to the `claude` binary (delegates to
  claude's native mechanism). It launches tmux with `-c <repo_root>` and
  lets claude re-root into the worktree.
- Session naming is already worktree-aware:
  `session_stem_for_worktree(repo_root, branch)`.
- **No TUI integration** — worktrees cannot be created/attached from the
  TUI.
- `SessionChoiceScreen` (commit ff383e5) is the just-shipped attach-vs-new
  modal, fired when a compatible session already exists for a launch
  target. Its purpose: stop accidental duplicate launches and stop the
  old silent auto-attach that ignored the operator's permission mode.

---

## 2. Key design decisions

### 2.1 Ownership — hybrid, config-toggled

New config key **`claude_native_worktree`** (bool, default `true`)
selects the worktree-creation backend **for claude**:

- `true` (default): claude worktrees are created natively via
  `claude -w <name>`. Claude owns the directory, branch naming, cleanup
  prompts, `.worktreeinclude`, and the `-w` extras (no-name generation,
  `#PR`).
- `false`: claude worktrees are created by uxon (`git worktree add` +
  `claude -c <path>`).

**codex and any other non-native agent are always uxon-managed** — the
toggle is ignored for them.

The native-vs-uxon decision is made in **one place**: the launch
planner. Rationale: native default gives claude users the behaviour they
already expect for free; uxon-managed creation makes the feature work for
codex and for a uniform agent-agnostic layout.

### 2.2 Listing is always unified via git

`git worktree list --porcelain` (run as `launch_user` in the repo) is
the single source of truth for *existing* worktrees, regardless of who
created them — claude's `.claude/worktrees/...`, uxon's
`.uxon/worktrees/...`, or manual `git worktree add`. **No uxon-side
worktree registry.** The ownership decision in 2.1 governs only
*creation*. The same porcelain output gives the **branch of each
worktree, including the primary working tree** (see §3).

### 2.3 Disk layout for uxon-managed worktrees — inside the repo

**Default: `<repo>/.uxon/worktrees/<branch-slug>/`**, with `.uxon/`
added to `.gitignore`.

This is decided by uxon's multi-user model, not aesthetics. `git worktree
add` writes both to the new worktree directory **and** to the main repo's
`.git/worktrees/<name>/` (registration), so `launch_user` must be able to
write the repo's `.git/` — already a precondition (they launch an agent
to edit it). Given that, the worktree directory must also be writable by
`launch_user` and pass `allowed_roots` gating. Comparing locations for a
globally-installed multi-user tool:

| Location | Writable by launch_user | Inside `allowed_roots` | Per-user isolation | Nesting |
|---|---|---|---|---|
| **Inside repo `.uxon/worktrees/`** | always (repo already writable) | always (repo is the launch target) | per-repo | yes (gitignored) |
| Sibling in repo parent | parent often root/shared → no | usually | yes | no |
| User home `~/.uxon/worktrees` | yes | **fails under a strict whitelist** | yes | no |
| Global dir | needs perms on a shared dir | must be added to roots | needs per-user namespacing | no |

Inside-repo is the only location that satisfies write + `allowed_roots` +
isolation **with zero extra configuration in every deployment**. Every
external location breaks at least one of those in some deployment
(unwritable shared parent, or a worktree outside the audited
`allowed_roots`). `.uxon/` is uxon's own namespace dir (mirrors
`.claude/`, `.git/`), so we are not imposing a generic top-level dir on a
user's repo. This is the same choice claude makes (`.claude/worktrees/`)
and for the same reason.

- **Override:** config key **`worktree_root`** (default empty). When set:
  `<worktree_root>/<repo-slug>/<branch-slug>/`. This covers the
  home/central/sibling models for an admin who deliberately wants them and
  will ensure both write permission and `allowed_roots` membership.
- **Gating:** the computed path is always run through
  `ensure_launch_target_allowed`; outside `allowed_roots` → clear error
  suggesting `worktree_root`. No silent fallback.

Native-claude worktrees still land in `.claude/worktrees/` (claude's
choice, not ours); `git worktree list` shows both. Accepted.

### 2.4 `.worktreeinclude` (copying gitignored files)

A fresh worktree lacks untracked files like `.env`. We support copying
them:

- **Native claude:** handled by claude itself — no uxon work.
- **uxon-managed (codex, or claude with the toggle off):** uxon copies
  gitignored files matching `.worktreeinclude` (`.gitignore` syntax) into
  the new worktree. Only files that match a pattern **and** are gitignored
  are copied (never tracked files), matching claude's semantics.

### 2.5 Removal — guarded, in scope

uxon provides an explicit, guarded remove gesture (both uxon-managed and
manual worktrees are real git worktrees):

- A key binding on a worktree row → confirmation → `git worktree remove`
  (as `launch_user`).
- **Refuse** if the worktree has uncommitted changes, untracked files, or
  unpushed commits, unless force-confirmed. Optionally delete the branch.
- **Never auto-delete.**
- **Native-claude worktrees** (`.claude/worktrees/...`) with a **live
  claude session** are not offered uxon-remove — claude owns their
  lifecycle and prompts on exit (see Known edges).

---

## 3. Unified launch screen

The launch flow stays as today (action → launch-options → optional
`SessionChoice` guard → launch), with one change: **the launch-options
screen grows a third column** so agent, permission mode, and target
workspace are chosen on one screen.

Columns; ←/→ move between columns, ↑/↓ within a column, Enter commits,
Esc cancels:

```
┌ Launch · myapp ──────────────────────────────────────────────────────┐
│  AGENT          PERMISSION         WORKSPACE                           │
│ ▸ claude        ▸ default          ▸ dev          (primary)            │
│   codex           auto               feature-auth                      │
│   cursor          plan               bugfix-123                        │
│                   danger             + New worktree…                    │
│                                                                        │
│  ←/→ panel · ↑/↓ move · Enter · Esc cancel                             │
└──────────────────────────────────────────────────────────────────────┘
```

### Columns

- **AGENT** — current left panel (enabled + available agents). Hidden
  when a single agent is enabled (unchanged).
- **PERMISSION** — current right panel (modes for the focused agent).
  Rebuilt on agent change (unchanged).
- **WORKSPACE** — new. **Folder selection, not session selection.** Rows:
  the primary working tree + one row per existing worktree (from
  `git worktree list`) + a `+ New worktree…` row.

### WORKSPACE rows

- The **first row is the primary working tree** (the repo's main
  checkout), labelled by its **actual current branch** plus `(primary)` —
  taken from `git worktree list --porcelain`, never hard-coded to `main`
  (the primary tree can be on any branch or detached). Default highlight.
- Each other row is an existing worktree, labelled by its branch.
- `+ New worktree…` is the create affordance.
- **No session markers and no session list on this screen** — workspaces
  are folders. Attaching to a specific existing session is the main
  dashboard's job (every session is a row there; Enter attaches).

### What Enter does

Selecting a workspace + Enter commits a launch into that folder with the
chosen agent + mode. The existing attach-vs-new guard is preserved
unchanged: at commit time uxon probes for a compatible session in that
**folder + agent** (the current `probe_tui_compatible_sessions` path); if
one exists, the existing `SessionChoiceScreen` modal appears (`a` attach /
`n` new alongside / Esc). If none, it launches directly. This keeps the
ff383e5 behaviour (no silent auto-attach, mode respected on the `new`
branch) and does not move that modal onto the unified screen.

`+ New worktree…` + Enter → branch-name input → planner decides native vs
uxon-managed → create → same commit path (guard, then launch).

### New-worktree input semantics

- **Native claude (`claude_native_worktree=true`):** passes through to
  `claude -w <value>`. Empty → claude generates a name; `#1234` → from
  that PR; otherwise a named worktree.
- **uxon-managed:** the input is a branch name (required — no empty / no
  `#PR` this release). If the branch exists,
  `git worktree add <path> <branch>` (checkout); otherwise
  `git worktree add <path> -b <branch>` (new branch off `origin/HEAD`,
  fallback to local `HEAD`). Then `.worktreeinclude` copying, then launch
  with `-c <path>`.

### Degradation

- Single agent → AGENT column hidden (unchanged).
- Non-git target → no WORKSPACE column; the flow is exactly as today
  (launch-options → `SessionChoice` guard if a session exists).

---

## 4. Architecture

### 4.1 Launch planner

A single planner decides the backend and builds the launch:

```
plan_worktree_launch(cfg, launch_user, repo_root, branch_or_name,
                     agent_id, mode_id) -> LaunchRequest
```

- claude + `claude_native_worktree=true` → tmux launch with
  `-c <repo_root>` and `claude … -w <name>` (native; the current
  passthrough, generalised).
- otherwise → compute uxon path (§2.3), gate it, `git worktree add` as
  `launch_user`, copy `.worktreeinclude` matches, tmux launch with
  `-c <worktree_path>` (no agent `-w`).

Both the TUI new-worktree path and the CLI `-w` flag go through this
planner. The existing "`-w` only for claude" guard is removed.

### 4.2 Workspace probe

New TUI callback to populate the WORKSPACE column:

```
on_probe_worktrees(repo_root) -> list[Workspace]
```

`Workspace = (label, branch, path, is_primary)`, parsed from
`git worktree list --porcelain` (as `launch_user`). **Folders only — no
session data.** A non-git target returns an empty list (no WORKSPACE
column). The attach-vs-new session probe stays the existing
`probe_tui_compatible_sessions(folder, agent)` at commit time — unchanged,
not per-keystroke.

### 4.3 Multi-user

All git operations (`rev-parse`, `worktree list`, `worktree add`,
`worktree remove`, status checks) run under
`command_prefix_for_user(launch_user)` (interactive, launch-time) or
`nonint_command_prefix_for_user` (background/probe), consistent with
`git_repo_root_as_user`.

### 4.4 Pure helpers (testable, no Textual)

Per the TUI test policy, branchy logic lives in pure helpers in
`tui/state.py` (or a sibling pure module):

- worktree path computation (default `.uxon/worktrees` vs `worktree_root`).
- branch/name → slug.
- `git worktree list --porcelain` parsing (fed raw text → workspaces with
  branch + is_primary), unit-tested without spawning git.
- remove eligibility (clean vs dirty/untracked/unpushed).

### 4.5 Config keys (5-step process in conventions.md)

- `claude_native_worktree: bool = true`
- `worktree_root: str = ""` (empty → default `.uxon/worktrees` layout)

Each: extend `DEFAULT_CONFIG` / `Config` / `load_config`; validation;
`SettingSpec` in `settings.py`; doc entry in
`docs/reference/configuration.md`; `load_config` + round-trip tests.

---

## 5. CLI parity

`uxon -w/--worktree <branch>` (and `new -w`) route through
`plan_worktree_launch`, so CLI and TUI share one backend. Behaviour
change: `-w` is no longer claude-only, and when uxon-managed it creates a
`<repo>/.uxon/worktrees/...` worktree instead of delegating to claude.
Note in CHANGELOG.

---

## 6. Known edges

- **Slug path collision (D):** two branch names that slugify to the same
  directory (e.g. `feature/auth` and `feature-auth`). `git worktree add`
  fails when the path is taken; uxon catches it and shows a clear
  "worktree path already exists — pick another name" error. No silent
  directory reuse.
- **Native trust dialog (E):** native `claude -w` errors (instead of
  prompting) the **first time** claude runs in a repo where workspace
  trust was never accepted. Narrow case (a repo where claude never ran,
  first launch via `-w`). Not silent — claude prints the error and the
  session exits non-zero, surfaced by the existing launch-failure pause
  banner (`tui/launch.py`). We do **not** parse claude's stderr to
  pre-detect it (that would be a hack). uxon-managed mode does not have
  this edge (a fresh `claude -c <path>` shows the trust prompt
  interactively). Documented; the workaround is `claude_native_worktree=
  false` or running `claude` once in the repo.
- **Remove vs claude ownership (G):** for `.claude/worktrees/...`
  worktrees with a live claude session, uxon does not offer remove —
  claude owns their cleanup.

---

## 7. Out of scope (this release)

- `#PR` / empty-name worktrees in the **uxon-managed** path (native claude
  still supports them via passthrough).
- Remote (peer) worktree creation — local-repo gesture only.
- **Repo-config consolidation:** moving project config `.uxon.toml` into
  `.uxon/config.toml`. Good for tidiness once `.uxon/` exists, but it is an
  orthogonal breaking change (`find_project_config` discovery, back-compat
  for deployed `.uxon.toml`, docs/migration) and must not be bundled into
  worktree work. Backlog, with dual-path + migration.

---

## 8. Testing

- Pure unit tests for every helper in §4.4 (path computation, slug,
  porcelain parsing incl. primary branch + detached HEAD, remove
  eligibility, collision detection).
- `load_config` + settings round-trip tests for the two config keys.
- CLI dry-run tests for `-w` in both backends (native passthrough vs
  uxon `git worktree add`).
- One `Pilot` smoke test for the extended launch screen: agent change
  rebuilds permission; WORKSPACE lists primary `(primary)` + worktrees +
  `+ New worktree…`; Enter on a workspace commits and (when a session
  exists) the existing `SessionChoice` guard appears.
- Keep branchy assertions in pure tests; reserve `Pilot` for wiring /
  focus / async behaviour, per the TUI test policy.
