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

- [Node.js 20+](https://nodejs.org/)
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

API available at <http://localhost:8000>  
Interactive docs at <http://localhost:8000/docs>

The local reader API contract is documented in [`docs/reader-api.md`](docs/reader-api.md).

### Web

```bash
cd web
npm install
npm run dev
```

Web available at <http://localhost:3000>

---

## Running with Docker Compose

```bash
cp infra/.env.example infra/.env
# edit infra/.env as needed
docker compose -f infra/docker-compose.yml up --build
```

| Service | URL                    |
|---------|------------------------|
| Web     | <http://localhost:3000> |
| API     | <http://localhost:8000> |

---

## Environment Variables

See [`infra/.env.example`](infra/.env.example) for all available variables.

| Variable              | Default                                                          | Description                              |
|-----------------------|------------------------------------------------------------------|------------------------------------------|
| `DATABASE_URL`        | `postgresql+psycopg://infinityscan:changeme@db:5432/infinityscan` | PostgreSQL connection URL (required)    |
| `POSTGRES_USER`       | `infinityscan`                                                   | Postgres username (used by `db` service) |
| `POSTGRES_PASSWORD`   | `changeme`                                                       | Postgres password — **change in prod**  |
| `POSTGRES_DB`         | `infinityscan`                                                   | Postgres database name                   |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000`                                          | Base URL the frontend uses for API       |
| `API_HOST`            | `0.0.0.0`                                                        | Host the API listens on                  |
| `API_PORT`            | `8000`                                                           | Port the API listens on                  |
| `OBJECT_STORAGE_ENABLED` | `false`                                                        | Enable private S3-compatible storage    |
| `S3_BUCKET`           | `infinityscan-pages`                                             | Object-storage bucket                   |

To run local object-storage integration, configure the `S3_*` variables in
`infra/.env`, then run `docker compose --profile storage up -d minio` followed
by `docker compose --profile storage run --rm minio-setup`. The API never creates
production buckets automatically.

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

Run the complete Phase 4 release gate from a non-`master` branch:

```bash
scripts/verify-phase4.sh
```

The gate first requires the Phase 1, 2, and 3 verifiers to complete with exit
code 0, no failures, and no mandatory skips. It then creates disposable
PostgreSQL and MinIO volumes, verifies the 200-chapter reader fixture and all
frontend quality gates, measures the long-scroll browser bounds, runs both
dependency audits, and always removes its containers and volumes.

## PostgreSQL Backups

Production-safe PostgreSQL backup and restore commands are documented in
[`docs/backup-tooling.md`](docs/backup-tooling.md). They use custom-format
`pg_dump` archives, manifest checksums, optional age encryption, and refuse
ambiguous or non-empty restore targets by default.
