"""Prove Phase 3 rejected-file behavior against PostgreSQL and object storage."""

from __future__ import annotations

import argparse
import json
import tempfile
import uuid
from pathlib import Path

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from importing.adapters.local import LocalAdapter, stable_series_id
from importing.service import ImportService
from models import ImportJob, ImportJobItem, ImportJobItemStatus, Page, Series
from settings import get_settings


def write_jpeg(path: Path) -> None:
    Image.new("RGB", (32, 48), (30, 60, 90)).save(path, "JPEG")


def object_count(storage: object, series_id: uuid.UUID) -> int:
    client = getattr(storage, "_client")
    bucket = getattr(storage, "bucket")
    response = client.list_objects_v2(Bucket=bucket, Prefix=f"series/{series_id}/")
    return int(response.get("KeyCount", 0))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def assert_case(
    db: Session,
    service: ImportService,
    storage: object,
    root: Path,
    slug: str,
    *,
    expected_pages: int,
    expected_objects: int,
    expected_status: ImportJobItemStatus | None,
    expected_error: str | None,
) -> dict[str, object]:
    series_id = stable_series_id(slug)
    before = object_count(storage, series_id)
    manifest = LocalAdapter(root).scan()
    require(len(manifest) == 1 and manifest[0].slug == slug, f"unexpected manifest for {slug}")
    job = service.run(manifest)
    db.refresh(job)
    series = db.query(Series).filter(Series.slug == slug).one()
    pages = db.query(Page).join(Page.chapter).filter_by(series_id=series.id).all()
    require(len(pages) == expected_pages, f"{slug}: expected {expected_pages} pages, found {len(pages)}")
    after = object_count(storage, series_id)
    require(after - before == expected_objects, f"{slug}: unexpected object count change {after - before}")

    rejected = db.query(ImportJobItem).filter(
        ImportJobItem.job_id == job.id,
        ImportJobItem.status.in_([ImportJobItemStatus.skipped, ImportJobItemStatus.failed]),
    ).all()
    observed_status: ImportJobItemStatus | None = None
    if expected_status is None:
        require(rejected == [], f"{slug}: unexpected rejected item")
    else:
        require(len(rejected) == 1, f"{slug}: expected one rejected item, found {len(rejected)}")
        item = rejected[0]
        observed_status = item.status
        require(item.status == expected_status, f"{slug}: unexpected rejection status {item.status}")
        require(item.object_key == "", f"{slug}: rejected item has an object key")
        require(item.sha256 is None, f"{slug}: rejected item has a digest")
        require(bool(item.error and expected_error and expected_error in item.error), f"{slug}: rejection error is incomplete")
        require(str(root) not in (item.error or ""), f"{slug}: rejection error leaked the import root")
        require("secret" not in (item.error or "").lower(), f"{slug}: rejection error leaked a secret")

    return {
        "slug": slug,
        "job": str(job.id),
        "pages": len(pages),
        "objectsAdded": after - before,
        "rejectedStatus": observed_status.value if observed_status else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    suffix = uuid.uuid4().hex[:10]
    results: dict[str, object] = {}

    with tempfile.TemporaryDirectory(prefix="infinityscan-phase3-rejections-") as raw_tmp:
        tmp = Path(raw_tmp)
        outside = tmp.parent / f"phase3-external-{suffix}.jpg"
        write_jpeg(outside)
        try:
            with Session(engine) as db:
                service = ImportService.from_settings(db, settings=settings)
                storage = service._storage
                require(storage is not None, "object storage is unavailable")

                unsupported_slug = f"phase3-unsupported-{suffix}"
                unsupported_root = tmp / "unsupported"
                unsupported_chapter = unsupported_root / unsupported_slug / "chapter_001"
                unsupported_chapter.mkdir(parents=True)
                write_jpeg(unsupported_chapter / "001.jpg")
                (unsupported_chapter / "002.txt").write_text("not an image", encoding="utf-8")
                results["unsupported"] = assert_case(
                    db, service, storage, unsupported_root, unsupported_slug,
                    expected_pages=1,
                    expected_objects=1,
                    expected_status=ImportJobItemStatus.skipped,
                    expected_error="not an image candidate",
                )

                external_slug = f"phase3-external-link-{suffix}"
                external_root = tmp / "external-link"
                external_chapter = external_root / external_slug / "chapter_001"
                external_chapter.mkdir(parents=True)
                write_jpeg(external_chapter / "001.jpg")
                (external_chapter / "002.jpg").symlink_to(outside)
                results["externalSymlink"] = assert_case(
                    db, service, storage, external_root, external_slug,
                    expected_pages=1,
                    expected_objects=1,
                    expected_status=ImportJobItemStatus.failed,
                    expected_error="outside the approved import root",
                )

                internal_slug = f"phase3-internal-link-{suffix}"
                internal_root = tmp / "internal-link"
                internal_chapter = internal_root / internal_slug / "chapter_001"
                internal_chapter.mkdir(parents=True)
                real_image = internal_chapter / "001.jpg"
                write_jpeg(real_image)
                (internal_chapter / "002.jpg").symlink_to(real_image)
                results["internalSymlink"] = assert_case(
                    db, service, storage, internal_root, internal_slug,
                    expected_pages=2,
                    expected_objects=2,
                    expected_status=None,
                    expected_error=None,
                )

                oversized_slug = f"phase3-oversized-{suffix}"
                oversized_root = tmp / "oversized"
                oversized_chapter = oversized_root / oversized_slug / "chapter_001"
                oversized_chapter.mkdir(parents=True)
                write_jpeg(oversized_chapter / "001.jpg")
                (oversized_chapter / "002.jpg").write_bytes(b"x" * (26 * 1024 * 1024))
                results["oversized"] = assert_case(
                    db, service, storage, oversized_root, oversized_slug,
                    expected_pages=1,
                    expected_objects=1,
                    expected_status=ImportJobItemStatus.failed,
                    expected_error="maximum byte size",
                )

                require(db.query(ImportJob).filter(ImportJob.id.in_([
                    uuid.UUID(str(value["job"])) for value in results.values()
                ])).count() == 4, "expected four rejection verification jobs")
        finally:
            outside.unlink(missing_ok=True)

    args.output.write_text(json.dumps(results, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
