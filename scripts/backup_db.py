#!/usr/bin/env python3
"""Safe PostgreSQL and object-storage backup, restore, and status tooling."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
except ImportError:  # pragma: no cover - exercised on CLI-only backup hosts
    psycopg = None
    sql = None
    conninfo_to_dict = None
    make_conninfo = None


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "api"
BACKUP_FORMAT_VERSION = 2
SUPPORTED_BACKUP_FORMATS = {1, 2}
ARTIFACT_SUFFIXES = (".dump", ".dump.age")
SAFE_DATABASE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")
TEST_DATABASE_NAME = re.compile(
    r"(?:^|[_-])(test|tests|tmp|temporary)(?:$|[_-])", re.IGNORECASE
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CANONICAL_OBJECT_HASH = re.compile(r"(?:cover-|[0-9]{5}-)([0-9a-f]{64})\.[a-z0-9]{1,16}$")
FORBIDDEN_METADATA_KEYS = re.compile(
    r"(?:database[_-]?url|dsn|password|secret|credential|private[_-]?key|identity|token|presign)",
    re.IGNORECASE,
)
FORBIDDEN_METADATA_VALUES = re.compile(
    r"(?:postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^\s]+:[^\s]+@|"
    r"-----BEGIN [^-]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|"
    r"(?:password|passwd|secret|credential|token)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
DEFAULT_MAX_OBJECT_BYTES = 1024 * 1024 * 1024


class BackupError(RuntimeError):
    """An expected, safe-to-report backup operation failure."""


@dataclass
class ObjectExpectation:
    key: str
    sha256: str | None = None
    byte_size: int | None = None
    mime_type: str | None = None


def _require_psycopg() -> None:
    if psycopg is None or sql is None or conninfo_to_dict is None or make_conninfo is None:
        raise BackupError("psycopg is required for backup and restore commands")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run(command: list[str], *, env: dict[str, str], label: str) -> None:
    try:
        result = subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise BackupError(f"{label} could not be started") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] or [""]
        safe_detail = re.sub(
            r"(?i)(password|passwd|secret|token)=\S+", r"\1=<redacted>", detail[0]
        )
        raise BackupError(
            f"{label} failed with exit code {result.returncode}: {safe_detail[:240]}"
        )


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return safe[:63] or "database"


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _application_version() -> str | None:
    if os.environ.get("APP_VERSION"):
        return os.environ["APP_VERSION"]
    settings_path = API_ROOT / "settings.py"
    try:
        text = settings_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(
        r"^\s*app_version:\s*str\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE
    )
    return match.group(1) if match else None


def _normalized_database_url(value: str) -> str:
    if value.startswith("postgresql+"):
        return "postgresql://" + value.split("://", 1)[1]
    return value


def _connection_parts(database_url: str) -> tuple[str, str | None]:
    _require_psycopg()
    normalized = _normalized_database_url(database_url)
    parts = conninfo_to_dict(normalized)
    password = parts.pop("password", None)
    return make_conninfo(**parts), password


def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("\n", "")


class DatabaseTarget:
    def __init__(self, database_url: str):
        _require_psycopg()
        if not database_url:
            raise BackupError("DATABASE_URL is required")
        self.database_url = _normalized_database_url(database_url)
        try:
            self.safe_dsn, self.password = _connection_parts(database_url)
            self._parts = conninfo_to_dict(self.database_url)
        except Exception as exc:
            raise BackupError("DATABASE_URL is invalid") from exc

    def subprocess_environment(self, temporary_directory: Path) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("DATABASE_URL", None)
        environment.pop("PGPASSWORD", None)
        if self.password is not None:
            pgpass = temporary_directory / "pgpass"
            host = self._parts.get("host") or "*"
            port = self._parts.get("port") or "*"
            database = self._parts.get("dbname") or "*"
            user = self._parts.get("user") or "*"
            pgpass.write_text(
                ":".join(
                    _pgpass_escape(value) for value in (host, port, database, user, self.password)
                )
                + "\n",
                encoding="utf-8",
            )
            pgpass.chmod(0o600)
            environment["PGPASSFILE"] = str(pgpass)
        else:
            environment.pop("PGPASSFILE", None)
        return environment

    def connect(self):
        try:
            return psycopg.connect(self.database_url)
        except Exception as exc:
            raise BackupError("database connection failed safely") from exc


@contextmanager
def _exported_snapshot(target: DatabaseTarget) -> Iterator[tuple[Any, str]]:
    try:
        with target.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                cursor.execute("SELECT pg_export_snapshot()")
                snapshot_id = str(cursor.fetchone()[0])
            if not snapshot_id or any(character.isspace() for character in snapshot_id):
                raise BackupError("PostgreSQL returned an invalid exported snapshot")
            yield connection, snapshot_id
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("PostgreSQL snapshot export failed safely") from exc


def _query_metadata(target: DatabaseTarget, connection: Any | None = None) -> dict[str, Any]:
    if connection is None:
        try:
            with target.connect() as owned_connection:
                return _query_metadata(target, owned_connection)
        except BackupError:
            raise
        except Exception as exc:
            raise BackupError("database metadata query failed safely") from exc

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_setting('server_version')")
            database_name, postgres_version = cursor.fetchone()
            cursor.execute("SELECT version_num FROM alembic_version ORDER BY version_num")
            revisions = [row[0] for row in cursor.fetchall()]

            critical_tables = (
                "users",
                "series",
                "chapters",
                "pages",
                "import_jobs",
                "import_job_items",
                "refresh_sessions",
                "audit_events",
            )
            counts: dict[str, int] = {}
            for table_name in critical_tables:
                cursor.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table_name))
                )
                counts[table_name] = int(cursor.fetchone()[0])

            cursor.execute(
                """
                SELECT
                    count(DISTINCT object_key) FILTER (
                        WHERE object_key IS NOT NULL AND btrim(object_key) <> ''
                    ),
                    count(*) FILTER (WHERE integrity_status = 'verified'),
                    coalesce(sum(file_size) FILTER (
                        WHERE object_key IS NOT NULL AND btrim(object_key) <> ''
                    ), 0),
                    count(*) FILTER (
                        WHERE integrity_status = 'verified'
                          AND (object_key IS NULL OR btrim(object_key) = '' OR sha256 IS NULL
                               OR width IS NULL OR height IS NULL OR file_size IS NULL
                               OR file_size <= 0 OR mime_type IS NULL OR btrim(mime_type) = ''
                               OR file_extension IS NULL OR btrim(file_extension) = ''
                               OR verified_at IS NULL)
                    )
                FROM pages
                """
            )
            referenced_count, verified_count, referenced_bytes, violations = cursor.fetchone()
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("database metadata query failed safely") from exc

    return {
        "database_logical_name": database_name,
        "postgresql_version": postgres_version,
        "alembic_revision": revisions[0] if len(revisions) == 1 else revisions or None,
        "critical_table_counts": counts,
        "storage_inventory": {
            "referenced_object_count": int(referenced_count or 0),
            "verified_page_count": int(verified_count or 0),
            "total_referenced_bytes": int(referenced_bytes or 0),
            "integrity_quick_check": {
                "result": "pass" if not violations else "fail",
                "violations": int(violations or 0),
            },
        },
    }


def _validate_object_key(key: str) -> None:
    if not key or key.startswith("/") or "\x00" in key or "\r" in key or "\n" in key:
        raise BackupError("database contains an invalid object key")


def _merge_object_expectation(
    expectations: dict[str, ObjectExpectation],
    *,
    key: str,
    sha256: str | None,
    byte_size: int | None,
    mime_type: str | None,
) -> None:
    _validate_object_key(key)
    if sha256 is not None and not SHA256.fullmatch(str(sha256)):
        raise BackupError("database contains an invalid object checksum")
    if byte_size is not None and (not isinstance(byte_size, int) or byte_size < 0):
        raise BackupError("database contains an invalid object byte size")
    if mime_type is not None and (not mime_type or "\r" in mime_type or "\n" in mime_type):
        raise BackupError("database contains an invalid object MIME type")

    current = expectations.setdefault(key, ObjectExpectation(key=key))
    for field_name, incoming in (
        ("sha256", str(sha256) if sha256 is not None else None),
        ("byte_size", byte_size),
        ("mime_type", str(mime_type) if mime_type is not None else None),
    ):
        existing = getattr(current, field_name)
        if existing is not None and incoming is not None and existing != incoming:
            raise BackupError(f"conflicting expectations for object {key}")
        if existing is None and incoming is not None:
            setattr(current, field_name, incoming)


def _query_object_expectations(connection: Any) -> list[ObjectExpectation]:
    expectations: dict[str, ObjectExpectation] = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) FROM import_jobs
                WHERE status IN ('pending', 'scanning', 'uploading', 'verifying')
                """
            )
            if int(cursor.fetchone()[0]):
                raise BackupError("active imports prevent a complete object backup")

            cursor.execute(
                """
                SELECT object_key, sha256, file_size, mime_type
                FROM pages
                WHERE object_key IS NOT NULL AND btrim(object_key) <> ''
                ORDER BY object_key
                """
            )
            for key, digest, size, mime_type in cursor.fetchall():
                if not SHA256.fullmatch(str(digest or "")) or size is None or int(size) < 1:
                    raise BackupError("a referenced page lacks complete object expectations")
                if not mime_type:
                    raise BackupError("a referenced page lacks complete object expectations")
                _merge_object_expectation(
                    expectations,
                    key=str(key),
                    sha256=str(digest),
                    byte_size=int(size),
                    mime_type=str(mime_type),
                )

            cursor.execute(
                """
                SELECT cover_object_key
                FROM series
                WHERE cover_object_key IS NOT NULL AND btrim(cover_object_key) <> ''
                ORDER BY cover_object_key
                """
            )
            for (key,) in cursor.fetchall():
                key_text = str(key)
                match = CANONICAL_OBJECT_HASH.search(key_text)
                _merge_object_expectation(
                    expectations,
                    key=key_text,
                    sha256=match.group(1) if match else None,
                    byte_size=None,
                    mime_type=None,
                )

            cursor.execute(
                """
                SELECT object_key, sha256, byte_size, mime_type
                FROM import_job_items
                WHERE status IN ('succeeded', 'skipped')
                  AND object_key IS NOT NULL AND btrim(object_key) <> ''
                ORDER BY object_key
                """
            )
            for key, digest, size, mime_type in cursor.fetchall():
                _merge_object_expectation(
                    expectations,
                    key=str(key),
                    sha256=str(digest) if digest is not None else None,
                    byte_size=int(size) if size is not None else None,
                    mime_type=str(mime_type) if mime_type is not None else None,
                )
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("object backup scope query failed safely") from exc
    return [expectations[key] for key in sorted(expectations)]


def _object_storage_for_bucket(bucket: str):
    if not bucket or "\r" in bucket or "\n" in bucket:
        raise BackupError("an explicit valid object-storage bucket is required")
    enabled = os.environ.get("OBJECT_STORAGE_ENABLED", "").lower() in {"1", "true", "yes"}
    if not enabled:
        raise BackupError("object storage must be enabled for complete backup or restore")
    if str(API_ROOT) not in sys.path:
        sys.path.insert(0, str(API_ROOT))
    try:
        from storage import S3CompatibleStorage, S3StorageConfig

        config = S3StorageConfig(
            endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
            region=os.environ.get("S3_REGION", "auto"),
            bucket=bucket,
            access_key_id=os.environ.get("S3_ACCESS_KEY_ID", ""),
            secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", ""),
            force_path_style=os.environ.get("S3_FORCE_PATH_STYLE", "").lower()
            in {"1", "true", "yes"},
            connect_timeout=float(os.environ.get("S3_CONNECT_TIMEOUT", "5")),
            read_timeout=float(os.environ.get("S3_READ_TIMEOUT", "30")),
            max_retries=int(os.environ.get("S3_MAX_RETRIES", "4")),
        )
        storage = S3CompatibleStorage(config)
        if not storage.health_check():
            raise BackupError("object-storage target bucket is unavailable")
        return storage
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("object storage is unavailable or incorrectly configured") from exc


def _metadata_matches_expectation(metadata: Any, expectation: ObjectExpectation) -> bool:
    return (
        (expectation.sha256 is None or metadata.sha256 in (None, expectation.sha256))
        and (expectation.byte_size is None or metadata.byte_size == expectation.byte_size)
        and (expectation.mime_type is None or metadata.mime_type == expectation.mime_type)
    )


def _capture_objects(
    storage: Any,
    expectations: list[ObjectExpectation],
    directory: Path,
    tar_path: Path,
    *,
    max_object_bytes: int,
) -> list[dict[str, Any]]:
    directory.mkdir()
    records: list[dict[str, Any]] = []
    try:
        for index, expectation in enumerate(expectations):
            metadata = storage.head_object(expectation.key)
            if metadata is None:
                raise BackupError(f"referenced object is missing: {expectation.key}")
            if not _metadata_matches_expectation(metadata, expectation):
                raise BackupError(f"referenced object metadata is inconsistent: {expectation.key}")
            expected_sha256 = expectation.sha256 or metadata.sha256
            expected_size = (
                expectation.byte_size
                if expectation.byte_size is not None
                else metadata.byte_size
            )
            expected_mime = expectation.mime_type or metadata.mime_type
            if expected_sha256 is not None and not SHA256.fullmatch(expected_sha256):
                raise BackupError(f"referenced object has no valid checksum: {expectation.key}")
            if expected_size < 0 or expected_size > max_object_bytes:
                raise BackupError(f"referenced object exceeds the download limit: {expectation.key}")
            if not expected_mime or "\r" in expected_mime or "\n" in expected_mime:
                raise BackupError(f"referenced object has no valid MIME type: {expectation.key}")
            if not metadata.etag:
                raise BackupError(f"referenced object has no stable ETag: {expectation.key}")

            member = str(index)
            destination = directory / member
            result = storage.download_file(
                expectation.key,
                destination,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
                max_bytes=max_object_bytes,
            )
            if result.mime_type != expected_mime or not result.etag:
                raise BackupError(f"referenced object changed during download: {expectation.key}")
            if result.etag != metadata.etag:
                raise BackupError(f"referenced object changed during download: {expectation.key}")
            after = storage.head_object(expectation.key)
            if (
                after is None
                or after.etag != result.etag
                or after.byte_size != result.byte_size
                or after.sha256 not in (None, result.sha256)
                or after.mime_type != expected_mime
            ):
                raise BackupError(f"referenced object changed during download: {expectation.key}")
            records.append(
                {
                    "member": member,
                    "key": expectation.key,
                    "sha256": result.sha256,
                    "byte_size": result.byte_size,
                    "mime_type": expected_mime,
                    "source_etag": result.etag,
                }
            )

        # A final HEAD closes the window between early downloads and tar publication.
        for record in records:
            metadata = storage.head_object(record["key"])
            if (
                metadata is None
                or metadata.etag != record["source_etag"]
                or metadata.byte_size != record["byte_size"]
                or metadata.sha256 not in (None, record["sha256"])
                or metadata.mime_type != record["mime_type"]
            ):
                raise BackupError(f"referenced object changed during backup: {record['key']}")

        with tarfile.open(tar_path, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for record in records:
                path = directory / record["member"]
                info = tarfile.TarInfo(record["member"])
                info.size = record["byte_size"]
                info.mode = 0o600
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("object backup failed safely") from exc
    return records


def _require_safe_database_name(database_name: str, *, allow_test_database: bool) -> None:
    if not SAFE_DATABASE_NAME.fullmatch(database_name):
        raise BackupError("database logical name is ambiguous or unsafe")
    if TEST_DATABASE_NAME.search(database_name) and not allow_test_database:
        raise BackupError(
            "temporary/test databases are excluded; use --allow-test-database explicitly"
        )


def _age_recipient(args: argparse.Namespace) -> str | None:
    return (
        args.age_recipient
        or os.environ.get("AGE_RECIPIENT")
        or os.environ.get("BACKUP_AGE_RECIPIENT")
    )


def _manifest_path(artifact: Path) -> Path:
    return artifact.with_name(artifact.name + ".manifest.json")


def _reject_secret_metadata(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if FORBIDDEN_METADATA_KEYS.search(str(key)):
                raise BackupError("manifest contains a forbidden secret-bearing field")
            _reject_secret_metadata(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _reject_secret_metadata(nested)
        return
    if isinstance(value, str) and FORBIDDEN_METADATA_VALUES.search(value):
        raise BackupError("manifest contains secret-bearing content")


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    _reject_secret_metadata(value)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.chmod(0o640)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _run_pg_restore_list(archive: Path, environment: dict[str, str]) -> None:
    _run(["pg_restore", "--list", str(archive)], env=environment, label="pg_restore --list")


def _encrypt_artifact(
    source: Path,
    destination: Path,
    recipient: str | None,
    environment: dict[str, str],
    *,
    label: str,
) -> None:
    if recipient:
        _run(
            [
                "age",
                "--encrypt",
                "--recipient",
                recipient,
                "--output",
                str(destination),
                str(source),
            ],
            env=environment,
            label=label,
        )
    else:
        source.replace(destination)


def create_backup(args: argparse.Namespace) -> int:
    app_env = os.environ.get("APP_ENV", "development").lower()
    recipient = _age_recipient(args)
    allow_unencrypted = bool(args.allow_unencrypted_dev)
    object_mode = bool(getattr(args, "object_mode", False))
    database_only = bool(getattr(args, "database_only", False))
    if app_env == "production" and not recipient:
        raise BackupError("production backups require AGE_RECIPIENT encryption")
    if app_env == "production" and not (object_mode or database_only):
        raise BackupError("production backups require explicit --objects or --database-only mode")
    if not recipient and not allow_unencrypted:
        raise BackupError(
            "encryption is not configured; pass --allow-unencrypted-dev only for development/test"
        )
    if allow_unencrypted and app_env == "production":
        raise BackupError("--allow-unencrypted-dev is forbidden in production")
    if args.allow_test_database and app_env == "production":
        raise BackupError("temporary/test databases are never allowed in production")
    if allow_unencrypted and not recipient:
        print(
            "WARNING: DEVELOPMENT/TEST ONLY: creating unencrypted backup artifacts.",
            file=sys.stderr,
        )

    max_object_bytes = int(getattr(args, "max_object_bytes", DEFAULT_MAX_OBJECT_BYTES))
    if max_object_bytes < 1:
        raise BackupError("--max-object-bytes must be positive")
    output_directory = Path(args.output_dir).expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    target = DatabaseTarget(os.environ.get("DATABASE_URL", ""))
    storage = None
    if object_mode:
        source_bucket = os.environ.get("S3_BUCKET", "")
        storage = _object_storage_for_bucket(source_bucket)

    timestamp = utc_text().replace("-", "").replace(":", "")
    published: list[Path] = []
    with tempfile.TemporaryDirectory(
        prefix="infinityscan-backup-", dir=output_directory
    ) as temporary_name:
        temporary_directory = Path(temporary_name)
        raw_dump = temporary_directory / "database.dump"
        raw_objects = temporary_directory / "objects.tar"
        environment = target.subprocess_environment(temporary_directory)

        with _exported_snapshot(target) as (connection, snapshot_id):
            metadata = _query_metadata(target, connection)
            database_name = str(metadata["database_logical_name"])
            _require_safe_database_name(
                database_name, allow_test_database=args.allow_test_database
            )
            expectations = _query_object_expectations(connection) if object_mode else []
            _run(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    "--snapshot",
                    snapshot_id,
                    "--file",
                    str(raw_dump),
                    "--dbname",
                    target.safe_dsn,
                ],
                env=environment,
                label="pg_dump",
            )
            _run_pg_restore_list(raw_dump, environment)
            records = (
                _capture_objects(
                    storage,
                    expectations,
                    temporary_directory / "object-members",
                    raw_objects,
                    max_object_bytes=max_object_bytes,
                )
                if object_mode
                else []
            )

        base = f"infinityscan-{_safe_filename(database_name)}-{timestamp}"
        database_suffix = ".dump.age" if recipient else ".dump"
        artifact = output_directory / f"{base}{database_suffix}"
        manifest_path = _manifest_path(artifact)
        object_artifact = (
            output_directory / f"{base}.objects.tar{'.age' if recipient else ''}"
            if object_mode
            else None
        )
        destinations = [artifact, manifest_path]
        if object_artifact is not None:
            destinations.append(object_artifact)
        if any(path.exists() for path in destinations):
            raise BackupError("refusing to overwrite an existing backup artifact")

        staged_dump = temporary_directory / f"staged{database_suffix}"
        _encrypt_artifact(
            raw_dump,
            staged_dump,
            recipient,
            environment,
            label="database age encryption",
        )
        dump_digest, dump_size = _hash_file(staged_dump)
        object_descriptor = None
        staged_objects = None
        if object_artifact is not None:
            staged_objects = temporary_directory / object_artifact.name
            _encrypt_artifact(
                raw_objects,
                staged_objects,
                recipient,
                environment,
                label="object age encryption",
            )
            object_digest, object_size = _hash_file(staged_objects)
            object_descriptor = {
                "artifact_filename": object_artifact.name,
                "archive_format": "tar",
                "encrypted": bool(recipient),
                "sha256": object_digest,
                "byte_size": object_size,
                "object_count": len(records),
                "total_plaintext_byte_size": sum(record["byte_size"] for record in records),
                "objects": records,
            }

        format_version = 2 if object_mode else 1
        manifest: dict[str, Any] = {
            "manifest_version": format_version,
            "backup_format_version": format_version,
            "archive_format": "custom",
            "includes": ["schema", "data"],
            "ownership_included": False,
            "encrypted": bool(recipient),
            "created_at_utc": utc_text(),
            "artifact_filename": artifact.name,
            "database_logical_name": database_name,
            "alembic_revision": metadata["alembic_revision"],
            "git_commit_sha": _git_sha(),
            "application_version": _application_version(),
            "dump_sha256": dump_digest,
            "dump_byte_size": dump_size,
            "postgresql_version": metadata["postgresql_version"],
            "storage_inventory": metadata["storage_inventory"],
            "critical_table_counts": metadata["critical_table_counts"],
        }
        if object_descriptor is not None:
            manifest["object_artifact"] = object_descriptor

        try:
            os.replace(staged_dump, artifact)
            artifact.chmod(0o640)
            published.append(artifact)
            if object_artifact is not None and staged_objects is not None:
                os.replace(staged_objects, object_artifact)
                object_artifact.chmod(0o640)
                published.append(object_artifact)
            _write_json_atomic(manifest_path, manifest)
        except Exception:
            for path in published:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise

    detail = f" artifact={artifact} manifest={manifest_path}"
    if object_artifact is not None:
        detail += f" objects={object_artifact}"
    print(f"backup complete utc={manifest['created_at_utc']}{detail}")
    return 0


def _required_manifest_fields() -> set[str]:
    return {
        "manifest_version",
        "backup_format_version",
        "archive_format",
        "encrypted",
        "created_at_utc",
        "artifact_filename",
        "database_logical_name",
        "alembic_revision",
        "git_commit_sha",
        "dump_sha256",
        "dump_byte_size",
        "postgresql_version",
        "storage_inventory",
        "critical_table_counts",
    }


def _validate_object_descriptor(descriptor: Any) -> None:
    if not isinstance(descriptor, dict):
        raise BackupError("v2 manifest object artifact is invalid")
    required = {
        "artifact_filename",
        "archive_format",
        "encrypted",
        "sha256",
        "byte_size",
        "object_count",
        "total_plaintext_byte_size",
        "objects",
    }
    if not required.issubset(descriptor):
        raise BackupError("v2 manifest object artifact is missing required fields")
    filename = descriptor["artifact_filename"]
    if (
        not isinstance(filename, str)
        or Path(filename).name != filename
        or descriptor["archive_format"] != "tar"
        or not isinstance(descriptor["encrypted"], bool)
        or not SHA256.fullmatch(str(descriptor["sha256"]))
        or not isinstance(descriptor["byte_size"], int)
        or descriptor["byte_size"] < 1
        or not isinstance(descriptor["object_count"], int)
        or descriptor["object_count"] < 0
        or not isinstance(descriptor["total_plaintext_byte_size"], int)
        or descriptor["total_plaintext_byte_size"] < 0
        or not isinstance(descriptor["objects"], list)
    ):
        raise BackupError("v2 manifest object artifact metadata is invalid")
    if descriptor["encrypted"] != filename.endswith(".age"):
        raise BackupError("v2 manifest object encryption metadata is inconsistent")
    if len(descriptor["objects"]) != descriptor["object_count"]:
        raise BackupError("v2 manifest object count is inconsistent")

    members: set[str] = set()
    keys: set[str] = set()
    total_size = 0
    for index, record in enumerate(descriptor["objects"]):
        if not isinstance(record, dict):
            raise BackupError("v2 manifest object record is invalid")
        required_record = {"member", "key", "sha256", "byte_size", "mime_type"}
        if not required_record.issubset(record):
            raise BackupError("v2 manifest object record is missing required fields")
        member = record["member"]
        key = record["key"]
        size = record["byte_size"]
        mime_type = record["mime_type"]
        if (
            not isinstance(member, str)
            or member != str(index)
            or not member.isdecimal()
            or not isinstance(key, str)
            or not SHA256.fullmatch(str(record["sha256"]))
            or not isinstance(size, int)
            or size < 0
            or not isinstance(mime_type, str)
            or not mime_type
            or "\r" in mime_type
            or "\n" in mime_type
        ):
            raise BackupError("v2 manifest object record metadata is invalid")
        _validate_object_key(key)
        if member in members or key in keys:
            raise BackupError("v2 manifest contains duplicate object records")
        members.add(member)
        keys.add(key)
        total_size += size
    if total_size != descriptor["total_plaintext_byte_size"]:
        raise BackupError("v2 manifest object byte size is inconsistent")


def _load_manifest(manifest_path: Path, artifact: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError("manifest is unreadable or invalid JSON") from exc
    if not isinstance(manifest, dict):
        raise BackupError("manifest must be a JSON object")
    _reject_secret_metadata(manifest)
    if not _required_manifest_fields().issubset(manifest):
        raise BackupError("manifest is missing required fields")
    version = manifest["backup_format_version"]
    if version not in SUPPORTED_BACKUP_FORMATS or manifest["manifest_version"] != version:
        raise BackupError("unsupported backup manifest version")
    if (
        manifest["archive_format"] != "custom"
        or manifest["artifact_filename"] != artifact.name
        or Path(str(manifest["artifact_filename"])).name != manifest["artifact_filename"]
    ):
        raise BackupError("manifest does not describe this backup artifact")
    if not isinstance(manifest["encrypted"], bool) or not SHA256.fullmatch(
        str(manifest["dump_sha256"])
    ):
        raise BackupError("manifest checksum or encryption flag is invalid")
    if not isinstance(manifest["dump_byte_size"], int) or manifest["dump_byte_size"] < 1:
        raise BackupError("manifest byte size is invalid")
    if not isinstance(manifest["database_logical_name"], str) or not SAFE_DATABASE_NAME.fullmatch(
        manifest["database_logical_name"]
    ):
        raise BackupError("manifest database name is invalid")
    if manifest["encrypted"] != artifact.name.endswith(".age"):
        raise BackupError("manifest database encryption metadata is inconsistent")
    if version == 2:
        _validate_object_descriptor(manifest.get("object_artifact"))
        if manifest["object_artifact"]["encrypted"] != manifest["encrypted"]:
            raise BackupError("v2 artifacts must use the same encryption policy")
    elif "object_artifact" in manifest:
        raise BackupError("v1 manifests cannot contain object artifacts")
    return manifest


def _age_identity(args: argparse.Namespace) -> str | None:
    return (
        getattr(args, "age_identity_file", None)
        or os.environ.get("AGE_IDENTITY_FILE")
        or os.environ.get("BACKUP_AGE_IDENTITY_FILE")
    )


def _decrypt_artifact(
    artifact: Path,
    *,
    encrypted: bool,
    args: argparse.Namespace,
    directory: Path,
    output_name: str,
    label: str,
) -> Path:
    if not encrypted:
        if artifact.name.endswith(".age"):
            raise BackupError("manifest says an artifact is unencrypted but its name is encrypted")
        return artifact
    if not artifact.name.endswith(".age"):
        raise BackupError("manifest says an artifact is encrypted but its name is not encrypted")
    identity = _age_identity(args)
    if not identity:
        raise BackupError("encrypted restore requires AGE_IDENTITY_FILE")
    decrypted = directory / output_name
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    _run(
        [
            "age",
            "--decrypt",
            "--identity",
            identity,
            "--output",
            str(decrypted),
            str(artifact),
        ],
        env=environment,
        label=label,
    )
    return decrypted


def _decrypt_if_needed(
    artifact: Path,
    manifest: dict[str, Any],
    args: argparse.Namespace,
    directory: Path,
) -> Path:
    return _decrypt_artifact(
        artifact,
        encrypted=manifest["encrypted"],
        args=args,
        directory=directory,
        output_name="restored.dump",
        label="age decryption",
    )


def _target_is_empty(target: DatabaseTarget) -> tuple[str, int]:
    try:
        with target.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                database_name = cursor.fetchone()[0]
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                      AND n.nspname NOT LIKE 'pg_toast%'
                      AND NOT (n.nspname = 'public' AND c.relname = 'alembic_version')
                      AND c.relkind IN ('r', 'p', 'm', 'S', 'v', 'f')
                    """
                )
                return str(database_name), int(cursor.fetchone()[0])
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("target database inspection failed safely") from exc


def _verify_artifact(path: Path, expected_sha256: str, expected_size: int, label: str) -> None:
    if not path.is_file():
        raise BackupError(f"{label} does not exist")
    digest, size = _hash_file(path)
    if digest != expected_sha256 or size != expected_size:
        raise BackupError(f"{label} checksum or byte size does not match manifest")


def _validate_and_stage_tar(
    archive_path: Path,
    descriptor: dict[str, Any],
    directory: Path,
    *,
    max_object_bytes: int,
) -> list[tuple[dict[str, Any], Path]]:
    expected = {record["member"]: record for record in descriptor["objects"]}
    staged: list[tuple[dict[str, Any], Path]] = []
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            members = archive.getmembers()
            if len(members) != len(expected):
                raise BackupError("object tar member count does not match manifest")
            seen: set[str] = set()
            for member in members:
                if (
                    member.name not in expected
                    or member.name in seen
                    or not member.isfile()
                    or not member.name.isdecimal()
                ):
                    raise BackupError("object tar contains an unsafe or unexpected member")
                seen.add(member.name)
                record = expected[member.name]
                if member.size != record["byte_size"] or member.size > max_object_bytes:
                    raise BackupError("object tar member exceeds or conflicts with its manifest")
                source = archive.extractfile(member)
                if source is None:
                    raise BackupError("object tar member is unreadable")
                destination = directory / f"object-{member.name}"
                digest = hashlib.sha256()
                written = 0
                with source, destination.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > record["byte_size"] or written > max_object_bytes:
                            raise BackupError("object tar member exceeds its validated size")
                        output.write(chunk)
                        digest.update(chunk)
                if written != record["byte_size"] or digest.hexdigest() != record["sha256"]:
                    raise BackupError("object tar member checksum or size is invalid")
                staged.append((record, destination))
    except BackupError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise BackupError("object tar is unreadable or invalid") from exc
    return sorted(staged, key=lambda item: int(item[0]["member"]))


def _verify_destination_object(storage: Any, record: dict[str, Any]) -> str:
    try:
        verification = storage.verify_object(
            record["key"],
            expected_sha256=record["sha256"],
            expected_size=record["byte_size"],
        )
        metadata = storage.head_object(record["key"])
    except Exception as exc:
        raise BackupError("destination object verification failed safely") from exc
    if (
        not verification.verified
        or metadata is None
        or metadata.byte_size != record["byte_size"]
        or metadata.sha256 not in (None, record["sha256"])
        or metadata.mime_type != record["mime_type"]
        or not metadata.etag
    ):
        raise BackupError(f"destination object conflicts with backup: {record['key']}")
    return metadata.etag


def _restore_objects(
    storage: Any, staged: list[tuple[dict[str, Any], Path]]
) -> dict[str, str]:
    missing: list[tuple[dict[str, Any], Path]] = []
    destination_etags: dict[str, str] = {}
    for record, path in staged:
        try:
            metadata = storage.head_object(record["key"])
        except Exception as exc:
            raise BackupError("destination object preflight failed safely") from exc
        if metadata is None:
            missing.append((record, path))
            continue
        destination_etags[record["key"]] = _verify_destination_object(storage, record)

    for record, path in missing:
        # Recheck immediately before creation so a newly visible conflict is never ignored.
        try:
            current = storage.head_object(record["key"])
            if current is not None:
                destination_etags[record["key"]] = _verify_destination_object(storage, record)
                continue
            result = storage.upload_file(record["key"], path, record["mime_type"])
        except BackupError:
            raise
        except Exception as exc:
            raise BackupError("destination object upload failed safely") from exc
        if result.sha256 != record["sha256"] or result.byte_size != record["byte_size"]:
            raise BackupError("destination object upload returned inconsistent metadata")
        destination_etags[record["key"]] = _verify_destination_object(storage, record)
    return destination_etags


def _rebind_destination_etags(
    target: DatabaseTarget,
    records: list[dict[str, Any]],
    destination_etags: dict[str, str],
) -> None:
    try:
        with target.connect() as connection:
            with connection.cursor() as cursor:
                for record in records:
                    key = record["key"]
                    cursor.execute(
                        """
                        SELECT count(*) FROM pages
                        WHERE object_key = %s
                          AND (sha256 IS DISTINCT FROM %s OR file_size IS DISTINCT FROM %s)
                        """,
                        (key, record["sha256"], record["byte_size"]),
                    )
                    if int(cursor.fetchone()[0]):
                        raise BackupError("restored page metadata conflicts with object archive")
                    cursor.execute(
                        """
                        SELECT count(*) FROM import_job_items
                        WHERE object_key = %s
                          AND ((sha256 IS NOT NULL AND sha256 <> %s)
                               OR (byte_size IS NOT NULL AND byte_size <> %s))
                        """,
                        (key, record["sha256"], record["byte_size"]),
                    )
                    if int(cursor.fetchone()[0]):
                        raise BackupError("restored import metadata conflicts with object archive")
                    cursor.execute(
                        "UPDATE pages SET storage_etag = %s WHERE object_key = %s",
                        (destination_etags[key], key),
                    )
                    cursor.execute(
                        "UPDATE import_job_items SET storage_etag = %s WHERE object_key = %s",
                        (destination_etags[key], key),
                    )
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("destination ETag rebinding failed safely") from exc


def restore_backup(args: argparse.Namespace) -> int:
    artifact = Path(args.artifact).expanduser().resolve()
    manifest_path = (
        Path(args.manifest).expanduser().resolve() if args.manifest else _manifest_path(artifact)
    )
    manifest = _load_manifest(manifest_path, artifact)
    _verify_artifact(
        artifact, manifest["dump_sha256"], manifest["dump_byte_size"], "database artifact"
    )
    if not SAFE_DATABASE_NAME.fullmatch(args.target_database):
        raise BackupError("--target-database must name one unambiguous database")
    if (
        os.environ.get("APP_ENV", "development").lower() == "production"
        and not args.confirm_production_restore
    ):
        raise BackupError("production restore requires --confirm-production-restore")
    max_object_bytes = int(getattr(args, "max_object_bytes", DEFAULT_MAX_OBJECT_BYTES))
    if max_object_bytes < 1:
        raise BackupError("--max-object-bytes must be positive")

    object_artifact = None
    object_descriptor = None
    if manifest["backup_format_version"] == 2:
        target_bucket = getattr(args, "target_bucket", None)
        if not target_bucket:
            raise BackupError("v2 restore requires explicit --target-bucket")
        object_descriptor = manifest["object_artifact"]
        object_artifact = artifact.with_name(object_descriptor["artifact_filename"])
        _verify_artifact(
            object_artifact,
            object_descriptor["sha256"],
            object_descriptor["byte_size"],
            "object artifact",
        )

    print(
        f"restore start utc={utc_text()} artifact={artifact.name} target={args.target_database}"
    )
    try:
        target = DatabaseTarget(os.environ.get("DATABASE_URL", ""))
        with tempfile.TemporaryDirectory(prefix="infinityscan-restore-") as temporary_name:
            temporary_directory = Path(temporary_name)
            archive = _decrypt_if_needed(artifact, manifest, args, temporary_directory)
            environment = target.subprocess_environment(temporary_directory)
            _run_pg_restore_list(archive, environment)

            staged_objects: list[tuple[dict[str, Any], Path]] = []
            storage = None
            if object_artifact is not None and object_descriptor is not None:
                raw_objects = _decrypt_artifact(
                    object_artifact,
                    encrypted=object_descriptor["encrypted"],
                    args=args,
                    directory=temporary_directory,
                    output_name="restored-objects.tar",
                    label="object age decryption",
                )
                staged_objects = _validate_and_stage_tar(
                    raw_objects,
                    object_descriptor,
                    temporary_directory,
                    max_object_bytes=max_object_bytes,
                )
                storage = _object_storage_for_bucket(args.target_bucket)

            actual_name, object_count = _target_is_empty(target)
            if actual_name != args.target_database:
                raise BackupError("connected database does not match explicit --target-database")
            if object_count and not args.allow_destructive:
                raise BackupError(
                    "target database is non-empty; refusing restore without --allow-destructive"
                )

            destination_etags = (
                _restore_objects(storage, staged_objects) if storage is not None else {}
            )
            command = [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "--single-transaction",
            ]
            if args.allow_destructive:
                command.extend(["--clean", "--if-exists"])
            command.extend(["--dbname", target.safe_dsn, str(archive)])
            _run(command, env=environment, label="pg_restore")
            if staged_objects:
                _rebind_destination_etags(
                    target,
                    [record for record, _path in staged_objects],
                    destination_etags,
                )
    except BackupError:
        print(
            f"restore end utc={utc_text()} status=failed target={args.target_database}",
            file=sys.stderr,
        )
        raise
    print(f"restore end utc={utc_text()} status=ok target={args.target_database}")
    return 0


def _artifact_state(path: Path, sha256: str, byte_size: int) -> str:
    if not path.is_file():
        return "missing_artifact"
    digest, size = _hash_file(path)
    return "ok" if digest == sha256 and size == byte_size else "checksum_mismatch"


def status_backups(args: argparse.Namespace) -> int:
    directory = Path(args.backup_dir).expanduser().resolve()
    records: list[dict[str, Any]] = []
    for manifest_path in sorted(directory.glob("*.manifest.json")):
        artifact_name = manifest_path.name[: -len(".manifest.json")]
        artifact = manifest_path.with_name(artifact_name)
        try:
            manifest = _load_manifest(manifest_path, artifact)
            database_state = _artifact_state(
                artifact, manifest["dump_sha256"], manifest["dump_byte_size"]
            )
            object_state = None
            object_name = None
            if manifest["backup_format_version"] == 2:
                descriptor = manifest["object_artifact"]
                object_name = descriptor["artifact_filename"]
                object_path = artifact.with_name(object_name)
                object_state = _artifact_state(
                    object_path, descriptor["sha256"], descriptor["byte_size"]
                )
            state = (
                "ok"
                if database_state == "ok" and object_state in (None, "ok")
                else "checksum_mismatch"
            )
            if database_state == "missing_artifact" or object_state == "missing_artifact":
                state = "missing_artifact"
            record = {
                "artifact": artifact.name,
                "manifest": manifest_path.name,
                "state": state,
                "database_state": database_state,
                "object_state": object_state,
                "object_artifact": object_name,
                "backup_format_version": manifest["backup_format_version"],
                "created_at_utc": manifest["created_at_utc"],
                "database_logical_name": manifest["database_logical_name"],
                "encrypted": manifest["encrypted"],
                "dump_byte_size": manifest["dump_byte_size"],
            }
        except BackupError as exc:
            record = {
                "artifact": artifact.name,
                "manifest": manifest_path.name,
                "state": "invalid_manifest",
                "detail": str(exc),
            }
        records.append(record)
    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
    else:
        for record in records:
            print(
                f"{record['state']} artifact={record['artifact']} "
                f"database={record.get('database_logical_name', 'unknown')}"
            )
        if not records:
            print("no backups found")
    return 0 if all(record["state"] == "ok" for record in records) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe PostgreSQL and object backup tooling")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup")
    backup.add_argument(
        "--output-dir", default=os.environ.get("BACKUP_DIR", str(ROOT / "backups"))
    )
    backup.add_argument("--age-recipient", help="age recipient; prefer AGE_RECIPIENT")
    backup.add_argument("--allow-unencrypted-dev", action="store_true")
    backup.add_argument("--allow-test-database", action="store_true")
    mode = backup.add_mutually_exclusive_group()
    mode.add_argument(
        "--objects",
        "--complete",
        dest="object_mode",
        action="store_true",
        help="create a complete v2 database and object backup",
    )
    mode.add_argument(
        "--database-only",
        action="store_true",
        help="create a v1 database-only backup",
    )
    backup.add_argument(
        "--max-object-bytes",
        type=int,
        default=int(os.environ.get("BACKUP_MAX_OBJECT_BYTES", str(DEFAULT_MAX_OBJECT_BYTES))),
    )
    backup.set_defaults(function=create_backup)

    restore = subparsers.add_parser("restore")
    restore.add_argument("artifact")
    restore.add_argument("--manifest")
    restore.add_argument("--target-database", required=True)
    restore.add_argument("--target-bucket")
    restore.add_argument("--age-identity-file", help="age identity file; prefer AGE_IDENTITY_FILE")
    restore.add_argument("--allow-destructive", action="store_true")
    restore.add_argument("--confirm-production-restore", action="store_true")
    restore.add_argument(
        "--max-object-bytes",
        type=int,
        default=int(os.environ.get("BACKUP_MAX_OBJECT_BYTES", str(DEFAULT_MAX_OBJECT_BYTES))),
    )
    restore.set_defaults(function=restore_backup)

    status = subparsers.add_parser("status")
    status.add_argument(
        "--backup-dir", default=os.environ.get("BACKUP_DIR", str(ROOT / "backups"))
    )
    status.add_argument("--json", action="store_true")
    status.set_defaults(function=status_backups)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.function(args))
    except BackupError as exc:
        print(f"backup tooling error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("backup tooling interrupted", file=sys.stderr)
        return 130
    except Exception:
        # Do not expose driver, subprocess, or object-store errors: they may contain secrets.
        print("backup tooling error: operation failed safely", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
