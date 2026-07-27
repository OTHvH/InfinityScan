"""Focused tests for the canonical cursor-based local reader API."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import event

from models import Chapter, ChapterImportStatus, ContentType, Page, PageIntegrityStatus, Series
from reader.cursor import ReaderDirection, encode_cursor
from storage.keys import page_object_key


SERIES_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER_SERIES_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _id(value: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-0000-0000-{value:012d}")


def _chapter(
    series: Series,
    chapter_id: uuid.UUID,
    number: str,
    *,
    language: str = "en",
    status: ChapterImportStatus = ChapterImportStatus.ready,
    page_values: list[tuple[int, PageIntegrityStatus, int | None, int | None]] | None = None,
) -> Chapter:
    chapter = Chapter(
        id=chapter_id,
        series=series,
        number=Decimal(number),
        title=f"Chapter {number}",
        language=language,
        page_count=len(page_values or []),
        import_status=status,
    )
    chapter.pages = [
        Page(
            id=_id(chapter_id.int % 1000 * 10 + page_number),
            chapter=chapter,
            page_number=page_number,
            object_key=page_object_key(series.id, chapter_id, page_number, "a" * 64, "jpg"),
            integrity_status=page_status,
            width=width,
            height=height,
            file_size=1,
            sha256="a" * 64,
            mime_type="image/jpeg",
            file_extension="jpg",
            verified_at=datetime.now(timezone.utc) if page_status == PageIntegrityStatus.verified else None,
        )
        for page_number, page_status, width, height in (page_values or [])
    ]
    return chapter


def _reader_fixture(db):
    series = Series(
        id=SERIES_ID,
        slug="cursor-series",
        title="Cursor Series",
        content_type=ContentType.manga,
    )
    chapters = {
        "1": _chapter(series, _id(101), "1", page_values=[(2, PageIntegrityStatus.verified, 800, 1200), (1, PageIntegrityStatus.verified, 1200, 1800)]),
        "1.5": _chapter(series, _id(102), "1.5", page_values=[(1, PageIntegrityStatus.verified, 1000, 1500), (2, PageIntegrityStatus.pending, 1000, 1500)]),
        "1.75": _chapter(series, _id(103), "1.75", page_values=[(1, PageIntegrityStatus.verified, 1000, 1500)]),
        "10": _chapter(series, _id(104), "10", page_values=[(1, PageIntegrityStatus.verified, 1200, 1800)]),
        "12a": _chapter(series, _id(105), "12", page_values=[(1, PageIntegrityStatus.verified, 1200, 1800)]),
        "12b": _chapter(series, _id(106), "12", language="ja", page_values=[(1, PageIntegrityStatus.verified, 1200, 1800)]),
        "11-importing": _chapter(series, _id(107), "11", status=ChapterImportStatus.importing, page_values=[(1, PageIntegrityStatus.verified, 1200, 1800)]),
        "13-failed": _chapter(series, _id(108), "13", status=ChapterImportStatus.failed, page_values=[(1, PageIntegrityStatus.verified, 1200, 1800)]),
    }
    other_series = Series(
        id=OTHER_SERIES_ID,
        slug="other-series",
        title="Other Series",
        content_type=ContentType.manga,
    )
    other_chapter = _chapter(other_series, _id(201), "1", page_values=[(1, PageIntegrityStatus.verified, 1, 1)])
    db.add_all([series, other_series, *chapters.values(), other_chapter])
    db.commit()
    return series, chapters, other_series, other_chapter


def _get_initial(client, series: Series, chapter: Chapter, **params):
    return client.get(
        f"/reader/{series.slug}/chunks",
        params={"start_chapter_id": str(chapter.id), **params},
    )


def test_initial_contract_decimal_strings_and_media_safety(client, db):
    series, chapters, _, _ = _reader_fixture(db)

    response = _get_initial(client, series, chapters["1.5"])

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "series", "chapters", "chapter_boundaries", "next_cursor", "previous_cursor", "has_more_next", "has_more_previous",
    }
    assert body["series"] == {"id": str(series.id), "slug": series.slug, "title": series.title}
    assert [chapter["number"] for chapter in body["chapters"]] == ["1.5", "1.75"]
    assert body["chapters"][0]["page_count"] == 1
    assert [page["page_number"] for page in body["chapters"][0]["pages"]] == [1]
    assert body["chapters"][0]["pages"][0]["media_path"].startswith("/media/pages/")
    assert body["chapters"][0]["pages"][0]["aspect_ratio"] == 0.6666667
    assert set(body["chapters"][0]) == {
        "id", "number", "title", "page_count", "previous_chapter_id", "next_chapter_id", "pages",
    }
    assert body["previous_cursor"] is not None
    assert body["next_cursor"] is not None
    assert [boundary["chapter_id"] for boundary in body["chapter_boundaries"]] == [
        chapter["id"] for chapter in body["chapters"]
    ]
    assert body["previous_cursor"] == body["chapter_boundaries"][0]["previous_cursor"]
    assert body["next_cursor"] == body["chapter_boundaries"][-1]["next_cursor"]
    assert "object_key" not in json.dumps(body)
    assert "X-Amz" not in json.dumps(body)
    assert "https://" not in json.dumps(body)


def test_next_and_previous_cursors_skip_unready_chapters(client, db):
    series, chapters, _, _ = _reader_fixture(db)
    initial = _get_initial(client, series, chapters["1.5"])
    initial_body = initial.json()

    following = client.get(
        f"/reader/{series.slug}/chunks",
        params={"cursor": initial_body["next_cursor"], "direction": "next", "limit": 5},
    )
    previous = client.get(
        f"/reader/{series.slug}/chunks",
        params={"cursor": initial_body["previous_cursor"], "direction": "previous", "limit": 5},
    )

    assert [chapter["number"] for chapter in following.json()["chapters"]] == ["10", "12", "12"]
    assert [chapter["number"] for chapter in previous.json()["chapters"]] == ["1"]
    assert all(chapter["number"] not in {"11", "13"} for chapter in following.json()["chapters"])


def test_chapter_boundary_cursors_resume_from_each_retained_edge(client, db):
    series, chapters, _, _ = _reader_fixture(db)
    body = _get_initial(client, series, chapters["1.5"], limit=3).json()
    boundaries = body["chapter_boundaries"]

    assert [boundary["chapter_id"] for boundary in boundaries] == [
        str(chapters["1.5"].id),
        str(chapters["1.75"].id),
        str(chapters["10"].id),
    ]
    assert all(boundary["has_more_next"] == (boundary["next_cursor"] is not None) for boundary in boundaries)
    assert all(
        boundary["has_more_previous"] == (boundary["previous_cursor"] is not None)
        for boundary in boundaries
    )

    after_first = client.get(
        f"/reader/{series.slug}/chunks",
        params={"cursor": boundaries[0]["next_cursor"], "direction": "next", "limit": 1},
    )
    before_middle = client.get(
        f"/reader/{series.slug}/chunks",
        params={"cursor": boundaries[1]["previous_cursor"], "direction": "previous", "limit": 1},
    )

    assert after_first.status_code == 200
    assert [chapter["id"] for chapter in after_first.json()["chapters"]] == [
        str(chapters["1.75"].id)
    ]
    assert before_middle.status_code == 200
    assert [chapter["id"] for chapter in before_middle.json()["chapters"]] == [
        str(chapters["1.5"].id)
    ]


def test_first_and_final_boundaries_return_null_cursors(client, db):
    series, chapters, _, _ = _reader_fixture(db)

    first = _get_initial(client, series, chapters["1"], limit=2).json()
    final = _get_initial(client, series, chapters["12b"], limit=2).json()

    assert first["previous_cursor"] is None
    assert first["has_more_previous"] is False
    assert first["chapter_boundaries"][0]["previous_cursor"] is None
    assert first["chapter_boundaries"][0]["has_more_previous"] is False
    assert final["next_cursor"] is None
    assert final["has_more_next"] is False
    assert final["chapter_boundaries"][-1]["next_cursor"] is None
    assert final["chapter_boundaries"][-1]["has_more_next"] is False


def test_decimal_order_and_duplicate_number_uuid_tiebreak(client, db):
    series, chapters, _, _ = _reader_fixture(db)
    first = _get_initial(client, series, chapters["1"], limit=5).json()
    next_page = client.get(
        f"/reader/{series.slug}/chunks",
        params={"cursor": first["next_cursor"], "direction": "next", "limit": 5},
    ).json()

    assert [chapter["number"] for chapter in first["chapters"]] == ["1", "1.5", "1.75", "10", "12"]
    assert [chapter["id"] for chapter in next_page["chapters"]] == [str(chapters["12b"].id)]

    duplicate = _get_initial(client, series, chapters["12a"], limit=2).json()
    assert [chapter["id"] for chapter in duplicate["chapters"]] == [str(chapters["12a"].id), str(chapters["12b"].id)]


def test_validation_rejects_missing_both_mutually_exclusive_and_bad_cursors(client, db):
    series, chapters, _, _ = _reader_fixture(db)
    initial = _get_initial(client, series, chapters["1"]).json()
    valid_cursor = initial["next_cursor"]

    assert client.get(f"/reader/{series.slug}/chunks").status_code == 400
    assert _get_initial(client, series, chapters["1"], cursor=valid_cursor).status_code == 400
    assert client.get(f"/reader/{series.slug}/chunks", params={"cursor": "malformed", "direction": "next"}).status_code == 400
    tampered = valid_cursor[:-1] + ("A" if valid_cursor[-1] != "A" else "B")
    assert client.get(f"/reader/{series.slug}/chunks", params={"cursor": tampered, "direction": "next"}).status_code == 400
    unsupported = encode_cursor(series.id, Decimal("1"), chapters["1"].id, ReaderDirection.next, version=2)
    assert client.get(f"/reader/{series.slug}/chunks", params={"cursor": unsupported, "direction": "next"}).status_code == 400
    assert client.get(
        f"/reader/{series.slug}/chunks",
        params={"cursor": valid_cursor, "direction": "previous"},
    ).status_code == 400


def test_cross_series_start_and_cursor_are_rejected(client, db):
    series, chapters, other_series, other_chapter = _reader_fixture(db)
    initial = _get_initial(client, series, chapters["1"]).json()

    assert _get_initial(client, other_series, chapters["1"]).status_code == 404
    response = client.get(
        f"/reader/{other_series.slug}/chunks",
        params={"cursor": initial["next_cursor"], "direction": "next"},
    )
    assert response.status_code == 400
    assert _get_initial(client, other_series, other_chapter).status_code == 200


def test_query_count_is_bounded_for_a_chunk(client, db):
    series, chapters, _, _ = _reader_fixture(db)
    count = 0

    def count_query(*_args):
        nonlocal count
        count += 1

    engine = db.get_bind()
    series_slug = series.slug
    start_id = chapters["1"].id
    event.listen(engine, "before_cursor_execute", count_query)
    try:
        response = client.get(
            f"/reader/{series_slug}/chunks",
            params={"start_chapter_id": str(start_id), "limit": 5},
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_query)

    assert response.status_code == 200
    assert count <= 5


def test_number_based_local_reader_route_is_deprecated(client):
    operation = client.get("/openapi.json").json()["paths"][
        "/library/{slug}/chapter/{number}"
    ]["get"]

    assert operation["deprecated"] is True
    assert "/reader/{series_slug}/chunks" in operation["description"]
