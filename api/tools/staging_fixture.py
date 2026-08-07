"""Create and remove deterministic synthetic staging data only."""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, select, text

from auth import hash_password
from database import _get_session_local
from models import Chapter, ChapterImportStatus, ContentType, Page, PageIntegrityStatus, ReadingMode, Series, SeriesStatus, User, UserRole

FIXTURE_VERSION = "staging-fixture-v1"
FIXTURE_SLUG = "synthetic-staging-series"
SMOKE_USERNAME = "staging-smoke"


def _require_staging(confirm: bool) -> None:
    if os.environ.get("DEPLOYMENT_ENVIRONMENT") != "staging":
        raise SystemExit("staging fixture requires DEPLOYMENT_ENVIRONMENT=staging")
    if os.environ.get("APP_ENV") == "production" and not confirm:
        raise SystemExit("production-mode staging fixture requires --confirm-staging")


def seed(confirm: bool) -> None:
    _require_staging(confirm)
    session_factory = _get_session_local()
    if session_factory is None:
        raise SystemExit("DATABASE_URL is required")
    session = session_factory()
    try:
        session.execute(
            text("INSERT INTO deployment_environment(environment, fixture_version) VALUES ('staging', :version) ON CONFLICT (environment) DO UPDATE SET fixture_version = EXCLUDED.fixture_version"),
            {"version": FIXTURE_VERSION},
        )
        series = session.scalar(select(Series).where(Series.slug == FIXTURE_SLUG))
        if series is None:
            series = Series(
                id=uuid.uuid5(uuid.NAMESPACE_URL, "https://infinityscan.example/staging/series"),
                slug=FIXTURE_SLUG,
                title="Synthetic Staging Series",
                synopsis="Synthetic staging fixture; contains no user data.",
                content_type=ContentType.manga,
                default_reading_mode=ReadingMode.paged,
                status=SeriesStatus.completed,
                year=2026,
            )
            session.add(series)
            session.flush()
        chapter = session.scalar(select(Chapter).where(Chapter.series_id == series.id, Chapter.number == Decimal("1"), Chapter.language == "en"))
        if chapter is None:
            chapter = Chapter(
                id=uuid.uuid5(uuid.NAMESPACE_URL, "https://infinityscan.example/staging/chapter/1"),
                series_id=series.id,
                number=Decimal("1"),
                title="Synthetic Chapter 1",
                language="en",
                page_count=1,
                import_status=ChapterImportStatus.ready,
                verified_at=datetime.now(timezone.utc),
            )
            session.add(chapter)
            session.flush()
        page = session.scalar(select(Page).where(Page.chapter_id == chapter.id, Page.page_number == 1))
        if page is None:
            session.add(Page(
                id=uuid.uuid5(uuid.NAMESPACE_URL, "https://infinityscan.example/staging/page/1"),
                chapter_id=chapter.id,
                page_number=1,
                object_key="staging-fixture/synthetic-staging-series/chapter-1/page-1.jpg",
                width=1,
                height=1,
                file_size=1,
                sha256="0" * 64,
                mime_type="image/jpeg",
                file_extension="jpg",
                integrity_status=PageIntegrityStatus.verified,
                verified_at=datetime.now(timezone.utc),
                imported_at=datetime.now(timezone.utc),
            ))
        user = session.scalar(select(User).where(User.username == SMOKE_USERNAME))
        if user is None:
            password = os.environ.get("STAGING_SMOKE_PASSWORD")
            if not password:
                raise SystemExit("STAGING_SMOKE_PASSWORD must be supplied without printing it")
            session.add(User(username=SMOKE_USERNAME, hashed_password=hash_password(password), role=UserRole.user, is_active=True))
        session.commit()
    finally:
        session.close()
    print(f"staging fixture ready: {FIXTURE_VERSION}")


def cleanup(confirm: bool) -> None:
    _require_staging(confirm)
    session_factory = _get_session_local()
    if session_factory is None:
        raise SystemExit("DATABASE_URL is required")
    session = session_factory()
    try:
        series = session.scalar(select(Series).where(Series.slug == FIXTURE_SLUG))
        if series is not None:
            session.execute(delete(Series).where(Series.id == series.id))
        session.execute(delete(User).where(User.username == SMOKE_USERNAME))
        session.execute(text("DELETE FROM deployment_environment WHERE environment = 'staging' AND fixture_version = :version"), {"version": FIXTURE_VERSION})
        session.commit()
    finally:
        session.close()
    print("staging fixture cleanup complete")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("seed", "cleanup"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--confirm-staging", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "seed":
        seed(args.confirm_staging)
    else:
        cleanup(args.confirm_staging)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
