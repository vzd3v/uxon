"""Per-target sudo capability probe.

Returns a *capability set*: which subset of ``session_users`` the caller
can actually reach through its configured execution backend.

Design constraints:

- **One-shot.** Probing happens once at startup. New sudo grants are
  picked up by restarting ``uxon``. There is no daemon, no SIGHUP, no
  per-refresh re-probe.
- **Non-interactive.** Every probe uses ``sudo -n`` (no password
  prompt, no keyboard interaction). A 0.5s per-probe timeout bounds
  startup delay.
- **Parallel.** Up to 8 probes run concurrently via
  ``ThreadPoolExecutor``. With the 0.5s per-probe ceiling the
  worst-case total wall time for N candidates is
  ``ceil(N / 8) * 0.5s`` — ~1s for the typical N <= 16.
- **Self-exclusion.** ``reachable_users`` never contains the OS user
  the process is running as: that's just "me", not "another reachable
  user", and the caller filters self out before listing.
- **Fail-soft.** Any per-probe failure (timeout, OSError, non-zero
  exit) means *not reachable*. No retries, no error surface.

The result is consumed by ``tui.context_builder.build_tui_context``
(TUI: own / others' sessions block) and by the ``app`` use-cases
(``app.listing`` / ``app.doctor``) for ``list --all-users`` (CLI
parity), and indirectly by ``infra.remote.collector`` for cross-host
aggregation (each peer runs its own probe on its own caller).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from uxon.domain.config import Config
from uxon.domain.sudo import SudoCapability
from uxon.infra.execution import probe as probe_execution

__all__ = ["SudoCapability", "probe_sudo_capability"]


PROBE_TIMEOUT_SEC = 0.5
"""Per-probe timeout budget. A slow PAM module that takes longer
than this is treated as "not reachable" — startup must not block."""

MAX_WORKERS = 8
"""Upper bound on concurrent probes. Sudo doesn't share inter-process
state, so threads are fine here — we're spawning subprocesses and
``wait()``-ing on them. Eight covers the typical N <= 16 in two
batches; widening this is cheap if a deployment has many session
users, but the per-probe ceiling already bounds the total wall time."""


def _probe_one_user(cfg: Config, target: str) -> tuple[str, bool]:
    """Run the fixed target UID/GID probe once. Returns ``(target, ok)``."""
    try:
        result = probe_execution(cfg, target)
    except (subprocess.TimeoutExpired, OSError, SystemExit):
        return target, False
    return target, result.ok


def _self_user() -> str:
    """Return the OS user this process is running as.

    Imported locally to keep this module free of a ``cli`` dependency.
    """
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


def probe_sudo_capability(cfg: Config, candidates: Iterable[str]) -> SudoCapability:
    """Probe per-target execution reachability in parallel.

    ``candidates`` is the list of OS users to probe — typically
    ``cfg.session_users`` minus the caller. The caller's own username
    is filtered out defensively here too: ``sudo -n -H -u <self>`` succeeds
    trivially for everyone, and including self would inflate
    ``reachable_users`` with a meaningless entry the TUI then has to
    strip.

    Per-probe budget is :data:`PROBE_TIMEOUT_SEC` (0.5s); there is no
    override knob. The probe is invoked at most once per process at
    startup. Per-refresh re-probing is intentionally absent, so a tunable
    timeout has no caller today and offering one in the API would be
    misleading.

    Returns a :class:`SudoCapability` snapshot. The function never
    raises for a probe failure — failures map to "not reachable".
    """
    self_user = _self_user()
    unique_targets = []
    seen: set[str] = set()
    for u in candidates:
        if not u or u == self_user or u in seen:
            continue
        seen.add(u)
        unique_targets.append(u)

    reachable: set[str] = set()
    workers = min(MAX_WORKERS, len(unique_targets))
    if workers == 0:
        return SudoCapability()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        user_futures = [pool.submit(_probe_one_user, cfg, u) for u in unique_targets]
        for fut in user_futures:
            user, ok = fut.result()
            if ok:
                reachable.add(user)

    return SudoCapability(reachable_users=frozenset(reachable))
