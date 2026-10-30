"""Active columns honor defaults, configured order, and runtime gates."""

from uxon.tui.dashboard.layout import LayoutFlags, build_active_columns


def test_default_columns_for_host_and_user_flags() -> None:
    for multi, cross, expected in (
        (False, False, ("name", "agent", "cpu", "ram", "new", "last")),
        (True, False, ("host", "name", "agent", "cpu", "ram", "new", "last")),
        (False, True, ("user", "name", "agent", "cpu", "ram", "new", "last")),
        (True, True, ("host", "user", "name", "agent", "cpu", "ram", "new", "last")),
    ):
        assert (
            tuple(
                c.id
                for c in build_active_columns(cfg_columns=None, flags=LayoutFlags(multi, cross))
            )
            == expected
        ), (multi, cross)


def test_configured_columns_gates_and_insertions() -> None:
    for requested, multi, cross, expected in (
        (
            (
                "path",
                "name",
                "cpu",
                "cmd",
                "pid",
                "wins",
                "ram",
                "last",
                "new",
                "agent",
                "host",
                "user",
            ),
            False,
            False,
            ("path", "name", "cpu", "cmd", "pid", "wins", "ram", "last", "new", "agent"),
        ),
        (("name", "bogus", "host", "user", "cpu"), True, True, ("name", "host", "user", "cpu")),
        (("name", "cpu", "cmd"), False, True, ("name", "user", "cpu", "cmd")),
        (("name", "cpu"), True, False, ("host", "name", "cpu")),
        (("name", "cpu"), True, True, ("host", "name", "user", "cpu")),
        (("user", "name", "cpu"), False, True, ("user", "name", "cpu")),
        (("name", "host", "cpu"), True, True, ("name", "host", "user", "cpu")),
        (("cpu", "cmd"), False, True, ("user", "cpu", "cmd")),
        ((), False, False, ()),
        ((), True, True, ("host", "user")),
    ):
        assert (
            tuple(
                c.id
                for c in build_active_columns(
                    cfg_columns=requested, flags=LayoutFlags(multi, cross)
                )
            )
            == expected
        ), (requested, multi, cross)
