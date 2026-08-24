# Team on a single host — bootstrap

End-to-end walkthrough for bringing up `uxon` on a shared Linux
box for several developers. Roughly an hour for the first host;
adding a developer afterwards is the
[onboarding runbook](../guides/operate/onboard-developer.md).

## What you'll learn

- The recommended per-caller paired-account setup
  (`nadia` + `nadia-agent`).
- Minimum sudoers grants for developers and for the lead.
- How the TUI's superuser block (cross-user dashboard) appears
  once `session_users` and passwordless sudo align.
- Two alternative caller-to-launch-user mappings if the
  recommended one doesn't fit.

## What you'll need

- Root or sudo on the host.
- A non-trivial host (≥ 32 GB RAM is comfortable for 3 devs;
  see [`explain/sizing-a-host.md`](../explain/sizing-a-host.md)).
- One or more agents (`claude`, `codex`, `cursor-agent`)
  installable for the launch users.

## Step 1 — Install host-wide

```bash
sudo pipx install --global uxon
# or: bundled installer
git clone https://github.com/vzd3v/uxon.git
cd uxon
sudo python3 install/install_uxon.py \
  --repo-dir "$(pwd)" \
  --install-path /usr/local/bin/uxon
```

Per-user install on a team host weakens audit integrity (a user
who can edit their own copy can change what it logs). Use the
host-wide path. Full options: [`start/install.md`](install.md).

## Step 2 — Create the project root

```bash
sudo install -d -o root -g devs /srv/projects
sudo chmod 2775 /srv/projects     # setgid: new files inherit `devs` group
```

(Adjust the group to whatever your team uses.) For richer ACL
schemes — per-developer subdirs, per-project group ownership —
see [`guides/harden/lay-out-shared-projects.md`](../guides/harden/lay-out-shared-projects.md).

## Step 3 — Per-developer accounts

For each developer (template):

```bash
sudo useradd -m -s /bin/bash nadia          # the developer
sudo useradd -m -s /bin/bash nadia-agent    # the paired agent account

# nadia -> nadia-agent (the developer can sudo into their own agent):
echo 'nadia ALL=(nadia-agent) NOPASSWD: ALL' \
  | sudo tee /etc/sudoers.d/uxon-nadia-agent
sudo chmod 440 /etc/sudoers.d/uxon-nadia-agent

# nadia-agent gets a writable subdir of the project root:
sudo install -d -o nadia-agent -g devs -m 2775 /srv/projects/nadia
```

The grant lets `nadia` become **`nadia-agent`**, not the other
way round. `nadia-agent` cannot impersonate `nadia`.

Install the agent binary for each `<user>-agent` — typically by
running `sudo -H -u nadia-agent -- /bin/bash` and then the agent's installer.
This is deliberately not a login shell; configure PATH explicitly or use an
absolute agent binary. The TUI auto-detects newly-installed agents and offers
one-keypress enable.

## Step 4 — Lead's supervision grant

For the team lead (and any other supervisor account):

```bash
echo 'lead ALL=(nadia-agent,liam-agent,ethan-agent) NOPASSWD: ALL' \
  | sudo tee /etc/sudoers.d/uxon-lead-supervisor
sudo chmod 440 /etc/sudoers.d/uxon-lead-supervisor
```

This is **supervision without impersonation** — the lead can
attach to and reap each developer's agent sessions but cannot
become the developer. Detailed property and three honest caveats:
[`explain/supervision-without-impersonation.md`](../explain/supervision-without-impersonation.md).

If you want lead-as-root supervision (sees every reachable user
in `session_users`), use the broader grant
`lead ALL=(ALL) NOPASSWD: ALL` — but understand that this also
lets the lead `sudo -H -u nadia --` and become the developer
(weakening the supervision-without-impersonation property). In
most teams the per-target grant is the right default.

## Step 5 — `config/config.toml`

```toml
default_launch_mode   = "fixed"
default_launch_user          = "team-agent"      # fallback for unmapped callers
session_users         = ["nadia-agent", "liam-agent", "ethan-agent"]
enable_all_users_list = true
allowed_roots         = ["/srv/projects"]
new_project_root      = "/srv/projects"

[launch_user_by_caller]
nadia = "nadia-agent"
liam   = "liam-agent"
ethan = "ethan-agent"

[launch]
enabled_profiles = ["claude"]
default_profile = "claude"
```

If you don't want a fallback launch user (i.e. unmapped callers
should fail outright), drop `default_launch_user` — `uxon` then refuses
to launch for callers that aren't in `[launch_user_by_caller]`.

## Step 6 — Verify

As a developer:

```bash
ssh nadia@host
uxon doctor                # caller=nadia, launch=nadia-agent
uxon                       # TUI opens, shows nadia's own sessions
# Launch one to verify:
uxon run                   # in some /srv/projects/nadia/repo
```

As the lead:

```bash
ssh lead@host
uxon doctor                # caller=lead, launch=lead (lead has no -agent)
uxon                       # TUI shows superuser block; (3/3 reachable)
# Press Enter on Nadia's session row -> attaches via sudo -n -H -u nadia-agent --.
```

If only some `session_users` are reachable, the section header
shows `(N/M users reachable)` — typical when the lead's grant
covers a subset and `session_users` lists more candidates. Add
to the lead's sudoers fragment to widen.

## Step 7 — Audit channel

The audit channel is on by default. Verify:

```bash
uxon doctor | grep audit
# audit:    enabled, sink=journald-native

journalctl SYSLOG_IDENTIFIER=uxon --since "5 minutes ago"
```

For shipping events to a central collector (recommended once you
have more than one host):
[`guides/operate/forward-audit-to-collector.md`](../guides/operate/forward-audit-to-collector.md).

For sharing what's recorded with the team:
[`privacy.md`](../privacy.md).

## Two alternative caller mappings

The walkthrough above is **mode (a)** — one paired account per
developer. Two alternatives:

**Mode (b) — shared low-priv account.** All agents run as
`team-agent`, regardless of who logged in. Useful when agents
need shared workspace / cache across developers.

```toml
default_launch_mode = "fixed"
default_launch_user        = "team-agent"
session_users       = ["team-agent"]
allowed_roots       = ["/srv/projects"]
new_project_root    = "/srv/projects"
```

Plus passwordless `sudo -H -u team-agent --` for every caller. Note:
every developer's agent shares one `~/.claude/`, one
`~/.gitconfig`, one session pool — a runaway agent affects
everybody.

**Mode (c) — each developer runs as themselves.** Simplest,
weakest sandbox. Each agent has the same trust as its caller.

```toml
default_launch_mode   = "caller"
session_users         = ["nadia", "liam", "ethan"]
enable_all_users_list = true
```

## Resolution order

For any of the three modes:

1. `launch_user_by_caller[<caller>]` if set;
2. else, `default_launch_mode = "caller"` → caller is the launch user;
3. else → `default_launch_user`.

## Where next

- Onboard the next developer:
  [`guides/operate/onboard-developer.md`](../guides/operate/onboard-developer.md).
- Apply per-UID resource limits so one runaway can't OOM the
  rest: [`guides/harden/apply-resource-limits.md`](../guides/harden/apply-resource-limits.md).
- Optionally run agents in a (rootless) container on top of the
  paired account:
  [`guides/customise/run-agents-in-a-container.md`](../guides/customise/run-agents-in-a-container.md),
  hardened per [`guides/harden/harden-a-container.md`](../guides/harden/harden-a-container.md).
- Add a second host:
  [`start/add-second-host.md`](add-second-host.md).
- Operate: [`scenarios/team-1.md`](../scenarios/team-1.md) lists
  every operations runbook.
