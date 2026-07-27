"""Static, immutable Alembic migration manifest verification."""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MigrationRecord:
    filename: str
    revision: str
    down_revision: str | None
    sha256: str


@dataclass(frozen=True)
class ManifestFinding:
    code: str
    detail: str
    filename: str | None = None
    revision: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "detail": self.detail,
            "filename": self.filename,
            "revision": self.revision,
        }


def _literal_assignment(tree: ast.Module, name: str) -> Any:
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise ValueError(f"migration does not define a literal {name}")


def _single_revision(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ValueError(f"{name} must be a string or null")


def parse_migration(path: Path, *, root: Path) -> MigrationRecord:
    if path.is_symlink():
        raise ValueError(f"symlink migration is not allowed: {path}")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"migration escapes versions directory: {path}") from exc
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    revision = _literal_assignment(tree, "revision")
    if not isinstance(revision, str) or not revision:
        raise ValueError("revision must be a non-empty string")
    down_revision = _single_revision(_literal_assignment(tree, "down_revision"), "down_revision")
    return MigrationRecord(
        filename=path.relative_to(root.parent.parent).as_posix(),
        revision=revision,
        down_revision=down_revision,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def discover_migrations(versions_dir: Path, *, project_root: Path | None = None) -> tuple[MigrationRecord, ...]:
    versions_dir = versions_dir.resolve()
    records: list[MigrationRecord] = []
    for path in sorted(versions_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        records.append(parse_migration(path, root=versions_dir))
    return tuple(sorted(records, key=lambda item: (item.revision, item.filename)))


def load_manifest(path: Path) -> tuple[MigrationRecord, ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != 1 or data.get("algorithm") != "sha256":
        raise ValueError("unsupported migration manifest format")
    records = []
    for item in data.get("migrations", []):
        records.append(
            MigrationRecord(
                filename=str(item["filename"]),
                revision=str(item["revision"]),
                down_revision=item.get("down_revision"),
                sha256=str(item["sha256"]),
            )
        )
    return tuple(records)


def build_manifest(versions_dir: Path, *, project_root: Path | None = None) -> dict[str, object]:
    records = discover_migrations(versions_dir, project_root=project_root)
    return {
        "format": 1,
        "algorithm": "sha256",
        "versions_dir": "alembic/versions",
        "migrations": [
            {
                "filename": record.filename,
                "revision": record.revision,
                "down_revision": record.down_revision,
                "sha256": record.sha256,
            }
            for record in records
        ],
    }


def verify_manifest(manifest_path: Path, versions_dir: Path) -> tuple[ManifestFinding, ...]:
    findings: list[ManifestFinding] = []
    try:
        expected = load_manifest(manifest_path)
        actual = discover_migrations(versions_dir)
    except (OSError, SyntaxError, ValueError, json.JSONDecodeError) as exc:
        return (ManifestFinding("manifest_unreadable", str(exc)),)

    expected_by_filename = {record.filename: record for record in expected}
    actual_by_filename = {record.filename: record for record in actual}
    if len(expected_by_filename) != len(expected):
        findings.append(ManifestFinding("duplicate_manifest_filename", "manifest contains duplicate filenames"))
    if len({record.revision for record in expected}) != len(expected):
        findings.append(ManifestFinding("duplicate_manifest_revision", "manifest contains duplicate revisions"))
    for filename in sorted(set(expected_by_filename) - set(actual_by_filename)):
        findings.append(ManifestFinding("missing_migration", "manifested migration file is missing", filename=filename))
    for filename in sorted(set(actual_by_filename) - set(expected_by_filename)):
        findings.append(ManifestFinding("unmanifested_migration", "migration file is not in the immutable manifest", filename=filename))
    for filename in sorted(set(expected_by_filename) & set(actual_by_filename)):
        before = expected_by_filename[filename]
        after = actual_by_filename[filename]
        if before.sha256 != after.sha256:
            findings.append(ManifestFinding("modified_migration", "migration bytes do not match the manifest", filename=filename, revision=after.revision))
        if before.revision != after.revision or before.down_revision != after.down_revision:
            findings.append(ManifestFinding("migration_identity_changed", "revision or down_revision changed", filename=filename, revision=after.revision))
    return tuple(sorted(findings, key=lambda item: (item.code, item.filename or "", item.revision or "")))
