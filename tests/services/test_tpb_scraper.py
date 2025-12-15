from unittest.mock import Mock

import httpx
import pytest

from telegram_bot.services.scrapers.tpb import scrape_tpb


class FakeResponse:
    def __init__(self, data: list[dict], status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://apibay.org/q.php")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class FakeAsyncClient:
    def __init__(
        self, response: FakeResponse | None = None, error: Exception | None = None
    ):
        self._response = response
        self._error = error
        self.last_params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, headers=None):
        self.last_params = params
        if self._error:
            raise self._error
        return self._response


def _ctx():
    ctx = Mock()
    ctx.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "resolutions": {"1080p": 5},
                    "codecs": {"x265": 3},
                    "uploaders": {"TrustedUploader": 4},
                },
                "tv": {
                    "resolutions": {"1080p": 5},
                    "codecs": {"x264": 2},
                    "uploaders": {"SceneTeam": 3},
                },
            }
        }
    }
    return ctx


@pytest.mark.asyncio
async def test_scrape_tpb_returns_filtered_results(mocker):
    data = [
        {
            "id": "1",
            "name": "Example Movie 2023 1080p x265",
            "info_hash": "ABC123ABC123ABC123ABC123ABC123ABC123ABCD",
            "seeders": "150",
            "leechers": "10",
            "size": str(4 * 1024**3),
            "username": "TrustedUploader",
            "category": "207",
        },
        {
            "id": "2",
            "name": "Completely Different Documentary",
            "info_hash": "FFF123ABC123ABC123ABC123ABC123ABC123AAAA",
            "seeders": "5",
            "leechers": "1",
            "size": str(1 * 1024**3),
            "username": "SomeoneElse",
            "category": "201",
        },
    ]
    fake_client = FakeAsyncClient(response=FakeResponse(data))
    mocker.patch("httpx.AsyncClient", return_value=fake_client)

    context = _ctx()
    results = await scrape_tpb("Example Movie 2023", "movie", "unused", context)
    assert len(results) == 1
    top = results[0]
    assert top["title"].startswith("Example Movie")
    assert top["source"] == "tpb"
    assert top["seeders"] == 150
    assert top["page_url"].startswith("magnet:?")


@pytest.mark.asyncio
async def test_scrape_tpb_matches_season_and_episode(mocker):
    data = [
        {
            "id": "11",
            "name": "Example.Show.S01E02.1080p.WEBRip.x264-SceneTeam",
            "info_hash": "ABC123AAA111ABC123ABC123ABC123ABC123AAA1",
            "seeders": "80",
            "leechers": "5",
            "size": str(2 * 1024**3),
            "username": "SceneTeam",
            "category": "208",
        },
        {
            "id": "12",
            "name": "Example.Show.S02E01.1080p.WEBRip.x264-SceneTeam",
            "info_hash": "ABC123AAA111ABC123ABC123ABC123ABC123AAA2",
            "seeders": "90",
            "leechers": "6",
            "size": str(2 * 1024**3),
            "username": "SceneTeam",
            "category": "208",
        },
    ]
    fake_client = FakeAsyncClient(response=FakeResponse(data))
    mocker.patch("httpx.AsyncClient", return_value=fake_client)

    context = _ctx()
    base = "Example Show S02E01"
    results = await scrape_tpb(
        base, "tv", "unused", context, base_query_for_filter=base
    )
    assert len(results) == 2
    assert "S02E01" in results[0]["title"]


@pytest.mark.asyncio
async def test_scrape_tpb_handles_http_errors(mocker):
    fake_client = FakeAsyncClient(error=httpx.HTTPError("boom"))
    mocker.patch("httpx.AsyncClient", return_value=fake_client)
    context = _ctx()
    results = await scrape_tpb("Example", "movie", "unused", context)
    assert results == []
