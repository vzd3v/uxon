# SPDX-License-Identifier: MIT
"""``ParsedArgs`` → ``app.*`` router.

Thin dispatch layer: each branch forwards a parsed command to the
matching use-case in :mod:`uxon.app`. No use-case bodies live here —
the ``list`` audit/scope logic that once sat inline in ``main`` now
lives in :func:`uxon.app.listing.do_list`. Imports of ``uxon.app.*``
are eager (they pull no ``textual`` / ``uxon.tui``), so this module
stays within the ``import uxon.cli`` latency budget.
"""

from __future__ import annotations

import os

import uxon.app.attach as attach_app
import uxon.app.doctor as doctor_app
import uxon.app.kill as kill_app
import uxon.app.listing as listing_app
import uxon.app.new as new_app
import uxon.app.run as run_app
from uxon.domain.args import ParsedArgs
from uxon.domain.authz import canonical
from uxon.domain.config import Config
from uxon.errors import fail


def dispatch(args: ParsedArgs, cfg: Config, caller_user: str, launch_user: str) -> int:
    """Route a substantive (non-version, non-interactive) action to its use-case."""
    if args.action == "doctor":
        return doctor_app.do_doctor(
            cfg,
            caller_user,
            launch_user,
            canonical(os.getcwd()),
            json_output=args.json_output,
            probe_remote=args.all_hosts,
        )
    if args.action == "run":
        return run_app.do_run(args, cfg, launch_user)
    if args.action == "list":
        return listing_app.do_list(args, cfg, launch_user)
    if args.action == "attach":
        return attach_app.do_attach(args, cfg, launch_user)
    if args.action == "kill":
        return kill_app.do_kill(args, cfg, launch_user)
    if args.action == "kill-all":
        return kill_app.do_kill_all(args, cfg, launch_user)
    if args.action == "new":
        return new_app.do_new(args, cfg, launch_user)
    fail(f"unsupported action: {args.action}")
    return 2
