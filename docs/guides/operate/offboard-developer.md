# Remove a developer from Uxon

Remove Uxon sessions and authority before retiring the associated accounts.
Coordinate shell access, project ownership and external credential revocation
through your organization's existing offboarding process.

## Stop the developer's sessions

On each relevant host, identify sessions belonging to the paired launch account:

```bash
uxon list --all-users --json |
  jq '.data.sessions[] | select(.user == "nadia-agent")'
```

Remove each intended session using its full name:

```bash
uxon kill --user nadia-agent uxon-project@claude --json --force
```

Check every result, including workload cleanup failures. The controller needs
the target execution grant and access to the
[protected launch record](../../reference/configuration.md#launch-records).
Do not delete accounts, grants or records while their sessions need cleanup.
A missing record is not proof that a workload has stopped.

`kill-all` covers only the current launch user, not every user on a host.

## Remove Uxon access

After cleanup succeeds:

1. Remove `nadia-agent` from `session_users` and remove Nadia's entry from
   `launch_user_by_caller`.
2. Remove the corresponding target-user execution grants, including the
   supervisor's grant if that launch account is being retired.
3. Remove the departing controller from the launch-record control group.
4. Review launch profiles pinned to either account and Git remote profiles
   whose `creds_user` names the departing account. Replace or disable them
   before allowing new launches.
5. Restart open dashboards and verify the remaining users' access.

Apply the same changes on each peer. Updating one host does not revoke access
on another.

## Review the audit record

Per-session operations name the launch account as target; the operator
performing the removal remains the actor:

```bash
journalctl SYSLOG_IDENTIFIER=uxon TARGET_USER=nadia-agent --since today
```

Bulk operations use `TARGET_USERS`; see the
[audit reference](../../reference/audit-events.md). Retention is controlled by
the host or collector. Removing configuration does not erase that history.

Use your normal account and credential lifecycle procedures for the remaining
work. Deleting an OS account does not revoke externally issued credentials.
