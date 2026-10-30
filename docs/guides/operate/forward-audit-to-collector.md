# Collect Uxon audit events

Uxon writes audit events to the host's platform log channel. Integrate it with
your existing collector; Uxon does not install a logging service.

## Verify the source

```bash
uxon doctor
journalctl SYSLOG_IDENTIFIER=uxon -n 5
```

Doctor identifies the active sink. With no available sink, events cannot be
retained. Resolve this through the host's logging configuration.

## Preserve the event fields

For native journald events, filter on `SYSLOG_IDENTIFIER=uxon` and preserve
structured fields. Uxon has no `uxon.service` unit to filter on. Retain host,
timestamp, event/outcome, process identity, target identity and correlation ID.

For the syslog fallback, extract the `@cee:` JSON payload. Its keys are
lowercase, such as `process_user`; native journald fields are uppercase.
See the [audit schema](../../reference/audit-events.md).

Configure transport security, access and retention using your collector's
documentation. Forwarding is optional; local queries remain useful.

## Query collected events

For a collector storing journals in `/var/log/journal/remote/`:

```bash
journalctl --directory=/var/log/journal/remote/ SYSLOG_IDENTIFIER=uxon CORRELATION_ID=<uuid>
```

Inspect non-successful operations in the local journal:

```bash
journalctl SYSLOG_IDENTIFIER=uxon -o json |
  jq -c 'select(.OUTCOME != "ok") | {host:.HOST, event:.EVENT, outcome:.OUTCOME, target:.TARGET_USER}'
```

Use your collector's equivalent query for fleet history. Correlation joins
operations; it does not authenticate an actor or guarantee both hosts retained
every event. Disclose collection using the [privacy page](../../privacy.md).
