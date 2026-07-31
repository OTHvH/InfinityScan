"""Pytest configuration for InfinityScan API tests.

Provides a test database (PostgreSQL when TEST_DATABASE_URL is set, SQLite fallback),
a TestClient, and fixtures for creating users and authenticated sessions.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# Ensure the api/ package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Force settings — ALL of these MUST be set before any app module is imported.
os.environ["COOKIE_SECURE"] = "false"
os.environ["REGISTER_RATE_LIMIT"] = "999999/second"
os.environ["LOGIN_RATE_LIMIT"] = "999999/second"
os.environ["CSRF_RATE_LIMIT"] = "999999/second"
os.environ["REFRESH_RATE_LIMIT"] = "999999/second"
os.environ["LOGOUT_RATE_LIMIT"] = "999999/second"
os.environ["BOOKMARK_WRITE_RATE_LIMIT"] = "999999/second"
os.environ["PROGRESS_WRITE_RATE_LIMIT"] = "999999/second"
os.environ["TRUSTED_HOSTS"] = "localhost,127.0.0.1,testserver"

# Select database backend: PostgreSQL if TEST_DATABASE_URL is set, else SQLite
_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")
if _TEST_DB_URL:
    os.environ["DATABASE_URL"] = _TEST_DB_URL
    _USE_POSTGRESQL = True
else:
    os.environ["DATABASE_URL"] = "sqlite://"
    _USE_POSTGRESQL = False

from auth import hash_password  # noqa: E402
from database import get_db  # noqa: E402
from main import app  # noqa: E402
from models import Base  # noqa: E402

# ── Database engine ───────────────────────────────────────────────────────────

if _USE_POSTGRESQL:
    _engine = create_engine(
        _TEST_DB_URL,
        pool_pre_ping=True,
    )
else:
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


# ── Helper functions (not fixtures) ────────────────────────────────────────


def _do_login(client, username: str, password: str):
    """Login via canonical /auth/login with pre-auth CSRF."""
    resp = client.get("/auth/csrf")
    csrf = resp.json()["csrf_token"]
    return client.post(
        "/auth/login",
        json={"username": username, "password": password},
        headers={"X-CSRF-Token": csrf},
    )


def _do_register(client, username: str, password: str, email: str | None = None):
    """Register via canonical /auth/register with pre-auth CSRF."""
    # Registration establishes an authenticated session.  Clear it when a
    # helper call represents a fresh pre-auth registration attempt.
    client.cookies.delete("is_access")
    client.cookies.delete("is_refresh")
    resp = client.get("/auth/csrf")
    csrf = resp.json()["csrf_token"]
    body = {"username": username, "password": password}
    if email:
        body["email"] = email
    return client.post(
        "/auth/register",
        json=body,
        headers={"X-CSRF-Token": csrf},
    )


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
        resp = _do_login(client, username, password)
        assert resp.status_code == 200, f"Login with failed: {resp.text}"
        return user

    return _login
