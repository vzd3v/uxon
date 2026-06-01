# SPDX-License-Identifier: MIT
"""Git-remote creation layer for uxon.

Profiles plus the GitHub-CLI and token backends behind the
``git_create_enabled`` flow. May import :mod:`uxon.domain`,
:mod:`uxon.infra`, :mod:`uxon.errors`, and each other; never ``app``,
``cli``, or ``tui``. Import the concrete leaf module directly
(``from uxon.gitremote.create import create_project_remote``); this package
intentionally exposes **no** convenience re-exports so the import graph
stays flat and the CLI-startup latency invariant holds.
"""
