# uxon documentation

Two ways in. Pick whichever matches what you have in mind.

## By scenario — "I'm here, what should I read?"

| Your situation | Start here |
|---|---|
| One developer, one host | [Solo on a single host](scenarios/solo-1.md) |
| One developer, several hosts | [Solo on multiple hosts](scenarios/solo-n.md) |
| Several developers sharing a host | [Team on a single host](scenarios/team-1.md) |
| Several developers, several hosts | [Team on multiple hosts](scenarios/team-n.md) |

Every scenario page is a small hub: the tutorials you need, the
how-to guides that match the operational shape, the reference pages
to bookmark, and the explanation pages worth reading once.

## By task — "I want to do X"

This site follows the [Diátaxis](https://diataxis.fr) model. Four
kinds of pages, four different purposes:

- **[start/](start/) — tutorials.** Linear, copy-pasteable
  walkthroughs for someone who has nothing set up yet.
- **[guides/](guides/) — how-to recipes.** "I have a working
  setup, now I want to *do* something." Operate (onboard,
  incident, upgrade), harden, customise, debug.
- **[reference/](reference/) — reference.** Every command, every
  flag, every config key, every audit event. Look up, don't read.
- **[explain/](explain/) — explanation.** Why uxon's model looks
  the way it does — isolation, supervision, multi-host, audit.
  Read once, refer back rarely.

## Top-level entry points

- [README.md](../README.md) — short pitch, install one-liner, and
  a pointer to this site.
- [start/install.md](start/install.md) — pick a flavour
  (per-user / host-wide / bundled installer) and bring up `uxon`
  on a host.
- [reference/cli.md](reference/cli.md) — every `uxon` subcommand.
- [reference/configuration.md](reference/configuration.md) —
  every config key.
- [SECURITY.md](../SECURITY.md) — threat model + disclosure
  policy. Read once if you operate a team host; it lays out the
  three honest caveats around `tmux` read-write attach,
  SSH-agent forwarding, and secrets persisted to `<user>_agent`
  homes.
- [CHANGELOG.md](../CHANGELOG.md) — what changed between releases.
- [privacy.md](privacy.md) — what `uxon`'s audit channel records
  about each developer; for operators to share with their team.
