# Session dashboard views — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the design from `docs/superpowers/specs/2026-05-07-session-dashboard-views-design.md` on `feat/dashboard-views` (branched from `dev`) as 14 buildable commits. After this plan ships:

- Hard sort contract: locals → cfg-order remotes → within-block by `last_attached_epoch` desc, name asc. No sort cycle, no sort buttons.
- Two view modes: `by_host` (default) with host tab strip + per-host status bar, `flat` toggle. `v` toggles. Filter forces flat.
- Search-first: `SearchBar` is default focus; `/` refocuses; non-empty filter forces flat.
- Reconciler bug fixed: `[A,B,C,D] → [D,C,B,A]` ends visually as `[D,C,B,A]` (today it doesn't).
- Block colours: locals → palette → operator pin per remote. No magenta. No `bold green` collision. Attached state via `●`/`○` glyph. Zebra dim within a block.
- Layout-invariant bindings (RU twins). Quit on `q`/`й`. Esc never quits.
- `ControlPersist` operator-configurable (default 300s).
- Wire envelope grows optional `host_stats` (additive — **no `WIRE_SCHEMA_VERSION` bump**).

**Architecture:** Pure selector → pure reconciler → presentational widget. The reconciler stays pure (`format(row)` unchanged); the widget owns block hue + zebra at render time via a parallel `row_key → (block_color, row_in_block)` map. `ApplyPlan(ops, new_keys)` lets the widget apply `RowAdd` ops in reverse new-index order so every `before_key` is already in the table.

**Tech Stack:** Python 3.11+, Textual (DataTable, Bindings, Screen), Rich (Text, style), tomlkit (round-trip writes), `msgspec.json` (envelope), pytest + Textual `Pilot` for TUI smoke.

**Branch & version:** `feat/dashboard-views` from `dev`. `__version__ = "3.4.0.dev0"` on this branch; bumped to `3.4.0` only on the release commit on `dev`.

**Project rules to honour:**
- AGENTS.md hard rule: no `git push` to protected branches, no tags, no GitHub mutations without per-turn approval. Pushing the feat branch by name is OK.
- `vz-general-rules`: smallest viable commit, one task = one commit, never trust subagent claims, verify each citation.
- All key handling goes through `BINDINGS` (`tests/test_uxon_tui_bindings.py` enforces this).
- `tomlkit` for write-back; `tomllib` for reads.
- Update `CHANGELOG.md` `[Unreleased]` block as features land — final polish in Task 13.

---

## File Structure

The plan touches these existing files (read each before editing):

- `src/uxon/__init__.py` — `__version__`.
- `src/uxon/wire_schema.py` — envelope `RemoteSessionPayload` and `SessionRecord`; add optional `HostStats` block.
- `src/uxon/probes.py` — add `read_host_stats()`.
- `src/uxon/cli.py` — `do_list` envelope builder embeds `host_stats`; `DEFAULT_CONFIG` adds `ssh_control_persist_seconds`; config loader adds new keys; remove `tui_table_default_sort_by` field plumbing.
- `src/uxon/remote_collector.py` — `_default_template` reads persist seconds from cfg; envelope parser tolerates absent `host_stats`; `RemoteSnapshot` carries `host_stats: HostStats | None`.
- `src/uxon/remote_hosts.py` — `RemoteHost.color: str | None` field; lenient validation.
- `src/uxon/settings.py` — register `ssh_multiplex`, `ssh_control_persist_seconds`, `tui.table.default_view`, `tui.search.fields`, `tui.color_palette`, `local_host.color`; remove `tui.table.default_sort_by`.
- `src/uxon/tui/context.py` — drop `tui_table_default_sort_by`; add the new fields above.
- `src/uxon/tui/main_data.py` — `host_stats: HostStats | None` on `MainData`.
- `src/uxon/tui/tui_state.py` — refresh worker plumbs `host_stats` from local probe.
- `src/uxon/tui/dashboard/ui_state.py` — drop `sort_by`/`sort_dir`/`cycle_sort`/`toggle_sort_dir`/`_SORT_CYCLE`; add `view_mode` + `set_view_mode`; keep `filter_text`/`set_filter`.
- `src/uxon/tui/dashboard/model.py` — rewrite `_build` per the new contract; `_matches_filter` consumes the configured field list.
- `src/uxon/tui/dashboard/columns.py` — drop `host_colour`/`_HOST_PALETTE`; add `assign_block_colors` (pure helper); rewrite `_format_name` to emit `●`/`○` + plain text; rewrite `_format_host` to emit plain text; `path.default_visible=False`. **No signature change to `format(row)`.**
- `src/uxon/tui/dashboard/reconcile.py` — `diff(...)` returns `ApplyPlan(ops, new_keys)`.
- `src/uxon/tui/widgets/session_dashboard_table.py` — `apply(plan)` applies `RowAdd` ops in reverse new-index order; widget keeps a parallel `_block_meta: dict[str, tuple[str, int]]` and wraps NAME/HOST cell `Text` with block hue + zebra dim before dispatching to DataTable.
- `src/uxon/tui/screens/main.py` — wire view toggle, host tabs, status bar, search bar; default focus to search; drop `s`/`S`/`Esc→quit`; use `bindings_with_aliases`; remove `cycle_sort`/`toggle_sort_dir` action handlers.
- `src/uxon/tui/styles.tcss` — tab-strip, status-bar, search-bar styles.
- `docs/configuration.md` — new keys, view contract, sort contract, search contract, keymap.
- `docs/agents/conventions.md` — flag `[ssh]/[tui]` namespace consolidation as backlog.
- `CHANGELOG.md` — `[Unreleased]` block.

The plan creates these new files:

- `src/uxon/tui/dashboard/buckets.py` — `HostBucket`, `select_host_buckets`, `select_host_status_block`.
- `src/uxon/tui/widgets/host_tab_strip.py` — `HostTabStrip` widget.
- `src/uxon/tui/widgets/host_status_bar.py` — `HostStatusBar` widget.
- `src/uxon/tui/widgets/search_bar.py` — `SearchBar` widget.
- `src/uxon/tui/keymap.py` — `LAYOUT_ALIASES` + `bindings_with_aliases`.

Plus tests under `tests/`:
- `tests/test_dashboard_model_order.py` *(new)*
- `tests/test_dashboard_buckets.py` *(new)*
- `tests/test_dashboard_block_colors.py` *(new)*
- `tests/test_uxon_keymap.py` *(new)*
- `tests/test_host_stats.py` *(new)*
- `tests/test_remote_hosts_color.py` *(new)*
- `tests/test_settings_ssh_keys.py` *(new)*
- `tests/test_dashboard_ui_state.py` *(rewrite cycle/toggle tests → view_mode tests)*
- `tests/test_dashboard_reconcile.py` *(extend with reverse-permutation regression)*
- `tests/test_uxon_tui_bindings.py` *(update to drift-guard the new bindings table)*
- `tests/test_uxon_tui_main_screen_pilot.py` *(extend smoke scenario)*

---

## Task 0: Bump version + open changelog block

**Files:**
- Modify: `src/uxon/__init__.py:8`
- Modify: `CHANGELOG.md` (top)

This commit is the only mutation; no logic changes. Lands first so every subsequent commit on the branch carries the dev-pre-release version string.

- [ ] **Step 1: Branch off `dev`**

```bash
git switch dev
git pull --ff-only origin dev
git switch -c feat/dashboard-views
```

- [ ] **Step 2: Bump `__version__`**

Replace line 8 of `src/uxon/__init__.py` with:

```python
__version__ = "3.4.0.dev0"
```

- [ ] **Step 3: Open `[Unreleased]` block in CHANGELOG.md**

Insert under the top heading (above the existing release entries):

```markdown
## [Unreleased] — 3.4.0

### Added
- Session dashboard: `by_host` view (default) with per-host tab strip and status bar; `flat` toggle via `v`.
- Search bar (default focus on TUI mount); `/` to refocus from anywhere.
- Optional `host_stats` block in the wire envelope (additive; no schema-version bump).
- Per-host colour pin: `[[remote_hosts]] color = "..."`; configurable palette `[tui] color_palette`; configurable local hue `[local_host] color`.
- `tui.table.default_view`, `tui.search.fields`, `[local_host]` section, `[tui] color_palette`.
- `ssh_control_persist_seconds` setting (default 300s; must be > 0).
- Layout-invariant bindings via JCUKEN ↔ QWERTY alias map.

### Changed
- Sort is now a hard contract, not a setting: locals → cfg-order remotes → within-block by last-attach desc, name asc.
- Attached state shown via `●` filled / `○` hollow glyph; no `bold green` override.
- Quit is `q`/`й` only. `Esc` is a scoped cancel; never quits.
- `PATH` column hidden by default. Operators opt back in via `tui.table.columns`.
- Reconciler `apply()` runs `RowAdd` ops in reverse new-index order — fixes a long-standing visual reorder bug on tab switches and large diffs.

### Removed
- Sort cycle bindings (`s`, `S`) and `tui.table.default_sort_by` setting.
- `Esc → quit` binding on `MainScreen`.
```

- [ ] **Step 4: Run version test**

```bash
uv run pytest tests/test_uxon_version.py -v
```
Expected: PASS (the existing test reads `__version__` and accepts any non-empty PEP-440 string; if it pins to `3.3.0` exactly, update it to read from `uxon.__version__` in this same commit).

- [ ] **Step 5: Commit**

```bash
git add src/uxon/__init__.py CHANGELOG.md tests/test_uxon_version.py
git commit -m "chore(version): bump to 3.4.0.dev0"
```

---

## Task 1: Reconciler `ApplyPlan` + reverse `RowAdd` apply

**Goal:** `[A,B,C,D] → [D,C,B,A]` ends visually as `[D,C,B,A]`. Today it doesn't because `_apply_add` falls back to append when `before_key` isn't yet in the table.

**Files:**
- Modify: `src/uxon/tui/dashboard/reconcile.py:54-92` (add `ApplyPlan`), `:109-175` (return type)
- Modify: `src/uxon/tui/widgets/session_dashboard_table.py:103-126` (`apply` signature + dispatch order), `:200-205` (`_new_index_of` helper)
- Modify: `src/uxon/tui/screens/main.py:415-416` (`diff` → `apply` call site)
- Test: `tests/test_dashboard_reconcile.py` *(extend)*
- Test: `tests/test_dashboard_session_table.py` *(extend; rename if missing)*

- [ ] **Step 1: Write the failing reverse-permutation regression test**

Add to `tests/test_dashboard_reconcile.py`:

```python
def test_reverse_permutation_lands_in_new_order():
    """`[A,B,C,D] → [D,C,B,A]` must end visually as `[D,C,B,A]`."""
    cols = (ColumnSpec(id="name", label="NAME", format=lambda r: r.name, sort_key=lambda r: r.name),)
    old = tuple(_mkrow(name=n) for n in ("a", "b", "c", "d"))
    new = tuple(_mkrow(name=n) for n in ("d", "c", "b", "a"))
    plan = diff(old, new, cols)
    # Apply against an in-memory ordered list mimicking the widget contract.
    visual: list[str] = [r.name for r in old]
    adds_in_reverse = sorted(
        (op for op in plan.ops if isinstance(op, RowAdd)),
        key=lambda op: -plan.new_keys.index(op.row_key),
    )
    for op in plan.ops:
        if isinstance(op, RowRemove):
            visual.remove(op.row_key.split("/")[-1])
    for op in adds_in_reverse:
        target_idx = plan.new_keys.index(op.row_key)
        visual.insert(min(target_idx, len(visual)), op.row_key.split("/")[-1])
    assert visual == ["d", "c", "b", "a"]
```

`_mkrow` is the existing test helper; if missing, add a thin wrapper that builds a `SessionRow` with a unique key.

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_dashboard_reconcile.py::test_reverse_permutation_lands_in_new_order -v
```
Expected: FAIL — `diff` returns `tuple[Op, ...]`, not an object with `.ops` / `.new_keys`. `AttributeError`.

- [ ] **Step 3: Add `ApplyPlan` and return it**

In `src/uxon/tui/dashboard/reconcile.py`, after the `RowRemove` dataclass (around line 87):

```python
@dataclass(frozen=True, slots=True)
class ApplyPlan:
    """Reconciler output: ordered ops + the new key list.

    The widget needs ``new_keys`` to apply ``RowAdd`` ops in reverse
    new-index order (so every ``before_key`` is already in the
    table). Pairing it with ``ops`` keeps the diff function pure —
    no widget contact, no positional metadata leakage.
    """

    ops: tuple[Op, ...]
    new_keys: tuple[str, ...]
```

Change `diff` signature:

```python
def diff(
    old: tuple[SessionRow, ...],
    new: tuple[SessionRow, ...],
    columns: tuple[ColumnSpec, ...],
) -> ApplyPlan:
    # ... existing body ...
    return ApplyPlan(ops=tuple(ops), new_keys=tuple(new_keys))
```

Update the module docstring's "returns a tuple of ops" wording to reflect `ApplyPlan`.

- [ ] **Step 4: Update widget `apply` to accept `ApplyPlan` and reverse-order adds**

Replace `SessionDashboardTable.apply` body in `src/uxon/tui/widgets/session_dashboard_table.py`:

```python
def apply(self, plan: ApplyPlan) -> None:
    """Dispatch reconciler ops against the underlying DataTable.

    RowAdd ops are applied in **reverse new-index order** so every
    ``before_key`` is either ``None`` or refers to a row already
    inserted earlier in this same reverse walk. The "anchor not
    present → append" branch in :meth:`_apply_add` becomes
    structurally unreachable in production: ``before_key`` is
    sourced from ``plan.new_keys``, and removed keys (in old but
    not in new) cannot appear there. The branch is kept as a
    defensive log only.
    """
    if not plan.ops:
        return
    t0 = time.perf_counter()
    counts = {"add": 0, "remove": 0, "update": 0}
    new_index = {k: i for i, k in enumerate(plan.new_keys)}
    removes_and_updates: list[Op] = []
    adds: list[RowAdd] = []
    for op in plan.ops:
        if isinstance(op, RowAdd):
            adds.append(op)
        else:
            removes_and_updates.append(op)
    # RowRemoves first, then in-place CellUpdates, then RowAdds in
    # reverse new-index order.
    for op in removes_and_updates:
        if isinstance(op, RowRemove):
            counts["remove"] += 1
            self._apply_remove(op)
        else:  # CellUpdate
            counts["update"] += 1
            self._apply_update(op)
    adds.sort(key=lambda op: -new_index[op.row_key])
    for op in adds:
        counts["add"] += 1
        self._apply_add(op)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    debug("tui-table", ms=elapsed_ms, ops=counts, rows=self.row_count)
```

`_apply_add`, `_apply_remove`, `_apply_update`, `_row_index_of` and `pin_cursor_to` keep their current bodies; they are correct already and the reverse-order contract makes the inline-insert path's worst case land on `before_key is None` or a present anchor.

- [ ] **Step 5: Update the call site in `MainScreen._refresh_dashboard`**

Around `src/uxon/tui/screens/main.py:415-416`:

```python
plan = diff(self._dashboard_rows, all_rows, self._active_columns)
widget.apply(plan)
```

(Was `ops = diff(...); widget.apply(ops)`.)

- [ ] **Step 6: Run reverse-permutation test**

```bash
uv run pytest tests/test_dashboard_reconcile.py::test_reverse_permutation_lands_in_new_order -v
```
Expected: PASS.

- [ ] **Step 6b: Update every existing `diff()` call site**

The signature change is **not transparent**: 30+ existing tests destructure the return value as a plain tuple (`assertEqual(diff(...), ())`, `len(ops)`, `for op in ops`, `table.apply(diff((), rows, cols))`). All of them break with `AttributeError`/`TypeError` until updated.

Files to touch (exact line counts verified):
- `tests/test_dashboard_reconcile.py` — lines 67, 72, 79, 91, 103, 113, 125, 137, 179, 187. Replace `diff(...)` with `diff(...).ops`, and `diff(...) == ()` with `diff(...).ops == ()`.
- `tests/test_dashboard_widget.py` — lines 95, 123, 154, 185, 194, 224, 233, 266, 269, 305, 308, 345, 378, 410, 417. Two patterns: `ops = diff(...)` → `plan = diff(...)`, then `table.apply(ops)` → `table.apply(plan)`. The widget's `apply` now takes the plan directly (Step 4).
- `tests/test_dashboard_perf.py` — lines 155, 168, 200, 206, 236, 248. Same patterns. The "no-op apply" assertion `len(ops) == 0` becomes `len(plan.ops) == 0`. The module docstring at line 10 (`diff(model, model, columns) == ()`) needs the same update.
- `tests/test_uxon_tui_commit11.py` — line 156: `ops = diff(first, second, cols)` → use `.ops` whenever destructured.

Mechanical edit; no behaviour change. Walk every match of `diff(` in the four files and pick the right pattern.

- [ ] **Step 7: Run full reconcile + table test suite**

```bash
uv run pytest tests/test_dashboard_reconcile.py tests/test_dashboard_widget.py tests/test_dashboard_perf.py tests/test_uxon_tui_commit11.py tests/test_dashboard_session_table.py -v
```
Expected: PASS (after Step 6b updates the call sites; the new reverse-permutation test from Step 1 also passes).

- [ ] **Step 8: Commit**

```bash
git add src/uxon/tui/dashboard/reconcile.py src/uxon/tui/widgets/session_dashboard_table.py src/uxon/tui/screens/main.py tests/test_dashboard_reconcile.py tests/test_dashboard_widget.py tests/test_dashboard_perf.py tests/test_uxon_tui_commit11.py
git commit -m "fix(reconcile): apply RowAdd in reverse new-index order"
```

---

## Task 2: `host_stats` in envelope (additive optional)

**Goal:** Local probe and CLI envelope can carry `HostStats`. **No `WIRE_SCHEMA_VERSION` bump** — the existing parser strict-equality gate at `remote_collector.py:447` would reject 3.3.0 peers wholesale on a bump. Treat `host_stats` as an additive optional field, same pattern as `data.scope_skipped` already documented in `wire_schema.py:32-41`.

**Files:**
- Modify: `src/uxon/wire_schema.py:53+` (`HostStats` typeddict, optional envelope field)
- Modify: `src/uxon/probes.py` (+ `read_host_stats()`)
- Modify: `src/uxon/cli.py` `do_list` envelope builder
- Modify: `src/uxon/remote_collector.py:440-...` parser (no version bump; tolerate absent field)
- Test: `tests/test_host_stats.py` *(new)*
- Test: `tests/test_wire_schema.py` *(extend if exists; otherwise add a small test in `tests/test_remote_collector.py`)*

- [ ] **Step 1: Write failing test for `read_host_stats`**

Create `tests/test_host_stats.py`:

```python
"""Verify ``read_host_stats`` against fixture ``/proc/*`` files."""
from __future__ import annotations

from pathlib import Path

import pytest

from uxon.probes import read_host_stats


def test_read_host_stats_returns_sane_ranges(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(
        "cpu  100 0 50 1000 0 0 0 0 0 0\n"
        "cpu0 100 0 50 1000 0 0 0 0 0 0\n",
    )
    (proc / "meminfo").write_text(
        "MemTotal:       16384000 kB\n"
        "MemAvailable:    8000000 kB\n"
    )
    (proc / "loadavg").write_text("0.42 0.50 0.55 1/123 4567\n")
    (proc / "uptime").write_text("12345.67 99999.00\n")
    monkeypatch.setattr("uxon.probes._PROC", str(proc))
    monkeypatch.setattr("uxon.probes._CPU_DELAY_S", 0.0)  # Avoid the 50 ms sleep.

    stats = read_host_stats()
    assert 0.0 <= stats.cpu_pct <= 100.0
    assert stats.mem_total_kib == 16_384_000
    assert stats.mem_used_kib == 16_384_000 - 8_000_000
    assert abs(stats.loadavg_1m - 0.42) < 1e-6
    assert stats.uptime_s == 12_345
    assert stats.kernel  # non-empty


def test_read_host_stats_handles_missing_meminfo(tmp_path, monkeypatch):
    """Absence is reported as zero — never raises."""
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("cpu  100 0 50 1000 0 0 0 0 0 0\n")
    (proc / "loadavg").write_text("0.0 0.0 0.0 1/1 1\n")
    (proc / "uptime").write_text("1 1\n")
    monkeypatch.setattr("uxon.probes._PROC", str(proc))
    monkeypatch.setattr("uxon.probes._CPU_DELAY_S", 0.0)
    stats = read_host_stats()
    assert stats.mem_total_kib == 0
    assert stats.mem_used_kib == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_host_stats.py -v
```
Expected: FAIL — `read_host_stats` not defined; `_PROC` / `_CPU_DELAY_S` not in `probes`.

- [ ] **Step 3: Implement `HostStats` and `read_host_stats`**

In `src/uxon/wire_schema.py` (after the existing `SessionRecord` typeddict, around the envelope section):

```python
class HostStats(TypedDict, total=False):
    """Snapshot of host-level metrics returned alongside session list.

    Forward-compatible: peers running schema 1 omit this field; the
    parser treats absence as ``None``. Adding a new key here is also
    additive — peers running an older binary omit unknown keys; the
    consumer should ``.get(...)`` defensively.
    """

    cpu_pct: float        # /proc/stat delta over ~50 ms
    mem_used_kib: int     # MemTotal - MemAvailable
    mem_total_kib: int    # MemTotal
    loadavg_1m: float     # /proc/loadavg field 0
    uptime_s: int         # /proc/uptime field 0
    kernel: str           # uname -r
```

Add `host_stats: HostStats | None` (optional) to whatever envelope dataclass / typeddict is documented in this file (the docstring at lines 26-50 lists the canonical envelope layout — append `host_stats` to the optional-fields documentation block).

In `src/uxon/probes.py`, append:

```python
import os
import platform
import time

_PROC = "/proc"
_CPU_DELAY_S = 0.05

@dataclass(frozen=True, slots=True)
class HostStatsResult:
    """Concrete shape returned by :func:`read_host_stats`.

    Mirrors the wire-schema ``HostStats`` typeddict; converted to a
    plain ``dict`` by the envelope builder before serialisation.
    """

    cpu_pct: float
    mem_used_kib: int
    mem_total_kib: int
    loadavg_1m: float
    uptime_s: int
    kernel: str


def _cpu_busy_pair() -> tuple[int, int]:
    with open(f"{_PROC}/stat") as fh:
        head = fh.readline()
    fields = [int(x) for x in head.split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    total = sum(fields)
    return total - idle, total


def _read_meminfo() -> tuple[int, int]:
    try:
        with open(f"{_PROC}/meminfo") as fh:
            blob = fh.read()
    except FileNotFoundError:
        return 0, 0
    total = 0
    avail = 0
    for line in blob.splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            avail = int(line.split()[1])
    return total, avail


def _read_loadavg_1m() -> float:
    try:
        with open(f"{_PROC}/loadavg") as fh:
            return float(fh.read().split()[0])
    except FileNotFoundError:
        return 0.0


def _read_uptime() -> int:
    try:
        with open(f"{_PROC}/uptime") as fh:
            return int(float(fh.read().split()[0]))
    except FileNotFoundError:
        return 0


def read_host_stats() -> HostStatsResult:
    """Sample /proc for one host_stats snapshot. Stdlib only.

    Two ``/proc/stat`` reads ~50 ms apart yield a CPU delta. Memory
    / loadavg / uptime are single-shot. ``kernel`` is ``platform.release()``.
    """
    busy_a, total_a = _cpu_busy_pair()
    if _CPU_DELAY_S > 0:
        time.sleep(_CPU_DELAY_S)
    busy_b, total_b = _cpu_busy_pair()
    cpu_pct = 0.0 if total_b <= total_a else 100.0 * (busy_b - busy_a) / (total_b - total_a)
    total_kib, avail_kib = _read_meminfo()
    used_kib = max(0, total_kib - avail_kib) if total_kib else 0
    return HostStatsResult(
        cpu_pct=max(0.0, min(100.0, cpu_pct)),
        mem_used_kib=used_kib,
        mem_total_kib=total_kib,
        loadavg_1m=_read_loadavg_1m(),
        uptime_s=_read_uptime(),
        kernel=platform.release(),
    )
```

- [ ] **Step 4: Run host-stats test**

```bash
uv run pytest tests/test_host_stats.py -v
```
Expected: PASS.

- [ ] **Step 5: Embed `host_stats` in `do_list` envelope**

Locate the envelope assembly inside `do_list` in `src/uxon/cli.py` (search for `"schema_version": WIRE_SCHEMA_VERSION` in the JSON-emitter). Add to the top-level envelope dict:

```python
hs = read_host_stats()
envelope["host_stats"] = {
    "cpu_pct": hs.cpu_pct,
    "mem_used_kib": hs.mem_used_kib,
    "mem_total_kib": hs.mem_total_kib,
    "loadavg_1m": hs.loadavg_1m,
    "uptime_s": hs.uptime_s,
    "kernel": hs.kernel,
}
```

Wrap in a `try/except Exception` that logs a single `UXON_DEBUG=probes` line on failure and omits the field — the producer must never abort the list output if `/proc` is partially unavailable.

- [ ] **Step 6: Tolerate absent `host_stats` in the parser**

In `src/uxon/remote_collector.py` after line 458 (`data = env.get("data")`), add envelope-level extraction:

```python
host_stats_raw = env.get("host_stats")
host_stats = host_stats_raw if isinstance(host_stats_raw, dict) else None
```

Pass `host_stats` through to `RemoteSnapshot` as a new optional field (define it on the dataclass — `host_stats: dict[str, Any] | None = None`). The version gate at line 447 stays untouched at `WIRE_SCHEMA_VERSION = "1"`.

- [ ] **Step 7: Run full envelope tests**

```bash
uv run pytest tests/test_remote_collector.py tests/test_host_stats.py -v
```
Expected: PASS. Old envelope fixtures (without `host_stats`) still parse: `host_stats=None` on the snapshot.

- [ ] **Step 8: Commit**

```bash
git add src/uxon/wire_schema.py src/uxon/probes.py src/uxon/cli.py src/uxon/remote_collector.py tests/test_host_stats.py
git commit -m "feat(wire): host_stats in envelope (additive optional)"
```

---

## Task 3: Hard sort contract; drop sort cycle

**Goal:** `select_dashboard_model` returns rows in the contract order — locals → cfg-order remotes → within-block by `(-last_attached_epoch, name)`. `sort_by`/`sort_dir` removed from `DashboardUiState`. `s`/`S` bindings removed from `MainScreen`. `tui.table.default_sort_by` removed from `SETTINGS_SPECS`. Reading it from existing TOML emits one `UXON_DEBUG=tui` line and is otherwise ignored (the loader at `src/uxon/cli.py:537-562` is deleted).

**Files:**
- Modify: `src/uxon/tui/dashboard/ui_state.py` (drop fields/reducers)
- Modify: `src/uxon/tui/dashboard/model.py:48-114` (`_matches_filter` + `_build` rewrite)
- Modify: `src/uxon/tui/screens/main.py:99-134` (drop bindings), `:159-161` (drop reading), `:972-988` (drop action handlers)
- Modify: `src/uxon/settings.py:101-105` (drop spec entry)
- Modify: `src/uxon/tui/context.py:296` (drop field)
- Modify: `src/uxon/cli.py:179-180`, `:537-562`, `:638` (drop field/loader/passthrough)
- Test: `tests/test_dashboard_model_order.py` *(new)*
- Test: `tests/test_dashboard_ui_state.py` (rewrite)
- Test: `tests/test_uxon_tui_bindings.py` (drift-guard update)

- [ ] **Step 1: Write the failing sort-contract test**

Create `tests/test_dashboard_model_order.py`:

```python
"""Hard sort contract: locals → cfg-order remotes → within-block by recency."""
from __future__ import annotations

from types import SimpleNamespace

from uxon.tui.dashboard.model import select_dashboard_model, _LAST_OUTPUT
from uxon.tui.dashboard.ui_state import DashboardUiState
from uxon.tui.dashboard import model as model_module


def _local_session(name, last_attached, user="me", cpu=0.0):
    return SimpleNamespace(name=name, last_attached_epoch=last_attached, user=user, cpu_pct=cpu)


def test_locals_first_then_cfg_order_remotes_then_recency_within_block():
    model_module._LAST_OUTPUT = ()
    state = SimpleNamespace(
        main=SimpleNamespace(
            sessions=[_local_session("alpha", 200), _local_session("bravo", 100)],
            other_sessions=[],
        ),
        remote={
            "kris": SimpleNamespace(value=SimpleNamespace(sessions=[
                _wire("k-old", last_attached=10), _wire("k-new", last_attached=500),
            ])),
            "ada": SimpleNamespace(value=SimpleNamespace(sessions=[
                _wire("a1", last_attached=50),
            ])),
        },
    )
    cfg = SimpleNamespace(remote_hosts=[SimpleNamespace(name="ada"), SimpleNamespace(name="kris")])
    ui = DashboardUiState()
    out = select_dashboard_model(state, cfg, ui)
    assert [r.host for r in out] == [None, None, "ada", "kris", "kris"]
    # Within locals: more-recent first ("alpha" 200 > "bravo" 100).
    assert [r.name for r in out[:2]] == ["alpha", "bravo"]
    # Within kris: 500 > 10.
    assert [r.name for r in out[3:]] == ["k-new", "k-old"]
```

`_wire` is a thin helper that builds an envelope-shaped record (mirror existing helpers in `tests/test_dashboard_model.py`).

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_dashboard_model_order.py -v
```
Expected: FAIL — current `_build` sorts globally by CPU (the default `ui.sort_by`).

- [ ] **Step 3: Rewrite `DashboardUiState`**

Replace the whole module body of `src/uxon/tui/dashboard/ui_state.py` with:

```python
"""Dashboard UI state + pure reducers.

Holds the operator's view choice (``view_mode``) and substring filter
(``filter_text``). Sort is a hard contract owned by the model
selector — not part of UI state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal


@dataclass(frozen=True, slots=True)
class DashboardUiState:
    view_mode: Literal["by_host", "flat"] = "by_host"
    filter_text: str = ""


def set_view_mode(
    ui: DashboardUiState,
    mode: Literal["by_host", "flat"],
) -> DashboardUiState:
    """Set ``view_mode``. Returns ``ui`` by identity on no-op."""
    if mode == ui.view_mode:
        return ui
    return replace(ui, view_mode=mode)


def set_filter(ui: DashboardUiState, text: str) -> DashboardUiState:
    """Set ``filter_text``. Returns ``ui`` by identity on no-op."""
    if text == ui.filter_text:
        return ui
    return replace(ui, filter_text=text)
```

- [ ] **Step 4: Rewrite `_matches_filter` and `_build` in `model.py`**

Replace lines 48-114 of `src/uxon/tui/dashboard/model.py`:

```python
_DEFAULT_SEARCH_FIELDS: tuple[str, ...] = ("name", "user")


def _matches_filter(row: SessionRow, needle: str, fields: tuple[str, ...]) -> bool:
    for f in fields:
        if f == "name" and needle in (row.short or row.name).lower():
            return True
        if f == "user" and needle in row.user.lower():
            return True
        if f == "host" and needle in (row.host or "local").lower():
            return True
        if f == "path" and needle in row.path.lower():
            return True
        if f == "cmd" and needle in row.cmd.lower():
            return True
    return False


def _within_block_key(row: SessionRow) -> tuple[float, str]:
    last = row.last_attached_epoch if row.last_attached_epoch is not None else float("-inf")
    return (-last, (row.short or row.name or "").lower())


def _build(state, cfg, ui):
    rows: list[SessionRow] = []
    if state.main is not None:
        for s in state.main.sessions:
            rows.append(from_tui_session(s))
        for s in state.main.other_sessions:
            rows.append(from_tui_session(s))
    for host in cfg.remote_hosts:
        slot = state.remote.get(host.name)
        if slot is None:
            continue
        snap = slot.value
        if snap is None:
            continue
        for rec in snap.sessions:
            rows.append(from_wire_record(host.name, rec))

    needle = ui.filter_text.strip().lower()
    if needle:
        fields = getattr(cfg, "tui_search_fields", _DEFAULT_SEARCH_FIELDS) or _DEFAULT_SEARCH_FIELDS
        rows = [r for r in rows if _matches_filter(r, needle, fields)]

    # Two stable sorts: within-block recency first, then by host
    # priority. Python's stable sort preserves the within-block
    # ordering during the host_priority pass.
    rows.sort(key=_within_block_key)
    host_priority: dict[str | None, int] = {None: -1}
    for idx, host in enumerate(cfg.remote_hosts):
        host_priority[host.name] = idx
    tail = len(cfg.remote_hosts)
    rows.sort(key=lambda r: host_priority.get(r.host, tail))
    return tuple(rows)
```

- [ ] **Step 5: Drop `sort_by`/`sort_dir` plumbing**

**Order matters: edit `src/uxon/tui/screens/main.py:45` (the import) FIRST. If `cycle_sort`/`toggle_sort_dir` is gone from `ui_state.py` (Step 3) but still imported here, every test that touches `MainScreen` collection-fails with `ImportError` before the body runs.**

In `src/uxon/tui/screens/main.py`:
  - Delete `cycle_sort, toggle_sort_dir` from the import on line 45 — leave only `DashboardUiState` (and add `set_view_mode` later in Task 6).
  - Delete lines 111-112 (`s`/`S` `Binding(...)`).
  - Delete lines 159-161 (`self._dashboard_ui = DashboardUiState(sort_by=...)` → replace with `self._dashboard_ui = DashboardUiState()`).
  - Delete lines 972-988 (`action_cycle_sort`, `action_toggle_sort_dir`).

In `src/uxon/tui/context.py:296` delete `tui_table_default_sort_by` field.

In `src/uxon/cli.py`:
  - Delete the `tui_table_default_sort_by` line in the cfg dataclass (line 180).
  - Delete the loader block at lines 537-562 (entirely; from `tui_table_default_sort_by_raw =` to the second `tui_table_default_sort_by = "cpu"` fallback assignment).
  - Delete the corresponding `tui_table_default_sort_by=...` argument from the cfg constructor call (line 638).

In `src/uxon/settings.py:101-105` delete the `SettingSpec("tui.table.default_sort_by", ...)` entry.

- [ ] **Step 5b: Remove tests that exercise the deleted reducers + bindings**

`tests/test_main_screen_dashboard_own.py` has a whole class of tests pinned to `s`/`S` keybindings — they exist solely to validate the removed contract:
  - `test_cycle_sort_via_s_keybinding` (~line 648)
  - `test_cycle_sort_changes_visible_order` (~line 674)
  - `test_toggle_sort_dir_via_S_keybinding` (~line 726)
  - The class-level docstring at ~line 632 referencing `cycle_sort` / `toggle_sort_dir`

Delete those three test methods. Update the class-level docstring to drop the reducer references; if the class becomes empty after deletion, delete the class. Per memory rule "no trivial pinning tests" — do **not** replace them with no-op stubs.

`tests/test_dashboard_ui_state.py:20-22` imports `cycle_sort, toggle_sort_dir`. The Step 6 rewrite (below) replaces the file's body with the new `view_mode` reducers; ensure the import line is the first thing dropped so the file collects cleanly even if the new tests aren't written yet.

- [ ] **Step 6: Rewrite `tests/test_dashboard_ui_state.py`**

Replace tests for `cycle_sort` / `toggle_sort_dir` with:

```python
def test_set_view_mode_returns_identity_on_noop():
    ui = DashboardUiState()
    assert set_view_mode(ui, "by_host") is ui


def test_set_view_mode_flips():
    ui = DashboardUiState()
    out = set_view_mode(ui, "flat")
    assert out.view_mode == "flat" and out is not ui


def test_set_filter_identity_on_noop():
    ui = DashboardUiState(filter_text="kris")
    assert set_filter(ui, "kris") is ui
```

Drop any imports of `cycle_sort` / `toggle_sort_dir`.

- [ ] **Step 7: Update bindings drift guard**

In `tests/test_uxon_tui_bindings.py`, update the expected `MainScreen.BINDINGS` snapshot:
- assert `s` and `S` bindings are absent;
- assert `q`, `r`, `d`, `D` are present (no `escape`).

(`escape → quit` removal lands in Task 5; for now the test only asserts `s`/`S` removal.)

- [ ] **Step 8: Run model + ui_state + bindings tests**

```bash
uv run pytest tests/test_dashboard_model_order.py tests/test_dashboard_ui_state.py tests/test_uxon_tui_bindings.py -v
```
Expected: PASS.

- [ ] **Step 9: Run full TUI test suite to check no other consumer reads `sort_by`/`sort_dir`**

```bash
uv run pytest tests/ -k "dashboard or tui or settings" -v
```
Expected: PASS. Anything still importing `cycle_sort`/`toggle_sort_dir` fails — fix import sites in this same commit.

- [ ] **Step 10: Commit**

```bash
git add src/uxon/tui/dashboard/ui_state.py src/uxon/tui/dashboard/model.py src/uxon/tui/screens/main.py src/uxon/tui/context.py src/uxon/cli.py src/uxon/settings.py tests/test_dashboard_model_order.py tests/test_dashboard_ui_state.py tests/test_uxon_tui_bindings.py tests/test_main_screen_dashboard_own.py
git commit -m "feat(dashboard): hard sort contract; drop sort cycle"
```

---

## Task 4: Block colours; attach via glyph; PATH off-by-default

**Goal:**
- `dashboard/columns.host_colour` (md5-hashed) deleted; `_HOST_PALETTE` deleted.
- Pure helper `assign_block_colors(remote_hosts, *, local_color, palette)` lives in `columns.py`.
- `_format_name` emits `●`/`○` glyph + plain `Text(name)`. No `bold green`. No block hue inside formatter.
- `_format_host` emits plain `Text(host or "local")`. No md5-hash. No per-host hue.
- `path` column default-visible flips to `False`.
- `RemoteHost.color: str | None` field added; `[[remote_hosts]] color = "..."` accepted as a free Rich-style string.
- Widget keeps a `_block_meta: dict[str, tuple[str, int]]` map and wraps the NAME and HOST cells with block hue + zebra dim at dispatch time.

**Files:**
- Modify: `src/uxon/tui/dashboard/columns.py:47-76` (drop palette + host_colour), `:147-162` (`_format_name`), `:132-136` (`_format_host`), `:274` (`path` default), append `assign_block_colors` helper.
- Modify: `src/uxon/remote_hosts.py:31-80` (add `color`), `:160-180` (allow `color` key).
- Modify: `src/uxon/tui/widgets/session_dashboard_table.py` — `_block_meta` map; wrap NAME/HOST cell.
- Modify: `src/uxon/tui/screens/main.py:_refresh_dashboard` — compute block meta, pass to widget.
- Test: `tests/test_dashboard_block_colors.py` *(new)*
- Test: `tests/test_remote_hosts_color.py` *(new)*
- Test: `tests/test_dashboard_columns.py` (extend; rename if missing)

- [ ] **Step 1: Write failing tests for `assign_block_colors`**

Create `tests/test_dashboard_block_colors.py`:

```python
from __future__ import annotations

from uxon.tui.dashboard.columns import assign_block_colors
from uxon.remote_hosts import RemoteHost


def _h(name, color=None):
    return RemoteHost(name=name, ssh_alias=f"alias-{name}", description="",
                     remote_uxon="uxon", color=color)


def test_locals_get_local_color():
    out = assign_block_colors((), local_color="green", palette=("cyan", "blue"))
    assert out == {None: "green"}


def test_auto_cycle_with_adjacency_skip():
    out = assign_block_colors(
        (_h("a"), _h("b"), _h("c")),
        local_color="green", palette=("cyan", "blue"),
    )
    # Local=green; remote-a auto-picks cyan (≠ green prev); remote-b
    # auto-picks blue (≠ cyan prev); remote-c auto-picks cyan (≠ blue prev).
    assert out == {None: "green", "a": "cyan", "b": "blue", "c": "cyan"}


def test_pin_overrides_auto_cycle():
    out = assign_block_colors(
        (_h("a", color="red"), _h("b")),
        local_color="green", palette=("cyan", "blue"),
    )
    # Pin always wins; no validation against prev.
    assert out[None] == "green"
    assert out["a"] == "red"
    assert out["b"] in ("cyan", "blue")  # auto-cycle resumes from a fresh idx


def test_pin_equal_prev_allowed():
    out = assign_block_colors(
        (_h("a", color="green"),),
        local_color="green", palette=("cyan",),
    )
    # Operator pinned green; visual collision is their choice.
    assert out["a"] == "green"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_dashboard_block_colors.py -v
```
Expected: FAIL — `assign_block_colors` doesn't exist; `RemoteHost.color` not defined.

- [ ] **Step 3: Add `RemoteHost.color`**

In `src/uxon/remote_hosts.py:71-80`, append after `command_template`:

```python
    color: str | None = None
```

In the docstring, document:

> `color`: Optional Rich style spec used to paint the per-host
> block (tab text, NAME column glyph, HOST cell). When ``None``,
> the TUI auto-assigns from `tui.color_palette` with adjacency
> skip. **Not validated against the palette** — operators may pin
> any Rich-accepted name; collisions with locals or peers are
> the operator's responsibility.

In `_validate_host` add `color` extraction:

```python
color_raw = raw.get("color")
if color_raw is None:
    color = None
elif isinstance(color_raw, str) and color_raw.strip():
    color = color_raw.strip()
else:
    raise RemoteHostError(f"remote_hosts[{name}]: color must be a non-empty string when set")
```

Add `"color"` to the `known_keys` set on line 163.

Pass `color=color` in the `RemoteHost(...)` constructor at line 181.

- [ ] **Step 4: Implement `assign_block_colors` in `columns.py`**

Delete `_HOST_PALETTE` and `host_colour` (lines 47-76 of `columns.py`). Add (preserving the module-docstring framing):

```python
def assign_block_colors(
    remote_hosts: tuple[RemoteHost, ...],
    *,
    local_color: str,
    palette: tuple[str, ...],
) -> dict[str | None, str]:
    """Map ``host_name`` (None == locals) → Rich style spec.

    Operator pins (``RemoteHost.color``) win unconditionally — no
    palette validation, no adjacency check against pinned colours.
    Auto-cycle (remotes with ``color is None``) walks ``palette``
    with an adjacency-skip against the previous block's colour
    (whatever its source). Empty ``palette`` falls through to a
    single dim style.
    """
    out: dict[str | None, str] = {None: local_color}
    prev = local_color
    cycle_idx = 0
    fallback_palette = palette or ("dim",)
    for host in remote_hosts:
        if host.color is not None:
            color = host.color
        else:
            color = fallback_palette[cycle_idx % len(fallback_palette)]
            cycle_idx += 1
            if color == prev and len(fallback_palette) > 1:
                color = fallback_palette[cycle_idx % len(fallback_palette)]
                cycle_idx += 1
        out[host.name] = color
        prev = color
    return out
```

- [ ] **Step 5: Rewrite `_format_name`, `_format_host`, flip `path` default**

In `src/uxon/tui/dashboard/columns.py:147-162` replace `_format_name` body:

```python
def _format_name(row: SessionRow) -> Text:
    """Emit ``●``/``○`` attach glyph + plain name.

    Block hue and zebra dim are layered by the widget at render
    time; this formatter stays pure data so the reconciler can
    diff cells without knowing positional metadata.
    """
    glyph = "● " if row.attached else "○ "
    text = Text(glyph)
    text.append(row.short or row.name or "-")
    return text
```

Replace `_format_host` body (lines 132-136):

```python
def _format_host(row: SessionRow) -> Text:
    return Text(row.host or "local")
```

Flip `path` default visibility — find the `ColumnSpec(id="path", ...)` entry around line 274 and add `default_visible=False`:

```python
ColumnSpec(id="path", label="PATH", format=_format_path, sort_key=_sort_path, default_visible=False),
```

- [ ] **Step 6: Add block-meta wrapping to the widget**

In `src/uxon/tui/widgets/session_dashboard_table.py`, add an instance attribute `_block_meta: dict[str, tuple[str, int]] = {}` initialised in `__init__`, plus a `set_block_meta(meta)` method invoked by `MainScreen` before `apply(plan)`:

```python
def __init__(
    self,
    columns: tuple[ColumnSpec, ...],
    *,
    id: str | None = None,
) -> None:
    super().__init__(id=id)
    self._columns = columns
    self._block_meta: dict[str, tuple[str, int]] = {}

def set_block_meta(self, meta: dict[str, tuple[str, int]]) -> None:
    """Update the ``row_key → (block_color, row_in_block)`` map.

    Called by the screen on every tick before :meth:`apply`. The
    map's values seed the block-hue + zebra wrapping done by
    :meth:`_wrap_cell` when dispatching ``RowAdd`` / ``CellUpdate``.
    """
    self._block_meta = meta
```

Add a private helper that wraps a cell `Text`/`str` value with block hue + zebra dim:

```python
def _wrap_cell(self, row_key: str, col_id: str, value: Any) -> Any:
    """Apply block hue + zebra dim to NAME / HOST cells.

    Other columns are passed through unchanged. CPU danger styling
    is per-cell already (set by ``format_cpu``); we never overwrite
    it.
    """
    if col_id not in ("name", "host"):
        return value
    meta = self._block_meta.get(row_key)
    if meta is None:
        return value
    block_color, row_in_block = meta
    style = block_color
    if row_in_block % 2 == 1:
        style = f"{style} dim"
    if isinstance(value, Text):
        wrapped = Text(value.plain, style=style)
        return wrapped
    return Text(str(value), style=style)
```

In `_apply_add` and `_apply_update`, route the cell payloads through `_wrap_cell` keyed on `op.row_key` and the matching column id:

```python
def _apply_add(self, op: RowAdd) -> None:
    cells = tuple(
        self._wrap_cell(op.row_key, col.id, cell)
        for col, cell in zip(self._columns, op.cells, strict=True)
    )
    # ... existing append/inline-insert logic, replacing op.cells with cells ...
```

```python
def _apply_update(self, op: CellUpdate) -> None:
    try:
        wrapped = self._wrap_cell(op.row_key, op.col_id, op.value)
        self.update_cell(op.row_key, op.col_id, wrapped)
    except Exception:
        debug("tui-table", op="update_miss", key=op.row_key, col=op.col_id)
```

- [ ] **Step 7: Compute block meta in `MainScreen._refresh_dashboard`**

Add a helper next to `_refresh_dashboard` in `src/uxon/tui/screens/main.py`:

```python
def _build_block_meta(
    self,
    rows: tuple[SessionRow, ...],
) -> dict[str, tuple[str, int]]:
    """Map each row's reconciler key to (block_color, row_in_block).

    ``block_color`` comes from :func:`assign_block_colors` on the
    cfg's remote hosts; ``row_in_block`` is the row's index inside
    its host block (0, 1, 2, ...) for zebra parity.
    """
    from ..dashboard.columns import assign_block_colors  # local — avoids circular at import time
    palette = tuple(self.ctx.tui_color_palette)
    local_color = self.ctx.local_host_color
    remote_hosts = tuple(self.ctx.remote_hosts)
    colors = assign_block_colors(remote_hosts, local_color=local_color, palette=palette)
    out: dict[str, tuple[str, int]] = {}
    counters: dict[str | None, int] = {}
    for row in rows:
        host_key = row.host  # None for locals
        idx = counters.get(host_key, 0)
        counters[host_key] = idx + 1
        key = f"{row.host or 'local'}/{row.user}/{row.name}"
        out[key] = (colors.get(host_key, local_color), idx)
    return out
```

In `_refresh_dashboard`, before `widget.apply(plan)`:

```python
widget.set_block_meta(self._build_block_meta(all_rows))
```

`ctx.tui_color_palette` and `ctx.local_host_color` get added in Task 6 (settings registration). Until they exist, defer this method's body to a no-op that returns `{}` — but **that pulls Task 4 into Task 6**. To avoid that cross-task dependency, this commit ships `_build_block_meta` reading `self.ctx.tui_color_palette` with a `getattr(self.ctx, "tui_color_palette", ("cyan", "blue"))` fallback and `getattr(self.ctx, "local_host_color", "green")`. Task 6 deletes the fallbacks once the fields exist on the cfg dataclass.

- [ ] **Step 8: Add `RemoteHost.color` test**

Create `tests/test_remote_hosts_color.py`:

```python
from __future__ import annotations
import pytest
from uxon.remote_hosts import load_remote_hosts, RemoteHostError


def test_color_defaults_to_none():
    hosts = load_remote_hosts([{"name": "a", "ssh_alias": "x"}])
    assert hosts[0].color is None


def test_color_accepted_when_string():
    hosts = load_remote_hosts([{"name": "a", "ssh_alias": "x", "color": "blue"}])
    assert hosts[0].color == "blue"


def test_color_rejects_empty_string():
    with pytest.raises(RemoteHostError, match="color"):
        load_remote_hosts([{"name": "a", "ssh_alias": "x", "color": ""}])
```

- [ ] **Step 8b: Update `tests/test_dashboard_columns.py`**

The current file imports the deleted `host_colour` (line 24) and asserts against it at lines 115, 116, 122, 213, 217, 279. Without this update the module fails at collection time with `ImportError`.

  - Drop `host_colour` from the import block on line 24.
  - Replace the `test_host_colour_*` cases (around lines 110-130) with equivalent tests against `assign_block_colors` — the new contract is "deterministic over `(remote_hosts, local_color, palette)` triple" rather than "stable hash of name". A short test asserting `assign_block_colors((), local_color="green", palette=("cyan",)) == {None: "green"}` plus the cycle/pin/adjacency tests in `test_dashboard_block_colors.py` (Step 1) cover the new surface — feel free to keep just one smoke case in `test_dashboard_columns.py` and delete the rest.
  - Lines 213, 217, 279 (`test_remote_row_uses_host_colour` and the second `host_colour("peer-1")` assertion): the new `_format_host` emits plain `Text(host or "local")` — block hue is layered by the widget, not the formatter. Replace these assertions with `self.assertEqual(text.plain, "peer-1")` and drop the style assertion. The widget-side wrapping is exercised by `tests/test_dashboard_widget.py` after Task 4 lands (those tests apply real ops via `widget.apply` and observe the rendered cells).

- [ ] **Step 9: Run full block-colors + columns + remote_hosts tests**

```bash
uv run pytest tests/test_dashboard_block_colors.py tests/test_remote_hosts_color.py tests/test_dashboard_columns.py -v
```
Expected: PASS.

- [ ] **Step 10: Run TUI smoke to make sure no formatter call site assumed wide signature**

```bash
uv run pytest tests/ -k "tui or dashboard" -v
```
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add src/uxon/tui/dashboard/columns.py src/uxon/remote_hosts.py src/uxon/tui/widgets/session_dashboard_table.py src/uxon/tui/screens/main.py tests/test_dashboard_block_colors.py tests/test_remote_hosts_color.py tests/test_dashboard_columns.py
git commit -m "feat(dashboard): block colors; attach via glyph"
```

---

## Task 5: Keymap aliases + remove `Esc → quit`

**Goal:** Every key binding can be entered on RU layout via a hidden twin. Quit is `q`/`й` only. Esc is scoped — does not reach `MainScreen.action_quit` and the binding is removed.

**Files:**
- Create: `src/uxon/tui/keymap.py`
- Modify: `src/uxon/tui/screens/main.py:99-134` (drop `Binding("escape", ...)`; rebuild via `bindings_with_aliases`)
- Modify: every other Screen subclass under `src/uxon/tui/screens/` that declares a `BINDINGS` ClassVar — wrap with `bindings_with_aliases` for layout-invariance. (One pass; per-screen no behaviour change.)
- Test: `tests/test_uxon_keymap.py` *(new)*
- Test: `tests/test_uxon_tui_bindings.py` (drop `escape → quit` expectation; add `q` + `й` twin assertion)

- [ ] **Step 1: Write the failing keymap tests**

Create `tests/test_uxon_keymap.py`:

```python
from __future__ import annotations
from textual.binding import Binding
from uxon.tui.keymap import LAYOUT_ALIASES, bindings_with_aliases


def test_known_key_gets_a_twin():
    out = bindings_with_aliases(Binding("q", "quit", "Quit", show=True))
    assert ("q", "й") <= {b.key for b in out}
    quit_actions = {b.action for b in out}
    assert quit_actions == {"quit"}
    twin = next(b for b in out if b.key == "й")
    assert twin.show is False  # twins never duplicate the footer entry


def test_unknown_key_passes_through_no_twin():
    out = bindings_with_aliases(Binding("f10", "help", "", show=False))
    keys = {b.key for b in out}
    assert keys == {"f10"}


def test_uppercase_letter_twin_exists():
    out = bindings_with_aliases(Binding("D", "kill_all", "", show=True))
    keys = {b.key for b in out}
    assert "D" in keys and "В" in keys
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_uxon_keymap.py -v
```
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `keymap.py`**

Create `src/uxon/tui/keymap.py`:

```python
"""JCUKEN ↔ QWERTY layout aliases for `BINDINGS`.

Every binding declared via :func:`bindings_with_aliases` ships with
a hidden RU twin when its physical key has an entry in
:data:`LAYOUT_ALIASES`. Unknown keys pass through untouched — no
warnings, no errors. The map grows when a new alias is needed.

Forward-compat note: when bindings move to TOML in a future pass,
the same helper still applies — only the source of binding tuples
changes.
"""

from __future__ import annotations
from textual.binding import Binding


LAYOUT_ALIASES: dict[str, str] = {
    # JCUKEN ↔ QWERTY shared positions.
    "[": "х", "]": "ъ",
    "d": "в", "D": "В",
    "r": "к", "R": "К",
    "v": "м", "V": "М",
    "q": "й", "Q": "Й",
    "a": "ф", "A": "Ф",
    "x": "ч", "X": "Ч",
    "s": "ы", "S": "Ы",
    "/": ".",
}


def bindings_with_aliases(*specs: Binding) -> list[Binding]:
    """Each spec, plus a hidden RU twin where one exists in
    :data:`LAYOUT_ALIASES`. Specs whose key is not in the map pass
    through untouched. No errors, no whitelist."""
    out: list[Binding] = []
    for spec in specs:
        out.append(spec)
        twin_key = LAYOUT_ALIASES.get(spec.key)
        if twin_key is None:
            continue
        out.append(Binding(twin_key, spec.action, spec.description, show=False, priority=spec.priority))
    return out
```

- [ ] **Step 4: Run keymap tests**

```bash
uv run pytest tests/test_uxon_keymap.py -v
```
Expected: PASS.

- [ ] **Step 5: Drop `Esc → quit` from `MainScreen.BINDINGS`**

In `src/uxon/tui/screens/main.py`:
  - Delete line 101: `Binding("escape", "quit", "Quit", show=False),`.
  - Wrap `BINDINGS` via `bindings_with_aliases` for layout-invariance:

    ```python
    from ..keymap import bindings_with_aliases
    ...
    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("q", "quit", "Quit", show=True),
        Binding("f1", "help", "Help", show=False),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("d", "kill", "Kill", show=True),
        Binding("D", "kill_all_own", "Kill-ALL (mine)", show=True),
        Binding("a", "enable_detected", "Enable detected", show=False),
        Binding("x", "dismiss_detected", "Dismiss detected", show=False),
        Binding("up", "app.focus_previous", "", show=False),
        Binding("down", "app.focus_next", "", show=False),
        Binding("1", "digit_jump(1)", "1-9 jump", show=True, priority=True),
        Binding("2", "digit_jump(2)", "", show=False, priority=True),
        Binding("3", "digit_jump(3)", "", show=False, priority=True),
        Binding("4", "digit_jump(4)", "", show=False, priority=True),
        Binding("5", "digit_jump(5)", "", show=False, priority=True),
        Binding("6", "digit_jump(6)", "", show=False, priority=True),
        Binding("7", "digit_jump(7)", "", show=False, priority=True),
        Binding("8", "digit_jump(8)", "", show=False, priority=True),
        Binding("9", "digit_jump(9)", "", show=False, priority=True),
    )
    ```

  (`v`, `[`, `]`, `/` arrive in later tasks — added through this same helper.)

- [ ] **Step 6: Wrap other screens' bindings**

For each `src/uxon/tui/screens/*.py` file declaring `BINDINGS: ClassVar[list[Binding]] = [...]`, change `[...]` to `bindings_with_aliases(...)`. Pure mechanical edit. Don't change any binding key, action, or `show` value.

- [ ] **Step 7: Update bindings drift guard**

In `tests/test_uxon_tui_bindings.py`, replace the `escape → quit` assertion with:

```python
def test_main_screen_quit_bindings():
    keys = {b.key for b in MainScreen.BINDINGS}
    assert "q" in keys and "й" in keys
    assert "escape" not in keys
```

- [ ] **Step 8: Pilot smoke — Esc no longer quits**

Add a small Pilot test in `tests/test_uxon_tui_main_screen_pilot.py`:

```python
async def test_escape_does_not_quit_main_screen():
    async with _make_app().run_test() as pilot:
        await pilot.press("escape")
        # If escape quit, the test would have terminated; assert app still running.
        assert pilot.app.screen.__class__.__name__ == "MainScreen"
```

- [ ] **Step 9: Run keymap + bindings + main-screen pilot tests**

```bash
uv run pytest tests/test_uxon_keymap.py tests/test_uxon_tui_bindings.py tests/test_uxon_tui_main_screen_pilot.py -v
```
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/uxon/tui/keymap.py src/uxon/tui/screens/ tests/test_uxon_keymap.py tests/test_uxon_tui_bindings.py tests/test_uxon_tui_main_screen_pilot.py
git commit -m "feat(tui): keymap aliases + remove Esc-quit"
```

---

## Task 6: View modes (`by_host` default, `flat` toggle)

**Goal:** `DashboardUiState.view_mode` reactive on the screen; `v` binding toggles; `tui.table.default_view` setting picks the initial value. Config schema gains `tui.table.default_view`, `tui.search.fields`, `tui.color_palette`, `local_host.color`. The `ssh_multiplex` registration follows in Task 10 to keep that commit single-purpose.

**Files:**
- Modify: `src/uxon/settings.py:37+` (registrations)
- Modify: `src/uxon/cli.py:88+` (`DEFAULT_CONFIG`), `:170+` (cfg dataclass), `:520+` (loader)
- Modify: `src/uxon/tui/context.py:289+` (cfg passthrough fields)
- Modify: `src/uxon/tui/screens/main.py` (read `view_mode`, add `v` binding + handler, render branch)
- Test: `tests/test_dashboard_ui_state.py` (extend with view_mode reducers — already covered in Task 3)
- Test: `tests/test_settings_view_keys.py` *(new)*

- [ ] **Step 1: Write failing test for the new settings**

Create `tests/test_settings_view_keys.py`:

```python
from __future__ import annotations
import pytest
from uxon.settings import SETTINGS_SPECS, SCHEMA_KEYS


def test_default_view_registered():
    assert "tui.table.default_view" in SCHEMA_KEYS


def test_search_fields_registered():
    assert "tui.search.fields" in SCHEMA_KEYS


def test_color_palette_registered():
    assert "tui.color_palette" in SCHEMA_KEYS


def test_local_host_color_registered():
    assert "local_host.color" in SCHEMA_KEYS
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_settings_view_keys.py -v
```
Expected: FAIL — keys not in `SETTINGS_SPECS`.

- [ ] **Step 3: Register keys**

Append to the `SETTINGS_SPECS` tuple in `src/uxon/settings.py:37+`:

```python
SettingSpec(
    "tui.table.default_view",
    "enum",
    "Default dashboard view (by_host or flat).",
    choices=("by_host", "flat"),
),
SettingSpec(
    "tui.search.fields",
    "array",
    "Fields the SearchBar substring-matches against. "
    "Allowed: name, user, host, path, cmd. Default ['name','user'].",
),
SettingSpec(
    "tui.color_palette",
    "array",
    "Auto-cycle palette for remote-host blocks (Rich style names). "
    "Default ['cyan','blue']; no magenta, no red, no yellow.",
),
SettingSpec(
    "local_host.color",
    "string",
    "Rich style spec painting the locals block. Default 'green'.",
),
```

- [ ] **Step 4: Add to `DEFAULT_CONFIG` and cfg loader**

In `src/uxon/cli.py:88+`, extend `DEFAULT_CONFIG`:

```python
"tui": {
    "table": {"default_view": "by_host"},
    "search": {"fields": ["name", "user"]},
    "color_palette": ["cyan", "blue"],
},
"local_host": {"color": "green"},
```

In the cfg dataclass (around line 170-180):

```python
tui_table_default_view: str = "by_host"
tui_search_fields: tuple[str, ...] = ("name", "user")
tui_color_palette: tuple[str, ...] = ("cyan", "blue")
local_host_color: str = "green"
```

In the loader (the block currently around `tui_table_columns_raw = ...`, ~lines 520-545):

```python
tui_table_default_view_raw = tui_table_tbl.get("default_view", "by_host")
if tui_table_default_view_raw not in ("by_host", "flat"):
    fail(f"tui.table.default_view must be 'by_host' or 'flat', got {tui_table_default_view_raw!r}")

tui_search_tbl = merged.get("tui", {}).get("search", {})
fields_raw = tui_search_tbl.get("fields", ["name", "user"])
allowed = {"name", "user", "host", "path", "cmd"}
if not isinstance(fields_raw, list) or not all(f in allowed for f in fields_raw):
    bad = [f for f in fields_raw if f not in allowed] if isinstance(fields_raw, list) else fields_raw
    fail(f"tui.search.fields: unknown entries {bad!r}; allowed {sorted(allowed)!r}")
tui_search_fields = tuple(fields_raw)

palette_raw = merged.get("tui", {}).get("color_palette", ["cyan", "blue"])
if not isinstance(palette_raw, list) or not all(isinstance(c, str) and c for c in palette_raw):
    fail("tui.color_palette must be a list of non-empty strings")
tui_color_palette = tuple(palette_raw)

local_host_color = str(merged.get("local_host", {}).get("color", "green"))
if not local_host_color:
    fail("local_host.color must be non-empty")
```

Pass these into the cfg constructor at the end of `load_config`.

- [ ] **Step 5: Plumb through `TuiContext`**

In `src/uxon/tui/context.py:289+`, append:

```python
tui_table_default_view: str = "by_host"
tui_search_fields: tuple[str, ...] = ("name", "user")
tui_color_palette: tuple[str, ...] = ("cyan", "blue")
local_host_color: str = "green"
```

Wire passthrough wherever the `TuiContext` is constructed from cfg (`src/uxon/cli.py` `make_tui_context` or equivalent).

- [ ] **Step 6: Add `v` binding + render branch**

In `src/uxon/tui/screens/main.py`, in the `bindings_with_aliases(...)` call:

```python
Binding("v", "toggle_view", "View", show=True),
```

In `__init__`, replace the `DashboardUiState()` line:

```python
self._dashboard_ui = DashboardUiState(view_mode=self.ctx.tui_table_default_view)
```

Drop the unused import `cycle_sort, toggle_sort_dir` already done in Task 3; add `set_view_mode`:

```python
from ..dashboard.ui_state import DashboardUiState, set_view_mode
```

Add the action handler:

```python
def action_toggle_view(self) -> None:
    new_mode = "flat" if self._dashboard_ui.view_mode == "by_host" else "by_host"
    self._dashboard_ui = set_view_mode(self._dashboard_ui, new_mode)
    self._refresh_dashboard()
    self.app.notify(f"View: {new_mode.replace('_', ' ')}")
```

The actual rendering branch (host tab strip on `by_host`, no strip on `flat`) lands in Task 8 — for now `action_toggle_view` flips the state and re-runs the model selector; the visual difference is filled in once the strip exists.

- [ ] **Step 7: Test view_mode reducers via existing `test_dashboard_ui_state.py`**

(Coverage already added in Task 3 Step 6.)

- [ ] **Step 8: Run settings + ui_state tests**

```bash
uv run pytest tests/test_settings_view_keys.py tests/test_dashboard_ui_state.py -v
```
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/uxon/settings.py src/uxon/cli.py src/uxon/tui/context.py src/uxon/tui/screens/main.py tests/test_settings_view_keys.py
git commit -m "feat(tui): view modes (by_host default, flat toggle)"
```

---

## Task 7: Host buckets + `HostStatusBar`

**Goal:** Pure selectors `select_host_buckets`, `select_host_status_block`. Widget `HostStatusBar` renders one or many `HostStatusLine`s. Status bar is mounted but its visibility is wired in Task 8 (under tabs) and Task 9 (above flat table).

**Files:**
- Create: `src/uxon/tui/dashboard/buckets.py`
- Create: `src/uxon/tui/widgets/host_status_bar.py`
- Test: `tests/test_dashboard_buckets.py` *(new)*

- [ ] **Step 1: Write failing tests for `select_host_buckets` + `select_host_status_block`**

Create `tests/test_dashboard_buckets.py`:

```python
from __future__ import annotations
from types import SimpleNamespace

from uxon.tui.dashboard.buckets import HostBucket, select_host_buckets, select_host_status_block


def _row(host, name, attached=False, cpu=0.0, user="me"):
    return SimpleNamespace(host=host, name=name, attached=attached, cpu_pct=cpu, user=user)


def test_buckets_in_cfg_order_with_locals_first_and_empty_kept():
    rows = (_row(None, "a"), _row("kris", "k1"), _row("kris", "k2"))
    cfg = SimpleNamespace(remote_hosts=[SimpleNamespace(name="kris"), SimpleNamespace(name="ada")])
    buckets = select_host_buckets(rows, cfg, state=SimpleNamespace())
    assert [b.host_name for b in buckets] == [None, "kris", "ada"]
    assert [len(b.rows) for b in buckets] == [1, 2, 0]


def test_status_block_aggregates_per_host():
    rows = (_row(None, "a", cpu=10), _row(None, "b", cpu=20, attached=True))
    cfg = SimpleNamespace(remote_hosts=[])
    state = SimpleNamespace(main=SimpleNamespace(host_stats=None))
    lines = select_host_status_block(rows, state, host_stats_local=None, cfg=cfg)
    local_line = lines[0]
    assert local_line.host_name is None
    assert local_line.session_count == 2
    assert local_line.attached_count == 1
    assert abs(local_line.cpu_pct_sum - 30.0) < 1e-6
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_dashboard_buckets.py -v
```
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `buckets.py`**

Create `src/uxon/tui/dashboard/buckets.py`:

```python
"""Pure selectors for by-host views: buckets + status lines.

Two selectors layered on the unified row tuple from
:func:`select_dashboard_model`. Both consume the *result*, never the
selector input — the row tuple is the contract.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .row import SessionRow


@dataclass(frozen=True, slots=True)
class HostBucket:
    """One per configured host plus locals; preserved across empty hosts."""

    host_name: str | None  # None == locals
    label: str             # "local" or RemoteHost.name
    rows: tuple[SessionRow, ...]


@dataclass(frozen=True, slots=True)
class HostStatusLine:
    """Aggregated status of one bucket; rendered by HostStatusBar."""

    host_name: str | None
    label: str
    session_count: int
    attached_count: int
    cpu_pct_sum: float
    mem_used_kib: int
    mem_total_kib: int
    loadavg_1m: float | None
    uptime_s: int | None
    state: str  # "" | "(cached)" | "pending…" | "unreachable"


def select_host_buckets(
    rows: tuple[SessionRow, ...],
    cfg,
    state,
) -> tuple[HostBucket, ...]:
    grouped: dict[str | None, list] = {None: []}
    for host in cfg.remote_hosts:
        grouped[host.name] = []
    for row in rows:
        grouped.setdefault(row.host, []).append(row)
    out: list[HostBucket] = [HostBucket(None, "local", tuple(grouped.get(None, ())))]
    for host in cfg.remote_hosts:
        out.append(HostBucket(host.name, host.name, tuple(grouped.get(host.name, ()))))
    return tuple(out)


def _bucket_state(host_name: str | None, state) -> str:
    if host_name is None:
        return ""
    slot = getattr(state, "remote", {}).get(host_name)
    if slot is None or getattr(slot, "value", None) is None:
        # No snapshot yet → pending.
        return "pending…"
    snap = slot.value
    if getattr(snap, "from_cache", False):
        return "(cached)"
    if getattr(slot, "breaker_open", False):
        return "unreachable"
    return ""


def _hs_field(stats: dict[str, Any] | None, key: str, default: Any = 0) -> Any:
    if stats is None:
        return default
    return stats.get(key, default)


def select_host_status_block(
    rows: tuple[SessionRow, ...],
    state,
    host_stats_local: Any,
    cfg,
) -> tuple[HostStatusLine, ...]:
    buckets = select_host_buckets(rows, cfg, state)
    out: list[HostStatusLine] = []
    for bucket in buckets:
        if bucket.host_name is None:
            stats = host_stats_local  # may be None on cold start
        else:
            slot = getattr(state, "remote", {}).get(bucket.host_name)
            snap = getattr(slot, "value", None) if slot else None
            stats = getattr(snap, "host_stats", None) if snap else None
        cpu_sum = sum(getattr(r, "cpu_pct", 0.0) or 0.0 for r in bucket.rows)
        attached = sum(1 for r in bucket.rows if getattr(r, "attached", False))
        out.append(HostStatusLine(
            host_name=bucket.host_name,
            label=bucket.label,
            session_count=len(bucket.rows),
            attached_count=attached,
            cpu_pct_sum=cpu_sum,
            mem_used_kib=_hs_field(stats, "mem_used_kib", 0),
            mem_total_kib=_hs_field(stats, "mem_total_kib", 0),
            loadavg_1m=_hs_field(stats, "loadavg_1m", None),
            uptime_s=_hs_field(stats, "uptime_s", None),
            state=_bucket_state(bucket.host_name, state),
        ))
    return tuple(out)
```

- [ ] **Step 4: Run buckets test**

```bash
uv run pytest tests/test_dashboard_buckets.py -v
```
Expected: PASS.

- [ ] **Step 5: Implement `HostStatusBar` widget**

Create `src/uxon/tui/widgets/host_status_bar.py`:

```python
"""HostStatusBar — renders one or many HostStatusLine entries.

Modes:
- ``compact``: single-line render of one bucket (used under the
  active tab in by_host view).
- ``expanded``: one line per bucket, vertical layout (used above
  the table in flat view).

The widget is presentational — it does no aggregation. Owners pass
in a freshly-computed tuple of ``HostStatusLine`` and call
:meth:`update_lines`.
"""

from __future__ import annotations
from typing import Literal

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from ..dashboard.buckets import HostStatusLine


def _format_uptime(seconds: int | None) -> str:
    if seconds is None or seconds <= 0:
        return "—"
    days = seconds // 86_400
    hours = (seconds % 86_400) // 3600
    if days:
        return f"{days}d{hours}h"
    minutes = (seconds % 3600) // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _format_mem(used: int, total: int) -> str:
    if total <= 0:
        return "—/—"
    return f"{used // 1024} MiB / {total // 1024} MiB"


def _render(line: HostStatusLine) -> str:
    state = f" · {line.state}" if line.state else ""
    la = f" · la {line.loadavg_1m:.2f}" if line.loadavg_1m is not None else ""
    return (
        f"{line.label}  {line.session_count} sess · {line.attached_count} attached · "
        f"cpu Σ{line.cpu_pct_sum:.0f}% · mem {_format_mem(line.mem_used_kib, line.mem_total_kib)}"
        f"{la} · up {_format_uptime(line.uptime_s)}{state}"
    )


class HostStatusBar(Widget):
    """One- or many-line per-host status renderer."""

    DEFAULT_CSS = """
    HostStatusBar {
        height: auto;
        padding: 0 1;
    }
    HostStatusBar > Static {
        color: $text-muted;
    }
    """

    def __init__(self, *, mode: Literal["compact", "expanded"], id: str | None = None) -> None:
        super().__init__(id=id)
        self._mode = mode
        self._lines: tuple[HostStatusLine, ...] = ()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("", id=f"{self.id}-line-0")

    def update_lines(self, lines: tuple[HostStatusLine, ...]) -> None:
        self._lines = lines
        try:
            container = self.query_one(Vertical)
        except Exception:
            return
        # Mount/dismount Static rows to match line count. Compact
        # mode shows just lines[0]; expanded shows all.
        target = lines[:1] if self._mode == "compact" else lines
        existing = list(container.children)
        # Drop excess.
        for w in existing[len(target):]:
            w.remove()
        # Update / add.
        for i, line in enumerate(target):
            text = _render(line)
            if i < len(existing):
                existing[i].update(text)
            else:
                container.mount(Static(text, id=f"{self.id or 'hsb'}-line-{i}"))
```

This commit ships the widget unmounted by `MainScreen` — Task 8 (host tabs) and Task 9 (search bar) wire it into the layout. The CSS lands in `styles.tcss` as part of those commits.

- [ ] **Step 6: Smoke test the renderer**

Append to `tests/test_dashboard_buckets.py`:

```python
def test_host_status_bar_renders_a_line():
    from uxon.tui.widgets.host_status_bar import _render
    line = HostStatusLine(
        host_name=None, label="local",
        session_count=3, attached_count=1, cpu_pct_sum=42.5,
        mem_used_kib=8_000_000, mem_total_kib=16_000_000,
        loadavg_1m=0.42, uptime_s=3600 * 26, state="",
    )
    rendered = _render(line)
    assert "local" in rendered and "3 sess" in rendered and "1 attached" in rendered
```

- [ ] **Step 7: Run buckets + status-bar tests**

```bash
uv run pytest tests/test_dashboard_buckets.py -v
```
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/uxon/tui/dashboard/buckets.py src/uxon/tui/widgets/host_status_bar.py tests/test_dashboard_buckets.py
git commit -m "feat(tui): host buckets + HostStatusBar"
```

---

## Task 8: Host tab strip

**Goal:** `HostTabStrip` widget shows one tab per `HostBucket` in priority order; active tab is highlighted; `[`/`]` move focus across tabs and post `HostTabActivated(index)`. `MainScreen` mounts it above the dashboard in `by_host` view; hides in `flat` view; `_refresh_dashboard` filters the row tuple to the active bucket when in `by_host`.

**Files:**
- Create: `src/uxon/tui/widgets/host_tab_strip.py`
- Modify: `src/uxon/tui/screens/main.py` (mount tabs, plumb `[`/`]` bindings, filter rows by active tab)
- Modify: `src/uxon/tui/styles.tcss` (tab classes)
- Test: `tests/test_host_tab_strip.py` *(new)*
- Test: `tests/test_uxon_tui_main_screen_pilot.py` (extend smoke)

- [ ] **Step 1: Write failing tab-strip tests**

Create `tests/test_host_tab_strip.py`:

```python
from __future__ import annotations
from textual.app import App
from uxon.tui.widgets.host_tab_strip import HostTabStrip, HostTabActivated
from uxon.tui.dashboard.buckets import HostBucket


def _bucket(name):
    return HostBucket(host_name=name, label=name or "local", rows=())


async def test_tab_strip_emits_activated_on_index_change():
    app = App()
    strip = HostTabStrip([_bucket(None), _bucket("kris"), _bucket("ada")])

    async with app.run_test() as pilot:
        await app.mount(strip)
        events: list[int] = []
        strip.add_listener("HostTabActivated", lambda ev: events.append(ev.index))
        strip.active_index = 1
        await pilot.pause()
        assert events == [1]
```

(Adjust to your project's existing event-listener test pattern; the assertion on `events == [1]` is the contract.)

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_host_tab_strip.py -v
```
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `HostTabStrip`**

Create `src/uxon/tui/widgets/host_tab_strip.py`:

```python
"""HostTabStrip — one tab per HostBucket.

Reactive ``active_index``. Posts :class:`HostTabActivated` on change.
The label is :attr:`HostBucket.label`; coloring is done by the screen
via the same ``assign_block_colors`` map shared with the dashboard.
"""

from __future__ import annotations
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ..dashboard.buckets import HostBucket


class HostTabActivated(Message):
    """Posted whenever ``active_index`` changes."""

    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index


class HostTabStrip(Widget):
    DEFAULT_CSS = """
    HostTabStrip {
        height: 1;
        padding: 0 1;
    }
    HostTabStrip > Horizontal {
        height: 1;
    }
    HostTabStrip Static {
        margin-right: 2;
        text-style: dim;
    }
    HostTabStrip Static.-active {
        text-style: bold;
        background: $accent 30%;
    }
    """

    active_index: reactive[int] = reactive(0)

    def __init__(self, buckets: list[HostBucket], *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._buckets = buckets

    def compose(self) -> ComposeResult:
        with Horizontal():
            for i, bucket in enumerate(self._buckets):
                cls = "-active" if i == self.active_index else ""
                yield Static(bucket.label, classes=cls, id=f"tab-{i}")

    def watch_active_index(self, old: int, new: int) -> None:
        if old == new:
            return
        for i, _ in enumerate(self._buckets):
            try:
                w = self.query_one(f"#tab-{i}", Static)
            except Exception:
                continue
            w.set_class(i == new, "-active")
        self.post_message(HostTabActivated(new))

    def set_buckets(self, buckets: list[HostBucket]) -> None:
        """Replace the bucket list (re-mount Static children)."""
        self._buckets = buckets
        # Remove old children, mount fresh.
        try:
            container = self.query_one(Horizontal)
        except Exception:
            return
        for child in list(container.children):
            child.remove()
        for i, bucket in enumerate(buckets):
            cls = "-active" if i == self.active_index else ""
            container.mount(Static(bucket.label, classes=cls, id=f"tab-{i}"))
```

- [ ] **Step 4: Mount tab strip + filter in `MainScreen`**

In `src/uxon/tui/screens/main.py`:
  - Add to `bindings_with_aliases(...)`: `Binding("[", "prev_tab", "Prev host", show=True)`, `Binding("]", "next_tab", "Next host", show=True)`.
  - In `compose`, between `── sessions ──` and the `SessionDashboardTable`, when `self._dashboard_ui.view_mode == "by_host"` yield `HostTabStrip([...])` with id `host-tabs` and a `HostStatusBar(mode="compact", id="host-status-compact")`.
  - In `_refresh_dashboard`, after computing `rows = select_dashboard_model(...)`:

    ```python
    buckets = select_host_buckets(rows, cfg_view, state)
    if self._dashboard_ui.view_mode == "by_host" and not self._dashboard_ui.filter_text:
        try:
            tab_strip = self.query_one("#host-tabs", HostTabStrip)
        except Exception:
            active_idx = 0
        else:
            tab_strip.set_buckets(list(buckets))
            active_idx = tab_strip.active_index
        active_bucket = buckets[active_idx] if 0 <= active_idx < len(buckets) else buckets[0]
        rows = active_bucket.rows
    # else: flat or filter-forced flat — render rows as-is.
    ```

  - Add action handlers:

    ```python
    def action_prev_tab(self) -> None:
        try:
            strip = self.query_one("#host-tabs", HostTabStrip)
        except Exception:
            return
        n = len(strip._buckets)
        if n <= 1:
            return
        strip.active_index = (strip.active_index - 1) % n

    def action_next_tab(self) -> None:
        try:
            strip = self.query_one("#host-tabs", HostTabStrip)
        except Exception:
            return
        n = len(strip._buckets)
        if n <= 1:
            return
        strip.active_index = (strip.active_index + 1) % n

    def on_host_tab_activated(self, event: HostTabActivated) -> None:
        self._refresh_dashboard()
    ```

- [ ] **Step 5: Add CSS for tab strip**

In `src/uxon/tui/styles.tcss`, append under the SessionDashboardTable section:

```css
/* ── HostTabStrip ───────────────────────────────────────────────── */

HostTabStrip {
    margin-bottom: 1;
}

/* ── HostStatusBar ──────────────────────────────────────────────── */

HostStatusBar {
    margin-bottom: 1;
}
```

- [ ] **Step 6: Run tab-strip + main-screen tests**

```bash
uv run pytest tests/test_host_tab_strip.py tests/test_uxon_tui_main_screen_pilot.py -v
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/uxon/tui/widgets/host_tab_strip.py src/uxon/tui/screens/main.py src/uxon/tui/styles.tcss tests/test_host_tab_strip.py
git commit -m "feat(tui): host tab strip"
```

---

## Task 9: Search bar

**Goal:** `SearchBar` widget mounted above the dashboard, default focus on screen mount; `/` from anywhere refocuses; non-empty filter forces flat render; Esc clears, then blurs, then no-ops.

**Files:**
- Create: `src/uxon/tui/widgets/search_bar.py`
- Modify: `src/uxon/tui/screens/main.py` (mount, default focus, `/` binding, filter-forces-flat branch)
- Modify: `src/uxon/tui/styles.tcss`
- Test: `tests/test_search_bar.py` *(new)*
- Test: `tests/test_uxon_tui_main_screen_pilot.py` (extend full smoke)

- [ ] **Step 1: Write failing search-bar test**

Create `tests/test_search_bar.py`:

```python
from __future__ import annotations
from textual.app import App
from uxon.tui.widgets.search_bar import SearchBar


async def test_search_bar_emits_filter_changed_on_typing():
    app = App()
    bar = SearchBar(id="search")
    async with app.run_test() as pilot:
        await app.mount(bar)
        events: list[str] = []
        bar.add_listener("FilterChanged", lambda ev: events.append(ev.text))
        bar.input.value = "kris"
        await pilot.pause()
        assert events[-1] == "kris"


async def test_search_bar_esc_clears_then_blurs():
    app = App()
    bar = SearchBar(id="search")
    async with app.run_test() as pilot:
        await app.mount(bar)
        bar.focus()
        bar.input.value = "abc"
        await pilot.press("escape")  # clears
        assert bar.input.value == ""
        assert app.focused is bar.input  # still focused
        await pilot.press("escape")  # blurs
        assert app.focused is not bar.input
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_search_bar.py -v
```
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `SearchBar`**

Create `src/uxon/tui/widgets/search_bar.py`:

```python
"""SearchBar — Input + match counter, scoped Esc behaviour."""

from __future__ import annotations
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Static


class FilterChanged(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class SearchBar(Widget):
    """Always-visible search bar above the dashboard.

    Esc behaviour (priority binding):
      - non-empty input: clear text, keep focus
      - empty input + focused: blur (move focus to dashboard)
    """

    DEFAULT_CSS = """
    SearchBar {
        height: 1;
        padding: 0 1;
    }
    SearchBar > Horizontal { height: 1; }
    SearchBar Input { width: 1fr; }
    SearchBar #match-count { width: auto; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "scope_cancel", "", show=False, priority=True),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.input = Input(placeholder="/ to search", id="search-input")
        self._counter = Static("", id="match-count")

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield self.input
            yield self._counter

    def on_input_changed(self, event: Input.Changed) -> None:
        self.post_message(FilterChanged(event.value))

    def set_match_count(self, count: int) -> None:
        if not self.input.value:
            self._counter.update("")
            return
        self._counter.update(f"{count} match{'es' if count != 1 else ''}")

    def action_scope_cancel(self) -> None:
        if self.input.value:
            self.input.value = ""
            return
        # blur to dashboard
        try:
            from .session_dashboard_table import SessionDashboardTable
            self.app.query_one(SessionDashboardTable).focus()
        except Exception:
            pass
```

- [ ] **Step 4: Mount in `MainScreen`, wire bindings**

In `src/uxon/tui/screens/main.py`:
  - In `bindings_with_aliases`: `Binding("/", "focus_search", "Search", show=True)`.
  - In `compose`, **above the `── sessions ──` header**, mount `SearchBar(id="search-bar")`.
  - In `on_mount`, after `_refresh_dashboard()`, set default focus:

    ```python
    try:
        bar = self.query_one("#search-bar", SearchBar)
        bar.input.focus()
    except Exception:
        pass
    ```

  - Replace `_focus_default_action` so it no longer steals focus when SearchBar exists (or, simpler, leave it intact — `on_mount` runs after `call_later`, so the SearchBar focus wins by being later in the call chain. Verify in the smoke test.)

  - Add action handler:

    ```python
    def action_focus_search(self) -> None:
        try:
            self.query_one("#search-bar", SearchBar).input.focus()
        except Exception:
            pass

    def on_filter_changed(self, event: FilterChanged) -> None:
        self._dashboard_ui = set_filter(self._dashboard_ui, event.text)
        self._refresh_dashboard()
        # Update match counter.
        try:
            bar = self.query_one("#search-bar", SearchBar)
            bar.set_match_count(len(self._dashboard_rows))
        except Exception:
            pass
    ```

  - In `_refresh_dashboard`, the filter-forces-flat branch:

    ```python
    needle = self._dashboard_ui.filter_text.strip()
    forced_flat = bool(needle)
    in_by_host = self._dashboard_ui.view_mode == "by_host" and not forced_flat
    # tab strip + status-compact visible only when in_by_host
    try:
        tab_strip = self.query_one("#host-tabs", HostTabStrip)
        tab_strip.display = in_by_host
    except Exception:
        pass
    ```

- [ ] **Step 5: Add CSS**

Append to `src/uxon/tui/styles.tcss`:

```css
/* ── SearchBar ──────────────────────────────────────────────────── */

SearchBar {
    margin-top: 1;
    margin-bottom: 0;
}
```

- [ ] **Step 6: Extend smoke pilot scenario**

In `tests/test_uxon_tui_main_screen_pilot.py`, add a scenario:

```python
async def test_smoke_search_filter_forces_flat_then_clear():
    async with _make_app(remote_hosts=[("kris",), ("ada",)]).run_test() as pilot:
        # SearchBar has focus.
        assert pilot.app.focused is not None and "Input" in type(pilot.app.focused).__name__
        await pilot.press("k", "r", "i", "s")  # type "kris"
        await pilot.pause()
        # Tab strip is hidden (filter forces flat).
        strip = pilot.app.query_one("#host-tabs")
        assert strip.display is False
        await pilot.press("escape")  # clear
        await pilot.pause()
        await pilot.press("escape")  # blur
        await pilot.press("]")
        await pilot.pause()
        await pilot.press("q")
```

- [ ] **Step 7: Run search + main-screen tests**

```bash
uv run pytest tests/test_search_bar.py tests/test_uxon_tui_main_screen_pilot.py -v
```
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/uxon/tui/widgets/search_bar.py src/uxon/tui/screens/main.py src/uxon/tui/styles.tcss tests/test_search_bar.py tests/test_uxon_tui_main_screen_pilot.py
git commit -m "feat(tui): search bar"
```

---

## Task 10: ControlPersist configurable; register `ssh_multiplex`

**Goal:** `_default_template` substitutes `ControlPersist={ssh_control_persist_seconds}s`. `ssh_control_persist_seconds` and `ssh_multiplex` are registered in `SETTINGS_SPECS`. Validation: persist must be positive integer.

**Files:**
- Modify: `src/uxon/remote_collector.py:188-214` (substitution placeholder + validator)
- Modify: `src/uxon/cli.py` (`DEFAULT_CONFIG`, cfg dataclass, loader, plumb into fetch path)
- Modify: `src/uxon/settings.py` (register both keys)
- Modify: `src/uxon/tui/context.py` (passthrough field)
- Test: `tests/test_settings_ssh_keys.py` *(new)*
- Test: `tests/test_remote_collector.py` (extend persist substitution)

- [ ] **Step 1: Write failing tests**

Create `tests/test_settings_ssh_keys.py`:

```python
from __future__ import annotations
import pytest
from uxon.settings import SCHEMA_KEYS


def test_ssh_multiplex_registered():
    assert "ssh_multiplex" in SCHEMA_KEYS


def test_ssh_control_persist_seconds_registered():
    assert "ssh_control_persist_seconds" in SCHEMA_KEYS
```

Add to `tests/test_remote_collector.py`:

```python
def test_default_template_uses_persist_placeholder():
    from uxon.remote_collector import _default_template
    template = _default_template()
    assert "ControlPersist={ssh_control_persist_seconds}s" in template
```

And a positive-integer validator test (write a small loader probe in this same file or in `test_cli_load_config.py`):

```python
def test_ssh_control_persist_seconds_must_be_positive():
    with pytest.raises(SystemExit, match="ssh_control_persist_seconds"):
        load_config_from_text('ssh_control_persist_seconds = 0\n')
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_settings_ssh_keys.py tests/test_remote_collector.py -v
```
Expected: FAIL.

- [ ] **Step 3: Substitute persist placeholder**

In `src/uxon/remote_collector.py:188-214` change the literal `"ControlPersist=60s"` to `"ControlPersist={ssh_control_persist_seconds}s"`. Update `_render_argv` (or wherever placeholders are resolved) to substitute `{ssh_control_persist_seconds}` from the cfg.

- [ ] **Step 4: Register settings**

In `src/uxon/settings.py:37+`, append:

```python
SettingSpec("ssh_multiplex", "enum", "Reuse one SSH master connection across fetches.", choices=("auto", "off")),
SettingSpec(
    "ssh_control_persist_seconds",
    "number",
    "ControlPersist for the multiplexed SSH master, seconds. Must be > 0; "
    "disable multiplexing via ssh_multiplex=off.",
),
```

In `src/uxon/cli.py:88+`, ensure `DEFAULT_CONFIG["ssh_control_persist_seconds"] = 300` is present.

In the cfg dataclass:

```python
ssh_control_persist_seconds: int = 300
```

In the loader, after the existing `ssh_multiplex` parse:

```python
persist_raw = merged.get("ssh_control_persist_seconds", 300)
try:
    persist = int(persist_raw)
except (TypeError, ValueError):
    fail(f"ssh_control_persist_seconds must be an integer, got {persist_raw!r}")
if persist <= 0:
    fail(f"ssh_control_persist_seconds must be > 0; disable via ssh_multiplex=off")
```

Pass `ssh_control_persist_seconds=persist` into the cfg constructor.

- [ ] **Step 5: Plumb through fetch call sites**

Every `fetch_remote_snapshot(..., ssh_multiplex=...)` site in `src/uxon/cli.py` (lines 1882, 1947, 1969, 2467, 2800, 3884, 4326, 4554, 4940, 4981, 5002) gains `ssh_control_persist_seconds=cfg.ssh_control_persist_seconds`. Update `fetch_remote_snapshot` signature in `remote_collector.py` to accept and pass it through to `_render_argv`.

- [ ] **Step 6: Plumb through `TuiContext`**

Add `ssh_control_persist_seconds: int = 300` to `src/uxon/tui/context.py`.

- [ ] **Step 7: Run new + existing tests**

```bash
uv run pytest tests/test_settings_ssh_keys.py tests/test_remote_collector.py tests/test_cli_load_config.py -v
```
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/uxon/remote_collector.py src/uxon/cli.py src/uxon/settings.py src/uxon/tui/context.py tests/test_settings_ssh_keys.py tests/test_remote_collector.py
git commit -m "feat(ssh): ControlPersist configurable; register schema"
```

---

## Task 11: Wire `host_stats` into `HostStatusBar`

**Goal:** `MainData.host_stats` field is populated by the local refresh worker; `MainScreen` reads it and `state.remote.<host>.value.host_stats` to render `HostStatusBar` for both view modes.

**Files:**
- Modify: `src/uxon/tui/main_data.py` (add `host_stats` field)
- Modify: `src/uxon/tui/tui_state.py` (refresh worker populates `host_stats`)
- Modify: `src/uxon/remote_collector.py` (`RemoteSnapshot.host_stats` field — already added in Task 2 if not, add here)
- Modify: `src/uxon/tui/screens/main.py` (mount `HostStatusBar` in both modes; render lines)
- Test: `tests/test_dashboard_buckets.py` (extend pending/cached/unreachable rendering)

- [ ] **Step 1: Add field to `MainData`**

In `src/uxon/tui/main_data.py`, add to the `MainData` dataclass:

```python
from uxon.probes import HostStatsResult

@dataclass(frozen=True, slots=True)
class MainData:
    # ... existing fields ...
    host_stats: HostStatsResult | None = None
```

- [ ] **Step 2: Populate from refresh worker**

In `src/uxon/tui/tui_state.py` (or wherever `MainData` is built — search for `MainData(`), call `read_host_stats()` once per refresh and embed:

```python
from uxon.probes import read_host_stats
...
hs = None
try:
    hs = read_host_stats()
except Exception:
    pass  # logged via debug; widget renders "pending…"
data = MainData(..., host_stats=hs)
```

- [ ] **Step 3: Render in `MainScreen`**

In `src/uxon/tui/screens/main.py`, after `_refresh_dashboard` recomputes `rows`:

```python
host_stats_local = state.main.host_stats if state.main is not None else None
status_lines = select_host_status_block(rows_full, state, host_stats_local, cfg_view)
# rows_full is the unfiltered tuple from select_dashboard_model
# (filter forces flat but the status bar still shows totals for all
# buckets in flat mode; in by_host mode the compact bar shows the
# active bucket's line only).
if in_by_host:
    bar = self.query_one("#host-status-compact", HostStatusBar)
    active = active_bucket.host_name
    line = next((l for l in status_lines if l.host_name == active), status_lines[0])
    bar.update_lines((line,))
else:
    bar = self.query_one("#host-status-expanded", HostStatusBar)
    bar.update_lines(status_lines)
```

Mount the expanded bar in `flat` view (toggle `display` like the tab strip).

- [ ] **Step 4: Add tests for pending/cached/unreachable**

Extend `tests/test_dashboard_buckets.py`:

```python
def test_status_block_marks_pending_when_no_snapshot():
    rows = ()
    cfg = SimpleNamespace(remote_hosts=[SimpleNamespace(name="kris")])
    state = SimpleNamespace(remote={}, main=None)
    lines = select_host_status_block(rows, state, host_stats_local=None, cfg=cfg)
    kris = next(l for l in lines if l.host_name == "kris")
    assert kris.state == "pending…"
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_dashboard_buckets.py tests/test_uxon_tui_main_screen_pilot.py -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/uxon/tui/main_data.py src/uxon/tui/tui_state.py src/uxon/remote_collector.py src/uxon/tui/screens/main.py tests/test_dashboard_buckets.py
git commit -m "feat(tui): wire host_stats into HostStatusBar"
```

---

## Task 12: Docs

**Files:**
- Modify: `docs/configuration.md` (new keys, contracts)
- Modify: `docs/agents/conventions.md` (flag namespace consolidation)

- [ ] **Step 1: Update `docs/configuration.md`**

Add a "Dashboard view + sort" section documenting:
  - Sort is a fixed contract (no setting); reading `tui.table.default_sort_by` is now silently ignored.
  - `tui.table.default_view` (`by_host` | `flat`).
  - `tui.search.fields` (default `["name", "user"]`; allowed `name|user|host|path|cmd`).
  - `tui.color_palette` (default `["cyan", "blue"]`).
  - `[local_host] color = "..."` and `[[remote_hosts]] color = "..."`.
  - `ssh_control_persist_seconds` default 300; must be > 0; `ssh_multiplex = "off"` to disable multiplexing.
  - Bindings table: `q`/`r`/`d`/`D`/`v`/`[`/`]`/`/`/`1-9`; `Esc` is a scoped cancel; layout aliases (RU twins) listed.

Add an "Attach indicator" note — `●` filled, `○` hollow.

- [ ] **Step 2: Update `docs/agents/conventions.md`**

Append under a new "Backlog" subsection:

> **Config namespace consolidation.** The `ssh_*` flat top-level
> keys (`ssh_multiplex`, `ssh_control_persist_seconds`) and the
> `tui.*` dotted family currently coexist with `[local_host]` and
> `[tui.color_palette]` sections. A future cleanup pass should
> consolidate to `[ssh]` and `[tui]` tables consistently. Touched
> in 2026-05-07 dashboard views work; not in scope for that PR.

- [ ] **Step 3: Commit**

```bash
git add docs/configuration.md docs/agents/conventions.md
git commit -m "docs: configuration + agents conventions backlog"
```

---

## Task 13: CHANGELOG polish

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Re-read and tighten the `[Unreleased]` block**

Open `CHANGELOG.md`. The `[Unreleased]` block was opened in Task 0 with predicted entries; Tasks 1-12 may have nudged some details. Walk the section line-by-line; remove any entry that didn't actually ship in this branch's diff (check `git log feat/dashboard-views --oneline`); add anything missed.

- [ ] **Step 2: Confirm CI gate stays green at every commit**

```bash
git log feat/dashboard-views --oneline
# Sanity check the sequence; no tag operations, no force-pushes.
uv run pytest -x
```
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "chore(changelog): finalise [Unreleased] entries"
```

---

## Self-Review (per writing-plans skill)

Walk the spec section-by-section. Each requirement points to a task above:

| Spec section | Tasks |
|---|---|
| Sort contract | Task 3 |
| Reconciler apply fix | Task 1 |
| View modes | Task 6 (state), Task 8 (rendering branch), Task 9 (filter forces flat) |
| Host tab strip | Task 8 |
| Host status bar | Task 7 (widget), Task 11 (data wiring) |
| Search | Task 9 |
| Block colours | Task 4 |
| Attached marker | Task 4 |
| Keymap and bindings | Task 5 (`q`/`й`, drop Esc-quit, helper); Task 6 (`v`); Task 8 (`[`/`]`); Task 9 (`/`) |
| SSH ControlPersist | Task 10 |
| Wire schema and host stats | Task 2 (envelope), Task 11 (consume in TUI) |
| Files table | Tasks 0-12 |
| Bindings final table | Tasks 5/6/8/9; drift guard updated in Task 5 |
| Edge cases | Single-host (Task 8 step "n ≤ 1 no-op"), pending/cached/unreachable (Task 11 step 4), filter+by_host (Task 9 step 4 branch), pinned-equal-prev (Task 4 step 1 test), old peer no host_stats (Task 2 step 6/7), search paste (Task 9 smoke) |
| Migration | Task 0 (version bump), Task 3 (drop sort_by silently), Task 2 (no schema bump) |
| Commit sequence | Task numbering 0-13 = spec commits 0-13 |

No placeholders. No "TBD". Commit message subjects match the spec's commit-sequence table.

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-07-session-dashboard-views.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints.

**Which approach?**
