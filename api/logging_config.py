"""Provider-neutral structured logging and secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

from request_context import client_ip_context, request_id_context, user_id_context


_SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|cookie|set-cookie|password|passwd|secret|token|credential|"
    r"private[_-]?key|database[_-]?url|access[_-]?key|signature|x-amz|"
    r"csrf|jwt|refresh|presigned|signed[_-]?url|age[_-]?(?:identity|private))",
    re.IGNORECASE,
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_URL_CREDENTIAL_RE = re.compile(
    r"((?:https?|postgres(?:ql)?(?:\+[A-Za-z0-9_]+)?)://)([^\s/:@]+):([^\s/@]+)@",
    re.IGNORECASE,
)
_SIGNED_URL_RE = re.compile(r"(https?://[^\s?]+)\?[^\s]+", re.IGNORECASE)
_ENV_SECRET_RE = re.compile(
    r"\b(?:DATABASE_URL|S3_ACCESS_KEY_ID|S3_SECRET_ACCESS_KEY|AGE_IDENTITY_FILE)\s*=\s*[^\s,;]+",
    re.IGNORECASE,
)
_HEADER_SECRET_RE = re.compile(
    r"\b(?:authorization|cookie|set-cookie|x-csrf-token|csrf-token)\s*:\s*[^\r\n]+",
    re.IGNORECASE,
)


def redact_text(value: str) -> str:
    text = _ENV_SECRET_RE.sub(
        lambda match: match.group(0).split("=", 1)[0] + "=[REDACTED]",
        value,
    )
    text = _HEADER_SECRET_RE.sub(
        lambda match: match.group(0).split(":", 1)[0] + ": [REDACTED]",
        text,
    )
    text = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", text)
    text = _SIGNED_URL_RE.sub(
        lambda match: match.group(1) + "?[REDACTED]"
        if re.search(r"(?:token|signature|credential|x-amz|expires|key)", match.group(0), re.IGNORECASE)
        else match.group(0),
        text,
    )
    text = _JWT_RE.sub("[REDACTED]", text)
    return text


def redact_value(value: Any, key: str | None = None) -> Any:
    if key and _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): redact_value(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class RedactionFilter(logging.Filter):
    """Redact structured values before a record reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact_value(record.args)
            else:
                record.args = tuple(redact_value(item) for item in record.args)
        # Redact custom structured fields before any formatter serializes them.
        standard_fields = {
            "args", "created", "exc_info", "exc_text", "filename", "funcName",
            "levelname", "levelno", "lineno", "module", "msecs", "msg", "name",
            "pathname", "process", "processName", "relativeCreated", "stack_info",
            "thread", "threadName",
        }
        for name, value in tuple(record.__dict__.items()):
            if name not in standard_fields:
                setattr(record, name, redact_value(value, name))
        return True


class JsonFormatter(logging.Formatter):
    """Emit a compact, redacted JSON log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for name in (
            "event_type",
            "method",
            "route",
            "status_code",
            "duration_ms",
            "outcome",
            "error_code",
            "error",
        ):
            if hasattr(record, name):
                payload[name] = redact_value(getattr(record, name), name)
        payload["request_id"] = getattr(record, "request_id", None) or request_id_context.get()
        payload["client_ip"] = getattr(record, "client_ip", None) or client_ip_context.get()
        payload["user_id"] = getattr(record, "user_id", None) or user_id_context.get()
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)


def configure_logging(level: str, log_format: str) -> None:
    """Configure one redacting application handler for production startup."""
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactionFilter())
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
