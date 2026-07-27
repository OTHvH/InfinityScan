"""PostgreSQL-only simultaneous refresh regression."""

from __future__ import annotations

import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

from auth import hash_password  # noqa: E402
from models import RefreshSession, User, UserRole  # noqa: E402
from session import issue_refresh_session, rotate_refresh_token  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not DATABASE_URL.startswith("postgres"),
    reason="requires disposable PostgreSQL DATABASE_URL",
)


@pytest.fixture()
def factory():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    config = Config(str(API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(API_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    command.upgrade(config, "head")
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def test_simultaneous_refresh_has_no_surviving_fork(factory):
    setup = factory()
    user = User(
        username=f"refresh-race-{uuid.uuid4().hex[:8]}",
        hashed_password=hash_password("strongpassword123"),
        role=UserRole.user,
    )
    setup.add(user)
    setup.commit()
    raw, original_id = issue_refresh_session(setup, user.id)
    setup.close()

    def refresh_once():
        session = factory()
        try:
            return rotate_refresh_token(session, raw)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: refresh_once(), (1, 2)))

    check = factory()
    try:
        original = check.get(RefreshSession, original_id)
        assert original is not None
        rows = list(
            check.scalars(
                select(RefreshSession).where(
                    RefreshSession.family_id == original.family_id
                )
            ).all()
        )
        assert sum(result is not None for result in results) == 1
        assert all(row.revoked_at is not None for row in rows)
        assert original.replaced_by_session_id is not None
    finally:
        check.close()
