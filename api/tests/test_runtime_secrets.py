from __future__ import annotations

import pytest

from runtime_secrets import required_runtime_value, runtime_value


def test_secret_file_is_loaded_without_logging_content(tmp_path, monkeypatch):
    secret = "postgres://user:password@example/db"
    path = tmp_path / "database_url"
    path.write_text(secret + "\n", encoding="utf-8")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL_FILE", str(path))

    assert required_runtime_value("DATABASE_URL") == secret


def test_direct_and_file_values_conflict(tmp_path, monkeypatch):
    path = tmp_path / "secret"
    path.write_text("file-secret", encoding="utf-8")
    monkeypatch.setenv("JWT_SECRET_KEY", "direct-secret")
    monkeypatch.setenv("JWT_SECRET_KEY_FILE", str(path))

    with pytest.raises(ValueError, match="must not both be set") as exc_info:
        runtime_value("JWT_SECRET_KEY")
    assert "file-secret" not in str(exc_info.value)
    assert "direct-secret" not in str(exc_info.value)


def test_missing_secret_file_fails_closed(monkeypatch):
    monkeypatch.delenv("CSRF_SECRET_KEY", raising=False)
    monkeypatch.setenv("CSRF_SECRET_KEY_FILE", "/missing/csrf-secret")
    with pytest.raises(ValueError, match="could not be read"):
        required_runtime_value("CSRF_SECRET_KEY")


def test_empty_secret_file_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "empty"
    path.write_text("\n", encoding="utf-8")
    monkeypatch.delenv("CSRF_SECRET_KEY", raising=False)
    monkeypatch.setenv("CSRF_SECRET_KEY_FILE", str(path))
    with pytest.raises(ValueError, match="must not be empty"):
        required_runtime_value("CSRF_SECRET_KEY")


def test_symlink_secret_file_fails_closed(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_text("secret", encoding="utf-8")
    path = tmp_path / "link"
    path.symlink_to(target)
    monkeypatch.delenv("CSRF_SECRET_KEY", raising=False)
    monkeypatch.setenv("CSRF_SECRET_KEY_FILE", str(path))
    with pytest.raises(ValueError, match="must not be a symlink"):
        required_runtime_value("CSRF_SECRET_KEY")


def test_empty_direct_value_is_not_a_secret(monkeypatch):
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "")
    assert runtime_value("S3_ACCESS_KEY_ID") == ""
