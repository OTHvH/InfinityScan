"""Startup dependency-readiness tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import database
import main


class _Result:
    def scalar_one(self) -> int:
        return 1


class _Connection:
    def __init__(self) -> None:
        self.statement = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement):
        self.statement = statement
        return _Result()


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def connect(self):
        return self.connection


class _Storage:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def health_check(self) -> bool:
        return self.ready


def test_database_readiness_executes_select_one(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr(database, "_engine", _Engine(connection))

    assert database.is_database_ready() is True
    assert str(connection.statement) == "SELECT 1"


def test_database_readiness_handles_connection_failure(monkeypatch):
    class UnreachableEngine:
        def connect(self):
            raise OSError("private database details")

    monkeypatch.setattr(database, "_engine", UnreachableEngine())
    assert database.is_database_ready() is False


def test_database_readiness_handles_engine_creation_failure(monkeypatch):
    def fail_to_create_engine():
        raise RuntimeError("private database details")

    monkeypatch.setattr(database, "_get_engine", fail_to_create_engine)
    assert database.is_database_ready() is False


@pytest.mark.asyncio
async def test_production_startup_fails_when_database_is_unreachable(monkeypatch):
    monkeypatch.setattr(
        main,
        "_cfg",
        SimpleNamespace(app_env="production", object_storage_enabled=False),
    )
    monkeypatch.setattr(main, "_media_storage", None)
    monkeypatch.setattr(main, "is_database_ready", lambda: False)

    with pytest.raises(RuntimeError, match="DATABASE_URL") as exc_info:
        async with main.lifespan(None):
            pytest.fail("startup must not serve requests")
    assert "private" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_development_startup_does_not_require_database(monkeypatch):
    monkeypatch.setattr(
        main,
        "_cfg",
        SimpleNamespace(app_env="development", object_storage_enabled=False),
    )
    monkeypatch.setattr(main, "_media_storage", None)
    monkeypatch.setattr(main, "is_database_ready", lambda: False)

    async with main.lifespan(None):
        assert main._http_client is not None


@pytest.mark.asyncio
async def test_startup_fails_when_enabled_storage_is_unreachable(monkeypatch):
    monkeypatch.setattr(
        main,
        "_cfg",
        SimpleNamespace(app_env="development", object_storage_enabled=True),
    )
    monkeypatch.setattr(main, "_media_storage", _Storage(ready=False))
    monkeypatch.setattr(main, "is_database_ready", lambda: True)

    with pytest.raises(RuntimeError, match="OBJECT_STORAGE_ENABLED"):
        async with main.lifespan(None):
            pytest.fail("startup must not serve requests")
