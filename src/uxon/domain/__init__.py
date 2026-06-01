# SPDX-License-Identifier: MIT
"""Pure domain layer for uxon.

Stdlib-only (plus :mod:`uxon.errors`). No subprocess, pwd, filesystem,
network, argparse, or textual. Import the concrete leaf module directly
(``from uxon.domain.session import slugify``); this package intentionally
exposes **no** convenience re-exports so the import graph stays flat and
the CLI-startup latency invariant holds.
"""
