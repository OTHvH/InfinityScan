"""Shared SlowAPI rate limiter instance.

Kept in a separate module to avoid circular imports — both main.py
and route modules import this rather than instantiating their own.
"""

from __future__ import annotations

from slowapi import Limiter

def get_client_identity(request) -> str:
    """Use the validated request-bound client address for rate limiting."""
    return str(getattr(request.state, "client_ip", None) or (request.client.host if request.client else "unknown"))


limiter = Limiter(key_func=get_client_identity)
