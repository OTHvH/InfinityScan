# InfinityScan Test Plan

## Overview
This test plan covers functional testing for the InfinityScan manga reader application, including import pipeline, reader functionality, user state, and deployment verification.

---

## 1. Import Correctness

### Test Cases

| ID | Description | Steps | Expected Result |
|----|------------|-------|-----------------|
| IMP-01 | Import single series with chapters | 1. Create folder structure: `Series/Chapter 1/001.jpg`<br>2. Run `python importer.py /path/to/root`<br>3. Query API `/series` | Series appears in API with correct chapter count |
| IMP-02 | Import flat series (no chapter folders) | 1. Create: `Manga/001.jpg`, `002.jpg`<br>2. Run importer | Auto-created as Chapter 1 |
| IMP-03 | Import series with half-chapters | 1. Create: `Manga/ch1/`, `Manga/ch1.5/`<br>2. Run importer | Chapters sorted as 1.0, 1.5 |
| IMP-04 | Skip empty chapter folders | 1. Create: `Manga/ch1/001.jpg`, `Manga/ch2/` (empty)<br>2. Run importer | Empty folder skipped |
| IMP-05 | Idempotent import (re-run) | 1. Import series<br>2. Re-run importer with same data | No duplicate chapters created |
| IMP-06 | Cover image detection | 1. Create series with cover in root<br>2. Run importer | Cover object key populated |
| IMP-07 | Dry-run mode | 1. Run with `--dry-run`<br>2. Check database | No records inserted |

### Test Data Structure
```
/tmp/test_library/
├── One Piece/
│   ├── cover.jpg
│   ├── Chapter 1/
│   │   ├── 001.jpg
│   │   └── 002.jpg
│   ├── Chapter 1.5/
│   │   └── 001.jpg
│   └── Chapter 2/
│       └── 001.jpg
├── Attack on Titan (flat)/
│   ├── 001.jpg
│   └── 002.jpg
└── Empty Folder/
```

---

## 2. Chapter Ordering

### Test Cases

| ID | Description | Steps | Expected Result |
|----|------------|-------|-----------------|
| CH-01 | Decimal ordering | Seed chapters 1, 1.5, 1.75, 10 | Numeric order is preserved; numbers are serialized as strings |
| CH-02 | Mixed naming conventions | Import: ch1, Chapter_02, chap-3, vol4 | All detected and sorted |
| CH-03 | Chapter title extraction | Chapter folder named "Chapter 5: The Battle" | Title extracted as "The Battle" |
| CH-04 | Reverse insertion order | Insert chapters in descending order | Cursor API returns deterministic `(number, UUID)` order |
| CH-05 | Volume and chapter | Import with volumes: vol1/ch1, vol1/ch2, vol2/ch1 | Sorted by chapter number |
| CH-06 | Skipped numbers | Seed chapters with integer gaps | Cursor traversal preserves gaps without synthesizing chapters |
| CH-07 | Duplicate numbers | Seed two languages at the same chapter number | UUID is the deterministic tie-breaker |

### Verification Query
```bash
curl "http://localhost:8000/reader/{slug}/chunks?start_chapter_id={uuid}&limit=5" | jq '.chapters[] | [.id, .number]'
```

---

## 3. Reader Stability

### Test Cases

| ID | Description | Steps | Expected Result |
|----|------------|-------|-----------------|
| SCR-01 | Continuous rendering | Open a 200-chapter fixture and scroll rapidly | Virtualized pages render while retained data stays bounded |
| SCR-02 | Paged rendering | Switch between vertical, horizontal, single, spread, LTR, and RTL | Visible chapter is retained and page navigation follows mode settings |
| SCR-03 | 100+ chapter traversal | Traverse beyond chapter 100 and continue to chapter 200 | No duplicate items, exhausted cursors stop requesting, and memory remains bounded |
| SCR-04 | Scroll position restoration | 1. Scroll to middle of chapter<br>2. Navigate away<br>3. Return to chapter | Position restored |
| SCR-05 | Bidirectional loading | Scroll forward until old chapters are evicted, then scroll backward | Previous cursors reload earlier chapters without duplicate requests |
| SCR-06 | URL replacement | Change the viewport-centered chapter while scrolling | URL is replaced with `/reader/{slug}/{chapter_uuid}` without adding history entries |
| SCR-07 | Image retry | Fail one media request temporarily | Retry succeeds within three attempts; HTTP 404 is terminal |

### Phase 4 Performance Limits

The automated long-scroll gate uses 200 chapters with 10 pages each and enforces:

- Retained chapters: at most 9.
- Retained page records: at most 250.
- Retention window: 3 chapters behind and 5 ahead of the protected chapter.
- Mounted page elements: at most 64.
- Mounted chapter separators: at most 8.
- Requests in flight: at most one next and one previous request.
- Chunk requests for the scenario: fewer than 180.
- API chunk size: 1–5 chapters and at most 5 SQL statements.
- Media retries after temporary failure: at most 3.
- Post-eviction Chromium JS heap growth over baseline: at most 64 MiB.

These are automated release limits. They do not claim total browser process
memory, decoded-image memory, frame rate, or initial-render latency.

---

## 4. Resume Progress

### Test Cases

| ID | Description | Steps | Expected Result |
|----|------------|-------|-----------------|
| RES-01 | Save progress locally | 1. Read to page 15<br>2. Wait 1.5 seconds<br>3. Check localStorage | Progress saved with timestamp, series slug, chapter UUID, page, and scroll ratio |
| RES-02 | Resume from localStorage | 1. Close browser at page 10<br>2. Reopen chapter | Jump to page 10 |
| RES-03 | Progress persists across refresh | 1. Read to page 5<br>2. Refresh page | Still at page 5 |
| RES-04 | Save on chapter change | 1. Read to page 10/20<br>2. Navigate to next chapter | Page 10 saved before navigation |
| RES-05 | API progress sync (if implemented) | 1. Login as user<br>2. Read chapter<br>3. Check /progress endpoint | Progress saved to database |

### Verification
```javascript
// Check localStorage
localStorage.getItem('infinityscan_progress_{slug}_{chapter_uuid}')
// Includes seriesSlug, chapterId, page, scrollRatio, and updatedAt.
```

---

## 5. Broken Image Handling

### Test Cases

| ID | Description | Steps | Expected Result |
|----|------------|-------|-----------------|
| IMG-01 | 404 image URL | 1. Mock API to return invalid page URL<br>2. Load chapter | Terminal error placeholder shown; 404 is not accepted as success or retried |
| IMG-02 | Temporary network failure | Abort one media request<br>2. Load pages | Loading state followed by bounded retry and successful image |
| IMG-03 | Corrupt image file | Return invalid JPEG data<br>2. Load page | Decode failure is shown without an infinite retry loop |
| IMG-04 | Empty page URL | 1. API returns page with empty url<br>2. Load chapter | Page skipped or placeholder |
| IMG-05 | Redirect URL handling | 1. Image URL redirects (302)<br>2. Load page | Follows redirect successfully |

### Expected UI Behavior
- Loading: skeleton state.
- Retryable failure: bounded retry with an attempt query marker.
- Terminal failure: error state with no false success.
- Eviction: abort pending work and clear retry timers.

---

## 6. Auth-Protected Routes

### Test Cases

| ID | Description | Steps | Expected Result |
|----|------------|-------|-----------------|
| AUTH-01 | Access bookmarks without auth | 1. GET /bookmarks (no cookie) | 401 Unauthorized |
| AUTH-02 | Consistent user identity | 1. Login as user A<br>2. GET bookmarks | Bookmarks for user A returned |
| AUTH-03 | Different users isolated | 1. User A adds bookmark<br>2. User B queries bookmarks | User B sees only their bookmarks |
| AUTH-05 | Progress user isolation | 1. User A reads to page 5<br>2. User B reads same chapter | Each has independent progress |

### Test Commands
```bash
# Authenticated (cookie-based)
curl -b "is_access=<token>" http://localhost:8000/bookmarks

# Unauthenticated (should return 401)
curl http://localhost:8000/bookmarks
```

---

## 7. Staging to Production Checklist

### Pre-Deployment

- [ ] All tests passing in CI
- [ ] Database migrations tested on staging DB
- [ ] No hardcoded localhost URLs in production
- [ ] Environment variables documented
- [ ] SSL/TLS certificate configured

### Infrastructure

| Check | Command |
|-------|---------|
| PostgreSQL connection | `docker compose exec db pg_isready` |
| API health | `curl http://localhost:8000/health` |
| Web builds | `cd web && npm run build` |
| No build warnings | `npm run lint` passes |

### Security

| Check | Verification |
|-------|--------------|
| CORS origins set | Check `CORS_ORIGINS` env var |
| Database credentials | Not committed to git |
| API rate limiting | Enabled in production |
| Image URL validation | SSRF protection in place |

### Smoke Tests (Post-Deploy)

```bash
# 1. API health
curl https://api.example.com/health
# Expected: {"status": "ok"}

# 2. Web loads
curl -I https://example.com
# Expected: 200 OK

# 3. Series list
curl https://api.example.com/series
# Expected: JSON array (may be empty)

# 4. Search (if using CopyManga)
curl "https://api.example.com/series?q=onepiece"
# Expected: Search results

# 5. Initial local reader chunk
curl "https://api.example.com/reader/onepiece/chunks?start_chapter_id=uuid"
# Expected: Ready chapters with verified same-origin media paths
```

### Rollback Plan

1. Check Docker Compose logs: `docker compose -f infra/docker-compose.yml logs`
2. Revert to previous image tag
3. Restore database from backup if needed
4. Verify with smoke tests

---

## Running Tests

### Unit Tests
```bash
# API tests
cd api && python -m pytest tests/ -v

# Web tests
cd web && npm test
```

### Integration Tests
```bash
# Start services
docker compose -f infra/docker-compose.yml up -d

# Run import test
python api/importer.py /tmp/test_library --dry-run

# Verify import
curl http://localhost:8000/series | jq
```

### E2E Tests (Playwright)
```bash
cd web && npm run build && npm run test:e2e
```

### Phase 4 Release Gate
```bash
scripts/verify-phase4.sh
```

---

## 8. Disaster Recovery and CI Ownership

Task 9 owns the disposable database/object-storage restore drill:

```bash
scripts/test-disaster-recovery.sh
```

Task 10 owns layered GitHub Actions checks. Fast CI runs backend/frontend
quality and dependency checks; integration CI runs the disposable DR drill and
phase-specific E2E ownership; security CI runs secret, CodeQL, dependency, and
container scans. No workflow invokes a wildcard Playwright command.

Task 11 owns the final Phase 5 release gate:

```bash
scripts/verify-phase5.sh
```

The Phase 5 gate executes Phase 1 through Phase 4 prerequisites sequentially,
then verifies integrity, recovery, sessions, audit events, backups, workflow
policy, containers, and cleanup. Required PostgreSQL checks use only explicit
disposable `TASK11_*` targets.
