# Install

Install paths and the after-install checklist for a fresh Linux
host. Read this once when bootstrapping a new environment; for
daily use the [scenario hubs](../scenarios/solo-1.md) and
[`reference/cli.md`](../reference/cli.md) are the right entry
points.

## Requirements

- **Python ≥ 3.11** (stdlib `tomllib` is used for config reads).
- **`tmux`** on the host.
- **Linux.** The runtime assumes per-user `tmux` sockets and
  `sudo -iu` style cross-user invocation. macOS / WSL work for
  development but aren't supported targets.
- Runtime deps `textual >= 0.80, < 9` and `tomlkit` come in
  automatically through every install path below.
- **`gh` CLI** only when an `auth = "gh"` git-remote profile is
  configured.

## Two flavours

Pick by who owns the binary on the host:

- **Host-wide install (recommended on shared / team hosts)** —
  one root-owned `uxon` in `/usr/local/bin/`. Operator owns the
  version, the update path, and the install location; launch
  users can append audit events but cannot edit the binary or
  the trail. Prerequisite for the TUI's cross-user dashboard
  (combined with passwordless `sudo` to launch users plus a
  `session_users` list).
- **Per-user install** — each OS user manages their own copy.
  Independently versioned, no `sudo` needed, easy to uninstall.
  Suits a single-owner host where the developer is also the
  operator; on a multi-user box it weakens audit integrity (a
  user who can edit their own copy can change what it logs).

## Host-wide install

```bash
# Simple: pipx as a system installer (pipx 1.5+).
sudo pipx install --global uxon
# Updates: sudo pipx upgrade --global uxon
```

```bash
# Explicit: bundled installer. Useful for fleet rollout (Ansible /
# Puppet) and when ops conventions pin paths like /opt/uxon/venv.
git clone https://github.com/vzd3v/uxon.git
cd uxon
sudo python3 install/install_uxon.py \
  --repo-dir "$(pwd)" \
  --install-path /usr/local/bin/uxon
# (uses /opt/uxon/venv by default; override with --venv-dir; --dry-run
# to preview)
# Updates: re-run with --reinstall
```

Both isolate `uxon`'s Python deps in a dedicated venv and put the
console script on `PATH` via a `/usr/local/bin/uxon` shim. The
package files end up root-owned, which is what makes the audit
trail tamper-evident — `uxon` does **not** try to defend at
runtime against a launch user running their own copy; the
host-wide, root-owned install is what enforces the property.

`install/install_uxon.py` creates a venv at `--venv-dir` (default
`/opt/uxon/venv`), `pip install`s the package into it, and
symlinks `/opt/uxon/venv/bin/uxon` to `--install-path`.

If `uv` is available you can skip the script:

```bash
sudo uv tool install --force git+https://github.com/vzd3v/uxon.git@<tag>
# uv places the entrypoint in /root/.local/bin or similar; symlink as needed
```

**Don't** use `sudo pip install uxon` — it dumps `textual` /
`tomlkit` into the system `site-packages` and conflicts with the
distro's package manager (this is what PEP 668 protects against).

## Per-user install

```bash
# uv tool — fast, isolated CLI install.
uv tool install uxon

# pipx — equivalent. Same console-script entrypoint.
pipx install uxon

# pip --user — no isolation. See PEP 668 caveat below.
pip install --user uxon
```

Updates:

```bash
uv tool upgrade uxon
pipx upgrade uxon
pip install --user --upgrade uxon
```

### PEP 668 caveat

On Debian / Ubuntu / Fedora system Python, PEP 668 blocks
`pip install --user`; use `pipx` (recommended) or
`pip install --user --break-system-packages uxon` if you know what
you're doing. With your own Python (pyenv / asdf / uv-managed)
PEP 668 doesn't apply.

### Unreleased changes from `main`

```bash
uv tool install git+https://github.com/vzd3v/uxon.git
# or:  pipx install git+https://github.com/vzd3v/uxon.git
```

## After install

```bash
uxon                              # launch the TUI; it self-diagnoses
```

Optional — bootstrap an example config. The file ships as a working
"solo on a single host" config and works as-is; uncomment a
scenario block at the bottom for team / multi-host setups:

```bash
curl -fsSL https://raw.githubusercontent.com/vzd3v/uxon/main/config/config.example.toml \
  -o ./config.toml
```

You'll need at least one of `claude`, `codex`, or `cursor-agent`
installed for the launch user — see the agent table in
[`README.md`](../../README.md#supported-agents). The TUI
auto-detects newly-installed agents and offers a one-keypress
enable.

## Where to go next

- One developer, one host → [`scenarios/solo-1.md`](../scenarios/solo-1.md)
- One developer, several hosts → [`scenarios/solo-n.md`](../scenarios/solo-n.md)
- Several developers sharing a host → [`scenarios/team-1.md`](../scenarios/team-1.md)
- Several developers, several hosts → [`scenarios/team-n.md`](../scenarios/team-n.md)

## Client side

For the laptop / phone / tablet that connects to the host — and the
SSH practices that make `uxon`'s session persistence useful in daily
work — see [`docs/clients.md`](../clients.md). Short version:
prefer Eternal Terminal (`et`) over bare `ssh`; put hosts in
`~/.ssh/config`; use a hardware-protected SSH key.

## Audit channel

`uxon` emits audit events to journald (preferred) or `/dev/log`
(syslog fallback). Per-event schema in
[`reference/audit-events.md`](../reference/audit-events.md);
operational topology, queries, and central forwarding in
[`guides/operate/forward-audit-to-collector.md`](../guides/operate/forward-audit-to-collector.md).
