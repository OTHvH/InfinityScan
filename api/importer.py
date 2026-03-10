#!/usr/bin/env python3
"""
InfinityScan Import Pipeline
============================
Scans a local folder tree, normalises series/chapter/page order, generates
a clean manifest, uploads page images to S3-compatible storage, and inserts
(or updates) metadata in PostgreSQL — all idempotently.

Folder layout expected
----------------------
<root>/
  <Series Title>/
    <Chapter N>/      (or "Chapter 001", "ch1", etc. — natural sort applied)
      001.jpg
      002.jpg
      ...

Usage
-----
  python importer.py /path/to/library
  python importer.py /path/to/library --dry-run
  python importer.py /path/to/library --series "Dragon Ball"
  python importer.py /path/to/library --manifest-only /tmp/manifest.json

Environment variables (override via .env or shell)
---------------------------------------------------
  DATABASE_URL      postgresql+psycopg://user:pass@host:5432/db
  S3_ENDPOINT_URL   https://s3.amazonaws.com  (or MinIO URL)
  S3_ACCESS_KEY     your-access-key
  S3_SECRET_KEY     your-secret-key
  S3_BUCKET         infinityscan-pages
  S3_PUBLIC_URL     https://cdn.example.com  (prefix for cover / page URLs)
"""

from __future__ import annotations

import argparse
import enum
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# ── Optional dependencies (installed only when needed) ────────────────────────
try:
    import boto3  # type: ignore
    from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    HAS_S3 = True
except ImportError:
    HAS_S3 = False

try:
    import psycopg  # type: ignore
    HAS_PG = True
except ImportError:
    HAS_PG = False

# ── Constants ─────────────────────────────────────────────────────────────────

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

CHAPTER_RE = re.compile(
    r"""
    (?:chapter|chap|ch|c)[\s._\-]*   # prefix
    (\d+(?:[._]\d+)?)                 # number (may be decimal like 1.5)
    |
    (\d+(?:[._]\d+)?)                 # bare number at start
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
log = logging.getLogger("importer")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(format=LOG_FORMAT, level=level, stream=sys.stderr)


# ── Natural sort ──────────────────────────────────────────────────────────────

def natural_key(s: str) -> list[object]:
    parts = re.split(r"(\d+)", s)
    out: list[object] = []
    for p in parts:
        out.append(int(p) if p.isdigit() else p.casefold())
    return out


# ── Chapter number extraction ─────────────────────────────────────────────────

def extract_chapter_number(name: str) -> float:
    m = CHAPTER_RE.search(name)
    if m:
        raw = m.group(1) or m.group(2)
        if raw:
            return float(raw.replace("_", "."))
    return 0.0


# ── Manifest data classes ─────────────────────────────────────────────────────

@dataclass
class PageEntry:
    page_number: int           # 1-based
    source_path: str           # absolute local path
    object_key: str            # destination S3 key
    width: Optional[int] = None
    height: Optional[int] = None
    file_size: Optional[int] = None
    content_hash: Optional[str] = None  # sha256 hex


@dataclass
class ChapterEntry:
    folder_name: str
    number: float
    title: Optional[str]
    language: str
    pages: list[PageEntry] = field(default_factory=list)


@dataclass
class SeriesEntry:
    folder_name: str
    slug: str
    title: str
    content_type: str          # manga | manhua | manhwa
    chapters: list[ChapterEntry] = field(default_factory=list)
    cover_source_path: Optional[str] = None
    cover_object_key: Optional[str] = None


# ── Slug generation ───────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


# ── Folder scanner ────────────────────────────────────────────────────────────

class LibraryScanner:
    """Scans a local folder tree and builds a list of SeriesEntry manifests."""

    def __init__(
        self,
        root: Path,
        language: str = "en",
        content_type: str = "manga",
        series_filter: Optional[str] = None,
    ) -> None:
        self.root = root.resolve()
        self.language = language
        self.content_type = content_type
        self.series_filter = series_filter.casefold() if series_filter else None

    def _is_image(self, p: Path) -> bool:
        return p.is_file() and p.suffix.casefold() in IMAGE_EXTS

    def _list_images(self, folder: Path) -> list[Path]:
        imgs = [p for p in folder.iterdir() if self._is_image(p)]
        imgs.sort(key=lambda p: natural_key(p.name))
        return imgs

    def _list_subdirs(self, folder: Path) -> list[Path]:
        dirs = [p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")]
        dirs.sort(key=lambda p: natural_key(p.name))
        return dirs

    def _build_object_key(self, *parts: str) -> str:
        return "/".join(parts)

    def _scan_chapter(
        self, series_slug: str, folder: Path, chapter_number: float
    ) -> ChapterEntry:
        pages_paths = self._list_images(folder)
        pages: list[PageEntry] = []
        for i, img in enumerate(pages_paths, start=1):
            stat = img.stat()
            key = self._build_object_key(
                "pages", series_slug, f"{chapter_number:.1f}", img.name
            )
            pages.append(
                PageEntry(
                    page_number=i,
                    source_path=str(img),
                    object_key=key,
                    file_size=stat.st_size,
                )
            )
        title_clean = re.sub(
            r"(?:chapter|chap|ch|c)[\s._\-]*\d+(?:[._]\d+)?",
            "",
            folder.name,
            flags=re.IGNORECASE,
        ).strip(" -_.")
        return ChapterEntry(
            folder_name=folder.name,
            number=chapter_number,
            title=title_clean or None,
            language=self.language,
            pages=pages,
        )

    def _scan_series(self, folder: Path) -> Optional[SeriesEntry]:
        chapter_dirs = self._list_subdirs(folder)
        chapters: list[ChapterEntry] = []
        cover_path: Optional[Path] = None

        # Pick the first image in the series root as cover
        top_imgs = self._list_images(folder)
        if top_imgs:
            cover_path = top_imgs[0]

        for ch_dir in chapter_dirs:
            if not self._list_images(ch_dir):
                continue  # skip empty sub-folders
            num = extract_chapter_number(ch_dir.name)
            chapters.append(self._scan_chapter(slugify(folder.name), ch_dir, num))

        if not chapters:
            # Check if the series root itself contains pages (flat structure)
            if top_imgs:
                log.debug("  '%s': flat series (no chapter folders), treating as ch1", folder.name)
                slug = slugify(folder.name)
                flat_chapter = ChapterEntry(
                    folder_name=folder.name,
                    number=1.0,
                    title=None,
                    language=self.language,
                    pages=[
                        PageEntry(
                            page_number=i,
                            source_path=str(img),
                            object_key=self._build_object_key("pages", slug, "1.0", img.name),
                            file_size=img.stat().st_size,
                        )
                        for i, img in enumerate(top_imgs, start=1)
                    ],
                )
                chapters = [flat_chapter]
            else:
                return None  # nothing to import

        # Sort chapters by number
        chapters.sort(key=lambda c: c.number)

        slug = slugify(folder.name)
        cover_key: Optional[str] = None
        if cover_path:
            cover_key = self._build_object_key("covers", slug, cover_path.name)

        return SeriesEntry(
            folder_name=folder.name,
            slug=slug,
            title=folder.name,
            content_type=self.content_type,
            chapters=chapters,
            cover_source_path=str(cover_path) if cover_path else None,
            cover_object_key=cover_key,
        )

    def scan(self) -> list[SeriesEntry]:
        series: list[SeriesEntry] = []
        top_dirs = self._list_subdirs(self.root)
        log.info("Scanning '%s' — found %d top-level directories", self.root, len(top_dirs))

        for folder in top_dirs:
            if self.series_filter and self.series_filter not in folder.name.casefold():
                continue
            log.info("  → %s", folder.name)
            entry = self._scan_series(folder)
            if entry is None:
                log.warning("    Skipped (no images found)")
                continue
            total_pages = sum(len(ch.pages) for ch in entry.chapters)
            log.info(
                "    %d chapter(s), %d page(s)", len(entry.chapters), total_pages
            )
            series.append(entry)

        log.info("Scan complete: %d series", len(series))
        return series


# ── Manifest generation ───────────────────────────────────────────────────────

def build_manifest(series_list: list[SeriesEntry]) -> dict[str, Any]:
    def _page_dict(p: PageEntry) -> dict[str, Any]:
        return {
            "page_number": p.page_number,
            "source_path": p.source_path,
            "object_key": p.object_key,
            "file_size": p.file_size,
        }

    def _chapter_dict(c: ChapterEntry) -> dict[str, Any]:
        return {
            "folder_name": c.folder_name,
            "number": c.number,
            "title": c.title,
            "language": c.language,
            "page_count": len(c.pages),
            "pages": [_page_dict(p) for p in c.pages],
        }

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "series_count": len(series_list),
        "series": [
            {
                "folder_name": s.folder_name,
                "slug": s.slug,
                "title": s.title,
                "content_type": s.content_type,
                "cover_source_path": s.cover_source_path,
                "cover_object_key": s.cover_object_key,
                "chapter_count": len(s.chapters),
                "chapters": [_chapter_dict(c) for c in s.chapters],
            }
            for s in series_list
        ],
    }


# ── Retry helper ──────────────────────────────────────────────────────────────

def with_retry(
    fn,
    *args,
    retries: int = 3,
    delay: float = 1.5,
    label: str = "",
    **kwargs,
) -> Any:
    last_exc: Exception = RuntimeError("unreachable")
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                wait = delay * (2 ** (attempt - 1))
                log.warning(
                    "  [retry %d/%d] %s failed: %s — retrying in %.1fs",
                    attempt, retries, label, exc, wait,
                )
                time.sleep(wait)
            else:
                log.error("  [retry %d/%d] %s failed: %s — giving up", attempt, retries, label, exc)
    raise last_exc


# ── S3 uploader ───────────────────────────────────────────────────────────────

class S3Uploader:
    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        dry_run: bool = False,
    ) -> None:
        if not HAS_S3:
            raise RuntimeError("boto3 is required for S3 uploads: pip install boto3")
        self.bucket = bucket
        self.dry_run = dry_run
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

    def object_exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise

    def upload_file(self, local_path: Path, key: str) -> bool:
        """Upload a file to S3. Returns True if uploaded, False if skipped."""
        if self.object_exists(key):
            log.debug("    SKIP (exists) %s", key)
            return False

        mime, _ = mimetypes.guess_type(str(local_path))
        extra = {"ContentType": mime or "application/octet-stream"}

        if self.dry_run:
            log.info("    DRY-RUN upload: %s → s3://%s/%s", local_path.name, self.bucket, key)
            return True

        def _do_upload():
            self._client.upload_file(str(local_path), self.bucket, key, ExtraArgs=extra)

        with_retry(_do_upload, retries=3, delay=2.0, label=f"upload {key}")
        log.debug("    UPLOADED %s", key)
        return True


# ── PostgreSQL inserter ───────────────────────────────────────────────────────

class PGInserter:
    """Idempotent upsert of series/chapter/page records into PostgreSQL."""

    def __init__(self, dsn: str, dry_run: bool = False) -> None:
        if not HAS_PG:
            raise RuntimeError("psycopg is required: pip install 'psycopg[binary]'")
        self.dsn = dsn
        self.dry_run = dry_run

    def _connect(self):
        return psycopg.connect(self.dsn, autocommit=False)

    def _upsert_series(self, cur, entry: SeriesEntry) -> uuid.UUID:
        cur.execute(
            """
            INSERT INTO series (id, slug, title, content_type, cover_object_key,
                                default_reading_mode, status)
            VALUES (gen_random_uuid(), %(slug)s, %(title)s,
                    %(content_type)s::content_type_enum,
                    %(cover_object_key)s, 'paged', 'ongoing')
            ON CONFLICT (slug) DO UPDATE
                SET title             = EXCLUDED.title,
                    cover_object_key  = COALESCE(EXCLUDED.cover_object_key, series.cover_object_key),
                    updated_at        = now()
            RETURNING id
            """,
            {
                "slug": entry.slug,
                "title": entry.title,
                "content_type": entry.content_type,
                "cover_object_key": entry.cover_object_key,
            },
        )
        row = cur.fetchone()
        return row[0]

    def _upsert_chapter(
        self, cur, series_id: uuid.UUID, ch: ChapterEntry
    ) -> uuid.UUID:
        cur.execute(
            """
            INSERT INTO chapters (id, series_id, number, title, language, page_count)
            VALUES (gen_random_uuid(), %(series_id)s, %(number)s,
                    %(title)s, %(language)s, %(page_count)s)
            ON CONFLICT (series_id, number, language) DO UPDATE
                SET title      = EXCLUDED.title,
                    page_count = EXCLUDED.page_count
            RETURNING id
            """,
            {
                "series_id": series_id,
                "number": ch.number,
                "title": ch.title,
                "language": ch.language,
                "page_count": len(ch.pages),
            },
        )
        row = cur.fetchone()
        return row[0]

    def _upsert_page(self, cur, chapter_id: uuid.UUID, page: PageEntry) -> None:
        cur.execute(
            """
            INSERT INTO pages (id, chapter_id, page_number, object_key,
                               width, height, file_size)
            VALUES (gen_random_uuid(), %(chapter_id)s, %(page_number)s,
                    %(object_key)s, %(width)s, %(height)s, %(file_size)s)
            ON CONFLICT (chapter_id, page_number) DO UPDATE
                SET object_key = EXCLUDED.object_key,
                    file_size  = EXCLUDED.file_size
            """,
            {
                "chapter_id": chapter_id,
                "page_number": page.page_number,
                "object_key": page.object_key,
                "width": page.width,
                "height": page.height,
                "file_size": page.file_size,
            },
        )

    def insert(self, series_list: list[SeriesEntry]) -> None:
        if self.dry_run:
            for s in series_list:
                log.info("DRY-RUN DB upsert: series '%s' (%d chapters)", s.slug, len(s.chapters))
            return

        with self._connect() as conn:
            with conn.cursor() as cur:
                for s in series_list:
                    series_id = self._upsert_series(cur, s)
                    log.info("  DB series '%s' → %s", s.slug, series_id)
                    for ch in s.chapters:
                        chapter_id = self._upsert_chapter(cur, series_id, ch)
                        for page in ch.pages:
                            self._upsert_page(cur, chapter_id, page)
                    log.info(
                        "    %d chapters, %d pages",
                        len(s.chapters),
                        sum(len(c.pages) for c in s.chapters),
                    )
            conn.commit()


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(args: argparse.Namespace) -> None:
    dry_run: bool = args.dry_run
    root = Path(args.root).expanduser().resolve()

    if not root.is_dir():
        log.error("Root directory does not exist: %s", root)
        sys.exit(1)

    # 1. Scan
    scanner = LibraryScanner(
        root=root,
        language=args.language,
        content_type=args.content_type,
        series_filter=args.series or None,
    )
    series_list = scanner.scan()

    if not series_list:
        log.warning("No series found — nothing to import.")
        return

    # 2. Generate manifest
    manifest = build_manifest(series_list)
    if args.manifest_only:
        manifest_path = Path(args.manifest_only)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info("Manifest written to %s", manifest_path)
        return

    if args.print_manifest:
        print(json.dumps(manifest, indent=2, ensure_ascii=False))

    # 3. Upload to S3
    endpoint = os.environ.get("S3_ENDPOINT_URL", "")
    access_key = os.environ.get("S3_ACCESS_KEY", "")
    secret_key = os.environ.get("S3_SECRET_KEY", "")
    bucket = os.environ.get("S3_BUCKET", "infinityscan-pages")

    if not args.skip_s3:
        if not HAS_S3:
            log.error("boto3 not installed — cannot upload to S3. Install with: pip install boto3")
            sys.exit(1)
        uploader = S3Uploader(
            endpoint_url=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            dry_run=dry_run,
        )
        log.info("Uploading pages to S3 bucket '%s'…", bucket)
        uploaded = skipped = 0
        for s in series_list:
            # Cover
            if s.cover_source_path and s.cover_object_key:
                if uploader.upload_file(Path(s.cover_source_path), s.cover_object_key):
                    uploaded += 1
                else:
                    skipped += 1
            for ch in s.chapters:
                for page in ch.pages:
                    if uploader.upload_file(Path(page.source_path), page.object_key):
                        uploaded += 1
                    else:
                        skipped += 1
        log.info("S3 upload done — %d uploaded, %d skipped (already existed)", uploaded, skipped)
    else:
        log.info("Skipping S3 upload (--skip-s3)")

    # 4. Insert into PostgreSQL
    db_url = os.environ.get("DATABASE_URL", "")
    if not args.skip_db:
        if not db_url:
            log.error("DATABASE_URL environment variable is not set.")
            sys.exit(1)
        if not HAS_PG:
            log.error("psycopg not installed. Install with: pip install 'psycopg[binary]'")
            sys.exit(1)
        log.info("Upserting metadata into PostgreSQL…")
        inserter = PGInserter(dsn=db_url, dry_run=dry_run)
        inserter.insert(series_list)
        log.info("Database upsert complete.")
    else:
        log.info("Skipping database upsert (--skip-db)")

    log.info("Import pipeline finished%s.", " (DRY-RUN)" if dry_run else "")


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="InfinityScan import pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("root", help="Root folder containing series sub-folders")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would happen without writing anything",
    )
    p.add_argument(
        "--series",
        metavar="NAME",
        help="Only import series whose folder name contains NAME (case-insensitive)",
    )
    p.add_argument(
        "--language",
        default="en",
        metavar="LANG",
        help="BCP-47 language code to assign to all chapters (default: en)",
    )
    p.add_argument(
        "--content-type",
        default="manga",
        choices=["manga", "manhua", "manhwa"],
        help="Content type to assign to all imported series (default: manga)",
    )
    p.add_argument(
        "--manifest-only",
        metavar="PATH",
        help="Write the manifest JSON to PATH and exit (no uploads or DB writes)",
    )
    p.add_argument(
        "--print-manifest",
        action="store_true",
        help="Print the manifest JSON to stdout before uploading",
    )
    p.add_argument(
        "--skip-s3",
        action="store_true",
        help="Skip S3 upload step",
    )
    p.add_argument(
        "--skip-db",
        action="store_true",
        help="Skip PostgreSQL upsert step",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(logging.DEBUG if args.debug else logging.INFO)
    run_pipeline(args)


if __name__ == "__main__":
    main()
