"""Seed the deterministic 200-chapter Phase 4 release fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from models import (
    Chapter,
    ChapterImportStatus,
    ContentType,
    Page,
    PageIntegrityStatus,
    Series,
)
from settings import get_settings
from storage import create_object_storage

SERIES_ID = uuid.UUID("60000000-0000-0000-0000-000000000001")
OTHER_SERIES_ID = uuid.UUID("60000000-0000-0000-0000-000000000002")
READY_CHAPTER_COUNT = 200
VERIFIED_PAGES_PER_CHAPTER = 10
SLUG = "phase4-reader"


def fixture_id(value: int) -> uuid.UUID:
    return uuid.UUID(f"60000000-0000-0000-0000-{value:012d}")


def seed(database_url: str) -> dict[str, object]:
    engine = create_engine(database_url, pool_pre_ping=True)
    digest = hashlib.sha256(b"phase4-reader-page").hexdigest()
    with Session(engine, expire_on_commit=False) as db:
        db.execute(delete(Series).where(Series.slug.in_([SLUG, "phase4-other-reader"])))
        series = Series(
            id=SERIES_ID,
            slug=SLUG,
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
            number = Decimal(index + 1 + index // 20)
            if index % 13 == 4:
                number += Decimal("0.5")
            language = "en"
            if index == 80:
                number = chapters[79].number
                language = "ja"
            chapter = Chapter(
                id=fixture_id(1_000 + index),
                series=series,
                number=number,
                title=f"Phase 4 Chapter {index + 1}",
                language=language,
                page_count=VERIFIED_PAGES_PER_CHAPTER,
                import_status=ChapterImportStatus.ready,
            )
            chapter.pages = [
                Page(
                    id=fixture_id(1_000_000 + index * 20 + page_number),
                    chapter=chapter,
                    page_number=page_number,
                    object_key=f"phase4/{chapter.id}/{page_number}.png",
                    width=16,
                    height=24,
                    file_size=18,
                    sha256=digest,
                    mime_type="image/png",
                    file_extension="png",
                    integrity_status=PageIntegrityStatus.verified,
                )
                for page_number in range(1, VERIFIED_PAGES_PER_CHAPTER + 1)
            ]
            chapters.append(chapter)

        chapters[20].pages.append(
            Page(
                id=fixture_id(9_000_001),
                chapter=chapters[20],
                page_number=11,
                object_key="phase4/pending.png",
                width=16,
                height=24,
                integrity_status=PageIntegrityStatus.pending,
            )
        )
        hidden_chapters = [
            Chapter(
                id=fixture_id(9_100_001),
                series=series,
                number=Decimal("0.5"),
                title="Importing",
                language="en",
                page_count=1,
                import_status=ChapterImportStatus.importing,
                pages=[
                    Page(
                        id=fixture_id(9_200_001),
                        page_number=1,
                        object_key="phase4/importing.png",
                        width=16,
                        height=24,
                        sha256=digest,
                        integrity_status=PageIntegrityStatus.verified,
                    )
                ],
            ),
            Chapter(
                id=fixture_id(9_100_002),
                series=series,
                number=Decimal("999"),
                title="Failed",
                language="en",
                page_count=1,
                import_status=ChapterImportStatus.failed,
                pages=[
                    Page(
                        id=fixture_id(9_200_002),
                        page_number=1,
                        object_key="phase4/failed.png",
                        width=16,
                        height=24,
                        sha256=digest,
                        integrity_status=PageIntegrityStatus.verified,
                    )
                ],
            ),
        ]
        other_chapter = Chapter(
            id=fixture_id(9_300_001),
            series=other_series,
            number=Decimal("1"),
            title="Other",
            language="en",
            page_count=1,
            import_status=ChapterImportStatus.ready,
            pages=[
                Page(
                    id=fixture_id(9_400_001),
                    page_number=1,
                    object_key="phase4/other.png",
                    width=16,
                    height=24,
                    sha256=digest,
                    integrity_status=PageIntegrityStatus.verified,
                )
            ],
        )
        db.add_all([series, other_series, *reversed(chapters), *hidden_chapters, other_chapter])
        db.commit()

    ordered = sorted(chapters, key=lambda chapter: (chapter.number, chapter.id))
    first_object_key = ordered[0].pages[0].object_key
    storage = create_object_storage()
    if storage is None:
        raise RuntimeError("Phase 4 fixture requires configured object storage")
    storage.upload_bytes(first_object_key, b"phase4-reader-page", "image/png")
    return {
        "slug": SLUG,
        "ready_chapters": READY_CHAPTER_COUNT,
        "verified_pages": READY_CHAPTER_COUNT * VERIFIED_PAGES_PER_CHAPTER,
        "first_chapter_id": str(ordered[0].id),
        "middle_chapter_id": str(ordered[100].id),
        "hidden_chapter_ids": [str(chapter.id) for chapter in hidden_chapters],
        "pending_page_id": str(fixture_id(9_000_001)),
        "sample_page_id": str(ordered[0].pages[0].id),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = seed(get_settings().database_url)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
