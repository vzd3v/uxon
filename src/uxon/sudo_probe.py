"""Per-target sudo capability probe.

Returns a *capability set*: which subset of ``session_users`` the caller
can actually reach via ``sudo -niu <U> -- true``, plus a separate flag
for whether the caller has root NOPASSWD (used for settings-write
gating, where there's no per-user target).

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

The result is consumed by ``cli._build_tui_context`` (TUI: own /
others' sessions block) and by ``cli`` for ``list --all-users`` (CLI
parity), and indirectly by ``remote_collector`` for cross-host
aggregation (each peer runs its own probe on its own caller).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

# The dataclass itself lives in ``uxon.tui.context`` so the TUI module
# is importable without pulling in ``subprocess``. We re-export the
# same name here so call sites can import :class:`SudoCapability`
# from the natural place (next to the probe machinery).
from uxon.tui.context import SudoCapability

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


def _probe_one_user(target: str) -> tuple[str, bool]:
    """Run ``sudo -niu <target> -- true`` once. Returns (target, ok).

    ``-n`` makes sudo non-interactive (no prompt). ``-i`` runs the
    target's login shell environment, matching how the launch path
    actually invokes commands as that user — keeping probe semantics
    aligned with launch semantics. ``--`` terminates sudo's option
    parsing so a target user named ``-foo`` (improbable, but cheap to
    defend against) cannot be mistaken for a flag.
    """
    try:
        cp = subprocess.run(
            ["sudo", "-niu", target, "--", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        return target, False
    return target, cp.returncode == 0


def _probe_root() -> bool:
    """Run ``sudo -n true``. Returns True iff exit 0.

    Distinct from the per-target probe: this asks "can I run *anything*
    as root without a password?", which is the property the Settings
    screen needs to ``sudo tee`` a root-owned config file.
    """
    if os.geteuid() == 0:
        return True
    try:
        cp = subprocess.run(
            ["sudo", "-n", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return cp.returncode == 0


def _self_user() -> str:
    """Return the OS user this process is running as.

    Imported lazily / locally rather than via ``cli.process_user`` to
    keep this module free of any ``cli`` dependency (the spec mandates
    the inverse import direction).
    """
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


def probe_sudo_capability(candidates: Iterable[str]) -> SudoCapability:
    """Probe per-target sudo reachability + root NOPASSWD, in parallel.

    ``candidates`` is the list of OS users to probe — typically
    ``cfg.session_users`` minus the caller. The caller's own username
    is filtered out defensively here too: ``sudo -niu <self>`` succeeds
    trivially for everyone, and including self would inflate
    ``reachable_users`` with a meaningless entry the TUI then has to
    strip.

    Per-probe budget is :data:`PROBE_TIMEOUT_SEC` (0.5s); there is no
    override knob. The probe is invoked at most once per process at
    startup — the spec forbids per-refresh re-probing, so a tunable
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
    # Fan out: per-user probes + the root probe, all in one pool.
    # Pool size is bounded; if there are zero candidates we still
    # need the root probe — keep min workers at 1.
    workers = max(1, min(MAX_WORKERS, len(unique_targets) + 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        user_futures = [pool.submit(_probe_one_user, u) for u in unique_targets]
        root_future = pool.submit(_probe_root)
        for fut in user_futures:
            user, ok = fut.result()
            if ok:
                reachable.add(user)
        can_root = root_future.result()

    return SudoCapability(
        reachable_users=frozenset(reachable),
        can_root=can_root,
    )
