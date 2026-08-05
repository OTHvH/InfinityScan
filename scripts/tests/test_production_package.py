from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]
VALID_DIGEST = "0" * 64


def make_fixture(tmp_path: Path, compose_text: str | None = None) -> tuple[Path, Path, Path]:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    for name, value in {
        "database_url": "postgresql+psycopg://u:p@db.example/app",
        "jwt_secret_key": "j" * 32,
        "csrf_secret_key": "c" * 32,
        "s3_access_key_id": "access",
        "s3_secret_access_key": "secret",
    }.items():
        path = secret_dir / name
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)

    env = tmp_path / "production.env"
    env.write_text(
        "\n".join(
            [
                "APP_ENV=production",
                "APP_DOMAIN=app.example.test",
                f"CADDY_IMAGE=ghcr.io/example/caddy@sha256:{VALID_DIGEST}",
                f"API_IMAGE=ghcr.io/example/infinityscan-api@sha256:{VALID_DIGEST}",
                f"WEB_IMAGE=ghcr.io/example/infinityscan-web@sha256:{VALID_DIGEST}",
                f"DATABASE_URL_FILE={secret_dir / 'database_url'}",
                f"JWT_SECRET_KEY_FILE={secret_dir / 'jwt_secret_key'}",
                f"CSRF_SECRET_KEY_FILE={secret_dir / 'csrf_secret_key'}",
                f"S3_ACCESS_KEY_ID_FILE={secret_dir / 's3_access_key_id'}",
                f"S3_SECRET_ACCESS_KEY_FILE={secret_dir / 's3_secret_access_key'}",
                "TRUSTED_HOSTS=app.example.test",
                "ALLOWED_ORIGINS=https://app.example.test",
                "CORS_ORIGINS=",
                "TRUST_FORWARDED_HEADERS=true",
                "TRUSTED_PROXY_CIDRS=172.31.0.0/24",
                "OBJECT_STORAGE_ENABLED=true",
                "S3_ENDPOINT_URL=https://account.r2.cloudflarestorage.com",
                "S3_REGION=auto",
                "S3_BUCKET=application-bucket",
                "MEDIA_CSP_ORIGINS=https://account.r2.cloudflarestorage.com",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    compose = tmp_path / "compose.yml"
    compose.write_text(compose_text or (ROOT / "infra/docker-compose.production.yml").read_text(), encoding="utf-8")
    return env, compose, secret_dir


def run_validator(env: Path, compose: Path, secrets: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROOT / "scripts/validate-production-config.sh"), str(env), str(compose), str(secrets)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=os.environ.copy(),
    )


def test_valid_production_compose_is_accepted(tmp_path):
    env, compose, secrets = make_fixture(tmp_path)
    result = run_validator(env, compose, secrets)
    assert result.returncode == 0, result.stderr


def test_mutable_image_is_rejected(tmp_path):
    env, compose, secrets = make_fixture(tmp_path)
    compose.write_text(compose.read_text().replace("${WEB_IMAGE:?WEB_IMAGE must be an immutable digest}", "ghcr.io/example/web:latest"))
    result = run_validator(env, compose, secrets)
    assert result.returncode != 0
    assert "digest" in result.stderr


def test_minio_service_is_rejected(tmp_path):
    env, compose, secrets = make_fixture(tmp_path)
    compose.write_text(
        compose.read_text().replace(
            "\nnetworks:",
            "\n  minio:\n    image: ghcr.io/example/minio@sha256:" + VALID_DIGEST + "\n\nnetworks:",
        )
    )
    result = run_validator(env, compose, secrets)
    assert result.returncode != 0
    assert "MinIO" in result.stderr


def test_api_host_port_is_rejected(tmp_path):
    env, compose, secrets = make_fixture(tmp_path)
    compose.write_text(compose.read_text().replace("  api:\n    image:", '  api:\n    ports: ["8000:8000"]\n    image:'))
    result = run_validator(env, compose, secrets)
    assert result.returncode != 0
    assert "host ports" in result.stderr


def test_web_backend_secret_is_rejected(tmp_path):
    env, compose, secrets = make_fixture(tmp_path)
    compose.write_text(compose.read_text().replace("      NEXT_PUBLIC_API_URL: /api", "      NEXT_PUBLIC_API_URL: /api\n      JWT_SECRET_KEY_FILE: /run/secrets/jwt_secret_key"))
    result = run_validator(env, compose, secrets)
    assert result.returncode != 0
    assert "backend secrets" in result.stderr


def test_migration_image_must_match_api(tmp_path):
    env, compose, secrets = make_fixture(tmp_path)
    compose.write_text(compose.read_text().replace(
        "  migrate:\n    image: ${API_IMAGE:?API_IMAGE must be an immutable digest}",
        "  migrate:\n    image: ${WEB_IMAGE:?WEB_IMAGE must be an immutable digest}",
    ))
    result = run_validator(env, compose, secrets)
    assert result.returncode != 0
    assert "migration image" in result.stderr


def test_privileged_service_is_rejected(tmp_path):
    env, compose, secrets = make_fixture(tmp_path)
    compose.write_text(compose.read_text().replace("  api:\n    image:", "  api:\n    privileged: true\n    image:"))
    result = run_validator(env, compose, secrets)
    assert result.returncode != 0
    assert "privileged" in result.stderr


def test_docker_socket_mount_is_rejected(tmp_path):
    env, compose, secrets = make_fixture(tmp_path)
    compose.write_text(compose.read_text().replace(
        "      - caddy_config:/config",
        "      - caddy_config:/config\n      - /var/run/docker.sock:/var/run/docker.sock",
    ))
    result = run_validator(env, compose, secrets)
    assert result.returncode != 0
    assert "Docker socket" in result.stderr


def test_missing_healthcheck_is_rejected(tmp_path):
    env, compose, secrets = make_fixture(tmp_path)
    compose.write_text(compose.read_text().replace("  api:\n    image:", "  api:\n    healthcheck: null\n    image:"))
    result = run_validator(env, compose, secrets)
    assert result.returncode != 0
    assert "healthcheck" in result.stderr
