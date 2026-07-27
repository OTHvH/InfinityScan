"""CLI entry point for the transactional import service.

Usage:
    python -m importing.cli scan ROOT [options]
    python -m importing.cli import ROOT [options]
    python -m importing.cli resume JOB_ID [options]
    python -m importing.cli status JOB_ID
    python -m importing.cli reconcile [options]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from importing.adapters.local import LocalAdapter
from importing.manifest import manifest_to_json
from importing.service import ImportService
from importing.validation import validate_manifest

LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
log = logging.getLogger("importing.cli")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(format=LOG_FORMAT, level=level, stream=sys.stderr)


def cmd_scan(args: argparse.Namespace) -> None:
    """Scan a local root and print the manifest."""
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        log.error("Root directory does not exist: %s", root)
        sys.exit(1)

    adapter = LocalAdapter(
        root=root,
        language=args.language,
        content_type=args.content_type,
        series_filter=args.series or None,
    )
    manifest = adapter.scan()
    if not manifest:
        log.warning("No series found.")
        return

    if args.json:
        print(manifest_to_json(manifest))
    else:
        total_chapters = sum(len(s.chapters) for s in manifest)
        total_pages = sum(sum(len(c.pages) for c in s.chapters) for s in manifest)
        print(f"Found {len(manifest)} series, {total_chapters} chapters, {total_pages} pages:")
        for s in manifest:
            ch_info = ", ".join(
                f"ch{s.number} ({len(c.pages)}p)" for c in s.chapters
            )
            print(f"  {s.slug}: {ch_info}")


def cmd_import(args: argparse.Namespace) -> None:
    """Import from a local root into the database and object storage."""
    from database import get_settings, _get_engine
    from sqlalchemy.orm import sessionmaker

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        log.error("Root directory does not exist: %s", root)
        sys.exit(1)

    adapter = LocalAdapter(
        root=root,
        language=args.language,
        content_type=args.content_type,
        series_filter=args.series or None,
    )
    manifest = adapter.scan()
    if not manifest:
        log.warning("No series found. Nothing to import.")
        return

    errors = validate_manifest(manifest)
    if errors:
        log.error("Manifest validation failed:")
        for e in errors:
            log.error("  %s", e)
        sys.exit(1)

    settings = get_settings()
    engine = _get_engine()
    if engine is None:
        log.error("DATABASE_URL is not configured.")
        sys.exit(1)

    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionFactory()
    try:
        service = ImportService.from_settings(session, dry_run=args.dry_run, settings=settings)
        job = service.run(
            manifest,
            prune=args.prune,
        )
        print(f"\nImport job {job.id} finished with status: {job.status.value}")
        print(
            f"  uploaded={job.uploaded_count}  skipped={job.skipped_count}  "
            f"failed={job.failed_count}  pages={job.page_count}"
        )
        if job.error_summary:
            print(f"  error: {job.error_summary}")
    except Exception as exc:
        log.error("Import failed: %s", exc)
        session.rollback()
        sys.exit(1)
    finally:
        session.close()


def cmd_resume(args: argparse.Namespace) -> None:
    """Resume a pending/importing job."""
    from database import _get_engine
    from sqlalchemy.orm import sessionmaker

    job_id = uuid.UUID(args.job_id)
    engine = _get_engine()
    if engine is None:
        log.error("DATABASE_URL is not configured.")
        sys.exit(1)

    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionFactory()
    try:
        service = ImportService.from_settings(session, dry_run=args.dry_run)
        job = service.resume(job_id, prune=args.prune)
        print(f"Job {job.id} status: {job.status.value}")
    except (ValueError, RuntimeError) as exc:
        log.error("%s", exc)
        session.rollback()
        sys.exit(1)
    finally:
        session.close()


def cmd_status(args: argparse.Namespace) -> None:
    """Show the status of an import job."""
    from database import _get_engine
    from sqlalchemy.orm import sessionmaker

    job_id = uuid.UUID(args.job_id)
    engine = _get_engine()
    if engine is None:
        log.error("DATABASE_URL is not configured.")
        sys.exit(1)

    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionFactory()
    try:
        service = ImportService(session)
        job = service.status(job_id)
        print(f"Job:           {job.id}")
        print(f"Status:        {job.status.value}")
        print(f"Idempotency:   {job.idempotency_key}")
        print(f"Manifest hash: {job.manifest_hash or '(none)'}")
        print(f"Created:       {job.created_at}")
        print(f"Started:       {job.started_at or '(not started)'}")
        print(f"Finished:      {job.finished_at or '(not finished)'}")
        print(f"Uploaded:      {job.uploaded_count}")
        print(f"Skipped:       {job.skipped_count}")
        print(f"Failed:        {job.failed_count}")
        print(f"Pages:         {job.page_count}")
        if job.error_summary:
            print(f"Error:         {job.error_summary}")

        if args.json:
            items = [
                {
                    "id": str(item.id),
                    "source_reference": item.source_reference,
                    "object_key": item.object_key,
                    "sha256": item.sha256,
                    "status": item.status.value,
                    "error": item.error,
                }
                for item in job.items
            ]
            print(json.dumps({"job_id": str(job.id), "items": items}, indent=2))
    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(1)
    finally:
        session.close()


def cmd_reconcile(args: argparse.Namespace) -> None:
    """Reconcile DB state against storage."""
    from database import _get_engine
    from sqlalchemy.orm import sessionmaker

    engine = _get_engine()
    if engine is None:
        log.error("DATABASE_URL is not configured.")
        sys.exit(1)

    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionFactory()
    try:
        service = ImportService(session, dry_run=args.dry_run)
        report = service.reconcile(prune=args.prune)
        print(f"Total series:      {report['total_series']}")
        print(f"Total chapters:    {report['total_chapters']}")
        print(f"Total pages:       {report['total_pages']}")
        print(f"Importing:         {report['importing_chapters']}")
        print(f"Failed:            {report['failed_chapters']}")
        print(f"Stale pages:       {report['stale_pages']}")
        print(f"Pruned pages:      {report['pruned_pages']}")
        print(f"Pruned objects:    {report['pruned_objects']}")
        for detail in report.get("details", []):
            print(f"  - {detail}")
    finally:
        session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="InfinityScan transactional import service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Scan a local root and print the manifest")
    p_scan.add_argument("root", help="Root folder containing series sub-folders")
    p_scan.add_argument("--series", metavar="NAME", help="Only scan series whose folder name contains NAME")
    p_scan.add_argument("--language", default="en", metavar="LANG", help="BCP-47 language code (default: en)")
    p_scan.add_argument(
        "--content-type", default="manga", choices=["manga", "manhua", "manhwa"],
        help="Content type (default: manga)",
    )
    p_scan.add_argument("--json", action="store_true", help="Print manifest as JSON")
    p_scan.add_argument("--debug", action="store_true", help="Enable debug logging")
    p_scan.set_defaults(func=cmd_scan)

    p_import = sub.add_parser("import", help="Import from a local root")
    p_import.add_argument("root", help="Root folder containing series sub-folders")
    p_import.add_argument("--series", metavar="NAME", help="Only import series whose folder name contains NAME")
    p_import.add_argument("--language", default="en", metavar="LANG", help="BCP-47 language code (default: en)")
    p_import.add_argument(
        "--content-type", default="manga", choices=["manga", "manhua", "manhwa"],
        help="Content type (default: manga)",
    )
    p_import.add_argument("--dry-run", action="store_true", help="Perform no DB writes or uploads")
    p_import.add_argument("--prune", action="store_true", help="Remove stale pages and unreferenced objects")
    p_import.add_argument("--debug", action="store_true", help="Enable debug logging")
    p_import.set_defaults(func=cmd_import)

    p_resume = sub.add_parser("resume", help="Resume a pending/importing job")
    p_resume.add_argument("job_id", help="UUID of the import job to resume")
    p_resume.add_argument("--dry-run", action="store_true", help="Perform no DB writes or uploads")
    p_resume.add_argument("--prune", action="store_true", help="Remove stale pages and unreferenced objects")
    p_resume.add_argument("--debug", action="store_true", help="Enable debug logging")
    p_resume.set_defaults(func=cmd_resume)

    p_status = sub.add_parser("status", help="Show the status of an import job")
    p_status.add_argument("job_id", help="UUID of the import job")
    p_status.add_argument("--json", action="store_true", help="Print items as JSON")
    p_status.add_argument("--debug", action="store_true", help="Enable debug logging")
    p_status.set_defaults(func=cmd_status)

    p_reconcile = sub.add_parser("reconcile", help="Reconcile DB state against storage")
    p_reconcile.add_argument("--prune", action="store_true", help="Remove stale pages and unreferenced objects")
    p_reconcile.add_argument("--dry-run", action="store_true", help="Report without modifying anything")
    p_reconcile.add_argument("--debug", action="store_true", help="Enable debug logging")
    p_reconcile.set_defaults(func=cmd_reconcile)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(logging.DEBUG if args.debug else logging.INFO)
    args.func(args)


if __name__ == "__main__":
    main()
