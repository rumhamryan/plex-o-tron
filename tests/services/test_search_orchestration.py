import asyncio
import logging
from unittest.mock import Mock

import pytest

from telegram_bot.services.discovery.orchestrator import PROVIDER_FACTORY
from telegram_bot.services.discovery.providers.base import BaseProvider
from telegram_bot.services.discovery.schemas import DiscoveryRequest, DiscoveryResult
from telegram_bot.services.search_logic import orchestrate_searches


def _ctx_with_config(
    providers_movies=None,
    providers_tv=None,
    prefs_movies=None,
    prefs_tv=None,
):
    ctx = Mock()
    ctx.bot_data = {
        "SCRAPER_MAX_TORRENT_SIZE_GIB": 22.0,
        "SEARCH_CONFIG": {
            "providers": {
                "movies": providers_movies or [],
                "tv": providers_tv or [],
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
        },
    }
    return ctx


class FakeDiscoveryProvider(BaseProvider):
    calls: list[DiscoveryRequest] = []
    results: list[DiscoveryResult] = []

    async def search(self, request: DiscoveryRequest) -> list[DiscoveryResult]:
        type(self).calls.append(request)
        return type(self).results


class CancelledDiscoveryProvider(BaseProvider):
    async def search(self, request: DiscoveryRequest) -> list[DiscoveryResult]:
        raise asyncio.CancelledError()


@pytest.fixture
def fake_discovery_provider(mocker):
    FakeDiscoveryProvider.calls = []
    FakeDiscoveryProvider.results = []
    mocker.patch.dict(PROVIDER_FACTORY, {"fake_discovery": FakeDiscoveryProvider})
    return FakeDiscoveryProvider


@pytest.fixture
def cancelled_discovery_provider(mocker):
    mocker.patch.dict(PROVIDER_FACTORY, {"cancel_discovery": CancelledDiscoveryProvider})
    return CancelledDiscoveryProvider


def _provider(provider_type: str = "fake_discovery", **overrides):
    config = {
        "name": "Prowlarr",
        "type": provider_type,
        "enabled": True,
        "search_url": "http://127.0.0.1:9696/1/api?q={query}",
    }
    config.update(overrides)
    return config


@pytest.mark.asyncio
async def test_orchestrate_searches_uses_discovery_provider(fake_discovery_provider):
    ctx = _ctx_with_config(
        providers_movies=[
            _provider(
                name="Prowlarr 1337x",
                timeout_seconds=1,
                extra_legacy_key="ignored",
            )
        ],
        prefs_movies={
            "codecs": {"x265": 5},
            "resolutions": {"1080p": 5},
            "uploaders": {"trusted": 10},
        },
    )
    fake_discovery_provider.results = [
        DiscoveryResult(
            title="Alien 1979 1080p x265",
            download_url="magnet:?xt=urn:btih:DISCOVERY",
            magnet_url="magnet:?xt=urn:btih:DISCOVERY",
            source="Prowlarr 1337x",
            size_bytes=2 * 1024**3,
            seeders=30,
            leechers=1,
            uploader="trusted",
        )
    ]

    results = await orchestrate_searches("Alien", "movie", ctx, year="1979")

    assert len(fake_discovery_provider.calls) == 1
    assert fake_discovery_provider.calls[0].query == "Alien 1979"
    assert len(results) == 1
    assert results[0]["source"] == "Prowlarr 1337x"
    assert results[0]["page_url"] == "magnet:?xt=urn:btih:DISCOVERY"


@pytest.mark.asyncio
async def test_orchestrate_searches_caps_discovery_size_at_config_limit(
    fake_discovery_provider,
):
    ctx = _ctx_with_config(
        providers_movies=[_provider()],
        prefs_movies={"codecs": {"x265": 5}, "resolutions": {"1080p": 5}},
    )
    fake_discovery_provider.results = [
        DiscoveryResult(
            title="Movie 1080p x265 oversized",
            download_url="magnet:?xt=urn:btih:OVERSIZED",
            source="Prowlarr",
            size_bytes=30 * 1024**3,
            seeders=50,
            leechers=0,
        ),
        DiscoveryResult(
            title="Movie 1080p x265 valid",
            download_url="magnet:?xt=urn:btih:VALID",
            source="Prowlarr",
            size_bytes=10 * 1024**3,
            seeders=50,
            leechers=0,
        ),
    ]

    results = await orchestrate_searches("Movie", "movie", ctx, max_size_gib=44)

    assert len(fake_discovery_provider.calls) == 1
    assert fake_discovery_provider.calls[0].max_size_gib == 22.0
    assert [result["title"] for result in results] == ["Movie 1080p x265 valid"]


@pytest.mark.asyncio
async def test_orchestrate_searches_reuses_discovery_circuit_breaker(
    fake_discovery_provider,
):
    ctx = _ctx_with_config(providers_movies=[_provider()])
    fake_discovery_provider.results = [
        DiscoveryResult(
            title="Movie 1080p x265",
            download_url="magnet:?xt=urn:btih:FIRST",
            source="Prowlarr",
            size_bytes=1024**3,
            seeders=30,
            leechers=0,
        )
    ]

    first_results = await orchestrate_searches("Movie", "movie", ctx)
    second_results = await orchestrate_searches("Movie", "movie", ctx)

    assert first_results
    assert second_results
    assert "DISCOVERY_CIRCUIT_BREAKER" in ctx.bot_data
    assert len(fake_discovery_provider.calls) == 2


@pytest.mark.asyncio
async def test_orchestrate_searches_returns_empty_without_discovery_provider(caplog):
    ctx = _ctx_with_config(
        providers_movies=[
            _provider(
                name="Legacy TPB Config",
                type="",
                search_url="https://example.invalid/search?q={query}",
            )
        ]
    )

    caplog.set_level(logging.WARNING, logger="telegram_bot.config")
    results = await orchestrate_searches("Movie", "movie", ctx)

    assert results == []
    assert "Legacy torrent scrapers have been removed" in caplog.text


@pytest.mark.asyncio
async def test_orchestrate_searches_logs_discovery_cancellation(
    cancelled_discovery_provider,
    caplog,
):
    ctx = _ctx_with_config(providers_movies=[_provider("cancel_discovery")])

    caplog.set_level(logging.INFO, logger="telegram_bot.config")
    with pytest.raises(asyncio.CancelledError):
        await orchestrate_searches("Movie", "movie", ctx)

    assert "Discovery search cancelled for 'Movie' (movie)." in caplog.text
