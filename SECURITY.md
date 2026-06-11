# Security policy

## Supported versions

| Version | Status         |
|---------|----------------|
| 3.x     | Security fixes |
| < 3.0   | Unsupported    |

## Reporting a vulnerability

Please use GitHub's **"Report a vulnerability"** form on the
[Security tab](https://github.com/vzd3v/uxon/security)
of this repository, or email `vz@vz.team` with the subject
`uxon security:`.

Expected acknowledgement within 72 hours. Coordinated-disclosure
window is 30 days unless agreed otherwise. Please do not open
public issues for security reports.

## Threat model

uxon is a privileged orchestrator on a shared host. The trust
boundaries are:

1. **Caller → launch user.** uxon uses `sudo -iu <user>` to fork
   `tmux` and the agent binary as a different OS user. Authorisation
   is enforced by the operator's `sudoers` configuration; uxon never
   elevates beyond what `sudoers` already grants.

   The recommended team setup pairs each developer's shell user
   with a low-privilege launch user (`<user>_agent`). A team-lead
   grant of the form
   `lead ALL=(alice_agent,bob_agent) NOPASSWD: ALL` lets the lead
   attach to and reap Alice's and Bob's agent sessions without
   gaining the developers' shell-user identity. This is the
   "supervision without impersonation" property used throughout
   the docs. It has three honest caveats the operator should be
   aware of:

   - **`tmux attach` is read–write by default.** Once an operator
     attaches to a developer's running pane, keypresses are
     delivered to whatever process is in that pane. uxon does not
     enforce read-only attach. If a true read-only audit posture
     is required, attach with `tmux attach -r` (or wrap the
     superuser action in such an alias) and document that in the
     team's runbook.
   - **`ForwardAgent yes` widens the boundary.** If the developer
     ran the agent with SSH-agent forwarding into the
     `<user>_agent` account, the agent's process holds a live
     handle to the developer's SSH agent socket. An operator
     attached to that pane can use the agent socket to sign as the
     developer (without the private key ever being copied). The
     paired-account model protects the developer's SSH key on
     disk; it does not revoke ambient delegations the agent
     already holds. Avoid blanket `ForwardAgent yes`; forward only
     for the duration of operations that need it.
   - **Secrets stored inside the `<user>_agent` account are
     reachable.** Anything the agent has been handed and persisted
     to its home — long-lived `OPENAI_API_KEY`, `~/.aws/credentials`
     copied in for convenience, session tokens cached under
     `~/.claude/`, or `.env` files in the project tree — is
     readable by anyone who can `sudo -iu <user>_agent`. Use
     ephemeral credentials (`aws-vault`-shaped helpers, short-lived
     tokens, per-session secret managers) rather than long-lived
     keys in agent home directories.

   Note on the developer's own grant. The recommended
   `<dev> ALL=(<dev>_agent) NOPASSWD: ALL` lets the developer run
   *any* command as `<dev>_agent`, including launching `tmux`
   directly on the per-user socket outside `uxon`. A session
   started that way without `uxon`'s session-name prefix is not
   surfaced by the TUI's scanner. This is intentional: the
   developer is the trust root on the host — they already have a
   shell, and could run agents under their own shell user without
   `uxon` ever seeing them. The grant's job is to bound the
   *agent*'s blast radius (a yolo run cannot touch `~<dev>`),
   not to constrain the developer. A command whitelist would not
   reduce the developer's attack surface; it would break every
   time an agent binary, launcher, or wrapper changed. Lead-side
   visibility is anchored to the OS account, not to the `uxon`
   process: the lead's `ALL=(<dev>_agent)` grant lets them
   `sudo -niu <dev>_agent tmux ls` and attach to whatever is
   running, prefix-matching or not.

2. **Per-peer authority.** Cross-host operation does not delegate
   trust between peers. Each peer's `sudoers` is evaluated
   independently by that peer's own SSH daemon. A compromised
   operator SSH key on host A grants only what host A's `sudoers`
   grants on host A; it does not propagate to host B. There is no
   shared-secret handshake, no central authority, no certificate
   chain that uxon installs across the fleet. To revoke an
   operator's reach on host B, edit `sudoers` on host B; touching
   the central config or the operator's machine does not change
   what host B will accept.

3. **Allowed roots.** When `allowed_roots` is non-empty, sessions
   cannot be started outside those paths. When `allowed_roots` is
   empty (default), sessions can be started in any directory the
   launch user can write to. Operators who need a directory
   whitelist must set `allowed_roots` explicitly. New projects are
   created only under `new_project_root`, which itself must be
   inside an allowed root.

4. **Git remote profiles.** Repo creation is limited to the
   explicit `git_remote_profiles` whitelist. With `auth = "token"`,
   uxon reads the PAT from `token_file` (read by `creds_user`),
   holds it in memory only for the duration of the API call, never
   logs it, and never echoes it in `--dry-run` output.

5. **Config writes.** The TUI Settings screen rewrites
   `config/config.toml` in place via a `tomlkit` round-trip. If the
   file is not directly writable, uxon shells out to `sudo tee`.
   The new content is staged in a temporary file and then atomically
   replaced.

## Out of scope

- **Sandbox escape from inside the agent binary.** uxon does not
  constrain what `claude`, `codex`, or `cursor-agent` can do once
  launched. Anything the agent's OS account can do, the agent can
  do.
- **The operator's `sudoers` configuration.** A misconfigured
  `NOPASSWD: ALL` entry, or a `<lead> ALL=(<dev-shell-user>) NOPASSWD: ALL`
  that defeats the paired-account model, is the operator's
  responsibility.
- **Container / VM isolation between users.** uxon is a thin
  wrapper over `tmux` + `sudo` + `ssh`. It does not configure
  cgroups, AppArmor, seccomp, kernel namespaces, or per-UID
  network policies.
- **tmux configuration.** uxon can apply a small set of tmux `set`
  options (mouse, OSC-52 passthrough, extended keys,
  terminal-features) to the sessions it launches, layered on top of
  each launch user's own tmux config — off by default, enabled with
  `tmux.manage_options = true`. The option values come only from
  the resolved `config.toml` (the shipped defaults or the operator's
  override), and a rejected option fails the launch rather than
  starting a degraded session. uxon never edits the user's
  `~/.tmux.conf` or any file.
- **Centralised RBAC, SSO, or audit infrastructure.** uxon is the
  runtime layer beneath these — it emits structured audit events
  to the host's platform log channel (journald native or `/dev/log`
  syslog; see [`docs/audit-events.md`](docs/reference/audit-events.md)) and
  the host's own `sudo` trail covers cross-user invocations, but
  uxon is not a replacement for an enterprise audit pipeline.

## Why OS users instead of containers

`uxon` uses dedicated low-privilege Linux users (`<user>_agent`)
and `sudo -iu` rather than spinning up a container per agent
session. Docker / Podman are stronger isolation primitives, but on
a shared development host they add four kinds of operational cost:

- **Bind-mount UID mapping.** With naive `docker run` defaults,
  files created in the container come back owned by `root` (or
  whatever UID was baked in) on the host, breaking save-and-edit.
  Rootless Podman with the `:U` mount option, and rootless Docker
  via `subuid` / `subgid`, close this — at the cost of per-host
  setup that is itself non-trivial.
- **Networking.** Anything the agent talks to on `localhost` (a
  local DB, a model proxy, an internal service, an
  `mDNS` / `.local` host) needs `host.docker.internal`,
  `--network=host`, or explicit port plumbing. SSH-agent
  forwarding needs socket bind-mounts and breaks across
  reconnects.
- **Auth duplication.** `~/.claude`, `~/.gitconfig`, `~/.aws/`,
  `known_hosts`, SSH keys — each has to be passed through, or the
  container becomes a second place to re-auth every agent.
- **Per-image maintenance.** Tool updates → image rebuilds → push
  or share. For a team that just wants "Claude Code with the
  project's deps", this is extra operational work.

OS-user isolation removes those four at the cost of relying on
Linux user separation rather than container primitives:

- **Same kernel.** A kernel-level escape from inside the agent
  binary reaches the host. Containers narrow this surface via
  default seccomp / AppArmor profiles; `<user>_agent` does not.
- **Same network namespace.** The agent can reach `127.0.0.1`
  services on the host and scan the LAN. `iptables` / `nftables`
  rules per UID can mitigate, but uxon does not configure them.
- **Same `/proc`.** Without `hidepid=2` mounted on `/proc`, every
  user can see every other user's command lines and environments
  (not their memory).

The isolation `<user>_agent` actually provides is what regular
Linux UID separation provides: the agent cannot read files outside
its UID's reach, cannot signal another UID's processes, cannot
read another user's `~/.ssh/`. That is enough when the host's
threat model is "developers on this team, plus their agents
running yolo by accident". It is not enough when you do not trust
the developers logging into the box.

If you need stronger isolation than that, run uxon itself inside a
VM (or container) per team and keep the OS-user model inside it.
The layers compose.

## Hardening recommendations

- **Run agents as a dedicated, low-privilege OS user.** The
  paired-account pattern (`alice` shell user + `alice_agent`
  launch user) is the recommended team setup. See
  [`docs/reference/configuration.md`](docs/scenarios/team-1.md)
  for the full template.

- **Audit `sudo` invocations against `*_agent` accounts.** uxon's
  own audit channel (journald / syslog) records who attached, who
  killed, and what was launched at the application level — see
  [`docs/audit-events.md`](docs/reference/audit-events.md). For an
  authoritative OS-level record (and full keystroke I/O), pair it
  with `sudo`'s own log via the following in `/etc/sudoers.d/`
  alongside the operator grants:

  ```
  Defaults!UXON_AGENT_OPS log_input, log_output
  Cmnd_Alias UXON_AGENT_OPS = /usr/bin/tmux, /bin/bash, /bin/sh
  ```

  See [`examples/sudoers/audit.example`](examples/sudoers/audit.example)
  for a complete fragment, including I/O log path layout and
  retention notes. I/O logs land under
  `/var/log/sudo-io/<lead>/<timestamp>/` by default.

- **`hidepid=2` on `/proc`.** Without it, every OS user can read
  every other user's command lines and environment via `/proc`.
  Mount with `mount -o remount,hidepid=2,gid=<adm-group> /proc`
  (and persist via `/etc/fstab`).

- **`ControlMaster` for multi-host operators.** Without SSH
  connection multiplexing, every TUI refresh tick to a peer opens
  a fresh handshake — slow, noisy in the peer's `auth.log`. See
  [`docs/explain/multi-host-philosophy.md`](docs/explain/multi-host-philosophy.md)
  for the recommended `~/.ssh/config` snippet.

- **Do not store long-lived credentials in `<user>_agent` home.**
  Anyone who can `sudo -iu <user>_agent` reads them. Prefer
  ephemeral credentials (`aws-vault`-shaped helpers, short-lived
  tokens, OAuth refresh in a session-scoped agent).

- **Avoid blanket `ForwardAgent yes`.** Forward only for the
  duration of an operation that needs it; the SSH agent socket is
  a live delegation of the developer's identity into whatever the
  agent is running.

- **Keep `git_remote_profiles` short and explicit.** Prefer
  `auth = "gh"` (delegated to a logged-in `gh` CLI) over storing
  a long-lived PAT on disk.

- **Set `enable_all_users_list = false` unless multi-user
  inspection is genuinely required.** The cross-user list relies
  on `sudo -niu` probes; turning it off reduces probe traffic and
  the chance of accidental visibility.

- **Restrict write access to `config/config.toml` to
  administrators.** The TUI's `sudo tee` fallback is a
  convenience, not an authorisation model.
