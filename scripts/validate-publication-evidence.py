#!/usr/bin/env python3
"""Validate the safe evidence emitted by the immutable image publication workflow."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "publication-evidence.schema.json"
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class PublicationEvidenceError(ValueError):
    """A publication evidence policy violation."""


def _schema_errors(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/$defs/"):
            return [f"{path}: unsupported schema reference"]
        definitions = json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]
        return _schema_errors(value, definitions[reference.removeprefix("#/$defs/")], path)

    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: {value!r} does not match the required constant")
    expected_type = schema.get("type")
    type_matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
    }
    if expected_type in type_matches and not type_matches[expected_type]:
        return [f"{path}: expected {expected_type}"]
    if isinstance(value, str) and "pattern" in schema and not re.fullmatch(schema["pattern"], value):
        errors.append(f"{path}: value does not match the required pattern")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                errors.append(f"{path}: missing required property {required!r}")
        if schema.get("additionalProperties") is False:
            errors.extend(f"{path}: unexpected property {key!r}" for key in value if key not in properties)
        for key, child_schema in properties.items():
            if key in value:
                errors.extend(_schema_errors(value[key], child_schema, f"{path}.{key}"))
    if isinstance(value, list) and "const" not in schema and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(_schema_errors(item, schema["items"], f"{path}[{index}]"))
    return errors


def validate_publication_evidence(data: Any, *, expected_repository: str | None = None, expected_sha: str | None = None) -> None:
    errors = _schema_errors(data, json.loads(SCHEMA.read_text(encoding="utf-8")))
    if errors:
        raise PublicationEvidenceError(errors[0])
    if expected_repository and data["repository"].lower() != expected_repository.lower():
        raise PublicationEvidenceError("publication evidence repository does not match")
    if expected_sha and data["git_sha"] != expected_sha:
        raise PublicationEvidenceError("publication evidence Git SHA does not match")
    for name, image in data["images"].items():
        if not SHA256.fullmatch(image["digest"]):
            raise PublicationEvidenceError(f"{name} digest is malformed")
        if image["reference"] != image["reference"].split("@", 1)[0] + "@" + image["digest"]:
            raise PublicationEvidenceError(f"{name} reference does not match digest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--expected-repository")
    parser.add_argument("--expected-git-sha")
    args = parser.parse_args(argv)
    try:
        validate_publication_evidence(
            json.loads(args.evidence.read_text(encoding="utf-8")),
            expected_repository=args.expected_repository,
            expected_sha=args.expected_git_sha,
        )
    except (OSError, json.JSONDecodeError, PublicationEvidenceError) as exc:
        print(f"invalid publication evidence: {exc}", file=sys.stderr)
        return 1
    print(f"valid publication evidence: {args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
