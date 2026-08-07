#!/usr/bin/env python3
"""Validate a release manifest and publication evidence before staging access."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publication = _load_module("publication_evidence", ROOT / "scripts/validate-publication-evidence.py")
manifest_module = _load_module("release_manifest", ROOT / "scripts/validate-release-manifest.py")
PublicationEvidenceError = publication.PublicationEvidenceError
validate_publication_evidence = publication.validate_publication_evidence
ManifestError = manifest_module.ManifestError
load_manifest = manifest_module.load_manifest


def validate_staging(manifest_path: Path, evidence_path: Path, repository: str, git_sha: str) -> None:
    manifest = load_manifest(manifest_path)
    if manifest["repository"].lower() != repository.lower():
        raise ValueError("manifest repository does not match")
    if manifest["git_sha"] != git_sha:
        raise ValueError("manifest Git SHA does not match")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    validate_publication_evidence(evidence, expected_repository=repository, expected_sha=git_sha)
    if evidence["release_id"] != manifest["release_id"] or evidence["run_id"] == "":
        raise ValueError("publication evidence does not bind to the manifest")
    for component in ("api", "web"):
        image = manifest["images"][component]
        proof = evidence["images"][component]
        if proof["reference"] != image["reference"] or proof["digest"] != f"sha256:{image['digest']}":
            raise ValueError(f"{component} evidence does not bind to the manifest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--git-sha", required=True)
    args = parser.parse_args(argv)
    try:
        validate_staging(args.manifest, args.evidence, args.repository, args.git_sha)
    except (OSError, json.JSONDecodeError, ManifestError, PublicationEvidenceError, ValueError) as exc:
        print(f"invalid staging release evidence: {exc}", file=sys.stderr)
        return 1
    print("staging release evidence is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
