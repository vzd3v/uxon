# Security policy

## Supported versions

| Version | Status         |
|---------|----------------|
| 4.x     | Security fixes |
| < 4.0   | Unsupported    |

## Reporting a vulnerability

Please use GitHub's **"Report a vulnerability"** form on the
[Security tab](https://github.com/vzd3v/uxon/security)
of this repository, or email `vz@vz.team` with the subject
`uxon security:`.

Expected acknowledgement within 72 hours. Coordinated-disclosure
window is 30 days unless agreed otherwise. Please do not open
public issues for security reports.

## Threat model

uxon is a privileged orchestrator that pairs each developer with a
low-privilege launch user and runs the agent through a configured execution
backend across one or more Linux hosts. The built-in local backend uses
`sudo -H -u USER --` for cross-user commands. It is **orthogonal to how
the agent process itself is isolated**: OS-user pairing is the
default, and it composes with a containerised agent rather than
competing with one (see
[Why OS users, and where containers fit](#why-os-users-and-where-containers-fit)).
The trust boundaries are:

1. **Caller → launch user.** The built-in local backend uses
   `sudo -H -u <user> --` to fork `tmux` and the agent binary as a different
   OS user. A command backend delegates this to its operator-owned helper.
   uxon never grants authority beyond the configured OS mechanism.

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
     expose a backend-safe read-only attach. If a true read-only audit posture
     is required, keep attach access disabled; invoking tmux directly bypasses
     the selected execution backend and the uxon attach audit event.
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
     readable by anyone who can `sudo -H -u <user>_agent --`. Use
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
   visibility is anchored to uxon-managed sessions on the configured execution
   backend. The lead's target-user grant permits backend access. Removal through Uxon also
   requires the same protected record store as the launching controller.

2. **Per-peer authority.** Cross-host operation does not delegate
   authority from one peer to another. Every target host independently
   authenticates the arriving SSH credential and evaluates its local execution
   backend (including `sudoers` for the built-in local backend). Uxon installs
   no shared secret, central authority, or fleet certificate chain. A
   credential reused across the fleet can nevertheless reach every host that
   accepts it. Compromise of that private key or certificate is not confined
   to the host where it was
   discovered. Revoke it at every accepting host (or at the SSH CA/identity
   provider) and remove any corresponding local grants. Editing Uxon config or
   deleting the original private-key file does not invalidate a copied key.

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
   holds it in memory for the operation, never
   logs it, and never echoes it in `--dry-run` output.

5. **Config writes.** The TUI Settings screen is read-only for non-root
   processes. A root-run TUI stages a root-owned mode-`0644` sibling, fsyncs it,
   atomically replaces `/etc/uxon/config.toml`, and fsyncs the directory.
   Unprivileged operators use `uxon config render` to a user-owned temporary
   file, inspect it, then cross the privilege boundary explicitly with
   `sudo install -o root -g root -m 0644`.

6. **Launch records.** All managed session removal, including direct workloads, trusts a finalized controller-side
   record, not pane environment alone. The default record store is private to one
   controller (`0700`/`0600`). Multi-controller supervision requires an explicit
   root-owned `2770` directory whose control group contains controllers but no
   launch user; shared records are `0640` and directories with access ACLs are
   rejected. Records contain operational session, profile, runtime, and timestamp
   metadata. Enumeration removes absent-session records older than seven days
   from finalization, in bounded batches; this is not a maximum retention time.
   Machine-readable bulk-kill results keep the tmux action and post-kill
   runtime/record cleanup as separate closed fields, so cleanup failure cannot
   be mistaken for a failed kill or silently hidden.

## Out of scope

- **Sandbox escape from inside the agent binary.** uxon does not
  constrain what `claude`, `codex`, or `cursor-agent` can do once
  launched. Anything the agent's OS account can do, the agent can
  do.
- **The operator's `sudoers` configuration.** A misconfigured
  `NOPASSWD: ALL` entry, or a `<lead> ALL=(<dev-shell-user>) NOPASSWD: ALL`
  that defeats the paired-account model, is the operator's
  responsibility.
- **Namespace / container / VM isolation policy.** uxon can route the complete
  target-user command boundary through an operator-supplied `[execution]`
  command prefix and can prepend a `[runtimes.<id>]` workload prefix. It does
  not create or harden those boundaries, and does not configure cgroups,
  AppArmor, seccomp, kernel namespaces, or per-UID network policies
  — the namespace/runtime definition is the operator's to harden (see
  [Why OS users, and where containers fit](#why-os-users-and-where-containers-fit)).
  The fixed execution probe confirms the effective target UID/GID and supplementary
  groups; it does not
  certify helper hardening, boundary continuity, or lifecycle. uxon cannot
  migrate or drain an external boundary that the helper no longer reaches.
- **tmux configuration.** uxon can apply a small set of tmux `set`
  options (mouse, OSC-52 passthrough, extended keys,
  terminal-features) to the sessions it launches, layered on top of
  each launch user's own tmux config — off by default, enabled with
  `tmux.manage_options = true`. The option values come only from
  `/etc/uxon/config.toml`, and a rejected option fails the launch rather than
  starting a degraded session. uxon never edits the user's
  `~/.tmux.conf` or any file.
- **Centralised RBAC, SSO, or audit infrastructure.** uxon is the
  runtime layer beneath these — it emits structured audit events
  to the host's platform log channel (journald native or `/dev/log`
  syslog; see [`docs/audit-events.md`](docs/reference/audit-events.md)) and
  the host's own `sudo` trail covers cross-user invocations, but
  uxon is not a replacement for an enterprise audit pipeline.

## Why OS users, and where containers fit

Paired accounts provide ordinary Unix permission separation. Files readable or
writable by the workload account remain accessible regardless of which developer
owns them. Protect personal directories and explicitly delegated credentials
with appropriate permissions. Uxon does not impose resource, network or kernel
isolation merely by changing UID.

An execution backend can place all target-user commands, including the tmux
server, in an operator-managed boundary. A workload runtime can then wrap the
coding-tool process inside that boundary. See the
[isolation model](docs/explain/isolation-model.md).

### Containerising does not relocate the credential exposures

Bind-mounted credentials and delegated sockets remain usable by the workload.
A host-side path permission check does not prove that the container mapping is
writable or isolated. Test ownership in the actual runtime.

A rootful Docker daemon grants host-root authority to accounts controlling it;
this defeats paired-account containment. The maintained
[container integration](docs/guides/customise/run-agents-in-a-container.md) uses
rootless Docker with an explicit UID mapping. Podman requires its own reviewed
adapter, mapping and Compose provider; swapping a binary name is insufficient.

Keep definitions and control interfaces outside workload-writable paths.
Operator-provided runtime templates are trusted commands, not policies Uxon can
prove safe. Rootless operation does not compensate for broad host mounts or
exposed runtime sockets. Follow the [runtime boundary review](docs/guides/harden/harden-a-container.md)
and the runtime's own security documentation.

## Operational boundaries

Uxon records its own application operations, not all activity on the host.
Developers with shell or target-account access can perform operations outside
Uxon. Root-owned installation and logging protect stored history; they do not
turn the application log into a complete enforcement record.

Use the existing host controls for account lifecycle, resource limits, process
visibility, credential revocation and audit retention. Uxon does not configure
those controls. Telemetry probes run through the selected target-user execution
boundary; restrictive process visibility inside that boundary may limit metrics.

For product-specific setup, use [team bootstrap](docs/start/team-1-bootstrap.md),
[shared project access](docs/guides/harden/lay-out-shared-projects.md), and the
[launch-record contract](docs/reference/configuration.md#launch-records).
