"""Pytest configuration for InfinityScan API tests.

Provides an in-memory SQLite database, a TestClient, and fixtures for
creating users and authenticated sessions.
"""

from __future__ import annotations

import os
import sys

# Ensure the api/ package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Force SQLite + disable rate limits + disable secure cookies —
# ALL of these MUST be set before any app module is imported.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["COOKIE_SECURE"] = "false"
os.environ["REGISTER_RATE_LIMIT"] = "999999/second"
os.environ["LOGIN_RATE_LIMIT"] = "999999/second"
os.environ["CSRF_RATE_LIMIT"] = "999999/second"
os.environ["REFRESH_RATE_LIMIT"] = "999999/second"
os.environ["LOGOUT_RATE_LIMIT"] = "999999/second"
os.environ["BOOKMARK_WRITE_RATE_LIMIT"] = "999999/second"
os.environ["PROGRESS_WRITE_RATE_LIMIT"] = "999999/second"
os.environ["TRUSTED_HOSTS"] = "localhost,127.0.0.1,testserver"

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from models import Base
from main import app
from database import get_db
from auth import hash_password

# ── SQLite with StaticPool (one shared connection) ────────────────────────────

from sqlalchemy.pool import StaticPool

_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


@event.listens_for(_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


_TestSession = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _create_tables():
    """Create all tables before each test, drop after."""
    Base.metadata.create_all(_engine)
    yield
    Base.metadata.drop_all(_engine)


def _override_get_db():
    db = _TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture()
def db():
    """Yield a test DB session."""
    session = _TestSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client():
    """Yield a FastAPI TestClient."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def user_factory(db):
    """Return a factory that creates users in the test DB."""

    def _create(
        username: str | None = None,
        password: str = "strongpassword123",
        role: str = "user",
        email: str | None = None,
    ):
        from models import User, UserRole

        uname = username or f"user_{uuid.uuid4().hex[:8]}"
        role_enum = UserRole.admin if role == "admin" else UserRole.user
        user = User(
            username=uname,
            email=email,
            hashed_password=hash_password(password),
            role=role_enum,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    return _create


@pytest.fixture()
def auth_client(client, user_factory):
    """Return a function that creates a user and logs in via cookies."""

    def _login(username: str = "testuser", password: str = "strongpassword123"):
        user = user_factory(username=username, password=password)
        resp = client.post("/token", json={"username": username, "password": password})
        assert resp.status_code == 200, f"Login failed: {resp.text}"
        return user

    return _login
