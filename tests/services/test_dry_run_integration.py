import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock
import pytest

from telegram_bot.services import scraping_service
from telegram_bot.services.search_logic import orchestrate_searches

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))


def _filter_by_resolution(results, resolution: str):
    res = resolution.lower()
    pats: tuple[str, ...]
    if res == "2160p":
        pats = ("2160p", "4k")
    elif res == "1080p":
        pats = ("1080p",)
    elif res == "720p":
        pats = ("720p",)
    else:
        pats = (res,)
    out = []
    for r in results:
        t = str(r.get("title", "")).lower()
        if any(p in t for p in pats):
            out.append(r)
    return out


def _ctx_with_search_config():
    ctx = Mock()
    ctx.bot_data = {
        "SEARCH_CONFIG": {
            "websites": {
                "movies": [
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
                ],
                "tv": [],
            },
            "preferences": {
                "movies": {
                    "resolutions": {"2160p": 5, "4k": 5, "1080p": 4, "720p": 1},
                    "codecs": {"x265": 4, "hevc": 4, "x264": 1, "h264": 1},
                    "uploaders": {"QxR": 5, "YTS": 4},
                },
                "tv": {
                    "resolutions": {"1080p": 5, "720p": 1},
                    "codecs": {"x265": 4, "hevc": 4, "x264": 1, "h264": 1},
                    "uploaders": {"EZTV": 5, "MeGusta": 5},
                },
            },
        }
    }
    return ctx


@pytest.mark.asyncio
async def test_dry_run_flow_uses_wiki_year_and_filters_resolution(mocker):
    # Mock Wikipedia years for 'Alien'
    mocker.patch(
        "telegram_bot.services.scraping_service.fetch_movie_years_from_wikipedia",
        new=AsyncMock(return_value=([1979], None)),
    )

    # Mock scrapers
    yts_results = [
        {"title": "Alien (1979) 1080p WEB x265 [YTS]", "score": 21, "source": "yts.lt"},
        {"title": "Alien (1979) 720p WEB x264 [YTS]", "score": 11, "source": "yts.lt"},
    ]
    x_results = [
        {"title": "Alien.1979.1080p.BluRay.x265", "score": 16, "source": "1337x"},
    ]
    m_yts = mocker.patch(
        "telegram_bot.services.scraping_service.scrape_yts",
        new=AsyncMock(return_value=yts_results),
    )
    m_1337 = mocker.patch(
        "telegram_bot.services.scraping_service.scrape_1337x",
        new=AsyncMock(return_value=x_results),
    )

    ctx = _ctx_with_search_config()
    title = "Alien"
    resolution = "1080p"

    years, corrected = await scraping_service.fetch_movie_years_from_wikipedia(title)
    base_for_search = corrected or title
    assert years == [1979]

    results = await orchestrate_searches(
        base_for_search, "movie", ctx, year=str(years[0]), resolution=resolution
    )

    # Ensure scrapers were called with expected args
    yts_call = m_yts.await_args
    x_call = m_1337.await_args
    assert yts_call.args[0] == base_for_search
    assert x_call.args[0] == f"{base_for_search} 1979"

    filtered = _filter_by_resolution(results, resolution)
    assert filtered and all("1080p" in r["title"].lower() for r in filtered)
    # Sorted by score desc: YTS 21, 1337x 16
    assert [r["score"] for r in filtered] == [21, 16]


@pytest.mark.asyncio
async def test_dry_run_movie_explicit_year_overrides_wiki(mocker):
    # Wikipedia would return an incorrect or different year, but explicit year should win
    mocker.patch(
        "telegram_bot.services.scraping_service.fetch_movie_years_from_wikipedia",
        new=AsyncMock(return_value=([1983], None)),
    )

    yts_results = [
        {
            "title": "The Thing (1982) 1080p WEB x265 [YTS]",
            "score": 22,
            "source": "yts.lt",
        },
    ]
    x_results = [
        {"title": "The.Thing.1982.1080p.BluRay.x265", "score": 18, "source": "1337x"},
    ]
    m_yts = mocker.patch(
        "telegram_bot.services.scraping_service.scrape_yts",
        new=AsyncMock(return_value=yts_results),
    )
    m_1337 = mocker.patch(
        "telegram_bot.services.scraping_service.scrape_1337x",
        new=AsyncMock(return_value=x_results),
    )

    ctx = _ctx_with_search_config()
    title = "The Thing 1982"
    resolution = "1080p"

    # Emulate dry-run logic: extract explicit year and base
    import re as _re

    m = _re.search(r"\b(19\d{2}|20\d{2})\b", title)
    explicit_year = m.group(1) if m else None
    base = title[: m.start()].strip() if m else title

    # Wikipedia still queried, but we must prefer explicit_year in orchestrate
    years, corrected = await scraping_service.fetch_movie_years_from_wikipedia(base)
    base_for_search = corrected or base
    assert years == [1983]
    assert explicit_year == "1982"

    results = await orchestrate_searches(
        base_for_search, "movie", ctx, year=explicit_year, resolution=resolution
    )

    # Ensure scrapers called with explicit year
    yts_call = m_yts.await_args
    x_call = m_1337.await_args
    assert yts_call.args[0] == base_for_search
    assert x_call.args[0] == f"{base_for_search} 1982"
    assert all("1982" in r["title"] for r in results)


@pytest.mark.asyncio
async def test_dry_run_tv_search_workflow_basic(mocker):
    # TV workflow uses TV websites set; here we only include 1337x TV category
    ctx = Mock()
    ctx.bot_data = {
        "SEARCH_CONFIG": {
            "websites": {
                "movies": [],
                "tv": [
                    {
                        "name": "1337x",
                        "enabled": True,
                        "search_url": "https://1337x.to/category-search/{query}/TV/1/",
                    }
                ],
            },
            "preferences": {
                "tv": {
                    "resolutions": {"1080p": 5, "720p": 1},
                    "codecs": {"x265": 4, "hevc": 4, "x264": 1, "h264": 1},
                    "uploaders": {"EZTV": 5, "MeGusta": 5},
                }
            },
        }
    }

    x_results = [
        {"title": "My.Show.S01E01.1080p.WEB.x265", "score": 17, "source": "1337x"},
        {"title": "My.Show.S01E01.720p.WEB.x264", "score": 9, "source": "1337x"},
    ]

    m_1337 = mocker.patch(
        "telegram_bot.services.scraping_service.scrape_1337x",
        new=AsyncMock(return_value=x_results),
    )

    query = "My Show S01E01"
    resolution = "1080p"
    results = await orchestrate_searches(query, "tv", ctx, resolution=resolution)

    # 1337x called with query as-is (no year appended for TV)
    x_call = m_1337.await_args
    assert x_call.args[0] == query

    # Filter and assert top result is 1080p
    filtered = _filter_by_resolution(results, resolution)
    assert filtered and filtered[0]["title"].lower().find("1080p") != -1
