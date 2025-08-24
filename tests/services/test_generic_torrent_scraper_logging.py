import logging
import httpx

import pytest

from telegram_bot.services.generic_torrent_scraper import GenericTorrentScraper


class DummyResponse:
    def __init__(
        self, text: str = "", status_code: int = 200, url: str = "https://example.com"
    ) -> None:
        self.text = text
        self.status_code = status_code
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", self.url)
            response = httpx.Response(self.status_code, text=self.text, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class DummyClient:
    def __init__(self, response: DummyResponse) -> None:
        self._response = response

    async def __aenter__(self) -> "DummyClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001, D401
        """No-op context manager exit."""

    async def get(self, url: str, headers=None):  # noqa: ANN001
        return self._response


@pytest.mark.asyncio
async def test_fetch_page_logs_status(mocker, caplog):
    response = DummyResponse(text="OK", status_code=200)
    mocker.patch("httpx.AsyncClient", return_value=DummyClient(response))

    scraper = GenericTorrentScraper(
        {
            "site_name": "TestSite",
            "base_url": "https://example.com",
            "search_path": "/",
            "category_mapping": {"movie": "/"},
            "results_page_selectors": {"rows": "tr"},
        }
    )

    caplog.set_level(logging.DEBUG)
    result = await scraper._fetch_page("https://example.com")
    assert result == "OK"
    assert any("GET https://example.com -> 200" in m for m in caplog.messages)


@pytest.mark.asyncio
async def test_fetch_page_logs_error_body(mocker, caplog):
    response = DummyResponse(text="Forbidden", status_code=403)
    mocker.patch("httpx.AsyncClient", return_value=DummyClient(response))

    scraper = GenericTorrentScraper(
        {
            "site_name": "TestSite",
            "base_url": "https://example.com",
            "search_path": "/",
            "category_mapping": {"movie": "/"},
            "results_page_selectors": {"rows": "tr"},
        }
    )

    caplog.set_level(logging.DEBUG)
    result = await scraper._fetch_page("https://example.com")
    assert result is None
    assert any("GET https://example.com -> 403" in m for m in caplog.messages)
    assert any("Error response body:" in m for m in caplog.messages)
