# Add a developer to Uxon

This guide assumes the host follows [team bootstrap](../../start/team-1-bootstrap.md).
Provision the developer's shell account and paired launch account through your
normal account-management process. The examples use `maya` and `maya-agent`.

## Grant access to the launch account

For the built-in local execution backend, give `maya` passwordless access to
`maya-agent`, and add that launch account to the supervisor's existing grant.
Use the [bootstrap grant pattern](../../start/team-1-bootstrap.md#step-4--leads-supervision-grant).
A command execution backend needs equivalent authorization in its helper.

Give the launch account and developer access to their project directory using
the [shared-project layout](../harden/lay-out-shared-projects.md). Install and
authenticate the selected coding tool for the effective launch user, following
that tool's instructions. Uxon does not install coding tools.

## Update host configuration

Add the launch account to the existing `session_users` list and the caller
mapping to the existing table:

```toml
session_users = ["nadia-agent", "maya-agent"]

[launch_user_by_caller]
nadia = "nadia-agent"
maya = "maya-agent"
```

Retain the other users and mappings already on the host. In strict launch mode,
enable the intended profiles using the
[launch reference](../../reference/configuration.md#launch-table).

Add the new **controller** account `maya` to the host's launch-record control
group. The paired workload account `maya-agent` must not join that group.
Supervisors need the same protected store to remove sessions created by other
controllers, including direct workloads. Follow the
[launch-record storage contract](../../reference/configuration.md#launch-records);
a sudo grant alone is insufficient.

## Verify from each controller

After group-membership changes, reconnect as the developer:

```bash
ssh maya@host
uxon doctor
uxon
```

Launch a disposable session from the developer's project directory. From the
supervisor's own login, verify that it appears in `uxon list --all-users`,
attach, then remove that disposable session through Uxon. Check the reported
cleanup outcome. This verifies target-user access and shared-record access.

Restart an open dashboard after changing its user configuration. Repeat these
changes on each relevant host; Uxon does not propagate accounts or policy.
Share the [team guide](../../scenarios/team-1.md) and [privacy page](../../privacy.md).
