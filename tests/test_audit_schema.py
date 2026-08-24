# SPDX-License-Identifier: MIT
"""Guard literal audit calls against the published schema vocabulary."""

from __future__ import annotations

import ast
from pathlib import Path

from uxon.domain.audit_schema import (
    AUDIT_COMMON_OPTIONAL_FIELDS,
    AUDIT_EVENT_FIELDS,
    AUDIT_EVENT_OUTCOMES,
)


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
            unknown = fields - AUDIT_EVENT_FIELDS[event] - AUDIT_COMMON_OPTIONAL_FIELDS
            assert not unknown, (
                f"undeclared fields {sorted(unknown)} for {event} at {path}:{node.lineno}"
            )
            outcome_keywords = [kw for kw in node.keywords if kw.arg == "outcome"]
            if not outcome_keywords:
                assert "ok" in AUDIT_EVENT_OUTCOMES[event]
                continue
            outcome_node = outcome_keywords[0].value
            literal_outcomes = {
                child.value
                for child in ast.walk(outcome_node)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            }
            unknown_outcomes = literal_outcomes - AUDIT_EVENT_OUTCOMES[event]
            assert not unknown_outcomes, (
                f"undeclared outcomes {sorted(unknown_outcomes)} for {event} "
                f"at {path}:{node.lineno}"
            )
    # ``session.kill`` is routed through the shared kill helper as a typed
    # event parameter so CLI and TUI use one finalization path.
    assert seen | {"session.kill"} == set(AUDIT_EVENT_FIELDS)
    assert set(AUDIT_EVENT_OUTCOMES) == set(AUDIT_EVENT_FIELDS)
    assert not any(AUDIT_COMMON_OPTIONAL_FIELDS & fields for fields in AUDIT_EVENT_FIELDS.values())
    assert set().union(*AUDIT_EVENT_OUTCOMES.values()) == {
        "ok",
        "error",
        "denied",
        "not_found",
        "skipped",
    }


def test_audit_reference_lists_every_declared_event() -> None:
    root = Path(__file__).resolve().parent.parent
    reference = (root / "docs" / "reference" / "audit-events.md").read_text(encoding="utf-8")
    for event in AUDIT_EVENT_FIELDS:
        assert f"| `{event}`" in reference
