"""Database engine, session factory, and session dependency.

Provides a single source of truth for SQLAlchemy engine creation and
session management.  All other modules import from here instead of
creating their own engines.
"""

from __future__ import annotations

from typing import Generator

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from settings import get_settings

# ── Engine & session factory ─────────────────────────────────────────────────

_engine = None
_SessionLocal = None


def _get_engine():
    global _engine
    if _engine is None:
        cfg = get_settings()
        if not cfg.database_url:
            return None
        _engine = create_engine(cfg.database_url, pool_pre_ping=True)
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
