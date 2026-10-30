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
- A protected store shared by controllers, separate from workload accounts.

## What you'll need

- Root or sudo on the host.
- Existing developer and supervisor shell accounts on the host.
- A terminal coding tool installable for the launch users, using a built-in
  preset or a [custom catalog entry](../reference/configuration.md#agentsid-catalog).

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
sudo groupadd -r devs
sudo install -d -o root -g devs -m 2750 /srv/projects
sudo install -d -o root -g devs -m 2770 /srv/projects/shared
sudo setfacl -d -m u::rwx,g::rwx,m::rwx,o::--- /srv/projects/shared
```

(Adjust the group to whatever your team uses.) For richer ACL
schemes — per-developer subdirs, per-project group ownership —
see [`guides/harden/lay-out-shared-projects.md`](../guides/harden/lay-out-shared-projects.md).

## Step 3 — Per-developer accounts

For each developer (template):

```bash
sudo useradd -m -s /bin/bash nadia-agent    # the paired agent account
sudo usermod -aG devs nadia
sudo usermod -aG devs nadia-agent

# nadia -> nadia-agent (the developer can sudo into their own agent):
echo 'nadia ALL=(nadia-agent) NOPASSWD: ALL' \
  | sudo tee /etc/sudoers.d/uxon-nadia-agent
sudo chmod 440 /etc/sudoers.d/uxon-nadia-agent
sudo visudo -c -f /etc/sudoers.d/uxon-nadia-agent

# nadia-agent gets a writable subdir of the project root:
sudo install -d -o nadia-agent -g devs -m 2750 /srv/projects/nadia
sudo setfacl -m u:nadia:rwx,g::r-x,m::rwx,o::--- /srv/projects/nadia
sudo setfacl -d -m u::rwx,u:nadia:rwx,g::r-x,m::rwx,o::--- /srv/projects/nadia
```

The grant lets `nadia` become **`nadia-agent`**, not the other
way round. `nadia-agent` cannot impersonate `nadia`. Repeat the account and
workspace steps for Liam and Ethan. Reconnect shell sessions after adding group
membership. Personal trees are peer-readable, not peer-writable; `/shared` is
intentionally collaborative.

Install the agent binary for each `<user>-agent` — typically by
running `sudo -H -u nadia-agent -- /bin/bash` and then the agent's installer.
This is deliberately not a login shell; configure PATH explicitly or use an
absolute agent binary. Refresh the TUI after installation; in strict mode the
operator must add the corresponding launch profile to `enabled_profiles`.

## Step 4 — Lead's supervision grant

For the team lead (and any other supervisor account):

```bash
sudo usermod -aG devs lead
sudo useradd -m -s /bin/bash lead-agent
sudo usermod -aG devs lead-agent
echo 'lead ALL=(lead-agent,nadia-agent,liam-agent,ethan-agent) NOPASSWD: ALL' \
  | sudo tee /etc/sudoers.d/uxon-lead-supervisor
sudo chmod 440 /etc/sudoers.d/uxon-lead-supervisor
sudo visudo -c -f /etc/sudoers.d/uxon-lead-supervisor
```

This is **supervision without impersonation** — the lead can
attach to and reap each developer's agent sessions but cannot
become the developer. Detailed property and three honest caveats:
[`explain/supervision-without-impersonation.md`](../explain/supervision-without-impersonation.md).

### Share protected launch records

The developer and lead are separate controllers. Provision a shared record
store before either launches a session. For a new store:

```bash
sudo groupadd -r uxon-controllers
for user in nadia liam ethan lead; do
  sudo usermod -aG uxon-controllers "$user"
done
sudo install -d -o root -g root -m 0755 /var/lib/uxon
sudo install -d -o root -g uxon-controllers -m 2770 /var/lib/uxon/launch-records
sudo getfacl /var/lib/uxon/launch-records
```

Reconnect controller logins after changing group membership. The store must
meet the [launch-record contract](../reference/configuration.md#launch-records),
including no access ACL and no launch accounts in its control group. Review
inherited ACLs instead of reusing an existing directory blindly. Private records
cannot authorize another controller's removal, even for direct workloads.

## Step 5 — `/etc/uxon/config.toml`

```toml
default_launch_mode   = "fixed"
default_launch_user = "lead-agent"
launch_record_dir = "/var/lib/uxon/launch-records"
session_users         = ["nadia-agent", "liam-agent", "ethan-agent"]
enable_all_users_list = true
allowed_roots         = ["/srv/projects"]
new_project_root      = "/srv/projects/shared"

[launch_user_by_caller]
nadia = "nadia-agent"
liam   = "liam-agent"
ethan = "ethan-agent"
lead = "lead-agent"

[launch]
enabled_profiles = ["claude"]
default_profile = "claude"
```

Each controller has a separate workload account, including the lead. Controller
accounts must not double as workload accounts in the shared-record control
group. Configure mappings before those accounts use Uxon.

"Create new project" uses the deliberately shared tree. For a personal project,
create its directory inside your own subtree, enter it, then use `uxon run` or
the TUI's current-folder action. The root directory is not writable by peers.

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
uxon doctor
uxon                       # inspect reachable launch users
# Press Enter on Nadia's session row -> attaches via sudo -n -H -u nadia-agent --.
```

Check reachability at the dashboard's Kill ALL row. From the lead's login,
remove one disposable session launched by a developer and verify the cleanup
result. This tests both the execution grant and shared-record authority.

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

## Where next

- Onboard the next developer:
  [`guides/operate/onboard-developer.md`](../guides/operate/onboard-developer.md).
- Optionally run agents in a (rootless) container on top of the
  paired account:
  [`guides/customise/run-agents-in-a-container.md`](../guides/customise/run-agents-in-a-container.md),
  hardened per [`guides/harden/harden-a-container.md`](../guides/harden/harden-a-container.md).
- Add a second host:
  [`start/add-second-host.md`](add-second-host.md).
- Operate: [`scenarios/team-1.md`](../scenarios/team-1.md) lists
  every operations runbook.
