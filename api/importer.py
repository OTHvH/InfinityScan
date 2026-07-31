#!/usr/bin/env python3
"""
InfinityScan Import Pipeline (legacy wrapper)
=============================================
This module is a thin compatibility wrapper around the new transactional
import service in ``importing``.  It delegates to ``importing.adapters.local``,
``importing.manifest``, and ``importing.service``.

For the new CLI interface, use::

    python -m importing.cli scan /path/to/library
    python -m importing.cli import /path/to/library
    python -m importing.cli import /path/to/library --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importing.adapters.local import (
    LocalAdapter,
    stable_series_id,
    stable_chapter_id,
    slugify,
    extract_chapter_number,
    natural_key,
)
from importing.manifest import hash_manifest, manifest_to_json, manifest_to_dict
from importing.service import ImportService
from importing.validation import validate_manifest

LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
log = logging.getLogger("importer")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(format=LOG_FORMAT, level=level, stream=sys.stderr)


# Re-export for any external code that imports from this module directly
__all__ = [
    "LocalAdapter",
    "stable_series_id",
    "stable_chapter_id",
    "slugify",
    "extract_chapter_number",
    "natural_key",
    "hash_manifest",
    "manifest_to_json",
    "manifest_to_dict",
    "validate_manifest",
    "ImportService",
    "setup_logging",
    "build_parser",
    "main",
]


def run_pipeline(args: argparse.Namespace) -> None:
    """Legacy pipeline entry point that delegates to the transactional service."""
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
        log.warning("No series found - nothing to import.")
        return

    if args.manifest_only:
        manifest_path = Path(args.manifest_only)
        manifest_path.write_text(manifest_to_json(manifest), encoding="utf-8")
        log.info("Manifest written to %s", manifest_path)
        return

    if args.print_manifest:
        print(manifest_to_json(manifest))

    errors = validate_manifest(manifest)
    if errors:
        log.error("Manifest validation failed:")
        for e in errors:
            log.error("  %s", e)
        sys.exit(1)

    if args.dry_run:
        log.info("DRY-RUN mode: no DB writes or uploads will be performed")
        for s in manifest:
            log.info("  series '%s': %d chapters, %d pages",
                     s.slug, len(s.chapters), sum(len(c.pages) for c in s.chapters))
        return

    from database import _get_engine
    from sqlalchemy.orm import sessionmaker

    engine = _get_engine()
    if engine is None:
        log.error("DATABASE_URL is not configured.")
        sys.exit(1)

    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionFactory()
    try:
        service = ImportService.from_settings(session, dry_run=False)
        job = service.run(manifest)
        log.info("Import job %s finished: %s", job.id, job.status.value)
        log.info("  uploaded=%d  skipped=%d  failed=%d  pages=%d",
                 job.uploaded_count, job.skipped_count, job.failed_count, job.page_count)
    except Exception as exc:
        log.error("Import failed: %s", exc)
        session.rollback()
        sys.exit(1)
    finally:
        session.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="InfinityScan import pipeline (legacy wrapper)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("root", help="Root folder containing series sub-folders")
    p.add_argument("--dry-run", action="store_true",
                   help="Log what would happen without writing anything")
    p.add_argument("--series", metavar="NAME",
                   help="Only import series whose folder name contains NAME (case-insensitive)")
    p.add_argument("--language", default="en", metavar="LANG",
                   help="BCP-47 language code to assign to all chapters (default: en)")
    p.add_argument("--content-type", default="manga", choices=["manga", "manhua", "manhwa"],
                   help="Content type to assign to all imported series (default: manga)")
    p.add_argument("--manifest-only", metavar="PATH",
                   help="Write the manifest JSON to PATH and exit (no uploads or DB writes)")
    p.add_argument("--print-manifest", action="store_true",
                   help="Print the manifest JSON to stdout before uploading")
    p.add_argument("--skip-s3", action="store_true",
                   help="Skip S3 upload step (legacy flag, ignored)")
    p.add_argument("--skip-db", action="store_true",
                   help="Skip database upsert step (legacy flag, ignored)")
    p.add_argument("--debug", action="store_true",
                   help="Enable debug logging")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(logging.DEBUG if args.debug else logging.INFO)
    run_pipeline(args)


if __name__ == "__main__":
    main()
