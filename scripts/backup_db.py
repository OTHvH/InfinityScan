#!/usr/bin/env python3
"""Safe PostgreSQL backup, restore, and backup-status implementation.

The shell entrypoints in this directory deliberately delegate to this module
so that metadata validation and subprocess safety are shared by all commands.
Database credentials are read from the environment and are never included in
subprocess arguments, manifests, or status output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
BACKUP_FORMAT_VERSION = 1
ARTIFACT_SUFFIXES = (".dump", ".dump.age")
SAFE_DATABASE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")
TEST_DATABASE_NAME = re.compile(r"(?:^|[_-])(test|tests|tmp|temporary)(?:$|[_-])", re.IGNORECASE)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_METADATA_KEYS = re.compile(
    r"(?:database[_-]?url|dsn|password|secret|credential|private[_-]?key|identity|token|presign)",
    re.IGNORECASE,
)
FORBIDDEN_METADATA_VALUES = re.compile(
    r"(?:postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^\s]+:[^\s]+@|-----BEGIN [^-]*PRIVATE KEY-----|AKIA[0-9A-Z]{16})",
    re.IGNORECASE,
)


class BackupError(RuntimeError):
    """An expected, safe-to-report backup operation failure."""


def _require_psycopg() -> None:
    if psycopg is None or sql is None or conninfo_to_dict is None or make_conninfo is None:
        raise BackupError("psycopg is required for backup and restore commands")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run(command: list[str], *, env: dict[str, str], label: str) -> None:
    try:
        result = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise BackupError(f"{label} could not be started") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] or [""]
        safe_detail = re.sub(r"(?i)(password|passwd|secret|token)=\S+", r"\1=<redacted>", detail[0])
        raise BackupError(f"{label} failed with exit code {result.returncode}: {safe_detail[:240]}")


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
    settings_path = ROOT / "api" / "settings.py"
    try:
        text = settings_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^\s*app_version:\s*str\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
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
                ":".join(_pgpass_escape(value) for value in (host, port, database, user, self.password)) + "\n",
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


def _query_metadata(target: DatabaseTarget) -> dict[str, Any]:
    try:
        with target.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database(), current_setting('server_version')")
                database_name, postgres_version = cursor.fetchone()

                cursor.execute(
                    "SELECT version_num FROM alembic_version ORDER BY version_num"
                )
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
                        count(DISTINCT object_key) FILTER (WHERE object_key IS NOT NULL AND btrim(object_key) <> ''),
                        count(*) FILTER (WHERE integrity_status = 'verified'),
                        coalesce(sum(file_size) FILTER (WHERE object_key IS NOT NULL AND btrim(object_key) <> ''), 0),
                        count(*) FILTER (
                            WHERE integrity_status = 'verified'
                              AND (object_key IS NULL OR btrim(object_key) = '' OR sha256 IS NULL
                                   OR width IS NULL OR height IS NULL OR file_size IS NULL OR file_size <= 0
                                   OR mime_type IS NULL OR btrim(mime_type) = ''
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


def _require_safe_database_name(database_name: str, *, allow_test_database: bool) -> None:
    if not SAFE_DATABASE_NAME.fullmatch(database_name):
        raise BackupError("database logical name is ambiguous or unsafe")
    if TEST_DATABASE_NAME.search(database_name) and not allow_test_database:
        raise BackupError("temporary/test databases are excluded; use --allow-test-database explicitly")


def _age_recipient(args: argparse.Namespace) -> str | None:
    return args.age_recipient or os.environ.get("AGE_RECIPIENT") or os.environ.get("BACKUP_AGE_RECIPIENT")


def _manifest_path(artifact: Path) -> Path:
    return artifact.with_name(artifact.name + ".manifest.json")


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o640)
    os.replace(temporary, path)


def _run_pg_restore_list(archive: Path, environment: dict[str, str]) -> None:
    _run(["pg_restore", "--list", str(archive)], env=environment, label="pg_restore --list")


def create_backup(args: argparse.Namespace) -> int:
    app_env = os.environ.get("APP_ENV", "development").lower()
    recipient = _age_recipient(args)
    allow_unencrypted = bool(args.allow_unencrypted_dev)
    if app_env == "production" and not recipient:
        raise BackupError("production backups require AGE_RECIPIENT encryption")
    if not recipient and not allow_unencrypted:
        raise BackupError("encryption is not configured; pass --allow-unencrypted-dev only for development/test")
    if allow_unencrypted and app_env == "production":
        raise BackupError("--allow-unencrypted-dev is forbidden in production")
    if args.allow_test_database and app_env == "production":
        raise BackupError("temporary/test databases are never allowed in production")
    if allow_unencrypted and not recipient:
        print("WARNING: DEVELOPMENT/TEST ONLY: creating an unencrypted PostgreSQL backup.", file=sys.stderr)

    output_directory = Path(args.output_dir).expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    database_url = os.environ.get("DATABASE_URL", "")
    target = DatabaseTarget(database_url)
    metadata = _query_metadata(target)
    database_name = str(metadata["database_logical_name"])
    _require_safe_database_name(database_name, allow_test_database=args.allow_test_database)

    timestamp = utc_text().replace("-", "").replace(":", "")
    suffix = ".dump.age" if recipient else ".dump"
    artifact = output_directory / f"infinityscan-{_safe_filename(database_name)}-{timestamp}{suffix}"
    manifest_path = _manifest_path(artifact)
    if artifact.exists() or manifest_path.exists():
        raise BackupError("refusing to overwrite an existing backup artifact")

    with tempfile.TemporaryDirectory(prefix="infinityscan-backup-", dir=output_directory) as temporary_name:
        temporary_directory = Path(temporary_name)
        raw_dump = temporary_directory / "database.dump"
        environment = target.subprocess_environment(temporary_directory)
        _run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--file",
                str(raw_dump),
                "--dbname",
                target.safe_dsn,
            ],
            env=environment,
            label="pg_dump",
        )
        _run_pg_restore_list(raw_dump, environment)
        if recipient:
            _run(
                ["age", "--encrypt", "--recipient", recipient, "--output", str(artifact), str(raw_dump)],
                env=environment,
                label="age encryption",
            )
        else:
            raw_dump.replace(artifact)

    artifact.chmod(0o640)
    digest, size = _hash_file(artifact)
    manifest = {
        "manifest_version": 1,
        "backup_format_version": BACKUP_FORMAT_VERSION,
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
        "dump_sha256": digest,
        "dump_byte_size": size,
        "postgresql_version": metadata["postgresql_version"],
        "storage_inventory": metadata["storage_inventory"],
        "critical_table_counts": metadata["critical_table_counts"],
    }
    _write_json_atomic(manifest_path, manifest)
    print(f"backup complete utc={manifest['created_at_utc']} artifact={artifact} manifest={manifest_path}")
    return 0


def _load_manifest(manifest_path: Path, artifact: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError("manifest is unreadable or invalid JSON") from exc
    if not isinstance(manifest, dict):
        raise BackupError("manifest must be a JSON object")
    for key, value in manifest.items():
        if FORBIDDEN_METADATA_KEYS.search(str(key)):
            raise BackupError("manifest contains a forbidden secret-bearing field")
        if FORBIDDEN_METADATA_VALUES.search(json.dumps(value, separators=(",", ":"))):
            raise BackupError("manifest contains secret-bearing content")
    required = {
        "manifest_version", "backup_format_version", "archive_format", "encrypted",
        "created_at_utc", "artifact_filename", "database_logical_name", "alembic_revision",
        "git_commit_sha", "dump_sha256", "dump_byte_size", "postgresql_version",
        "storage_inventory", "critical_table_counts",
    }
    if not required.issubset(manifest):
        raise BackupError("manifest is missing required fields")
    if manifest["manifest_version"] != 1 or manifest["backup_format_version"] != BACKUP_FORMAT_VERSION:
        raise BackupError("unsupported backup manifest version")
    if manifest["archive_format"] != "custom" or manifest["artifact_filename"] != artifact.name:
        raise BackupError("manifest does not describe this backup artifact")
    if not isinstance(manifest["encrypted"], bool) or not SHA256.fullmatch(str(manifest["dump_sha256"])):
        raise BackupError("manifest checksum or encryption flag is invalid")
    if not isinstance(manifest["dump_byte_size"], int) or manifest["dump_byte_size"] < 1:
        raise BackupError("manifest byte size is invalid")
    if not isinstance(manifest["database_logical_name"], str):
        raise BackupError("manifest database name is invalid")
    return manifest


def _decrypt_if_needed(artifact: Path, manifest: dict[str, Any], args: argparse.Namespace, directory: Path) -> Path:
    if not manifest["encrypted"]:
        if artifact.name.endswith(".age"):
            raise BackupError("manifest says backup is unencrypted but artifact is age-encrypted")
        return artifact
    if not artifact.name.endswith(".age"):
        raise BackupError("manifest says backup is encrypted but artifact is not age-encrypted")
    identity = args.age_identity_file or os.environ.get("AGE_IDENTITY_FILE") or os.environ.get("BACKUP_AGE_IDENTITY_FILE")
    if not identity:
        raise BackupError("encrypted restore requires AGE_IDENTITY_FILE")
    decrypted = directory / "restored.dump"
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    _run(["age", "--decrypt", "--identity", identity, "--output", str(decrypted), str(artifact)], env=environment, label="age decryption")
    return decrypted


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


def restore_backup(args: argparse.Namespace) -> int:
    artifact = Path(args.artifact).expanduser().resolve()
    if not artifact.is_file():
        raise BackupError("backup artifact does not exist")
    manifest_path = Path(args.manifest).expanduser().resolve() if args.manifest else _manifest_path(artifact)
    manifest = _load_manifest(manifest_path, artifact)
    digest, size = _hash_file(artifact)
    if digest != manifest["dump_sha256"] or size != manifest["dump_byte_size"]:
        raise BackupError("backup checksum or byte size does not match manifest")
    if not SAFE_DATABASE_NAME.fullmatch(args.target_database):
        raise BackupError("--target-database must name one unambiguous database")
    if os.environ.get("APP_ENV", "development").lower() == "production" and not args.confirm_production_restore:
        raise BackupError("production restore requires --confirm-production-restore")
    print(f"restore start utc={utc_text()} artifact={artifact.name} target={args.target_database}")
    try:
        database_url = os.environ.get("DATABASE_URL", "")
        target = DatabaseTarget(database_url)
        with tempfile.TemporaryDirectory(prefix="infinityscan-restore-") as temporary_name:
            archive = _decrypt_if_needed(artifact, manifest, args, Path(temporary_name))
            environment = target.subprocess_environment(Path(temporary_name))
            _run_pg_restore_list(archive, environment)
            actual_name, object_count = _target_is_empty(target)
            if actual_name != args.target_database:
                raise BackupError("connected database does not match explicit --target-database")
            if object_count and not args.allow_destructive:
                raise BackupError("target database is non-empty; refusing restore without --allow-destructive")
            command = ["pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "--single-transaction"]
            if args.allow_destructive:
                command.extend(["--clean", "--if-exists"])
            command.extend(["--dbname", target.safe_dsn, str(archive)])
            _run(command, env=environment, label="pg_restore")
    except BackupError:
        print(f"restore end utc={utc_text()} status=failed target={args.target_database}", file=sys.stderr)
        raise
    print(f"restore end utc={utc_text()} status=ok target={args.target_database}")
    return 0


def status_backups(args: argparse.Namespace) -> int:
    directory = Path(args.backup_dir).expanduser().resolve()
    records: list[dict[str, Any]] = []
    for manifest_path in sorted(directory.glob("*.manifest.json")):
        artifact_name = manifest_path.name[: -len(".manifest.json")]
        artifact = manifest_path.with_name(artifact_name)
        try:
            manifest = _load_manifest(manifest_path, artifact)
            if artifact.exists():
                digest, size = _hash_file(artifact)
                state = "ok" if digest == manifest["dump_sha256"] and size == manifest["dump_byte_size"] else "checksum_mismatch"
            else:
                state = "missing_artifact"
            record = {
                "artifact": artifact.name,
                "manifest": manifest_path.name,
                "state": state if artifact.exists() else "missing_artifact",
                "created_at_utc": manifest["created_at_utc"],
                "database_logical_name": manifest["database_logical_name"],
                "encrypted": manifest["encrypted"],
                "dump_byte_size": manifest["dump_byte_size"],
            }
        except BackupError as exc:
            record = {"artifact": artifact.name, "manifest": manifest_path.name, "state": "invalid_manifest", "detail": str(exc)}
        records.append(record)
    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
    else:
        for record in records:
            print(f"{record['state']} artifact={record['artifact']} database={record.get('database_logical_name', 'unknown')}")
        if not records:
            print("no backups found")
    return 0 if all(record["state"] == "ok" for record in records) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe PostgreSQL backup tooling")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup")
    backup.add_argument("--output-dir", default=os.environ.get("BACKUP_DIR", str(ROOT / "backups")))
    backup.add_argument("--age-recipient", help="age recipient; prefer AGE_RECIPIENT")
    backup.add_argument("--allow-unencrypted-dev", action="store_true")
    backup.add_argument("--allow-test-database", action="store_true")
    backup.set_defaults(function=create_backup)

    restore = subparsers.add_parser("restore")
    restore.add_argument("artifact")
    restore.add_argument("--manifest")
    restore.add_argument("--target-database", required=True)
    restore.add_argument("--age-identity-file", help="age identity file; prefer AGE_IDENTITY_FILE")
    restore.add_argument("--allow-destructive", action="store_true")
    restore.add_argument("--confirm-production-restore", action="store_true")
    restore.set_defaults(function=restore_backup)

    status = subparsers.add_parser("status")
    status.add_argument("--backup-dir", default=os.environ.get("BACKUP_DIR", str(ROOT / "backups")))
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
        # Do not expose driver errors: they may contain a full connection URI.
        print("backup tooling error: operation failed safely", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
