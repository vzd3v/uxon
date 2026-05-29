# Worktree support (3.5.0) — design

Native, first-class git-worktree support in `uxon`: create worktrees and
attach to existing ones — **uniformly across all agents**
(`claude`, `codex`), managed by uxon itself rather than delegating to any
agent's native worktree flag. Surfaced by extending the launch flow with a
workspace column; the existing `SessionChoice` modal stays as the
attach-vs-new guard. Worktree **removal** is deferred to a follow-up (§7).

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
| Exit-time auto-cleanup (clean → remove, dirty → prompt) | not replicated this release; removal deferred (§7) | the only real trade |

The only behaviour not cheaply replicable is claude's exit-time
auto-cleanup — and it applies only to `-w`-created worktrees, is
known-incomplete in practice (anthropics/claude-code #26725, #31488),
and is absent for codex anyway. uxon ships no removal gesture this
release: worktrees are created and attached to; removal is deferred (§7)
and done manually via `git worktree remove` meanwhile.

Decisive benefit: **uniformity makes session↔worktree identity
consistent.** Every session launches with `-c <worktree_path>`, so a
session's tmux cwd *is* its worktree path. (Delegating to native `-w`
would have launched with `-c repo_root`, leaving sessions distinguishable
only by name-stem and breaking the attach guard.) The exact naming and
matching scheme is specified in §2.5 — it must be **repo-qualified and
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

The append is also **concurrency-safe**: the multi-user model means two
`launch_user`s may create worktrees in the same repo at once, so the
read-modify-write of `info/exclude` is serialised (an `flock` on the file,
or a temp-file-then-rename) and tolerates a pre-existing `.uxon/` line —
it never double-appends or clobbers a concurrent writer. (`git worktree
add` already takes git's own lock; the hand-written exclude append does
not, hence this guard.) The exclude write is **skipped entirely when
`worktree_root` is set** — an out-of-repo worktree is not in the checkout,
so there is nothing to exclude.

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

**Mechanism — delegate to git, never hand-roll `.gitignore`.** uxon does
not reimplement `.gitignore` semantics (`**`, anchoring, negation,
directory matches) and does not pull in a pattern-matching library. The
copy set is the **intersection of two git queries**, both run as
`launch_user`:

- set A — files git reports as ignored-and-untracked:
  `git ls-files -o -i --exclude-standard`;
- set B — untracked files matching the `.worktreeinclude` patterns:
  `git ls-files -o -i --exclude-from=<.worktreeinclude>`.

Copy `A ∩ B` (gitignored **and** worktreeinclude-matching; tracked files
are excluded by construction since both queries are `--others`). Git is the
single authority for both "is gitignored" and "matches a pattern", so the
copy set can never drift from git's own judgement.

### 2.5 Session naming & matching for worktrees (identity)

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

**Critical: the probe and the launch planner both derive the stem — they
must derive the *same* one.** `session_stem_for_path` (basename-only) is
used at **three** sites today: `_plan_tui_run_agent`,
`_plan_tui_existing_session_or_launch`, and `probe_tui_compatible_sessions`.
Generalising only the probe is **not enough** — the planner that calls
`allocate_session_name` must also pass the worktree-aware stem for a
worktree target. If the planner uses the basename stem while the probe uses
the repo-qualified one, the created session is named `uxon-<branch>@…` but
the next probe looks for `uxon-<repo>-<branch>@…`: it never matches (silent
duplicate session), and the cross-repo allocate still hard-`fail()`s. The
CLI path (`do_run` / `new`) already branches on `branch` and uses
`session_stem_for_worktree` before `allocate_session_name`; the **TUI
planners do not** and must be made worktree-aware (detect a worktree target
and switch stems, or route worktree targets through a worktree-aware
variant). `allocate_session_name` already takes `stem` as a parameter, so
no signature change is needed there — only the call sites choose the stem.

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

Left/right navigation cycles only the **visible** columns: `_active_panel`
becomes a three-value state (`agent` / `mode` / `workspace`) and the
focus-cycle skips any hidden column, so with AGENT hidden (single agent)
←/→ moves between PERMISSION and WORKSPACE.

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
  creation. The **primary** tree reuses the standard path-based planner
  unchanged; a **worktree** target reuses the same planner **but with the
  worktree-aware stem** (§2.5) — it must not run the basename-stem planner
  verbatim, or the session is misnamed and the probe stops matching (§2.5).
- **`+ New worktree…`** + Enter → branch-name input → `plan_worktree_launch`
  creates the worktree → launches into it.

In both cases the attach-vs-new guard is preserved: at commit time uxon
probes for a compatible session in that **folder + agent**; if one exists,
`SessionChoiceScreen` appears (`a` attach / `n` new / Esc). The probe uses
the **worktree-aware stem** for worktree targets and the plain path-based
probe for the primary tree (§2.5) — this is the detail that makes the
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
- Git repo with no extra worktrees → WORKSPACE still shows (primary
  `(primary)` + `+ New worktree…`). Deliberate, for discoverability, and
  free in practice: the primary row is the default highlight, so Enter
  commits the common "just launch in the primary" case without ever
  entering the column.

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

The standard planner must, however, select the **worktree-aware stem** for
a worktree target (§2.5): either generalise `_plan_tui_run_agent` /
`_plan_tui_existing_session_or_launch` to detect a worktree and switch
stems, or route worktree targets through a worktree-aware variant. Reusing
the basename-stem planner verbatim for a worktree reintroduces the §2.5
collision. (The CLI path already does this; only the TUI planners need the
change.)

### 4.2 Workspace probe

```
on_probe_worktrees(cwd) -> list[Workspace]
```

`Workspace = (label, branch, path, is_primary)`, parsed from
`git worktree list --porcelain`. **Folders only — no session data.**
Non-git target → empty list (no WORKSPACE column). Runs **once** when the
launch screen opens (not per-keystroke), in a **worker** (not
synchronously in `on_mount`) so it never blocks the event loop, and via
**`nonint_command_prefix_for_user`** — the fullscreen TUI cannot show an
interactive `sudo` prompt, so a missing NOPASSWD grant must fail fast
rather than hang.

**The `cwd → repo_root` resolution is part of this probe and must also be
non-interactive.** The probe takes `cwd` (not a pre-resolved `repo_root`)
precisely so the whole chain — resolve the repo root, then list worktrees —
runs once, off the event loop, under the non-interactive prefix. It must
**not** reuse `git_repo_root_as_user`, which uses the *interactive*
`command_prefix_for_user` and would hang the fullscreen TUI on a hidden
`sudo` prompt; use a non-interactive repo-root resolver
(`nonint_command_prefix_for_user`) instead.

The attach-vs-new session probe (at commit time) uses the **worktree-aware
stem** for worktree targets per §2.5 — i.e. `probe_tui_compatible_sessions`
is generalised to accept an explicit stem (or a worktree variant is added),
not the basename-only stem. The primary tree uses the existing plain probe
unchanged.

### 4.2a Context wiring (touchpoints)

The new callbacks must be threaded through the existing TUI context
plumbing, not just referenced:

- add `on_probe_worktrees` and `on_create_worktree`
  (→ `plan_worktree_launch`) to the `TuiContext` dataclass in
  `src/uxon/tui/context.py`;
- construct + `_wrap_tui_callback`-wrap them in `cli.py`'s
  `_build_tui_context`, alongside `on_probe_existing_sessions`;
- the create/launch result flows through the existing
  `app.request_launch(LaunchRequest)` path.

Note `_wrap_tui_callback` uses a **process-global** `redirect_stderr`,
while `on_probe_worktrees` runs in a worker thread (§4.2) — the same
combination the existing `on_probe_link_health` probe already uses. Reuse
that established pattern; if its global-stderr-across-threads behaviour is
ever tightened, the worktree probe should follow suit rather than invent a
second convention.

### 4.3 Multi-user

All git operations (`rev-parse`, `worktree list/add`, status
checks, `info/exclude` write) run under
`command_prefix_for_user(launch_user)` (interactive, launch-time) or
`nonint_command_prefix_for_user` (background/probe), mirroring the
prefix split that `git_repo_root_as_user` uses. Note `git_repo_root_as_user`
itself is interactive-only; the probe path needs the non-interactive
repo-root resolver called for in §4.2 (a small new helper, or a `nonint`
parameter on the existing one), not a reuse of it as-is.

### 4.4 Pure helpers (testable, no Textual)

- worktree path computation (default `.uxon/worktrees` vs `worktree_root`).
- branch/name → slug.
- `git worktree list --porcelain` parsing: workspaces with branch +
  is_primary (first entry / path == repo root); handle `detached`, `bare`.

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

Worktree creation changes state and must be audited, consistent with
`session.new` / `session.attach` / `session.kill` (via `src/uxon/audit.py`):

- **`worktree.create`** — emitted from the create path (CLI `-w` and TUI
  new-worktree), with `agent`, `project` (repo_root), `branch`, `path`,
  `base` (`local`/`remote`), and the launched `session`.

(A `worktree.remove` event is part of the deferred removal work — §7.)

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
  attach + a short explanation note) stating plainly that uxon
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

- **Worktree removal** — a guarded remove/cleanup gesture (refuse on
  dirty / unpushed work or a live session, optional branch deletion,
  audited as `worktree.remove`). Deferred to a follow-up and tracked as a
  feature request; the UX surface is left open. Meanwhile worktrees are
  removed manually via `git worktree remove`. This release only creates
  and attaches.
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
- **Branch already checked out elsewhere:** a branch live in another
  worktree is already a WORKSPACE row, but if it is typed into
  `+ New worktree…`, `git worktree add <path> <branch>` fails ("already
  checked out at …"). uxon catches this and points the operator at the
  existing worktree row rather than surfacing the raw git error.
- **Detached-HEAD worktree:** a worktree with no branch (porcelain
  `detached`) is labelled by its short SHA in the WORKSPACE column, and its
  session stem substitutes that short SHA for the branch
  (`session_stem_for_worktree(repo_root, <short-sha>)`) so identity stays
  repo-qualified. This only ever applies to **externally-created** worktrees
  (manual or claude subagent): uxon always creates with `-b`, so a
  uxon-made worktree always has a branch. Caveat for the external case — if
  HEAD advances in the detached worktree the short SHA changes, so a later
  launch derives a different stem and the probe won't match the prior
  session (a silent duplicate, not a hard fail, since the tmux cwd is
  unchanged). Acceptable best-effort for this rare case.

(The native-`-w` trust-dialog edge from earlier drafts no longer applies —
there is no native path. The session↔worktree identity mismatch is **not**
gone; it is handled by the repo-qualified naming + worktree-aware probe in
§2.5, which must be implemented exactly as specified.)

---

## 9. Testing

- Pure unit tests for every helper in §4.4 (path computation, slug
  incl. distinct branches that slugify to the same directory — the
  pre-condition for the §8 path collision, porcelain parsing incl.
  primary/detached/bare). The collision *handling* itself (the
  `git worktree add` failure path) is a runtime path, covered by the CLI
  dry-run / error tests below, not a pure helper.
- **Identity test (§2.5):** the worktree launch **planner** names the
  session with `session_stem_for_worktree` (assert the *allocated session
  name*, not only that the probe matches — both planner and probe must
  derive the same stem); the worktree-aware probe then finds that session;
  and two repos with a same-named worktree do **not** collide / hard-fail
  (`compatible_indexed_sessions` "session conflict" path stays quiet). This
  is the regression guard for the §2.5 correctness fix.
- **Audit:** `worktree.create` emitted with the documented fields (assert
  via the audit test harness).
- `load_config` + settings round-trip tests for `worktree_root` and
  `worktree_base` (incl. validation: only `local`/`remote`).
- CLI dry-run tests for `-w` (uxon `git worktree add` path; gating
  failure → clear error).
- One `Pilot` smoke test for the extended launch screen: agent change
  rebuilds permission; WORKSPACE lists primary `(primary)` + worktrees +
  `+ New worktree…`; Enter on an existing workspace commits and (when a
  session exists) the `SessionChoice` guard appears; `+ New worktree…`
  opens the input.
- Keep branchy assertions in pure tests; reserve `Pilot` for wiring /
  focus / async behaviour, per the TUI test policy.
