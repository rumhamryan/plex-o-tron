# tests/services/test_scraping_service.py

import sys
from pathlib import Path
import pytest
from unittest.mock import Mock, AsyncMock
import wikipedia
from telegram_bot.services import scraping_service

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))


class DummyResponse:
    def __init__(self, text="", json_data=None, status_code=200):
        self.text = text
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class DummyClient:
    def __init__(self, responses):
        self._responses = responses
        self._index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def get(self, url, *args, **kwargs):
        response = self._responses[self._index]
        self._index += 1
        return response


DEDICATED_HTML = """
<table class="wikitable">
<tr><th>Season</th></tr>
<tr><td>0</td></tr>
<tr><td>1</td></tr>
</table>
<h3>Season 1</h3>
<table class="wikitable">
<tr><th>No.</th><th>No. in season</th><th>Title</th></tr>
<tr><td>1</td><td>1</td><td>"Pilot"</td></tr>
</table>
"""


EMBEDDED_HTML = """
<table class="wikitable">
<tr><th>Info</th><th>Title</th></tr>
<tr><td>1 1</td><td>"Pilot"</td></tr>
</table>
"""


NO_EPISODE_HTML = """
<table class="wikitable">
<tr><th>Info</th><th>Title</th></tr>
<tr><td>2 5</td><td>"Other"</td></tr>
</table>
"""

SEASON_OVERVIEW_HTML = """
<table class="wikitable">
<tr><th>Season</th><th>Episodes</th></tr>
<tr><td>1</td><td>10</td></tr>
<tr><td>2</td><td>8</td></tr>
</table>
"""


@pytest.mark.asyncio
async def test_fetch_episode_title_dedicated_page(mocker):
    mock_page = mocker.Mock()
    mock_page.title = "Show"
    mock_page.url = "http://example.com"
    mocker.patch("wikipedia.search", return_value=["Show"])
    mocker.patch("wikipedia.page", return_value=mock_page)
    mocker.patch(
        "telegram_bot.services.scraping_service._get_page_html",
        return_value=DEDICATED_HTML,
    )

    title, corrected = await scraping_service.fetch_episode_title_from_wikipedia(
        "Show", 1, 1
    )
    assert title == "Pilot"
    assert corrected is None


@pytest.mark.asyncio
async def test_fetch_episode_title_embedded_page(mocker):
    mock_list_page = mocker.Mock()
    mock_list_page.url = "http://example.com/list"
    mocker.patch(
        "telegram_bot.services.scraping_service._get_page_html",
        return_value=EMBEDDED_HTML,
    )

    mocker.patch("wikipedia.search", return_value=["Show"])
    mocker.patch(
        "wikipedia.page",
        side_effect=[wikipedia.exceptions.PageError("not found"), mock_list_page],
    )

    title, _ = await scraping_service.fetch_episode_title_from_wikipedia("Show", 1, 1)
    assert title is None    


@pytest.mark.asyncio
async def test_fetch_episode_title_not_found(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mocker.patch("wikipedia.search", return_value=["Show"])
    mocker.patch("wikipedia.page", return_value=mock_page)
    mocker.patch(
        "telegram_bot.services.scraping_service._get_page_html",
        return_value=NO_EPISODE_HTML,
    )

    title, _ = await scraping_service.fetch_episode_title_from_wikipedia("Show", 1, 1)
    assert title is None


@pytest.mark.asyncio
async def test_fetch_season_episode_count(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mocker.patch("wikipedia.page", return_value=mock_page)
    mocker.patch(
        "telegram_bot.services.scraping_service._get_page_html",
        return_value=SEASON_OVERVIEW_HTML,
    )

    count = await scraping_service.fetch_season_episode_count_from_wikipedia("Show", 2)
    assert count == 8


@pytest.mark.asyncio
async def test_scrape_1337x_parses_results(mocker):
    # This is the response for the initial search results page
    search_html = """
    <table><tbody>
    <tr>
      <td>
        <a href="/cat">Movies</a>
        <a href="/torrent/1/Sample.Movie.2023.1080p.x265/">Sample.Movie.2023.1080p.x265</a>
      </td>
      <td>10</td><td>0</td><td>0</td><td>1.5 GB</td><td><a>Anonymous</a></td>
    </tr>
    </tbody></table>
    """

    # This is the required second response for the torrent detail page
    detail_html = """
    <div>
      <a href="magnet:?xt=urn:btih:FAKEHASH">Magnet Download</a>
    </div>
    """

    # The mock client now has TWO responses to give, and they will have a default status_code of 200
    responses = [DummyResponse(text=search_html), DummyResponse(text=detail_html)]
    mocker.patch("httpx.AsyncClient", return_value=DummyClient(responses))

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "codecs": {"x265": 5},
                    "resolutions": {"1080p": 3},
                    "uploaders": {"Anonymous": 2},
                }
            }
        }
    }

    results = await scraping_service.scrape_1337x(
        "Sample Movie 2023",
        "movie",
        "https://1337x.to/search/{query}/1/",
        context,
        base_query_for_filter="Sample Movie",
    )

    assert len(results) == 1
    assert results[0]["title"] == "Sample.Movie.2023.1080p.x265"
    assert results[0]["page_url"].startswith("magnet:")
    assert results[0]["source"] == "1337x"


@pytest.mark.asyncio
async def test_scrape_1337x_no_results(mocker):
    html = "<html><body>No results</body></html>"
    responses = [DummyResponse(text=html)]
    mocker.patch("httpx.AsyncClient", return_value=DummyClient(responses))

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "codecs": {},
                    "resolutions": {},
                    "uploaders": {},
                }
            }
        }
    }

    results = await scraping_service.scrape_1337x(
        "Sample",
        "movie",
        "https://1337x.to/search/{query}/1/",
        context,  # Pass the mock object here
        base_query_for_filter="Sample Movie",
    )

    assert results == []


@pytest.mark.asyncio
async def test_scrape_yts_parses_results(mocker):
    search_html = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.mx/movies/test-movie">Test Movie</a>
      <div class="browse-movie-year">2023</div>
    </div>
    """
    movie_html = '<div id="movie-info" data-movie-id="1234"></div>'
    api_json = {
        "status": "ok",
        "data": {
            "movie": {
                "title_long": "Test Movie (2023)",
                "year": 2023,
                "torrents": [
                    {
                        "quality": "1080p",
                        "type": "WEB",
                        "size_bytes": 1024**3,
                        "hash": "abcdef",
                        "seeds": 10,
                    }
                ],
            }
        },
    }
    responses = [
        DummyResponse(text=search_html),
        DummyResponse(text=movie_html),
        DummyResponse(json_data=api_json),
    ]
    mocker.patch("httpx.AsyncClient", return_value=DummyClient(responses))

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "codecs": {"x264": 5},
                    "resolutions": {"1080p": 3},
                    "uploaders": {"YTS": 2},
                }
            }
        }
    }

    results = await scraping_service.scrape_yts(
        "Test Movie",
        "movie",
        "https://yts.mx/browse-movies/{query}",
        context,  # Pass the mock object here
        year="2023",
        resolution="1080p",
    )

    assert len(results) == 1
    assert results[0]["source"] == "YTS.mx"
    assert results[0]["seeders"] == 10