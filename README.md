# InfinityScan

Production-ready monorepo with a Next.js frontend, FastAPI backend, and Docker Compose infrastructure.

## Structure

```
.
├── web/     # Next.js App Router + TypeScript + Tailwind
├── api/     # FastAPI
└── infra/   # Docker Compose + environment examples
```

## Prerequisites

- [Node.js 24](https://nodejs.org/) (`web/package.json` requires `>=24.18.1 <25`)
- [Python 3.12+](https://www.python.org/)
- [Docker](https://www.docker.com/) & [Docker Compose](https://docs.docker.com/compose/)

---

## Local Development (without Docker)

### API

```bash
cd api
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

FastAPI listens directly at <http://127.0.0.1:8000>; its routes are unprefixed,
so interactive docs are at <http://127.0.0.1:8000/docs>. This listener is the
web server's upstream, not the URL browser code should use.

The local reader API contract is documented in [`docs/reader-api.md`](docs/reader-api.md).

### Web

```bash
cd web
npm ci
npm run dev
```

Open <http://localhost:3000>. Browser API requests are always same-origin under
`/api` (for example, <http://localhost:3000/api/health>). Next.js strips that
prefix when proxying to FastAPI, where the route remains `/health`.

`API_INTERNAL_URL` selects the Next.js server's upstream and defaults to
`http://localhost:8000` only in development. It is server-only and must never be
renamed to a `NEXT_PUBLIC_*` variable or otherwise exposed in client JavaScript.

---

## Running with Docker Compose

```bash
cp infra/.env.example infra/.env
# edit infra/.env as needed
docker compose --env-file infra/.env -f infra/docker-compose.yml up --build
```

| Service | URL                    |
|---------|------------------------|
| Web     | <http://localhost:3000> |
| Browser API | <http://localhost:3000/api> |
| FastAPI direct (operator diagnostics) | <http://127.0.0.1:8000> |

---

## Environment Variables

[`infra/.env.example`](infra/.env.example) is the local development template
copied to `infra/.env`. [`infra/.env.production.example`](infra/.env.production.example)
is a placeholder-only production checklist: replace every angle-bracket value
in a protected environment source before use; do not use it as credentials.

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | Required PostgreSQL SQLAlchemy URL for the API and migrations |
| `JWT_SECRET_KEY`, `CSRF_SECRET_KEY` | Separate authentication secrets; production requires distinct values of at least 32 bytes each |
| `TRUSTED_HOSTS`, `ALLOWED_ORIGINS` | Explicit deployment host and unsafe-request origin allowlists |
| `CORS_ORIGINS` | Exact cross-origin browser origins; set it explicitly empty for a same-origin deployment |
| `API_INTERNAL_URL` | Private Next.js-server-to-FastAPI origin; never available to browser JavaScript |
| `OBJECT_STORAGE_ENABLED` | Enables private S3-compatible storage and startup readiness checks |
| `S3_BUCKET` | Object-storage bucket used by the API |

Production also requires bounded database runtime settings (`DB_POOL_SIZE`,
`DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_SECONDS`, `DB_POOL_RECYCLE_SECONDS`,
`DB_CONNECT_TIMEOUT_SECONDS`, and `DB_STATEMENT_TIMEOUT_MS`), a TLS `DB_SSLMODE`,
and `OBJECT_STORAGE_ENABLED=true` with an HTTPS S3-compatible endpoint.
Production uses `/api/livez` for process liveness and `/api/readyz` for
dependency readiness. `LOG_FORMAT=json` enables redacted structured logs.
Forwarded headers remain ignored unless `TRUST_FORWARDED_HEADERS=true` and the
immediate proxy network is explicitly listed in `TRUSTED_PROXY_CIDRS`. The
process-local limiter is a single-API-process invariant; scale-out requires a
shared limiter or enforced edge limiting. Configure `MEDIA_CSP_ORIGINS` with
the exact HTTPS R2/S3 media origin(s) used by private redirects.

The repository also contains a production-only deployment package using
digest-pinned GHCR images, Caddy, external TLS PostgreSQL, private HTTPS S3
storage, runtime secret files, and one-shot migrations. Start with
[`docs/production-deployment.md`](docs/production-deployment.md); production
Compose does not include PostgreSQL or MinIO.

To run local object-storage integration, configure the `S3_*` variables in
`infra/.env`, then run
`docker compose --env-file infra/.env -f infra/docker-compose.yml --profile storage up -d minio`
followed by
`docker compose --env-file infra/.env -f infra/docker-compose.yml --profile storage run --rm minio-setup`.
The API never creates production buckets automatically.

---

## Project Scripts

### Web

| Command         | Description              |
|-----------------|--------------------------|
| `npm run dev`   | Start dev server         |
| `npm run build` | Production build         |
| `npm run start` | Start production server  |
| `npm run lint`  | Run ESLint               |
| `npm run test`  | Run Vitest unit tests    |
| `npm run test:e2e` | Run Playwright tests |

### API

| Command                              | Description            |
|--------------------------------------|------------------------|
| `uvicorn main:app --reload`          | Start with hot-reload  |
| `uvicorn main:app --host 0.0.0.0`   | Start for Docker       |

## Reader

Reader links use a series slug and immutable chapter UUID:
`/reader/{series_slug}/{chapter_uuid}`. The frontend obtains bounded chapter
chunks from the signed cursor API; it does not construct storage URLs or use
chapter-number arithmetic.

Supported modes:

- **Continuous** virtualizes a cross-chapter vertical feed and loads in both directions.
- **Vertical** virtualizes one chapter at a time.
- **Horizontal** shows one page or an optional two-page spread, with LTR or RTL navigation.

Reader preferences are stored per series. Available presentation controls are
single/spread layout, first-page-alone, fit width, reading direction, and
40–160% zoom.

Keyboard controls:

| Key | Action |
|-----|--------|
| `[` / `]` | Previous / next chapter |
| `Left` / `Right` | Previous / next page in paged modes; reversed in RTL |
| `Space` | Next page or spread |
| `Home` / `End` | First / last page |
| `+` or `=` / `-` | Zoom in / out |
| `0` | Reset zoom and fit-width |
| `F` | Toggle fit-width |
| `?` | Toggle shortcut help |

Shortcuts are disabled while an editable control has focus. Continuous mode
uses native scrolling, except for chapter, zoom, fit, and help shortcuts.

## Release Verification

Run the cumulative Phase 5 release gate:

```bash
scripts/verify-phase5.sh
```

Direct Phase 3-5 invocations run their prerequisite phases sequentially;
`--phase-only` is for CI/orchestrators that already enforce those dependencies.
Each verifier uses a temporary synthetic environment, random loopback ports, a
unique Compose project and volumes, and checks that `infra/.env` remains
byte-identical. Failed runs retain only redacted diagnostics under `/tmp`.

## Backups

Production backup and restore commands are documented in
[`docs/backup-tooling.md`](docs/backup-tooling.md). v1 remains database-only for
compatibility; `--objects` (alias `--complete`) creates the v2 database and
object sidecar. Production requires an explicit mode and age encryption.
