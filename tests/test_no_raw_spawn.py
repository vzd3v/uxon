# SPDX-License-Identifier: MIT
"""Static enforcement of the single-spawn-seam policy.

Every external-tool spawn in ``src/uxon/`` must go through ``run_query``
(background, Lane A) or the one sanctioned interactive handoff in
``app/session_handoff.py`` (Lane B). This walks the AST of every first-party module
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
#: ``app/session_handoff.py`` is the single Lane-B interactive handoff.
_ALLOWED = frozenset(
    {
        _SRC_ROOT / "infra" / "run.py",
        _SRC_ROOT / "app" / "session_handoff.py",
    }
)

#: ``subprocess.<attr>(`` spawn entry points.
_SUBPROCESS_SPAWN_ATTRS = frozenset({"run", "call", "check_output", "check_call", "Popen"})


def _spawn_callee_name(call: ast.Call, modules: set[str], imports: dict[str, str]) -> str | None:
    """Return a label for a raw-spawn call-expression, else ``None``.

    Imported module/function aliases retain their original subprocess API
    name in diagnostics. Unrelated bare ``run`` functions are allowed.
    Dynamic dispatch is outside this source check; the runtime guard
    independently verifies spawns when the application executes them.
    """
    func = call.func
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id in modules:
            if func.attr in _SUBPROCESS_SPAWN_ATTRS:
                return f"subprocess.{func.attr}"
    elif isinstance(func, ast.Name):
        return imports.get(func.id)
    return None


def _violations_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "subprocess"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            imports.update(
                {
                    alias.asname or alias.name: f"subprocess.{alias.name}"
                    for alias in node.names
                    if alias.name in _SUBPROCESS_SPAWN_ATTRS
                }
            )
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _spawn_callee_name(node, modules, imports)
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
