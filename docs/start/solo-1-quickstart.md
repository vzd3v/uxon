# Solo on a single host — quickstart

Get `uxon` managing your agent sessions on one Linux box, in
about 10 minutes. Two flavours below: the simplest setup (agent
runs as you), and the recommended paired-account setup (agent
runs as a sandboxed `<user>_agent`).

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

Pair your shell user (say `vz`) with a low-privilege agent
account (`vz_agent`). The agent runs as `vz_agent` via
`sudo -iu`; your shell user stays the trust boundary that holds
your dotfiles, SSH keys, and credentials. A yolo-mode (`--dsp`)
run blasts `vz_agent`'s files, not yours.

One-time host setup:

```bash
sudo useradd -m -s /bin/bash vz_agent

# Allow your shell user to sudo into the agent account without a password:
echo 'vz ALL=(vz_agent) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/uxon-vz-agent
sudo chmod 440 /etc/sudoers.d/uxon-vz-agent

# Give vz_agent a workspace it owns:
sudo install -d -o vz_agent -g vz_agent /srv/projects
```

`config/config.toml`:

```toml
default_launch_mode = "fixed"
runtime_user        = "vz_agent"
session_users       = ["vz_agent"]
allowed_roots       = ["/srv/projects"]
new_project_root    = "/srv/projects"
```

Install the agent binary for `vz_agent` (claude / codex / cursor)
— `sudo -iu vz_agent` and run the agent's installer there. Then:

```bash
uxon                          # the TUI launches into the new setup
```

You'll see your sessions running as `vz_agent`. The TUI's
superuser block doesn't appear in solo because there's only one
launch user.

If the agent needs your SSH keys (e.g. to push to private repos),
forward them explicitly: `ssh -A` from your laptop, and ensure
`vz_agent` can read your `SSH_AUTH_SOCK` (group ACL, or set up
the agent forwarding inside the `sudo -iu` step).

## Daily flow

`uxon` (no args) — opens the TUI.

In the TUI:
- `↑` / `↓` to navigate, `Enter` to activate.
- `1`–`9` to jump to an item by number.
- `d` kills the highlighted session (with `kill` confirmation).
- `D` kills *all your sessions* (with `kill-all` confirmation).
- `v` toggles between `by_host` and `flat` view (multi-host only).
- `/` focuses the search bar; `Esc` clears + blurs it.
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
- Understand the model:
  [`explain/isolation-model.md`](../explain/isolation-model.md).
