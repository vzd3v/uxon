"""Multi-host: SSH-driven remote-snapshot collection, split by concern.

The package that was once a single 934-line collector module.
Six single-purpose modules, no re-exports here — import the concrete
symbol from its concrete module:

- :mod:`uxon.infra.remote.ssh_argv` — argv template + builder +
  ``validate_command_template`` (a true leaf: stdlib + ``platformdirs``
  + ``RemoteSessionPayload`` from ``domain.wire_schema`` only).
- :mod:`uxon.infra.remote.master_recovery` — ``recover_wedged_master``
  and its ``/proc`` + ``ssh -O exit`` helpers.
- :mod:`uxon.infra.remote.envelope` — ``parse_envelope`` (wire-shape
  validation, shared with ``infra.demo``).
- :mod:`uxon.infra.remote.cache` — on-disk snapshot cache read/write.
- :mod:`uxon.infra.remote.collector` — ``fetch_remote_snapshot``, the
  orchestrator that composes the other five.

The collector is the single point where the local uxon process talks
to a peer machine. It SSH-runs ``uxon list --json`` on the peer,
parses the wire-schema envelope, and returns an in-memory
:class:`uxon.domain.wire_schema.RemoteSnapshot`. On any failure it
returns a snapshot with ``error`` populated rather than raising.

Design constraints (from the multi-host spec):

- **Fail-soft.** A bad host must never raise into the TUI event loop
  or block another host's poll. Every error path returns a snapshot
  object instead of raising; the only exceptions that propagate are
  ``KeyboardInterrupt`` / ``SystemExit`` for Ctrl-C.
- **Cached on disk.** The last successful payload is written to
  ``${XDG_STATE_HOME:-~/.local/state}/uxon/remote/<name>.json`` so a
  brief outage doesn't blank the TUI table.
- **SSH config is the source of truth.** The collector passes the
  configured ``ssh_alias`` to ``ssh`` verbatim — port, user, identity,
  ProxyCommand all come from the operator's ``~/.ssh/config``.
- **No prompts.** ``BatchMode=yes`` forbids password prompts,
  keyboard-interactive, and host-key TOFU prompts.
"""
