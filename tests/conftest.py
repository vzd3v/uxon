# SPDX-License-Identifier: MIT
"""Test-suite global configuration.

Disables the audit channel by default so unit tests can exercise CLI
handlers without opening real ``AF_UNIX`` sockets to journald or
``/dev/log``.  ``tests/test_uxon_audit.py`` opts back in (and resets
state) per-test via its own ``_reset_audit_state`` helper.
"""

from __future__ import annotations

from unittest import mock

import pytest

from uxon import audit as _audit
from uxon import probes as _probes

# Fully-installed CATALOG, used as the autouse ``probe_host`` stub. Tests
# that exercise the install-gate path explicitly mock ``probe_host`` in
# their own scope (the inner ``with mock.patch`` shadows this fixture for
# the duration of that block).
_STUB_HOST_REPORT = _probes.HostReport(
    tmux=_probes.BinaryStatus("tmux", "/usr/bin/tmux", ""),
    agents={
        "claude": _probes.BinaryStatus("claude", "/usr/local/bin/claude", ""),
        "codex": _probes.BinaryStatus("codex", "/usr/local/bin/codex", ""),
        "cursor": _probes.BinaryStatus("cursor-agent", "/usr/local/bin/cursor-agent", ""),
    },
    launch_user="",
)


@pytest.fixture(autouse=True)
def _stub_probe_host_by_default(request: pytest.FixtureRequest):
    """Default ``probes.probe_host`` to a fully-installed CATALOG.

    ``resolve_agent_id`` install-gates the resolved agent against
    ``probe_host``; without this stub every unit test that drives a
    launch path would have to mock the probe itself. Tests in
    ``tests/test_uxon_probes.py`` exercise the probe internals
    directly and opt out so the real implementation runs.
    """
    if request.node.fspath.basename == "test_uxon_probes.py":
        yield
        return
    with mock.patch("uxon.probes.probe_host", return_value=_STUB_HOST_REPORT):
        yield


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
    _audit.sink = "none"
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
    _audit.sink = "none"
    _audit._initialized = False
