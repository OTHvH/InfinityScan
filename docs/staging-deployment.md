# Staging Deployment

Staging is a disposable, production-like environment. It uses the same
digest-pinned API, web, Caddy, Compose, migration, and smoke-test paths as
production, but it has separate host paths, credentials, database, object
storage buckets, backup metadata, domain, and protected GitHub environment.

The staging workflow is dispatch-only:

```text
authorize -> fetch publication evidence -> verify exact release
          -> staging preflight -> deploy -> smoke -> publish bounded evidence
```

It never builds or publishes images and never provisions cloud resources.

## Required Isolation

Use `infra/.env.staging.example` as a checklist only. The protected `staging`
GitHub environment must provide the deployment-host transport and the host must
have:

- `/etc/infinityscan/staging.env`
- `/etc/infinityscan/staging-secrets/` with regular `0600` or `0400` files
- `/opt/infinityscan-staging` as the deployment root
- `/var/lib/infinityscan/staging/` as the state and lock root
- A staging-only PostgreSQL database and private object-storage buckets
- An explicit `staging` environment marker on the host

The staging environment must use `APP_ENV=production` and
`DEPLOYMENT_ENVIRONMENT=staging`. `S3_ENDPOINT_URL` must be HTTPS, and both
`S3_BUCKET` and `BACKUP_BUCKET` must be staging-named. Production domains,
credentials, host markers, buckets, and evidence are rejected by preflight.

## Selecting a Release

Start from a successful image-publication workflow run. Supply its exact run
ID, artifact name, and commit SHA to `.github/workflows/deploy-staging.yml`.
The workflow downloads `publication-evidence.json`, validates its repository,
SHA, release ID, image digests, platforms, signatures, provenance, SBOMs, and
scan results, then binds it to the release manifest. Image tags are never
resolved during staging deployment.

Use `dry_run=true` to authorize and run host preflight without changing
services. Use `rollback_rehearsal=true` to run the fake-host rehearsal after a
successful smoke test.

## Synthetic Fixture

Seed only the deterministic staging fixture. Supply the password through the
protected environment, never as a command-line argument:

```bash
export DEPLOYMENT_ENVIRONMENT=staging
export APP_ENV=production
export STAGING_SMOKE_PASSWORD='use-the-protected-value'
(cd api && .venv/bin/python -m tools.staging_fixture seed --confirm-staging)
```

The fixture creates one synthetic series, one chapter, one verified image
record, one smoke user, and a staging environment marker. It is idempotent and
contains no production data. Remove it after the rehearsal:

```bash
(cd api && .venv/bin/python -m tools.staging_fixture cleanup --confirm-staging)
```

Both commands require `DEPLOYMENT_ENVIRONMENT=staging`; production-mode
execution without `--confirm-staging` is refused.

## Rollback

Rollback changes staging images and traffic only. It does not downgrade
Alembic migrations:

```bash
DEPLOYMENT_ENVIRONMENT=staging \
scripts/run-on-deployment-host.sh rollback previous
```

The rollback path acquires the staging lock, validates the selected immutable
release and publication evidence, starts the target slot, waits for health, and
records a redacted state event. Use the bounded rehearsal before an operator
change:

```bash
scripts/rehearse-staging-rollback.sh --fake-host
```

## Evidence and Status

Collect only redacted deployment evidence. Do not collect environment files,
secret files, database URLs, cookies, tokens, signed URLs, or object contents:

```bash
scripts/run-on-deployment-host.sh status
scripts/run-on-deployment-host.sh collect-safe-evidence /tmp/staging-evidence
```

Retain the publication artifact and staging result for the configured evidence
retention period. Failed releases and rollback state remain available for
forensics until that period expires.
