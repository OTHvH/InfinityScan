# InfinityScan - Infrastructure and Deployment

Local Docker Compose stack for development. Runs PostgreSQL, the FastAPI API,
and the Next.js frontend. An optional MinIO profile provides local S3-compatible
object storage.

The production package is separate: use `docker-compose.production.yml` with
external managed PostgreSQL and private HTTPS S3-compatible storage. Production
Compose contains Caddy, web, API, and one-shot migration services only; it does
not contain PostgreSQL or MinIO. See [`docs/production-deployment.md`](../docs/production-deployment.md).

The staging package uses the same production Compose file with
[`infra/.env.staging.example`](.env.staging.example) as a placeholder-only
checklist. Staging requires separate host paths, secret files, PostgreSQL,
private buckets, domain, and deployment state; it does not reuse production
values or local MinIO.

## Prerequisites

- Docker Engine ≥ 24 and Docker Compose v2
- (Optional) `docker buildx` for cross-platform builds (e.g. building ARM64 on x86)

## Quick start

```bash
cd infra

# 1. Create your local environment file
cp .env.example .env
# This is a local-development file. Do not reuse its values in production.

# 2. Build images
docker compose build

# 3. Start PostgreSQL, run migrations, then start API + web
docker compose up -d db
docker compose run --rm migrate        # one-off Alembic migration
docker compose up -d api web

# Optional: start MinIO and explicitly create the configured private bucket
docker compose --profile storage up -d minio
docker compose --profile storage run --rm minio-setup

# 4. Check health
docker compose ps                      # all services should be "Up" or "healthy"
curl -s http://localhost:3000/api/health # compatibility check
curl -s http://localhost:3000/api/livez   # process liveness
curl -s http://localhost:3000/api/readyz  # dependency readiness
# Web UI: http://localhost:3000

# 5. Stop everything
docker compose down

# 6. Stop and delete all data (volume)
docker compose down -v
```

## Services

| Service    | Port  | Description                                     |
|------------|-------|-------------------------------------------------|
| `db`       | 5432  | PostgreSQL 16 (Alpine). Data persists in `postgres_data` volume. |
| `migrate`  | —     | One-off Alembic migration. Runs then exits.     |
| `api`      | 8000  | Loopback-only direct FastAPI listener. Starts after `db` is healthy and `migrate` succeeds. |
| `web`      | 3000  | Next.js standalone server. Starts only after `api` is healthy. |
| `minio`    | 9000/9001 | Optional private S3-compatible API and console (`storage` profile). |
| `minio-setup` | — | Explicit local bucket setup job (`storage` profile). |

## Migration handling

Migrations are **not** run inside the API container. Instead:

1. `docker compose up -d db` — starts PostgreSQL and waits for it to be healthy.
2. `docker compose run --rm migrate` — runs `alembic upgrade head` once.
3. `docker compose up -d api web` — starts the API (which now depends on `migrate` having completed).

This design prevents multiple API replicas from racing to run migrations.
If you scale the API (`--scale api=N`), migrations have already finished.

## API routing

Expose the Next.js service as the public origin. Browser code calls only the
same-origin `/api` prefix. The Next.js server proxies `/api/:path*` to
`API_INTERNAL_URL/:path*`, so FastAPI routes remain unprefixed (`/health`,
`/auth/*`, `/reader/*`, and so on).

Compose supplies `API_INTERNAL_URL=http://api:8000`. Outside development it is
required and must be an absolute HTTP(S) origin without credentials, a path,
query, or fragment. It is server-only: never place it in a `NEXT_PUBLIC_*`
variable or client bundle.

## Cross-platform builds

The Python 3.12, Node 24, PostgreSQL 16, and MinIO images support both
`linux/amd64` and `linux/arm64`. No `platform:` field is set in Compose, so
images build for the host architecture.

To build an ARM64 image on an x86 host (or vice versa):

```bash
# Ensure a buildx builder with QEMU is available
docker buildx create --use --name multiarch --driver docker-container
docker buildx inspect --bootstrap

# Build API for ARM64
docker buildx build --platform linux/arm64 -f api/Dockerfile -t infinityscan-api:arm64 api

# Build Web for ARM64
docker buildx build --platform linux/arm64 \
  --build-arg API_INTERNAL_URL=http://api:8000 \
  -f web/Dockerfile -t infinityscan-web:arm64 web
```

## Health and readiness checks

Every service defines a Docker health check:

| Service | Check method                     | Interval | Start period |
|---------|----------------------------------|----------|--------------|
| `db`    | `pg_isready`                     | 5s       | —            |
| `api`   | `GET /readyz` via Python urllib  | 30s      | 10s          |
| `web`   | `GET :3000` via Node.js          | 30s      | 15s          |

Services that depend on another service wait for it to be **healthy**
(not just started) before launching.

The API keeps `/health` as a shallow compatibility endpoint. Orchestration and
the future Caddy deployment must use `/livez` for process liveness and `/readyz`
for database, migration-head, and enabled object-storage readiness. `/readyz`
returns `503` with safe component states until every mandatory dependency is
ready; it never returns credentials, URLs, object keys, or provider error bodies.

Readiness uses bounded database and object-storage operations and a short cache
to prevent a monitoring loop from amplifying provider failures. It does not
create buckets, objects, or run integrity scans.

## Signal handling

All containers use `init: true` (tini as PID 1) so SIGTERM reaches
the application process directly. Uvicorn and Node.js both handle
SIGTERM for graceful shutdown.

## Environment variables

Use `.env.example` only to create the development `infra/.env`. The production
template, `.env.production.example`, contains deliberately unusable
angle-bracket placeholders and is a checklist rather than a credential file.

The most important variables are:

| Variable              | Where used | Notes                                           |
|-----------------------|------------|-------------------------------------------------|
| `POSTGRES_PASSWORD`   | db         | **Change this.**                                |
| `DATABASE_URL`        | api, migrate | Must match POSTGRES_* settings.               |
| `JWT_SECRET_KEY`, `CSRF_SECRET_KEY` | api | Development values may be synthetic; deployed values must be explicit. |
| `TRUSTED_HOSTS` | api | Host-header allowlist. Include `api` for the local Compose network. |
| `ALLOWED_ORIGINS` | api | Exact origins accepted for unsafe requests. |
| `CORS_ORIGINS` | api | Exact cross-origin clients; explicitly empty is valid for same-origin. |
| `API_INTERNAL_URL` | web server/build | Private FastAPI origin; Compose supplies it. Never expose it to client JS. |
| `OBJECT_STORAGE_ENABLED` | api | Enables private server-side object storage. Disabled by default. |
| `S3_*` | api, minio-setup | S3-compatible endpoint, credentials, bucket, timeouts, and presign settings. |
| `STORAGE_PUBLIC_BASE_URL` | api | Optional CDN base URL; presigned delivery remains the default. |

Secrets are `POSTGRES_PASSWORD` when Compose PostgreSQL is used,
`JWT_SECRET_KEY`, `CSRF_SECRET_KEY`, `S3_ACCESS_KEY_ID`,
`S3_SECRET_ACCESS_KEY`, and any backup-storage credentials. Keep them in the
protected runtime secret source only. `DATABASE_URL` is also sensitive because
it can contain a URL-encoded password and must never be logged. Hosts, bucket
names, regions, pool bounds, proxy CIDRs, logging mode, endpoint URLs, and
`MEDIA_CSP_ORIGINS` are non-secret configuration, although private endpoints
and bucket names should still not be exposed unnecessarily.

Application content storage and backup storage are separate private buckets.
The API uses only the application bucket; backup tooling receives its distinct
storage settings through its protected operator environment. No production
bucket, credential, domain, or certificate is created by this repository task.

### Database runtime settings

`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_SECONDS`,
`DB_POOL_RECYCLE_SECONDS`, `DB_CONNECT_TIMEOUT_SECONDS`, and
`DB_STATEMENT_TIMEOUT_MS` bound each API process's PostgreSQL usage. The
defaults are conservative for one small API container and must be included in
the database connection budget before scaling the API.

Production PostgreSQL requires `DB_SSLMODE=require`, `verify-ca`, or
`verify-full`; `DB_SSLROOTCERT` is optional for providers that need an explicit
CA path. SQLite tests and local development are not forced to use PostgreSQL
TLS. Database URLs are never logged.

### Proxy and logging settings

Forwarded headers are ignored unless `TRUST_FORWARDED_HEADERS=true` and every
immediate proxy CIDR is listed in `TRUSTED_PROXY_CIDRS`. The application parses
the chain from right to left and selects the first untrusted address. It does
not trust arbitrary client-supplied Cloudflare or `X-Forwarded-*` headers.

`LOG_FORMAT=json` enables redacted structured production logs. The logger never
emits cookies, authorization headers, JWTs, CSRF tokens, passwords, database
URLs, storage credentials, age identities, or presigned URLs. `LOG_FORMAT=text`
remains available for development.

Rate limiting uses the validated request client identity. The current limiter
is process-local and is valid only while one API process is deployed. Scaling
the API requires a shared limiter backend or an enforced trusted edge limiter;
that is a deployment invariant, not an application assumption.

### Production fail-fast checks

With `APP_ENV=production`, startup requires a parseable PostgreSQL
`DATABASE_URL` and a successful database readiness query. It also requires
distinct `JWT_SECRET_KEY` and `CSRF_SECRET_KEY` values of at least 32 bytes,
`COOKIE_SECURE=true`, and explicit `TRUSTED_HOSTS`, `ALLOWED_ORIGINS`, and
`CORS_ORIGINS`. Trusted hosts and allowed origins cannot be empty or wildcarded;
for a same-origin deployment, set `CORS_ORIGINS=` explicitly.

If `OBJECT_STORAGE_ENABLED=true`, all required S3 settings must be present and
the configured bucket must pass its startup health check. Production requires
object storage to be enabled, uses an explicit HTTPS `S3_ENDPOINT_URL`, and
requires an explicit `S3_REGION` (`auto` for R2; provider-specific elsewhere).
Production endpoints cannot contain
credentials, query strings, fragments, loopback hosts, or private IP literals.
The API does not create a production bucket.

### Same-origin production contract

The public ingress exposes only the Next.js service:

- `/` is served by Next.js.
- `/api/*` is forwarded unchanged to Next.js.
- Next.js rewrites `/api/*` to the private FastAPI origin and strips the prefix.
- FastAPI is not directly public.
- PostgreSQL and MinIO are not public; MinIO is local/test-only.

The application adds API security headers and the Next.js server emits a
configured CSP for same-origin resources and HTTPS media origins. It does not
enable HSTS while local HTTP development is active. HSTS belongs at the future
HTTPS Caddy layer.

### Immutable pins

Dockerfiles, Compose images, workflow service images, and `docker://` actions
are pinned by SHA-256 digest. Non-local GitHub Actions are pinned to full
40-character commit SHAs; adjacent comments retain the reviewed release
version. `scripts/check-workflows.py` enforces this policy. Because the current
`actions/checkout` and `actions/setup-node` releases use the Node 24 action
runtime, self-hosted Actions runners must be version `2.327.1` or newer.

Dependabot checks GitHub Actions weekly and Docker images monthly. To refresh a
pin, review the upstream release and provenance, update the readable tag/version
comment and immutable SHA or digest together, then run:

```bash
python scripts/check-workflows.py
actionlint .github/workflows/*.yml
scripts/verify-phase5.sh
```

## Production package

Production images are supplied through `API_IMAGE`, `WEB_IMAGE`, and
`CADDY_IMAGE`; each must be a GHCR reference with a full SHA-256 digest. The VM
does not build images. Only Caddy publishes ports 80 and 443. Web and API use
the internal `edge` and `backend` networks, and only the API/migration services
receive secret-file mounts.

Run `scripts/bootstrap-production-host.sh --dry-run` before host changes,
`scripts/validate-production-config.sh` before rendering, and
`scripts/smoke-deployment.sh` after promotion. Deployment and rollback use a
lock and retain safe release metadata under `/var/lib/infinityscan/releases`.
See [`docs/rollback.md`](../docs/rollback.md) and
[`docs/operator-runbook.md`](../docs/operator-runbook.md).

## Troubleshooting

**API won't start - "connection refused" on db**
PostgreSQL may still be starting. Check with `docker compose ps`. The API
depends on `db` being healthy, so it should not start until ready.

**Migration errors**
Ensure `DATABASE_URL` in `.env` matches `POSTGRES_USER`, `POSTGRES_PASSWORD`,
and `POSTGRES_DB`. Run `docker compose run --rm migrate` manually to see output.

**Permission errors on volumes**
The `postgres_data` volume is owned by the postgres container user. If you
switch users in `.env`, delete the volume first:
```bash
docker compose down -v
```
