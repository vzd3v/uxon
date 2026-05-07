# Solo on a single host

You're the only user of one Linux box (a daily-driver, a dev VM,
or a small VPS) and you'd like `uxon` to manage your agent
sessions on it.

## What you get

- A persistent TUI over `tmux` that survives terminal disconnects.
- One sortable dashboard listing every agent session on the host
  (yours).
- Optional confinement of agents to a low-privilege OS account
  (`<user>_agent`), so a yolo-mode (`--dsp`) run can damage only
  what that account can write to — not your `$HOME`, SSH keys,
  or credentials.

## Get started

1. **Install** — pick a flavour in [`start/install.md`](../start/install.md).
   Per-user `uv tool install uxon` is fine for solo.
2. **First session** — [`start/solo-1-quickstart.md`](../start/solo-1-quickstart.md)
   walks the simplest setup ("agent runs as you") in 10 minutes.
3. **Recommended hardening** — set up the paired `<user>_agent`
   account so yolo-mode is sandboxed; see
   [`start/solo-1-quickstart.md` § Recommended](../start/solo-1-quickstart.md#recommended-paired-account).

## Likely customisations

- **Switch the default agent** —
  [`guides/customise/switch-default-agent.md`](../guides/customise/switch-default-agent.md)
- **Auto-create a GitHub repo on `uxon new`** —
  [`guides/customise/configure-github-on-new-project.md`](../guides/customise/configure-github-on-new-project.md)
- **Pick a project-local override (`.uxon.toml`)** —
  [`guides/customise/template-uxon-toml.md`](../guides/customise/template-uxon-toml.md)
- **Tune dashboard refresh on a slow link** —
  [`guides/customise/tune-refresh-cadence.md`](../guides/customise/tune-refresh-cadence.md)

## Reference

- [`reference/cli.md`](../reference/cli.md) — every subcommand and flag.
- [`reference/configuration.md`](../reference/configuration.md) — every config key.
- [`reference/keybindings.md`](../reference/keybindings.md) — TUI keys.

## Worth understanding

- [`explain/isolation-model.md`](../explain/isolation-model.md)
  — why OS users instead of containers.
- [`explain/audit-channel-design.md`](../explain/audit-channel-design.md)
  — what `uxon` records and why (also relevant for solo, since the
  channel is on by default; you can disable it via `audit.enabled
  = false` if you don't need a personal trail).

## What you don't need (yet)

- Multi-host setup — when you add a second machine, see
  [`scenarios/solo-n.md`](solo-n.md).
- Cross-user supervision — the superuser block, `session_users`,
  `enable_all_users_list` are team features and stay invisible on
  a solo host.
- Operations runbooks under `guides/operate/` — those are written
  for team operators. Solo failure modes are smaller and the
  recovery is "delete the offending project and start over".
