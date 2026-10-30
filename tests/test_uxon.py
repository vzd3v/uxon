import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uxon.cli as uxon_cli
from uxon.cli.main import do_interactive, main
from uxon.domain import session as domain_session
from uxon.domain.config import Config
from uxon.infra import (
    config_loader,
    sessions_probe,
)

UXON_PATH = Path(uxon_cli.__file__).resolve()


def _mk_session(
    name: str, path: str = "/srv/repos/x", agent: str = "claude", legacy: bool = False
) -> domain_session.SessionInfo:
    return domain_session.SessionInfo(
        user="u",
        name=name,
        attached="0",
        windows="1",
        created="-",
        last_attached="-",
        pane_pids=(),
        active_pid=None,
        active_cmd="",
        active_path=path,
        agent=agent,
        launch_record_verified=True,
        legacy=legacy,
    )


class SessionNamingTests(unittest.TestCase):
    """Tests for the uxon-<stem>@<profile> session naming scheme."""

    def test_parse_session_name_new(self) -> None:
        self.assertEqual(
            domain_session.parse_session_name("uxon-foo@codex"), ("foo", "codex", 1, False)
        )
        self.assertEqual(
            domain_session.parse_session_name("uxon-foo@codex-3"), ("foo", "codex", 3, False)
        )
        self.assertEqual(
            domain_session.parse_session_name("uxon-my-repo-branch@claude"),
            ("my-repo-branch", "claude", 1, False),
        )

    def test_parse_session_name_legacy_at_prefix(self) -> None:
        # ``ccw-`` sessions still parse when listed in ``legacy_prefixes`` and
        # are flagged ``legacy=True`` (default prefix is ``uxon-``).
        self.assertEqual(
            domain_session.parse_session_name("ccw-foo@codex", legacy_prefixes=("ccw-",)),
            ("foo", "codex", 1, True),
        )
        # When ``uxon-`` is the configured current prefix, the same-prefixed
        # name is not legacy.
        self.assertEqual(
            domain_session.parse_session_name("uxon-foo@codex", prefix="uxon-"),
            ("foo", "codex", 1, False),
        )
        # Without legacy_prefixes, a ccw- name does not match (default prefix
        # is uxon-).
        self.assertIsNone(domain_session.parse_session_name("ccw-foo@codex"))
        # And with a non-matching explicit prefix, uxon- is also unrecognised.
        self.assertIsNone(domain_session.parse_session_name("uxon-foo@codex", prefix="ccw-"))

    def test_parse_session_name_rejects_garbage(self) -> None:
        self.assertIsNone(domain_session.parse_session_name("random-x"))
        self.assertIsNone(domain_session.parse_session_name("uxon-foo"))  # missing @agent
        self.assertIsNone(
            domain_session.parse_session_name("cc-foo")
        )  # ancient format no longer recognised

    def test_candidate_session_name(self) -> None:
        self.assertEqual(
            domain_session.candidate_session_name("foo", 1, "cursor"), "uxon-foo@cursor"
        )
        self.assertEqual(
            domain_session.candidate_session_name("foo", 2, "cursor"), "uxon-foo@cursor-2"
        )

    def test_resolve_full_new(self) -> None:
        sessions = [_mk_session("uxon-foo@claude"), _mk_session("uxon-foo@codex", agent="codex")]
        self.assertEqual(
            sessions_probe.resolve_session("uxon-foo@codex", sessions, "uxon-").name,
            "uxon-foo@codex",
        )

    def test_resolve_suffixed_without_prefix(self) -> None:
        sessions = [_mk_session("uxon-foo@codex", agent="codex")]
        self.assertEqual(
            sessions_probe.resolve_session("foo@codex", sessions, "uxon-").name,
            "uxon-foo@codex",
        )

    def test_resolve_stem_unique(self) -> None:
        sessions = [_mk_session("uxon-foo@codex", agent="codex")]
        self.assertEqual(
            sessions_probe.resolve_session("foo", sessions, "uxon-").name,
            "uxon-foo@codex",
        )

    def test_resolve_stem_ambiguous(self) -> None:
        sessions = [_mk_session("uxon-foo@claude"), _mk_session("uxon-foo@codex", agent="codex")]
        with self.assertRaises(SystemExit):
            sessions_probe.resolve_session("foo", sessions, "uxon-")


class CliPreflightTests(unittest.TestCase):
    """Tests for CLI preflight probe in main()."""

    def test_ssh_environment_does_not_reclassify_denied_list(self) -> None:
        from uxon.infra import audit as uxon_audit

        recorded: list[tuple[str, dict]] = []

        def fake_audit(event: str, *, outcome: str = "ok", **fields: object) -> None:
            recorded.append((event, {"outcome": outcome, **fields}))

        cfg = Config(
            default_launch_user="",
            default_launch_mode="caller",
            enable_all_users_list=False,
            launch_user_by_caller={},
            session_users=[],
            allowed_roots=["/srv/repos"],
            session_prefix="uxon-",
            legacy_session_prefixes=(),
            new_project_root="/srv/repos",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/uxon-{user}.sock",
            tui_refresh_interval_seconds=2.0,
            git_create_enabled=False,
            git_remote_profiles=[],
        )

        with mock.patch("uxon.infra.probes.probe_host") as probe:
            mock_report = mock.MagicMock()
            mock_report.tmux.path = "/usr/bin/tmux"
            mock_report.agents = {"claude": mock.MagicMock(path=None)}
            probe.return_value = mock_report
            with (
                mock.patch.dict("os.environ", {"SSH_CONNECTION": "1.2.3.4 22 5.6.7.8 22"}),
                mock.patch("uxon.infra.config_loader.load_config", return_value=cfg),
                mock.patch.object(uxon_audit, "audit", side_effect=fake_audit),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                with self.assertRaises(SystemExit):
                    main(["list", "--all-users"])

        list_emits = [e for e in recorded if e[0] == "list.peek"]
        self.assertEqual(len(list_emits), 1)
        self.assertEqual(list_emits[0][0], "list.peek")
        self.assertEqual(list_emits[0][1]["outcome"], "denied")


class DoInteractiveTextualMissingTests(unittest.TestCase):
    """With textual unavailable, ``uxon`` (interactive) must print a single
    install hint on stderr, no traceback, and return 1."""

    def test_prints_install_hint_when_textual_missing(self) -> None:
        # Simulate a stripped install where ``uxon.tui`` (and its
        # textual dep) is unavailable. A ``sys.modules`` sentinel alone
        # is insufficient because the package may already be cached as
        # an attribute on ``uxon``; we also clear that attribute and
        # restore it on teardown.
        import uxon as uxon_pkg

        saved_uxon_tui_module = sys.modules.get("uxon.tui")
        saved_uxon_tui_attr = getattr(uxon_pkg, "tui", None)
        sys.modules["uxon.tui"] = None  # type: ignore[assignment]
        if hasattr(uxon_pkg, "tui"):
            delattr(uxon_pkg, "tui")
        try:
            with tempfile.TemporaryDirectory():
                cfg = config_loader.load_config()
                buf_err = io.StringIO()
                buf_out = io.StringIO()
                with (
                    mock.patch.object(sys, "stderr", buf_err),
                    mock.patch.object(sys, "stdout", buf_out),
                ):
                    rc = do_interactive(cfg, "nobody", "nobody")
                self.assertEqual(rc, 1)
                err_text = buf_err.getvalue()
                self.assertIn("Textual dependency", err_text)
                self.assertIn("uv tool install --reinstall uxon", err_text)
                self.assertIn("pipx reinstall uxon", err_text)
                self.assertIn("Import error:", err_text)
                self.assertNotIn("pip install --user textual", err_text)
                self.assertNotIn("Traceback", err_text)
        finally:
            if saved_uxon_tui_module is None:
                sys.modules.pop("uxon.tui", None)
            else:
                sys.modules["uxon.tui"] = saved_uxon_tui_module
            if saved_uxon_tui_attr is not None:
                uxon_pkg.tui = saved_uxon_tui_attr  # type: ignore[attr-defined]
