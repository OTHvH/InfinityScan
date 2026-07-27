"""PostgreSQL-only migration and schema integrity tests.

Run explicitly with DATABASE_URL pointed at a disposable PostgreSQL database.
These tests intentionally live outside ``api/tests`` so ORM ``create_all``
fixtures cannot mask Alembic migration behavior.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

from models import Chapter  # noqa: E402
from importing.service import SeriesImportLock  # noqa: E402
from tools.integrity import repair_safe  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must point to a disposable PostgreSQL database")

SERIES_ID = uuid.UUID("71000000-0000-0000-0000-000000000001")
CHAPTER_ID = uuid.UUID("71000000-0000-0000-0000-000000000002")
PAGE_ID = uuid.UUID("71000000-0000-0000-0000-000000000003")
USER_ID = uuid.UUID("71000000-0000-0000-0000-000000000004")
PROGRESS_ID = uuid.UUID("71000000-0000-0000-0000-000000000005")
SOURCE_ID = uuid.UUID("71000000-0000-0000-0000-000000000006")
SOURCE_SERIES_ID = uuid.UUID("71000000-0000-0000-0000-000000000007")
JOB_ID = uuid.UUID("71000000-0000-0000-0000-000000000008")
ITEM_ID = uuid.UUID("71000000-0000-0000-0000-000000000009")
SESSION_ID = uuid.UUID("71000000-0000-0000-0000-000000000010")

PHASE5_CONSTRAINTS = {
    "ck_chapters_page_count_nonnegative",
    "ck_pages_page_number_positive",
    "ck_pages_file_size_positive",
    "ck_pages_width_positive",
    "ck_pages_height_positive",
    "ck_pages_sha256_format",
    "ck_pages_verified_requirements",
    "ck_reading_progress_last_page_nonnegative",
    "ck_reading_progress_scroll_position_range",
    "ck_import_jobs_nonnegative_counts",
    "ck_refresh_sessions_token_hash_format",
    "ck_refresh_sessions_expires_after_created",
    "ck_refresh_sessions_last_used_after_created",
    "ck_refresh_sessions_revoked_after_created",
    "ck_refresh_sessions_replacement_not_self",
    "ck_source_series_external_id_nonempty",
}

RECOVERY_CONSTRAINTS = {
    "ck_import_jobs_recovery_numbers_nonnegative",
    "ck_import_jobs_lease_fields_paired",
    "ck_import_jobs_recovery_json_objects",
    "ck_import_job_items_attempt_count_nonnegative",
    "uq_import_job_items_job_item_key",
}


def _alembic_config() -> Config:
    config = Config(str(API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(API_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.fixture(scope="session")
def engine():
    result = create_engine(DATABASE_URL, pool_pre_ping=True)
    if result.dialect.name != "postgresql":
        raise RuntimeError("Phase 5 schema tests require PostgreSQL")
    yield result
    result.dispose()


def _reset_schema(engine) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


@pytest.fixture(autouse=True)
def clean_schema(engine):
    _reset_schema(engine)
    yield
    _reset_schema(engine)


def _current_revision(engine) -> str | None:
    with engine.connect() as connection:
        exists = connection.scalar(text("SELECT to_regclass('public.alembic_version')"))
        if exists is None:
            return None
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def _constraint_names(engine) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.scalars(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE connamespace = 'public'::regnamespace"
                )
            )
        )


def _index_names(engine) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.scalars(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
        )


def _seed_valid_graph(engine) -> None:
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO series "
                "(id, slug, title, content_type, default_reading_mode, status, is_nsfw) "
                "VALUES (:id, 'phase5-schema', 'Phase 5 Schema', 'manga', 'paged', 'ongoing', false)"
            ),
            {"id": SERIES_ID},
        )
        connection.execute(
            text(
                "INSERT INTO chapters "
                "(id, series_id, number, language, page_count, import_status, verified_at) "
                "VALUES (:id, :series_id, 1, 'en', 1, 'ready', :verified_at)"
            ),
            {"id": CHAPTER_ID, "series_id": SERIES_ID, "verified_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO pages "
                "(id, chapter_id, page_number, object_key, width, height, file_size, sha256, "
                "mime_type, file_extension, integrity_status, verified_at) "
                "VALUES (:id, :chapter_id, 1, 'series/phase5/page.jpg', 100, 200, 10, :sha256, "
                "'image/jpeg', 'jpg', 'verified', :verified_at)"
            ),
            {
                "id": PAGE_ID,
                "chapter_id": CHAPTER_ID,
                "sha256": "a" * 64,
                "verified_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (:id, 'phase5-user', 'hash', 'user', true)"
            ),
            {"id": USER_ID},
        )
        connection.execute(
            text(
                "INSERT INTO reading_progress "
                "(id, user_id, chapter_id, last_page, scroll_position, completed) "
                "VALUES (:id, :user_id, :chapter_id, 1, 0.5, false)"
            ),
            {"id": PROGRESS_ID, "user_id": USER_ID, "chapter_id": CHAPTER_ID},
        )
        connection.execute(
            text(
                "INSERT INTO sources (id, key, display_name, adapter_type, enabled, configuration) "
                "VALUES (:id, 'phase5', 'Phase 5', 'local', true, '{}')"
            ),
            {"id": SOURCE_ID},
        )
        connection.execute(
            text(
                "INSERT INTO source_series (id, source_id, series_id, external_series_id) "
                "VALUES (:id, :source_id, :series_id, 'external-1')"
            ),
            {"id": SOURCE_SERIES_ID, "source_id": SOURCE_ID, "series_id": SERIES_ID},
        )
        connection.execute(
            text(
                "INSERT INTO import_jobs "
                "(id, source_id, status, idempotency_key, manifest_hash, series_count, "
                "chapter_count, page_count, uploaded_count, skipped_count, failed_count) "
                "VALUES (:id, :source_id, 'pending', 'phase5-job', :manifest_hash, 1, 1, 1, 1, 0, 0)"
            ),
            {"id": JOB_ID, "source_id": SOURCE_ID, "manifest_hash": "b" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO import_job_items "
                "(id, job_id, source_reference, object_key, sha256, status) "
                "VALUES (:id, :job_id, 'page.jpg', 'series/phase5/page.jpg', :sha256, 'succeeded')"
            ),
            {"id": ITEM_ID, "job_id": JOB_ID, "sha256": "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO refresh_sessions "
                "(id, user_id, family_id, token_hash, created_at, expires_at) "
                "VALUES (:id, :user_id, :family_id, :token_hash, :created_at, :expires_at)"
            ),
            {
                "id": SESSION_ID,
                "user_id": USER_ID,
                "family_id": uuid.uuid4(),
                "token_hash": "c" * 64,
                "created_at": now,
                "expires_at": now + timedelta(days=1),
            },
        )


def _assert_constraint_failure(engine, statement: str, expected_constraint: str) -> None:
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(IntegrityError) as exc_info:
            connection.execute(text(statement))
        assert exc_info.value.orig.diag.constraint_name == expected_constraint
        transaction.rollback()


def test_clean_upgrade_downgrade_and_reupgrade(engine):
    config = _alembic_config()
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_current_head() == "0007"

    command.upgrade(config, "0006")
    assert _current_revision(engine) == "0006"
    assert PHASE5_CONSTRAINTS <= _constraint_names(engine)
    assert {
        "ix_chapters_reader_order",
        "ix_refresh_sessions_replaced_by_session_id",
    } <= _index_names(engine)
    command.downgrade(config, "-1")
    assert _current_revision(engine) == "0005"
    assert not (PHASE5_CONSTRAINTS - {"ck_pages_verified_requirements"}) & _constraint_names(engine)
    assert "ck_pages_verified_requirements" in _constraint_names(engine)

    command.upgrade(config, "head")
    assert _current_revision(engine) == "0007"
    assert PHASE5_CONSTRAINTS <= _constraint_names(engine)
    command.check(config)


def test_import_recovery_migration_is_reversible(engine):
    config = _alembic_config()
    command.upgrade(config, "head")
    assert _current_revision(engine) == "0007"
    assert RECOVERY_CONSTRAINTS <= _constraint_names(engine)
    assert {
        "ix_import_jobs_heartbeat_at",
        "ix_import_jobs_lease_expires_at",
        "ix_import_jobs_recovery_candidates",
        "ix_import_job_items_series_id",
        "ix_import_job_items_chapter_id",
    } <= _index_names(engine)
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'import_jobs' AND column_name = 'resume_payload'"
            )
        ) == 1

    command.downgrade(config, "0006")
    assert _current_revision(engine) == "0006"
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'import_jobs' AND column_name = 'resume_payload'"
            )
        ) == 0
    command.upgrade(config, "head")
    assert _current_revision(engine) == "0007"


def test_import_recovery_constraints_reject_invalid_state(engine):
    command.upgrade(_alembic_config(), "head")
    _seed_valid_graph(engine)
    _assert_constraint_failure(
        engine,
        f"UPDATE import_jobs SET lease_owner_id = '{uuid.uuid4()}', lease_expires_at = NULL",
        "ck_import_jobs_lease_fields_paired",
    )
    _assert_constraint_failure(
        engine,
        "UPDATE import_jobs SET fencing_token = -1",
        "ck_import_jobs_recovery_numbers_nonnegative",
    )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE import_job_items SET item_key = 'page:one' WHERE id = :id"),
            {"id": ITEM_ID},
        )
    _assert_constraint_failure(
        engine,
        "INSERT INTO import_job_items "
        "(id, job_id, source_reference, object_key, item_key, status) "
        f"VALUES ('{uuid.uuid4()}', '{JOB_ID}', 'safe-ref', 'safe-key', "
        "'page:one', 'pending')",
        "uq_import_job_items_job_item_key",
    )


def test_postgresql_advisory_lock_serializes_recovery(engine):
    command.upgrade(_alembic_config(), "head")
    with Session(engine) as first, Session(engine) as second:
        owner = SeriesImportLock(first, f"job:{JOB_ID}")
        contender = SeriesImportLock(second, f"job:{JOB_ID}")
        assert owner.acquire(blocking=False)
        try:
            assert not contender.acquire(blocking=False)
        finally:
            owner.release()
        assert contender.acquire(blocking=False)
        contender.release()


def test_dirty_data_preflight_aborts_without_repair(engine):
    config = _alembic_config()
    command.upgrade(config, "0005")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO series "
                "(id, slug, title, content_type, default_reading_mode, status, is_nsfw) "
                "VALUES (:id, 'dirty-series', 'Dirty', 'manga', 'paged', 'ongoing', false)"
            ),
            {"id": SERIES_ID},
        )
        connection.execute(
            text(
                "INSERT INTO chapters "
                "(id, series_id, number, language, page_count, import_status) "
                "VALUES (:id, :series_id, 1, 'en', 1, 'importing')"
            ),
            {"id": CHAPTER_ID, "series_id": SERIES_ID},
        )
        connection.execute(
            text(
                "INSERT INTO pages (id, chapter_id, page_number, object_key, integrity_status) "
                "VALUES (:id, :chapter_id, 0, 'dirty/page.jpg', 'pending')"
            ),
            {"id": PAGE_ID, "chapter_id": CHAPTER_ID},
        )

    with pytest.raises(DBAPIError, match="pages.page_number >= 1"):
        command.upgrade(config, "head")
    assert _current_revision(engine) == "0005"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT page_number FROM pages WHERE id = :id"), {"id": PAGE_ID}) == 0


def test_every_phase5_check_constraint_rejects_invalid_rows(engine):
    command.upgrade(_alembic_config(), "head")
    _seed_valid_graph(engine)
    checks = (
        ("UPDATE chapters SET page_count = -1", "ck_chapters_page_count_nonnegative"),
        ("UPDATE pages SET page_number = 0", "ck_pages_page_number_positive"),
        ("UPDATE pages SET file_size = 0", "ck_pages_file_size_positive"),
        ("UPDATE pages SET width = 0", "ck_pages_width_positive"),
        ("UPDATE pages SET height = -1", "ck_pages_height_positive"),
        ("UPDATE pages SET sha256 = 'ABC'", "ck_pages_sha256_format"),
        ("UPDATE pages SET mime_type = NULL", "ck_pages_verified_requirements"),
        ("UPDATE reading_progress SET last_page = -1", "ck_reading_progress_last_page_nonnegative"),
        ("UPDATE reading_progress SET scroll_position = 1.1", "ck_reading_progress_scroll_position_range"),
        ("UPDATE import_jobs SET series_count = -1", "ck_import_jobs_nonnegative_counts"),
        ("UPDATE import_jobs SET chapter_count = -1", "ck_import_jobs_nonnegative_counts"),
        ("UPDATE import_jobs SET page_count = -1", "ck_import_jobs_nonnegative_counts"),
        ("UPDATE import_jobs SET uploaded_count = -1", "ck_import_jobs_nonnegative_counts"),
        ("UPDATE import_jobs SET skipped_count = -1", "ck_import_jobs_nonnegative_counts"),
        ("UPDATE import_jobs SET failed_count = -1", "ck_import_jobs_nonnegative_counts"),
        ("UPDATE refresh_sessions SET token_hash = 'invalid'", "ck_refresh_sessions_token_hash_format"),
        ("UPDATE refresh_sessions SET expires_at = created_at", "ck_refresh_sessions_expires_after_created"),
        (
            "UPDATE refresh_sessions SET last_used_at = created_at - interval '1 second'",
            "ck_refresh_sessions_last_used_after_created",
        ),
        (
            "UPDATE refresh_sessions SET revoked_at = created_at - interval '1 second'",
            "ck_refresh_sessions_revoked_after_created",
        ),
        (
            "UPDATE refresh_sessions SET replaced_by_session_id = id",
            "ck_refresh_sessions_replacement_not_self",
        ),
        ("UPDATE source_series SET external_series_id = ' '", "ck_source_series_external_id_nonempty"),
    )
    for statement, constraint in checks:
        _assert_constraint_failure(engine, statement, constraint)


def test_existing_uniqueness_and_foreign_key_cascades(engine):
    command.upgrade(_alembic_config(), "head")
    _seed_valid_graph(engine)

    _assert_constraint_failure(
        engine,
        "INSERT INTO pages (id, chapter_id, page_number, object_key, integrity_status) "
        f"VALUES ('{uuid.uuid4()}', '{CHAPTER_ID}', 1, 'other.jpg', 'pending')",
        "uq_page_chapter_number",
    )
    _assert_constraint_failure(
        engine,
        "INSERT INTO source_series (id, source_id, series_id, external_series_id) "
        f"VALUES ('{uuid.uuid4()}', '{SOURCE_ID}', '{SERIES_ID}', 'external-1')",
        "uq_source_series_external_id",
    )

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM series WHERE id = :id"), {"id": SERIES_ID})
        assert connection.scalar(text("SELECT count(*) FROM chapters WHERE id = :id"), {"id": CHAPTER_ID}) == 0
        assert connection.scalar(text("SELECT count(*) FROM pages WHERE id = :id"), {"id": PAGE_ID}) == 0
        assert connection.scalar(text("SELECT count(*) FROM reading_progress WHERE id = :id"), {"id": PROGRESS_ID}) == 0
        assert connection.scalar(text("SELECT count(*) FROM source_series WHERE id = :id"), {"id": SOURCE_SERIES_ID}) == 0
        connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": USER_ID})
        assert connection.scalar(text("SELECT count(*) FROM refresh_sessions WHERE id = :id"), {"id": SESSION_ID}) == 0
        connection.execute(text("DELETE FROM import_jobs WHERE id = :id"), {"id": JOB_ID})
        assert connection.scalar(text("SELECT count(*) FROM import_job_items WHERE id = :id"), {"id": ITEM_ID}) == 0


def test_repair_safe_respects_migrated_constraints(engine):
    command.upgrade(_alembic_config(), "head")
    _seed_valid_graph(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE chapters SET page_count = 2 WHERE id = :id"), {"id": CHAPTER_ID})

    with Session(engine) as session:
        summary = repair_safe(session, None)
        assert summary.total_issues == 0
        assert session.get(Chapter, CHAPTER_ID).page_count == 1

    _assert_constraint_failure(engine, "UPDATE pages SET file_size = 0", "ck_pages_file_size_positive")
