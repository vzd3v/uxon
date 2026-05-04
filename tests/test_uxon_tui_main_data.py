"""Unit tests for :class:`uxon.tui.main_data.MainData`.

Stage 8 commit 2: introduces :class:`MainData` as a read-only mirror
of the rebuild-derived fields on :class:`TuiContext`. The contract
this test file pins:

* ``MainData.from_context(ctx)`` reflects every rebuild-derived
  field as a structural snapshot — no aliasing of the source list.
* The dataclass is frozen and uses ``slots`` (no ``__dict__``).
* The ``loading`` field is **not** present (intentional — see plan).
* Two ``from_context`` calls on equivalent inputs produce equal but
  not identical objects (``MainData`` is value-semantic, not memoised).
"""

from __future__ import annotations

import dataclasses
import unittest

from uxon.tui.context import (
    LaunchRequest,
    ServerStatus,
    SudoCapability,
    TuiContext,
    TuiSession,
)
from uxon.tui.main_data import MainData


def _mk_ctx(**overrides) -> TuiContext:
    base = dict(
        sessions=[
            TuiSession(
                name="s1",
                short="s1",
                attached=False,
                pid="42",
                cpu="0",
                ram="0",
                created="now",
                last_activity="now",
                cmd="-",
                path="/srv/work",
                user="devagent",
            )
        ],
        total_cpu="13",
        total_ram="800M",
        version="0.0.0-test",
        cwd="/srv/work",
        cwd_short="work",
        new_project_root="/srv/work",
        existing_projects=[("proj-a", "1m"), ("proj-b", "5m")],
        server_status=ServerStatus(load="0.5"),
        cwd_writable=True,
        current_user="devagent",
        sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
        scope_skipped_users=("bob",),
        other_sessions=[],
        repo_config_writable=True,
        on_attach=lambda u, n: LaunchRequest(cmd=("/bin/true",), label="attach"),
    )
    base.update(overrides)
    return TuiContext(**base)


class FromContextTests(unittest.TestCase):
    def test_every_field_is_mirrored(self) -> None:
        ctx = _mk_ctx()
        md = MainData.from_context(ctx)
        self.assertEqual(md.sessions, tuple(ctx.sessions))
        self.assertEqual(md.other_sessions, ())
        self.assertIs(md.server_status, ctx.server_status)
        self.assertIs(md.sudo_caps, ctx.sudo_caps)
        self.assertEqual(md.scope_skipped_users, ("bob",))
        self.assertEqual(md.cwd, "/srv/work")
        self.assertEqual(md.cwd_short, "work")
        self.assertEqual(md.new_project_root, "/srv/work")
        self.assertEqual(md.existing_projects, (("proj-a", "1m"), ("proj-b", "5m")))
        self.assertEqual(md.total_cpu, "13")
        self.assertEqual(md.total_ram, "800M")
        self.assertEqual(md.version, "0.0.0-test")
        self.assertTrue(md.repo_config_writable)

    def test_sequences_are_tuples(self) -> None:
        ctx = _mk_ctx()
        md = MainData.from_context(ctx)
        self.assertIsInstance(md.sessions, tuple)
        self.assertIsInstance(md.other_sessions, tuple)
        self.assertIsInstance(md.scope_skipped_users, tuple)
        self.assertIsInstance(md.existing_projects, tuple)

    def test_no_aliasing_of_source_lists(self) -> None:
        ctx = _mk_ctx()
        md = MainData.from_context(ctx)
        # Mutating the source list does not leak into the snapshot.
        ctx.sessions.clear()
        self.assertEqual(len(md.sessions), 1)


class FrozenSlotsTests(unittest.TestCase):
    def test_frozen(self) -> None:
        md = MainData.from_context(_mk_ctx())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            md.cwd = "/tmp"  # type: ignore[misc]

    def test_no_dict(self) -> None:
        # ``slots=True`` removes ``__dict__`` — verify so a future
        # commit doesn't silently regress the memory-saving contract.
        md = MainData.from_context(_mk_ctx())
        self.assertFalse(hasattr(md, "__dict__"))

    def test_loading_is_not_a_field(self) -> None:
        # ``loading`` is intentionally absent — see plan §commit 2.
        # It is a property of the slot store ("nothing has landed
        # yet"), not of the rebuild output. Pinning this prevents a
        # well-meaning future commit from copy-pasting the field
        # onto MainData and obscuring the structural distinction.
        field_names = {f.name for f in dataclasses.fields(MainData)}
        self.assertNotIn("loading", field_names)


class EqualityTests(unittest.TestCase):
    def test_equal_inputs_produce_equal_outputs(self) -> None:
        a = MainData.from_context(_mk_ctx())
        b = MainData.from_context(_mk_ctx())
        self.assertEqual(a, b)
        # But not identical — MainData is value-semantic, not memoised.
        self.assertIsNot(a, b)


if __name__ == "__main__":
    unittest.main()
