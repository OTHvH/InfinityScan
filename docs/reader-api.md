# Reader API

The public browser endpoint is same-origin:

```text
GET /api/reader/{series_slug}/chunks
```

Next.js removes `/api` when proxying. The canonical FastAPI route therefore
remains unprefixed:

```text
GET /reader/{series_slug}/chunks
```

Browser code must use the root-relative public path and must not know the
server-only `API_INTERNAL_URL`.

## Request

Exactly one position source is required:

- `start_chapter_id`: chapter UUID for an initial or deep-linked request.
- `cursor`: opaque cursor returned by an earlier chunk response.

Query parameters:

| Parameter | Values | Default |
|-----------|--------|---------|
| `direction` | `next`, `previous` | `next` |
| `limit` | integer from 1 through 5 | `2` |

Cursors are signed, versioned, and bound to their series and direction. Clients
must not decode, alter, synthesize, persist as chapter identity, or reuse a
cursor with another series/direction. Chapter UUIDs are the stable identities.

## Response

```json
{
  "series": { "id": "uuid", "slug": "series", "title": "Series" },
  "chapters": [
    {
      "id": "uuid",
      "number": "1.5",
      "title": "Chapter title",
      "page_count": 1,
      "previous_chapter_id": "uuid-or-null",
      "next_chapter_id": "uuid-or-null",
      "pages": [
        {
          "id": "uuid",
          "page_number": 1,
          "media_path": "/media/pages/uuid",
          "width": 1200,
          "height": 1800,
          "aspect_ratio": 0.6666667
        }
      ]
    }
  ],
  "chapter_boundaries": [
    {
      "chapter_id": "uuid",
      "next_cursor": "opaque-or-null",
      "previous_cursor": "opaque-or-null",
      "has_more_next": true,
      "has_more_previous": false
    }
  ],
  "next_cursor": "opaque-or-null",
  "previous_cursor": "opaque-or-null",
  "has_more_next": true,
  "has_more_previous": false
}
```

Chapter numbers are decimal strings for display only. Ordering is numeric by
`(chapter.number, chapter.id)`, which supports decimals, gaps, and duplicate
numbers without frontend arithmetic. Only chapters with `ready` import status
and pages with `verified` integrity status are returned.

`chapter_boundaries` has one entry for each chapter at the same array index.
Its cursors resume immediately after or before that chapter, allowing a bounded
client to evict chapter data and later reload from its retained edges. The
top-level cursors are the last chapter's `next_cursor` and the first chapter's
`previous_cursor`, respectively. A cursor and its corresponding `has_more`
value are null/false at an exhausted series boundary.

FastAPI returns `media_path` as an unprefixed API path such as
`/media/pages/{uuid}`. The browser client validates it and requests
`/api/media/pages/{uuid}` through the same-origin proxy. Reader responses never
expose object keys, bucket names, storage endpoints, credentials, or presigned
URLs. Clients follow the media service response; HTTP 404 is a failure.

## Errors

- `400`: missing/both position sources, malformed/tampered cursor, wrong-series cursor, or wrong-direction cursor.
- `404`: series or starting chapter is unavailable.
- `422`: invalid UUID, direction, or limit.

## Deprecated Route

The FastAPI route `GET /library/{slug}/chapter/{number}` remains temporarily for
compatibility and is marked deprecated in OpenAPI. It is ambiguous when
multiple languages share a chapter number. The frontend does not call or link
to this route; all page links use `/reader/{series_slug}/{chapter_uuid}` and all
browser data loading uses the `/api/reader/.../chunks` endpoint above.
