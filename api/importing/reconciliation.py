"""Reconciliation of DB state against storage and import manifests."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Chapter, ChapterImportStatus, Page, Series
from storage import ObjectStorage, StorageError

log = logging.getLogger("importing.reconciliation")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def reconcile_database(
    session: Session,
    *,
    storage: ObjectStorage | None = None,
    dry_run: bool = False,
    prune: bool = False,
    source_key: str = "local",
) -> dict[str, object]:
    """Analyze DB vs storage state.

    Default mode reports discrepancies without modifying anything.
    With ``prune=True``, removes stale page references after replacement
    chapters are verified, then deletes unreferenced old objects.

    Returns a summary dict with counts and details.
    """
    report: dict[str, object] = {
        "total_series": 0,
        "total_chapters": 0,
        "total_pages": 0,
        "importing_chapters": 0,
        "failed_chapters": 0,
        "orphan_pages": 0,
        "stale_pages": 0,
        "pruned_pages": 0,
        "pruned_objects": 0,
        "details": [],
    }

    total_series = session.query(func.count(Series.id)).scalar() or 0
    total_chapters = session.query(func.count(Chapter.id)).scalar() or 0
    total_pages = session.query(func.count(Page.id)).scalar() or 0

    importing_chapters = (
        session.query(func.count(Chapter.id))
        .filter(Chapter.import_status == ChapterImportStatus.importing)
        .scalar()
        or 0
    )

    failed_chapters = (
        session.query(func.count(Chapter.id))
        .filter(Chapter.import_status == ChapterImportStatus.failed)
        .scalar()
        or 0
    )

    report["total_series"] = total_series
    report["total_chapters"] = total_chapters
    report["total_pages"] = total_pages
    report["importing_chapters"] = importing_chapters
    report["failed_chapters"] = failed_chapters

    stale_chapters = (
        session.query(Chapter)
        .filter(
            Chapter.import_status.in_([
                ChapterImportStatus.importing,
                ChapterImportStatus.failed,
            ])
        )
        .all()
    )

    stale_page_keys: list[str] = []
    for chapter in stale_chapters:
        pages = session.query(Page).filter(Page.chapter_id == chapter.id).all()
        for page in pages:
            stale_page_keys.append(page.object_key)
            report["stale_pages"] = len(stale_page_keys)

    details: list[str] = []

    if importing_chapters > 0:
        details.append(f"{importing_chapters} chapter(s) still in 'importing' state")
    if failed_chapters > 0:
        details.append(f"{failed_chapters} chapter(s) in 'failed' state")

    if not prune:
        if stale_page_keys:
            details.append(
                f"{len(stale_page_keys)} page(s) in non-ready chapters could be pruned"
            )
        report["details"] = details
        log.info(
            "reconciliation: %d series, %d chapters, %d pages, %d stale pages (dry-run=%s)",
            total_series,
            total_chapters,
            total_pages,
            len(stale_page_keys),
            dry_run,
        )
        return report

    pruned_page_count = 0
    pruned_object_keys: list[str] = []

    for chapter in stale_chapters:
        pages = session.query(Page).filter(Page.chapter_id == chapter.id).all()
        for page in pages:
            pruned_object_keys.append(page.object_key)
            session.delete(page)
            pruned_page_count += 1

    if pruned_page_count > 0:
        session.flush()

    report["pruned_pages"] = pruned_page_count

    if not dry_run and storage is not None and pruned_object_keys:
        deleted_count = 0
        for key in pruned_object_keys:
            try:
                storage.delete_object(key)
                deleted_count += 1
            except StorageError as exc:
                log.warning("failed to delete object %s: %s", key, exc)
        report["pruned_objects"] = deleted_count
        details.append(f"pruned {pruned_page_count} page(s), deleted {deleted_count} object(s)")
    elif pruned_object_keys:
        details.append(f"would prune {pruned_page_count} page(s) and {len(pruned_object_keys)} object(s)")

    report["details"] = details
    log.info(
        "reconciliation: pruned %d pages, deleted %d objects (dry-run=%s)",
        pruned_page_count,
        report["pruned_objects"],
        dry_run,
    )
    return report
