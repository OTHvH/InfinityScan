"""Deterministic tests for immutable image release metadata and policy."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-release-manifest.py"
SPEC = importlib.util.spec_from_file_location("validate_release_manifest", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VALIDATOR
SPEC.loader.exec_module(VALIDATOR)
EVIDENCE_SCRIPT = ROOT / "scripts" / "validate-release-evidence.py"
EVIDENCE_SPEC = importlib.util.spec_from_file_location("validate_release_evidence", EVIDENCE_SCRIPT)
assert EVIDENCE_SPEC is not None and EVIDENCE_SPEC.loader is not None
EVIDENCE = importlib.util.module_from_spec(EVIDENCE_SPEC)
sys.modules[EVIDENCE_SPEC.name] = EVIDENCE
EVIDENCE_SPEC.loader.exec_module(EVIDENCE)


def valid_manifest() -> dict[str, object]:
    digest = "a" * 64
    return {
        "schema_version": 1,
        "release_id": "123-1",
        "git_sha": "b" * 40,
        "repository": "owner/InfinityScan",
        "created_at": "2026-08-05T12:00:00Z",
        "lock_hashes": {
            "api/requirements.txt": "c" * 64,
            "web/package-lock.json": "d" * 64,
        },
        "images": {
            "api": {
                "repository": "ghcr.io/owner/infinityscan-api",
                "digest": digest,
                "reference": f"ghcr.io/owner/infinityscan-api@sha256:{digest}",
                "platforms": ["linux/amd64", "linux/arm64"],
                "dockerfile": "api/Dockerfile",
            },
            "web": {
                "repository": "ghcr.io/owner/infinityscan-web",
                "digest": digest,
                "reference": f"ghcr.io/owner/infinityscan-web@sha256:{digest}",
                "platforms": ["linux/amd64", "linux/arm64"],
                "dockerfile": "web/Dockerfile",
            },
        },
    }


def test_valid_manifest() -> None:
    VALIDATOR.validate_manifest(valid_manifest())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["images"]["api"].update(digest="not-a-digest"),
        lambda value: value["images"]["api"].update(reference="ghcr.io/owner/infinityscan-api:latest"),
        lambda value: value["images"]["api"].update(repository="ghcr.io/other/infinityscan-api"),
        lambda value: value["images"]["api"].update(platforms=["linux/amd64"]),
        lambda value: value.update(git_sha="not-a-commit"),
        lambda value: value.update(GITHUB_TOKEN="should-not-be-here"),
    ],
)
def test_invalid_manifest_is_rejected(mutate) -> None:
    value = copy.deepcopy(valid_manifest())
    mutate(value)
    with pytest.raises(VALIDATOR.ManifestError):
        VALIDATOR.validate_manifest(value)


def test_schema_file_is_checked_in() -> None:
    schema = json.loads((ROOT / "schemas" / "release-manifest.schema.json").read_text())
    assert schema["$schema"].endswith("draft/2020-12/schema")


def test_runtime_dockerfiles_and_contexts_enforce_image_policy() -> None:
    api_dockerfile = (ROOT / "api" / "Dockerfile").read_text()
    web_dockerfile = (ROOT / "web" / "Dockerfile").read_text()
    assert "USER appuser" in api_dockerfile
    assert "USER nextjs" in web_dockerfile
    for context in (ROOT / "api" / ".dockerignore", ROOT / "web" / ".dockerignore"):
        ignored = context.read_text()
        assert ".env" in ignored
        assert "backups/" in ignored
        assert "age-keys/" in ignored
        assert "screenshots/" in ignored


def test_signature_and_provenance_fixtures_bind_identity_and_digest() -> None:
    identity = "https://github.com/owner/InfinityScan/.github/workflows/publish-images.yml@refs/heads/phase-6-production-deployment"
    digest = "sha256:" + "a" * 64
    sha = "b" * 40
    signature = {
        "valid": True,
        "identity": identity,
        "issuer": "https://token.actions.githubusercontent.com",
        "repository": "owner/InfinityScan",
        "digest": digest,
    }
    provenance = {
        "present": True,
        "repository": "owner/InfinityScan",
        "workflow": identity,
        "digest": digest,
        "git_sha": sha,
    }
    EVIDENCE.validate_evidence(
        signature,
        provenance,
        expected_identity=identity,
        expected_repository="owner/InfinityScan",
        expected_digest=digest,
        expected_git_sha=sha,
    )
    for invalid in (
        {**signature, "identity": "wrong"},
        {**signature, "digest": "sha256:" + "c" * 64},
    ):
        with pytest.raises(EVIDENCE.EvidenceError):
            EVIDENCE.validate_evidence(
                invalid,
                provenance,
                expected_identity=identity,
                expected_repository="owner/InfinityScan",
                expected_digest=digest,
                expected_git_sha=sha,
            )


def test_offline_image_verification_requires_no_registry(tmp_path: Path) -> None:
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(json.dumps(valid_manifest()) + "\n")
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "verify-release-images.sh"),
            "--manifest",
            str(manifest),
            "--expected-repository",
            "owner/InfinityScan",
            "--offline",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
