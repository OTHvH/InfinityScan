# InfinityScan Web

Next.js App Router frontend for InfinityScan.

## Requirements

- Node.js `>=24.18.0 <25`
- The FastAPI development server listening on `127.0.0.1:8000`, or another
  private upstream supplied through `API_INTERNAL_URL`

## Development

```bash
npm ci
npm run dev
```

Open <http://localhost:3000>. Browser requests use root-relative, same-origin
URLs under `/api`; for example, the browser requests `/api/health`, while the
proxied FastAPI route is `/health`.

In development, the Next.js server defaults `API_INTERNAL_URL` to
`http://localhost:8000`. To use another upstream:

```bash
API_INTERNAL_URL=http://127.0.0.1:8080 npm run dev
```

`API_INTERNAL_URL` is required outside development and accepts only an absolute
HTTP(S) origin with no credentials, path, query, or fragment. It is server-only
and must never use a `NEXT_PUBLIC_*` name, be read by a client component, or be
included in browser JavaScript. Production builds and runtime processes should
receive the same private upstream value.

## Commands

| Command | Purpose |
|---------|---------|
| `npm run dev` | Start the development server |
| `npm run build` | Build the standalone production server |
| `npm run start` | Start the production server |
| `npm test` | Run Vitest tests |
| `npm run lint` | Run ESLint |
| `npx tsc --noEmit` | Run the TypeScript check |
| `npm run test:e2e:all` | Run all owned Playwright phases |
