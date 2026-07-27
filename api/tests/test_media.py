"""Tests for private object-storage media delivery."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from storage import ObjectMetadata

from models import Chapter, ChapterImportStatus, ContentType, Page, PageIntegrityStatus, Series


class FakeMediaStorage:
    def __init__(self, keys: set[str] | None = None) -> None:
        self.keys = keys or set()
        self.presign_calls: list[tuple[str, int | None]] = []

    def head_object(self, key: str) -> ObjectMetadata | None:
        if key not in self.keys:
            return None
        return ObjectMetadata(
            key=key,
            byte_size=10,
            mime_type="image/jpeg",
            sha256=None,
            etag="etag",
        )

    def upload_file(self, key: str, file_path: str | Path, mime_type: str):
        raise AssertionError("media delivery must not upload")

    def upload_bytes(self, key: str, data: bytes, mime_type: str):
        raise AssertionError("media delivery must not upload")

    def delete_object(self, key: str) -> bool:
        raise AssertionError("media delivery must not delete")

    def object_exists(self, key: str) -> bool:
        return key in self.keys

    def generate_presigned_get(self, key: str, expires_in: int | None = None) -> str:
        self.presign_calls.append((key, expires_in))
        return "https://objects.example.test/private?X-Amz-Credential=public-id&X-Amz-Signature=signature"

    def verify_object(self, key: str, expected_sha256=None, expected_size=None):
        raise AssertionError("media delivery must not download objects")

    def health_check(self) -> bool:
        return True


def _create_content(db, *, chapter_status=ChapterImportStatus.ready, page_status=PageIntegrityStatus.verified):
    series = Series(
        id=uuid.uuid4(),
        slug=f"media-{uuid.uuid4().hex}",
        title="Media Test",
        content_type=ContentType.manga,
        cover_object_key="series/cover.jpg",
    )
    chapter = Chapter(
        id=uuid.uuid4(),
        series=series,
        number=Decimal("1"),
        language="en",
        page_count=1,
        import_status=chapter_status,
    )
    page = Page(
        id=uuid.uuid4(),
        chapter=chapter,
        page_number=1,
        object_key="series/page.jpg",
        integrity_status=page_status,
        sha256="a" * 64,
        mime_type="image/jpeg",
        file_extension="jpg",
        width=100,
        height=100,
        file_size=10,
        verified_at=datetime.now(timezone.utc) if page_status == PageIntegrityStatus.verified else None,
    )
    db.add(series)
    db.commit()
    db.refresh(series)
    db.refresh(chapter)
    db.refresh(page)
    return series, chapter, page


def test_verified_page_redirect(client, db, monkeypatch):
    from main import _cfg
    import main

    _, _, page = _create_content(db)
    storage = FakeMediaStorage({page.object_key})
    monkeypatch.setattr(main, "_media_storage", storage)

    response = client.get(f"/media/pages/{page.id}", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://objects.example.test/")
    assert storage.presign_calls == [(page.object_key, _cfg.s3_presign_ttl_seconds)]


def test_pending_page_hidden(client, db, monkeypatch):
    import main

    _, _, page = _create_content(db, page_status=PageIntegrityStatus.pending)
    monkeypatch.setattr(main, "_media_storage", FakeMediaStorage({page.object_key}))

    response = client.get(f"/media/pages/{page.id}", follow_redirects=False)

    assert response.status_code == 404


def test_failed_chapter_hidden(client, db, monkeypatch):
    import main

    _, _, page = _create_content(db, chapter_status=ChapterImportStatus.failed)
    monkeypatch.setattr(main, "_media_storage", FakeMediaStorage({page.object_key}))

    response = client.get(f"/media/pages/{page.id}", follow_redirects=False)

    assert response.status_code == 404


def test_missing_object_marks_page_for_review(client, db, monkeypatch):
    import main

    _, _, page = _create_content(db)
    monkeypatch.setattr(main, "_media_storage", FakeMediaStorage())

    response = client.get(f"/media/pages/{page.id}", follow_redirects=False)

    assert response.status_code == 404
    db.refresh(page)
    assert page.integrity_status == PageIntegrityStatus.missing
    assert page.verified_at is None


def test_invalid_page_id_returns_404(client, db, monkeypatch):
    import main

    storage = FakeMediaStorage()
    monkeypatch.setattr(main, "_media_storage", storage)

    response = client.get("/media/pages/not-a-uuid", follow_redirects=False)

    assert response.status_code == 404
    assert storage.presign_calls == []


def test_cover_redirect(client, db, monkeypatch):
    import main

    series, _, _ = _create_content(db)
    storage = FakeMediaStorage({series.cover_object_key})
    monkeypatch.setattr(main, "_media_storage", storage)

    response = client.get(f"/media/covers/{series.id}", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://objects.example.test/")
    assert storage.presign_calls == [(series.cover_object_key, main._cfg.s3_presign_ttl_seconds)]


def test_cover_without_ready_chapter_hidden(client, db, monkeypatch):
    import main

    series, _, _ = _create_content(db, chapter_status=ChapterImportStatus.importing)
    storage = FakeMediaStorage({series.cover_object_key})
    monkeypatch.setattr(main, "_media_storage", storage)

    response = client.get(f"/media/covers/{series.id}", follow_redirects=False)

    assert response.status_code == 404
    assert storage.presign_calls == []


def test_media_responses_do_not_leak_storage_credentials(client, db, monkeypatch):
    import main

    series, _, page = _create_content(db)
    storage = FakeMediaStorage({page.object_key, series.cover_object_key})
    monkeypatch.setattr(main, "_media_storage", storage)

    page_response = client.get(f"/media/pages/{page.id}", follow_redirects=False)
    cover_response = client.get(f"/media/covers/{series.id}", follow_redirects=False)

    for response in (page_response, cover_response):
        assert "secret" not in response.headers["location"].lower()
        assert "secret-access-key" not in response.headers["location"].lower()
        assert "objects.example.test" in response.headers["location"]


def test_local_reader_page_urls_are_same_origin_media_urls(client, db):

    series, chapter, page = _create_content(db)
    response = client.get(f"/library/{series.slug}/chapter/{chapter.number}")

    assert response.status_code == 200
    assert response.json()["pages"] == [{
        "page_number": 1,
        "url": f"/media/pages/{page.id}",
    }]


def test_local_series_cover_url_is_same_origin_media_url(client, db):
    series, _, _ = _create_content(db)
    response = client.get(f"/library/{series.slug}")

    assert response.status_code == 200
    assert response.json()["cover_url"] == f"/media/covers/{series.id}"
