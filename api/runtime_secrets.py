"""Load runtime values from direct environment variables or protected files."""

from __future__ import annotations

import os
from pathlib import Path


def runtime_value(name: str) -> str | None:
    """Return one value, rejecting direct/file ambiguity and empty files."""
    direct_present = name in os.environ
    file_name = f"{name}_FILE"
    file_present = file_name in os.environ
    if direct_present and file_present:
        raise ValueError(f"{name} and {file_name} must not both be set")

    if direct_present:
        return os.environ.get(name)
    if not file_present:
        return None

    file_path = os.environ.get(file_name, "")
    if not file_path:
        raise ValueError(f"{file_name} must name a non-empty file")
    path = Path(file_path)
    if path.is_symlink():
        raise ValueError(f"{file_name} must not be a symlink")
    try:
        value = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"{file_name} could not be read") from exc
    value = value.rstrip("\r\n")
    if not value.strip():
        raise ValueError(f"{file_name} must not be empty")
    return value


def required_runtime_value(name: str) -> str:
    value = runtime_value(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} or {name}_FILE must be set")
    return value
