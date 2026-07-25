from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from providers.base import (
    NormalizedChapter,
    NormalizedPageReference,
    NormalizedSeries,
    ProviderError,
    ProviderSecurityError,
    ProviderTimeout,
)
from providers.ssrf import validate_redirect_url, validate_url
from providers.cache import MetadataCache

logger = logging.getLogger(__name__)

_ACCEPTED_CONTENT_TYPES = frozenset({
    "application/json",
    "text/json",
    "text/plain",
    "application/octet-stream",
})


class CopyMangaAdapter:
    """Adapter for the CopyManga external provider.

    All CopyManga-specific parsing is isolated here.
    The frontend never sees raw CopyManga response formats.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        enabled: bool = True,
        timeout: float = 15.0,
        max_response_bytes: int = 2 * 1024 * 1024,
        max_redirects: int = 5,
        cache_ttl: int = 300,
        cache_max_size: int = 256,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._enabled = enabled
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._max_redirects = max_redirects
        self._headers: dict[str, str] = {
            "User-Agent": "Mozilla/5.0 (compatible; InfinityScan/1.0)",
            "Accept": "application/json",
        }
        if token:
            self._headers["Authorization"] = f"Token {token}"
        self._cache: MetadataCache[str, Any] = MetadataCache(
            max_size=cache_max_size,
            default_ttl_seconds=cache_ttl,
        )
        self._client: httpx.AsyncClient | None = None

    @property
    def key(self) -> str:
        return "copymanga"

    @property
    def display_name(self) -> str:
        return "CopyManga"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=self._headers,
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            url = f"{self._base_url}/api/v3/"
            validate_url(url)
            r = await client.get(url)
            return r.status_code < 500
        except Exception:
            return False

    async def search_series(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> list[NormalizedSeries]:
        cache_key = f"search:{query}:{limit}:{offset}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        body = await self._get("search/comic", params={"q": query, "limit": limit, "offset": offset, "platform": 1})
        raw_list = self._extract_list(body, "list")
        result = [self._parse_series(item) for item in raw_list]
        self._cache.set(cache_key, result)
        return result

    async def get_series(self, external_id: str) -> NormalizedSeries:
        cache_key = f"series:{external_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        body = await self._get(f"comic/{external_id}")
        raw = None
        if isinstance(body, dict):
            raw = body.get("comic")
            if raw is None:
                raw = body
        if not isinstance(raw, dict):
            raise ProviderError("Malformed series response from provider")
        result = self._parse_series(raw)
        self._cache.set(cache_key, result)
        return result

    async def list_chapters(
        self, external_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[NormalizedChapter]:
        cache_key = f"chapters:{external_id}:{limit}:{offset}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        body = await self._get(
            f"comic/{external_id}/group/default/chapters",
            params={"limit": limit, "offset": offset},
        )
        raw_list = self._extract_list(body, "list")
        result = [self._parse_chapter(item) for item in raw_list]
        self._cache.set(cache_key, result)
        return result

    async def get_chapter_pages(
        self, external_id: str, chapter_external_id: str
    ) -> list[NormalizedPageReference]:
        cache_key = f"pages:{external_id}:{chapter_external_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        body = await self._get(
            f"comic/{external_id}/chapter/{chapter_external_id}",
        )
        raw_list = self._extract_list(body, "list")
        if not raw_list:
            raise ProviderError("Malformed chapter pages list from provider")
        result = [
            NormalizedPageReference(
                page_number=i + 1,
                url=str(item.get("url", "")),
                width=self._safe_int(item.get("width")),
                height=self._safe_int(item.get("height")),
                mime_type=None,
            )
            for i, item in enumerate(raw_list)
            if isinstance(item, dict) and item.get("url")
        ]
        self._cache.set(cache_key, result, ttl_seconds=60)
        return result

    # ── internal helpers ──────────────────────────────────────────

    def _api_path(self, path: str) -> str:
        return f"{self._base_url}/api/v3/{path.lstrip('/')}"

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self._api_path(path)
        try:
            validate_url(url)
        except ValueError as exc:
            raise ProviderSecurityError(f"URL validation failed: {exc}") from exc
        try:
            client = await self._get_client()
            request_params = {k: v for k, v in (params or {}).items() if v is not None}
            for redirect_count in range(self._max_redirects + 1):
                r = await client.get(url, params=request_params if redirect_count == 0 else None)
                if not r.is_redirect:
                    break
                location = r.headers.get("location")
                if not location:
                    raise ProviderError("Provider redirect is missing a location")
                url = str(r.url.join(location))
                try:
                    validate_redirect_url(url)
                except ValueError as exc:
                    raise ProviderSecurityError(f"Redirect URL validation failed: {exc}") from exc
            else:
                raise ProviderError("Provider exceeded the redirect limit")
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"Provider request timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise ProviderError(f"Provider request failed: {exc}") from exc
        self._check_size(r)
        self._validate_content_type(r)
        body = self._parse_json(r)
        code = body.get("code") if isinstance(body, dict) else None
        if code not in (100, 200, None):
            raise ProviderError(f"Provider error (code {code}): {body.get('message', 'unknown')}")
        if isinstance(body, dict):
            return body.get("results") or body.get("data") or body
        return body

    def _check_size(self, r: httpx.Response) -> None:
        length = r.headers.get("content-length")
        if length and length.isdigit() and int(length) > self._max_response_bytes:
            raise ProviderError("Response exceeds maximum allowed size")

    def _validate_content_type(self, r: httpx.Response) -> None:
        ct = r.headers.get("content-type", "")
        main = ct.split(";")[0].strip().lower()
        if main and not any(main.startswith(t) for t in _ACCEPTED_CONTENT_TYPES):
            raise ProviderError(f"Unexpected content type: {main!r}")

    def _parse_json(self, r: httpx.Response) -> Any:
        try:
            return r.json()
        except Exception as exc:
            raise ProviderError("Failed to parse provider response as JSON") from exc

    @staticmethod
    def _extract_list(body: Any, key: str) -> list[dict[str, Any]]:
        if not isinstance(body, dict):
            return []
        data = body.get("data")
        if isinstance(data, dict):
            raw = data.get(key)
            if isinstance(raw, list):
                return raw
        raw = body.get(key)
        if isinstance(raw, list):
            return raw
        return []

    @staticmethod
    def _parse_series(raw: dict[str, Any]) -> NormalizedSeries:
        status_obj = raw.get("status")
        if isinstance(status_obj, dict):
            status = status_obj.get("name")
        else:
            status = status_obj if isinstance(status_obj, str) else None
        authors = tuple(
            a.get("name", "") if isinstance(a, dict) else str(a)
            for a in (raw.get("author") or [])
            if a
        )
        tags = tuple(
            t.get("name", "") if isinstance(t, dict) else str(t)
            for t in (raw.get("theme") or raw.get("tag") or [])
            if t
        )
        return NormalizedSeries(
            external_id=str(raw.get("path_word", "")),
            title=str(raw.get("name", "")),
            description=str(raw.get("brief", "")),
            cover_url=raw.get("cover"),
            status=status,
            content_type=None,
            year=None,
            tags=tags,
            authors=authors,
            raw=raw,
        )

    @staticmethod
    def _parse_chapter(raw: dict[str, Any]) -> NormalizedChapter:
        raw_number = raw.get("chapter_number") or raw.get("num")
        try:
            number = Decimal(str(raw_number)) if raw_number is not None else Decimal("0")
        except (InvalidOperation, ValueError):
            number = Decimal("0")
        groups = raw.get("groups") or []
        if isinstance(groups, list) and groups:
            lang = groups[0].get("language", "") if isinstance(groups[0], dict) else ""
        else:
            lang = ""
        return NormalizedChapter(
            external_id=str(raw.get("uuid", "")),
            number=number,
            title=raw.get("title") or raw.get("name"),
            volume=raw.get("volume"),
            language=str(lang),
            page_count=int(raw.get("size") or 0),
            published_at=raw.get("pub_date") or raw.get("datetime_created"),
            raw=raw,
        )

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
