"""Multi-host: SSH-driven remote-snapshot collection, split by concern.

The package that was once a single 934-line collector module.
Four single-purpose modules, no re-exports here — import the concrete
symbol from its concrete module:

- :mod:`uxon.infra.remote.ssh_argv` — argv template + builder +
  ``validate_command_template`` (a true leaf: stdlib + ``platformdirs``
  + ``RemoteSessionPayload`` from ``domain.wire_schema`` only).
- :mod:`uxon.infra.remote.master_recovery` — ``recover_wedged_master``
  and its ``/proc`` + ``ssh -O exit`` helpers.
- :mod:`uxon.infra.remote.envelope` — ``decode_envelope`` (wire-shape
  validation, shared with ``infra.demo``).
- :mod:`uxon.infra.remote.collector` — ``fetch_remote_snapshot``, the
  orchestrator that composes the other three.

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
- **Live data only.** Session snapshots are not persisted. Failed polls
  expose diagnostics and empty data rather than a historical list.
- **SSH config is the source of truth.** The collector passes the
  configured ``ssh_alias`` to ``ssh`` verbatim — port, user, identity,
  ProxyCommand all come from the operator's ``~/.ssh/config``.
- **No prompts.** ``BatchMode=yes`` forbids password prompts,
  keyboard-interactive, and host-key TOFU prompts.
"""
