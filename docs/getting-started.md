# Getting started

Install paths, first run, and the after-install checklist for a
fresh Linux host. Read this once when bootstrapping a new
environment; for daily use, the [README](../README.md) and
[`docs/configuration.md`](configuration.md) are the right entry
points.

`uxon` requires **Python 3.11+**, `tmux`, and Linux. Runtime
dependencies (`textual`, `tomlkit`) come in automatically via every
install path below.

Two install flavours, picked by who owns the binary on the host:

- **Host-wide install (recommended on shared / team hosts)** — one
  root-owned `uxon` in `/usr/local/bin/`. Operator owns the
  version, the update path, and the install location; launch users
  can append audit events but cannot edit the binary or the trail.
  Prerequisite for the TUI's cross-user dashboard (combined with
  passwordless `sudo` to launch users plus a `session_users` list).
- **Per-user install** — each OS user manages their own copy.
  Independently versioned, no `sudo` needed, easy to uninstall.
  Suits a single-owner host where the developer is also the
  operator; on a multi-user box it weakens audit integrity (a user
  who can edit their own copy can change what it logs).

## Host-wide install (recommended for shared / team hosts)

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
# (uses /opt/uxon/venv by default; override with --venv-dir)
# Updates: re-run with --reinstall
```

Both isolate `uxon`'s Python deps in a dedicated venv and put the
console script on `PATH` via a `/usr/local/bin/uxon` shim. The
package files end up root-owned, which is what makes the audit
trail tamper-evident — `uxon` does not try to defend at runtime
against a launch user running their own copy, so the host-wide,
root-owned install is what enforces the property.

**Don't** use `sudo pip install uxon` — it dumps `textual` /
`tomlkit` / etc. into the system Python `site-packages` and
conflicts with the distro's package manager (this is what PEP 668
protects against).

For multi-host rollout, JSON-rendered configs, and pinned refs,
see [`docs/deployment.md`](deployment.md).

## Per-user install

For solo / single-owner hosts where the developer is also the
operator. Each OS user runs one of these in their own account:

```bash
# uv tool — fast, isolated CLI install.
uv tool install uxon

# pipx — equivalent. Same console-script entrypoint.
pipx install uxon

# pip --user — no isolation. See PEP 668 caveat below.
pip install --user uxon
```

`uv` and `pipx` isolate `uxon` and its deps in a per-user venv;
`pip --user` puts them under `~/.local/` shared with anything else
installed that way. All three put a `uxon` console script on the
user's `PATH`.

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
curl -fsSL https://raw.githubusercontent.com/vzd3v/uxon/main/config/config.example.toml -o ./config.toml
```

For scriptable host inspection see
[`docs/cli.md`](cli.md#doctor).

You'll need at least one of the agent CLIs (`claude`, `codex`,
`cursor-agent`) installed for the launch user — see
[README §Supported agents](../README.md#supported-agents). The TUI
auto-detects newly-installed agents and offers a one-keypress
enable.

`uxon` emits audit events to the platform log channel (journald
native on systemd hosts, `/dev/log` syslog fallback). Per-event
schema in [`docs/audit-events.md`](audit-events.md); channel
topology and `journalctl` recipes in
[`docs/deployment.md`](deployment.md#audit-channel).

## Client side

For the laptop / phone / tablet that connects to the host — and
the SSH practices that make `uxon`'s session persistence useful in
daily work — see [`docs/clients.md`](clients.md). The short
version: prefer Eternal Terminal (`et`) over bare `ssh`; put hosts
in `~/.ssh/config`; use a hardware-protected SSH key.
