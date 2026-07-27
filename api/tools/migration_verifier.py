"""Production migration-chain, immutability, and schema-drift verification."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from models import Base

from .migration_graph import GraphFinding, inspect_graph
from .migration_manifest import ManifestFinding, verify_manifest
from .schema_snapshot import compare_schema, snapshot_engine, snapshot_metadata


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    detail: str
    path: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail, "path": self.path}


@dataclass
class VerificationReport:
    findings: list[Finding] = field(default_factory=list)
    schema_comparison: dict[str, object] | None = None

    @property
    def ok(self) -> bool:
        return not self.findings and (self.schema_comparison is None or bool(self.schema_comparison["equal"]))

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "findings": [finding.to_dict() for finding in sorted(self.findings, key=lambda item: (item.code, item.path or ""))],
            "schema_comparison": self.schema_comparison,
        }


def _finding_from_manifest(finding: ManifestFinding) -> Finding:
    return Finding(finding.code, "error", finding.detail, finding.filename)


def _finding_from_graph(finding: GraphFinding) -> Finding:
    return Finding(finding.code, "error", finding.detail, finding.revision)


def _alembic_config(api_dir: Path, database_url: str | None = None) -> Config:
    config = Config(str(api_dir / "alembic.ini"))
    config.set_main_option("script_location", str(api_dir / "alembic"))
    if database_url:
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


@contextmanager
def _database_environment(database_url: str) -> Iterator[None]:
    before = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = before


def _check_database_head(engine, expected_head: str, findings: list[Finding]) -> None:
    with engine.connect() as connection:
        exists = connection.scalar(text("SELECT to_regclass('public.alembic_version')"))
        if exists is None:
            findings.append(Finding("missing_alembic_version", "error", "database has no alembic_version table"))
            return
        revisions = list(connection.scalars(text("SELECT version_num FROM alembic_version ORDER BY version_num")))
    if revisions != [expected_head]:
        findings.append(
            Finding(
                "database_not_at_head",
                f"database revision does not equal the single migration head (expected={expected_head}; actual={revisions})",
            )
        )


def verify_migrations(
    *,
    api_dir: Path,
    manifest_path: Path,
    database_url: str | None = None,
    scratch_url: str | None = None,
    require_db_head: bool = False,
    require_schema: bool = False,
) -> VerificationReport:
    report = VerificationReport()
    versions_dir = api_dir / "alembic" / "versions"
    report.findings.extend(_finding_from_manifest(item) for item in verify_manifest(manifest_path, versions_dir))
    graph = inspect_graph(versions_dir)
    report.findings.extend(_finding_from_graph(item) for item in graph.findings)
    if not graph.ok:
        report.findings.append(Finding("invalid_revision_graph", "error", "migration graph must have exactly one valid head"))
    if scratch_url and database_url and scratch_url == database_url:
        report.findings.append(Finding("scratch_database_equals_live", "error", "scratch migration target must differ from live target"))
    if scratch_url:
        try:
            config = _alembic_config(api_dir, scratch_url)
            with _database_environment(scratch_url):
                command.upgrade(config, "head")
        except Exception as exc:
            report.findings.append(Finding("scratch_upgrade_failed", "error", "scratch database migration failed safely: " + type(exc).__name__))
    if database_url:
        try:
            engine = create_engine(database_url, pool_pre_ping=True)
            if require_db_head:
                _check_database_head(engine, graph.heads[0] if len(graph.heads) == 1 else "", report.findings)
            if require_schema:
                expected = snapshot_metadata(Base.metadata, dialect="postgresql")
                actual = snapshot_engine(engine, schema="public")
                report.schema_comparison = compare_schema(expected, actual)
                if not report.schema_comparison["equal"]:
                    report.findings.append(Finding("schema_drift", "error", "live PostgreSQL schema differs from ORM metadata"))
            engine.dispose()
        except Exception as exc:
            report.findings.append(Finding("database_verification_failed", "error", "database verification failed safely: " + type(exc).__name__))
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify Alembic migrations and schema drift")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Verify migration manifest, graph, and optional database schema")
    check.add_argument("--api-dir", type=Path, default=Path(__file__).resolve().parents[1])
    check.add_argument("--manifest", type=Path, default=None)
    check.add_argument("--database-url", default=None)
    check.add_argument("--scratch-url", default=None)
    check.add_argument("--require-db-head", action="store_true")
    check.add_argument("--require-schema", action="store_true")
    check.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    api_dir = args.api_dir.resolve()
    manifest = args.manifest or api_dir / "alembic" / "migration-manifest.json"
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    try:
        report = verify_migrations(
            api_dir=api_dir,
            manifest_path=manifest,
            database_url=database_url,
            scratch_url=args.scratch_url,
            require_db_head=args.require_db_head,
            require_schema=args.require_schema,
        )
    except (OSError, ValueError) as exc:
        print(f"migration verification failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print("migration verification: " + ("ok" if report.ok else "failed"))
        for finding in report.findings:
            print(f"{finding.severity.upper()}: {finding.code}: {finding.detail}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
