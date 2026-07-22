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

### API

| Command                              | Description            |
|--------------------------------------|------------------------|
| `uvicorn main:app --reload`          | Start with hot-reload  |
| `uvicorn main:app --host 0.0.0.0`   | Start for Docker       |
