# SPDX-License-Identifier: MIT
"""uxon: readable wrapper for terminal AI coding agent sessions.

The ``cli`` package is ``parse → dispatch → main``:

- :mod:`uxon.cli.parsing` — impure argv parsing above ``domain.args``;
- :mod:`uxon.cli.dispatch` — ``ParsedArgs`` → ``app.*`` router;
- :mod:`uxon.cli.main` — the ``main()`` spine + ``do_interactive``.

Only :func:`main` is re-exported here — it backs the
``uxon = "uxon.cli:main"`` console-script and ``python -m uxon``. This
module (and the ``__init__ → main → dispatch → parsing`` load chain) MUST
stay free of ``uxon.tui`` / ``textual`` imports (latency invariant #7).
"""

from __future__ import annotations

from uxon.cli.main import main

__all__ = ["main"]
