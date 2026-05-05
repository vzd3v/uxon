# SPDX-License-Identifier: MIT
"""Test-suite global configuration.

Disables the audit channel by default so unit tests can exercise CLI
handlers without opening real ``AF_UNIX`` sockets to journald or
``/dev/log``.  ``tests/test_uxon_audit.py`` opts back in (and resets
state) per-test via its own ``_reset_audit_state`` helper.
"""

from __future__ import annotations

import pytest

from uxon import audit as _audit


@pytest.fixture(autouse=True)
def _disable_audit_by_default(request: pytest.FixtureRequest):
    """Default to ``audit.enabled = False`` for every test.

    Tests that exercise the audit channel directly (under
    ``tests/test_uxon_audit.py``) reset the module state in their own
    ``setUp``; their explicit assignments override this fixture for the
    duration of those tests.
    """
    # Reset to first-call shape so cached sockets / prefixes from prior
    # tests do not bleed across.  ``enabled=False`` then short-circuits
    # ``audit()`` before any socket work runs.
    _audit.enabled = False
    _audit._initialized = False
    _audit._socket = None
    _audit._prefix = {}
    _audit._prefix_subcmd = ""
    _audit._correlation_id = None
    yield
    # Best-effort post-test teardown: close any socket the test opened
    # so resource warnings don't surface as PytestUnraisableExceptionWarning.
    sock = _audit._socket
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
    _audit._socket = None
    _audit._initialized = False
