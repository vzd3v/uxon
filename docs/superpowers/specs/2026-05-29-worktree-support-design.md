# Worktree support (3.5.0) — design

Native, first-class git-worktree support in `uxon`: create worktrees,
attach to existing ones, and remove them — **uniformly across all agents**
(`claude`, `codex`), managed by uxon itself rather than delegating to any
agent's native worktree flag. Surfaced by extending the launch flow with a
workspace column; the existing `SessionChoice` modal stays as the
attach-vs-new guard.

Status: approved design, pre-plan.

---

## 1. Background

`uxon` is a multi-user `tmux` wrapper for AI coding agents, typically
installed globally and run by several users; agents run as `launch_user`
via `sudo`. Worktrees let several agents edit one repository in parallel,
each on its own branch and directory, sharing one `.git`.

Native agent support today:

- **Claude Code** — native `--worktree` / `-w`: creates
  `.claude/worktrees/<name>`, branch `worktree-<name>`, with extras
  (baseRef, `#PR`, name generation, `.worktreeinclude`, exit-time
  cleanup). uxon **deliberately does not use this** — see §2.1.
- **Codex CLI** — no native worktree flag.
- **Cursor** — out of scope.

### Current state in uxon

- A CLI-only `-w/--worktree <branch>` flag exists, **claude-only**, that
  passes `-w <branch>` to the `claude` binary (delegates to claude's
  native mechanism) and launches tmux with `-c <repo_root>`. This design
  replaces that delegation with a uxon-managed implementation.
- Session naming is already worktree-aware:
  `session_stem_for_worktree(repo_root, branch)`.
- **No TUI integration** — worktrees cannot be created/attached from the
  TUI.
- `SessionChoiceScreen` (commit ff383e5) is the attach-vs-new modal, fired
  when a compatible session already exists for a launch target. Purpose:
  stop accidental duplicate launches and the old silent auto-attach that
  ignored the operator's permission mode.

---

## 2. Key design decisions

### 2.1 Single backend — uxon manages all worktrees

uxon creates and manages worktrees itself for **every** agent:
`git worktree add` (as `launch_user`) + launch the agent with
`-c <worktree_path>` and **no** agent-native worktree flag. There is no
per-agent backend and no config toggle.

**Why not claude's native `-w`.** We evaluated delegating to `claude -w`
for claude. A uniform uxon implementation loses almost nothing and gains
consistency:

| Native `-w` behaviour | uxon replication | Outcome |
|---|---|---|
| Create worktree + branch | `git worktree add` (our layout) | equal |
| baseRef `fresh`/`head` (branch off `origin/HEAD` vs local `HEAD`) | `worktree_base` config, default `local` (no fetch) — deliberate deviation, see §4.5 | replicated (network-free by default) |
| `.worktreeinclude` copying | implemented by uxon (§2.4) | equal |
| Subagent worktree isolation, `EnterWorktree` | claude-internal, independent of launch method | unaffected |
| Trust dialog | a plain `claude -c` prompts interactively | improved (no first-run error) |
| `#PR` worktrees, auto-generated name | deferred (§7) | minor, niche |
| Exit-time auto-cleanup (clean → remove, dirty → prompt) | replaced by uxon's explicit guarded remove (§2.5) | the only real trade |

The only behaviour not cheaply replicable is claude's exit-time
auto-cleanup — and it applies only to `-w`-created worktrees, is
known-incomplete in practice (anthropics/claude-code #26725, #31488),
is absent for codex anyway, and is superseded by uxon's explicit remove.

Decisive benefit: **uniformity makes session↔worktree identity
consistent.** Every session launches with `-c <worktree_path>`, so a
session's tmux cwd *is* its worktree path. (Delegating to native `-w`
would have launched with `-c repo_root`, leaving sessions distinguishable
only by name-stem and breaking the attach guard.) The exact naming and
matching scheme is specified in §2.6 — it must be **repo-qualified and
identical at create and probe**, or the attach guard misses / hard-fails
across repos.

**This is a behaviour change for the existing `-w` flag and MUST be
documented** as an explicit project decision (§6).

### 2.2 Listing via git

`git worktree list --porcelain` (as `launch_user` in the repo) is the
single source of truth for existing worktrees — uxon's
`.uxon/worktrees/...`, claude subagent worktrees under `.claude/`, or
manual ones. **No uxon-side registry.** The porcelain output gives the
branch of each worktree, including the primary working tree (§3). There
is no explicit "primary" field: the primary is the first entry / the one
whose path equals the repo root; detached HEAD yields a `detached` line
instead of `branch`; bare repos yield `bare`. The parser handles all
three.

### 2.3 Disk layout — inside the repo

**Default: `<repo>/.uxon/worktrees/<branch-slug>/`.** `.uxon/` is
excluded from git via **`.git/info/exclude`** (written by uxon as
`launch_user`), not by editing the tracked `.gitignore` — `info/exclude`
is local, never committed, and uxon already has `.git/` write access (a
precondition of `git worktree add`). This keeps worktrees out of the
main checkout's `git status` without touching a tracked file — **more
automatic than claude**, which only advises the user to add
`.claude/worktrees/` to `.gitignore` by hand. The exclude entry is
written **before** the first `git worktree add` (so the in-tree worktree
never shows as untracked) and the append is **idempotent** (the `.uxon/`
line is added at most once).

This location is decided by uxon's multi-user model. `git worktree add`
writes both to the new worktree directory **and** to the main repo's
`.git/worktrees/<name>/` (registration), so `launch_user` must write the
repo's `.git/` — already a precondition. Given that, the worktree dir
must also be writable by `launch_user` and pass `allowed_roots`:

| Location | Writable by launch_user | Inside `allowed_roots` | Per-user isolation | Nesting |
|---|---|---|---|---|
| **Inside repo `.uxon/worktrees/`** | always (repo already writable) | always (repo is the launch target) | per-repo | yes (excluded) |
| Sibling in repo parent | parent often root/shared → no | usually | yes | no |
| User home `~/.uxon/worktrees` | yes | **fails under a strict whitelist** | yes | no |
| Global dir | needs perms on a shared dir | must be added to roots | needs per-user namespacing | no |

Inside-repo is the only location that satisfies write + `allowed_roots` +
isolation **with zero extra configuration in every deployment**. `.uxon/`
is uxon's own namespace dir (mirrors `.claude/`, `.git/`), so we are not
imposing a generic top-level dir on a user's repo.

- **Override:** config key **`worktree_root`** (default empty). When set:
  `<worktree_root>/<repo-slug>/<branch-slug>/`. Covers home/central/sibling
  models for an admin who ensures write permission and `allowed_roots`
  membership.
- **Gating:** the computed path is always run through
  `ensure_launch_target_allowed`; outside `allowed_roots` → clear error
  suggesting `worktree_root`. No silent fallback.

### 2.4 `.worktreeinclude` (copying gitignored files)

A fresh worktree lacks untracked files like `.env`. On worktree creation
uxon copies gitignored files matching a `.worktreeinclude` file
(`.gitignore` syntax) from the main checkout into the new worktree. Only
files that match a pattern **and** are gitignored are copied (never
tracked files), matching claude's semantics.

### 2.5 Removal — guarded

uxon provides an explicit, guarded remove gesture.

**Where it lives.** Remove is a *management* action, not a launch action,
so it does **not** live on the launch screen's WORKSPACE column (which is
"where to launch" and carries no session data). It lives on the **main
dashboard**: a worktree-backed session row gets a remove-worktree binding
(distinct from `kill` — kill ends the session, remove deletes the
worktree). For worktrees that have **no** session, removal is offered from
a small **worktree-management modal** reachable from the dashboard
(lists the repo's worktrees via `git worktree list`, with a remove
binding per row). The launch screen stays purely about launching.

Mechanics:

- Confirmation → `git worktree remove` (as `launch_user`).
- **Refuse** if the worktree has uncommitted changes, untracked files, or
  unpushed commits, unless force-confirmed. Branch deletion (`git branch
  -d/-D`) is a separate, optional step.
- **Refuse while a live tmux session** points at the worktree — the remove
  path runs its own session check (it does not rely on the launch screen),
  covering both uxon worktrees and any claude subagent worktree listed.
- **Never auto-delete.**
- The remove binding is **destructive**, so per the project rule it has
  `show=True` + a description and is covered by the `BINDINGS` drift guard
  (`tests/test_uxon_tui_bindings.py`).

### 2.6 Session naming & matching for worktrees (identity)

Attach-vs-new correctness depends on the session name being derivable
**identically at create time and at probe time**. The plain launch path
derives the stem from the directory basename
(`session_stem_for_path(target_dir)`), which for a worktree would be just
the branch slug (e.g. `feature`) — **not repo-qualified**, so two repos
with a same-named worktree collide on the stem and trip
`compatible_indexed_sessions`' hard `fail()` ("session conflict").

Therefore worktree sessions use the existing **repo-qualified**
`session_stem_for_worktree(repo_root, branch)` (`<repo>-<branch>`) at
**both** create and probe:

- **Create:** name the session with `session_stem_for_worktree(repo_root,
  branch)`; tmux cwd = `<worktree_path>`.
- **Probe (launch into an existing worktree / the new-worktree commit):** a
  **worktree-aware probe** — *not* the plain
  `probe_tui_compatible_sessions` — that computes the same
  `session_stem_for_worktree(repo_root, branch)` and uses the worktree path
  as `compatibility_root`. Generalise the existing probe to accept an
  explicit stem (or add a worktree variant) rather than always deriving it
  from the basename.

The primary working tree keeps the ordinary path-based stem
(`session_stem_for_path(repo_root)`) — it is the existing non-worktree
behaviour and is unaffected.

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

- **AGENT** — current left panel. Hidden when a single agent is enabled.
- **PERMISSION** — current right panel; rebuilt on agent change.
- **WORKSPACE** — new. **Folder selection, not session selection.** Rows:
  the primary working tree + one row per existing worktree (from
  `git worktree list`) + a `+ New worktree…` row.

### WORKSPACE rows

- The **first row is the primary working tree**, labelled by its **actual
  current branch** plus `(primary)` (from porcelain; never hard-coded to
  `main`; the primary can be on any branch or detached). Default highlight.
- Each other row is an existing worktree, labelled by its branch.
- `+ New worktree…` is the create affordance.
- **No session markers and no session list on this screen.** Attaching to
  a specific existing session is the main dashboard's job.

### What Enter does

- **Existing workspace (primary or a worktree)** + Enter → a normal launch
  into that folder: `target_dir = <that path>`, `-c <path>`, no worktree
  creation. This reuses the standard launch planner.
- **`+ New worktree…`** + Enter → branch-name input → `plan_worktree_launch`
  creates the worktree → launches into it.

In both cases the attach-vs-new guard is preserved: at commit time uxon
probes for a compatible session in that **folder + agent**; if one exists,
`SessionChoiceScreen` appears (`a` attach / `n` new / Esc). The probe uses
the **worktree-aware stem** for worktree targets and the plain path-based
probe for the primary tree (§2.6) — this is the detail that makes the
guard reliable; "same tmux cwd" alone is not sufficient because matching
is by name-stem.

### New-worktree input semantics

The input is a branch name (required this release — no empty / no `#PR`,
see §7). If the branch exists, `git worktree add <path> <branch>`
(checkout); otherwise `git worktree add <path> -b <branch>` off the base
selected by `worktree_base` (§4.5; default `local` → local `origin/HEAD`,
fallback local `HEAD`; no fetch). Then `.worktreeinclude` copying, then
launch with `-c <path>`.

### Degradation

- Single agent → AGENT column hidden.
- Non-git target → no WORKSPACE column; flow exactly as today
  (launch-options → `SessionChoice` guard if a session exists).

---

## 4. Architecture

### 4.1 Launch planner (create path)

A single planner builds the create-and-launch request:

```
plan_worktree_launch(cfg, launch_user, repo_root, branch_name,
                     agent_id, mode_id) -> LaunchRequest
```

- Compute the worktree path (§2.3) and gate it.
- `git worktree add` as `launch_user` (new branch off base ref, or
  checkout existing branch).
- Copy `.worktreeinclude` matches.
- tmux launch with `-c <worktree_path>` and the chosen agent + mode.

Launching into an **existing** worktree (or the primary) is *not* this
function — it is the standard launch path with `target_dir =
<worktree_path>`. Both the TUI new-worktree path and the CLI `-w` flag use
`plan_worktree_launch`. The existing "`-w` only for claude" guard and the
native `-w` passthrough are removed.

### 4.2 Workspace probe

```
on_probe_worktrees(repo_root) -> list[Workspace]
```

`Workspace = (label, branch, path, is_primary)`, parsed from
`git worktree list --porcelain`. **Folders only — no session data.**
Non-git target → empty list (no WORKSPACE column). Runs **once** when the
launch screen opens (not per-keystroke), in a **worker** (not
synchronously in `on_mount`) so it never blocks the event loop, and via
**`nonint_command_prefix_for_user`** — the fullscreen TUI cannot show an
interactive `sudo` prompt, so a missing NOPASSWD grant must fail fast
rather than hang.

The attach-vs-new session probe (at commit time) uses the **worktree-aware
stem** for worktree targets per §2.6 — i.e. `probe_tui_compatible_sessions`
is generalised to accept an explicit stem (or a worktree variant is added),
not the basename-only stem. The primary tree uses the existing plain probe
unchanged.

### 4.2a Context wiring (touchpoints)

The new callbacks must be threaded through the existing TUI context
plumbing, not just referenced:

- add `on_probe_worktrees`, `on_create_worktree` (→ `plan_worktree_launch`),
  and `on_remove_worktree` to the `TuiContext` dataclass in
  `src/uxon/tui/context.py`;
- construct + `_wrap_tui_callback`-wrap them in `cli.py`'s
  `_build_tui_context`, alongside `on_probe_existing_sessions`;
- the create/launch result flows through the existing
  `app.request_launch(LaunchRequest)` path; remove returns a status the
  dashboard surfaces as a toast (no relaunch).

### 4.3 Multi-user

All git operations (`rev-parse`, `worktree list/add/remove`, status
checks, `info/exclude` write) run under
`command_prefix_for_user(launch_user)` (interactive, launch-time) or
`nonint_command_prefix_for_user` (background/probe), consistent with
`git_repo_root_as_user`.

### 4.4 Pure helpers (testable, no Textual)

- worktree path computation (default `.uxon/worktrees` vs `worktree_root`).
- branch/name → slug.
- `git worktree list --porcelain` parsing: workspaces with branch +
  is_primary (first entry / path == repo root); handle `detached`, `bare`.
- remove eligibility (clean vs dirty/untracked/unpushed).

### 4.5 Config keys (5-step process in conventions.md)

- `worktree_root: str = ""` (empty → default `.uxon/worktrees` layout).
- `worktree_base: str = "local"` — where a new branch is based:
  - **`local`** (default): branch off the **local** `origin/HEAD` if it
    exists, else local `HEAD`. **No `git fetch`, no network.**
  - **`remote`**: `git fetch` origin first, then branch off the freshly
    fetched `origin/HEAD` (claude-like). Needs network + credentials.

  **This default deviates from `claude -w`**, which fetches by default for
  a tree matching the latest remote. uxon defaults to `local` because in
  the multi-user/`sudo` launch context an implicit per-create `git fetch`
  against a possibly-private remote can hang, prompt for credentials, or
  fail — `local` is deterministic and network-free. The deviation must be
  stated in the docs (§6).

Each: extend `DEFAULT_CONFIG` / `Config` / `load_config`; validation;
`SettingSpec` in `settings.py`; doc entry in
`docs/reference/configuration.md`; `load_config` + round-trip tests.

### 4.6 Audit

Worktree create/remove change state and must be audited, consistent with
`session.new` / `session.attach` / `session.kill` (via `src/uxon/audit.py`):

- **`worktree.create`** — emitted from the create path (CLI `-w` and TUI
  new-worktree), with `agent`, `project` (repo_root), `branch`, `path`,
  `base` (`local`/`remote`), and the launched `session`.
- **`worktree.remove`** — emitted from the remove gesture, with `project`,
  `branch`, `path`, and whether the branch was deleted / force was used.

The session that a worktree launch starts still emits its own
`session.new` (unchanged) — `worktree.create` is the additional
worktree-lifecycle event, not a replacement.

---

## 5. CLI parity

`uxon -w/--worktree <branch>` (and `new -w`) route through
`plan_worktree_launch`, agent-agnostic. Behaviour change: `-w` is no
longer claude-only and creates a `<repo>/.uxon/worktrees/...` worktree
managed by uxon instead of delegating to `claude -w`.

---

## 6. Documentation (required)

The decision to manage worktrees in uxon rather than via an agent's native
flag must be stated explicitly, so the behaviour change is discoverable
and the rationale is not lost:

- **CHANGELOG.md** — entry for the `-w` behaviour change (uxon-managed,
  agent-agnostic, new `.uxon/worktrees/` layout; claude's native `-w` is
  no longer used).
- **`docs/reference/configuration.md`** — `worktree_root`,
  `worktree_base`.
- **User-facing how-to/explanation** (Diátaxis: a how-to for create/
  attach/remove + a short explanation note) stating plainly that uxon
  creates and owns worktrees itself (not claude's `-w`), where they live
  (`.uxon/worktrees/`, excluded automatically via `.git/info/exclude` —
  unlike claude, which only advises a manual `.gitignore` edit), and why
  (uniform across agents, consistent session matching, multi-user gating).
  **Must call out the two deliberate deviations from `claude -w`:**
  (1) uxon manages worktrees itself (no native `-w`); (2) `worktree_base`
  defaults to `local` (no fetch), whereas `claude -w` fetches by default —
  set `worktree_base = remote` for claude-like freshness.
  Read `docs/agents/maintaining-docs.md` before editing user-facing docs.
- **AGENTS.md / code-map** — note that worktree creation is owned by the
  launch builder / `plan_worktree_launch`, consistent with the existing
  "single place builds agent command lines" rule.

---

## 7. Out of scope (this release)

- `#PR` worktrees and empty-name auto-generation (replicable later via
  `git fetch pull/<n>/head` and a name generator).
- Remote (peer) worktree creation — local-repo gesture only.
- **Repo-config consolidation:** moving project config `.uxon.toml` into
  `.uxon/config.toml`. Sensible once `.uxon/` exists, but an orthogonal
  breaking change (`find_project_config` discovery, back-compat for
  deployed `.uxon.toml`, migration). Backlog, with dual-path support.

---

## 8. Known edges

- **Slug path collision:** two branch names that slugify to the same
  directory (e.g. `feature/auth` and `feature-auth`). `git worktree add`
  fails when the path is taken; uxon catches it and shows a clear
  "worktree path already exists — pick another name" error. No silent
  reuse.
- **Worktree-from-worktree:** creating a worktree while launched inside a
  worktree — `git rev-parse --show-toplevel` returns the linked worktree's
  root. Normalise to the main working tree via
  `git rev-parse --git-common-dir` so new worktrees always anchor to the
  primary repo, not a nested one.

(The native-`-w` trust-dialog edge from earlier drafts no longer applies —
there is no native path. The session↔worktree identity mismatch is **not**
gone; it is handled by the repo-qualified naming + worktree-aware probe in
§2.6, which must be implemented exactly as specified.)

---

## 9. Testing

- Pure unit tests for every helper in §4.4 (path computation, slug,
  porcelain parsing incl. primary/detached/bare, remove eligibility,
  collision detection).
- **Identity test (§2.6):** worktree session created with
  `session_stem_for_worktree` is found by the worktree-aware probe (same
  stem); and two repos with a same-named worktree do **not** collide /
  hard-fail (`compatible_indexed_sessions` "session conflict" path stays
  quiet). This is the regression guard for the §2.6 correctness fix.
- **Remove guard:** refuses on dirty/untracked/unpushed and while a live
  session points at the worktree; succeeds on a clean, session-less
  worktree.
- **Audit:** `worktree.create` / `worktree.remove` emitted with the
  documented fields (assert via the audit test harness).
- `load_config` + settings round-trip tests for `worktree_root` and
  `worktree_base` (incl. validation: only `local`/`remote`).
- CLI dry-run tests for `-w` (uxon `git worktree add` path; gating
  failure → clear error).
- One `Pilot` smoke test for the extended launch screen: agent change
  rebuilds permission; WORKSPACE lists primary `(primary)` + worktrees +
  `+ New worktree…`; Enter on an existing workspace commits and (when a
  session exists) the `SessionChoice` guard appears; `+ New worktree…`
  opens the input.
- **`BINDINGS` drift guard** (`tests/test_uxon_tui_bindings.py`) covers the
  new destructive remove binding (`show=True` + description).
- Keep branchy assertions in pure tests; reserve `Pilot` for wiring /
  focus / async behaviour, per the TUI test policy.
