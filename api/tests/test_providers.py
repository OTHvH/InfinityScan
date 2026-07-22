from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from providers.base import (
    NormalizedChapter,
    NormalizedPageReference,
    NormalizedSeries,
    ProviderAdapter,
    ProviderError,
    ProviderTimeout,
)
from providers.cache import MetadataCache
from providers.copymanga import CopyMangaAdapter
from providers.local import LocalContentAdapter
from providers.ssrf import validate_url, is_private_ip


# ═══════════════════════════════════════════════════════════════════════════════
# SSRF Protection Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSSRF:
    def test_https_allowed(self):
        validate_url("https://api.example.com/v1/test")

    def test_http_rejected_in_production(self):
        with pytest.raises(ValueError, match="Only HTTPS"):
            validate_url("http://api.example.com/v1/test")

    def test_localhost_blocked(self):
        with pytest.raises(ValueError, match="Loopback hostname blocked"):
            validate_url("https://localhost/api", allow_localhost=False)

    def test_localhost_allowed_when_permitted(self):
        validate_url("https://localhost/api", allow_localhost=True)

    def test_127_0_0_1_blocked(self):
        with pytest.raises(ValueError, match="blocked"):
            validate_url("https://127.0.0.1/api")

    def test_private_ip_10_blocked(self):
        with pytest.raises(ValueError, match="blocked network"):
            validate_url("https://10.0.0.1/api")

    def test_private_ip_172_blocked(self):
        with pytest.raises(ValueError, match="blocked network"):
            validate_url("https://172.16.0.1/api")

    def test_private_ip_192_168_blocked(self):
        with pytest.raises(ValueError, match="blocked network"):
            validate_url("https://192.168.1.1/api")

    def test_link_local_blocked(self):
        with pytest.raises(ValueError, match="blocked network"):
            validate_url("https://169.254.1.1/api")

    def test_reserved_ip_blocked(self):
        with pytest.raises(ValueError, match="blocked network"):
            validate_url("https://192.0.2.1/api")

    def test_no_scheme_rejected(self):
        with pytest.raises(ValueError, match="URL scheme must be http or https"):
            validate_url("ftp://example.com/file")

    def test_no_hostname_rejected(self):
        with pytest.raises(ValueError, match="must have a hostname"):
            validate_url("https:///path")

    def test_redirect_to_private_ip(self):
        with pytest.raises(ValueError, match="blocked network"):
            validate_url("https://192.168.1.100/redirect?next=http://evil.com")

    def test_ipv6_loopback_blocked(self):
        with pytest.raises(ValueError, match="blocked"):
            validate_url("https://[::1]/api")

    def test_is_private_ip_detection(self):
        assert is_private_ip("10.0.0.1") is True
        assert is_private_ip("192.168.1.1") is True
        assert is_private_ip("1.1.1.1") is False
        assert is_private_ip("8.8.8.8") is False
        assert is_private_ip("not-an-ip") is False


# ═══════════════════════════════════════════════════════════════════════════════
# Cache Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMetadataCache:
    def test_set_and_get(self):
        cache = MetadataCache(max_size=10, default_ttl_seconds=300)
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

    def test_miss_returns_none(self):
        cache = MetadataCache(max_size=10, default_ttl_seconds=300)
        assert cache.get("missing") is None

    def test_eviction_on_max_size(self):
        cache = MetadataCache(max_size=3, default_ttl_seconds=300)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        cache.set("d", 4)
        assert cache.get("a") is None
        assert cache.get("d") == 4

    def test_expiry(self):
        cache = MetadataCache(max_size=10, default_ttl_seconds=0)
        cache.set("key", "val", ttl_seconds=0)
        time.sleep(0.01)
        assert cache.get("key") is None

    def test_invalidate(self):
        cache = MetadataCache(max_size=10, default_ttl_seconds=300)
        cache.set("key", "val")
        cache.invalidate("key")
        assert cache.get("key") is None

    def test_clear(self):
        cache = MetadataCache(max_size=10, default_ttl_seconds=300)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.clear()
        assert len(cache) == 0

    def test_contains(self):
        cache = MetadataCache(max_size=10, default_ttl_seconds=300)
        cache.set("key", "val")
        assert "key" in cache
        assert "missing" not in cache

    def test_lru_ordering(self):
        cache = MetadataCache(max_size=2, default_ttl_seconds=300)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")
        cache.set("c", 3)
        assert cache.get("a") == 1
        assert cache.get("b") is None


# ═══════════════════════════════════════════════════════════════════════════════
# CopyManga Adapter Tests
# ═══════════════════════════════════════════════════════════════════════════════


def _make_response(
    json_data: dict | list | str,
    *,
    status: int = 200,
    content_type: str = "application/json",
    content_length: int | None = None,
) -> httpx.Response:
    if isinstance(json_data, (dict, list)):
        body = json.dumps(json_data).encode()
    else:
        body = json_data.encode()
    headers: dict[str, str] = {"content-type": content_type}
    if content_length is not None:
        headers["content-length"] = str(content_length)
    return httpx.Response(status_code=status, content=body, headers=headers, request=httpx.Request("GET", "https://api.example.com"))


class TestCopyMangaAdapter:
    def setup_method(self):
        self.adapter = CopyMangaAdapter(
            "https://api.copymanga.tv",
            enabled=True,
            timeout=5.0,
            max_response_bytes=1024 * 1024,
            cache_ttl=0,
        )

    def test_properties(self):
        assert self.adapter.key == "copymanga"
        assert self.adapter.display_name == "CopyManga"
        assert self.adapter.enabled is True
        assert self.adapter.base_url == "https://api.copymanga.tv"

    def test_disabled_adapter(self):
        adapter = CopyMangaAdapter("https://api.copymanga.tv", enabled=False)
        assert adapter.enabled is False

    @pytest.mark.asyncio
    async def test_normalized_search(self):
        mock_body = {
            "code": 200,
            "results": {
                "list": [
                    {
                        "path_word": "one-piece",
                        "name": "One Piece",
                        "brief": "A pirate adventure",
                        "cover": "https://example.com/cover.jpg",
                        "status": {"name": "ongoing"},
                        "author": [{"name": "Oda Eiichiro"}],
                        "theme": [{"name": "Action"}, {"name": "Adventure"}],
                    }
                ],
                "total": 1,
            },
        }
        mock_response = _make_response(mock_body)

        with patch.object(self.adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = client
            results = await self.adapter.search_series("one piece", limit=10, offset=0)

        assert len(results) == 1
        s = results[0]
        assert isinstance(s, NormalizedSeries)
        assert s.external_id == "one-piece"
        assert s.title == "One Piece"
        assert s.description == "A pirate adventure"
        assert s.cover_url == "https://example.com/cover.jpg"
        assert s.status == "ongoing"
        assert s.authors == ("Oda Eiichiro",)
        assert s.tags == ("Action", "Adventure")
        assert "raw" not in str(s)

    @pytest.mark.asyncio
    async def test_normalized_get_series(self):
        mock_body = {
            "code": 200,
            "data": {
                "comic": {
                    "path_word": "naruto",
                    "name": "Naruto",
                    "brief": "A ninja story",
                    "cover": "https://example.com/naruto.jpg",
                    "status": "completed",
                    "author": [{"name": "Kishimoto"}],
                    "theme": [{"name": "Action"}],
                }
            },
        }
        mock_response = _make_response(mock_body)

        with patch.object(self.adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = client
            series = await self.adapter.get_series("naruto")

        assert isinstance(series, NormalizedSeries)
        assert series.external_id == "naruto"
        assert series.title == "Naruto"
        assert series.status == "completed"

    @pytest.mark.asyncio
    async def test_normalized_list_chapters(self):
        mock_body = {
            "code": 200,
            "data": {
                "list": [
                    {
                        "uuid": "ch-001",
                        "chapter_number": 1,
                        "title": "Chapter 1",
                        "volume": "01",
                        "size": 20,
                        "groups": [{"language": "ja"}],
                        "pub_date": "2023-01-01",
                    },
                    {
                        "uuid": "ch-002",
                        "chapter_number": 2,
                        "title": "Chapter 2",
                        "volume": "01",
                        "size": 18,
                        "groups": [{"language": "ja"}],
                        "pub_date": "2023-01-08",
                    },
                ],
                "total": 2,
            },
        }
        mock_response = _make_response(mock_body)

        with patch.object(self.adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = client
            chapters = await self.adapter.list_chapters("naruto", limit=50, offset=0)

        assert len(chapters) == 2
        ch = chapters[0]
        assert isinstance(ch, NormalizedChapter)
        assert ch.external_id == "ch-001"
        assert ch.number == Decimal("1")
        assert ch.title == "Chapter 1"
        assert ch.volume == "01"
        assert ch.page_count == 20
        assert ch.language == "ja"

    @pytest.mark.asyncio
    async def test_normalized_chapter_pages(self):
        mock_body = {
            "code": 200,
            "results": {
                "list": [
                    {"url": "https://img.example.com/p1.jpg", "width": 800, "height": 1200},
                    {"url": "https://img.example.com/p2.jpg", "width": 800, "height": 1200},
                ],
            },
        }
        mock_response = _make_response(mock_body)

        with patch.object(self.adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = client
            pages = await self.adapter.get_chapter_pages("naruto", "ch-001")

        assert len(pages) == 2
        assert isinstance(pages[0], NormalizedPageReference)
        assert pages[0].page_number == 1
        assert pages[0].url == "https://img.example.com/p1.jpg"
        assert pages[0].width == 800
        assert pages[0].height == 1200

    @pytest.mark.asyncio
    async def test_malformed_response_raises_provider_error(self):
        mock_body = {"code": 500, "message": "internal error"}
        mock_response = _make_response(mock_body)

        with patch.object(self.adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = client
            with pytest.raises(ProviderError, match="Provider error"):
                await self.adapter.search_series("test")

    @pytest.mark.asyncio
    async def test_timeout_raises_provider_timeout(self):
        with patch.object(self.adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
            mock_get_client.return_value = client
            with pytest.raises(ProviderTimeout, match="timed out"):
                await self.adapter.search_series("test")

    @pytest.mark.asyncio
    async def test_connection_error_raises_provider_error(self):
        with patch.object(self.adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
            mock_get_client.return_value = client
            with pytest.raises(ProviderError, match="request failed"):
                await self.adapter.search_series("test")

    @pytest.mark.asyncio
    async def test_oversized_response_rejected(self):
        mock_response = httpx.Response(
            status_code=200,
            content=b'{"code": 200}',
            headers={"content-type": "application/json", "content-length": "99999999"},
            request=httpx.Request("GET", "https://api.example.com"),
        )

        adapter = CopyMangaAdapter("https://api.copymanga.tv", max_response_bytes=1024)
        with patch.object(adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = client
            with pytest.raises(ProviderError, match="exceeds maximum"):
                await adapter.search_series("test")

    @pytest.mark.asyncio
    async def test_wrong_content_type_rejected(self):
        mock_response = httpx.Response(
            status_code=200,
            content=b"<html>not json</html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://api.example.com"),
        )

        with patch.object(self.adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = client
            with pytest.raises(ProviderError, match="Unexpected content type"):
                await self.adapter.search_series("test")

    @pytest.mark.asyncio
    async def test_invalid_json_rejected(self):
        mock_response = httpx.Response(
            status_code=200,
            content=b"not json at all",
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "https://api.example.com"),
        )

        with patch.object(self.adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = client
            with pytest.raises(ProviderError, match="Failed to parse"):
                await self.adapter.search_series("test")

    @pytest.mark.asyncio
    async def test_health_check_success(self):
        mock_response = _make_response({"status": "ok"})
        with patch.object(self.adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = client
            assert await self.adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self):
        with patch.object(self.adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=Exception("connection refused"))
            mock_get_client.return_value = client
            assert await self.adapter.health_check() is False

    @pytest.mark.asyncio
    async def test_token_header_set(self):
        adapter = CopyMangaAdapter("https://api.copymanga.tv", token="my-secret-token")
        assert adapter._headers["Authorization"] == "Token my-secret-token"

    @pytest.mark.asyncio
    async def test_no_token_header(self):
        adapter = CopyMangaAdapter("https://api.copymanga.tv", token="")
        assert "Authorization" not in adapter._headers

    @pytest.mark.asyncio
    async def test_provider_failure_does_not_corrupt_cache(self):
        call_count = 0
        original_get = self.adapter._get

        async def flaky_get(path, params=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ProviderError("transient failure")
            return await original_get(path, params=params)

        with patch.object(self.adapter, "_get", side_effect=flaky_get):
            with pytest.raises(ProviderError):
                await self.adapter.search_series("test")

    @pytest.mark.asyncio
    async def test_empty_search_results(self):
        mock_body = {
            "code": 200,
            "results": {"list": [], "total": 0},
        }
        mock_response = _make_response(mock_body)

        with patch.object(self.adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = client
            results = await self.adapter.search_series("nonexistent")

        assert results == []

    @pytest.mark.asyncio
    async def test_search_results_cached(self):
        mock_body = {
            "code": 200,
            "results": {
                "list": [{"path_word": "test", "name": "Test"}],
                "total": 1,
            },
        }
        mock_response = _make_response(mock_body)

        adapter = CopyMangaAdapter("https://api.copymanga.tv", cache_ttl=300)
        with patch.object(adapter, "_get_client") as mock_get_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = client
            r1 = await adapter.search_series("test")
            r2 = await adapter.search_series("test")

        assert len(r1) == 1
        assert len(r2) == 1
        assert client.get.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Local Content Adapter Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLocalContentAdapter:
    def setup_method(self, tmp_path=None):
        self.root = Path("/tmp/test_local_content")
        self.root.mkdir(parents=True, exist_ok=True)

    def teardown_method(self):
        import shutil
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_properties(self):
        adapter = LocalContentAdapter(self.root)
        assert adapter.key == "local"
        assert adapter.display_name == "Local Import"
        assert adapter.enabled is True

    def test_disabled_adapter(self):
        adapter = LocalContentAdapter(self.root, enabled=False)
        assert adapter.enabled is False

    @pytest.mark.asyncio
    async def test_health_check(self):
        adapter = LocalContentAdapter(self.root)
        assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_missing_dir(self):
        adapter = LocalContentAdapter(Path("/nonexistent"))
        assert await adapter.health_check() is False

    @pytest.mark.asyncio
    async def test_search_series(self):
        series_dir = self.root / "test-series"
        series_dir.mkdir()
        (series_dir / "series.json").write_text(json.dumps({"title": "Test Series", "synopsis": "A test"}))

        adapter = LocalContentAdapter(self.root)
        results = await adapter.search_series("test")

        assert len(results) == 1
        assert results[0].title == "Test Series"
        assert results[0].external_id == "test-series"

    @pytest.mark.asyncio
    async def test_search_series_no_match(self):
        series_dir = self.root / "other-series"
        series_dir.mkdir()
        (series_dir / "series.json").write_text(json.dumps({"title": "Other"}))

        adapter = LocalContentAdapter(self.root)
        results = await adapter.search_series("test")
        assert results == []

    @pytest.mark.asyncio
    async def test_get_series(self):
        series_dir = self.root / "my-series"
        series_dir.mkdir()
        (series_dir / "series.json").write_text(json.dumps({"title": "My Series"}))

        adapter = LocalContentAdapter(self.root)
        series = await adapter.get_series("my-series")

        assert series.title == "My Series"
        assert series.external_id == "my-series"

    @pytest.mark.asyncio
    async def test_get_series_not_found(self):
        adapter = LocalContentAdapter(self.root)
        with pytest.raises(ProviderError, match="not found"):
            await adapter.get_series("nonexistent")

    @pytest.mark.asyncio
    async def test_list_chapters(self):
        series_dir = self.root / "series"
        series_dir.mkdir()
        ch1 = series_dir / "1"
        ch1.mkdir()
        (ch1 / "page1.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        ch2 = series_dir / "2"
        ch2.mkdir()
        (ch2 / "page1.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        adapter = LocalContentAdapter(self.root)
        chapters = await adapter.list_chapters("series")

        assert len(chapters) == 2
        assert chapters[0].number == Decimal("1")
        assert chapters[0].page_count == 1

    @pytest.mark.asyncio
    async def test_get_chapter_pages(self):
        series_dir = self.root / "series"
        ch_dir = series_dir / "1"
        ch_dir.mkdir(parents=True)
        (ch_dir / "page1.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 50)
        (ch_dir / "page2.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 50)

        adapter = LocalContentAdapter(self.root)
        pages = await adapter.get_chapter_pages("series", "1")

        assert len(pages) == 2
        assert pages[0].page_number == 1
        assert pages[0].url.startswith("file://")

    @pytest.mark.asyncio
    async def test_root_not_found(self):
        adapter = LocalContentAdapter(Path("/nonexistent"))
        with pytest.raises(ProviderError, match="not found"):
            await adapter.search_series("test")


# ═══════════════════════════════════════════════════════════════════════════════
# Adapter Isolation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAdapterIsolation:
    def test_copymanga_and_local_are_independent(self):
        cm = CopyMangaAdapter("https://api.copymanga.tv", enabled=True)
        local = LocalContentAdapter(Path("/tmp/test"), enabled=True)

        assert cm.key != local.key
        assert cm.enabled is True
        assert local.enabled is True

    def test_disabling_one_does_not_affect_other(self):
        cm = CopyMangaAdapter("https://api.copymanga.tv", enabled=False)
        local = LocalContentAdapter(Path("/tmp/test"), enabled=True)

        assert cm.enabled is False
        assert local.enabled is True

    def test_different_base_urls(self):
        cm = CopyMangaAdapter("https://api.copymanga.tv")
        assert cm.base_url == "https://api.copymanga.tv"

    def test_protocol_compliance(self):
        cm = CopyMangaAdapter("https://api.copymanga.tv")
        local = LocalContentAdapter(Path("/tmp/test"))
        assert isinstance(cm, ProviderAdapter)
        assert isinstance(local, ProviderAdapter)

    def test_cache_isolation(self):
        cm1 = CopyMangaAdapter("https://api.copymanga.tv", cache_ttl=300)
        cm2 = CopyMangaAdapter("https://api.copymanga.tv", cache_ttl=60)

        cm1._cache.set("key", "val1")
        cm2._cache.set("key", "val2")

        assert cm1._cache.get("key") == "val1"
        assert cm2._cache.get("key") == "val2"


# ═══════════════════════════════════════════════════════════════════════════════
# DTO Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizedDTOs:
    def test_normalized_series_frozen(self):
        s = NormalizedSeries(external_id="test", title="Test")
        with pytest.raises(AttributeError):
            s.title = "Changed"

    def test_normalized_chapter_frozen(self):
        ch = NormalizedChapter(external_id="ch1", number=Decimal("1.5"))
        with pytest.raises(AttributeError):
            ch.number = Decimal("2")

    def test_normalized_page_reference(self):
        p = NormalizedPageReference(page_number=1, url="https://img.example.com/p1.jpg")
        assert p.width is None
        assert p.mime_type is None

    def test_provider_error_is_exception(self):
        exc = ProviderError("test error")
        assert str(exc) == "test error"
        assert isinstance(exc, Exception)

    def test_provider_timeout_is_provider_error(self):
        exc = ProviderTimeout("timeout")
        assert isinstance(exc, ProviderError)
        assert isinstance(exc, Exception)
