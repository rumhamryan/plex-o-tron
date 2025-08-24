# tests/services/test_scraping_service.py

import sys
from pathlib import Path
import pytest
from unittest.mock import Mock
import wikipedia
from bs4 import BeautifulSoup
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
<tr><td><a title="Show season 1">Season 1</a></td></tr>
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

SIMPLE_EMBEDDED_HTML = """
<h3>Episodes</h3>
<table class="wikitable">
<tr><th>No.</th><th>Title</th></tr>
<tr><td>1</td><td>"Pilot"</td></tr>
</table>
"""


WRONG_HEADER_HTML = """
<h3>Overview</h3>
<table class="wikitable">
<tr><th>No.</th><th>No. in season</th><th>Title</th></tr>
<tr><td>1</td><td>1</td><td>"Pilot"</td></tr>
</table>
"""

VARIED_COLUMNS_HTML = """
<table class="wikitable">
<tr><th>Season</th><th>No.</th><th>Title</th></tr>
<tr><td>1</td><td>1</td><td><i>Pilot</i></td></tr>
</table>
"""

TWO_COLUMN_HTML = """
<table class="wikitable">
<tr><th>No.</th><th>Title</th></tr>
<tr><td>1</td><td>"Pilot"</td></tr>
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
    mock_main_page = mocker.Mock()
    mock_main_page.title = "Show"
    mock_main_page.url = "http://example.com/main"

    mocker.patch("wikipedia.search", return_value=["Show"])
    mocker.patch(
        "wikipedia.page",
        side_effect=[mock_main_page, wikipedia.exceptions.PageError("no list")],
    )
    mocker.patch(
        "telegram_bot.services.scraping_service._get_page_html",
        return_value=SIMPLE_EMBEDDED_HTML,
    )

    title, _ = await scraping_service.fetch_episode_title_from_wikipedia("Show", 1, 1)
    assert title == "Pilot"


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
async def test_scrape_site_parses_results(mocker):
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

    results = await scraping_service.scrape_site(
        "1337x",
        "Sample Movie 2023",
        "movie",
        context,
    )

    assert len(results) == 1
    assert results[0]["title"] == "Sample.Movie.2023.1080p.x265"
    assert results[0]["page_url"].startswith("magnet:")
    assert results[0]["source"] == "1337x"


@pytest.mark.asyncio
async def test_scrape_site_no_results(mocker):
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

    results = await scraping_service.scrape_site(
        "1337x",
        "Sample",
        "movie",
        context,
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


def test_strategy_find_direct_links_magnet():
    html = '<a href="magnet:?xt=urn:btih:123">Magnet</a>'
    soup = BeautifulSoup(html, "lxml")
    links = scraping_service._strategy_find_direct_links(soup)
    assert links == {"magnet:?xt=urn:btih:123"}


def test_strategy_find_direct_links_torrent():
    html = '<a href="https://example.com/file.torrent">Download</a>'
    soup = BeautifulSoup(html, "lxml")
    links = scraping_service._strategy_find_direct_links(soup)
    assert links == {"https://example.com/file.torrent"}


def test_strategy_find_direct_links_none():
    html = '<a href="/other">Link</a>'
    soup = BeautifulSoup(html, "lxml")
    links = scraping_service._strategy_find_direct_links(soup)
    assert links == set()


def test_strategy_contextual_search_keyword():
    html = '<a href="/download/123">Download Torrent</a>'
    soup = BeautifulSoup(html, "lxml")
    links = scraping_service._strategy_contextual_search(soup, "Query")
    assert "/download/123" in links


def test_strategy_contextual_search_query_match():
    html = '<a href="/details.php?id=456">My Show S01E01 1080p</a>'
    soup = BeautifulSoup(html, "lxml")
    links = scraping_service._strategy_contextual_search(soup, "My Show")
    assert "/details.php?id=456" in links


def test_strategy_contextual_search_unrelated_keyword():
    html = '<a href="/about">About our download policy</a>'
    soup = BeautifulSoup(html, "lxml")
    links = scraping_service._strategy_contextual_search(soup, "My Show")
    assert "/about" in links


def test_strategy_find_in_tables_single_match():
    html = '<table><tr><td>My Show</td><td><a href="/dl">Download</a></td></tr></table>'
    soup = BeautifulSoup(html, "lxml")
    results = scraping_service._strategy_find_in_tables(soup, "My Show")
    assert "/dl" in results


def test_strategy_find_in_tables_multiple_matches():
    html = """
    <table>
      <tr><td>My Show S01E01</td><td><a href="/e1">DL</a></td></tr>
      <tr><td>My Show S01E02</td><td><a href="/e2">DL</a></td></tr>
    </table>
    """
    soup = BeautifulSoup(html, "lxml")
    results = scraping_service._strategy_find_in_tables(soup, "My Show")
    assert {"/e1", "/e2"}.issubset(results.keys())


def test_strategy_find_in_tables_ignores_unrelated_tables():
    html = """
    <table><tr><td>Other</td><td><a href="/x">X</a></td></tr></table>
    <table><tr><td>My Show</td><td><a href="/dl">Download</a></td></tr></table>
    """
    soup = BeautifulSoup(html, "lxml")
    results = scraping_service._strategy_find_in_tables(soup, "My Show")
    assert "/dl" in results and "/x" not in results


def test_score_candidate_links_prefers_magnet():
    html = (
        '<div><a href="magnet:?xt=urn:btih:1">Magnet</a></div>'
        '<div><a href="/context">Download Torrent</a></div>'
        '<table><tr><td>My Show</td><td><a href="/table">Link</a></td></tr></table>'
    )
    soup = BeautifulSoup(html, "lxml")
    links = {"magnet:?xt=urn:btih:1", "/context", "/table"}
    table_links = {"/table": 80.0}
    best = scraping_service._score_candidate_links(links, "My Show", table_links, soup)
    assert best == "magnet:?xt=urn:btih:1"


def test_score_candidate_links_penalizes_ads():
    html = (
        '<div class="ad"><a href="/bad">My Show 1080p</a></div>'
        '<div><a href="/good">My Show 1080p</a></div>'
    )
    soup = BeautifulSoup(html, "lxml")
    links = {"/bad", "/good"}
    best = scraping_service._score_candidate_links(links, "My Show", {}, soup)
    assert best == "/good"


def test_score_candidate_links_prefers_better_match():
    html = (
        '<div><a href="/high">My Show Episode</a></div>'
        '<div><a href="/low">Another Show</a></div>'
    )
    soup = BeautifulSoup(html, "lxml")
    links = {"/high", "/low"}
    best = scraping_service._score_candidate_links(links, "My Show Episode", {}, soup)
    assert best == "/high"
