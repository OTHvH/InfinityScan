#!/usr/bin/env python3
"""Validate an immutable InfinityScan image release manifest."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "release-manifest.schema.json"
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^[^/\s]+/[^/\s]+$")
IMAGE_REPOSITORY = re.compile(r"^ghcr\.io/[^/@\s]+/[^/@\s]+$")
REFERENCE = re.compile(r"^ghcr\.io/[^/@\s]+/[^/@\s]+@sha256:([0-9a-f]{64})$")
RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PLATFORMS = ["linux/amd64", "linux/arm64"]


class ManifestError(ValueError):
    """A release manifest policy violation."""


def _object(value: Any, name: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{name} must be an object")
    unexpected = set(value) - keys
    missing = keys - set(value)
    if unexpected:
        raise ManifestError(f"{name} has unexpected fields: {sorted(unexpected)}")
    if missing:
        raise ManifestError(f"{name} is missing fields: {sorted(missing)}")
    return value


def _string(value: Any, name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ManifestError(f"{name} is malformed")
    return value


def _validate_image(value: Any, name: str, expected_dockerfile: str) -> None:
    image = _object(
        value,
        name,
        {"repository", "digest", "reference", "platforms", "dockerfile"},
    )
    repository = _string(image["repository"], f"{name}.repository", IMAGE_REPOSITORY)
    digest = _string(image["digest"], f"{name}.digest", SHA256)
    reference = _string(image["reference"], f"{name}.reference", REFERENCE)
    if reference != f"{repository}@sha256:{digest}":
        raise ManifestError(f"{name}.reference does not match repository and digest")
    platforms = image["platforms"]
    if platforms != PLATFORMS:
        raise ManifestError(f"{name}.platforms must be exactly {PLATFORMS}")
    if image["dockerfile"] != expected_dockerfile:
        raise ManifestError(f"{name}.dockerfile does not match the image name")


def validate_manifest(data: Any) -> None:
    manifest = _object(
        data,
        "manifest",
        {"schema_version", "release_id", "git_sha", "repository", "created_at", "lock_hashes", "images"},
    )
    if manifest["schema_version"] != 1:
        raise ManifestError("schema_version must be 1")
    _string(manifest["release_id"], "release_id", RELEASE_ID)
    _string(manifest["git_sha"], "git_sha", SHA1)
    repository = _string(manifest["repository"], "repository", REPOSITORY)
    if not isinstance(manifest["created_at"], str):
        raise ManifestError("created_at must be an ISO-8601 string")
    try:
        datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError("created_at must be an ISO-8601 timestamp") from exc

    locks = _object(
        manifest["lock_hashes"],
        "lock_hashes",
        {"api/requirements.txt", "web/package-lock.json"},
    )
    for name, value in locks.items():
        _string(value, f"lock_hashes.{name}", SHA256)

    images = _object(manifest["images"], "images", {"api", "web"})
    _validate_image(images["api"], "images.api", "api/Dockerfile")
    _validate_image(images["web"], "images.web", "web/Dockerfile")
    repository_owner = repository.split("/", 1)[0].lower()
    for component in ("api", "web"):
        image_owner = images[component]["repository"].split("/", 3)[1].lower()
        if image_owner != repository_owner:
            raise ManifestError(f"images.{component}.repository owner does not match repository")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{path}: invalid JSON: {exc}") from exc
    validate_manifest(data)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--schema", type=Path, default=SCHEMA)
    args = parser.parse_args(argv)
    try:
        if not args.schema.is_file():
            raise ManifestError(f"schema is missing: {args.schema}")
        load_manifest(args.manifest)
    except ManifestError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"valid release manifest: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
