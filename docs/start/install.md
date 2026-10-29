# Install

Install paths and the after-install checklist for a fresh Linux
host. Read this once when bootstrapping a new environment; for
daily use the [scenario hubs](../scenarios/solo-1.md) and
[`reference/cli.md`](../reference/cli.md) are the right entry
points.

## Requirements

- **Python ≥ 3.11** (stdlib `tomllib` is used for config reads).
- **`tmux` ≥ 3.6**, or a maintained distribution build backporting the
  child-status fix below; 3.8 is recommended.
- **Linux.** The runtime assumes per-user `tmux` sockets and
  `sudo -H -u USER --` style cross-user invocation. macOS / WSL work for
  development but aren't supported targets.
- Runtime deps, including the pinned Textual release and `tomlkit`, come in
  automatically through every install path below.
- **`gh` CLI** only when an `auth = "gh"` git-remote profile is
  configured.

## Tmux child-status caveat

Some tmux builds, including 3.4 with libutempter, can lose a child-status
notification: a failed pane remains dead but its exit status is empty. This is
[upstream issue 4559](https://github.com/tmux/tmux/issues/4559), fixed by
[commit fa5f3ce](https://github.com/tmux/tmux/commit/fa5f3cef3d651b0eb9abfa77fc37ccade81679b5).
Use a maintained distribution package containing that fix, such as 3.7c, or
upstream 3.8. The operational minimum reflects reliable child-status handling.
Integration CI uses
3.8 with libutempter enabled to exercise the affected branch. Libutempter is
optional tmux login accounting, not a Uxon dependency. Uxon does not work around
the defect by weakening exit-status checks.

## Two flavours

Pick by who owns the binary on the host:

- **Host-wide install (recommended on shared / team hosts)** —
  one root-owned `uxon` in `/usr/local/bin/`. Operator owns the
  version, the update path, and the install location; launch
  users can append audit events but cannot edit the binary or
  the trail. Cross-user access also needs execution grants and `session_users`;
  cross-controller removal needs a protected shared record store.
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

Both isolate Python dependencies and install a host-wide entrypoint. Root-owned
package files prevent launch users from editing that installation; they do not
prevent users from executing other programs or forging application messages.

`install/install_uxon.py` creates a venv at `--venv-dir` (default
`/opt/uxon/venv`), `pip install`s the package into it, and
symlinks `/opt/uxon/venv/bin/uxon` to `--install-path`.

## Per-user install

```bash
# uv tool — fast, isolated CLI install.
uv tool install uxon

# pipx — equivalent. Same console-script entrypoint.
pipx install uxon

```

Updates:

```bash
uv tool upgrade uxon
# or, for a pipx installation:
pipx upgrade uxon
```

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
  -o /tmp/uxon-config.toml
sudo install -d -o root -g root -m 0755 /etc/uxon
sudo install -o root -g root -m 0644 /tmp/uxon-config.toml \
  /etc/uxon/config.toml
```

You'll need at least one of `claude`, `codex`, or `cursor-agent`
installed for the launch user — see the agent table in
[`README.md`](../../README.md#supported-agents). Refresh the TUI after
installation; in strict mode the operator must add the corresponding launch
profile to `enabled_profiles`.

## Where to go next

- One developer, one host → [`scenarios/solo-1.md`](../scenarios/solo-1.md)
- One developer, several hosts → [`scenarios/solo-n.md`](../scenarios/solo-n.md)
- Several developers sharing a host → [`scenarios/team-1.md`](../scenarios/team-1.md)
- Several developers, several hosts → [`scenarios/team-n.md`](../scenarios/team-n.md)

## Client side

See [Connect to a Uxon host](../clients.md) for SSH entry and reconnection.

## Audit channel

`uxon` emits audit events to journald (preferred) or `/dev/log`
(syslog fallback). Per-event schema in
[`reference/audit-events.md`](../reference/audit-events.md);
operational topology, queries, and central forwarding in
[`guides/operate/forward-audit-to-collector.md`](../guides/operate/forward-audit-to-collector.md).
