"""Context-local request fields used by logs and audit events."""

from __future__ import annotations

from contextvars import ContextVar


request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
client_ip_context: ContextVar[str | None] = ContextVar("client_ip", default=None)
user_id_context: ContextVar[str | None] = ContextVar("user_id", default=None)
