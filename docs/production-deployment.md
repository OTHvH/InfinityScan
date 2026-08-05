# Production Deployment

This package targets one Linux VM and keeps PostgreSQL and object storage
external. The VM runs only Caddy, Next.js, FastAPI, and a one-shot migration
container. MinIO and PostgreSQL are development/test services only.

## Layout

```text
/opt/infinityscan/current -> /opt/infinityscan/releases/<release-id>
/opt/infinityscan/releases/<release-id>/infra/docker-compose.production.yml
/var/lib/infinityscan/releases/<release-id>.json
/etc/infinityscan/production.env
/etc/infinityscan/secrets/{database_url,jwt_secret_key,csrf_secret_key,s3_access_key_id,s3_secret_access_key}
```

Secret files must be outside Git, owned by root or the deployment operator, and
mode `0600` or `0400`. Secret paths must be regular files, not symlinks; the
application and deployment preflight reject symlinks. The web container
receives no backend secret.

## Preconditions

Install Docker Engine and the Compose plugin using the host distribution's
verified package mechanism. Do not install Docker with an unreviewed shell
pipeline. Run the host bootstrap in dry-run mode first:

```bash
scripts/bootstrap-production-host.sh --dry-run
```

The environment file must set `APP_ENV=production`, an application domain,
digest-pinned GHCR `API_IMAGE`, `WEB_IMAGE`, and `CADDY_IMAGE`, TLS PostgreSQL
settings, an HTTPS S3 endpoint, and `_FILE` secret variables.

Validate without starting services:

```bash
scripts/validate-production-config.sh /etc/infinityscan/production.env \
  /opt/infinityscan/current/infra/docker-compose.production.yml \
  /etc/infinityscan/secrets
```

Image references must be `ghcr.io/<owner>/<image>@sha256:<64-hex-digest>`.
Production Compose never builds images on the VM.

## Deployment

Run from the reviewed deployment package, not a mutable application checkout:

```bash
API_IMAGE='ghcr.io/example/infinityscan-api@sha256:<digest>' \
WEB_IMAGE='ghcr.io/example/infinityscan-web@sha256:<digest>' \
scripts/deploy-production.sh <release-id>
```

The script validates the host, environment, external secret files, image
references, TLS endpoint, lock, and backup policy before changing services. It
pulls exact digests, runs the same API image for migrations, waits for readiness,
and records safe release metadata. It never prints secret contents and never
downgrades a database.

The Compose network exposes only Caddy's host ports 80 and 443. Caddy sends `/`
and `/api/*` to Next.js; Next.js retains the existing `/api` rewrite to FastAPI.

## Blue/Green Operation

The deployment state records the intended active/inactive slot and image
digests. An operator or deployment wrapper starts the inactive app slot,
requires `/livez`, `/readyz`, and web health, runs smoke checks, then reloads
Caddy to the new upstream. The previous slot remains available through the
configured observation window. A failed post-switch smoke test must immediately
reload the prior Caddy upstream.

HSTS is intentionally not enabled by the application package until HTTPS is
confirmed. Enable a long-lived HSTS policy only as a reviewed Caddy change.

## Architecture

The API and web images are built for both `linux/amd64` and `linux/arm64`.
Initial VM deployment can therefore use ARM64 while staging may use AMD64.
Resource limits are conservative for a small VM and can be tuned in
`docker-compose.production.yml` after observing health, reader, and recovery
workloads.
