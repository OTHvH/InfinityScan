"""Request-boundary middleware for InfinityScan API.

Provides:
  - Origin validation for unsafe cookie-authenticated requests
  - Configurable request-body size limit (413 on overflow)
  - Cache-Control: no-store on authentication responses

All middleware operates at the ASGI/Starlette level for
maximum performance and no framework coupling.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlparse

from settings import get_settings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from audit_events import validate_request_id
from contextvars import ContextVar

logger = logging.getLogger(__name__)
request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


def _record_security_event(request, event_type: str, reason_code: str) -> None:
    try:
        from audit_events import record_event_safe
        from database import _get_session_local
        from models import AuditEventOutcome

        session_factory = _get_session_local()
        if session_factory is None:
            return
        with session_factory() as db:
            event = record_event_safe(
                db,
                event_type=event_type,
                outcome=AuditEventOutcome.denied,
                request_id=getattr(request.state, "request_id", None),
                subject_type="request",
                subject_id=event_type.split(".", 1)[0],
                metadata={"reason_code": reason_code},
            )
            if event is not None:
                db.commit()
    except Exception:
        logger.error("security audit persistence failed event_type=%s", event_type)


# ── Unsafe HTTP methods ─────────────────────────────────────────────────────

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RequestIDMiddleware:
    """Propagate a bounded request ID through context and the response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        request_id = validate_request_id(supplied) or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_context.set(request_id)

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": response_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_context.reset(token)


# ── Origin validation middleware ─────────────────────────────────────────────


class OriginValidationMiddleware(BaseHTTPMiddleware):
    """Validate Origin header on unsafe cookie-authenticated requests.

    For POST/PUT/PATCH/DELETE requests that may carry cookies:
      1. If an Origin header is present, validate it against allowed_origins.
      2. If no Origin but a Referer header is present, validate its origin.
      3. If neither is present, allow the request (non-browser clients,
         same-origin requests, or requests without cookies).

    This prevents cross-site request forgery from untrusted origins
    while preserving compatibility with same-origin production /api
    architecture and non-browser HTTP clients.
    """

    async def dispatch(self, request: Request, call_next):
        cfg = get_settings()

        if request.method not in _UNSAFE_METHODS:
            return await call_next(request)

        # Only enforce for requests that carry cookies
        if "cookie" not in request.headers:
            return await call_next(request)

        origin = request.headers.get("origin")
        if origin:
            if not _origin_is_allowed(origin, cfg.allowed_origins):
                logger.warning("Blocked request from disallowed origin")
                _record_security_event(request, "origin.denied", "origin_not_allowed")
                return _security_error(request, 403, "Origin not allowed")
            return await call_next(request)

        # Fall back to Referer for browser compatibility
        referer = request.headers.get("referer")
        if referer:
            parsed = urlparse(referer)
            referer_origin = f"{parsed.scheme}://{parsed.netloc}"
            if not _origin_is_allowed(referer_origin, cfg.allowed_origins):
                logger.warning("Blocked request from disallowed referer origin")
                _record_security_event(request, "origin.denied", "referer_not_allowed")
                return _security_error(request, 403, "Origin not allowed")

        return await call_next(request)


def _origin_is_allowed(origin: str, allowed: list[str]) -> bool:
    """Check if *origin* matches any entry in *allowed* origins.

    Supports exact matches and port-wildcard patterns:
      - ``http://localhost:3000`` matches exactly
      - ``http://localhost:*`` matches any port on localhost
    """
    parsed = urlparse(origin)
    origin_netloc = parsed.netloc

    for allowed_entry in allowed:
        allowed_parsed = urlparse(allowed_entry)
        allowed_netloc = allowed_parsed.netloc

        # Scheme must match
        if parsed.scheme != allowed_parsed.scheme:
            continue

        # Exact netloc match
        if origin_netloc == allowed_netloc:
            return True

        # Port-wildcard: ``host:*`` matches any port
        if allowed_netloc.endswith(":*"):
            allowed_host = allowed_netloc[:-2]
            origin_host = origin_netloc.split(":")[0]
            if origin_host == allowed_host:
                return True

    return False


# ── Request body size limit middleware ───────────────────────────────────────


class RequestBodyLimitMiddleware:
    """Buffer and bound request bodies, including chunked requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return

        cfg = get_settings()
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                await _send_json(scope, send, 400, "Invalid Content-Length")
                return
            if declared < 0 or declared > cfg.max_body_bytes:
                await _send_json(scope, send, 413, "Request body too large")
                return

        messages = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > cfg.max_body_bytes:
                    await _send_json(scope, send, 413, "Request body too large")
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(messages):
                current = messages[index]
                index += 1
                return current
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


# ── No-cache auth middleware ────────────────────────────────────────────────


class NoCacheAuthMiddleware(BaseHTTPMiddleware):
    """Add Cache-Control: no-store to authentication responses.

    Applies to /auth/* and legacy /token, /register, /refresh, /logout,
    /me endpoints to prevent browsers from caching sensitive data.
    """

    _AUTH_PATHS = frozenset({
        "/auth/register",
        "/auth/login",
        "/auth/refresh",
        "/auth/logout",
        "/auth/logout-all",
        "/auth/me",
        "/auth/csrf",
    })

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Match auth-related paths
        path = request.url.path.rstrip("/")
        if path in self._AUTH_PATHS or path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"

        return response


def _security_error(request, status_code: int, detail: str) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    if request.url.path.startswith("/auth/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


async def _send_json(scope, send, status_code: int, detail: str) -> None:
    body = ('{"detail":"' + detail + '"}').encode("utf-8")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    path = scope.get("path", "")
    if path.startswith("/auth/"):
        headers.extend([(b"cache-control", b"no-store"), (b"pragma", b"no-cache")])
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})
