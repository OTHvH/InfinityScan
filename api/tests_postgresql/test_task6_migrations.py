"""Task 6 PostgreSQL migration reproducibility and drift gates."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

from models import Base  # noqa: E402
from tools.migration_graph import inspect_graph  # noqa: E402
from tools.migration_manifest import verify_manifest  # noqa: E402
from tools.migration_verifier import verify_migrations  # noqa: E402
from tools.schema_snapshot import compare_schema, snapshot_engine, snapshot_metadata  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must point to a disposable PostgreSQL database")
if os.environ.get("INFINITYSCAN_DISPOSABLE_MIGRATION_TEST") != "1":
    raise RuntimeError("INFINITYSCAN_DISPOSABLE_MIGRATION_TEST=1 is required for destructive migration tests")


def _assert_disposable_url(value: str, prefix: str) -> None:
    parsed = make_url(value)
    if parsed.get_backend_name() != "postgresql" or parsed.host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("migration tests require loopback PostgreSQL targets")
    if not parsed.database or not parsed.database.startswith(prefix):
        raise RuntimeError("migration tests require gate-owned disposable database names")


_assert_disposable_url(DATABASE_URL, "phase5_migrations_")


def _config() -> Config:
    config = Config(str(API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(API_DIR / "alembic"))
    config.attributes["database_url"] = DATABASE_URL
    return config


def _reset(engine) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


@pytest.fixture()
def engine():
    result = create_engine(DATABASE_URL, pool_pre_ping=True)
    if result.dialect.name != "postgresql":
        pytest.skip("Task 6 migration tests require PostgreSQL")
    _reset(result)
    yield result
    _reset(result)
    result.dispose()


def test_manifest_graph_and_single_head_are_valid():
    manifest = API_DIR / "alembic" / "migration-manifest.json"
    versions = API_DIR / "alembic" / "versions"
    assert verify_manifest(manifest, versions) == ()
    graph = inspect_graph(versions)
    assert graph.ok
    assert graph.heads == ("0009",)
    scripts = ScriptDirectory.from_config(_config())
    assert scripts.get_heads() == ["0009"]


def test_fresh_base_head_and_alembic_current(engine):
    config = _config()
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0009"
        assert connection.scalar(text("SELECT to_regclass('public.refresh_tokens')")) is None
    command.current(config, verbose=False)


def test_full_downgrade_to_base_and_reupgrade(engine):
    config = _config()
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM alembic_version")) == 0
        assert connection.scalar(text("SELECT to_regclass('public.users')")) is None
        assert connection.scalar(
            text("SELECT count(*) FROM pg_type WHERE typname IN ('content_type_enum', 'import_job_status_enum')")
        ) == 0
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0009"


def test_live_schema_matches_canonical_orm_snapshot(engine):
    command.upgrade(_config(), "head")
    expected = snapshot_metadata(Base.metadata)
    actual = snapshot_engine(engine)
    comparison = compare_schema(expected, actual)
    assert comparison["equal"] is True

    report = verify_migrations(
        api_dir=API_DIR,
        manifest_path=API_DIR / "alembic" / "migration-manifest.json",
        database_url=DATABASE_URL,
        require_db_head=True,
        require_schema=True,
    )
    assert report.ok, report.to_dict()


def test_two_fresh_database_snapshots_are_equal(engine):
    second_url = os.environ.get("TASK6_SECOND_DATABASE_URL")
    if not second_url:
        raise RuntimeError("TASK6_SECOND_DATABASE_URL is required for the two-database reproducibility gate")
    assert second_url != DATABASE_URL
    _assert_disposable_url(second_url, "phase5_scratch_")
    second = create_engine(second_url, pool_pre_ping=True)
    try:
        _reset(second)
        command.upgrade(_config(), "head")
        second_config = Config(str(API_DIR / "alembic.ini"))
        second_config.set_main_option("script_location", str(API_DIR / "alembic"))
        # The encoded query value exercises ConfigParser-safe programmatic URLs,
        # while DATABASE_URL still points at the first database.
        separator = "&" if "?" in second_url else "?"
        second_config.attributes["database_url"] = f"{second_url}{separator}application_name=task6%25second"
        command.upgrade(second_config, "head")
        assert compare_schema(snapshot_engine(engine), snapshot_engine(second))["equal"] is True
    finally:
        _reset(second)
        second.dispose()
