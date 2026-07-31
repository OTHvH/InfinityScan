# InfinityScan - Infrastructure and Deployment

Local Docker Compose stack for development. Runs PostgreSQL, the FastAPI API,
and the Next.js frontend. An optional MinIO profile provides local S3-compatible
object storage.

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
curl -s http://localhost:3000/api/health # should return {"status":"ok"}
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

## Health checks

Every service defines a Docker health check:

| Service | Check method                     | Interval | Start period |
|---------|----------------------------------|----------|--------------|
| `db`    | `pg_isready`                     | 5s       | —            |
| `api`   | `GET /health` via Python urllib  | 30s      | 10s          |
| `web`   | `GET :3000` via Node.js          | 30s      | 15s          |

Services that depend on another service wait for it to be **healthy**
(not just started) before launching.

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

### Production fail-fast checks

With `APP_ENV=production`, startup requires a parseable PostgreSQL
`DATABASE_URL` and a successful database readiness query. It also requires
distinct `JWT_SECRET_KEY` and `CSRF_SECRET_KEY` values of at least 32 bytes,
`COOKIE_SECURE=true`, and explicit `TRUSTED_HOSTS`, `ALLOWED_ORIGINS`, and
`CORS_ORIGINS`. Trusted hosts and allowed origins cannot be empty or wildcarded;
for a same-origin deployment, set `CORS_ORIGINS=` explicitly.

If `OBJECT_STORAGE_ENABLED=true`, all required S3 settings must be present and
the configured bucket must pass its startup health check. Production R2 uses an
explicit `S3_ENDPOINT_URL` and `S3_REGION=auto`. The API does not create a
production bucket.

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
