# SPDX-License-Identifier: MIT
"""Test-suite global configuration.

Disables the audit channel by default so unit tests can exercise CLI
handlers without opening real ``AF_UNIX`` sockets to journald or
``/dev/log``. Audit delivery tests enable the channel explicitly; each test
restores the complete cached socket, identity, correlation and configuration state.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
from unittest import mock

import pytest

from uxon.domain.host_report import BinaryStatus, HostReport
from uxon.infra import audit as _audit


@pytest.fixture(scope="session", autouse=True)
def _install_loop_guard():
    """Install the event-loop blocking guard for the whole test session.

    Process-wide and a no-op off the loop, so non-Pilot tests are
    unaffected. Under a Textual ``Pilot`` (event loop on the test
    thread) any interactive action that still spawns a subprocess on
    the loop raises :class:`EventLoopBlockedError` at the spawn site —
    turning the keystroke-swallow class into a hard test failure.
    """
    from uxon.infra.loop_guard import install_subprocess_guard

    install_subprocess_guard()
    yield


@pytest.fixture(autouse=True)
def _isolate_operator_config(tmp_path):
    """Keep unit tests from reading the host's ``/etc/uxon/config.toml``."""
    with mock.patch("uxon.infra.config_loader.OPERATOR_CONFIG_PATH", tmp_path / "config.toml"):
        yield


@pytest.fixture(autouse=True)
def _stub_execution_identity_probe_by_default(request: pytest.FixtureRequest):
    """Keep generic launch tests focused; execution tests override this stub."""
    from uxon.infra.execution import ExecutionProbe

    if "real_execution" in request.fixturenames:
        yield
        return
    with mock.patch(
        "uxon.infra.execution.require_probe",
        return_value=ExecutionProbe(backend="local", ok=True),
    ):
        yield


@pytest.fixture(autouse=True)
def _synthetic_target_filesystem(request: pytest.FixtureRequest):
    """Emulate only the external JSON adapter in synthetic launch unit tests.

    These modules use fictional OS users and controller-owned temporary trees.
    Run the fixed probe against their fixture tree; execution-boundary tests and
    integration tests remain unmocked, including actual permission checks.
    """
    if "real_execution" in request.fixturenames or request.node.fspath.basename not in {
        "test_uxon.py",
        "test_uxon_runtime.py",
        "test_uxon_launch_profiles.py",
    }:
        yield
        return
    from uxon.infra import execution, path_probe

    original = execution.run_query

    def query(argv, **kwargs):
        if "uxon.infra.path_probe" not in argv:
            return original(argv, **kwargs)
        args = list(argv[argv.index("uxon.infra.path_probe") + 1 :])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = path_probe.main(args)
        return subprocess.CompletedProcess(argv, status, output.getvalue(), "")

    with mock.patch.object(execution, "run_query", side_effect=query):
        yield


@pytest.fixture(autouse=True)
def _stub_probe_host_by_default(request: pytest.FixtureRequest):
    """Default ``probes.probe_host`` to a fully-installed CATALOG.

    Launch-profile resolution install-gates the resolved agent against
    ``probe_host``; without this stub every unit test that drives a
    launch path would have to mock the probe itself. Tests in
    ``tests/test_uxon_probes.py`` exercise the probe internals
    directly and opt out so the real implementation runs.
    """
    if (
        "real_host_probes" in request.fixturenames
        or request.node.fspath.basename == "test_uxon_probes.py"
    ):
        yield
        return

    def report(_cfg, user, catalog):
        return HostReport(
            tmux=BinaryStatus("tmux", "/usr/bin/tmux", ""),
            agents={
                aid: BinaryStatus(aid, f"/usr/local/bin/{spec.binary}", spec.install_hint)
                for aid, spec in catalog.items()
            },
            launch_user=user,
        )

    with mock.patch("uxon.infra.probes.probe_host", side_effect=report):
        yield


@pytest.fixture
def real_execution():
    """Exercise execution attestation and filesystem adapters without defaults."""


@pytest.fixture
def real_host_probes():
    """Exercise host availability through the external command boundary."""


@pytest.fixture(autouse=True)
def _disable_audit_by_default(request: pytest.FixtureRequest):
    """Default to ``audit.enabled = False`` for every test.

    Audit delivery tests enable the channel explicitly. Restore all cached
    state and close any socket opened during the test at teardown.
    """
    names = (
        "enabled",
        "sink",
        "_initialized",
        "_socket",
        "_prefix",
        "_prefix_subcmd",
        "_syslog_facility_name",
        "_correlation_id",
    )
    saved = {name: getattr(_audit, name) for name in names}
    # Reset to first-call shape so cached sockets / prefixes from prior
    # tests do not bleed across.  ``enabled=False`` then short-circuits
    # ``audit()`` before any socket work runs.
    _audit.enabled = False
    _audit._initialized = False
    _audit._socket = None
    _audit.sink = "none"
    _audit._prefix = {}
    _audit._prefix_subcmd = ""
    _audit._syslog_facility_name = "user"
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
    for name, value in saved.items():
        setattr(_audit, name, value)
