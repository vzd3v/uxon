# Solo on a single host — quickstart

Get `uxon` managing your agent sessions on one Linux box, in
about 10 minutes. Two flavours below: the simplest setup (agent
runs as you), and the recommended paired-account setup (agent
runs as a sandboxed `<user>-agent`).

## What you'll learn

- How to install `uxon` for one user.
- How to launch your first agent session and reattach to it
  later.
- When to upgrade to the paired-account setup, and how.

## What you'll need

- A Linux host with `tmux` and Python ≥ 3.11.
- One of `claude`, `codex`, or `cursor-agent` installed for your
  user.
- Optional but useful: `et` (Eternal Terminal) on the laptop —
  see [`docs/clients.md`](../clients.md).

## Simplest: agent runs as you

```bash
uv tool install uxon          # or: pipx install uxon
uxon                          # launch the TUI
```

That's it. The TUI shows three actions:

1. **New session in current folder** — runs the agent in `$PWD`.
2. **Create new project** — prompts for a name, creates
   `<new_project_root>/<name>`, launches the agent there.
3. **Open existing project** — picks a directory under
   `new_project_root` and launches there.

Before every launch the TUI asks whether to start in normal mode
or with `--dangerously-skip-permissions` ("yolo"). Say no the
first time around.

If you want `uxon new <name>` (project scaffolding), set up the
project root once:

```toml
# config/config.toml
allowed_roots    = ["~/projects"]
new_project_root = "~/projects"
```

`uxon run` and the TUI's "New session in current folder" need
nothing else — they gate on write access alone when
`allowed_roots` is empty.

To switch the default agent, add:

```toml
[agents]
enabled = ["claude", "codex"]
default = "claude"
```

## Recommended: paired account <a id="recommended-paired-account"></a>

Pair your shell user (say `wes`) with a low-privilege agent
account (`wes-agent`). The agent runs as `wes-agent` via
`sudo -iu`; your shell user stays the trust boundary that holds
your dotfiles, SSH keys, and credentials. A yolo-mode (`--mode yolo`)
run blasts `wes-agent`'s files, not yours.

One-time host setup:

```bash
sudo useradd -m -s /bin/bash wes-agent

# Allow your shell user to sudo into the agent account without a password:
echo 'wes ALL=(wes-agent) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/uxon-wes-agent
sudo chmod 440 /etc/sudoers.d/uxon-wes-agent

# Give wes-agent a workspace it owns:
sudo install -d -o wes-agent -g wes-agent /srv/projects
```

`config/config.toml`:

```toml
default_launch_mode = "fixed"
runtime_user        = "wes-agent"
session_users       = ["wes-agent"]
allowed_roots       = ["/srv/projects"]
new_project_root    = "/srv/projects"
```

Install the agent binary for `wes-agent` (claude / codex / cursor)
— `sudo -iu wes-agent` and run the agent's installer there. Then:

```bash
uxon                          # the TUI launches into the new setup
```

You'll see your sessions running as `wes-agent`. The TUI's
superuser block doesn't appear in solo because there's only one
launch user.

If the agent needs your SSH keys (e.g. to push to private repos),
forward them explicitly: `ssh -A` from your laptop, and ensure
`wes-agent` can read your `SSH_AUTH_SOCK` (group ACL, or set up
the agent forwarding inside the `sudo -iu` step).

## Daily flow

`uxon` (no args) — opens the TUI.

In the TUI:
- `↑` / `↓` / `←` / `→` to navigate, `Enter` to activate.
- `d` kills the highlighted session (with `kill` confirmation).
- `D` kills *all your sessions* (with `kill-all` confirmation).
- `v` toggles between `flat` (default) and `by_host` view.
- `s` (or `/`) summons the search bar; `Esc` clears the query.
- `q` quits. `Esc` is a scoped cancel and never quits.

When the launched session exits — or you `Ctrl-b d` to detach —
the TUI returns with a refreshed list. The same binary you
launched is the same binary you come back to.

Non-interactive equivalents:

```bash
uxon list                     # show your sessions
uxon attach myproj            # reattach by stem
uxon kill myproj              # kill one
uxon run -- --model haiku     # forward agent flags
uxon new mynew                # create + launch
```

Full CLI reference: [`reference/cli.md`](../reference/cli.md).

## Where next

- Add a second host: [`scenarios/solo-n.md`](../scenarios/solo-n.md).
- Switch default agent or auto-create GitHub repos:
  [`scenarios/solo-1.md`](../scenarios/solo-1.md) "Likely
  customisations".
- Optionally run the agent in a container:
  [`guides/customise/run-agents-in-a-container.md`](../guides/customise/run-agents-in-a-container.md)
  (and [`guides/harden/harden-a-container.md`](../guides/harden/harden-a-container.md)
  to lock it down).
- Understand the model:
  [`explain/isolation-model.md`](../explain/isolation-model.md).
