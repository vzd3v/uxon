# Rotate credentials

Tokens, keys, and passwords get leaked, expired, or simply
rotated on a schedule. This page enumerates the credentials
`uxon` interacts with and how to rotate each without disrupting
live sessions.

## What credentials live where

| Credential | Where it lives | Used by | Rotation trigger |
|---|---|---|---|
| GitHub PAT (fine-grained) | `token_file` referenced from `[[git_remote_profiles]]` (`auth = "token"`) | `uxon new --git-remote <profile>` REST call | PAT expiry; suspected leak; `creds_user` change. |
| `gh auth` token | `~/.config/gh/hosts.yml` under `creds_user` | `uxon new --git-remote <profile>` (`auth = "gh"`) | PAT expiry; suspected leak; `creds_user` change. |
| Anthropic / OpenAI / etc. API keys | `~/.claude/`, `~/.config/openai/`, `.env` files in project trees | The agent binary itself, not `uxon` | Provider rotation policy; suspected leak; agent went rogue (see [`respond-to-rogue-agent.md`](respond-to-rogue-agent.md)). |
| SSH keys (developer → host) | `~/.ssh/` on developer's laptop, `~/.ssh/authorized_keys` on host | All transport (`ssh`, `et`, `uxon` aggregator polls) | Key compromise, hardware change, departing developer. |
| SSH keys (aggregator → peers) | `~/.ssh/` on aggregator | `[[remote_hosts]]` polling, `attach --host`, `kill --host` | Same as above. |
| Sudoers grants | `/etc/sudoers.d/uxon-*` | `uxon` cross-user actions | Onboarding / offboarding (see [`onboard-developer.md`](onboard-developer.md), [`offboard-developer.md`](offboard-developer.md)). |

## GitHub PAT rotation (`auth = "token"`)

```bash
# 1. Generate a new fine-grained PAT at GitHub.
#    Required scope: 'repo'. Optionally 'read:org' if your profile
#    targets an org owner.

# 2. Stage the new token under creds_user.
#    Find creds_user from the matching profile in config.toml:
PROFILE=acme-org
CREDS_USER=$(grep -A 4 "name *= *\"$PROFILE\"" /opt/uxon/checkout/config/config.toml \
             | awk -F'"' '/creds_user/ {print $2; exit}')
NEW_TOKEN=ghp_...
sudo -niu "$CREDS_USER" \
  bash -c "umask 077 && printf '%s\n' '$NEW_TOKEN' > ~/.secrets/uxon-${PROFILE}.token.new"

# 3. Atomically swap.
sudo -niu "$CREDS_USER" mv ~/.secrets/uxon-${PROFILE}.token.new \
                            ~/.secrets/uxon-${PROFILE}.token

# 4. Verify by dry-run.
uxon doctor
# Look for:  profile=acme-org  status=ok
# (or warn:no-token / warn:unreadable-token if step 3 missed)

# 5. Revoke the old PAT at GitHub.
```

`token_file` in the profile points at the absolute path. You can
swap content without restarting `uxon` — every `uxon new
--git-remote` reads the file fresh. No live sessions are
affected (the agent itself doesn't use this token; only the
project-create step does).

## `gh auth` token rotation (`auth = "gh"`)

```bash
sudo -iu <creds_user>
gh auth refresh         # interactive; or `gh auth logout && gh auth login`
exit

uxon doctor             # confirm profile shows ok
```

`gh auth refresh` rotates the OAuth token without a full
re-login if `gh` was set up with `--web`. For PAT-backed
`gh auth`, `gh auth login --with-token < new-token-file` does
the same.

## API keys for the agent itself

`uxon` does not store these. The agent CLI does — typically in
`~/.claude/`, `~/.config/openai/`, or environment variables
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) sourced from `.env`.

To rotate:

1. Issue the new key at the provider.
2. Update wherever the agent reads it (the agent's own login
   flow, an `.env` file in the project tree, a `direnv` setup).
3. Revoke the old key at the provider.

This is **per `<user>_agent` account, per host** — there is no
central rotation `uxon` can drive. For a team box with
3 developers × 5 hosts, plan for a 15-minute pass per provider.

The audit channel does not record the API key itself (sanitised
out of `flags`). It does record `cli.start` flags lists; if you
ever passed a key on the command line by mistake, grep
`flags` to spot it and rotate immediately.

## SSH key rotation

Owned by the OS / your laptop hardware, not by `uxon`. Patterns
that work well with `uxon`:

- **Hardware-backed keys** (Secretive on macOS, YubiKey on
  cross-platform). Key never leaves the device; rotation is
  re-issuing on a new device.
- **Per-host `IdentityFile` in `~/.ssh/config`.** Different
  keys for `dev-ai-1`, `dev-ai-2`, etc. — easier to revoke per
  host than to rotate one global key.
- **CA-signed SSH certificates.** Revocation at the CA is
  fleet-wide; ideal for team·N. Configure each peer's `sshd` to
  trust the CA. Outside `uxon`'s scope but the integration is
  invisible to it.

For client-side hardening overall see
[`docs/clients.md`](../../clients.md).

## Sudoers grant rotation

Adding / removing developers is
[`onboard-developer.md`](onboard-developer.md) /
[`offboard-developer.md`](offboard-developer.md). Rotating a
**lead** is similar: edit
`/etc/sudoers.d/uxon-lead-supervisor` to swap the principal
name, `visudo -c -f` to verify, done.

## Audit footprint

Every `uxon new --git-remote <profile>` emits a
`git.remote.create` audit event. The token is **not** in the
event; only the profile name, repo, `creds_user`, and rc.

Old-token usage attempts after revocation will land
`git.remote.create` with `outcome = error`. Spot them with:

```bash
journalctl SYSLOG_IDENTIFIER=uxon EVENT=git.remote.create \
  -o json | jq -c 'select(.OUTCOME == "error")'
```

## Common mistakes

- **Editing `token_file` in place with a text editor.** A
  partial write during the editor's save can leave the file
  empty for a window, breaking concurrent `uxon new` calls.
  Stage to a new file, swap with `mv` (atomic on the same
  filesystem).
- **Forgetting to revoke the old token at the provider.** The
  rotation is "issue new + use new" — the leak window stays
  open until the old is dead. Revoke last, after verifying the
  new works.
- **Storing the token under the wrong owner.** `token_file`
  must be readable by `creds_user`. `uxon` does **not** read it
  as the launch user; the read happens under `creds_user`.
- **Treating `gh auth login` as a rotation.** It's a re-login,
  which produces a new OAuth token, but the *previous* token
  isn't necessarily invalidated. Check `gh auth status` and
  revoke explicitly at GitHub if you need a guaranteed-dead old
  token.

## Related

- [`onboard-developer.md`](onboard-developer.md), [`offboard-developer.md`](offboard-developer.md) — when developer credentials enter / leave the host.
- [`respond-to-rogue-agent.md`](respond-to-rogue-agent.md) — the post-incident credential rotation.
- [`reference/configuration.md`](../../reference/configuration.md) — `[[git_remote_profiles]]` schema.
