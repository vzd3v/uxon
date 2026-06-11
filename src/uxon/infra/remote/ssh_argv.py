"""SSH argv template + builder for talking to one peer.

A true leaf: depends only on the stdlib, ``platformdirs``, and
``RemoteSessionPayload`` from :mod:`uxon.domain.wire_schema` (imported
for the type-only reference in docstrings — no runtime dep). Crucially
it does **not** import :class:`uxon.infra.remote_hosts.RemoteHost`:
:func:`build_peer_ssh_argv` takes the four primitive host fields it
needs (``command_template``, ``extra_ssh_options``, ``ssh_alias``,
``remote_uxon``) so that ``remote_hosts`` can import
:func:`validate_command_template` at module top without a cycle.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import platformdirs

# ── Argv template & fetch-argv builder ───────────────────────────────────

PLACEHOLDER_CLOSED_SET: frozenset[str] = frozenset(
    {
        "{ssh_alias}",
        "{remote_uxon}",
        "{connect_timeout}",
        "{ssh_control_dir}",
        "{remote_command}",
        "{ssh_control_persist_seconds}",
    }
)


def _ssh_control_dir() -> str:
    """Return the directory that holds uxon's ssh ``ControlPath`` sockets,
    creating it on demand with mode 0o700.

    Resolves to ``$XDG_CACHE_HOME/uxon`` (falling back to ``~/.cache/uxon``).
    OpenSSH refuses to bind a ControlMaster socket whose parent directory
    does not exist, so this helper *owns* the directory: callers receive a
    path that is guaranteed to be usable. Symmetrical to
    ``write_cached_snapshot``, which owns the on-disk snapshot dir.

    Idempotent — safe to call on every fetch / kill / attach. Mode 0o700
    keeps socket names invisible to other users on a shared host.
    """
    path = Path(platformdirs.user_cache_dir(appauthor=False)) / "uxon"
    # ``mkdir(mode=0o700)`` only applies to a freshly-created directory.
    # Force-apply the mode afterwards so a pre-existing world-readable
    # ``~/.cache/uxon`` is brought into line — best-effort: a read-only
    # filesystem or unwritable parent surfaces on the actual ssh bind
    # below, where the operator gets a real error message.
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return str(path)


def _default_template() -> list[str]:
    """The default SSH argv template.

    Tokens in ``{...}`` are placeholders resolved by :func:`_render_argv`.
    The template includes ``ControlMaster=auto`` so the second-and-later
    fetches against the same peer reuse a multiplexed session — first
    tick costs 200-500 ms (TCP+auth), warm ticks 5-20 ms.
    """
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ConnectTimeout={connect_timeout}",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPath={ssh_control_dir}/ssh-%C",
        "-o",
        "ControlPersist={ssh_control_persist_seconds}s",
        "{ssh_alias}",
        "{remote_command}",
    ]


def validate_command_template(template: list[str]) -> None:
    """Raise :class:`ValueError` if ``template`` contains unknown
    placeholders or violates the ``{remote_command}`` / ``{remote_uxon}``
    mutual-exclusion rule.

    Mutual exclusion: ``{remote_command}`` is the rendered
    ``"<remote_uxon> list ..."`` string; using both in the same template
    would produce two competing remote-shell invocations.
    """
    if not template:
        raise ValueError("command_template must be non-empty")
    seen: set[str] = set()
    for token in template:
        if not isinstance(token, str) or not token:
            raise ValueError(f"command_template tokens must be non-empty strings, got {token!r}")
        # Scan for placeholders in this token. We deliberately don't
        # support nested or repeated placeholders within a single token
        # — that's a code smell in argv construction.
        i = 0
        while i < len(token):
            start = token.find("{", i)
            if start == -1:
                break
            end = token.find("}", start)
            if end == -1:
                # Lone "{" without "}" — treat as literal, skip.
                break
            placeholder = token[start : end + 1]
            seen.add(placeholder)
            if placeholder not in PLACEHOLDER_CLOSED_SET:
                raise ValueError(
                    f"command_template contains unknown placeholder {placeholder!r}; "
                    f"valid placeholders are {sorted(PLACEHOLDER_CLOSED_SET)}"
                )
            i = end + 1
    if "{remote_command}" in seen and "{remote_uxon}" in seen:
        raise ValueError(
            "command_template uses both {remote_command} and {remote_uxon} — "
            "they are mutually exclusive ({remote_command} already includes "
            "the rendered remote uxon invocation)"
        )


def _render_argv(
    template: list[str],
    *,
    ssh_alias: str,
    remote_uxon: str,
    connect_timeout: int,
    ssh_control_dir: str,
    remote_command: str,
    ssh_control_persist_seconds: int = 300,
) -> list[str]:
    """Substitute placeholders in ``template``. Empty tokens after
    substitution are dropped (e.g. an empty extra-options list).
    """
    mapping = {
        "{ssh_alias}": ssh_alias,
        "{remote_uxon}": remote_uxon,
        "{connect_timeout}": str(connect_timeout),
        "{ssh_control_dir}": ssh_control_dir,
        "{remote_command}": remote_command,
        "{ssh_control_persist_seconds}": str(ssh_control_persist_seconds),
    }
    rendered: list[str] = []
    for token in template:
        out = token
        for placeholder, value in mapping.items():
            if placeholder in out:
                out = out.replace(placeholder, value)
        if out:
            rendered.append(out)
    return rendered


def _strip_multiplex(template: list[str]) -> list[str]:
    """Drop ``-o ControlMaster/ControlPath/ControlPersist`` token pairs.

    Operators that prohibit ControlPersist sockets (e.g. paranoid
    ProxyCommand topologies, fleet without writable XDG cache) set
    ``ssh_multiplex = "off"`` in config; we strip the three options
    from the default template at render time. No effect on a
    user-supplied ``command_template`` (operator owns that argv).
    """
    out: list[str] = []
    i = 0
    while i < len(template):
        tok = template[i]
        nxt = template[i + 1] if i + 1 < len(template) else ""
        if tok == "-o" and nxt.startswith(("ControlMaster=", "ControlPath=", "ControlPersist=")):
            i += 2
            continue
        out.append(tok)
        i += 1
    return out


def build_peer_ssh_argv(
    *,
    command_template: Sequence[str] | None,
    extra_ssh_options: Sequence[str],
    ssh_alias: str,
    remote_uxon: str,
    remote_command: str,
    allocate_tty: bool,
    connect_timeout: int,
    ssh_multiplex: str,
    ssh_control_persist_seconds: int = 300,
) -> list[str]:
    """Single source of truth for ssh-argv to one peer.

    Takes the four primitive ``RemoteHost`` fields directly
    (``command_template``, ``extra_ssh_options``, ``ssh_alias``,
    ``remote_uxon``) rather than a whole ``RemoteHost`` so this module
    stays a leaf — that decoupling is what lets ``remote_hosts`` import
    :func:`validate_command_template` from here without a cycle.

    Used by fetch (poller), kill, and attach paths so all three honour
    the host's ``command_template`` / ``extra_ssh_options`` and share
    the multiplexed ControlMaster started by the poller.

    ``allocate_tty=True`` inserts ``-tt`` immediately after the first
    token when the first token is ``"ssh"`` — interactive sessions
    (attach) need a forced PTY. Custom non-ssh templates (kubectl
    exec etc.) are left alone; the operator owns tty plumbing in
    their argv.

    Selection of template:
      - ``command_template`` set → render that directly. Operator
        owns the argv; ``extra_ssh_options`` and ``ssh_multiplex`` are
        ignored because both target the default ssh template.
      - Otherwise → start from :func:`_default_template`, optionally
        strip multiplex options, insert ``extra_ssh_options``
        before ``{ssh_alias}``.
    """
    if command_template:
        template: list[str] = list(command_template)
    else:
        template = _default_template()
        if ssh_multiplex == "off":
            template = _strip_multiplex(template)
        if extra_ssh_options:
            try:
                idx = template.index("{ssh_alias}")
            except ValueError:
                idx = len(template)
            template = template[:idx] + list(extra_ssh_options) + template[idx:]
    if allocate_tty and template and template[0] == "ssh":
        template = [template[0], "-tt", *template[1:]]
    # Resolve the ssh control-socket directory lazily: only call
    # :func:`_ssh_control_dir` (which has the side effect of mkdir'ing
    # the directory) when at least one rendered token actually needs
    # it. Keeps ``ssh_multiplex = "off"`` and custom command_templates
    # that don't use ControlMaster free of an unwanted ``~/.cache/uxon``.
    needs_control_dir = any("{ssh_control_dir}" in tok for tok in template)
    return _render_argv(
        template,
        ssh_alias=ssh_alias,
        remote_uxon=remote_uxon,
        connect_timeout=connect_timeout,
        ssh_control_dir=_ssh_control_dir() if needs_control_dir else "",
        remote_command=remote_command,
        ssh_control_persist_seconds=ssh_control_persist_seconds,
    )
