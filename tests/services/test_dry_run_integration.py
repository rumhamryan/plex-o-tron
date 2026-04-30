from unittest.mock import AsyncMock, Mock

import pytest

from telegram_bot.services import scraping_service
from telegram_bot.services.discovery.orchestrator import PROVIDER_FACTORY
from telegram_bot.services.discovery.providers.base import BaseProvider
from telegram_bot.services.discovery.schemas import DiscoveryRequest, DiscoveryResult
from telegram_bot.services.search_logic import orchestrate_searches

SCRAPER_MAX_TORRENT_SIZE_BOT_DATA_KEY = "SCRAPER_MAX_TORRENT_SIZE_GIB"


class DryRunDiscoveryProvider(BaseProvider):
    calls: list[DiscoveryRequest] = []
    results: list[DiscoveryResult] = []

    async def search(self, request: DiscoveryRequest) -> list[DiscoveryResult]:
        type(self).calls.append(request)
        return type(self).results


@pytest.fixture
def dry_run_provider(mocker):
    DryRunDiscoveryProvider.calls = []
    DryRunDiscoveryProvider.results = []
    mocker.patch.dict(PROVIDER_FACTORY, {"dry_run": DryRunDiscoveryProvider})
    return DryRunDiscoveryProvider


def _ctx_with_search_config(media_type: str = "movie"):
    provider = {
        "name": "Dry Run Provider",
        "type": "dry_run",
        "enabled": True,
        "search_url": "http://127.0.0.1:9696/1/api?q={query}",
    }
    return Mock(
        bot_data={
            SCRAPER_MAX_TORRENT_SIZE_BOT_DATA_KEY: 22.0,
            "SEARCH_CONFIG": {
                "providers": {
                    "movies": [provider] if media_type == "movie" else [],
                    "tv": [provider] if media_type == "tv" else [],
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
            },
        }
    )


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
    for result in results:
        title = str(result.get("title", "")).lower()
        if any(pattern in title for pattern in pats):
            out.append(result)
    return out


@pytest.mark.asyncio
async def test_dry_run_flow_uses_wiki_year_and_filters_resolution(mocker, dry_run_provider):
    mocker.patch(
        "telegram_bot.services.scraping_service.fetch_movie_years_from_wikipedia",
        new=AsyncMock(return_value=([1979], None)),
    )
    dry_run_provider.results = [
        DiscoveryResult(
            title="Alien (1979) 1080p WEB x265 [YTS]",
            download_url="magnet:?xt=urn:btih:ALIEN1080",
            source="Dry Run Provider",
            size_bytes=2 * 1024**3,
            seeders=100,
            leechers=4,
            uploader="YTS",
        ),
        DiscoveryResult(
            title="Alien (1979) 720p WEB x264 [YTS]",
            download_url="magnet:?xt=urn:btih:ALIEN720",
            source="Dry Run Provider",
            size_bytes=1024**3,
            seeders=50,
            leechers=2,
            uploader="YTS",
        ),
    ]

    ctx = _ctx_with_search_config()
    title = "Alien"
    resolution = "1080p"

    years, corrected = await scraping_service.fetch_movie_years_from_wikipedia(title)
    base_for_search = corrected or title
    assert years == [1979]

    results = await orchestrate_searches(
        base_for_search, "movie", ctx, year=str(years[0]), resolution=resolution
    )

    assert dry_run_provider.calls[0].query == "Alien 1979"
    filtered = _filter_by_resolution(results, resolution)
    assert filtered and all("1080p" in result["title"].lower() for result in filtered)


@pytest.mark.asyncio
async def test_dry_run_movie_explicit_year_overrides_wiki(mocker, dry_run_provider):
    mocker.patch(
        "telegram_bot.services.scraping_service.fetch_movie_years_from_wikipedia",
        new=AsyncMock(return_value=([1983], None)),
    )
    dry_run_provider.results = [
        DiscoveryResult(
            title="The Thing (1982) 1080p WEB x265",
            download_url="magnet:?xt=urn:btih:THING1982",
            source="Dry Run Provider",
            size_bytes=2 * 1024**3,
            seeders=100,
            leechers=5,
        ),
    ]

    ctx = _ctx_with_search_config()
    title = "The Thing 1982"
    resolution = "1080p"

    import re as _re

    match = _re.search(r"\b(19\d{2}|20\d{2})\b", title)
    explicit_year = match.group(1) if match else None
    base = title[: match.start()].strip() if match else title

    years, corrected = await scraping_service.fetch_movie_years_from_wikipedia(base)
    base_for_search = corrected or base
    assert years == [1983]
    assert explicit_year == "1982"

    results = await orchestrate_searches(
        base_for_search, "movie", ctx, year=explicit_year, resolution=resolution
    )

    assert dry_run_provider.calls[0].query == "The Thing 1982"
    assert all("1982" in result["title"] for result in results)


@pytest.mark.asyncio
async def test_dry_run_tv_search_workflow_basic(dry_run_provider):
    dry_run_provider.results = [
        DiscoveryResult(
            title="My.Show.S01E01.1080p.WEB.x265",
            download_url="magnet:?xt=urn:btih:SHOW1080",
            source="Dry Run Provider",
            size_bytes=2 * 1024**3,
            seeders=100,
            leechers=5,
        ),
        DiscoveryResult(
            title="My.Show.S01E01.720p.WEB.x264",
            download_url="magnet:?xt=urn:btih:SHOW720",
            source="Dry Run Provider",
            size_bytes=1024**3,
            seeders=50,
            leechers=2,
        ),
    ]

    ctx = _ctx_with_search_config("tv")
    query = "My Show S01E01"
    resolution = "1080p"
    results = await orchestrate_searches(query, "tv", ctx, resolution=resolution)

    assert dry_run_provider.calls[0].query == query
    filtered = _filter_by_resolution(results, resolution)
    assert filtered and "1080p" in filtered[0]["title"].lower()
