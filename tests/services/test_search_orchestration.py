import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock
import pytest

from telegram_bot.services.search_logic import orchestrate_searches

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))


def _ctx_with_config(
    websites_movies=None, websites_tv=None, prefs_movies=None, prefs_tv=None
):
    ctx = Mock()
    ctx.bot_data = {
        "SEARCH_CONFIG": {
            "websites": {
                "movies": websites_movies or [],
                "tv": websites_tv or [],
            },
            "preferences": {
                "movies": prefs_movies
                or {
                    "codecs": {"x265": 4},
                    "resolutions": {"1080p": 3},
                    "uploaders": {},
                },
                "tv": prefs_tv
                or {
                    "codecs": {"x265": 4},
                    "resolutions": {"1080p": 3},
                    "uploaders": {},
                },
            },
        }
    }
    return ctx


@pytest.mark.asyncio
async def test_orchestrate_searches_calls_sites_and_sorts(mocker):
    ctx = _ctx_with_config(
        websites_movies=[
            {
                "name": "yts.lt",
                "enabled": True,
                "search_url": "https://yts.lt/browse-movies/{query}/all/all/0/latest/0/all",
            },
            {
                "name": "1337x",
                "enabled": True,
                "search_url": "https://1337x.to/category-search/{query}/Movies/1/",
            },
        ]
    )

    yts_results = [
        {"title": "Alien (1979) 1080p", "score": 20, "source": "yts.lt", "seeders": 25},
        {"title": "Alien (1979) 720p", "score": 10, "source": "yts.lt", "seeders": 25},
    ]
    txx_results = [
        {"title": "Alien.1979.1080p", "score": 15, "source": "1337x", "seeders": 25},
    ]
    m_yts = mocker.patch(
        "telegram_bot.services.scraping_service.scrape_yts",
        new=AsyncMock(return_value=yts_results),
    )
    m_1337 = mocker.patch(
        "telegram_bot.services.scraping_service.scrape_1337x",
        new=AsyncMock(return_value=txx_results),
    )

    results = await orchestrate_searches("Alien", "movie", ctx, year="1979")

    # Both scrapers called
    assert m_yts.await_count == 1
    assert m_1337.await_count == 1

    # 1337x receives year appended to query; YTS does not
    yts_call = m_yts.await_args
    x_call = m_1337.await_args

    assert yts_call.args[0] == "Alien"  # query as-is
    assert x_call.args[0] == "Alien 1979"  # year appended
    # base_query_for_filter passed to 1337x and equals original query
    assert x_call.kwargs.get("base_query_for_filter") == "Alien"

    # Sorted by score desc: top is YTS 1080p 20, then 1337x 15, then YTS 720p 10
    assert [r["score"] for r in results] == [20, 15, 10]


@pytest.mark.asyncio
async def test_orchestrate_searches_respects_enabled_flag(mocker):
    ctx = _ctx_with_config(
        websites_movies=[
            {
                "name": "yts.lt",
                "enabled": False,
                "search_url": "https://yts.lt/browse-movies/{query}",
            },
            {
                "name": "1337x",
                "enabled": True,
                "search_url": "https://1337x.to/category-search/{query}/Movies/1/",
            },
        ]
    )

    m_yts = mocker.patch(
        "telegram_bot.services.scraping_service.scrape_yts",
        new=AsyncMock(return_value=[]),
    )
    m_1337 = mocker.patch(
        "telegram_bot.services.scraping_service.scrape_1337x",
        new=AsyncMock(
            return_value=[
                {
                    "title": "Alien.1979.1080p",
                    "score": 10,
                    "source": "1337x",
                    "seeders": 25,
                }
            ]
        ),
    )

    results = await orchestrate_searches("Alien", "movie", ctx, year="1979")
    assert results and results[0]["source"] == "1337x"
    # YTS disabled, so not called
    assert m_yts.await_count == 0
    assert m_1337.await_count == 1


@pytest.mark.asyncio
async def test_orchestrate_searches_yaml_fallback_for_unknown_site(mocker):
    ctx = _ctx_with_config(
        websites_movies=[
            {
                "name": "EZTV",
                "enabled": True,
                "search_url": "https://eztv.re/search/{query}",
            },
        ]
    )

    m_yaml = mocker.patch(
        "telegram_bot.services.scraping_service.scrape_yaml_site",
        new=AsyncMock(
            return_value=[
                {
                    "title": "Alien (1979) EZ",
                    "score": 9,
                    "source": "EZTV",
                    "seeders": 25,
                }
            ]
        ),
    )

    results = await orchestrate_searches("Alien", "movie", ctx, year="1979")
    assert results and results[0]["source"] == "EZTV"

    # Ensure YAML path used with site_name and base_query_for_filter
    call = m_yaml.await_args
    # Positional args: query, media_type, _search_url_template, context
    assert call.args[0].startswith("Alien")
    assert call.kwargs.get("site_name") == "eztv"
    assert call.kwargs.get("base_query_for_filter") == "Alien"


@pytest.mark.asyncio
async def test_orchestrate_searches_handles_yts_name_variants(mocker):
    ctx = _ctx_with_config(
        websites_movies=[
            {
                "name": "YTS.LT",  # Mixed case to ensure normalization
                "enabled": True,
                "search_url": "https://yts.lt/browse-movies/{query}/all/all/0/latest/0/all",
            }
        ]
    )

    m_yts = mocker.patch(
        "telegram_bot.services.scraping_service.scrape_yts",
        new=AsyncMock(
            return_value=[
                {"title": "Alien", "score": 10, "source": "yts.lt", "seeders": 25}
            ]
        ),
    )
    results = await orchestrate_searches("Alien", "movie", ctx, year="1979")
    assert results and results[0]["source"] == "yts.lt"
    assert m_yts.await_count == 1
    # ensure query unmodified for YTS
    call = m_yts.await_args
    assert call.args[0] == "Alien"


@pytest.mark.asyncio
async def test_orchestrate_searches_normalizes_eztv_domains(mocker):
    ctx = _ctx_with_config(
        websites_tv=[
            {
                "name": "eztvx.to",
                "enabled": True,
                "search_url": "https://eztvx.to/search/{query}",
            }
        ]
    )

    m_yaml = mocker.patch(
        "telegram_bot.services.scraping_service.scrape_yaml_site",
        new=AsyncMock(
            return_value=[
                {"title": "Show", "score": 10, "source": "eztv", "seeders": 25}
            ]
        ),
    )
    results = await orchestrate_searches("Example Show S01E01", "tv", ctx)

    assert results and results[0]["source"] == "eztv"
    assert m_yaml.await_count == 1
    call = m_yaml.await_args
    assert call.kwargs.get("site_name") == "eztv"


@pytest.mark.asyncio
async def test_orchestrate_searches_uses_tpb_scraper(mocker):
    ctx = _ctx_with_config(
        websites_movies=[
            {
                "name": "TPB",
                "enabled": True,
                "search_url": "https://thepiratebay.org/search.php?q={query}&cat=0",
            }
        ]
    )

    m_tpb = mocker.patch(
        "telegram_bot.services.scraping_service.scrape_tpb",
        new=AsyncMock(
            return_value=[
                {"title": "Movie", "score": 8, "source": "tpb", "seeders": 25}
            ]
        ),
    )

    results = await orchestrate_searches("Movie", "movie", ctx)
    assert results and results[0]["source"] == "tpb"
    assert m_tpb.await_count == 1
    call = m_tpb.await_args
    # Query forwarded as-is; base_query_for_filter also included.
    assert call.args[0] == "Movie"
    assert call.kwargs.get("base_query_for_filter") == "Movie"
