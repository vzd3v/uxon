# SPDX-License-Identifier: MIT
"""Guard literal audit calls against the published schema vocabulary."""

from __future__ import annotations

import ast
from pathlib import Path

from uxon.domain.audit_schema import AUDIT_EVENT_FIELDS


def test_literal_audit_calls_use_declared_events_and_fields() -> None:
    root = Path(__file__).resolve().parent.parent / "src" / "uxon"
    seen: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "audit":
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            event = node.args[0].value
            if not isinstance(event, str):
                continue
            seen.add(event)
            assert event in AUDIT_EVENT_FIELDS, (
                f"undeclared audit event {event!r} at {path}:{node.lineno}"
            )
            fields = {kw.arg for kw in node.keywords if kw.arg not in {None, "outcome"}}
            unknown = fields - AUDIT_EVENT_FIELDS[event]
            assert not unknown, (
                f"undeclared fields {sorted(unknown)} for {event} at {path}:{node.lineno}"
            )
    # ``session.kill`` is routed through the shared kill helper as a typed
    # event parameter so CLI and TUI use one finalization path.
    assert seen | {"session.kill"} == set(AUDIT_EVENT_FIELDS)


def test_audit_reference_lists_every_declared_event() -> None:
    root = Path(__file__).resolve().parent.parent
    reference = (root / "docs" / "reference" / "audit-events.md").read_text(encoding="utf-8")
    for event in AUDIT_EVENT_FIELDS:
        assert f"| `{event}`" in reference
