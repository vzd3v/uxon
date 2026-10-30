# SPDX-License-Identifier: MIT
"""Use-case layer for uxon.

One module per use-case; orchestrates :mod:`uxon.domain`,
:mod:`uxon.infra`, and :mod:`uxon.gitremote`. May import those and
:mod:`uxon.errors`; never ``cli``, ``tui``, or ``textual``. No argparse,
no Textual. Import concrete leaf modules directly; this package intentionally
exposes **no** convenience re-exports so the import graph stays flat and the
CLI-startup latency invariant holds.
"""
