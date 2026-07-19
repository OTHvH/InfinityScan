# InfinityScan — Infrastructure & Deployment

Local Docker Compose stack for development. Runs PostgreSQL, the FastAPI API,
and the Next.js frontend.

## Prerequisites

- Docker Engine ≥ 24 and Docker Compose v2
- (Optional) `docker buildx` for cross-platform builds (e.g. building ARM64 on x86)

## Quick start

```bash
cd infra

# 1. Create your local environment file
cp .env.example .env
# Edit .env — at minimum set a real POSTGRES_PASSWORD.

# 2. Build images
docker compose build

# 3. Start PostgreSQL, run migrations, then start API + web
docker compose up -d db
docker compose run --rm migrate        # one-off Alembic migration
docker compose up -d api web

# 4. Check health
docker compose ps                      # all services should be "Up" or "healthy"
curl -s http://localhost:8000/health    # should return {"status":"ok"}
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
| `api`      | 8000  | FastAPI + Uvicorn. Starts only after `db` is healthy and `migrate` succeeds. |
| `web`      | 3000  | Next.js standalone server. Starts only after `api` is healthy. |

## Migration handling

Migrations are **not** run inside the API container. Instead:

1. `docker compose up -d db` — starts PostgreSQL and waits for it to be healthy.
2. `docker compose run --rm migrate` — runs `alembic upgrade head` once.
3. `docker compose up -d api web` — starts the API (which now depends on `migrate` having completed).

This design prevents multiple API replicas from racing to run migrations.
If you scale the API (`--scale api=N`), migrations have already finished.

## Cross-platform builds

All base images (`python:3.12-slim`, `node:20-alpine`, `postgres:16-alpine`)
support both `linux/amd64` and `linux/arm64`. No `platform:` field is set in
the Compose file, so images build for the host architecture.

To build an ARM64 image on an x86 host (or vice versa):

```bash
# Ensure a buildx builder with QEMU is available
docker buildx create --use --name multiarch --driver docker-container
docker buildx inspect --bootstrap

# Build API for ARM64
docker buildx build --platform linux/arm64 -f api/Dockerfile -t infinityscan-api:arm64 api

# Build Web for ARM64
docker buildx build --platform linux/arm64 \
  --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 \
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

See `.env.example` for the full list. The most important variables:

| Variable              | Where used | Notes                                           |
|-----------------------|------------|-------------------------------------------------|
| `POSTGRES_PASSWORD`   | db         | **Change this.**                                |
| `DATABASE_URL`        | api, migrate | Must match POSTGRES_* settings.               |
| `SECRET_KEY`          | api        | Optional for local dev (ephemeral key generated if missing). |
| `NEXT_PUBLIC_API_URL` | web (build-time) | Must be reachable by end-user browsers. |

## Troubleshooting

**API won't start — "connection refused" on db**
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
