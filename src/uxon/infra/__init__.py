# SPDX-License-Identifier: MIT
"""Impure adapter layer for uxon.

Thin adapters over external systems (subprocess, filesystem, network,
pwd/env). May import :mod:`uxon.domain` and :mod:`uxon.errors`; never
``app``, ``cli``, or ``tui``. Import the concrete leaf module directly
(``from uxon.infra.probes import probe_host``); this package intentionally
exposes **no** convenience re-exports so the import graph stays flat and
the CLI-startup latency invariant holds.
"""
