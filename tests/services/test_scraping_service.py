# tests/services/test_scraping_service.py

import sys
from pathlib import Path
import logging
import pytest
from unittest.mock import Mock, AsyncMock
import wikipedia
from bs4 import BeautifulSoup
from telegram_bot.services import scraping_service

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))


# Ensure per-test isolation by clearing any module-level caches used by the
# scraping_service. This prevents earlier tests from influencing later ones
# (e.g., Wikipedia title caches across the same show/season).
@pytest.fixture(autouse=True)
def _clear_wiki_caches():
    try:
        scraping_service._WIKI_TITLES_CACHE.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        scraping_service._WIKI_SOUP_CACHE.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        scraping_service._WIKI_MOVIE_CACHE.clear()  # type: ignore[attr-defined]
    except Exception:
        pass


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

DEDICATED_WITH_OVERVIEW_ONGOING_HTML = """
<h3>Season 27</h3>
<table class="wikitable">
<tr><th>No. in season</th><th>Title</th></tr>
<tr><td>1</td><td><i>Ep1</i></td></tr>
<tr><td>2</td><td><i>Ep2</i></td></tr>
<tr><td>3</td><td><i>Ep3</i></td></tr>
<tr><td>4</td><td><i>Ep4</i></td></tr>
</table>

<h2>Series overview</h2>
<table class="wikitable">
<tr><th>Season</th><th>Episodes</th><th>Last aired</th></tr>
<tr><td>27</td><td>10</td><td>present</td></tr>
</table>
"""

OVERVIEW_ONGOING_ONLY_HTML = """
<h2>Series overview</h2>
<table class="wikitable">
<tr><th>Season</th><th>Episodes</th><th>Originally aired</th></tr>
<tr><td>27</td><td>10</td><td>2024–present</td></tr>
</table>
"""


@pytest.mark.asyncio
async def test_fetch_episode_title_dedicated_page(mocker):
    mock_page = mocker.Mock()
    mock_page.title = "Show"
    mock_page.url = "http://example.com"
    mock_page.html.return_value = DEDICATED_HTML
    mocker.patch("wikipedia.search", return_value=["Show"])
    mocker.patch("wikipedia.page", return_value=mock_page)

    title, corrected = await scraping_service.fetch_episode_title_from_wikipedia(
        "Show", 1, 1
    )
    assert title == "Pilot"
    assert corrected is None


@pytest.mark.asyncio
async def test_fetch_episode_title_strips_miniseries_suffix(mocker):
    mock_main_page = mocker.Mock()
    mock_main_page.title = "Show (miniseries)"
    mock_main_page.url = "http://example.com/show"

    mock_list_page = mocker.Mock()
    mock_list_page.url = "http://example.com/list"
    mock_list_page.html.return_value = DEDICATED_HTML

    mocker.patch("wikipedia.search", return_value=["Show (miniseries)"])
    page_patch = mocker.patch(
        "wikipedia.page", side_effect=[mock_main_page, mock_list_page]
    )

    title, corrected = await scraping_service.fetch_episode_title_from_wikipedia(
        "Show", 1, 1
    )

    assert title == "Pilot"
    assert corrected is None
    assert page_patch.call_args_list[1].args[0] == "List of Show episodes"


@pytest.mark.asyncio
async def test_fetch_episode_title_strips_tv_series_suffix(mocker):
    mock_main_page = mocker.Mock()
    mock_main_page.title = "Show (TV series)"
    mock_main_page.url = "http://example.com/show"

    mock_list_page = mocker.Mock()
    mock_list_page.url = "http://example.com/list"
    mock_list_page.html.return_value = DEDICATED_HTML

    mocker.patch("wikipedia.search", return_value=["Show (TV series)"])
    page_patch = mocker.patch(
        "wikipedia.page", side_effect=[mock_main_page, mock_list_page]
    )

    title, corrected = await scraping_service.fetch_episode_title_from_wikipedia(
        "Show", 1, 1
    )

    assert title == "Pilot"
    assert corrected is None
    assert page_patch.call_args_list[1].args[0] == "List of Show episodes"


@pytest.mark.asyncio
async def test_fetch_episode_title_embedded_page(mocker):
    mock_main_page = mocker.Mock()
    mock_main_page.title = "Show"
    mock_main_page.url = "http://example.com/main"
    mock_main_page.html.return_value = SIMPLE_EMBEDDED_HTML

    mocker.patch("wikipedia.search", return_value=["Show"])
    mocker.patch(
        "wikipedia.page",
        side_effect=[mock_main_page, wikipedia.exceptions.PageError("no list")],
    )

    title, _ = await scraping_service.fetch_episode_title_from_wikipedia("Show", 1, 1)
    assert title == "Pilot"


@pytest.mark.asyncio
async def test_fetch_episode_title_not_found(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mock_page.html.return_value = NO_EPISODE_HTML
    mocker.patch("wikipedia.search", return_value=["Show"])
    mocker.patch("wikipedia.page", return_value=mock_page)

    title, _ = await scraping_service.fetch_episode_title_from_wikipedia("Show", 1, 1)
    assert title is None


@pytest.mark.asyncio
async def test_fetch_season_episode_count(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mock_page.html.return_value = SEASON_OVERVIEW_HTML
    mocker.patch("wikipedia.page", return_value=mock_page)

    count = await scraping_service.fetch_season_episode_count_from_wikipedia("Show", 2)
    assert count == 8


@pytest.mark.asyncio
async def test_fetch_season_episode_count_prefers_titles_over_overview(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mock_page.html.return_value = DEDICATED_WITH_OVERVIEW_ONGOING_HTML
    mocker.patch("wikipedia.page", return_value=mock_page)

    # Should return the enumerated title count (4), not the overview's 10
    count = await scraping_service.fetch_season_episode_count_from_wikipedia("Show", 27)
    assert count == 4


@pytest.mark.asyncio
async def test_fetch_season_episode_count_skips_ongoing_overview(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mock_page.html.return_value = OVERVIEW_ONGOING_ONLY_HTML
    mocker.patch("wikipedia.page", return_value=mock_page)

    # No titles are present and overview is marked ongoing -> expect None
    count = await scraping_service.fetch_season_episode_count_from_wikipedia("Show", 27)
    assert count is None


@pytest.mark.asyncio
async def test_scrape_1337x_parses_results(mocker):
    # This is the response for the initial search results page
    search_html = """
    <table class="table-list"><tbody>
    <tr>
      <td class="name">
        <a href="/cat">Movies</a>
        <a href="/torrent/1/Sample.Movie.2023.1080p.x265/">Sample.Movie.2023.1080p.x265</a>
      </td>
      <td class="seeds">10</td>
      <td class="leeches">0</td>
      <td class="size">1.5 GB</td>
      <td class="uploader"><a>Anonymous</a></td>
    </tr>
    </tbody></table>
    """

    # This is the required second response for the torrent detail page
    detail_html = """
    <div>
      <a class="btn-magnet" href="magnet:?xt=urn:btih:FAKEHASH">Magnet Download</a>
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
async def test_scrape_1337x_fuzzy_filter(mocker):
    """Non-matching titles should be filtered out when fuzzy filter is enabled."""

    search_html = """
    <table class="table-list"><tbody>
    <tr>
      <td class="name">
        <a href="/cat">Movies</a>
        <a href="/torrent/1/Sample.Movie.2023.1080p.x265/">Sample.Movie.2023.1080p.x265</a>
      </td>
      <td class="seeds">10</td>
      <td class="leeches">0</td>
      <td class="size">1.5 GB</td>
      <td class="uploader"><a>Anonymous</a></td>
    </tr>
    <tr>
      <td class="name">
        <a href="/cat">Movies</a>
        <a href="/torrent/2/Unrelated.File.2023.1080p.x265/">Unrelated.File.2023.1080p.x265</a>
      </td>
      <td class="seeds">5</td>
      <td class="leeches">0</td>
      <td class="size">1.0 GB</td>
      <td class="uploader"><a>Anonymous</a></td>
    </tr>
    </tbody></table>
    """

    detail_good = """
    <div><a class="btn-magnet" href="magnet:?xt=urn:btih:GOOD">Magnet</a></div>
    """

    responses = [
        DummyResponse(text=search_html),
        DummyResponse(text=detail_good),
    ]
    client = DummyClient(responses)
    mocker.patch("httpx.AsyncClient", return_value=client)

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
    # Only the search page and one detail page should have been requested
    assert client._index == 2


@pytest.mark.asyncio
async def test_scrape_1337x_passes_limit(mocker):
    """scrape_1337x should forward the limit argument to the scraper."""

    mock_search = AsyncMock(return_value=[])
    mocker.patch.object(scraping_service.GenericTorrentScraper, "search", mock_search)

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {"preferences": {"movies": {"codecs": {"x": 1}}}}
    }

    await scraping_service.scrape_1337x(
        "query", "movie", "https://example.com/{query}", context, limit=7
    )

    mock_search.assert_awaited_once()
    assert mock_search.call_args.kwargs["limit"] == 7


@pytest.mark.asyncio
async def test_scrape_eztv_parses_results(mocker):
    """EZTV YAML scraper parses results and resolves magnet links."""

    search_html = """
    <table>
      <tr class="forum_header_border" name="hover">
        <td>Example Show</td>
        <td>
          <a class="epinfo" href="/episodes/12345">
            Example.Show.S01E01.1080p.WEB.x264
          </a>
        </td>
        <td class="forum_thread_post_end">150</td>
        <td>1.4 GB</td>
        <td>0</td>
        <td>SceneGroup</td>
      </tr>
    </table>
    """
    detail_html = """
    <div>
      <a href="magnet:?xt=urn:btih:EZTVHASH&dn=Example+Show">Magnet</a>
    </div>
    """

    fetch_mock = mocker.patch(
        "telegram_bot.services.generic_torrent_scraper.GenericTorrentScraper._fetch_page",
        new_callable=AsyncMock,
        side_effect=[search_html, detail_html],
    )

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "tv": {
                    "codecs": {"x264": 2},
                    "resolutions": {"1080p": 3},
                    "uploaders": {},
                }
            }
        }
    }

    results = await scraping_service.scrape_yaml_site(
        "Example Show S01E01",
        "tv",
        "https://eztvx.to/search/{query}",
        context,
        site_name="eztv",
        base_query_for_filter="Example Show S01E01",
    )

    assert fetch_mock.await_count == 2
    assert len(results) == 1
    entry = results[0]
    assert entry["source"] == "eztv"
    assert entry["seeders"] == 150
    assert entry["page_url"].startswith("magnet:?xt=urn:btih:EZTVHASH")


@pytest.mark.asyncio
async def test_scrape_yts_parses_results(mocker):
    search_html = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.lt/movies/test-movie">Test Movie</a>
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
        "https://yts.lt/browse-movies/{query}",
        context,  # Pass the mock object here
        year="2023",
        resolution="1080p",
    )

    assert len(results) == 1
    assert results[0]["source"] == "yts.lt"
    assert results[0]["seeders"] == 10


@pytest.mark.asyncio
async def test_scrape_yts_retries_on_validation_failure(caplog, mocker):
    """YTS scraper retries when API validation fails."""
    search_html = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.lt/movies/test-movie">Test Movie</a>
      <div class="browse-movie-year">2023</div>
    </div>
    """
    movie_html = '<div id="movie-info" data-movie-id="1234"></div>'
    bad_api_json = {
        "status": "ok",
        "data": {
            "movie": {
                "title_long": "Test Movie (2023)",
                "year": 2023,
                "torrents": [],  # Missing torrents triggers retry
            }
        },
    }
    good_api_json = {
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
        DummyResponse(json_data=bad_api_json),
        DummyResponse(json_data=good_api_json),
    ]
    mocker.patch("httpx.AsyncClient", return_value=DummyClient(responses))
    mocker.patch("asyncio.sleep", new=AsyncMock())

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

    with caplog.at_level(logging.DEBUG):
        results = await scraping_service.scrape_yts(
            "Test Movie",
            "movie",
            "https://yts.lt/browse-movies/{query}",
            context,
            year="2023",
            resolution="1080p",
        )

    assert len(results) == 1
    assert any("attempt 1 failed validation" in m for m in caplog.messages)
    assert any("attempt 2 succeeded" in m for m in caplog.messages)


@pytest.mark.asyncio
async def test_scrape_yts_paginates_browse_pages_to_find_year(mocker):
    """When page 1 has no matching year, the scraper paginates to find older films."""
    # Page 1: no matching movies for the given year
    search_html_p1 = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.lt/movies/alien-xyz">Alien Something</a>
      <div class="browse-movie-year">2003</div>
    </div>
    """
    # Page 2: contains the correct 1979 entry
    search_html_p2 = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.lt/movies/alien-1979">Alien</a>
      <div class="browse-movie-year">1979</div>
    </div>
    """
    movie_html = '<div id="movie-info" data-movie-id="1234"></div>'
    api_json = {
        "status": "ok",
        "data": {
            "movie": {
                "title_long": "Alien (1979)",
                "year": 1979,
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
        DummyResponse(text=search_html_p1),  # browse page 1
        DummyResponse(text=search_html_p2),  # browse page 2
        DummyResponse(text=movie_html),  # movie page
        DummyResponse(json_data=api_json),  # details API
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
        "Alien",
        "movie",
        "https://yts.lt/browse-movies/{query}",
        context,
        year="1979",
        resolution="1080p",
    )

    assert len(results) == 1
    assert results[0]["source"] == "yts.lt"
    assert results[0]["seeders"] == 10


@pytest.mark.asyncio
async def test_scrape_yts_api_fallback_relaxes_quality(mocker):
    """API fallback tries again without quality when the first pass returns 0."""
    # No browse matches -> triggers API fallback
    search_html = """
    <div class="other"></div>
    """
    # Attempt 1 (year+quality): 0 movies
    api_empty = {"status": "ok", "data": {"movie_count": 0}}
    # Attempt 2 (year only): has one movie with 1080p torrent
    api_with_movie = {
        "status": "ok",
        "data": {
            "movies": [
                {
                    "title_long": "Test Movie (1979)",
                    "year": 1979,
                    "torrents": [
                        {
                            "quality": "1080p",
                            "type": "WEB",
                            "size_bytes": 1024**3,
                            "hash": "abcdef",
                            "seeds": 7,
                        }
                    ],
                }
            ]
        },
    }

    responses = [
        DummyResponse(text=search_html),  # browse page 1 (no choices)
        DummyResponse(text=search_html),  # browse page 2 (still no choices)
        DummyResponse(text=search_html),  # browse page 3
        DummyResponse(text=search_html),  # browse page 4
        DummyResponse(text=search_html),  # browse page 5
        DummyResponse(json_data=api_empty),  # API attempt 1 (year+quality)
        DummyResponse(json_data=api_with_movie),  # API attempt 2 (year only)
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
        "https://yts.lt/browse-movies/{query}",
        context,
        year="1979",
        resolution="1080p",
    )

    assert len(results) == 1
    assert results[0]["seeders"] == 7
    assert results[0]["source"] == "yts.lt"


@pytest.mark.asyncio
async def test_scrape_yts_api_fallback_relaxes_year(mocker):
    """API fallback eventually drops year param and filters locally by year."""
    # No browse matches -> triggers API fallback
    search_html = '<div class="other"></div>'
    api_empty = {"status": "ok", "data": {"movie_count": 0}}
    # Attempt 3 (no year param): include multiple years, only target year remains
    api_all_years = {
        "status": "ok",
        "data": {
            "movies": [
                {
                    "title_long": "Alien (1979)",
                    "year": 1979,
                    "torrents": [
                        {
                            "quality": "1080p",
                            "type": "WEB",
                            "size_bytes": 1024**3,
                            "hash": "abcd11",
                            "seeds": 5,
                        }
                    ],
                },
                {
                    "title_long": "Alien (2012)",
                    "year": 2012,
                    "torrents": [
                        {
                            "quality": "1080p",
                            "type": "WEB",
                            "size_bytes": 1024**3,
                            "hash": "efgh22",
                            "seeds": 9,
                        }
                    ],
                },
            ]
        },
    }

    responses = [
        DummyResponse(text=search_html),  # browse page 1
        DummyResponse(text=search_html),  # browse page 2
        DummyResponse(text=search_html),  # browse page 3
        DummyResponse(text=search_html),  # browse page 4
        DummyResponse(text=search_html),  # browse page 5
        DummyResponse(json_data=api_empty),  # API attempt 1 (year+quality)
        DummyResponse(json_data=api_empty),  # API attempt 2 (year only)
        DummyResponse(json_data=api_all_years),  # API attempt 3 (no year param)
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
        "Alien",
        "movie",
        "https://yts.lt/browse-movies/{query}",
        context,
        year="1979",
        resolution="1080p",
    )

    # Only the 1979 entry should remain after local filtering
    assert len(results) == 1
    assert (
        results[0]["title"].startswith("Alien (1979)")
        or "(1979)" in results[0]["title"]
    )
    assert results[0]["source"] == "yts.lt"


@pytest.mark.asyncio
async def test_scrape_yts_token_gate_avoids_near_homonyms(mocker):
    """With a year present, token gate avoids false matches like 'The Dunes' for 'Dune'."""
    # Page 1 contains 'The Dunes' (fails token gate), then pages 2-5 empty -> fallback
    browse_dunes = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.lt/movies/the-dunes-1979">The Dunes</a>
      <div class="browse-movie-year">1979</div>
    </div>
    """
    browse_empty = '<div class="other"></div>'

    api_with_movie = {
        "status": "ok",
        "data": {
            "movies": [
                {
                    "title_long": "Dune (1979)",
                    "year": 1979,
                    "torrents": [
                        {
                            "quality": "1080p",
                            "type": "WEB",
                            "size_bytes": 1024**3,
                            "hash": "aaaaaa",
                            "seeds": 3,
                        }
                    ],
                }
            ]
        },
    }

    responses = [
        DummyResponse(text=browse_dunes),  # page 1 (gated out)
        DummyResponse(text=browse_empty),  # page 2
        DummyResponse(text=browse_empty),  # page 3
        DummyResponse(text=browse_empty),  # page 4
        DummyResponse(text=browse_empty),  # page 5
        DummyResponse(json_data=api_with_movie),  # API fallback
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
        "Dune",
        "movie",
        "https://yts.lt/browse-movies/{query}",
        context,
        year="1979",
        resolution="1080p",
    )

    assert len(results) == 1
    assert results[0]["source"] == "yts.lt"


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
