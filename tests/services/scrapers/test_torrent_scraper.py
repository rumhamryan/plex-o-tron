import sys
from pathlib import Path
import logging
import pytest
from unittest.mock import Mock, AsyncMock
from telegram_bot.services.scrapers import torrent_scraper

sys.path.append(str(Path(__file__).resolve().parent.parent.parent.parent))


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


# --- 1337x Tests (using GenericTorrentScraper) ---


@pytest.mark.asyncio
async def test_scrape_1337x_parses_results(mocker):
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
    detail_html = """
    <div>
      <a class="btn-magnet" href="magnet:?xt=urn:btih:FAKEHASH">Magnet Download</a>
    </div>
    """
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

    results = await torrent_scraper.scrape_1337x(
        "Sample Movie 2023",
        "movie",
        search_url="https://1337x.to/search/{query}/1/",
        context=context,
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
                "movies": {},
            }
        }
    }

    results = await torrent_scraper.scrape_1337x(
        "Sample",
        "movie",
        search_url="https://1337x.to/search/{query}/1/",
        context=context,
        base_query_for_filter="Sample Movie",
    )

    assert results == []


@pytest.mark.asyncio
async def test_scrape_1337x_fuzzy_filter(mocker):
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

    results = await torrent_scraper.scrape_1337x(
        "Sample Movie 2023",
        "movie",
        search_url="https://1337x.to/search/{query}/1/",
        context=context,
        base_query_for_filter="Sample Movie",
    )

    assert len(results) == 1
    assert results[0]["title"] == "Sample.Movie.2023.1080p.x265"
    assert client._index == 2


@pytest.mark.asyncio
async def test_scrape_1337x_passes_limit(mocker):
    mock_search = AsyncMock(return_value=[])
    mocker.patch(
        "telegram_bot.services.scrapers.torrent_scraper.GenericTorrentScraper.search",
        mock_search,
    )

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {"preferences": {"movies": {"codecs": {"x": 1}}}}
    }

    await torrent_scraper.scrape_1337x(
        "query",
        "movie",
        search_url="https://example.com/{query}",
        context=context,
        limit=7,
    )

    mock_search.assert_awaited_once()
    assert mock_search.call_args.kwargs["limit"] == 7


# --- YTS Tests (using YtsScraper in torrent_scraper.py) ---


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

    scraper = torrent_scraper.YtsScraper()
    results = await scraper.search(
        "Test Movie",
        "movie",
        search_url="https://yts.mx/browse-movies/{query}",
        context=context,
        year="2023",
        resolution="1080p",
    )

    assert len(results) == 1
    assert results[0]["source"] == "YTS.mx"
    assert results[0]["seeders"] == 10


@pytest.mark.asyncio
async def test_scrape_yts_retries_on_validation_failure(caplog, mocker):
    search_html = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.mx/movies/test-movie">Test Movie</a>
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
                "torrents": [],
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

    scraper = torrent_scraper.YtsScraper()
    with caplog.at_level(logging.DEBUG):
        results = await scraper.search(
            "Test Movie",
            "movie",
            search_url="https://yts.mx/browse-movies/{query}",
            context=context,
            year="2023",
            resolution="1080p",
        )

    assert len(results) == 1
    assert any("attempt 1 failed validation" in m for m in caplog.messages)
    assert any("attempt 2 succeeded" in m for m in caplog.messages)


@pytest.mark.asyncio
async def test_scrape_yts_paginates_browse_pages_to_find_year(mocker):
    search_html_p1 = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.mx/movies/alien-xyz">Alien Something</a>
      <div class="browse-movie-year">2003</div>
    </div>
    """
    search_html_p2 = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.mx/movies/alien-1979">Alien</a>
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
        DummyResponse(text=search_html_p1),
        DummyResponse(text=search_html_p2),
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

    scraper = torrent_scraper.YtsScraper()
    results = await scraper.search(
        "Alien",
        "movie",
        search_url="https://yts.mx/browse-movies/{query}",
        context=context,
        year="1979",
        resolution="1080p",
    )

    assert len(results) == 1
    assert results[0]["source"] == "YTS.mx"
    assert results[0]["seeders"] == 10


@pytest.mark.asyncio
async def test_scrape_yts_api_fallback_relaxes_quality(mocker):
    search_html = """
    <div class="other"></div>
    """
    api_empty = {"status": "ok", "data": {"movie_count": 0}}
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
        DummyResponse(text=search_html),
        DummyResponse(text=search_html),
        DummyResponse(text=search_html),
        DummyResponse(text=search_html),
        DummyResponse(text=search_html),
        DummyResponse(json_data=api_empty),
        DummyResponse(json_data=api_with_movie),
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

    scraper = torrent_scraper.YtsScraper()
    results = await scraper.search(
        "Test Movie",
        "movie",
        search_url="https://yts.mx/browse-movies/{query}",
        context=context,
        year="1979",
        resolution="1080p",
    )

    assert len(results) == 1
    assert results[0]["seeders"] == 7
    assert results[0]["source"] == "YTS.mx"


@pytest.mark.asyncio
async def test_scrape_yts_api_fallback_relaxes_year(mocker):
    search_html = '<div class="other"></div>'
    api_empty = {"status": "ok", "data": {"movie_count": 0}}
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
        DummyResponse(text=search_html),
        DummyResponse(text=search_html),
        DummyResponse(text=search_html),
        DummyResponse(text=search_html),
        DummyResponse(text=search_html),
        DummyResponse(json_data=api_empty),
        DummyResponse(json_data=api_empty),
        DummyResponse(json_data=api_all_years),
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

    scraper = torrent_scraper.YtsScraper()
    results = await scraper.search(
        "Alien",
        "movie",
        search_url="https://yts.mx/browse-movies/{query}",
        context=context,
        year="1979",
        resolution="1080p",
    )

    assert len(results) == 1
    assert (
        results[0]["title"].startswith("Alien (1979)")
        or "(1979)" in results[0]["title"]
    )
    assert results[0]["source"] == "YTS.mx"


@pytest.mark.asyncio
async def test_scrape_yts_token_gate_avoids_near_homonyms(mocker):
    browse_dunes = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.mx/movies/the-dunes-1979">The Dunes</a>
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
        DummyResponse(text=browse_dunes),
        DummyResponse(text=browse_empty),
        DummyResponse(text=browse_empty),
        DummyResponse(text=browse_empty),
        DummyResponse(text=browse_empty),
        DummyResponse(json_data=api_with_movie),
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

    scraper = torrent_scraper.YtsScraper()
    results = await scraper.search(
        "Dune",
        "movie",
        search_url="https://yts.mx/browse-movies/{query}",
        context=context,
        year="1979",
        resolution="1080p",
    )

    assert len(results) == 1
    assert results[0]["source"] == "YTS.mx"
