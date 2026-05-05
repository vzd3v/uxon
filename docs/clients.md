# Connecting to a uxon host

`uxon` keeps agent sessions alive on the host after the developer's
local terminal disconnects. That property is most useful when the
transport between the developer's laptop and the host also survives
ordinary network events: a closed laptop lid, a Wi-Fi to LTE
handover, a moved seat in an office, a hotel-network outage.

Bare `ssh` does not provide that. A dropped connection kills the
local tab; the server-side `tmux` survives, but the developer has
to re-`ssh` and re-`uxon attach`. With dozens of tabs and several
hosts, manual reconnects become operational overhead.

This page collects the client-side practices that make uxon's
persistence useful in daily work. None of them are uxon-specific.

## Eternal Terminal (`et`) — recommended

[Eternal Terminal](https://eternalterminal.dev/) (`et`) is an SSH
replacement for the *interactive* connection. It keeps a remote
session alive across network drops and IP changes; the local tab
does not die when the laptop sleeps. Authentication and the
underlying transport piggy-back on SSH and the user's
`~/.ssh/config`.

Install (`brew install MisterTea/et/et` on macOS,
`apt install et` on Debian/Ubuntu, packages for Fedora and Arch).
The host needs `etserver` running; the systemd unit ships with the
package.

Daily use looks like this:

```bash
et dev-ai-1
# … inside the et session …
uxon
```

Closing the laptop, switching to mobile internet, reconnecting an
hour later — the `et` tab reattaches the same session.

## Why `et` and not `mosh`

[`mosh`](https://mosh.org/) solves a similar problem in a
different way and is a reasonable choice for terminals that are
mostly shell. For long-running agent TUIs it is more awkward:

- `mosh` synchronises a screen state instead of streaming a
  byte-for-byte PTY. That model has historically had rough edges
  with full-screen TUIs that rely on alternate-screen,
  scrollback, and bracketed-paste — which is exactly what
  `claude`, `codex`, `cursor-agent`, and the uxon TUI itself
  use heavily.
- `mosh` does not preserve native scrollback into the host
  terminal — the local terminal emulator's scrollback ends at
  whatever `mosh` rendered.
- Local-echo prediction is a feature on bad networks and a
  source of small visual artefacts on good ones.

`et` keeps the byte stream as bare SSH would and adds reconnection
on top. For the uxon use case that is the right trade-off.

## `~/.ssh/config` — short aliases

Hosts that you connect to often go in `~/.ssh/config` once, with
short aliases. Avoids long `ssh -i ... -p ... user@host`
incantations and is the source of truth for `et` and any other
SSH-aware tool.

```
Host dev-ai-1
    HostName    1.2.3.4
    User        vasily
    Port        22
    IdentityFile ~/.ssh/id_ed25519_sk
    # Multiplex repeat connections — saves handshake round trips
    # for tools that poll (uxon's multi-host, scp, rsync, etc.):
    ControlMaster auto
    ControlPath  ~/.ssh/cm-%r@%h:%p
    ControlPersist 10m

Host dev-ai-2
    HostName    5.6.7.8
    User        vasily
    Port        22
    IdentityFile ~/.ssh/id_ed25519_sk
```

Connect with `et dev-ai-1` (or `ssh dev-ai-1`).

## SSH key hardening

uxon hosts hold the keys to the rest of the team's work — agent
sessions, project trees, dev credentials, occasionally `gh` /
`aws` tokens. The bar for the SSH key that opens that host should
be at least the bar for the key that opens GitHub.

Minimum: an SSH key with a strong passphrase. Better: a key whose
private half lives in hardware and is used only after a
biometric / touch confirmation.

- **macOS — Secure Enclave.** Tools like
  [Secretive](https://github.com/maxgoedjen/secretive) generate
  keys whose private half stays inside the Apple Secure Enclave.
  The key cannot be copied out as a file from `~/.ssh`. Each
  signature requires Touch ID. Configure as the `IdentityAgent`
  in `~/.ssh/config`; `et` and `ssh` use it transparently.
- **Linux / cross-platform — hardware security key.** A YubiKey
  (or equivalent FIDO2 device) holds the key on-device.
  `ssh-keygen -t ed25519-sk` generates a resident or non-resident
  key whose use requires touching the YubiKey. Works with
  OpenSSH 8.2+; the public key references the hardware token.
- **Windows — Windows Hello / hardware-backed keys.** PuTTY
  successors and OpenSSH-for-Windows can route through Windows
  Hello or a hardware token similarly. Use PowerShell 7 or a
  modern terminal (Windows Terminal, Wezterm, Alacritty); avoid
  the legacy `cmd.exe` shell.

A passphrase-only key on a laptop hard drive is the floor. If the
host is shared with other developers, raise it.

## Optional: fzf-driven host picker

When `~/.ssh/config` grows past 5–10 hosts, a small picker pays
for itself. Example for `zsh` on macOS:

```bash
# In ~/.zshrc:

# Hosts from ~/.ssh/config, ignoring wildcard entries:
_ssh_config_hosts() {
  awk '$1=="Host" && $2!~/[*?]/ {print $2}' ~/.ssh/config 2>/dev/null
}

# Tab-completion for ssh / scp / et — only configured hosts, no junk:
zstyle -e ':completion:*:(ssh|scp|sftp|mosh|et):*' hosts \
  'reply=(${(f)"$(_ssh_config_hosts)"})'
compdef _ssh et

# fzf picker — `ets` opens an interactive list:
ets() {
  command -v et  >/dev/null || { echo "et not installed"; return 1 }
  command -v fzf >/dev/null || { echo "fzf not installed"; return 1 }
  local h=$(_ssh_config_hosts | fzf --height 40% --reverse \
    --preview 'ssh -G {} | grep -E "^(hostname|user|port|proxyjump) "')
  [[ -n $h ]] && et "$h"
}
```

Then `ets` opens a fuzzy list of hosts with a preview of the
resolved SSH config. The same pattern translates to bash, fish,
and PowerShell.

## What this page does *not* cover

- VPN / WireGuard / Tailscale topology to reach the dev hosts —
  that is a network-design choice, not a uxon concern.
- Host-side hardening (PAM, fail2ban, port choice, auth methods)
  — see your distribution's hardening guide.
- Notification webhooks (Telegram / Slack) for long-running
  agents — those are configured per-agent in its hooks/skills,
  not in uxon.
