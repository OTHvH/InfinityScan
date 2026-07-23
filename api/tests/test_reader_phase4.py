"""At-scale deterministic Phase 4 reader contract tests."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event

from models import Chapter, ChapterImportStatus, ContentType, Page, PageIntegrityStatus, Series


SERIES_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
OTHER_SERIES_ID = uuid.UUID("40000000-0000-0000-0000-000000000002")
READY_CHAPTER_COUNT = 200
VERIFIED_PAGES_PER_CHAPTER = 10
PAGE_WIDTH = 16
PAGE_HEIGHT = 24
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _id(value: int) -> uuid.UUID:
    return uuid.UUID(f"40000000-0000-0000-0000-{value:012d}")


@dataclass(frozen=True)
class Phase4ReaderFixture:
    series: Series
    other_series: Series
    ordered_chapters: tuple[Chapter, ...]
    tiny_image: Path


@pytest.fixture()
def phase4_reader_fixture(db, tmp_path: Path) -> Phase4ReaderFixture:
    image_path = tmp_path / "phase4-page.png"
    image_path.write_bytes(TINY_PNG)
    digest = hashlib.sha256(TINY_PNG).hexdigest()
    series = Series(
        id=SERIES_ID,
        slug="phase4-reader",
        title="Phase 4 Reader",
        content_type=ContentType.manga,
    )
    other_series = Series(
        id=OTHER_SERIES_ID,
        slug="phase4-other-reader",
        title="Phase 4 Other Reader",
        content_type=ContentType.manga,
    )
    chapters: list[Chapter] = []
    for index in range(READY_CHAPTER_COUNT):
        # Skip one integer after each block and include regular decimal chapters.
        number = Decimal(index + 1 + index // 20)
        if index % 13 == 4:
            number += Decimal("0.5")
        language = "en"
        if index == 80:
            number = chapters[79].number
            language = "ja"  # Same number remains legal while UUID controls ordering.
        chapter = Chapter(
            id=_id(1_000 + index),
            series=series,
            number=number,
            title=f"Phase 4 Chapter {index + 1}",
            language=language,
            page_count=VERIFIED_PAGES_PER_CHAPTER,
            import_status=ChapterImportStatus.ready,
        )
        chapter.pages = [
            Page(
                id=_id(1_000_000 + index * 20 + page_number),
                chapter=chapter,
                page_number=page_number,
                object_key=f"phase4/{chapter.id}/{page_number}.png",
                width=PAGE_WIDTH,
                height=PAGE_HEIGHT,
                file_size=len(TINY_PNG),
                sha256=digest,
                mime_type="image/png",
                file_extension="png",
                integrity_status=PageIntegrityStatus.verified,
            )
            for page_number in range(1, VERIFIED_PAGES_PER_CHAPTER + 1)
        ]
        chapters.append(chapter)

    # A ready chapter can contain an unverified row, but it must never leak.
    chapters[20].pages.append(
        Page(
            id=_id(9_000_001),
            chapter=chapters[20],
            page_number=11,
            object_key="phase4/pending.png",
            width=PAGE_WIDTH,
            height=PAGE_HEIGHT,
            file_size=len(TINY_PNG),
            sha256=digest,
            mime_type="image/png",
            file_extension="png",
            integrity_status=PageIntegrityStatus.pending,
        )
    )
    hidden = [
        Chapter(
            id=_id(9_100_001), series=series, number=Decimal("0.5"), title="Importing",
            language="en", page_count=1, import_status=ChapterImportStatus.importing,
            pages=[Page(id=_id(9_200_001), page_number=1, object_key="phase4/importing.png", width=PAGE_WIDTH, height=PAGE_HEIGHT, integrity_status=PageIntegrityStatus.verified)],
        ),
        Chapter(
            id=_id(9_100_002), series=series, number=Decimal("999"), title="Failed",
            language="en", page_count=1, import_status=ChapterImportStatus.failed,
            pages=[Page(id=_id(9_200_002), page_number=1, object_key="phase4/failed.png", width=PAGE_WIDTH, height=PAGE_HEIGHT, integrity_status=PageIntegrityStatus.verified)],
        ),
    ]
    other_chapter = Chapter(
        id=_id(9_300_001), series=other_series, number=Decimal("1"), title="Other",
        language="en", page_count=1, import_status=ChapterImportStatus.ready,
        pages=[Page(id=_id(9_400_001), page_number=1, object_key="phase4/other.png", width=PAGE_WIDTH, height=PAGE_HEIGHT, integrity_status=PageIntegrityStatus.verified)],
    )
    # Reverse insertion ensures assertions exercise explicit database ordering.
    db.add_all([series, other_series, *reversed(chapters), *hidden, other_chapter])
    db.commit()
    ordered = tuple(sorted(chapters, key=lambda chapter: (chapter.number, chapter.id)))
    return Phase4ReaderFixture(series, other_series, ordered, image_path)


def _initial(client, fixture: Phase4ReaderFixture, index: int = 0, **params):
    return client.get(
        f"/reader/{fixture.series.slug}/chunks",
        params={"start_chapter_id": str(fixture.ordered_chapters[index].id), **params},
    )


def test_phase4_large_fixture_cursor_contract_and_filters(client, db, phase4_reader_fixture):
    fixture = phase4_reader_fixture
    assert fixture.tiny_image.read_bytes() == TINY_PNG
    expected_ids = [str(chapter.id) for chapter in fixture.ordered_chapters]

    initial = _initial(client, fixture, limit=5)
    assert initial.status_code == 200
    body = initial.json()
    assert len(body["chapters"]) == 5
    assert [chapter["id"] for chapter in body["chapters"]] == expected_ids[:5]
    assert body["previous_cursor"] is None
    assert body["next_cursor"] is not None

    traversed = list(body["chapters"])
    cursor = body["next_cursor"]
    while cursor:
        response = client.get(
            f"/reader/{fixture.series.slug}/chunks",
            params={"cursor": cursor, "direction": "next", "limit": 5},
        )
        assert response.status_code == 200
        chunk = response.json()
        assert len(chunk["chapters"]) <= 5
        traversed.extend(chunk["chapters"])
        cursor = chunk["next_cursor"]

    assert [chapter["id"] for chapter in traversed] == expected_ids
    assert len(traversed) == READY_CHAPTER_COUNT
    assert len({chapter["id"] for chapter in traversed}) == READY_CHAPTER_COUNT
    numbers = [Decimal(chapter["number"]) for chapter in traversed]
    assert any(number % 1 for number in numbers)
    assert any(right - left > 1 for left, right in zip(numbers, numbers[1:]))
    duplicate_positions = [index for index in range(len(numbers) - 1) if numbers[index] == numbers[index + 1]]
    assert duplicate_positions
    duplicate_index = duplicate_positions[0]
    assert traversed[duplicate_index]["id"] < traversed[duplicate_index + 1]["id"]

    serialized = json.dumps(traversed)
    assert "object_key" not in serialized
    assert "phase4/" not in serialized
    assert "X-Amz" not in serialized
    for chapter in traversed:
        assert chapter["page_count"] == VERIFIED_PAGES_PER_CHAPTER
        assert len(chapter["pages"]) == VERIFIED_PAGES_PER_CHAPTER
        assert [page["page_number"] for page in chapter["pages"]] == list(range(1, 11))
        assert all(page["width"] == PAGE_WIDTH and page["height"] == PAGE_HEIGHT for page in chapter["pages"])
        assert all(page["media_path"] == f'/media/pages/{page["id"]}' for page in chapter["pages"])
    assert str(_id(9_000_001)) not in serialized
    assert str(_id(9_100_001)) not in serialized
    assert str(_id(9_100_002)) not in serialized

    middle = _initial(client, fixture, 100, direction="previous", limit=5).json()
    assert [chapter["id"] for chapter in middle["chapters"]] == expected_ids[96:101]
    previous = client.get(
        f"/reader/{fixture.series.slug}/chunks",
        params={"cursor": middle["previous_cursor"], "direction": "previous", "limit": 5},
    ).json()
    assert [chapter["id"] for chapter in previous["chapters"]] == expected_ids[91:96]

    assert _initial(client, fixture, limit=6).status_code == 422
    count = 0

    def count_query(*_args):
        nonlocal count
        count += 1

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", count_query)
    try:
        response = _initial(client, fixture, limit=5)
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    assert response.status_code == 200
    assert count <= 5


def test_phase4_cursor_tampering_and_series_mismatch(client, phase4_reader_fixture):
    fixture = phase4_reader_fixture
    body = _initial(client, fixture, limit=5).json()
    cursor = body["next_cursor"]
    assert cursor
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    assert client.get(
        f"/reader/{fixture.series.slug}/chunks",
        params={"cursor": tampered, "direction": "next", "limit": 5},
    ).status_code == 400
    assert client.get(
        f"/reader/{fixture.other_series.slug}/chunks",
        params={"cursor": cursor, "direction": "next", "limit": 5},
    ).status_code == 400
