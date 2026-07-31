"""Task 8 backup-tooling tests that do not contact a real database."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import backup_db  # noqa: E402
from storage import DownloadResult, ObjectMetadata, ObjectVerification, UploadResult  # noqa: E402


def _manifest(artifact: Path, *, encrypted: bool = False) -> dict:
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return {
        "manifest_version": 1,
        "backup_format_version": 1,
        "archive_format": "custom",
        "includes": ["schema", "data"],
        "ownership_included": False,
        "encrypted": encrypted,
        "created_at_utc": "2026-07-27T00:00:00Z",
        "artifact_filename": artifact.name,
        "database_logical_name": "infinityscan",
        "alembic_revision": "0008",
        "git_commit_sha": "a" * 40,
        "application_version": "0.2.0",
        "dump_sha256": digest,
        "dump_byte_size": artifact.stat().st_size,
        "postgresql_version": "16.0",
        "storage_inventory": {
            "referenced_object_count": 0,
            "verified_page_count": 0,
            "total_referenced_bytes": 0,
            "integrity_quick_check": {"result": "pass", "violations": 0},
        },
        "critical_table_counts": {"users": 1, "pages": 0},
    }


def _write_manifest(artifact: Path, manifest: dict) -> Path:
    path = backup_db._manifest_path(artifact)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


@contextmanager
def _fake_snapshot(_target):
    yield object(), "00000003-0000001B-1"


def _metadata() -> dict:
    return {
        "database_logical_name": "infinityscan",
        "postgresql_version": "16.0",
        "alembic_revision": "0008",
        "critical_table_counts": {"pages": 0},
        "storage_inventory": {
            "referenced_object_count": 0,
            "verified_page_count": 0,
            "total_referenced_bytes": 0,
            "integrity_quick_check": {"result": "pass", "violations": 0},
        },
    }


class MemoryObjectStorage:
    def __init__(self, events: list[str] | None = None) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}
        self.events = events if events is not None else []

    def put(self, key: str, data: bytes, mime_type: str = "image/jpeg") -> None:
        self.objects[key] = (data, mime_type, hashlib.md5(data, usedforsecurity=False).hexdigest())

    def head_object(self, key: str) -> ObjectMetadata | None:
        stored = self.objects.get(key)
        if stored is None:
            return None
        data, mime_type, etag = stored
        return ObjectMetadata(
            key=key,
            byte_size=len(data),
            mime_type=mime_type,
            sha256=hashlib.sha256(data).hexdigest(),
            etag=etag,
        )

    def download_file(
        self,
        key: str,
        destination: str | Path,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        max_bytes: int = backup_db.DEFAULT_MAX_OBJECT_BYTES,
    ) -> DownloadResult:
        data, mime_type, etag = self.objects[key]
        assert len(data) <= max_bytes
        assert expected_sha256 in (None, hashlib.sha256(data).hexdigest())
        assert expected_size in (None, len(data))
        Path(destination).write_bytes(data)
        return DownloadResult(
            key=key,
            byte_size=len(data),
            mime_type=mime_type,
            sha256=hashlib.sha256(data).hexdigest(),
            etag=etag,
        )

    def upload_file(self, key: str, path: str | Path, mime_type: str) -> UploadResult:
        self.events.append("object_upload")
        data = Path(path).read_bytes()
        self.put(key, data, mime_type)
        metadata = self.head_object(key)
        assert metadata is not None
        return UploadResult(
            key=key,
            byte_size=len(data),
            mime_type=mime_type,
            sha256=hashlib.sha256(data).hexdigest(),
            etag=metadata.etag,
        )

    def verify_object(
        self,
        key: str,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        metadata: ObjectMetadata | None = None,
    ) -> ObjectVerification:
        del metadata
        stored = self.objects.get(key)
        if stored is None:
            return ObjectVerification(
                key=key,
                exists=False,
                verified=False,
                expected_sha256=expected_sha256,
                actual_sha256=None,
                expected_size=expected_size,
                actual_size=None,
                reason="missing",
            )
        data, _mime_type, _etag = stored
        digest = hashlib.sha256(data).hexdigest()
        verified = digest == expected_sha256 and len(data) == expected_size
        return ObjectVerification(
            key=key,
            exists=True,
            verified=verified,
            expected_sha256=expected_sha256,
            actual_sha256=digest,
            expected_size=expected_size,
            actual_size=len(data),
            reason=None if verified else "mismatch",
        )


def _v2_manifest(
    database_artifact: Path,
    object_artifact: Path,
    records: list[dict],
) -> dict:
    manifest = _manifest(database_artifact)
    object_digest, object_size = backup_db._hash_file(object_artifact)
    manifest.update(
        {
            "manifest_version": 2,
            "backup_format_version": 2,
            "object_artifact": {
                "artifact_filename": object_artifact.name,
                "archive_format": "tar",
                "encrypted": False,
                "sha256": object_digest,
                "byte_size": object_size,
                "object_count": len(records),
                "total_plaintext_byte_size": sum(record["byte_size"] for record in records),
                "objects": records,
            },
        }
    )
    return manifest


def _write_object_tar(path: Path, name: str, data: bytes) -> None:
    with tarfile.open(path, "w") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))


def test_manifest_creation_and_no_secret_fields(tmp_path):
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"custom-format-dump")
    manifest = _manifest(artifact)
    path = _write_manifest(artifact, manifest)

    loaded = backup_db._load_manifest(path, artifact)
    encoded = path.read_text(encoding="utf-8")
    assert loaded["dump_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert "DATABASE_URL" not in encoded
    assert "password" not in encoded.lower()
    assert "secret" not in encoded.lower()
    assert "presigned" not in encoded.lower()


def test_sqlalchemy_postgresql_url_is_normalized_without_password_in_dsn():
    target = backup_db.DatabaseTarget("postgresql+psycopg://backup_user:do-not-print@db:5432/infinityscan")
    assert target.database_url == "postgresql://backup_user:do-not-print@db:5432/infinityscan"
    assert "do-not-print" not in target.safe_dsn


def test_corrupted_archive_fails_checksum_validation(tmp_path):
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"valid-archive")
    path = _write_manifest(artifact, _manifest(artifact))
    artifact.write_bytes(b"corrupted-archive")
    args = Namespace(
        artifact=str(artifact),
        manifest=str(path),
        target_database="infinityscan",
        age_identity_file=None,
        allow_destructive=False,
        confirm_production_restore=False,
    )
    with pytest.raises(backup_db.BackupError, match="checksum"):
        backup_db.restore_backup(args)


def test_corrupted_manifest_is_rejected(tmp_path):
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"archive")
    path = _write_manifest(artifact, _manifest(artifact))
    path.write_text(json.dumps({"database_url": "postgresql://user:password@host/db"}), encoding="utf-8")
    with pytest.raises(backup_db.BackupError, match="forbidden"):
        backup_db._load_manifest(path, artifact)


def test_nested_secret_bearing_manifest_key_is_rejected(tmp_path):
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"archive")
    manifest = _manifest(artifact)
    manifest["storage_inventory"]["nested"] = {"api_token": "not-for-a-manifest"}
    path = _write_manifest(artifact, manifest)

    with pytest.raises(backup_db.BackupError, match="forbidden"):
        backup_db._load_manifest(path, artifact)


def test_nested_secret_bearing_manifest_value_is_rejected(tmp_path):
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"archive")
    manifest = _manifest(artifact)
    manifest["storage_inventory"]["note"] = "password=must-not-leak"
    path = _write_manifest(artifact, manifest)

    with pytest.raises(backup_db.BackupError, match="secret-bearing content"):
        backup_db._load_manifest(path, artifact)


def test_status_detects_checksum_mismatch(tmp_path, capsys):
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"archive")
    _write_manifest(artifact, _manifest(artifact))
    artifact.write_bytes(b"changed")

    result = backup_db.status_backups(Namespace(backup_dir=str(tmp_path), json=True))
    assert result == 1
    assert json.loads(capsys.readouterr().out)[0]["state"] == "checksum_mismatch"


def test_backup_invokes_custom_dump_and_restore_list(tmp_path, monkeypatch):
    class FakeTarget:
        safe_dsn = "dbname=infinityscan"

        def subprocess_environment(self, _directory):
            return os.environ.copy()

    calls: list[list[str]] = []

    def fake_run(command, *, env, label):
        del env
        calls.append(command)
        if label == "pg_dump":
            dump_path = Path(command[command.index("--file") + 1])
            dump_path.write_bytes(b"custom-format-dump")

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "postgresql://not-used")
    monkeypatch.setattr(backup_db, "DatabaseTarget", lambda _url: FakeTarget())
    monkeypatch.setattr(backup_db, "_exported_snapshot", _fake_snapshot)
    monkeypatch.setattr(
        backup_db,
        "_query_metadata",
        lambda _target, _connection: _metadata(),
    )
    monkeypatch.setattr(backup_db, "_run", fake_run)
    args = backup_db.build_parser().parse_args(
        ["backup", "--output-dir", str(tmp_path), "--allow-unencrypted-dev"]
    )

    assert backup_db.create_backup(args) == 0
    assert calls[0][0] == "pg_dump"
    assert "--format=custom" in calls[0]
    assert "--no-owner" in calls[0]
    assert calls[0][calls[0].index("--snapshot") + 1] == "00000003-0000001B-1"
    assert calls[1][:2] == ["pg_restore", "--list"]
    assert len(list(tmp_path.glob("*.dump"))) == 1
    assert len(list(tmp_path.glob("*.manifest.json"))) == 1


def test_production_requires_age_encryption(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    args = backup_db.build_parser().parse_args(["backup", "--output-dir", str(tmp_path)])
    with pytest.raises(backup_db.BackupError, match="require AGE_RECIPIENT"):
        backup_db.create_backup(args)


def test_production_requires_explicit_complete_or_database_only_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AGE_RECIPIENT", "age1examplepublicrecipient")
    args = backup_db.build_parser().parse_args(["backup", "--output-dir", str(tmp_path)])
    with pytest.raises(backup_db.BackupError, match="explicit --objects or --database-only"):
        backup_db.create_backup(args)


def test_encrypted_backup_and_wrong_age_identity_rejected(tmp_path, monkeypatch):
    class FakeTarget:
        safe_dsn = "dbname=infinityscan"

        def subprocess_environment(self, _directory):
            return os.environ.copy()

    calls: list[list[str]] = []

    def fake_run(command, *, env, label):
        del env
        calls.append(command)
        if label == "pg_dump":
            Path(command[command.index("--file") + 1]).write_bytes(b"custom-format-dump")
        elif label == "database age encryption":
            Path(command[command.index("--output") + 1]).write_bytes(b"encrypted-dump")

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AGE_RECIPIENT", "age1examplepublicrecipient")
    monkeypatch.setenv("DATABASE_URL", "postgresql://not-used")
    monkeypatch.setattr(backup_db, "DatabaseTarget", lambda _url: FakeTarget())
    monkeypatch.setattr(backup_db, "_exported_snapshot", _fake_snapshot)
    monkeypatch.setattr(
        backup_db,
        "_query_metadata",
        lambda _target, _connection: _metadata(),
    )
    monkeypatch.setattr(backup_db, "_run", fake_run)
    args = backup_db.build_parser().parse_args(["backup", "--output-dir", str(tmp_path)])
    assert backup_db.create_backup(args) == 0
    artifact = next(tmp_path.glob("*.dump.age"))
    manifest = json.loads(backup_db._manifest_path(artifact).read_text(encoding="utf-8"))
    assert manifest["encrypted"] is True
    assert any(command[0] == "age" for command in calls)

    identity = tmp_path / "wrong-identity.txt"
    identity.write_text("AGE-SECRET-KEY-1WRONG\n", encoding="utf-8")
    args = Namespace(age_identity_file=str(identity))
    monkeypatch.setattr(
        backup_db,
        "_run",
        lambda command, **kwargs: (_ for _ in ()).throw(backup_db.BackupError("age decryption failed")),
    )
    with pytest.raises(backup_db.BackupError, match="age decryption"):
        backup_db._decrypt_if_needed(artifact, manifest, args, tmp_path)


def test_complete_backup_creates_v2_numeric_object_tar_and_publishes_manifest_last(
    tmp_path, monkeypatch
):
    class FakeTarget:
        safe_dsn = "dbname=infinityscan"

        def subprocess_environment(self, _directory):
            return os.environ.copy()

    payload = b"object payload"
    digest = hashlib.sha256(payload).hexdigest()
    key = f"series/one/cover-{digest}.jpg"
    storage = MemoryObjectStorage()
    storage.put(key, payload)
    calls: list[list[str]] = []

    def fake_run(command, *, env, label):
        del env
        calls.append(command)
        if label == "pg_dump":
            Path(command[command.index("--file") + 1]).write_bytes(b"database dump")

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "postgresql://not-used")
    monkeypatch.setenv("OBJECT_STORAGE_ENABLED", "true")
    monkeypatch.setenv("S3_BUCKET", "source-bucket")
    monkeypatch.setattr(backup_db, "DatabaseTarget", lambda _url: FakeTarget())
    monkeypatch.setattr(backup_db, "_exported_snapshot", _fake_snapshot)
    monkeypatch.setattr(backup_db, "_query_metadata", lambda _target, _connection: _metadata())
    monkeypatch.setattr(
        backup_db,
        "_query_object_expectations",
        lambda _connection: [
            backup_db.ObjectExpectation(
                key=key,
                sha256=digest,
                byte_size=len(payload),
                mime_type="image/jpeg",
            )
        ],
    )
    monkeypatch.setattr(backup_db, "_object_storage_for_bucket", lambda bucket: storage)
    monkeypatch.setattr(backup_db, "_run", fake_run)
    args = backup_db.build_parser().parse_args(
        [
            "backup",
            "--objects",
            "--output-dir",
            str(tmp_path),
            "--allow-unencrypted-dev",
        ]
    )

    assert backup_db.create_backup(args) == 0

    database_artifact = next(tmp_path.glob("*.dump"))
    object_artifact = next(tmp_path.glob("*.objects.tar"))
    manifest_path = backup_db._manifest_path(database_artifact)
    manifest = backup_db._load_manifest(manifest_path, database_artifact)
    assert manifest["backup_format_version"] == 2
    assert manifest["object_artifact"]["objects"][0]["key"] == key
    with tarfile.open(object_artifact, "r:") as archive:
        assert [member.name for member in archive.getmembers()] == ["0"]
        assert archive.extractfile("0").read() == payload


def test_status_verifies_v2_database_and_object_artifacts_independently(tmp_path, capsys):
    database_artifact = tmp_path / "backup.dump"
    object_artifact = tmp_path / "backup.objects.tar"
    database_artifact.write_bytes(b"database")
    _write_object_tar(object_artifact, "0", b"object")
    record = {
        "member": "0",
        "key": "series/object.jpg",
        "sha256": hashlib.sha256(b"object").hexdigest(),
        "byte_size": len(b"object"),
        "mime_type": "image/jpeg",
    }
    _write_manifest(database_artifact, _v2_manifest(database_artifact, object_artifact, [record]))
    object_artifact.write_bytes(b"corrupted")

    assert backup_db.status_backups(Namespace(backup_dir=str(tmp_path), json=True)) == 1
    result = json.loads(capsys.readouterr().out)[0]
    assert result["database_state"] == "ok"
    assert result["object_state"] == "checksum_mismatch"


def test_object_tar_rejects_non_numeric_path_without_extracting(tmp_path):
    archive = tmp_path / "objects.tar"
    _write_object_tar(archive, "../0", b"object")
    descriptor = {
        "objects": [
            {
                "member": "0",
                "key": "series/object.jpg",
                "sha256": hashlib.sha256(b"object").hexdigest(),
                "byte_size": len(b"object"),
                "mime_type": "image/jpeg",
            }
        ]
    }

    with pytest.raises(backup_db.BackupError, match="unsafe or unexpected"):
        backup_db._validate_and_stage_tar(
            archive,
            descriptor,
            tmp_path,
            max_object_bytes=1024,
        )
    assert not (tmp_path.parent / "0").exists()


def test_conflicting_duplicate_object_expectations_are_rejected():
    expectations: dict[str, backup_db.ObjectExpectation] = {}
    backup_db._merge_object_expectation(
        expectations,
        key="series/object.jpg",
        sha256="a" * 64,
        byte_size=1,
        mime_type="image/jpeg",
    )
    with pytest.raises(backup_db.BackupError, match="conflicting expectations"):
        backup_db._merge_object_expectation(
            expectations,
            key="series/object.jpg",
            sha256="b" * 64,
            byte_size=1,
            mime_type="image/jpeg",
        )


def test_complete_scope_refuses_any_active_import():
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, params=None):
            assert "import_jobs" in query
            assert params is None

        def fetchone(self):
            return (1,)

    class Connection:
        def cursor(self):
            return Cursor()

    with pytest.raises(backup_db.BackupError, match="active imports"):
        backup_db._query_object_expectations(Connection())


def test_restore_uploads_objects_before_transactional_database_restore_and_rebinds_etags(
    tmp_path, monkeypatch
):
    database_artifact = tmp_path / "backup.dump"
    object_artifact = tmp_path / "backup.objects.tar"
    database_artifact.write_bytes(b"database")
    payload = b"object"
    _write_object_tar(object_artifact, "0", payload)
    record = {
        "member": "0",
        "key": "series/object.jpg",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "mime_type": "image/jpeg",
        "source_etag": "source-etag",
    }
    manifest = _write_manifest(
        database_artifact,
        _v2_manifest(database_artifact, object_artifact, [record]),
    )
    events: list[str] = []
    storage = MemoryObjectStorage(events)

    class FakeTarget:
        safe_dsn = "dbname=restore_target"

        def subprocess_environment(self, _directory):
            return os.environ.copy()

    def fake_run(command, *, env, label):
        del env
        if label == "pg_restore":
            assert "--single-transaction" in command
            events.append("database_restore")

    def fake_rebind(_target, records, etags):
        assert records == [record]
        assert etags[record["key"]] != record["source_etag"]
        events.append("etag_rebind")

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "postgresql://not-used")
    monkeypatch.setattr(backup_db, "DatabaseTarget", lambda _url: FakeTarget())
    monkeypatch.setattr(backup_db, "_target_is_empty", lambda _target: ("restore_target", 0))
    monkeypatch.setattr(backup_db, "_object_storage_for_bucket", lambda bucket: storage)
    monkeypatch.setattr(backup_db, "_rebind_destination_etags", fake_rebind)
    monkeypatch.setattr(backup_db, "_run", fake_run)
    args = Namespace(
        artifact=str(database_artifact),
        manifest=str(manifest),
        target_database="restore_target",
        target_bucket="restore-bucket",
        age_identity_file=None,
        allow_destructive=False,
        confirm_production_restore=False,
        max_object_bytes=1024,
    )

    assert backup_db.restore_backup(args) == 0
    assert events == ["object_upload", "database_restore", "etag_rebind"]


def test_restore_skips_exact_objects_and_aborts_mismatches_without_mutation(tmp_path):
    payload = b"object"
    path = tmp_path / "object"
    path.write_bytes(payload)
    record = {
        "member": "0",
        "key": "series/object.jpg",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "mime_type": "image/jpeg",
    }
    events: list[str] = []
    storage = MemoryObjectStorage(events)
    storage.put(record["key"], payload)

    etags = backup_db._restore_objects(storage, [(record, path)])
    assert events == []
    assert etags[record["key"]] == storage.head_object(record["key"]).etag

    storage.put(record["key"], b"conflict")
    with pytest.raises(backup_db.BackupError, match="conflicts with backup"):
        backup_db._restore_objects(storage, [(record, path)])
    assert events == []
    assert storage.objects[record["key"]][0] == b"conflict"


def test_non_empty_restore_is_refused(tmp_path, monkeypatch):
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"archive")
    manifest = _write_manifest(artifact, _manifest(artifact))

    class FakeTarget:
        safe_dsn = "dbname=infinityscan"

        def subprocess_environment(self, _directory):
            return os.environ.copy()

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setattr(backup_db, "DatabaseTarget", lambda _url: FakeTarget())
    monkeypatch.setattr(backup_db, "_run", lambda command, **kwargs: None)
    monkeypatch.setattr(backup_db, "_target_is_empty", lambda _target: ("infinityscan", 1))
    args = Namespace(
        artifact=str(artifact),
        manifest=str(manifest),
        target_database="infinityscan",
        age_identity_file=None,
        allow_destructive=False,
        confirm_production_restore=False,
    )
    with pytest.raises(backup_db.BackupError, match="non-empty"):
        backup_db.restore_backup(args)
