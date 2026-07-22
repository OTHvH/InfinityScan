from __future__ import annotations

import time
from collections import OrderedDict
from typing import Generic, TypeVar

_KT = TypeVar("_KT")
_VT = TypeVar("_VT")


class MetadataCache(Generic[_KT, _VT]):
    """Bounded in-memory cache with per-entry TTL.

    Evicts oldest entries when max_size is exceeded.
    """

    def __init__(self, *, max_size: int = 256, default_ttl_seconds: int = 300) -> None:
        self._max_size = max_size
        self._default_ttl = default_ttl_seconds
        self._store: OrderedDict[_KT, tuple[_VT, float]] = OrderedDict()

    def get(self, key: _KT) -> _VT | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return value

    def set(self, key: _KT, value: _VT, ttl_seconds: int | None = None) -> None:
        if key in self._store:
            self._store.pop(key)
        while len(self._store) >= self._max_size:
            self._store.popitem(last=False)
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        self._store[key] = (value, time.monotonic() + ttl)

    def invalidate(self, key: _KT) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: object) -> bool:
        entry = self._store.get(key)  # type: ignore[arg-type]
        if entry is None:
            return False
        _, expires_at = entry
        if time.monotonic() > expires_at:
            self._store.pop(key, None)  # type: ignore[arg-type]
            return False
        return True
