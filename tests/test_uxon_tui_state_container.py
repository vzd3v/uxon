"""Availability seeding and loading/rebuild behavior at the App boundary."""

from __future__ import annotations

from uxon.tui.context import TuiContext


def _bare_ctx(**overrides) -> TuiContext:
    base = dict(
        sessions=[],
        total_cpu="",
        total_ram="",
        version="",
        cwd="",
        cwd_short="",
        new_project_root="",
        existing_projects=[],
    )
    base.update(overrides)
    return TuiContext(**base)


def test_availability_seed_copies_nonempty_mapping_in_both_directions() -> None:
    from uxon.tui.app import UxonApp

    seed = {"claude": "pending"}
    ctx = _bare_ctx(loading=True, agent_availability=seed)
    app = UxonApp(ctx, probe_agents=False)
    assert app.state.main is None
    assert app.state.agent_availability.value == {"claude": "pending"}
    seed["claude"] = "missing"
    seed["cursor"] = "ok"
    assert app.state.agent_availability.value == {"claude": "pending"}
    app.state.agent_availability.value["codex"] = "ok"
    assert seed == {"claude": "missing", "cursor": "ok"}
