# Rollback

Rollback changes application images and traffic only. It does not reverse
database migrations.

Select the previous recorded release:

```bash
ROLLBACK_SMOKE_URL=https://app.example.com \
scripts/rollback-production.sh previous
```

Or select an explicit release ID:

```bash
scripts/rollback-production.sh <release-id>
```

The script acquires the deployment lock, validates the target digests, starts
the target application in the inactive slot, waits for liveness/readiness/web
health, runs optional smoke tests, and records a rollback event. Current traffic
is not intentionally stopped before the target is healthy.

Automatic rollback is allowed only when the target application supports the
current Alembic revision. If the target is incompatible with the current
schema, stop and use a forward fix or the separately documented database
restore procedure. Destructive data migrations are never automatically
downgraded.

Keep failed releases until the observation and forensic-retention policy has
expired. Remove only release directories and metadata that are no longer
needed, never the Caddy certificate data volume or application backup data.

## Staging Rollback

Use the staging host wrapper so the environment-specific lock, state directory,
and evidence checks are applied:

```bash
scripts/run-on-deployment-host.sh rollback previous
```

Before a change, run the disposable fake-host rehearsal:

```bash
scripts/rehearse-staging-rollback.sh --fake-host
```

The rehearsal creates only temporary synthetic files and cannot contact a live
host or cloud service.
