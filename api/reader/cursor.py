"""Signed opaque cursors for deterministic local chapter traversal."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import uuid
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from settings import get_settings


CURSOR_VERSION = 1
_CURSOR_PURPOSE = b"infinityscan:reader-cursor:v1"


class ReaderDirection(str, Enum):
    next = "next"
    previous = "previous"


class CursorError(ValueError):
    """Raised when a cursor is malformed, unsupported, or out of scope."""


def canonical_decimal(value: Decimal | str) -> str:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CursorError("Invalid cursor chapter number")
    if not decimal.is_finite():
        raise CursorError("Invalid cursor chapter number")
    normalized = decimal.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _signing_key() -> bytes:
    """Derive a cursor-only key so cursor signing is purpose separated."""
    return hmac.new(
        get_settings().secret_key.encode(),
        _CURSOR_PURPOSE,
        hashlib.sha256,
    ).digest()


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_bytes(value: str) -> bytes:
    if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise CursorError("Malformed reader cursor")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise CursorError("Malformed reader cursor")
    if _encode_bytes(decoded) != value.rstrip("="):
        raise CursorError("Malformed reader cursor")
    return decoded


def encode_cursor(
    series_id: uuid.UUID,
    boundary_number: Decimal | str,
    boundary_chapter_id: uuid.UUID,
    direction: ReaderDirection,
    *,
    version: int = CURSOR_VERSION,
) -> str:
    payload = json.dumps(
        {
            "boundary_chapter_id": str(boundary_chapter_id),
            "boundary_number": canonical_decimal(boundary_number),
            "direction": direction.value,
            "series_id": str(series_id),
            "version": version,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded_payload = _encode_bytes(payload)
    signature = hmac.new(_signing_key(), encoded_payload.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded_payload}.{_encode_bytes(signature)}"


def decode_cursor(
    cursor: str,
    expected_series_id: uuid.UUID,
    expected_direction: ReaderDirection,
) -> tuple[Decimal, uuid.UUID]:
    try:
        encoded_payload, encoded_signature = cursor.split(".", 1)
        payload_bytes = _decode_bytes(encoded_payload)
        signature = _decode_bytes(encoded_signature)
        expected_signature = hmac.new(
            _signing_key(),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise CursorError("Invalid reader cursor signature")
        payload: Any = json.loads(payload_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            raise CursorError("Malformed reader cursor")
        if payload.get("version") != CURSOR_VERSION:
            raise CursorError("Unsupported reader cursor version")
        if payload.get("series_id") != str(expected_series_id):
            raise CursorError("Reader cursor belongs to another series")
        if payload.get("direction") != expected_direction.value:
            raise CursorError("Reader cursor direction mismatch")
        boundary_id = uuid.UUID(str(payload["boundary_chapter_id"]))
        boundary_number = Decimal(str(payload["boundary_number"]))
        if canonical_decimal(boundary_number) != payload["boundary_number"]:
            raise CursorError("Non-canonical reader cursor")
        return boundary_number, boundary_id
    except CursorError:
        raise
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        raise CursorError("Malformed reader cursor")
