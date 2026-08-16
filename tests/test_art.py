import pytest

from metamatch import art


class FakeResponse:
    def __init__(self, status_code=200, content=b"IMAGEBYTES", content_type="image/jpeg"):
        self.status_code = status_code
        self.content = content
        self.headers = {"Content-Type": content_type}


class TestFetchCoverArt:
    def setup_method(self):
        art._cache.clear()
        art._negative_cache.clear()

    def test_returns_none_for_missing_release_id(self):
        assert art.fetch_cover_art(None) is None
        assert art.fetch_cover_art("") is None

    def test_returns_bytes_and_mime_on_success(self, monkeypatch):
        def fake_get(url, headers=None, timeout=None, allow_redirects=None):
            assert "release-123" in url
            return FakeResponse(200, b"IMAGEDATA", "image/jpeg")

        monkeypatch.setattr(art.requests, "get", fake_get)
        result = art.fetch_cover_art("release-123")
        assert result == (b"IMAGEDATA", "image/jpeg")

    def test_returns_none_on_404(self, monkeypatch):
        def fake_get(*a, **k):
            return FakeResponse(404, b"", "text/html")

        monkeypatch.setattr(art.requests, "get", fake_get)
        assert art.fetch_cover_art("missing-release") is None

    def test_returns_none_on_network_error(self, monkeypatch):
        import requests

        def fake_get(*a, **k):
            raise requests.RequestException("boom")

        monkeypatch.setattr(art.requests, "get", fake_get)
        assert art.fetch_cover_art("release-123") is None

    def test_caches_result_and_avoids_second_request(self, monkeypatch):
        call_count = {"n": 0}

        def fake_get(*a, **k):
            call_count["n"] += 1
            return FakeResponse(200, b"DATA", "image/jpeg")

        monkeypatch.setattr(art.requests, "get", fake_get)
        art.fetch_cover_art("release-abc")
        art.fetch_cover_art("release-abc")
        assert call_count["n"] == 1

    def test_different_sizes_are_cached_separately(self, monkeypatch):
        seen_urls = []

        def fake_get(url, **k):
            seen_urls.append(url)
            return FakeResponse(200, b"DATA", "image/jpeg")

        monkeypatch.setattr(art.requests, "get", fake_get)
        art.fetch_cover_art("release-xyz", size="250")
        art.fetch_cover_art("release-xyz", size="500")
        assert len(seen_urls) == 2
        assert seen_urls[0] != seen_urls[1]
