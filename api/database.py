"""Database engine, session factory, and session dependency.

Provides a single source of truth for SQLAlchemy engine creation and
session management.  All other modules import from here instead of
creating their own engines.
"""

from __future__ import annotations

from pathlib import Path
from typing import Generator

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from settings import get_settings
from tools.migration_graph import inspect_graph

# ── Engine & session factory ─────────────────────────────────────────────────

_engine: Engine | None = None
_SessionLocal = None


def _engine_options(database_url: str, cfg) -> dict:
    """Build bounded engine options without rewriting credential-bearing URLs."""
    parsed = make_url(database_url)
    options: dict = {"pool_pre_ping": True}
    if parsed.get_backend_name() != "postgresql":
        return options

    options.update(
        {
            "pool_size": cfg.db_pool_size,
            "max_overflow": cfg.db_max_overflow,
            "pool_timeout": cfg.db_pool_timeout_seconds,
            "pool_recycle": cfg.db_pool_recycle_seconds,
        }
    )
    connect_args = {
        "connect_timeout": cfg.db_connect_timeout_seconds,
        "sslmode": cfg.db_sslmode,
        "options": f"-c statement_timeout={cfg.db_statement_timeout_ms}",
    }
    if cfg.db_sslrootcert:
        connect_args["sslrootcert"] = cfg.db_sslrootcert
    options["connect_args"] = connect_args
    return options


def _get_engine():
    global _engine
    if _engine is None:
        cfg = get_settings()
        if not cfg.database_url:
            return None
        _engine = create_engine(cfg.database_url, **_engine_options(cfg.database_url, cfg))
    return _engine


def _get_session_local():
    global _SessionLocal
    if _SessionLocal is None:
        engine = _get_engine()
        if engine is None:
            return None
        _SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return _SessionLocal


def is_database_ready() -> bool:
    """Return whether the configured database accepts a minimal query."""
    try:
        engine = _get_engine()
        if engine is None:
            return False
        with engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar_one() == 1
    except Exception:
        return False


def expected_migration_head() -> str | None:
    report = inspect_graph(Path(__file__).parent / "alembic" / "versions")
    if not report.ok:
        return None
    return report.heads[0]


def is_database_revision_ready() -> bool:
    """Return whether PostgreSQL has exactly the repository's migration head."""
    expected = expected_migration_head()
    if expected is None:
        return False
    try:
        engine = _get_engine()
        if engine is None:
            return False
        with engine.connect() as connection:
            revisions = list(
                connection.scalars(
                    text("SELECT version_num FROM alembic_version ORDER BY version_num")
                )
            )
        return revisions == [expected]
    except Exception:
        return False


def get_db() -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session; called per-request."""
    SessionLocal = _get_session_local()
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_all_tables():
    """Create all tables (for testing only)."""
    from models import Base

    engine = _get_engine()
    if engine:
        Base.metadata.create_all(engine)


def drop_all_tables():
    """Drop all tables (for testing only)."""
    from models import Base

    engine = _get_engine()
    if engine:
        Base.metadata.drop_all(engine)
