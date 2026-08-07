# Operator Runbook

## Health

Use the public same-origin probes:

```bash
curl -fsS https://app.example.com/api/livez
curl -fsS https://app.example.com/api/readyz
curl -fsS https://app.example.com/api/health
```

`/health` is compatibility-only. `/livez` is process liveness. `/readyz` is
database, migration-head, and private object-storage readiness.

## Logs

Application logs are JSON and redacted by the API. Caddy emits JSON logs to the
container output. Docker's `json-file` driver rotates logs by size and count;
inspect with `docker compose logs` and do not copy secrets into diagnostics.

## Common failures

- Readiness database unavailable: verify the managed PostgreSQL endpoint, TLS
  mode, CA path, and pool timeout without printing `DATABASE_URL`.
- Readiness migration mismatch: compare the deployed release metadata with the
  sole Alembic head; run the one-shot migration image before promotion.
- Readiness storage unavailable: verify the HTTPS endpoint, region, bucket, and
  secret-file permissions. The API never creates a bucket.
- Web unhealthy: inspect Next.js logs and verify its private `API_INTERNAL_URL`.
- Caddy unhealthy: validate the Caddyfile and preserve the Caddy data volume.

## Backups

Require a recent verified encrypted database backup and object inventory before
production migrations. Application and backup buckets are separate private
stores. Use the existing backup tooling and never place age identities or
credentials in the Git checkout.

## Safety

Do not expose API or web host ports, add a Docker socket mount, enable debug
servers, provision cloud resources, or edit historical Alembic migrations as an
incident response shortcut.

## Staging Operations

Run the dispatch-only staging workflow with an exact publication run ID,
artifact name, and commit SHA. Use the protected staging environment, never
production credentials or buckets. Host preflight requires the staging marker,
isolated secret directory, HTTPS object storage, staging-named buckets, and
the exact publication evidence artifact.

The staging fixture is synthetic and deterministic:

```bash
(cd api && .venv/bin/python -m tools.staging_fixture seed --confirm-staging)
(cd api && .venv/bin/python -m tools.staging_fixture cleanup --confirm-staging)
```

Run `scripts/rehearse-staging-rollback.sh --fake-host` before exercising a
staging rollback. Collect only the redacted output from
`staging-status.py` and `collect-staging-evidence.sh`; never archive secret
files, environment files, cookies, tokens, signed URLs, or object contents.
