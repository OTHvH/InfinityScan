"""Task 8 backup-tooling tests that do not contact a real database."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import backup_db  # noqa: E402


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
    monkeypatch.setattr(
        backup_db,
        "_query_metadata",
        lambda _target: {
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
        },
    )
    monkeypatch.setattr(backup_db, "_run", fake_run)
    args = backup_db.build_parser().parse_args(
        ["backup", "--output-dir", str(tmp_path), "--allow-unencrypted-dev"]
    )

    assert backup_db.create_backup(args) == 0
    assert calls[0][0] == "pg_dump"
    assert "--format=custom" in calls[0]
    assert "--no-owner" in calls[0]
    assert calls[1][:2] == ["pg_restore", "--list"]
    assert len(list(tmp_path.glob("*.dump"))) == 1
    assert len(list(tmp_path.glob("*.manifest.json"))) == 1


def test_production_requires_age_encryption(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    args = backup_db.build_parser().parse_args(["backup", "--output-dir", str(tmp_path)])
    with pytest.raises(backup_db.BackupError, match="require AGE_RECIPIENT"):
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
        elif label == "age encryption":
            Path(command[command.index("--output") + 1]).write_bytes(b"encrypted-dump")

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AGE_RECIPIENT", "age1examplepublicrecipient")
    monkeypatch.setenv("DATABASE_URL", "postgresql://not-used")
    monkeypatch.setattr(backup_db, "DatabaseTarget", lambda _url: FakeTarget())
    monkeypatch.setattr(
        backup_db,
        "_query_metadata",
        lambda _target: {
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
        },
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
