# SPDX-License-Identifier: MIT
"""AC8 — static enforcement of the single-spawn-seam policy.

Every external-tool spawn in ``src/uxon/`` must go through ``run_query``
(background, Lane A) or the one sanctioned interactive handoff in
``tui/launch.py`` (Lane B). This walks the AST of every first-party module
and fails if a raw ``subprocess.run`` / ``.call`` / ``.check_output`` /
``.check_call`` / ``Popen`` *call-expression* appears anywhere else.

AST (not grep) so comments, strings, and the loop guard's
``subprocess.Popen.__init__`` *patch* (an attribute assignment, never a
call) don't produce false positives.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "uxon"

#: Files permitted to spawn directly. ``infra/run.py`` *is* ``run_query``;
#: ``tui/launch.py`` is the single Lane-B interactive handoff.
_ALLOWED = frozenset(
    {
        _SRC_ROOT / "infra" / "run.py",
        _SRC_ROOT / "tui" / "launch.py",
    }
)

#: ``subprocess.<attr>(`` spawn entry points.
_SUBPROCESS_SPAWN_ATTRS = frozenset({"run", "call", "check_output", "check_call", "Popen"})


def _spawn_callee_name(call: ast.Call) -> str | None:
    """Return a label for a raw-spawn call-expression, else ``None``.

    Matches ``subprocess.run(`` / ``.call(`` / ``.check_output(`` /
    ``.check_call(`` / ``.Popen(`` and a bare ``Popen(`` (from-import form).
    A bare ``run(``/``call(`` is *not* matched — those collide with unrelated
    methods (``self.run``, ``thread.run``); the spawn policy only forbids the
    ``subprocess`` module's spawns and any ``Popen``.
    """
    func = call.func
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
            if func.attr in _SUBPROCESS_SPAWN_ATTRS:
                return f"subprocess.{func.attr}"
        if func.attr == "Popen":
            return "Popen"
    elif isinstance(func, ast.Name) and func.id == "Popen":
        return "Popen"
    return None


def _violations_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _spawn_callee_name(node)
            if name is not None:
                out.append(f"{path}:{node.lineno}: raw spawn {name}(")
    return out


def test_no_raw_subprocess_spawn_outside_run_query() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path in _ALLOWED:
            continue
        offenders.extend(_violations_in(path))
    assert not offenders, "raw external-tool spawns must go through run_query:\n" + "\n".join(
        offenders
    )


def test_guard_is_a_recognised_allowlist_invariant() -> None:
    # The allowlist must stay tiny and real — guard against a file being
    # added to it that no longer exists (a silent hole in the policy).
    for path in _ALLOWED:
        assert path.is_file(), f"allowlisted spawn site missing: {path}"
