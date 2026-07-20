"""Shared SlowAPI rate limiter instance.

Kept in a separate module to avoid circular imports — both main.py
and route modules import this rather than instantiating their own.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
