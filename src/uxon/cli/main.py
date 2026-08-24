# SPDX-License-Identifier: MIT
"""``uxon`` entrypoint spine: parse → preflight → dispatch, plus ``do_interactive``.

This is the composition root. It keeps the ``uxon.tui`` and ``audit``
imports lazy so module-load of ``uxon.cli`` never pulls ``uxon.tui`` /
Textual (latency invariant #7); the two ``except ImportError`` guards
in :func:`do_interactive` implement the optional-textual UX and are
sanctioned.
"""

from __future__ import annotations

import os
import sys

import uxon.app.listing as listing_app
from uxon.cli.dispatch import dispatch
from uxon.cli.parsing import parse_args
from uxon.domain.authz import canonical
from uxon.domain.config import Config
from uxon.errors import eprint
from uxon.infra import config_loader, identity, version_probe

# The TUI context-builder lives in ``uxon.tui.context_builder`` and is
# imported lazily inside ``do_interactive`` so module-load of
# ``uxon.cli`` never pulls ``uxon.tui`` / Textual (~90 ms saved on
# ``uxon version`` / ``uxon list``; latency invariant #7).


def format_version() -> str:
    """Render the ``uxon version`` display string.

    Delegates to :func:`uxon.infra.version_probe.format_version`, which
    composes the impure git/FS readers with the pure
    :func:`uxon.domain.version.format_version` builder.
    """
    return version_probe.format_version()


def do_interactive(cfg: Config, caller_user: str, launch_user: str) -> int:
    try:
        from uxon import tui as uxon_tui
    except ImportError:
        try:
            from uxon.tui.hints import TEXTUAL_MISSING_HINT

            eprint(TEXTUAL_MISSING_HINT)
        except ImportError:
            eprint(
                "uxon: interactive mode requires the 'textual' package "
                "(pip install --user textual)."
            )
        return 1
    # Lazy import: the bridge pulls in Textual-adjacent tui modules, which
    # must stay out of ``import uxon.cli`` (latency invariant #7).
    from uxon.tui.context_builder import build_tui_context

    cwd = canonical(os.getcwd())
    # Hand the TUI a skeleton ctx so the first frame paints immediately;
    # the real ctx is loaded by a worker once the app is mounted.
    ctx = build_tui_context(cfg, caller_user, launch_user, cwd, skeleton=True)
    return uxon_tui.run(ctx)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = parse_args(argv)
    except SystemExit as ex:
        # argparse always raises SystemExit with an int (0 for --help,
        # 2 for parse errors); guard the typed-as-``str | int | None`` shape.
        return int(ex.code) if isinstance(ex.code, int) else (0 if ex.code is None else 2)
    from uxon.infra import audit as _audit

    try:
        cfg = config_loader.load_config(os.getcwd())
    except SystemExit as ex:
        # Bug 5 part 2 — convert config-load failure into an audit event.
        # The audit module's compile-time defaults (``enabled=True``,
        # ``syslog_facility="user"``) are what fires here; ``configure()``
        # has not run yet because ``config_loader.load_config`` is what feeds it.
        # Spec says ``error`` carries the first 256 chars of the error
        # text; ``fail()`` stashes the human-readable message on
        # ``ex.uxon_msg`` so we don't end up logging just the int exit
        # code (``str(SystemExit(1)) == "1"``).
        err_msg = getattr(ex, "uxon_msg", None) or str(ex.code)
        _audit.audit(
            "config.error",
            outcome="error",
            path=str(config_loader.repo_config_path()),
            error=err_msg[:256],
        )
        raise
    _audit.configure(
        enabled=cfg.audit_enabled,
        syslog_facility=cfg.audit_syslog_facility,
        subcmd=args.action,
    )
    caller_user = identity.resolve_caller_user()
    launch_user = identity.resolve_launch_user(cfg, caller_user)

    # CLI preflight: non-launch actions still use the caller-derived launch
    # user. ``run`` and ``new`` resolve launch profiles first, because a
    # profile may pin a different launch user and path policy must fail before
    # tmux/agent probes.
    if args.action in {"attach", "list", "kill", "kill-all"}:
        from uxon.infra import probes as uxon_probes

        report = uxon_probes.probe_host(cfg, launch_user, cfg.agents)
        if report.tmux.path is None:
            from uxon.errors import fail

            fail(f"tmux is not installed.\n{report.tmux.install_hint}", 1)
        # Stash the report on ``args`` for use-cases that can reuse it.
        args.host_report = report

    if args.action == "interactive":
        return do_interactive(cfg, caller_user, launch_user)
    if args.action == "version":
        if args.json_output:
            listing_app._emit_json("version", version_probe._version_data())
            return 0
        print(format_version())
        return 0
    # Emit ``cli.start`` *after* the ``version`` early-return (the version
    # subcommand is a no-op probe; we don't litter the audit trail with it)
    # but *before* the ``doctor`` early-return — ``uxon doctor`` is a
    # substantive operator gesture that belongs in the audit history.
    _audit.audit(
        "cli.start",
        flags=_audit._sanitize_flags(list(argv or [])),
        profiles_enabled=list(cfg.launch.effective_enabled_profiles),
        enable_all_users_list=cfg.enable_all_users_list,
        audit_enabled=True,
        allowed_roots_count=len(cfg.allowed_roots),
        remote_hosts_count=len(cfg.remote_hosts),
    )
    return dispatch(args, cfg, caller_user, launch_user)


if __name__ == "__main__":
    raise SystemExit(main())
