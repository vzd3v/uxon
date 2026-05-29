# Worktree Support (3.5.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add native, uxon-managed git-worktree support — create worktrees and attach to existing ones uniformly across all agents — surfaced by a third "WORKSPACE" column on the launch-options screen and via the CLI `-w/--worktree` flag, with repo-qualified session identity so the attach-vs-new guard stays reliable.

**Architecture:** uxon owns every worktree (`git worktree add` as `launch_user` + launch with `-c <worktree_path>`, no agent-native `-w`). Pure helpers (path/slug/porcelain-parse) are testable without Textual; a worktree-aware planner and probe derive the **same** repo-qualified stem (`session_stem_for_worktree`) at create and probe time; the launch-options screen grows a non-blocking worker-driven workspace column; the CLI `-w` flag routes through the same create planner. Worktree **removal** is out of scope (§7).

**Tech Stack:** Python 3.11+, `subprocess` (git under `sudo` prefixes), Textual (TUI), `tomllib`/`tomlkit` (config), `pytest` + Textual `Pilot` (tests).

---

## Conventions (read before starting)

- Code style/naming/test idioms: discover from the files each task names; do **not** invent new patterns.
- Project rules: `AGENTS.md` (hard rules — single launch builder, BINDINGS-only key handling, lazy textual import, config writes via `tomlkit`), `docs/agents/conventions.md` (the 5-step config process), `docs/agents/code-map.md` (TUI test runtime policy — pure helpers over Pilot), `docs/agents/maintaining-docs.md` (read before editing user-facing docs).
- **NO crutches/hacks.** Ideal architecture and runtime speed: render/compute without blocking the Textual event loop — git probes run in a worker, under `nonint_command_prefix_for_user` (no hidden `sudo` prompt), and **once** when the screen opens (not per keystroke).
- `__version__` is already `3.5.0` in `src/uxon/__init__.py`; no bump needed.
- Local checks before each commit:
  ```bash
  python3 -m py_compile $(git ls-files '*.py')
  ruff check . && ruff format --check .
  pyright
  pytest tests/ -n auto
  python -c "import uxon.cli"   # must NOT pull in textual
  ```

## Out of scope (do NOT implement — §7)

- Worktree **removal** / cleanup gesture (the `worktree.remove` audit event, dirty/unpushed guards, branch deletion). Manual `git worktree remove` meanwhile.
- `#PR` worktrees and empty-name auto-generation.
- Remote (peer) worktree creation — local-repo gesture only.
- Repo-config consolidation (`.uxon.toml` → `.uxon/config.toml`).

---

## File structure

**New files:**
- `src/uxon/worktrees.py` — pure, Textual-free helpers: `Workspace` dataclass, `worktree_path_for`, `parse_worktree_porcelain`. Importable by `uxon.cli` and tests without pulling in `subprocess`-heavy code or Textual. Mirrors the "pure data structures" boundary `uxon.tui.context` already uses.
- `tests/test_uxon_worktrees.py` — pure unit tests for the helpers (§9 first bullet).
- `src/uxon/tui/screens/worktree_branch.py` — `WorktreeBranchScreen` (a `ModalScreen[str | None]` for the new-worktree branch name; allows `/`, unlike `NewProjectScreen`) + the `worktree_branch_valid` pure validator.
- `docs/guides/customise/worktrees.md` — how-to: create/attach a worktree (Diátaxis how-to).
- `docs/explain/worktrees.md` — explanation: why uxon owns worktrees, the two deviations from `claude -w` (Diátaxis explanation).

**Modified files:**
- `src/uxon/cli.py` — `DEFAULT_CONFIG`/`Config`/`load_config` (config keys); `_build_tmux_launch_request` (drop native `-w` passthrough); `do_run`/`do_new` (route `-w` through `plan_worktree_launch`); new `plan_worktree_launch`, `git_repo_root_nonint_as_user`, `git_common_dir_root_as_user`, `write_uxon_exclude_entry`, `copy_worktreeinclude_matches`, `_branch_exists_as_user`, `_local_base_ref_as_user`; worktree-aware planners/probe (`session_stem_for_worktree` at TUI call sites + `probe_tui_compatible_sessions(..., stem=)`); new `on_probe_worktrees`/`on_create_worktree` closures + wiring in `_build_tui_context`.
- `src/uxon/settings.py` — `SETTINGS_SPECS`: `worktree_root`, `worktree_base`.
- `src/uxon/tui/context.py` — `TuiContext`: add four callbacks (`on_probe_worktrees`, `on_create_worktree`, `on_launch_existing_worktree`, `on_probe_existing_worktree_sessions`).
- `src/uxon/tui/config.py` — `TuiConfig`: snapshot the four new callbacks in `from_context`.
- `src/uxon/tui/state.py` — pure focus-cycle helper for the three-value `_active_panel` (workspace column).
- `src/uxon/tui/screens/launch_options.py` — third WORKSPACE column; three-value `_active_panel`; visible-column focus cycle; emit a workspace selection in the dismiss value.
- `src/uxon/tui/screens/main.py` — branch the launch commit on the chosen workspace (primary vs existing worktree vs new-worktree), wiring the worktree-aware probe + `on_create_worktree`.
- `src/uxon/tui/app.py` — worker that runs `on_probe_worktrees` off the event loop on launch-screen open.
- `CHANGELOG.md` — `-w` behaviour change + new config keys.
- `docs/reference/configuration.md` — `worktree_root`, `worktree_base` rows.
- `AGENTS.md` — note worktree creation is owned by `plan_worktree_launch` / the launch builder.

**Critical tasks (orchestrator: insert review checkpoints):** Task 6 (worktree-aware planner stem), Task 7 (worktree-aware probe), Task 8 (identity regression test), Task 11 (create planner `plan_worktree_launch`). These are the §2.5 correctness core.

---

## Task 1: Pure helper — worktree path computation (§4.4)

**Files:**
- Create: `src/uxon/worktrees.py`
- Test: `tests/test_uxon_worktrees.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_uxon_worktrees.py
"""Pure unit tests for uxon.worktrees (no Textual, no subprocess)."""

from __future__ import annotations

import unittest

from uxon.worktrees import Workspace, compute_worktree_path, parse_worktree_porcelain


class ComputeWorktreePathTests(unittest.TestCase):
    def test_default_layout_inside_repo(self) -> None:
        path = compute_worktree_path(
            repo_root="/srv/work/myapp", branch="feature/auth", worktree_root=""
        )
        self.assertEqual(path, "/srv/work/myapp/.uxon/worktrees/feature-auth")

    def test_worktree_root_override_adds_repo_slug(self) -> None:
        path = compute_worktree_path(
            repo_root="/srv/work/myapp", branch="bugfix-1", worktree_root="/data/wt"
        )
        self.assertEqual(path, "/data/wt/myapp/bugfix-1")

    def test_distinct_branches_can_slugify_to_same_path(self) -> None:
        # §8 slug collision precondition: both reduce to "feature-auth".
        a = compute_worktree_path(repo_root="/r/app", branch="feature/auth", worktree_root="")
        b = compute_worktree_path(repo_root="/r/app", branch="feature-auth", worktree_root="")
        self.assertEqual(a, b)


class SlugParityTests(unittest.TestCase):
    """C5: the worktree-path slug and the session-stem slug MUST match.

    ``compute_worktree_path`` uses ``worktrees._slugify``; the session stem
    uses ``cli.slugify`` (via ``session_stem_for_worktree``). If they ever
    diverge, the created session name and the probe-derived name disagree
    and identity breaks silently. Lock them together here.
    """

    def test_slugify_matches_cli_slugify(self) -> None:
        import uxon.cli as cli
        from uxon.worktrees import _slugify

        for branch in ["feature/auth", "feature-auth", "BugFix_123",
                       "weird//name!!", "", "déjà-vu", "a/b/c"]:
            self.assertEqual(_slugify(branch), cli.slugify(branch),
                             f"slug mismatch for {branch!r}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon_worktrees.py::ComputeWorktreePathTests -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'uxon.worktrees'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/uxon/worktrees.py
"""Pure, Textual-free git-worktree helpers.

This module imports no UI framework and no ``subprocess`` machinery — it
holds the deterministic computations (path layout, slug, porcelain
parsing) so they are unit-testable without spinning up a git repo or the
TUI. The side-effecting git calls live in ``uxon.cli`` and pass their
captured stdout into :func:`parse_worktree_porcelain`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


def _slugify(name: str) -> str:
    # Byte-identical to ``uxon.cli.slugify`` (cli.py:796-798). Duplicated —
    # NOT imported — because ``uxon.cli`` imports THIS module, so importing
    # ``slugify`` back from cli would create a circular import. CRITICAL:
    # this rule and ``cli.slugify`` must stay identical, or the worktree
    # PATH slug (here) and the session STEM slug (``session_stem_for_worktree``
    # → ``cli.slugify``) diverge and session↔worktree identity (§2.5) breaks
    # silently. The parity is locked by a test (Task 1 SlugParityTests).
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return slug or "workspace"


def compute_worktree_path(*, repo_root: str, branch: str, worktree_root: str) -> str:
    """Return the absolute directory a worktree for ``branch`` should occupy.

    Default (empty ``worktree_root``): ``<repo_root>/.uxon/worktrees/<slug>``.
    Override: ``<worktree_root>/<repo-slug>/<slug>``. The caller gates the
    result through ``is_worktree_target_allowed`` (the not-yet-exists
    predicate — the dir does not exist yet) — this function never touches
    the filesystem.
    """
    branch_slug = _slugify(branch)
    if worktree_root:
        repo_slug = _slugify(os.path.basename(repo_root.rstrip("/")))
        return os.path.join(worktree_root, repo_slug, branch_slug)
    return os.path.join(repo_root, ".uxon", "worktrees", branch_slug)
```

(`Workspace` and `parse_worktree_porcelain` are added in Task 2 — leave the test imports referencing them; Task 2 makes the module import succeed for the parse tests. For this task run only the `ComputeWorktreePathTests` selector, which does not exercise the not-yet-defined names at import time only if they exist. To keep Step 2/4 self-contained, add the stubs now:)

```python
@dataclass(frozen=True)
class Workspace:
    """One git working tree as surfaced to the TUI WORKSPACE column.

    ``label`` is the user-facing row text (branch name, or short SHA for a
    detached worktree, with ``(primary)`` appended by the renderer — not
    here). ``branch`` is the porcelain branch short-name or the short SHA
    for a detached HEAD. ``path`` is the absolute worktree directory.
    ``is_primary`` marks the main working tree (first porcelain entry /
    path == repo root).
    """

    label: str
    branch: str
    path: str
    is_primary: bool


def parse_worktree_porcelain(text: str, *, repo_root: str) -> list[Workspace]:
    raise NotImplementedError  # implemented in Task 2
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon_worktrees.py::ComputeWorktreePathTests tests/test_uxon_worktrees.py::SlugParityTests -v`
Expected: PASS (3 + 1 passed). `SlugParityTests` imports `uxon.cli` — confirm that does not pull in textual (`python -c "import uxon.cli"` stays textual-free; this is the existing AGENTS.md hard rule).

- [ ] **Step 5: Commit**

```bash
git add src/uxon/worktrees.py tests/test_uxon_worktrees.py
git commit -m "feat(worktrees): pure worktree path computation + slug-parity lock (C5)"
```

---

## Task 2: Pure helper — `git worktree list --porcelain` parsing (§2.2, §4.4, §8 detached/bare)

> §2.2: `git worktree list --porcelain` is the single source of truth — no uxon-side registry. This parser + the Task 14 probe are the whole listing path.

**Files:**
- Modify: `src/uxon/worktrees.py`
- Test: `tests/test_uxon_worktrees.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_uxon_worktrees.py
class ParsePorcelainTests(unittest.TestCase):
    def _sample(self) -> str:
        # Real ``git worktree list --porcelain`` shape: blank-line separated
        # records, "worktree <path>" first, then "HEAD"/"branch" or
        # "detached" or "bare".
        return (
            "worktree /srv/work/myapp\n"
            "HEAD 1111111111111111111111111111111111111111\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /srv/work/myapp/.uxon/worktrees/feature-auth\n"
            "HEAD 2222222222222222222222222222222222222222\n"
            "branch refs/heads/feature/auth\n"
            "\n"
            "worktree /srv/work/myapp/.uxon/worktrees/detached-one\n"
            "HEAD 3333333333333333333333333333333333333333\n"
            "detached\n"
        )

    def test_primary_is_first_and_repo_root(self) -> None:
        rows = parse_worktree_porcelain(self._sample(), repo_root="/srv/work/myapp")
        self.assertTrue(rows[0].is_primary)
        self.assertEqual(rows[0].branch, "main")
        self.assertEqual(rows[0].path, "/srv/work/myapp")

    def test_linked_worktree_branch_short_name(self) -> None:
        rows = parse_worktree_porcelain(self._sample(), repo_root="/srv/work/myapp")
        self.assertEqual(rows[1].branch, "feature/auth")
        self.assertFalse(rows[1].is_primary)
        self.assertEqual(rows[1].path, "/srv/work/myapp/.uxon/worktrees/feature-auth")

    def test_detached_uses_short_sha_as_branch(self) -> None:
        rows = parse_worktree_porcelain(self._sample(), repo_root="/srv/work/myapp")
        self.assertEqual(rows[2].branch, "3333333")  # 7-char short sha
        self.assertEqual(rows[2].label, "3333333")

    def test_bare_repo_entry_skipped(self) -> None:
        text = "worktree /srv/work/bare.git\nbare\n"
        rows = parse_worktree_porcelain(text, repo_root="/srv/work/bare.git")
        # A bare repo has no working tree to launch into — it is dropped.
        self.assertEqual(rows, [])

    def test_primary_detected_by_path_match_not_only_order(self) -> None:
        # If git ever reorders, path==repo_root still flags the primary.
        text = (
            "worktree /srv/work/myapp/.uxon/worktrees/x\n"
            "HEAD 4444444444444444444444444444444444444444\n"
            "branch refs/heads/x\n"
            "\n"
            "worktree /srv/work/myapp\n"
            "HEAD 5555555555555555555555555555555555555555\n"
            "branch refs/heads/main\n"
        )
        rows = parse_worktree_porcelain(text, repo_root="/srv/work/myapp")
        primary = [w for w in rows if w.is_primary]
        self.assertEqual(len(primary), 1)
        self.assertEqual(primary[0].path, "/srv/work/myapp")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon_worktrees.py::ParsePorcelainTests -v`
Expected: FAIL with `NotImplementedError`

- [ ] **Step 3: Write minimal implementation**

Replace the `parse_worktree_porcelain` stub in `src/uxon/worktrees.py` with:

```python
def parse_worktree_porcelain(text: str, *, repo_root: str) -> list[Workspace]:
    """Parse ``git worktree list --porcelain`` into :class:`Workspace` rows.

    Records are separated by blank lines. Each starts with ``worktree
    <path>``; the working tree's ref is ``branch refs/heads/<name>`` (a
    real branch), ``detached`` (no branch — labelled by short SHA), or
    ``bare`` (no working tree — dropped). The primary working tree is the
    one whose path equals ``repo_root`` (it is also conventionally first;
    we detect by path so a future reorder can't fool us).
    """
    repo_root = repo_root.rstrip("/")
    rows: list[Workspace] = []
    path = ""
    head = ""
    branch = ""
    is_bare = False
    is_detached = False

    def flush() -> None:
        nonlocal path, head, branch, is_bare, is_detached
        if path and not is_bare:
            if branch:
                label = branch
            else:  # detached HEAD — short SHA stands in for the branch.
                label = head[:7]
                branch = head[:7]
            rows.append(
                Workspace(
                    label=label,
                    branch=branch,
                    path=path,
                    is_primary=(path.rstrip("/") == repo_root),
                )
            )
        path = ""
        head = ""
        branch = ""
        is_bare = False
        is_detached = False

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            flush()
            continue
        if line.startswith("worktree "):
            flush()
            path = line[len("worktree ") :].strip()
        elif line.startswith("HEAD "):
            head = line[len("HEAD ") :].strip()
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        elif line.strip() == "detached":
            is_detached = True
        elif line.strip() == "bare":
            is_bare = True
    flush()
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon_worktrees.py -v`
Expected: PASS (all ComputeWorktreePath + ParsePorcelain tests)

- [ ] **Step 5: Commit**

```bash
git add src/uxon/worktrees.py tests/test_uxon_worktrees.py
git commit -m "feat(worktrees): porcelain parser (primary/detached/bare)"
```

---

## Task 3: Config keys `worktree_root` + `worktree_base` (§4.5, conventions.md 5-step)

**Files:**
- Modify: `src/uxon/cli.py` (`DEFAULT_CONFIG` ~line 115, `Config` ~line 228, `load_config` ~line 762)
- Test: `tests/test_uxon.py`

- [ ] **Step 1: Write the failing test**

```python
# add to the load_config test class in tests/test_uxon.py
def test_load_config_worktree_keys_default(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = self._write_and_load_cfg("", tmpdir)
    self.assertEqual(cfg.worktree_root, "")
    self.assertEqual(cfg.worktree_base, "local")

def test_load_config_reads_worktree_keys(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = self._write_and_load_cfg(
            'worktree_root = "/data/wt"\nworktree_base = "remote"\n', tmpdir
        )
    self.assertEqual(cfg.worktree_root, "/data/wt")
    self.assertEqual(cfg.worktree_base, "remote")

def test_load_config_rejects_invalid_worktree_base(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        with self.assertRaises(SystemExit):
            self._write_and_load_cfg('worktree_base = "origin"\n', tmpdir)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon.py -k worktree -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'worktree_root'`

- [ ] **Step 3: Write minimal implementation**

In `DEFAULT_CONFIG` (after `"repeat_noninteractive_mode": "fail",`):

```python
    # Worktree layout + base ref (3.5.0). Empty worktree_root → default
    # <repo>/.uxon/worktrees/<slug>. worktree_base picks where a new
    # branch is based: "local" (default, no fetch) off local origin/HEAD
    # else local HEAD; "remote" fetches origin first (claude-like).
    "worktree_root": "",
    "worktree_base": "local",
```

In `Config`, add the two fields to the **keyword-default block** (alongside `tui_table_columns`, after ~line 265 — NOT after `repeat_noninteractive_mode`, which is in the non-default block: a defaulted field there would raise `TypeError: non-default argument follows default argument`):

```python
    tui_color_palette: tuple[str, ...] = ("cyan", "blue")
    local_host_color: str = "green"
    worktree_root: str = ""
    worktree_base: str = "local"
```

Add a validator near `validate_repeat_mode` (~line 494):

```python
def validate_worktree_base(value: str, source: str) -> str:
    mode = value.strip().lower()
    if mode not in {"local", "remote"}:
        fail(f"invalid {source}: {value!r} (expected 'local' or 'remote')")
    return mode
```

In `load_config`, before the `return Config(`:

```python
    worktree_root = str(merged.get("worktree_root", DEFAULT_CONFIG["worktree_root"]))
    worktree_base = validate_worktree_base(
        str(merged.get("worktree_base", DEFAULT_CONFIG["worktree_base"])),
        "worktree_base",
    )
```

And in the `Config(...)` call add:

```python
        worktree_root=worktree_root,
        worktree_base=worktree_base,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon.py -k worktree -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon.py
git commit -m "feat(config): worktree_root + worktree_base keys"
```

---

## Task 4: Settings specs + round-trip for the new keys (§4.5 step 3/5)

**Files:**
- Modify: `src/uxon/settings.py` (`SETTINGS_SPECS` ~line 74, after the `new_project_root` spec)
- Test: `tests/test_uxon_settings.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon_settings.py
class WorktreeSettingsSpecTests(unittest.TestCase):
    def test_worktree_specs_present(self) -> None:
        from uxon.settings import SETTINGS_SPECS

        by_key = {s.key: s for s in SETTINGS_SPECS}
        self.assertIn("worktree_root", by_key)
        self.assertEqual(by_key["worktree_root"].kind, "string")
        self.assertIn("worktree_base", by_key)
        self.assertEqual(by_key["worktree_base"].kind, "enum")
        self.assertEqual(by_key["worktree_base"].choices, ("local", "remote"))

    def test_worktree_base_round_trip(self) -> None:
        import tempfile
        from pathlib import Path

        from uxon import settings as s

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            s.persist_repo_config_updates(path, {"worktree_base": "remote"})
            s.persist_repo_config_updates(path, {"worktree_root": "/data/wt"})
            text = path.read_text()
        self.assertIn('worktree_base = "remote"', text)
        self.assertIn('worktree_root = "/data/wt"', text)
```

(Check the actual writer entrypoint name in `src/uxon/settings.py`; the TUI wiring calls `persist_repo_config_updates`. If the writer in your tree differs, use the same call the existing round-trip tests use — see `tests/test_uxon_settings.py` patterns around `persist_repo_config_updates`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon_settings.py::WorktreeSettingsSpecTests -v`
Expected: FAIL with `AssertionError` (worktree_root not in by_key)

- [ ] **Step 3: Write minimal implementation**

In `SETTINGS_SPECS`, after the `new_project_root` spec:

```python
    SettingSpec("worktree_root", "string", "Base dir for uxon-managed worktrees. Empty = <repo>/.uxon/worktrees."),
    SettingSpec(
        "worktree_base",
        "enum",
        "Base ref for a new worktree branch: 'local' (no fetch) or 'remote' (git fetch first).",
        choices=("local", "remote"),
    ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon_settings.py::WorktreeSettingsSpecTests -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/uxon/settings.py tests/test_uxon_settings.py
git commit -m "feat(settings): worktree_root + worktree_base specs"
```

---

## Task 5: Non-interactive git resolvers (repo-root + common-dir) (§4.2, §4.3, §8 worktree-from-worktree)

**Files:**
- Modify: `src/uxon/cli.py` (after `git_repo_root_as_user` ~line 1208)
- Test: `tests/test_uxon.py`

These are the off-event-loop git seams the TUI probe needs. `git_repo_root_as_user` uses the **interactive** prefix and would hang the fullscreen TUI on a hidden `sudo` prompt — so we add non-interactive variants. `git_common_dir_as_user` normalises a worktree-from-worktree to the primary repo (§8).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon.py (a class that can monkeypatch subprocess.run)
class NonintGitResolverTests(unittest.TestCase):
    def test_repo_root_nonint_uses_nonint_prefix(self) -> None:
        import uxon.cli as cli

        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            class CP:
                returncode = 0
                stdout = "/srv/work/myapp\n"
                stderr = ""
            return CP()

        with mock.patch.object(cli.subprocess, "run", fake_run), \
             mock.patch.object(cli, "process_user", return_value="caller"):
            root = cli.git_repo_root_nonint_as_user("/srv/work/myapp/sub", "devagent")
        self.assertEqual(root, cli.canonical("/srv/work/myapp"))
        self.assertIn("-n", seen["cmd"])  # nonint sudo prefix is present

    def test_repo_root_nonint_none_on_failure(self) -> None:
        import uxon.cli as cli

        def fake_run(cmd, **kw):
            class CP:
                returncode = 128
                stdout = ""
                stderr = "not a git repo"
            return CP()

        with mock.patch.object(cli.subprocess, "run", fake_run), \
             mock.patch.object(cli, "process_user", return_value="caller"):
            self.assertIsNone(cli.git_repo_root_nonint_as_user("/tmp/x", "devagent"))

    def test_common_dir_normalises_to_primary_root(self) -> None:
        import uxon.cli as cli

        # git rev-parse --git-common-dir on a linked worktree returns the
        # primary repo's .git; the primary root is its parent.
        def fake_run(cmd, **kw):
            class CP:
                returncode = 0
                stdout = "/srv/work/myapp/.git\n"
                stderr = ""
            return CP()

        with mock.patch.object(cli.subprocess, "run", fake_run), \
             mock.patch.object(cli, "process_user", return_value="caller"):
            root = cli.git_common_dir_root_as_user(
                "/srv/work/myapp/.uxon/worktrees/feat", "devagent"
            )
        self.assertEqual(root, cli.canonical("/srv/work/myapp"))
```

(Confirm the import alias used for `unittest.mock` at the top of `tests/test_uxon.py`; if it imports `from unittest import mock`, the `mock.patch` calls above match. If it uses `patch` directly, adjust.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon.py::NonintGitResolverTests -v`
Expected: FAIL with `AttributeError: module 'uxon.cli' has no attribute 'git_repo_root_nonint_as_user'`

- [ ] **Step 3: Write minimal implementation**

After `git_repo_root_as_user` in `src/uxon/cli.py`:

```python
def git_repo_root_nonint_as_user(cwd: str, target_user: str) -> str | None:
    """Non-interactive variant of :func:`git_repo_root_as_user`.

    Uses :func:`nonint_command_prefix_for_user` (``sudo -n``) so a missing
    NOPASSWD grant fails fast instead of blocking on a hidden password
    prompt — required for the fullscreen TUI's worktree probe (§4.2).
    """
    cp = subprocess.run(
        nonint_command_prefix_for_user(target_user)
        + ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
    )
    if cp.returncode != 0:
        return None
    out = (cp.stdout or "").strip()
    if not out:
        return None
    return canonical(out)


def git_common_dir_root_as_user(cwd: str, target_user: str) -> str | None:
    """Resolve the *primary* working tree of the repo containing ``cwd``.

    Uses ``git rev-parse --git-common-dir``: on a linked worktree this
    returns the primary repo's ``.git`` (whereas ``--show-toplevel``
    returns the *linked* worktree root). The primary root is that dir's
    parent. This anchors new worktrees to the primary repo even when
    launched from inside another worktree (§8 worktree-from-worktree).
    Non-interactive prefix, same rationale as the resolver above.
    """
    cp = subprocess.run(
        nonint_command_prefix_for_user(target_user)
        + ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
        text=True,
        capture_output=True,
    )
    if cp.returncode != 0:
        return None
    common = (cp.stdout or "").strip()
    if not common:
        return None
    common_abs = common if os.path.isabs(common) else os.path.join(cwd, common)
    # ``<root>/.git`` → ``<root>``.
    return canonical(os.path.dirname(common_abs))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon.py::NonintGitResolverTests -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon.py
git commit -m "feat(worktrees): non-interactive git repo-root + common-dir resolvers"
```

---

## Task 6 [CRITICAL]: Worktree-aware TUI planner stem (§2.5, §4.1)

**Files:**
- Modify: `src/uxon/cli.py` — `_plan_tui_run_agent` (~4336)
- Test: `tests/test_uxon.py`

**Why critical:** §2.5 — the planner that calls `allocate_session_name` must use the **repo-qualified** stem for a worktree target, or the created session is misnamed (`uxon-<branch>@…`) while the probe looks for `uxon-<repo>-<branch>@…`: silent duplicate + cross-repo hard `fail()`. We add an explicit, optional worktree stem path to `_plan_tui_run_agent`.

Scope note: only `_plan_tui_run_agent` is edited. `_plan_tui_existing_session_or_launch` serves the project-create / project-open flows (`<new_project_root>/<name>`), which never target a worktree path, so it keeps the basename stem unchanged. The existing-worktree TUI launch routes through `_plan_tui_run_agent` (via `on_launch_existing_worktree`, Task 14) with the worktree path as `cwd` and an explicit `(repo_root, branch)` so the stem is `session_stem_for_worktree`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon.py
class TuiPlannerWorktreeStemTests(unittest.TestCase):
    def test_run_agent_uses_worktree_stem_when_branch_given(self) -> None:
        import uxon.cli as cli

        captured = {}

        def fake_alloc(stem, agent, root, sessions, *, prefix):
            captured["stem"] = stem
            return f"{prefix}{stem}@{agent}"

        cfg = cli.load_config("/tmp")
        with mock.patch.object(cli, "ensure_launch_target_allowed", lambda *a, **k: None), \
             mock.patch.object(cli, "collect_sessions", return_value=[]), \
             mock.patch.object(cli, "allocate_session_name", fake_alloc), \
             mock.patch.object(cli, "_build_tmux_launch_request",
                               lambda *a, **k: cli._tui_launch_request_cls()(cmd=("true",), label="x")):
            cli._plan_tui_run_agent(
                cfg, "devagent", "/srv/work/myapp/.uxon/worktrees/feature-auth",
                "claude", "default",
                worktree=("/srv/work/myapp", "feature/auth"),
            )
        self.assertEqual(captured["stem"], "myapp-feature-auth")

    def test_run_agent_uses_path_stem_without_worktree(self) -> None:
        import uxon.cli as cli

        captured = {}

        def fake_alloc(stem, agent, root, sessions, *, prefix):
            captured["stem"] = stem
            return f"{prefix}{stem}@{agent}"

        cfg = cli.load_config("/tmp")
        with mock.patch.object(cli, "ensure_launch_target_allowed", lambda *a, **k: None), \
             mock.patch.object(cli, "collect_sessions", return_value=[]), \
             mock.patch.object(cli, "allocate_session_name", fake_alloc), \
             mock.patch.object(cli, "_build_tmux_launch_request",
                               lambda *a, **k: cli._tui_launch_request_cls()(cmd=("true",), label="x")):
            cli._plan_tui_run_agent(cfg, "devagent", "/srv/work/plain", "claude", "default")
        self.assertEqual(captured["stem"], "plain")
```

(Confirm `_tui_launch_request_cls` exists — it is referenced in `_build_tmux_launch_request`. Grep if unsure: `grep -n "_tui_launch_request_cls" src/uxon/cli.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon.py::TuiPlannerWorktreeStemTests -v`
Expected: FAIL — `_plan_tui_run_agent() got an unexpected keyword argument 'worktree'`

- [ ] **Step 3: Write minimal implementation**

Change `_plan_tui_run_agent` signature + stem selection:

```python
def _plan_tui_run_agent(
    cfg: Config,
    launch_user: str,
    cwd: str,
    agent_id: str,
    mode_id: str,
    worktree: tuple[str, str] | None = None,
):
    """Build a LaunchRequest for the TUI launch-into-folder action.

    When ``worktree`` is ``(repo_root, branch)`` the session stem is the
    repo-qualified :func:`session_stem_for_worktree` (§2.5) — identical to
    the stem the worktree-aware probe derives — instead of the
    basename-only :func:`session_stem_for_path`. ``cwd`` is the worktree
    path in that case; for a plain (primary / non-git) target ``worktree``
    is ``None`` and the basename stem is used unchanged.
    """
    ensure_launch_target_allowed(cfg, launch_user, cwd)
    target_dir = cwd
    if worktree is not None:
        repo_root, branch = worktree
        session_stem = session_stem_for_worktree(repo_root, branch)
    else:
        session_stem = session_stem_for_path(target_dir)
    sessions = collect_sessions([launch_user], cfg)
    session = allocate_session_name(
        session_stem, agent_id, target_dir, sessions, prefix=cfg.session_prefix
    )
    args = ParsedArgs(action="run", agent=agent_id, permission_mode=mode_id)
    return _build_tmux_launch_request(target_dir, session, args, cfg, None, launch_user)
```

Note: `_build_tmux_launch_request` is called with `branch=None` even for a worktree, because uxon launches with `-c <worktree_path>` and **no** native `-w` (Task 9 removes the passthrough). The session is already named by the worktree-aware stem above.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon.py::TuiPlannerWorktreeStemTests -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon.py
git commit -m "feat(worktrees): worktree-aware stem in TUI run planner (§2.5)"
```

---

## Task 7 [CRITICAL]: Worktree-aware probe (`probe_tui_compatible_sessions(..., stem=)`) (§2.5, §4.2)

**Files:**
- Modify: `src/uxon/cli.py` — `probe_tui_compatible_sessions` (~4470)
- Test: `tests/test_uxon.py`

**Why critical:** §2.5/§4.2 — the commit-time probe must derive the **same** repo-qualified stem the planner used. We generalise the probe to accept an explicit stem (default: basename, unchanged behaviour) and an explicit compatibility root, so worktree targets probe by `session_stem_for_worktree`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon.py
class ProbeWorktreeStemTests(unittest.TestCase):
    def _session(self, name: str, path: str):
        import uxon.cli as cli
        return cli.SessionInfo(
            user="devagent", name=name, attached="0", windows="1",
            created="", last_attached="", pane_pids=(), active_pid=None,
            active_cmd="claude", active_path=path,
        )

    def test_explicit_stem_matches_worktree_session(self) -> None:
        import uxon.cli as cli

        wt = "/srv/work/myapp/.uxon/worktrees/feature-auth"
        sess = [self._session("uxon-myapp-feature-auth@claude", wt)]
        cfg = cli.load_config("/tmp")
        with mock.patch.object(cli, "collect_sessions", return_value=sess):
            out = cli.probe_tui_compatible_sessions(
                cfg, "devagent", wt, "claude",
                stem="myapp-feature-auth", compatibility_root=wt,
            )
        self.assertEqual([s.name for s in out], ["uxon-myapp-feature-auth@claude"])

    def test_default_stem_unchanged_for_plain_target(self) -> None:
        import uxon.cli as cli

        target = "/srv/work/plain"
        sess = [self._session("uxon-plain@claude", target)]
        cfg = cli.load_config("/tmp")
        with mock.patch.object(cli, "collect_sessions", return_value=sess):
            out = cli.probe_tui_compatible_sessions(cfg, "devagent", target, "claude")
        self.assertEqual([s.name for s in out], ["uxon-plain@claude"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon.py::ProbeWorktreeStemTests -v`
Expected: FAIL — `probe_tui_compatible_sessions() got an unexpected keyword argument 'stem'`

- [ ] **Step 3: Write minimal implementation**

```python
def probe_tui_compatible_sessions(
    cfg: Config,
    launch_user: str,
    target_dir: str,
    agent_id: str,
    *,
    stem: str | None = None,
    compatibility_root: str | None = None,
) -> tuple[SessionInfo, ...]:
    """Return launch_user's sessions compatible with the target + agent.

    ``stem`` and ``compatibility_root`` default to the basename-derived
    stem and the target dir (the unchanged primary/non-worktree path). For
    a worktree target the caller passes the repo-qualified
    :func:`session_stem_for_worktree` and the worktree path so the probe
    derives the *same* stem the planner used (§2.5) — generalising here
    rather than always deriving from the basename is the fix that keeps
    the attach guard reliable across repos.
    """
    target_canonical = canonical(target_dir)
    session_stem = stem if stem is not None else session_stem_for_path(target_canonical)
    root = canonical(compatibility_root) if compatibility_root is not None else target_canonical
    sessions = collect_sessions([launch_user], cfg)
    matches = compatible_indexed_sessions(
        session_stem,
        agent_id,
        root,
        sessions,
        prefix=cfg.session_prefix,
        legacy_prefixes=cfg.legacy_session_prefixes,
    )
    return tuple(matches)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon.py::ProbeWorktreeStemTests -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon.py
git commit -m "feat(worktrees): worktree-aware probe stem (§2.5)"
```

---

## Task 8 [CRITICAL]: Identity regression test (§2.5, §9 identity test)

**Files:**
- Test: `tests/test_uxon.py`

**Why critical:** This is the §9 regression guard for the §2.5 fix. It asserts (a) the planner names the session with `session_stem_for_worktree` — the *allocated name*, not just that the probe matches; (b) the worktree-aware probe then finds that session; (c) two repos with a same-named worktree do **not** collide / hard-`fail()`.

- [ ] **Step 1: Write the failing test (it will pass only because Tasks 6+7 are done — this task locks the invariant)**

```python
# add to tests/test_uxon.py
class WorktreeIdentityRegressionTests(unittest.TestCase):
    """Regression guard for §2.5: planner and probe derive the SAME
    repo-qualified stem; cross-repo same-named worktrees never collide.
    """

    def _session(self, name: str, path: str):
        import uxon.cli as cli
        return cli.SessionInfo(
            user="devagent", name=name, attached="0", windows="1",
            created="", last_attached="", pane_pids=(), active_pid=None,
            active_cmd="claude", active_path=path,
        )

    def test_planner_allocates_repo_qualified_name_probe_then_matches(self) -> None:
        import uxon.cli as cli

        repo = "/srv/work/myapp"
        wt = "/srv/work/myapp/.uxon/worktrees/feature-auth"
        branch = "feature/auth"
        cfg = cli.load_config("/tmp")

        # (a) planner names the session with the worktree stem.
        with mock.patch.object(cli, "ensure_launch_target_allowed", lambda *a, **k: None), \
             mock.patch.object(cli, "collect_sessions", return_value=[]), \
             mock.patch.object(cli, "_build_tmux_launch_request",
                               lambda td, s, *a, **k: cli._tui_launch_request_cls()(
                                   cmd=("true",), label=f"launch {s}")):
            req = cli._plan_tui_run_agent(cfg, "devagent", wt, "claude", "default",
                                          worktree=(repo, branch))
        self.assertEqual(req.label, "launch uxon-myapp-feature-auth@claude")

        # (b) the worktree-aware probe finds exactly that session.
        live = [self._session("uxon-myapp-feature-auth@claude", wt)]
        with mock.patch.object(cli, "collect_sessions", return_value=live):
            found = cli.probe_tui_compatible_sessions(
                cfg, "devagent", wt, "claude",
                stem=cli.session_stem_for_worktree(repo, branch),
                compatibility_root=wt,
            )
        self.assertEqual([s.name for s in found], ["uxon-myapp-feature-auth@claude"])

    def test_two_repos_same_branch_do_not_collide(self) -> None:
        import uxon.cli as cli

        repo_a, repo_b = "/srv/work/alpha", "/srv/work/beta"
        wt_a = "/srv/work/alpha/.uxon/worktrees/feature"
        wt_b = "/srv/work/beta/.uxon/worktrees/feature"
        cfg = cli.load_config("/tmp")
        # alpha's worktree session is live; probing beta's worktree must
        # NOT match it and must NOT hard-fail (distinct repo-qualified stems).
        live = [self._session("uxon-alpha-feature@claude", wt_a)]
        with mock.patch.object(cli, "collect_sessions", return_value=live):
            found = cli.probe_tui_compatible_sessions(
                cfg, "devagent", wt_b, "claude",
                stem=cli.session_stem_for_worktree(repo_b, "feature"),
                compatibility_root=wt_b,
            )
        self.assertEqual(found, ())  # no match, no SystemExit
```

- [ ] **Step 2: Run test to verify it passes (invariant holds with Tasks 6+7)**

Run: `pytest tests/test_uxon.py::WorktreeIdentityRegressionTests -v`
Expected: PASS (2 passed). If it FAILS with a mismatch on the label or a `SystemExit` (session conflict), Tasks 6/7 are wrong — fix there, not here.

- [ ] **Step 3: (no implementation — this task is the guard)**

- [ ] **Step 4: Re-run to confirm green**

Run: `pytest tests/test_uxon.py::WorktreeIdentityRegressionTests -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_uxon.py
git commit -m "test(worktrees): §2.5 identity regression guard"
```

---

## Task 9: Drop native `-w` passthrough from the launch builder (§2.1, §4.1, §5)

**Files:**
- Modify: `src/uxon/cli.py` — `_build_tmux_launch_request` (~3400-3409)
- Test: `tests/test_uxon.py`

uxon no longer passes `-w <branch>` to the agent binary and no longer rejects `-w` for non-claude. The worktree is created by `git worktree add` (Task 11) and launched with `-c <worktree_path>`. The `branch` parameter of `_build_tmux_launch_request` becomes informational only (still printed in dry-run) — it must **not** append `-w`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon.py
class LaunchBuilderNoNativeWorktreeTests(unittest.TestCase):
    def test_branch_does_not_add_native_w_flag(self) -> None:
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        args = cli.ParsedArgs(action="run", agent="claude", permission_mode="default")
        req = cli._build_tmux_launch_request(
            "/srv/work/myapp/.uxon/worktrees/feat", "uxon-myapp-feat@claude",
            args, cfg, "feat", "devagent",
        )
        joined = " ".join(req.cmd)
        self.assertNotIn(" -w ", f" {joined} ")
        self.assertNotIn("-w feat", joined)

    def test_branch_allowed_for_non_claude_agent(self) -> None:
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        args = cli.ParsedArgs(action="run", agent="codex", permission_mode="default")
        # Must not raise the old "-w is only supported for claude" fail().
        req = cli._build_tmux_launch_request(
            "/srv/work/myapp/.uxon/worktrees/feat", "uxon-myapp-feat@codex",
            args, cfg, "feat", "devagent",
        )
        self.assertTrue(req.cmd)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon.py::LaunchBuilderNoNativeWorktreeTests -v`
Expected: FAIL — `test_branch_allowed_for_non_claude_agent` raises `SystemExit` (the old guard), and/or `-w feat` appears in cmd.

- [ ] **Step 3: Write minimal implementation**

Remove these two blocks from `_build_tmux_launch_request`:

```python
    if branch and agent_id != "claude":
        fail(f"-w/--worktree is only supported for claude (got agent={agent_id})")
```

and

```python
    if branch:
        final_cmd += ["-w", branch]
```

Leave the `branch` parameter in the signature (callers and the dry-run print in `launch_in_tmux` still pass/print it) but it no longer affects `final_cmd`. Update the function docstring to note uxon launches worktrees via `-c <path>`, not the agent's native `-w`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon.py::LaunchBuilderNoNativeWorktreeTests -v`
Expected: PASS (2 passed). Also run the full launch-builder suite to catch any test that asserted the old passthrough: `pytest tests/test_uxon.py -k "launch or worktree or build_tmux" -v` and fix any now-stale assertion (the old `-w` passthrough behaviour is intentionally gone).

- [ ] **Step 5: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon.py
git commit -m "feat(worktrees): stop delegating to native -w; launch with -c <path>"
```

---

## Task 10: `.git/info/exclude` writer + `.worktreeinclude` copy (§2.3, §2.4)

**Files:**
- Modify: `src/uxon/cli.py` (add `write_uxon_exclude_entry`, `copy_worktreeinclude_matches` near the worktree helpers)
- Test: `tests/test_uxon.py`

These run as `launch_user` and are side-effecting; tests drive them against a real temp git repo (same-user fast path, so no `sudo`). The exclude append is idempotent + concurrency-safe (temp-file-then-rename). The copy set is `A ∩ B` of two `git ls-files` queries — git is the sole authority (§2.4).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon.py
import os
import subprocess as _sp


def _init_repo(path: str) -> None:
    _sp.run(["git", "init", "-q", path], check=True)
    _sp.run(["git", "-C", path, "config", "user.email", "t@t"], check=True)
    _sp.run(["git", "-C", path, "config", "user.name", "t"], check=True)


class ExcludeWriterTests(unittest.TestCase):
    def test_appends_uxon_line_once_idempotent(self) -> None:
        import uxon.cli as cli
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            cli.write_uxon_exclude_entry(d, "devagent")
            cli.write_uxon_exclude_entry(d, "devagent")  # idempotent
            text = open(os.path.join(d, ".git", "info", "exclude")).read()
        self.assertEqual(text.count(".uxon/"), 1)


class WorktreeIncludeCopyTests(unittest.TestCase):
    def test_copies_only_gitignored_and_matching(self) -> None:
        import uxon.cli as cli
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            # tracked file (must NOT copy), gitignored+matching (.env, copy),
            # gitignored+not-matching (debug.log, skip).
            open(os.path.join(d, "tracked.txt"), "w").write("x")
            open(os.path.join(d, ".gitignore"), "w").write(".env\n*.log\n")
            open(os.path.join(d, ".worktreeinclude"), "w").write(".env\n")
            open(os.path.join(d, ".env"), "w").write("SECRET=1")
            open(os.path.join(d, "debug.log"), "w").write("noise")
            _sp.run(["git", "-C", d, "add", "tracked.txt", ".gitignore",
                     ".worktreeinclude"], check=True)
            _sp.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
            dest = os.path.join(d, ".uxon", "worktrees", "feat")
            os.makedirs(dest)
            cli.copy_worktreeinclude_matches(d, dest, "devagent")
            self.assertTrue(os.path.exists(os.path.join(dest, ".env")))
            self.assertFalse(os.path.exists(os.path.join(dest, "debug.log")))
            self.assertFalse(os.path.exists(os.path.join(dest, "tracked.txt")))

    def test_no_worktreeinclude_is_noop(self) -> None:
        import uxon.cli as cli
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            dest = os.path.join(d, "dest")
            os.makedirs(dest)
            cli.copy_worktreeinclude_matches(d, dest, "devagent")  # no raise
            self.assertEqual(os.listdir(dest), [])
```

(These tests run git as the current user; `command_prefix_for_user` returns `[]` when `process_user() == target_user`, so no `sudo` is invoked — keep them as real-repo tests, not mocks.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon.py::ExcludeWriterTests tests/test_uxon.py::WorktreeIncludeCopyTests -v`
Expected: FAIL — `module 'uxon.cli' has no attribute 'write_uxon_exclude_entry'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/uxon/cli.py` (near the other worktree helpers):

```python
_UXON_EXCLUDE_LINE = ".uxon/"


def write_uxon_exclude_entry(repo_root: str, launch_user: str) -> None:
    """Idempotently append ``.uxon/`` to ``.git/info/exclude`` as launch_user.

    Local-only (never committed) and concurrency-safe: read-modify-write
    via a temp file + atomic rename so two simultaneous ``launch_user``
    creates can't double-append or clobber each other (§2.3). Skipped by
    the caller when ``worktree_root`` is set (out-of-repo worktree).
    """
    prefix = command_prefix_for_user(launch_user)
    exclude_path = os.path.join(repo_root, ".git", "info", "exclude")
    # Read current contents (tolerate absent file).
    cp = subprocess.run(
        prefix + ["sh", "-c", f"cat {shlex.quote(exclude_path)} 2>/dev/null || true"],
        text=True,
        capture_output=True,
    )
    current = cp.stdout or ""
    if any(line.strip() == _UXON_EXCLUDE_LINE for line in current.splitlines()):
        return  # already present — idempotent
    new_contents = current
    if new_contents and not new_contents.endswith("\n"):
        new_contents += "\n"
    new_contents += _UXON_EXCLUDE_LINE + "\n"
    # Atomic temp-file-then-rename under the info/ dir (same filesystem),
    # serialising the read-modify-write against concurrent writers.
    info_dir = os.path.join(repo_root, ".git", "info")
    script = (
        f"mkdir -p {shlex.quote(info_dir)} && "
        f"tmp=$(mktemp {shlex.quote(info_dir)}/exclude.XXXXXX) && "
        f"cat > \"$tmp\" && mv -f \"$tmp\" {shlex.quote(exclude_path)}"
    )
    # run_cmd() does not forward stdin, so feed the new contents directly
    # via subprocess.run (same capture/text conventions as run_cmd) and
    # fail() with the captured stderr on a non-zero exit.
    cp = subprocess.run(
        prefix + ["sh", "-c", script],
        text=True,
        input=new_contents,
        capture_output=True,
    )
    if cp.returncode != 0:
        fail((cp.stderr or "").strip() or "failed to write .git/info/exclude")


def copy_worktreeinclude_matches(repo_root: str, dest: str, launch_user: str) -> None:
    """Copy gitignored files matching ``.worktreeinclude`` into ``dest``.

    Copy set = ``A ∩ B`` where A = ``git ls-files -o -i --exclude-standard``
    (gitignored + untracked) and B = ``git ls-files -o -i
    --exclude-from=<.worktreeinclude>`` (untracked matching the include
    patterns). Both queries are ``--others`` so tracked files are excluded
    by construction; git is the sole authority for ignore + match (§2.4).
    No-op when ``.worktreeinclude`` is absent.
    """
    prefix = command_prefix_for_user(launch_user)
    include_file = os.path.join(repo_root, ".worktreeinclude")
    if not os.path.exists(include_file):
        return

    def _ls(extra: list[str]) -> set[str]:
        cp = subprocess.run(
            prefix + ["git", "-C", repo_root, "ls-files", "-o", "-i"] + extra,
            text=True,
            capture_output=True,
        )
        if cp.returncode != 0:
            return set()
        return {ln for ln in (cp.stdout or "").splitlines() if ln.strip()}

    set_a = _ls(["--exclude-standard"])
    set_b = _ls([f"--exclude-from={include_file}"])
    for rel in sorted(set_a & set_b):
        src = os.path.join(repo_root, rel)
        dst = os.path.join(dest, rel)
        run_cmd(prefix + ["mkdir", "-p", os.path.dirname(dst)], check=True)
        run_cmd(prefix + ["cp", "-p", src, dst], check=True)
```

(Note: `run_cmd` at `src/uxon/cli.py:1175` does **not** forward stdin and calls `fail()` on non-zero — that is why `write_uxon_exclude_entry` uses `subprocess.run(..., input=...)` directly. The `cp` / `cp -p` / `mkdir` calls inside `copy_worktreeinclude_matches` stay on `run_cmd` since they take no stdin.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon.py::ExcludeWriterTests tests/test_uxon.py::WorktreeIncludeCopyTests -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon.py
git commit -m "feat(worktrees): idempotent .git/info/exclude + .worktreeinclude copy"
```

---

## Task 11 [CRITICAL]: `plan_worktree_launch` create planner + audit (§4.1, §4.6, §8 edges)

**Files:**
- Modify: `src/uxon/cli.py` (add `plan_worktree_launch`)
- Test: `tests/test_uxon.py`

**Why critical:** the single create-and-launch planner used by both the CLI `-w` flag (on a "new" decision) and the TUI new-worktree path (§4.1). It **gates the computed path** through the not-yet-exists predicate (§2.3 — parent-writable + `is_under_allowed_roots`, like `ensure_new_project_target_allowed`, NOT `ensure_launch_target_allowed` which hard-fails on a missing dir), runs `git worktree add` (new branch off `worktree_base`, or checkout existing), copies `.worktreeinclude`, writes the exclude entry, and launches with the worktree-aware stem. Emits **both** `worktree.create` **and** `session.new` for the launched session (§4.6: the session "still emits its own `session.new`; `worktree.create` is the additional event, not a replacement"). Handles §8 edges (slug collision, branch-already-checked-out) as clear errors. It does **not** decide attach-vs-new — the CLI caller keeps that guard (see Task 12); by the time `plan_worktree_launch` runs the decision is already "new".

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon.py
class PlanWorktreeLaunchTests(unittest.TestCase):
    def test_new_branch_local_base_adds_worktree_and_names_session(self) -> None:
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        repo = "/srv/work/myapp"
        calls: list[list[str]] = []

        def fake_run_cmd(cmd, check=True, **kw):
            calls.append(cmd)
            class CP:
                returncode = 0
                stdout = ""
                stderr = ""
            return CP()

        events: list[tuple[str, dict]] = []

        def fake_audit(event, **fields):
            events.append((event, fields))

        with mock.patch.object(cli, "is_worktree_target_allowed", return_value=True), \
             mock.patch.object(cli, "collect_sessions", return_value=[]), \
             mock.patch.object(cli, "run_cmd", fake_run_cmd), \
             mock.patch.object(cli, "write_uxon_exclude_entry", lambda *a, **k: None), \
             mock.patch.object(cli, "copy_worktreeinclude_matches", lambda *a, **k: None), \
             mock.patch.object(cli, "_local_base_ref_as_user", return_value="origin/HEAD"), \
             mock.patch.object(cli, "_branch_exists_as_user", return_value=False), \
             mock.patch.object(cli, "_build_tmux_launch_request",
                               lambda td, s, *a, **k: cli._tui_launch_request_cls()(
                                   cmd=("true",), label=f"launch {s}")), \
             mock.patch("uxon.audit.audit", fake_audit):
            req = cli.plan_worktree_launch(
                cfg, "devagent", repo, "feature/auth", "claude", "default",
            )
        # session named with the worktree stem
        self.assertEqual(req.label, "launch uxon-myapp-feature-auth@claude")
        # a `git worktree add ... -b feature/auth` was issued
        add = [c for c in calls if "worktree" in c and "add" in c]
        self.assertTrue(add)
        self.assertIn("-b", add[0])
        # BOTH worktree.create AND session.new emitted (§4.6, B3).
        names = [e for e, _ in events]
        self.assertIn("worktree.create", names)
        self.assertIn("session.new", names)
        wc = dict(events[names.index("worktree.create")][1])
        self.assertEqual(wc.get("branch"), "feature/auth")
        self.assertEqual(wc.get("project"), repo)
        self.assertEqual(wc.get("base"), "local")
        self.assertEqual(wc.get("agent"), "claude")
        self.assertEqual(wc.get("session"), "uxon-myapp-feature-auth@claude")
        self.assertTrue(wc.get("path", "").endswith("/.uxon/worktrees/feature-auth"))
        sn = dict(events[names.index("session.new")][1])
        self.assertEqual(sn.get("session"), "uxon-myapp-feature-auth@claude")
        self.assertEqual(sn.get("branch"), "feature/auth")
        self.assertEqual(sn.get("project"), repo)

    def test_worktree_root_outside_allowed_roots_rejected(self) -> None:
        # B1 / §2.3 / §9 "gating failure → clear error": a worktree_root
        # pointing outside allowed_roots must fail with an actionable error
        # BEFORE any git work runs.
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        cfg.worktree_root = "/not/allowed"
        cfg.allowed_roots = ["/srv/work"]
        called: list[list[str]] = []

        def fake_run_cmd(cmd, check=True, **kw):
            called.append(cmd)
            class CP:
                returncode = 0; stdout = ""; stderr = ""
            return CP()

        with mock.patch.object(cli, "probe_cwd_writable", return_value=True), \
             mock.patch.object(cli, "run_cmd", fake_run_cmd):
            with self.assertRaises(SystemExit) as cm:
                cli.plan_worktree_launch(cfg, "devagent", "/srv/work/myapp",
                                         "feature/auth", "claude", "default")
        msg = getattr(cm.exception, "uxon_msg", "")
        self.assertIn("allowed_roots", msg)
        self.assertIn("worktree_root", msg)  # error suggests the override key
        # No git worktree add was attempted before the gate failed.
        self.assertFalse([c for c in called if "worktree" in c and "add" in c])

    def test_existing_branch_checks_out_without_b(self) -> None:
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        calls: list[list[str]] = []

        def fake_run_cmd(cmd, check=True, **kw):
            calls.append(cmd)
            class CP:
                returncode = 0; stdout = ""; stderr = ""
            return CP()

        with mock.patch.object(cli, "ensure_launch_target_allowed", lambda *a, **k: None), \
             mock.patch.object(cli, "collect_sessions", return_value=[]), \
             mock.patch.object(cli, "run_cmd", fake_run_cmd), \
             mock.patch.object(cli, "write_uxon_exclude_entry", lambda *a, **k: None), \
             mock.patch.object(cli, "copy_worktreeinclude_matches", lambda *a, **k: None), \
             mock.patch.object(cli, "_branch_exists_as_user", return_value=True), \
             mock.patch.object(cli, "_build_tmux_launch_request",
                               lambda td, s, *a, **k: cli._tui_launch_request_cls()(
                                   cmd=("true",), label=f"launch {s}")), \
             mock.patch("uxon.audit.audit", lambda *a, **k: None):
            cli.plan_worktree_launch(cfg, "devagent", "/srv/work/myapp",
                                     "existing", "claude", "default")
        add = [c for c in calls if "worktree" in c and "add" in c]
        self.assertTrue(add)
        self.assertNotIn("-b", add[0])

    def test_worktree_add_failure_surfaces_clear_error(self) -> None:
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")

        # The planner runs the add with check=False and inspects the
        # result itself (run_cmd's own failure path calls fail() with the
        # raw git stderr; the planner wants a friendlier message). Simulate
        # git refusing because the branch is already checked out.
        def fake_run_cmd(cmd, check=True, **kw):
            class CP:
                stdout = ""
                stderr = ""
                returncode = 0
            if "worktree" in cmd and "add" in cmd:
                CP.returncode = 128
                CP.stderr = "fatal: 'feature/auth' is already checked out at '...'"
            return CP()

        with mock.patch.object(cli, "ensure_launch_target_allowed", lambda *a, **k: None), \
             mock.patch.object(cli, "collect_sessions", return_value=[]), \
             mock.patch.object(cli, "run_cmd", fake_run_cmd), \
             mock.patch.object(cli, "write_uxon_exclude_entry", lambda *a, **k: None), \
             mock.patch.object(cli, "_branch_exists_as_user", return_value=False), \
             mock.patch.object(cli, "_local_base_ref_as_user", return_value="HEAD"):
            with self.assertRaises(SystemExit) as cm:
                cli.plan_worktree_launch(cfg, "devagent", "/srv/work/myapp",
                                         "feature/auth", "claude", "default")
        # Friendly message, not the raw git fatal. fail() stashes the
        # human-readable text on the SystemExit as ``uxon_msg`` (str() of a
        # SystemExit yields only the exit code).
        self.assertIn("already checked out", getattr(cm.exception, "uxon_msg", ""))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon.py::PlanWorktreeLaunchTests -v`
Expected: FAIL — `module 'uxon.cli' has no attribute 'plan_worktree_launch'`

- [ ] **Step 3: Write minimal implementation**

Add the gating helper, the base-ref / branch-existence helpers, and the planner to `src/uxon/cli.py`:

```python
def is_worktree_target_allowed(cfg: Config, launch_user: str, worktree_path: str) -> bool:
    """Return True if ``worktree_path`` may be created by uxon.

    Not-yet-exists predicate (the worktree dir does not exist yet — this
    is why ``ensure_launch_target_allowed``/``is_launch_target_allowed``,
    which hard-fail on a missing dir, cannot be used here). Mirrors
    :func:`is_new_project_target_allowed`: the *parent* must be writable by
    ``launch_user`` and the path must satisfy the ``allowed_roots``
    whitelist when non-empty (§2.3). The parent is created later by the
    caller; here we only check policy.
    """
    parent = os.path.dirname(worktree_path) or "/"
    # The immediate parent may not exist yet (e.g. ``.uxon/worktrees`` on
    # first use); walk up to the nearest existing ancestor for the
    # write-access probe, which is what mkdir -p will actually need.
    probe_dir = parent
    while probe_dir and probe_dir != "/" and not os.path.isdir(probe_dir):
        probe_dir = os.path.dirname(probe_dir)
    if not probe_cwd_writable(launch_user, probe_dir):
        return False
    return is_under_allowed_roots(cfg, worktree_path)


def _branch_exists_as_user(repo_root: str, branch: str, launch_user: str) -> bool:
    cp = subprocess.run(
        nonint_command_prefix_for_user(launch_user)
        + ["git", "-C", repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        text=True,
        capture_output=True,
    )
    return cp.returncode == 0


def _local_base_ref_as_user(repo_root: str, launch_user: str) -> str:
    """Local base ref for a new branch: local origin/HEAD if present, else HEAD.

    No network — origin/HEAD is consulted only if a local remote-tracking
    symref exists (``worktree_base = "local"`` contract, §4.5).
    """
    cp = subprocess.run(
        nonint_command_prefix_for_user(launch_user)
        + ["git", "-C", repo_root, "rev-parse", "--verify", "--quiet", "origin/HEAD"],
        text=True,
        capture_output=True,
    )
    return "origin/HEAD" if cp.returncode == 0 else "HEAD"


def _remote_base_ref_as_user(repo_root: str, launch_user: str) -> str:
    """Base ref after a ``worktree_base = "remote"`` fetch (§4.5, C4).

    ``git fetch origin`` does NOT create the local ``origin/HEAD`` symref
    (only clone / ``git remote set-head`` do), so we cannot assume it
    exists. Establish it explicitly via ``git remote set-head origin -a``
    (a local, network-free operation that points ``origin/HEAD`` at the
    remote's default branch using the already-fetched refs); then use
    ``origin/HEAD``. If that still fails (no default detectable), fall
    back to the verified local resolver so the add never gets a
    non-existent ref.
    """
    prefix = command_prefix_for_user(launch_user)
    run_cmd(prefix + ["git", "-C", repo_root, "remote", "set-head", "origin", "-a"], check=False)
    cp = subprocess.run(
        nonint_command_prefix_for_user(launch_user)
        + ["git", "-C", repo_root, "rev-parse", "--verify", "--quiet", "origin/HEAD"],
        text=True,
        capture_output=True,
    )
    if cp.returncode == 0:
        return "origin/HEAD"
    return _local_base_ref_as_user(repo_root, launch_user)


def plan_worktree_launch(
    cfg: Config,
    launch_user: str,
    repo_root: str,
    branch_name: str,
    agent_id: str,
    mode_id: str,
    *,
    dry_run: bool = False,
):
    """Create a uxon-managed worktree and return a launch request for it.

    Single create-and-launch planner for both the CLI ``-w`` flag (on a
    "new" decision — the CLI keeps its own attach-vs-new guard, Task 12)
    and the TUI new-worktree path (§4.1). Gates the computed path via the
    not-yet-exists predicate (§2.3); when ``worktree_base == "remote"``
    fetches origin first, else stays local and network-free (§4.5). Adds
    the worktree (``-b`` for a new branch, plain checkout for an existing
    one), copies ``.worktreeinclude`` (§2.4), writes the
    ``.git/info/exclude`` entry unless ``worktree_root`` moves the tree out
    of the repo (§2.3), then launches with the worktree-aware stem (§2.5).
    Emits **both** ``worktree.create`` and ``session.new`` for the launched
    session (§4.6, B3).

    ``dry_run=True`` (CLI ``-w --dry-run``) still gates the path and
    resolves the base ref / branch existence, but prints the git commands
    instead of running ``git worktree add`` / copy / exclude, and emits no
    audit events — no side effects. The returned LaunchRequest is built
    against the computed (not-yet-created) worktree path so the caller can
    print the exec line.
    """
    from uxon import audit as _audit

    worktree_path = compute_worktree_path(
        repo_root=repo_root, branch=branch_name, worktree_root=cfg.worktree_root
    )
    # Gate the computed path BEFORE any git work or mkdir (§2.3, B1). An
    # out-of-roots worktree_root is the common failure — name the override
    # key in the error so the operator knows how to fix it. Runs in dry-run
    # too, so a misconfigured worktree_root is caught without side effects.
    if not is_worktree_target_allowed(cfg, launch_user, worktree_path):
        eprint("uxon: worktree directory must be under one of allowed_roots:")
        for base_root in cfg.allowed_roots:
            eprint(f"uxon:   - {base_root}")
        fail(
            f"got: {worktree_path} — set worktree_root to a path inside allowed_roots "
            "(and writable by the launch user) to relocate worktrees"
        )

    prefix = command_prefix_for_user(launch_user)
    base = cfg.worktree_base
    branch_exists = _branch_exists_as_user(repo_root, branch_name, launch_user)
    if branch_exists:
        add_cmd = prefix + ["git", "-C", repo_root, "worktree", "add",
                            worktree_path, branch_name]
    else:
        # For remote base the real ref is resolved AFTER the fetch (below);
        # use a provisional "origin/HEAD" here so the dry-run print and the
        # request shape are correct. No fetch / set-head side effect runs in
        # this pre-guard block (those are post-dry-run-guard, non-dry-run).
        if base == "remote":
            base_ref = "origin/HEAD"
        else:
            base_ref = _local_base_ref_as_user(repo_root, launch_user)
        add_cmd = prefix + ["git", "-C", repo_root, "worktree", "add",
                            worktree_path, "-b", branch_name, base_ref]

    parent = os.path.dirname(worktree_path)
    session_stem = session_stem_for_worktree(repo_root, branch_name)
    sessions = collect_sessions([launch_user], cfg)
    session = allocate_session_name(
        session_stem, agent_id, worktree_path, sessions, prefix=cfg.session_prefix
    )
    run_args = ParsedArgs(action="run", agent=agent_id, permission_mode=mode_id)
    req = _build_tmux_launch_request(worktree_path, session, run_args, cfg, None, launch_user)

    if dry_run:
        # No side effects: print the git plan, skip add/copy/exclude/audit.
        print(f"worktree_path={shlex.quote(worktree_path)}")
        if base == "remote":
            print(f"fetch={shlex.join(prefix + ['git', '-C', repo_root, 'fetch', 'origin'])}")
        print(f"worktree_add={shlex.join(add_cmd)}")
        return req

    run_cmd(prefix + ["mkdir", "-p", parent], check=True)
    # ``.uxon/`` exclusion must precede the first add so the in-tree
    # worktree never shows as untracked (§2.3); skipped for out-of-repo.
    if not cfg.worktree_root:
        write_uxon_exclude_entry(repo_root, launch_user)
    if base == "remote":
        run_cmd(prefix + ["git", "-C", repo_root, "fetch", "origin"], check=True)
        if not branch_exists:
            # Re-resolve the base ref post-fetch (set-head needs the fetch).
            add_cmd[-1] = _remote_base_ref_as_user(repo_root, launch_user)
    # Run with check=False and inspect the result ourselves: run_cmd's own
    # failure path would surface the raw ``fatal:`` git stderr; we want a
    # friendlier, actionable message for the §8 edges.
    cp = run_cmd(add_cmd, check=False)
    if cp.returncode != 0:
        stderr = (cp.stderr or cp.stdout or "").strip()
        if "already checked out" in stderr:
            fail(
                f"branch {branch_name!r} is already checked out in another worktree — "
                "use that workspace row instead of creating a new one"
            )
        fail(
            f"worktree path already exists or git refused the add: {worktree_path} "
            f"(pick another branch name). git said: {stderr or 'no detail'}"
        )

    copy_worktreeinclude_matches(repo_root, worktree_path, launch_user)

    _audit.audit(
        "worktree.create",
        agent=agent_id,
        project=repo_root,
        branch=branch_name,
        path=worktree_path,
        base=base,
        session=session,
    )
    # §4.6 / B3: the launched session still emits its own session.new —
    # worktree.create is the ADDITIONAL lifecycle event, not a replacement.
    _audit.audit(
        "session.new",
        agent=agent_id,
        project=worktree_path,
        branch=branch_name,
        session=session,
        dry_run=False,
    )
    return req
```

Note: `eprint`, `probe_cwd_writable`, `is_under_allowed_roots` already exist in `cli.py` (used by `ensure_new_project_target_allowed` / `is_new_project_target_allowed`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon.py::PlanWorktreeLaunchTests -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon.py
git commit -m "feat(worktrees): plan_worktree_launch create planner + worktree.create audit"
```

---

## Task 12: CLI parity — route `-w` through `plan_worktree_launch` (§5, §8, §9 CLI dry-run)

**Files:**
- Modify: `src/uxon/cli.py` — `do_run` (~3690), `do_new` (~3477)
- Test: `tests/test_uxon.py`

`uxon -w <branch>` and `uxon new <name> -w <branch>` now create a uxon-managed worktree via `plan_worktree_launch` instead of the old native passthrough. `do_run` resolves the repo via `git_repo_root_nonint_as_user` first (and normalises worktree-from-worktree via `git_common_dir_root_as_user`, §8). `plan_worktree_launch` emits BOTH `worktree.create` and `session.new` (Task 11, B3).

**C1 — attach-vs-new on the CLI `-w` path is preserved.** `do_new -w` keeps its existing attach-vs-new guard (`compatible_indexed_sessions` + `choose_attach_session` + `resolve_repeat_decision`, cli.py:3521-3561): if a compatible worktree session already exists and the decision is "attach", it attaches (emitting `session.attach`) and never creates a worktree. Only on a "new" decision (or no existing session) does it call `plan_worktree_launch`. `do_run -w` has no attach guard today (it allocates a fresh indexed session unconditionally), so it routes straight through `plan_worktree_launch` — unchanged decision semantics, just the new backend. This is a behaviour change only in *what* a new worktree session does (uxon-managed dir vs native `-w`), not in the attach decision — call it out in the CHANGELOG (Task 20) and rewrite the two affected tests (Step 4 below).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon.py
class CliWorktreeRoutingTests(unittest.TestCase):
    def test_do_run_w_routes_through_plan_worktree_launch(self) -> None:
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        args = cli.ParsedArgs(action="run", agent="claude", permission_mode="default",
                              worktree_branch="feature/auth", dry_run=True)
        captured = {}

        def fake_plan(cfg_, user, repo, branch, agent, mode, *, dry_run=False):
            captured.update(repo=repo, branch=branch, agent=agent, dry_run=dry_run)
            return cli._tui_launch_request_cls()(cmd=("true",), label="launch x")

        with mock.patch.object(cli, "ensure_launch_target_allowed", lambda *a, **k: None), \
             mock.patch.object(cli.os, "getcwd", return_value="/srv/work/myapp/sub"), \
             mock.patch.object(cli, "git_repo_root_nonint_as_user", return_value="/srv/work/myapp"), \
             mock.patch.object(cli, "git_common_dir_root_as_user", return_value="/srv/work/myapp"), \
             mock.patch.object(cli, "resolve_agent_id", return_value="claude"), \
             mock.patch.object(cli, "plan_worktree_launch", fake_plan):
            # dry_run=True → no execvp; do_run returns 0 after printing.
            rc = cli.do_run(args, cfg, "devagent")
        self.assertEqual(rc, 0)
        self.assertEqual(captured["repo"], "/srv/work/myapp")
        self.assertEqual(captured["branch"], "feature/auth")
        self.assertTrue(captured["dry_run"])  # dry_run threaded through (no side effects)
```

(`do_run`'s `if branch:` arm calls `plan_worktree_launch(..., dry_run=args.dry_run)`. On `dry_run` it returns 0 without `execvp`; on a real run it execs `req.cmd` after `req.prelaunch` — exactly how `launch_in_tmux` hands off today, but driven from the already-built request. The test mocks only `getcwd` on the real `cli.os` module rather than replacing `cli.os` wholesale, so `os.path`/`os.execvp` stay real.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon.py::CliWorktreeRoutingTests -v`
Expected: FAIL — `do_run` does not call `plan_worktree_launch` yet.

- [ ] **Step 3: Write minimal implementation**

In `do_run`, replace the `if branch:` block so it routes through the create planner. Resolve the **primary** repo root (normalise worktree-from-worktree, §8):

```python
def do_run(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    cwd = canonical(os.getcwd())
    ensure_launch_target_allowed(cfg, launch_user, cwd)
    branch = args.worktree_branch
    if branch:
        repo_root = git_repo_root_nonint_as_user(cwd, launch_user)
        if not repo_root:
            fail(f"run -w must be run inside a git repository readable by {launch_user}")
        # Normalise to the PRIMARY working tree so a worktree-from-worktree
        # anchors to the main repo, not a nested one (§8).
        primary = git_common_dir_root_as_user(cwd, launch_user)
        if primary:
            repo_root = primary
        ensure_launch_target_allowed(cfg, launch_user, repo_root)
        _agent = resolve_agent_id(cfg, launch_user, args.agent, report=args.host_report)
        args.agent = _agent
        # plan_worktree_launch gates the worktree path, runs git worktree
        # add, copies includes, emits worktree.create + session.new, and
        # returns the launch request. In dry-run it prints the git plan and
        # does no side effects (Task 11).
        req = plan_worktree_launch(
            cfg, launch_user, repo_root, branch, _agent, args.permission_mode,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            print(f"launch_user={shlex.quote(launch_user)}")
            print(f"exec {shlex.join(req.cmd)}")
            return 0
        for pre in req.prelaunch:
            run_cmd(list(pre))
        os.execvp(req.cmd[0], list(req.cmd))
        return 0
    # ── non-worktree path unchanged below (existing do_run tail) ──
    target_dir = cwd
    session_stem = session_stem_for_path(target_dir)
    compatibility_root = target_dir
    # ... existing non-worktree body verbatim (agent resolve, allocate,
    #     session.new audit, launch_in_tmux) — do NOT edit it.
```

(Only the `if branch:` arm changes; the existing non-worktree tail of `do_run` is left verbatim. `args.permission_mode` already exists on `ParsedArgs` (cli.py:297). The test mocks `cli.os.getcwd` and `plan_worktree_launch`, so `os.execvp` is never reached on the dry-run path.)

**`do_new -w` (preserve the attach guard, C1).** Edit only the `if branch:` arm (cli.py:3486-3502 resolves `repo_root`; the shared tail at 3520-3578 does attach/allocate/launch). Keep the attach-vs-new guard; route the *new-session* case through `plan_worktree_launch`:

```python
    branch = args.worktree_branch
    if branch:
        if not os.path.isdir(project_dir):
            fail(f"new -w requires an existing project directory: {project_dir} "
                 f"(create it first with 'uxon -n {name}')")
        repo_root = git_repo_root_as_user(project_dir, launch_user)
        if not repo_root:
            fail(f"new -w requires a git repository (checked as launch user {launch_user}) "
                 f"in {project_dir}")
        # Normalise worktree-from-worktree to the primary repo (§8).
        primary = git_common_dir_root_as_user(project_dir, launch_user)
        if primary:
            repo_root = primary
        ensure_launch_target_allowed(cfg, launch_user, repo_root)
        _agent = resolve_agent_id(cfg, launch_user, args.agent, report=args.host_report)
        args.agent = _agent
        session_stem = session_stem_for_worktree(repo_root, branch)
        compatibility_root = compute_worktree_path(
            repo_root=repo_root, branch=branch, worktree_root=cfg.worktree_root
        )
        target_desc = f"{repo_root} (worktree {branch})"
        sessions = collect_sessions([launch_user], cfg)
        existing = compatible_indexed_sessions(
            session_stem, _agent, compatibility_root, sessions,
            prefix=cfg.session_prefix, legacy_prefixes=cfg.legacy_session_prefixes,
        )
        if existing:
            attach_target = choose_attach_session(
                existing, session_stem, _agent,
                prefix=cfg.session_prefix, legacy_prefixes=cfg.legacy_session_prefixes,
            )
            decision = resolve_repeat_decision(
                args.repeat_mode, cfg, target_desc, attach_target, existing
            )
            if decision == "attach":
                from uxon import audit as _audit
                _audit.audit("session.attach", session=attach_target.name,
                             target_user=launch_user)
                return attach_session(attach_target, cfg, launch_user, args.dry_run)
        # No existing session, or decision == "new": create + launch.
        req = plan_worktree_launch(
            cfg, launch_user, repo_root, branch, _agent, args.permission_mode,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            print(f"launch_user={shlex.quote(launch_user)}")
            print(f"exec {shlex.join(req.cmd)}")
            return 0
        for pre in req.prelaunch:
            run_cmd(list(pre))
        os.execvp(req.cmd[0], list(req.cmd))
        return 0
    # ── non-worktree do_new body unchanged below ──
```

Note: `compatibility_root` for the worktree probe is now the **worktree path** (matching `plan_worktree_launch`'s `allocate_session_name` root and §2.5), not `repo_root` as the old code used. The `--git-remote` + `-w` rejection still lives in `_do_create_git_remote`; leave it. `choose_attach_session`, `resolve_repeat_decision`, `attach_session` all already exist.

- [ ] **Step 4: Rewrite the two existing `do_new -w` tests for the new `compatibility_root` (C1)**

The two tests at `tests/test_uxon.py:775` (`test_do_new_existing_worktree_session_defaults_to_attach_in_tty`) and `:802` (`test_do_new_existing_worktree_session_uses_configured_noninteractive_new`) assert the attach-vs-new guard, which is **preserved** — but they place the existing session's `active_path` at the repo root (`/srv/repos/demo`). Under §2.5 the worktree compatibility root is now the **worktree path** (`/srv/repos/demo/.uxon/worktrees/feature-x`), and the session's tmux cwd for a uxon-managed worktree is that worktree path. So both tests must (a) set the existing session's `active_path` to the worktree path, and (b) mock `git_common_dir_root_as_user` (new call in the `-w` arm). Rationale to put in the test docstring/comment: "uxon-managed worktree sessions live at the worktree path, not the repo root — the §2.5 compatibility root changed accordingly; the attach decision itself is unchanged."

Rewrite both (showing the attach-default one; apply the same two edits to the noninteractive-`new` one):

```python
    def test_do_new_existing_worktree_session_defaults_to_attach_in_tty(self) -> None:
        # uxon-managed worktree sessions live at the worktree path (§2.5),
        # so the compatible session's active_path is the worktree dir, not
        # the repo root. The attach-vs-new decision is unchanged.
        cfg = self.make_config()
        args = uxon.ParsedArgs(
            action="new", target_id="demo", worktree_branch="feature-x", agent_args=[]
        )
        wt = "/srv/repos/demo/.uxon/worktrees/feature-x"
        existing = [self.make_session("uxon-demo-feature-x@claude", wt)]

        with mock.patch.object(uxon.os.path, "isdir", return_value=True), \
             mock.patch.object(uxon, "probe_cwd_writable", return_value=True), \
             mock.patch.object(uxon, "git_repo_root_as_user", return_value="/srv/repos/demo"), \
             mock.patch.object(uxon, "git_common_dir_root_as_user", return_value="/srv/repos/demo"), \
             mock.patch.object(uxon, "collect_sessions", return_value=existing), \
             mock.patch.object(uxon, "is_interactive_tty", return_value=True), \
             mock.patch("builtins.input", return_value=""), \
             mock.patch.object(uxon, "attach_session", return_value=0) as attach, \
             mock.patch.object(uxon, "plan_worktree_launch") as plan:
            result = uxon.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        attach.assert_called_once()
        plan.assert_not_called()  # attach decision → no worktree creation
```

For the noninteractive-`new` test: keep `is_interactive_tty=False` + `cfg.repeat_noninteractive_mode = "new"`, set the same worktree `active_path`, mock `git_common_dir_root_as_user`, and assert `plan_worktree_launch` **is** called once (replacing the old `allocate_session_name` + `launch_in_tmux` assertions, since creation now lives inside the planner). Mock `plan_worktree_launch` to return a `_tui_launch_request_cls()(cmd=("true",), ...)` and mock `uxon.os.execvp` (or pass `dry_run`) so the test doesn't exec.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_uxon.py::CliWorktreeRoutingTests -v && pytest tests/test_uxon.py -k "do_new_existing_worktree" -v`
Expected: PASS. Then sweep for any remaining stale native-`-w` assertion: `pytest tests/test_uxon.py -k "do_run or do_new or worktree" -v` and update accordingly.

- [ ] **Step 6: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon.py
git commit -m "feat(worktrees): CLI -w routes through plan_worktree_launch (agent-agnostic)"
```

---

## Task 13: `TuiContext` + `TuiConfig` callbacks for worktrees (§4.2a)

**Files:**
- Modify: `src/uxon/tui/context.py` (`TuiContext` dataclass), `src/uxon/tui/config.py` (`TuiConfig` + `from_context`)
- Test: `tests/test_uxon_tui_config.py`

Add **four** callbacks to the context and snapshot them into the frozen `TuiConfig`:
- `on_probe_worktrees(cwd) -> list[Workspace]` — the worker-driven workspace probe (§4.2).
- `on_create_worktree(repo_root, branch, agent_id, mode_id) -> LaunchRequest` — create + launch a new worktree (§4.1).
- `on_launch_existing_worktree(repo_root, branch, worktree_path, agent_id, mode_id) -> LaunchRequest` — launch into an **existing** worktree with the worktree-aware stem (§2.5; no re-create).
- `on_probe_existing_worktree_sessions(worktree_path, repo_root, branch, agent_id) -> tuple[tuple[str, bool], ...]` — the worktree-aware attach-vs-new probe (§2.5, §3).

Defining all four here removes the forward references in Tasks 16–18. (Task 18 adds the focused unit test for `on_probe_existing_worktree_sessions`; this task only declares the field + default + snapshot.)

Note (review-confirmed): the two *extra* callbacks beyond §4.2a's named pair (`on_launch_existing_worktree`, `on_probe_existing_worktree_sessions`) are intentional, not gold-plating — launching into an existing worktree genuinely needs a different session stem than `on_launch_cwd` (`session_stem_for_worktree`, not the basename), and the §3 attach guard for a worktree target needs the worktree-aware probe stem. Both are direct §2.5 consequences.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon_tui_config.py
def test_from_context_snapshots_worktree_callbacks(self) -> None:
    from uxon.tui.config import TuiConfig
    from uxon.tui.context import TuiContext

    sentinel_probe = lambda cwd: []
    sentinel_create = lambda repo, branch, agent, mode: None
    ctx = TuiContext(
        sessions=[], total_cpu="0", total_ram="0", version="3.5.0",
        cwd="/srv/work", cwd_short="work", new_project_root="/srv/work",
        existing_projects=[],
        on_probe_worktrees=sentinel_probe,
        on_create_worktree=sentinel_create,
    )
    cfg = TuiConfig.from_context(ctx)
    self.assertIs(cfg.on_probe_worktrees, sentinel_probe)
    self.assertIs(cfg.on_create_worktree, sentinel_create)
```

(Match the construction style other tests in this file use for `TuiContext` — copy a working ctx-construction from an existing test if the required-field set differs.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon_tui_config.py -k worktree -v`
Expected: FAIL — `TuiContext.__init__() got an unexpected keyword argument 'on_probe_worktrees'`

- [ ] **Step 3: Write minimal implementation**

In `src/uxon/tui/context.py`, inside `TuiContext`, near `on_probe_existing_sessions`:

```python
    # Worktree probe (3.5.0): returns the workspaces (folders only — no
    # session data) for ``cwd``'s repo, parsed from ``git worktree list``.
    # Empty list for a non-git target → no WORKSPACE column. Runs ONCE in
    # a worker when the launch screen opens, under the non-interactive
    # sudo prefix so a missing NOPASSWD grant fails fast (§4.2).
    on_probe_worktrees: Callable[[str], list] = lambda cwd: []
    # Worktree create (3.5.0) → plan_worktree_launch. Builds + launches a
    # uxon-managed worktree for ``branch`` under the repo at ``repo_root``.
    on_create_worktree: Callable[[str, str, str, str], LaunchRequest] = (
        lambda repo_root, branch, agent_id, mode_id: LaunchRequest(
            cmd=("true",), label="noop-create-worktree"
        )
    )
    # Launch into an EXISTING worktree (or the primary tree treated as a
    # worktree target) with the repo-qualified stem (§2.5) — never
    # re-creates the worktree.
    on_launch_existing_worktree: Callable[[str, str, str, str, str], LaunchRequest] = (
        lambda repo_root, branch, worktree_path, agent_id, mode_id: LaunchRequest(
            cmd=("true",), label="noop-launch-existing-worktree"
        )
    )
    # Worktree-aware attach-vs-new probe (§2.5, §3): derives the
    # repo-qualified stem and uses the worktree path as compatibility root.
    on_probe_existing_worktree_sessions: Callable[
        [str, str, str, str], tuple[tuple[str, bool], ...]
    ] = lambda worktree_path, repo_root, branch, agent_id: ()
```

In `src/uxon/tui/config.py`, add to `TuiConfig` (after `on_probe_existing_sessions`):

```python
    on_probe_worktrees: Callable[[str], list]
    on_create_worktree: Callable[[str, str, str, str], LaunchRequest]
    on_launch_existing_worktree: Callable[[str, str, str, str, str], LaunchRequest]
    on_probe_existing_worktree_sessions: Callable[
        [str, str, str, str], tuple[tuple[str, bool], ...]
    ]
```

and in `from_context(...)` add:

```python
            on_probe_worktrees=ctx.on_probe_worktrees,
            on_create_worktree=ctx.on_create_worktree,
            on_launch_existing_worktree=ctx.on_launch_existing_worktree,
            on_probe_existing_worktree_sessions=ctx.on_probe_existing_worktree_sessions,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon_tui_config.py -k worktree -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/uxon/tui/context.py src/uxon/tui/config.py tests/test_uxon_tui_config.py
git commit -m "feat(worktrees): TuiContext/TuiConfig worktree callbacks (§4.2a)"
```

---

## Task 14: Wire `on_probe_worktrees` + `on_create_worktree` in `_build_tui_context` (§4.2a, §4.6)

**Files:**
- Modify: `src/uxon/cli.py` — `_build_tui_context` (callbacks block ~4994, wrap block ~5047, constructor ~5263)
- Test: `tests/test_uxon.py`

The probe resolves `cwd → repo_root` non-interactively (Task 5), normalises to primary (Task 5), runs `git worktree list --porcelain` under `nonint_command_prefix_for_user`, and parses with Task 2. The create closure calls `plan_worktree_launch` and emits no extra audit (the planner already emits `worktree.create`). Both are wrapped with `_wrap_tui_callback` per the established pattern (§4.2a).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon.py
class BuildTuiContextWorktreeWiringTests(unittest.TestCase):
    def test_probe_worktrees_returns_workspaces(self) -> None:
        import uxon.cli as cli

        porcelain = (
            "worktree /srv/work/myapp\nHEAD 1111111111111111111111111111111111111111\n"
            "branch refs/heads/main\n\n"
            "worktree /srv/work/myapp/.uxon/worktrees/feature-auth\n"
            "HEAD 2222222222222222222222222222222222222222\n"
            "branch refs/heads/feature/auth\n"
        )

        def fake_run(cmd, **kw):
            class CP:
                returncode = 0
                stdout = porcelain
                stderr = ""
            return CP()

        cfg = cli.load_config("/tmp")
        with mock.patch.object(cli, "git_repo_root_nonint_as_user", return_value="/srv/work/myapp"), \
             mock.patch.object(cli, "git_common_dir_root_as_user", return_value="/srv/work/myapp"), \
             mock.patch.object(cli.subprocess, "run", fake_run), \
             mock.patch.object(cli, "process_user", return_value="devagent"):
            ctx = cli._build_tui_context(cfg, "devagent", "/srv/work/myapp", skeleton=True)
            rows = ctx.on_probe_worktrees("/srv/work/myapp")
        self.assertTrue(rows[0].is_primary)
        self.assertEqual(rows[1].branch, "feature/auth")

    def test_probe_worktrees_non_git_returns_empty(self) -> None:
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        with mock.patch.object(cli, "git_repo_root_nonint_as_user", return_value=None):
            ctx = cli._build_tui_context(cfg, "devagent", "/tmp/plain", skeleton=True)
            self.assertEqual(ctx.on_probe_worktrees("/tmp/plain"), [])
```

(Building a skeleton ctx avoids the blocking probes. If `_build_tui_context(skeleton=True)` does not wire callbacks, build a non-skeleton ctx with the heavy probes mocked, mirroring how existing `_build_tui_context` tests in `tests/test_uxon.py` set it up — grep `_build_tui_context(` in tests.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon.py::BuildTuiContextWorktreeWiringTests -v`
Expected: FAIL — `on_probe_worktrees` is the default lambda returning `[]`, so `test_probe_worktrees_returns_workspaces` fails on `rows[0]`.

- [ ] **Step 3: Write minimal implementation**

In `_build_tui_context`, alongside the other closures (near `on_probe_existing_sessions`):

```python
    def on_probe_worktrees(cwd_arg: str) -> list:
        """Workspaces for ``cwd_arg``'s repo (folders only). Non-git → []."""
        from uxon.worktrees import parse_worktree_porcelain

        repo_root = git_repo_root_nonint_as_user(cwd_arg, launch_user)
        if not repo_root:
            return []
        primary = git_common_dir_root_as_user(cwd_arg, launch_user)
        if primary:
            repo_root = primary
        cp = subprocess.run(
            nonint_command_prefix_for_user(launch_user)
            + ["git", "-C", repo_root, "worktree", "list", "--porcelain"],
            text=True,
            capture_output=True,
        )
        if cp.returncode != 0:
            return []
        return parse_worktree_porcelain(cp.stdout or "", repo_root=repo_root)

    def on_create_worktree(repo_root: str, branch: str, agent_id: str, mode_id: str):
        # plan_worktree_launch emits its own worktree.create audit event.
        return plan_worktree_launch(cfg, launch_user, repo_root, branch, agent_id, mode_id)

    def on_launch_existing_worktree(repo_root, branch, worktree_path, agent_id, mode_id):
        # Launch into an existing worktree with the worktree-aware stem.
        req = _plan_tui_run_agent(
            cfg, launch_user, worktree_path, agent_id, mode_id,
            worktree=(repo_root, branch),
        )
        from uxon import audit as _audit

        _audit.audit(
            "session.new",
            agent=agent_id,
            project=worktree_path,
            branch=branch,
            session=_session_name_from_launch_label(req.label),
            dry_run=False,
        )
        return req

    def on_probe_existing_worktree_sessions(worktree_path, repo_root, branch, agent_id):
        matches = probe_tui_compatible_sessions(
            cfg, launch_user, worktree_path, agent_id,
            stem=session_stem_for_worktree(repo_root, branch),
            compatibility_root=worktree_path,
        )
        return tuple((s.name, s.attached == "1") for s in matches)
```

(Confirm the existing label→session helper name — the file uses `_session_name_from_launch_label` in `on_launch_cwd`/`on_launch_new`; reuse that exact name.)

In the wrap block (near `on_probe_existing_sessions = _wrap_tui_callback(...)`):

```python
    on_probe_worktrees = _wrap_tui_callback(on_probe_worktrees, _CbErr)
    on_create_worktree = _wrap_tui_callback(on_create_worktree, _CbErr)
    on_launch_existing_worktree = _wrap_tui_callback(on_launch_existing_worktree, _CbErr)
    on_probe_existing_worktree_sessions = _wrap_tui_callback(
        on_probe_existing_worktree_sessions, _CbErr
    )
```

In the `TuiContext(...)` constructor (near `on_probe_existing_sessions=...`):

```python
        on_probe_worktrees=on_probe_worktrees,
        on_create_worktree=on_create_worktree,
        on_launch_existing_worktree=on_launch_existing_worktree,
        on_probe_existing_worktree_sessions=on_probe_existing_worktree_sessions,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon.py::BuildTuiContextWorktreeWiringTests -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon.py
git commit -m "feat(worktrees): wire on_probe_worktrees + on_create_worktree in cli"
```

---

## Task 15: Pure focus-cycle helper for the three-value `_active_panel` (§3)

**Files:**
- Modify: `src/uxon/tui/state.py`
- Test: `tests/test_uxon_tui.py`

`_active_panel` becomes `agent | mode | workspace`; ←/→ cycles only the **visible** columns (AGENT hidden under a single agent; WORKSPACE absent for a non-git target). Keep the branchy logic in a pure helper per the TUI test policy (code-map.md) so Pilot only covers wiring.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon_tui.py
class LaunchPanelCycleTests(unittest.TestCase):
    def test_cycle_right_skips_hidden_agent(self) -> None:
        from uxon.tui.state import next_launch_panel

        # AGENT hidden (single agent), WORKSPACE present.
        order = ("mode", "workspace")
        self.assertEqual(next_launch_panel("mode", +1, order), "workspace")
        self.assertEqual(next_launch_panel("workspace", +1, order), "mode")

    def test_cycle_left_wraps(self) -> None:
        from uxon.tui.state import next_launch_panel

        order = ("agent", "mode", "workspace")
        self.assertEqual(next_launch_panel("agent", -1, order), "workspace")
        self.assertEqual(next_launch_panel("mode", -1, order), "agent")

    def test_no_workspace_column(self) -> None:
        from uxon.tui.state import next_launch_panel

        order = ("agent", "mode")
        self.assertEqual(next_launch_panel("agent", +1, order), "mode")
        self.assertEqual(next_launch_panel("mode", +1, order), "agent")

    def test_unknown_current_returns_first(self) -> None:
        from uxon.tui.state import next_launch_panel

        self.assertEqual(next_launch_panel("agent", +1, ("mode", "workspace")), "mode")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon_tui.py::LaunchPanelCycleTests -v`
Expected: FAIL — `cannot import name 'next_launch_panel'`

- [ ] **Step 3: Write minimal implementation**

In `src/uxon/tui/state.py` (near the other launch-options helpers):

```python
def next_launch_panel(current: str, direction: int, order: tuple[str, ...]) -> str:
    """Cycle the active launch-options panel across only the VISIBLE columns.

    ``order`` is the visible-column sequence (a subset of
    ``("agent", "mode", "workspace")`` — AGENT is dropped under a single
    agent, WORKSPACE is absent for a non-git target). ``direction`` is
    +1 (right) or -1 (left); the cycle wraps. An unknown ``current``
    (e.g. the previously-active column is now hidden) snaps to the first
    visible column.
    """
    if not order:
        return current
    if current not in order:
        return order[0]
    idx = order.index(current)
    return order[(idx + direction) % len(order)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon_tui.py::LaunchPanelCycleTests -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/uxon/tui/state.py tests/test_uxon_tui.py
git commit -m "feat(worktrees): pure focus-cycle helper for launch panels (§3)"
```

---

## Task 16: WORKSPACE column in LaunchOptionsScreen (§3)

**Files:**
- Modify: `src/uxon/tui/screens/launch_options.py`
- Test: `tests/test_uxon_tui_screens.py` (Pilot smoke; pure logic already covered by Task 15)

The screen grows a third `#workspace-panel` column. Its rows are built from a `workspaces: list[Workspace]` passed in at construction (the app fills it from the worker — Task 17).

**B2 — dismiss-value contract (variable arity, no caller breakage).** The screen dismisses a **2-tuple `(agent_id, mode_id)` when constructed WITHOUT `workspaces`** (the project-create / project-open flows that never have a workspace column), and a **3-tuple `(agent_id, mode_id, workspace_choice)` ONLY when constructed WITH a non-empty `workspaces`**. This means the two existing unpack sites in `src/uxon/tui/screens/main.py` that always build the screen workspace-free stay untouched:
- `_launch_new._on_opts` at line 768 (`agent_id, mode_id = result`) — unchanged.
- `_launch_existing.after_opts` at line 827 (`agent_id, mode_id = result`) — unchanged.
Only `_launch_cwd`'s `after_opts` at line 753 is rewritten by Task 17 to construct the screen WITH workspaces and unpack the 3-tuple. `workspace_choice` is one of: `("primary", repo_root)`, `("worktree", path, branch)`, `("new", None)`.

- [ ] **Step 1: Write the failing test (Pilot smoke — batch into the existing screen-scenario harness)**

```python
# add to tests/test_uxon_tui_screens.py
class LaunchOptionsWorkspaceColumnTests(unittest.TestCase):
    @unittest.skipUnless(_textual_available(), "textual not installed")
    def test_workspace_column_lists_primary_and_new(self) -> None:
        import asyncio

        from uxon.tui.screens.launch_options import LaunchOptionsScreen
        from uxon.tui.context import TuiContext
        from uxon.worktrees import Workspace

        async def scenario() -> None:
            from uxon.tui.app import UxonApp

            workspaces = [
                Workspace(label="main", branch="main", path="/srv/work/myapp", is_primary=True),
                Workspace(label="feature/auth", branch="feature/auth",
                          path="/srv/work/myapp/.uxon/worktrees/feature-auth", is_primary=False),
            ]
            ctx = _mk_ctx(enabled_agents=("claude",), default_agent="claude")
            app = UxonApp(ctx, probe_agents=False)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen = LaunchOptionsScreen(ctx, workspaces=workspaces)
                await app.push_screen(screen)
                await pilot.pause()
                labels = [str(i.children[0].renderable)
                          for i in screen.query("#workspace-list ListItem")]
                assert any("main" in s for s in labels)
                assert any("feature/auth" in s for s in labels)
                assert any("New worktree" in s for s in labels)

        asyncio.run(scenario())
```

(Adapt to the file's actual harness: the existing tests use `run_screen_scenarios` / `ScreenScenario` from `harness.textual_scenarios` and batch `run_test()` calls per the TUI runtime policy. Prefer joining an existing batched scenario over a standalone `asyncio.run` — the snippet above is illustrative of the assertion, not the batching. Read `tests/harness/textual_scenarios.py` and mirror it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon_tui_screens.py -k Workspace -v`
Expected: FAIL — `LaunchOptionsScreen.__init__() got an unexpected keyword argument 'workspaces'`

- [ ] **Step 3: Write minimal implementation**

Edit `src/uxon/tui/screens/launch_options.py`:

- `__init__(self, ctx, workspaces: list | None = None, repo_root: str = "")`: store `self._workspaces = list(workspaces or [])` and `self._repo_root = repo_root` (the primary repo root resolved by the probe — Task 17 threads it in so `action_commit`'s workspace dispatch has it without re-resolving on the event loop); compute the visible-column order: `("agent",) if not single_agent else ()` + `("mode",)` + `("workspace",) if self._workspaces else ()`. Store as `self._panel_order`.
- `compose`: after the mode panel, conditionally add:
  ```python
              if self._workspaces:
                  with Vertical(id="workspace-panel"):
                      yield Static("Workspace", classes="panel-title")
                      yield ListView(id="workspace-list")
  ```
- `on_mount`: populate `#workspace-list` — one `ListItem` per workspace (`f"{w.label}  (primary)"` when `w.is_primary`, else `w.label`), plus a final `ListItem(Static("+ New worktree…"), id="workspace-new")`. Default highlight index 0 (the primary). Update the binding actions to use the pure `next_launch_panel(self._active_panel, +1/-1, tuple(self._panel_order))` from Task 15 instead of the hardcoded left/right swap.
- `_reflect_focus`: extend to focus `#workspace-list` when `self._active_panel == "workspace"`.
- `action_commit`: compute the dismiss tuple. Read `mode_id` exactly as the current commit does (via `launch_commit_decision` → `decision.mode_id`). Then:
  - **No workspace column** (`not self._workspaces`): dismiss the existing **2-tuple** `(self._current_agent, mode_id)` — preserves the contract for the two untouched callers (B2).
  - **Workspace column present**: build `workspace_choice` and dismiss the **3-tuple** `(self._current_agent, mode_id, workspace_choice)`. The choice is the highlighted `#workspace-list` row regardless of which panel committed (so Enter from the mode column launches into the default-highlighted primary): the `+ New worktree…` row → `("new", None)`; the primary row → `("primary", self._repo_root)`; an existing worktree row → `("worktree", workspace.path, workspace.branch)`.
  - Keep the existing `launch_commit_decision` early-returns (`ignore` / `switch-to-mode` / `dismiss`) — only the final committing branch changes shape.

Keep BINDINGS the only key handler (no `on_key`). The `left`/`right` Binding descriptions can stay "Agent"/"Mode"; do not add per-letter bindings.

Update the dismiss type annotation to cover both arities: `ModalScreen["tuple[str, str] | tuple[str, str, object] | None"]`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon_tui_screens.py -k Workspace -v`
Expected: PASS. Then run the bindings drift guard: `pytest tests/test_uxon_tui_bindings.py -v` (must stay green — no `on_key`).

- [ ] **Step 5: Commit**

```bash
git add src/uxon/tui/screens/launch_options.py tests/test_uxon_tui_screens.py
git commit -m "feat(worktrees): WORKSPACE column on launch-options screen (§3)"
```

---

## Task 17: Probe worktrees in a worker on launch-screen open (§4.2 non-blocking)

**Files:**
- Create: `src/uxon/tui/screens/worktree_branch.py` (`WorktreeBranchScreen` + `worktree_branch_valid` — branch input that allows `/`, C3)
- Modify: `src/uxon/tui/app.py` (worker, mirroring `_probe_link_health_worker`), `src/uxon/tui/screens/main.py` (pass `workspaces` into `LaunchOptionsScreen`; 3-tuple dispatch; new-worktree input)
- Test: `tests/test_uxon_tui_screens.py` (Pilot), `tests/test_uxon_tui.py` (`worktree_branch_valid` unit)

The workspace list must be probed **once** when the launch flow opens, in a worker (not synchronously in `on_mount`), so the event loop never blocks on git/`sudo`. We run the probe before pushing `LaunchOptionsScreen` and hand the result in. The probe reads `on_probe_worktrees` off the frozen `TuiConfig` from the worker (no mutable-ctx access), matching the `_probe_link_health_worker` rule.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon_tui_screens.py
class LaunchProbeWorktreesWiringTests(unittest.TestCase):
    @unittest.skipUnless(_textual_available(), "textual not installed")
    def test_launch_cwd_passes_probed_workspaces(self) -> None:
        import asyncio
        from uxon.worktrees import Workspace

        async def scenario() -> None:
            from uxon.tui.app import UxonApp
            from uxon.tui.screens.launch_options import LaunchOptionsScreen

            probed = [Workspace(label="main", branch="main",
                                path="/srv/work/myapp", is_primary=True)]
            ctx = _mk_ctx(
                enabled_agents=("claude",), default_agent="claude",
                cwd="/srv/work/myapp", cwd_writable=True,
                on_probe_worktrees=lambda cwd: probed,
            )
            app = UxonApp(ctx, probe_agents=False)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                # Activate "New session in current folder" (first action row).
                await pilot.press("enter")
                await pilot.pause()
                await pilot.pause()  # allow the probe worker to land
                top = app.screen_stack[-1]
                assert isinstance(top, LaunchOptionsScreen)
                assert top._workspaces and top._workspaces[0].is_primary

        asyncio.run(scenario())
```

(Adapt to the batched harness as in Task 16. If the worker is asynchronous and the screen is pushed only after the result arrives, assert on the eventual top screen after enough `pilot.pause()` cycles.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uxon_tui_screens.py -k ProbeWorktrees -v`
Expected: FAIL — `_workspaces` empty (probe not wired to the launch flow).

- [ ] **Step 3: Write minimal implementation**

In `src/uxon/tui/screens/main.py`, change `_launch_cwd` (and the analogous `_launch_new`/`_launch_existing` after-name closures) so that before pushing `LaunchOptionsScreen` it requests a worktree probe and pushes the screen with the result. Use an app worker so the git call is off the event loop:

- Add an app method in `src/uxon/tui/app.py` mirroring `_probe_link_health_worker`:

```python
    def probe_workspaces_then(self, cwd: str, on_done) -> None:
        """Run on_probe_worktrees in a worker; call on_done(list) on the loop.

        Off-event-loop git probe (under the non-interactive sudo prefix on
        the CLI side). The result is posted back and ``on_done`` runs on the
        message loop so it can push the launch screen safely.
        """
        def _worker() -> list:
            try:
                probe = self.cfg.on_probe_worktrees
                return list(probe(cwd)) if callable(probe) else []
            except Exception:
                return []

        def _finish(worker) -> None:
            self.call_from_thread  # noqa: B018 — see below
        self.run_worker(
            lambda: self.call_later(on_done, _worker()),
            thread=True, exclusive=False, group="worktree_probe",
        )
```

Simplify to match the codebase's existing worker idiom (the `_probe_*_worker` methods post a message, then an `on__*` handler calls `call_later`). Prefer that message+handler shape over the inline `call_later` above if the file uses messages everywhere; the contract is: probe runs in a thread, `on_done(workspaces)` runs on the loop and pushes `LaunchOptionsScreen(self.ctx, workspaces=workspaces)`.

- In `main.py` `_launch_cwd`, replace the final `self.app.push_screen(LaunchOptionsScreen(self.ctx), after_opts)` with:

```python
        def _push_with_workspaces(workspaces) -> None:
            self.app.push_screen(LaunchOptionsScreen(self.ctx, workspaces=workspaces), after_opts)

        self.app.probe_workspaces_then(self.ctx.cwd, _push_with_workspaces)  # type: ignore[attr-defined]
```

- Extend `after_opts` to handle the 3-tuple dismiss value `(agent_id, mode_id, workspace_choice)` from Task 16. All four worktree callbacks were defined in Tasks 13/14 (`on_create_worktree`, `on_launch_existing_worktree`, `on_probe_existing_worktree_sessions`), so dispatch is a straight call:
  - `workspace_choice is None` (no WORKSPACE column — non-git target) → unchanged behaviour: `_maybe_show_session_choice(target_dir=self.ctx.cwd, ..., on_new=lambda: commit_new(...))` using the existing `on_launch_cwd` + `on_probe_existing_sessions`.
  - `("primary", repo_root)` → launch into the primary tree: unchanged path-stem launch via `on_launch_cwd` guarded by `on_probe_existing_sessions` (the primary keeps the plain path-based probe per §3).
  - `("worktree", path, branch)` → guard with the **worktree-aware** probe, then launch the existing worktree:
    ```python
    repo_root = self.ctx.cwd  # primary repo root resolved by the probe; pass the workspace's repo
    existing = self.ctx.on_probe_existing_worktree_sessions(path, repo_root, branch, agent_id)
    # if existing → SessionChoiceScreen; on "new" or empty → commit:
    req = self.ctx.on_launch_existing_worktree(repo_root, branch, path, agent_id, mode_id)
    self.app.request_launch(req)
    ```
    Reuse the existing `_maybe_show_session_choice` plumbing but with the worktree probe; the simplest path is to generalise `_maybe_show_session_choice` to take an optional `probe` callable defaulting to `self.ctx.on_probe_existing_sessions`, and pass `on_probe_existing_worktree_sessions` (closed over `repo_root`/`branch`) for the worktree case. The attach branch still routes through `_attach_session` unchanged.
  - `("new", None)` → push the dedicated `WorktreeBranchScreen` (below), then on submit call the new-worktree guard + `self.ctx.on_create_worktree(repo_root, branch, agent_id, mode_id)` and `self.app.request_launch(req)`. **`NewProjectScreen` CANNOT be reused** — its validator `project_name_valid` (state.py:464-465) rejects any name containing `/`, and branch names like `feature/auth` are the common case (C3). Add a dedicated screen:

```python
# src/uxon/tui/screens/worktree_branch.py
"""WorktreeBranchScreen — one Input for a new worktree's branch name.

Dismiss value: the entered branch name (stripped) or ``None`` on cancel.
Unlike NewProjectScreen, slashes are allowed — git branch names routinely
contain ``/`` (``feature/auth``). BINDINGS-only key handling (no on_key),
per the AGENTS.md drift guard.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from ..keymap import bindings_with_aliases


def worktree_branch_valid(value: str) -> bool:
    """Accept a non-empty branch name. Permits ``/`` (unlike project names).

    Rejects only what git itself forbids cheaply up front: empty, leading
    ``-``, whitespace, and the obvious bad tokens; git's own ``worktree
    add`` is the authority for the rest (and surfaces a clear error via
    plan_worktree_launch's §8 handling).
    """
    name = value.strip()
    if not name or name.startswith("-"):
        return False
    if name in (".", ".."):
        return False
    return not any(c.isspace() for c in name)


class WorktreeBranchScreen(ModalScreen["str | None"]):
    DEFAULT_CSS = """
    WorktreeBranchScreen { align: center middle; }
    WorktreeBranchScreen > Vertical {
        width: 64; height: auto; padding: 1 2;
        border: round $accent; background: $surface;
    }
    WorktreeBranchScreen .title { text-style: bold; margin-bottom: 1; }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("enter", "submit", "Create", show=True, priority=True),
    )

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("New worktree — branch name", classes="title")
            yield Input(placeholder="feature/auth", id="branch-input")

    def on_mount(self) -> None:
        self.query_one("#branch-input", Input).focus()

    def action_submit(self) -> None:
        value = self.query_one("#branch-input", Input).value.strip()
        if not worktree_branch_valid(value):
            self.app.notify("Enter a valid branch name.", severity="warning", timeout=4)
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)
```

Add a pure unit test for `worktree_branch_valid` in `tests/test_uxon_tui.py` (accepts `feature/auth`, `bugfix-1`; rejects ``, `-x`, `a b`, `..`).

  `repo_root` for an existing-worktree / new-worktree launch is the primary repo root. The launch screen knows it: the workspace probe (Task 14) resolved `cwd → primary repo_root` and the `Workspace` rows carry their own `path`; thread the resolved `repo_root` into `LaunchOptionsScreen` (add a `repo_root: str = ""` constructor arg in Task 16) so `after_opts` has it without re-resolving on the event loop.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_uxon_tui_screens.py -k "ProbeWorktrees or Workspace" -v`
Expected: PASS. Run the full TUI suite: `pytest tests/ -k tui -n auto`.

- [ ] **Step 5: Commit**

```bash
git add src/uxon/tui/app.py src/uxon/tui/screens/main.py src/uxon/tui/context.py src/uxon/tui/config.py src/uxon/cli.py tests/test_uxon_tui_screens.py
git commit -m "feat(worktrees): probe workspaces in a worker, wire launch dispatch (§3, §4.2)"
```

---

## Task 18: SessionChoice guard uses the worktree-aware probe for worktree targets (§2.5, §3)

**Files:**
- Test: `tests/test_uxon.py` (probe callback unit), `tests/test_uxon_tui_screens.py` (Pilot guard appears)

The `on_probe_existing_worktree_sessions` callback was defined + wired in Tasks 13/14, and Task 17 routes worktree-target launches through it. This task is the focused regression coverage locking the §3 detail: "the probe uses the worktree-aware stem for worktree targets and the plain path-based probe for the primary tree."

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon.py
class ProbeExistingWorktreeSessionsCallbackTests(unittest.TestCase):
    def test_callback_uses_worktree_stem(self) -> None:
        import uxon.cli as cli

        repo = "/srv/work/myapp"
        wt = "/srv/work/myapp/.uxon/worktrees/feature-auth"
        sess = cli.SessionInfo(
            user="devagent", name="uxon-myapp-feature-auth@claude", attached="1",
            windows="1", created="", last_attached="", pane_pids=(), active_pid=None,
            active_cmd="claude", active_path=wt,
        )
        cfg = cli.load_config("/tmp")
        with mock.patch.object(cli, "collect_sessions", return_value=[sess]), \
             mock.patch.object(cli, "git_repo_root_nonint_as_user", return_value=repo), \
             mock.patch.object(cli, "git_common_dir_root_as_user", return_value=repo), \
             mock.patch.object(cli, "process_user", return_value="devagent"):
            ctx = cli._build_tui_context(cfg, "devagent", repo, skeleton=True)
            out = ctx.on_probe_existing_worktree_sessions(wt, repo, "feature/auth", "claude")
        self.assertEqual(out, (("uxon-myapp-feature-auth@claude", True),))
```

- [ ] **Step 2: Run test to verify it passes (callback already wired in Tasks 13/14)**

Run: `pytest tests/test_uxon.py::ProbeExistingWorktreeSessionsCallbackTests -v`
Expected: PASS. If it FAILS with `'TuiContext' object has no attribute 'on_probe_existing_worktree_sessions'`, Tasks 13/14 are incomplete — fix there.

- [ ] **Step 3: (no implementation — the callback was defined + wired in Tasks 13/14; this task is the regression guard)**

Confirm `main.py`'s worktree-target launch dispatch (Task 17) uses `on_probe_existing_worktree_sessions` for `("worktree", …)` choices and the plain `on_probe_existing_sessions` for the primary tree.

- [ ] **Step 4: Re-run to confirm green**

Run: `pytest tests/test_uxon.py::ProbeExistingWorktreeSessionsCallbackTests -v`
Expected: PASS

Also add/keep one Pilot assertion in `tests/test_uxon_tui_screens.py` that, given a worktree row + a live compatible session, selecting the worktree and committing pushes `SessionChoiceScreen` (batch into the workspace scenario from Task 16).

- [ ] **Step 5: Commit**

```bash
git add src/uxon/cli.py src/uxon/tui/context.py src/uxon/tui/config.py src/uxon/tui/screens/main.py tests/test_uxon.py tests/test_uxon_tui_screens.py
git commit -m "feat(worktrees): worktree-aware SessionChoice guard probe (§2.5, §3)"
```

---

## Task 19: Audit — `worktree.create` field coverage test (§4.6, §9 audit)

**Files:**
- Test: `tests/test_uxon_audit.py`

`plan_worktree_launch` emits **both** `worktree.create` and `session.new` on the create path (Task 11; the both-events assertion against the planner lives in Task 11's `PlanWorktreeLaunchTests`, B3). This task adds the §9 audit-harness assertion that the documented `worktree.create` fields serialize correctly through the real audit wire path, using the `_send_raw` recorder seam.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_uxon_audit.py
class WorktreeCreateAuditTests(_BaseAuditTests):
    def test_worktree_create_fields_serialized(self) -> None:
        import json
        from unittest.mock import patch

        import uxon.audit as au

        recorded: list[bytes] = []
        with (
            patch.object(au, "_detect_sink", return_value="syslog"),
            patch.object(au, "_open_sink_socket", return_value=object()),
            patch.object(au, "_send_raw", side_effect=recorded.append),
            patch.dict("os.environ", {"USER": "tester"}, clear=False),
        ):
            au.configure(enabled=True, syslog_facility="user", subcmd="run")
            au.audit(
                "worktree.create",
                agent="claude",
                project="/srv/work/myapp",
                branch="feature/auth",
                path="/srv/work/myapp/.uxon/worktrees/feature-auth",
                base="local",
                session="uxon-myapp-feature-auth@claude",
            )
        self.assertEqual(len(recorded), 1)
        body = recorded[0].decode("utf-8").split("@cee: ", 1)[1]
        fields = json.loads(body)
        self.assertEqual(fields["event"], "worktree.create")
        self.assertEqual(fields["agent"], "claude")
        self.assertEqual(fields["project"], "/srv/work/myapp")
        self.assertEqual(fields["branch"], "feature/auth")
        self.assertEqual(fields["base"], "local")
        self.assertEqual(fields["session"], "uxon-myapp-feature-auth@claude")
        self.assertEqual(fields["path"], "/srv/work/myapp/.uxon/worktrees/feature-auth")
```

(Confirm `_BaseAuditTests` exists and how it resets module state between tests — mirror the existing `AuditDisabledTests` / serializer-test setup in the file.)

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `pytest tests/test_uxon_audit.py::WorktreeCreateAuditTests -v`
Expected: PASS already (audit is generic; this locks the field set). If it FAILS on the CEE-split, adjust the body extraction to match `_serialize_syslog`'s exact header (`@cee: ` with a trailing space).

- [ ] **Step 3: (no implementation — guard only)**

- [ ] **Step 4: Re-run**

Run: `pytest tests/test_uxon_audit.py::WorktreeCreateAuditTests -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_uxon_audit.py
git commit -m "test(worktrees): worktree.create audit field coverage (§4.6)"
```

---

## Task 20: Documentation (§6) — CHANGELOG, configuration reference, how-to + explanation, AGENTS.md

**Files:**
- Modify: `CHANGELOG.md`, `docs/reference/configuration.md`, `AGENTS.md`
- Create: `docs/guides/customise/worktrees.md`, `docs/explain/worktrees.md`

**Read `docs/agents/maintaining-docs.md` before editing user-facing docs** (Diátaxis kinds, single-source-of-truth, link conventions). The how-to holds the commands; the explanation holds the rationale + the two deviations from `claude -w`; the reference table holds the config keys; CHANGELOG holds the user-visible behaviour change. No duplication across them.

- [ ] **Step 1: CHANGELOG.md — under `## [Unreleased]`**

Under `### Added`:
```markdown
- Native, uxon-managed git worktrees for every agent. The launch-options screen has a new WORKSPACE column (primary tree + existing worktrees + "+ New worktree…"); pick a workspace or create one and uxon launches the agent there. New config keys `worktree_root` and `worktree_base`.
```
Under `### Changed (breaking)`:
```markdown
- `uxon -w/--worktree <branch>` no longer delegates to `claude`'s native `-w` and is no longer claude-only. uxon now creates and owns the worktree itself under `<repo>/.uxon/worktrees/<branch-slug>/` (excluded automatically via `.git/info/exclude`) and launches any agent there. Set `worktree_base = "remote"` for the previous claude-like behaviour of basing new branches on a freshly fetched `origin/HEAD` (the default `local` does no network fetch).
```

- [ ] **Step 2: docs/reference/configuration.md — add two rows to the top-level keys table**

After the `repeat_noninteractive_mode` row:
```markdown
| `worktree_root` | string | `""` | Base directory for uxon-managed worktrees. Empty = default `<repo>/.uxon/worktrees/<branch-slug>/` (excluded from git via `.git/info/exclude`). When set: `<worktree_root>/<repo-slug>/<branch-slug>/` — the admin must ensure it is writable by the launch user and inside `allowed_roots`. |
| `worktree_base` | `"local"` / `"remote"` | `"local"` | Base ref for a *new* worktree branch. `local` (default): branch off the local `origin/HEAD` if present, else local `HEAD` — no `git fetch`, no network. `remote`: `git fetch origin` first, then branch off the fetched `origin/HEAD` (claude-like; needs network + credentials). |
```

- [ ] **Step 3: Create docs/guides/customise/worktrees.md (how-to)**

```markdown
# Work in a git worktree

Run an agent in an isolated worktree of a repo — a separate directory on
its own branch, sharing the repo's `.git`. uxon creates and owns the
worktree (no agent-native `-w`).

## From the TUI

1. Start uxon in (or open) a git repository.
2. Trigger a launch ("New session in current folder").
3. In the launch dialog, move to the **WORKSPACE** column with `→`.
4. Pick the primary tree, an existing worktree, or **+ New worktree…**.
   For a new worktree, type a branch name and press Enter.
5. uxon creates the worktree under `.uxon/worktrees/<branch>/` (or under
   `worktree_root` if configured) and launches the agent there.

## From the CLI

```bash
uxon -w feature/auth          # create/attach worktree for feature/auth, launch in cwd's repo
uxon new myproj -w feature/x  # same, for a repo under <new_project_root>/myproj
```

## Copying gitignored files into a new worktree

Add a `.worktreeinclude` file (`.gitignore` syntax) at the repo root.
On worktree creation uxon copies untracked, gitignored files that match
its patterns (e.g. `.env`) into the new worktree.

## Removing a worktree

Not yet a uxon gesture — remove manually:

```bash
git worktree remove .uxon/worktrees/<branch>
```
```

- [ ] **Step 4: Create docs/explain/worktrees.md (explanation)**

```markdown
# Why uxon manages worktrees itself

uxon creates and owns every worktree (`git worktree add` + launch with
`-c <worktree_path>`), rather than delegating to an agent's native
worktree flag. This is uniform across `claude` and `codex`, and it keeps
**session ↔ worktree identity consistent**: every session's tmux working
directory *is* its worktree path, so the attach-vs-new guard can match a
session to a workspace reliably (sessions are repo-qualified —
`<repo>-<branch>` — so two repos with a same-named branch never collide).

Worktrees live under `<repo>/.uxon/worktrees/<branch-slug>/` and are
excluded from git automatically via `.git/info/exclude` — no manual
`.gitignore` edit. (Set `worktree_root` to relocate them.)

## Two deliberate deviations from `claude -w`

1. **uxon manages the worktree, not the agent.** uxon does not call
   `claude -w`; it runs `git worktree add` itself for every agent. The
   one behaviour not replicated is claude's exit-time auto-cleanup —
   worktree *removal* is a manual `git worktree remove` for now.
2. **`worktree_base` defaults to `local` (no fetch).** `claude -w`
   fetches `origin` by default so the new branch tracks the latest
   remote. uxon defaults to `local` because in the multi-user/`sudo`
   launch context an implicit per-create `git fetch` against a possibly
   private remote can hang or prompt for credentials. Set
   `worktree_base = "remote"` for claude-like freshness.
```

- [ ] **Step 5: AGENTS.md — extend the forbidden-patterns / ownership note**

Under the "No agent invocations added outside the launch builder" bullet, add a sibling sentence:
```markdown
- **Worktree creation is owned by `plan_worktree_launch`.** Both the CLI
  `-w` flag and the TUI new-worktree path route through it; do not add a
  second `git worktree add` call site. Consistent with the single
  launch-builder rule above.
```

- [ ] **Step 6: Verify docs render + links, then commit**

Run: `python3 -m py_compile $(git ls-files '*.py') && ruff check . && ruff format --check . && pyright && pytest tests/ -n auto && python -c "import uxon.cli"`
Expected: all green; `import uxon.cli` does not pull in textual.

```bash
git add CHANGELOG.md docs/reference/configuration.md docs/guides/customise/worktrees.md docs/explain/worktrees.md AGENTS.md
git commit -m "docs(worktrees): -w behaviour change, config keys, how-to + explanation (§6)"
```

---

## Final verification (run after Task 20)

- [ ] **Full local-checks pass:**

```bash
python3 -m py_compile $(git ls-files '*.py')
ruff check . && ruff format --check .
pyright
pytest tests/ -n auto
python -c "import uxon.cli"   # must NOT pull in textual
```

- [ ] **§9 testing checklist — confirm a test exists for each:**
  - Pure helpers: path computation, slug-collision precondition, slug-parity with `cli.slugify` (C5), porcelain parse (primary/detached/bare) — Tasks 1, 2.
  - Identity (§2.5): planner names with `session_stem_for_worktree` (asserts allocated name), worktree-aware probe finds it, cross-repo same-named worktrees don't collide/hard-fail — Task 8 (+ Tasks 6, 7).
  - Audit: `worktree.create` field serialization — Task 19; **both** `worktree.create` AND `session.new` emitted on the create path (§4.6, B3) — Task 11.
  - `load_config` + settings round-trip for `worktree_root`/`worktree_base` incl. `local`/`remote` validation — Tasks 3, 4.
  - CLI dry-run for `-w` (routes through `plan_worktree_launch(dry_run=True)`, no side effects, `dry_run` threaded) — Task 12; `git worktree add` failure → clear error — Task 11.
  - **Gating rejection (B1, §2.3, §9):** `worktree_root` outside `allowed_roots` → clear error naming `worktree_root`, before any git work — Task 11 (`test_worktree_root_outside_allowed_roots_rejected`).
  - **CLI `-w` attach-vs-new preserved (C1):** the two rewritten `do_new -w` tests (attach-default-in-TTY; configured-noninteractive-`new`) — Task 12 Step 4.
  - One Pilot smoke for the extended launch screen (agent change rebuilds permission [existing coverage]; WORKSPACE lists primary `(primary)` + worktrees + new; Enter on existing workspace + SessionChoice guard; new opens input) — Tasks 16, 17, 18.
  - `worktree_branch_valid` accepts `/` (C3) — Task 17.
  - §8 edges: slug collision (Task 1 precondition + Task 11 add-failure error), worktree-from-worktree via `--git-common-dir` (Tasks 5, 12, 14), branch-already-checked-out (Task 11), detached-HEAD (Task 2).
  - **B2 (no caller breakage):** the two workspace-free `LaunchOptionsScreen` callers (main.py:768, :827) still unpack a 2-tuple — confirm `pytest tests/ -k tui` stays green after Task 16.
