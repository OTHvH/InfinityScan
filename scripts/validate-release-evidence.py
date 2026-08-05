#!/usr/bin/env python3
"""Validate deterministic signature and provenance evidence fixtures."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


class EvidenceError(ValueError):
    """A signature or provenance evidence policy violation."""


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{path}: invalid JSON") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{path}: evidence must be an object")
    return value


def validate_evidence(
    signature: dict[str, Any],
    provenance: dict[str, Any],
    *,
    expected_identity: str,
    expected_repository: str,
    expected_digest: str,
    expected_git_sha: str,
) -> None:
    if signature.get("valid") is not True:
        raise EvidenceError("signature is not valid")
    if signature.get("identity") != expected_identity:
        raise EvidenceError("signature identity does not match")
    if signature.get("issuer") != "https://token.actions.githubusercontent.com":
        raise EvidenceError("signature issuer does not match")
    if signature.get("repository") != expected_repository:
        raise EvidenceError("signature repository does not match")
    if signature.get("digest") != expected_digest:
        raise EvidenceError("signature digest does not match")

    if provenance.get("present") is not True:
        raise EvidenceError("provenance is missing")
    if provenance.get("repository") != expected_repository:
        raise EvidenceError("provenance repository does not match")
    if provenance.get("workflow") != expected_identity:
        raise EvidenceError("provenance workflow identity does not match")
    if provenance.get("digest") != expected_digest:
        raise EvidenceError("provenance digest does not match")
    if not isinstance(provenance.get("git_sha"), str) or not re.fullmatch(
        r"[0-9a-f]{40}", provenance["git_sha"]
    ):
        raise EvidenceError("provenance Git SHA is malformed")
    if provenance["git_sha"] != expected_git_sha:
        raise EvidenceError("provenance Git SHA does not match")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--expected-identity", required=True)
    parser.add_argument("--expected-repository", required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--expected-git-sha", required=True)
    args = parser.parse_args(argv)
    try:
        validate_evidence(
            _load(args.signature),
            _load(args.provenance),
            expected_identity=args.expected_identity,
            expected_repository=args.expected_repository,
            expected_digest=args.expected_digest,
            expected_git_sha=args.expected_git_sha,
        )
    except EvidenceError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("release evidence is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
