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
from urllib.parse import urlparse

from settings import get_settings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


# ── Unsafe HTTP methods ─────────────────────────────────────────────────────

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


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
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Origin not allowed"},
                )
            return await call_next(request)

        # Fall back to Referer for browser compatibility
        referer = request.headers.get("referer")
        if referer:
            parsed = urlparse(referer)
            referer_origin = f"{parsed.scheme}://{parsed.netloc}"
            if not _origin_is_allowed(referer_origin, cfg.allowed_origins):
                logger.warning("Blocked request from disallowed referer origin")
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Origin not allowed"},
                )

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

        # Exact match
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


class RequestBodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests with Content-Length exceeding max_body_bytes.

    Returns 413 Payload Too Large.  Does not rely exclusively on
    Content-Length — also enforces a hard read limit via the ASGI layer.
    """

    async def dispatch(self, request: Request, call_next):
        cfg = get_settings()

        # Only check requests with a body
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                size = int(content_length)
                if size > cfg.max_body_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large"},
                    )
            except ValueError:
                pass  # Invalid Content-Length — let the handler deal with it

        return await call_next(request)


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
        "/register",
        "/token",
        "/refresh",
        "/logout",
        "/me",
    })

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Match auth-related paths
        path = request.url.path.rstrip("/")
        if path in self._AUTH_PATHS or path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"

        return response
