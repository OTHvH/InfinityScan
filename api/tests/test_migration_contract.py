"""Task 6 static migration manifest, graph, and metadata contract tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from models import Base
from tools.migration_graph import validate_revision_graph
from tools.migration_manifest import MigrationRecord, verify_manifest
from tools.migration_verifier import verify_migrations
from tools.schema_snapshot import compare_schema, snapshot_metadata


API_DIR = Path(__file__).resolve().parents[1]
MANIFEST = API_DIR / "alembic" / "migration-manifest.json"
VERSIONS = API_DIR / "alembic" / "versions"


def test_current_migration_manifest_and_graph_are_valid():
    assert verify_manifest(MANIFEST, VERSIONS) == ()
    report = verify_migrations(api_dir=API_DIR, manifest_path=MANIFEST)
    assert report.ok
    assert report.to_dict()["findings"] == []


def test_modified_historical_migration_is_detected(tmp_path):
    api_copy = tmp_path / "api"
    shutil.copytree(API_DIR / "alembic", api_copy / "alembic")
    migration = api_copy / "alembic" / "versions" / "0001_initial_schema.py"
    migration.write_text(migration.read_text() + "\n# unauthorized edit\n", encoding="utf-8")

    findings = verify_manifest(
        api_copy / "alembic" / "migration-manifest.json",
        api_copy / "alembic" / "versions",
    )
    assert any(finding.code == "modified_migration" for finding in findings)


def test_missing_and_unmanifested_migrations_are_detected(tmp_path):
    api_copy = tmp_path / "api"
    shutil.copytree(API_DIR / "alembic", api_copy / "alembic")
    (api_copy / "alembic" / "versions" / "0007_import_recovery.py").unlink()
    missing = verify_manifest(
        api_copy / "alembic" / "migration-manifest.json",
        api_copy / "alembic" / "versions",
    )
    assert any(finding.code == "missing_migration" for finding in missing)

    extra = api_copy / "alembic" / "versions" / "0008_test_only.py"
    extra.write_text(
        'revision = "0008"\ndown_revision = "0006"\nbranch_labels = None\ndepends_on = None\n',
        encoding="utf-8",
    )
    unmanifested = verify_manifest(
        api_copy / "alembic" / "migration-manifest.json",
        api_copy / "alembic" / "versions",
    )
    assert any(finding.code == "unmanifested_migration" for finding in unmanifested)


def test_duplicate_and_multiple_heads_are_detected():
    duplicate = MigrationRecord("a.py", "0001", None, "a")
    duplicate_again = MigrationRecord("b.py", "0001", None, "b")
    duplicate_report = validate_revision_graph((duplicate, duplicate_again))
    assert any(finding.code == "duplicate_revision" for finding in duplicate_report.findings)

    multiple = validate_revision_graph(
        (
            MigrationRecord("a.py", "0001", None, "a"),
            MigrationRecord("b.py", "0002", "0001", "b"),
            MigrationRecord("c.py", "0003", "0001", "c"),
        )
    )
    assert multiple.heads == ("0002", "0003")
    assert any(finding.code == "multiple_heads" for finding in multiple.findings)


def test_metadata_snapshot_is_deterministic_and_comparable():
    first = snapshot_metadata(Base.metadata)
    second = snapshot_metadata(Base.metadata)
    comparison = compare_schema(first, second)
    assert comparison["equal"] is True
    assert comparison["expected_hash"] == comparison["actual_hash"]
    changed = json.loads(json.dumps(first))
    changed["tables"][0]["columns"][0]["nullable"] = not changed["tables"][0]["columns"][0]["nullable"]
    assert compare_schema(first, changed)["equal"] is False
